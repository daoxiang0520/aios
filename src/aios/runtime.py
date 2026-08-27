from __future__ import annotations

import logging
import re
import signal
import time
import uuid
from dataclasses import asdict

from .config import Settings
from .capabilities import CapabilityRegistry, EvidenceContract
from .controller import ControllerError, LLMController
from .evolution import AutonomousEvolutionEngine
from .evaluation import Verifier
from .goals import GoalManager, IntentArbiter
from .memory import ContextComposer, MemoryManager
from .plugins import PluginManager
from .security import SecurityKernel
from .sandbox import DockerSandboxBroker, SandboxPolicyError
from .skills import SkillManager
from .storage import StateStore
from .tools import ToolExecutor, ToolRegistry
from .types import ActionResult, Event, MemoryType, Task, TaskStatus

LOGGER = logging.getLogger("aios.runtime")


class AIOSRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.ensure_directories()
        self.store = StateStore(settings.database)
        self.store.initialize()
        self.goals = GoalManager(self.store)
        self.arbiter = IntentArbiter()
        self.controller = LLMController(settings.model)
        self.memories = MemoryManager(self.store)
        self.context = ContextComposer(self.memories)
        self.verifier = Verifier()
        self.plugins = PluginManager(settings.extensions, self.store, settings.workspace)
        self.skills = SkillManager(settings.skills_root, settings.skills)
        if settings.skills.enabled:
            self.skills.bootstrap_builtins()
        self.sandbox = DockerSandboxBroker(
            settings.sandbox_root, settings.sandbox, self.skills.runtime,
            network_enabled=settings.capabilities.network_enabled,
        )
        self.capabilities = CapabilityRegistry.default(
            sandbox_available=self.sandbox.available(),
            network_enabled=settings.capabilities.network_enabled,
            allowed_domains=settings.capabilities.allowed_domains,
        )
        registry = ToolRegistry(settings.permissions, self.plugins, self.sandbox)
        self.controller.set_tool_schemas(registry.schemas())
        self.security = SecurityKernel(settings.workspace, settings.permissions)
        self.executor = ToolExecutor(registry, self.security)
        self.evolution = AutonomousEvolutionEngine(
            self.store, self.plugins, settings.evolution
        )
        self.shutdown_requested = False

    def request_shutdown(self, *_: object) -> None:
        self.shutdown_requested = True

    def run_forever(self) -> None:
        self.store.recover_processing_events()
        signal.signal(signal.SIGINT, self.request_shutdown)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self.request_shutdown)
        LOGGER.info("AIOS started; database=%s workspace=%s", self.settings.database, self.settings.workspace)
        while not self.shutdown_requested:
            worked = self.run_once()
            if not worked:
                time.sleep(self.settings.poll_interval_seconds)
        LOGGER.info("AIOS stopped")

    def run_once(self) -> bool:
        model_calls_used = 0
        model_tokens_total = 0
        events = self.store.claim_events(limit=1)
        if not events:
            return False
        cycle_id = uuid.uuid4().hex
        event_ids = [int(event.id) for event in events if event.id is not None]
        self.store.trace(cycle_id, "cycle_started", {"events": [asdict(event) for event in events]})
        event = events[0]
        task: Task | None = None
        try:
            task = self._load_or_create_task(event)
            task = self.store.start_task_attempt(int(task.id))
            self.store.add_checkpoint(int(task.id), "started", {"cycle_id": cycle_id, "attempt": task.attempts})
            active_goals = self.goals.active()
            intent = self.arbiter.select(events, active_goals)
            if intent is None:
                self.store.finish_events(event_ids)
                return True
            self.store.trace(cycle_id, "intent_selected", asdict(intent))
            self.store.add_checkpoint(int(task.id), "intent", asdict(intent))

            expected_artifacts = self.verifier._expected_artifacts(task.request)
            contract = EvidenceContract.from_request(task.request, expected_artifacts)
            assessment = self.capabilities.assess(contract)
            preflight = {"contract": contract.as_dict(), "assessment": assessment}
            self.store.trace(cycle_id, "capability_preflight", preflight)
            self.store.add_checkpoint(int(task.id), "capability_preflight", preflight)
            if not assessment["satisfied"]:
                status = TaskStatus.NEEDS_AUTHORITY if assessment["needs_authority"] else TaskStatus.BLOCKED_CAPABILITY
                reason = "Required authority is missing" if assessment["needs_authority"] else "Required capability is unavailable"
                result = {"cycle_id": cycle_id, "summary": reason, "capability_preflight": preflight, "evidence": {"success": False}}
                self.store.finish_events(event_ids)
                self.store.update_task(int(task.id), status, result=result, error=reason)
                self.store.add_checkpoint(int(task.id), status.value, result)
                LOGGER.warning("Task %s stopped at preflight: %s", task.id, status.value)
                return True

            snapshot = self.sandbox.prepare(int(task.id), self.settings.workspace)
            self.sandbox.expose_read_only_state({
                "tasks": [asdict(item) for item in self.store.list_tasks(limit=100)],
                "traces": self.store.recent_traces(limit=100),
                "dead-letters": self.store.list_dead_letters(limit=100),
                "memory": [asdict(item) for item in self.store.list_memories(limit=100)],
                "skill-usage": self.store.list_skill_usage(limit=100),
                "capabilities": self.capabilities.as_dict(),
                "skills": self.skills.catalog(self.capabilities) if self.settings.skills.enabled else [],
            })
            self.security.workspace = snapshot.resolve()

            harness = self.store.active_harness()
            harness_settings = harness.get("settings", {})
            memory_limit = harness_settings.get("memory_context_characters")
            if isinstance(memory_limit, int):
                self.context.max_characters = memory_limit
            context = self.context.compose(task.request)
            context["evidence_contract"] = contract.as_dict()
            context["capabilities"] = self.capabilities.as_dict()
            context["skills"] = (
                self.skills.catalog(self.capabilities) if self.settings.skills.enabled else []
            )
            skill_authoring = (
                self.skills.authoring_context(task.request) if self.settings.skills.enabled else None
            )
            if skill_authoring is not None:
                context["skill_authoring"] = skill_authoring
            context["harness"] = harness_settings
            context["harness_version"] = harness.get("version")
            self.store.trace(cycle_id, "context_composed", context)
            harness_action_cap = harness_settings.get(
                "max_actions_per_cycle", self.settings.max_actions_per_cycle
            )
            action_cap = min(
                int(harness_action_cap),
                self.settings.budget.max_tool_calls_per_cycle,
            )
            model_round_cap = max(1, self.settings.budget.max_model_calls_per_cycle)
            all_actions = []
            all_results = []
            skill_sequence = 0
            rounds = []
            observations = []
            protocol_messages = []
            planned_count = 0
            task_done = False
            final_summary = ""
            budget_truncated = False
            budget_deferred = 0
            for round_number in range(1, model_round_cap + 1):
                remaining_before_round = max(0, action_cap - len(all_actions))
                artifact_written = any(
                    action.tool in {"write", "write_file"} and result.ok
                    for action, result in zip(all_actions, all_results, strict=False)
                )
                skill_candidate_written = self._skill_candidate_written(all_actions, all_results)
                reserved = (
                    min(
                        max(
                            2 if skill_authoring is not None and not skill_candidate_written else 0,
                            max(0, self.settings.budget.reserved_completion_tool_calls),
                        ),
                        remaining_before_round,
                    )
                    if (expected_artifacts and not artifact_written)
                    or (skill_authoring is not None and not skill_candidate_written)
                    else 0
                )
                context["observations"] = observations
                context["round"] = round_number
                context["budget"] = {
                    "model_call": round_number,
                    "max_model_calls": model_round_cap,
                    "remaining_model_calls_after_this": model_round_cap - round_number,
                    "used_tool_calls": len(all_actions),
                    "remaining_tool_calls": remaining_before_round,
                    "reserved_completion_tool_calls": reserved,
                    "protocol_repairs_remaining": 1,
                    "required_artifacts": expected_artifacts,
                    "instruction": (
                        "When remaining calls reach the reserved count, stop inspection and create the requested artifact. "
                        "The final model call cannot use tools and must synthesize a final answer from existing observations."
                    ),
                }
                context["_protocol_messages"] = protocol_messages
                plan = self.controller.plan(intent, active_goals, context)
                model_usage = plan.model_usage or {}
                model_calls_used += int(model_usage.get("model_calls", 1))
                model_tokens_total += int(model_usage.get("total_tokens", 0))
                if plan.protocol_message:
                    protocol_messages.append(plan.protocol_message)
                final_summary = plan.summary
                actions, deferred_actions = self._select_actions(
                    plan.actions,
                    remaining_before_round,
                    reserved,
                )
                if skill_authoring is not None and not skill_candidate_written:
                    authoring_writes = [
                        action for action in actions if action.tool in {"write", "write_file"}
                    ]
                    deferred_actions = [
                        *[action for action in actions if action.tool not in {"write", "write_file"}],
                        *deferred_actions,
                    ]
                    actions = authoring_writes
                planned_count += len(actions)
                budget_deferred += len(deferred_actions)
                budget_truncated = budget_truncated or bool(deferred_actions)
                plan_data = {
                    "round": round_number,
                    "summary": plan.summary,
                    "done": plan.done,
                    "actions": [asdict(action) for action in actions],
                }
                rounds.append(plan_data)
                self.store.trace(cycle_id, "plan_created", plan_data)
                self.store.add_checkpoint(int(task.id), f"planned_round_{round_number}", plan_data)

                round_results = []
                for action in actions:
                    skill_invocation = None
                    if action.tool == "bash" and isinstance(action.arguments.get("command"), str):
                        try:
                            described = self.sandbox.describe_skill_invocation(action.arguments["command"])
                        except SandboxPolicyError:
                            described = None
                        if described is not None:
                            skill_sequence += 1
                            invocation_id = uuid.uuid4().hex
                            catalog_entry = next(
                                (
                                    item for item in self.skills.catalog(self.capabilities)
                                    if item["name"] == described["name"]
                                ),
                                None,
                            )
                            capability_assessment = (
                                catalog_entry["capability_assessment"] if catalog_entry is not None
                                else {"satisfied": False, "blocking": [{"name": "skill.manifest", "state": "missing"}], "needs_authority": []}
                            )
                            skill_invocation = {
                                "invocation_id": invocation_id,
                                "cycle_id": cycle_id,
                                "task_id": int(task.id),
                                "model_round": round_number,
                                "sequence_index": skill_sequence,
                                "skill_name": described["name"],
                                "skill_version": described["version"],
                                "input_digest": described["input_digest"],
                                "input_keys": described["input_keys"],
                                "required_capabilities": described["required_capabilities"],
                                "capability_assessment": capability_assessment,
                                "fallback_used": False,
                                "model_calls_before": model_calls_used,
                                "tokens_before": model_tokens_total,
                            }
                            self.store.trace(cycle_id, "SKILL_INVOKE", {
                                key: value for key, value in skill_invocation.items()
                                if key not in {"capability_assessment"}
                            })
                            self.store.trace(cycle_id, "SKILL_CAPABILITY_CHECK", {
                                "invocation_id": invocation_id,
                                "skill_name": described["name"],
                                "skill_version": described["version"],
                                "assessment": capability_assessment,
                            })
                    result = self.executor.execute(action)
                    all_actions.append(action)
                    all_results.append(result)
                    round_results.append(result)
                    result_data = {"round": round_number, **asdict(result)}
                    self.store.trace(cycle_id, "action_result", result_data)
                    if skill_invocation is not None:
                        output = result.output if isinstance(result.output, dict) else {}
                        exit_code = output.get("exit_code")
                        assessment = skill_invocation["capability_assessment"]
                        if result.ok:
                            skill_status = "success"
                        elif not assessment.get("satisfied", False):
                            skill_status = "blocked_capability"
                        else:
                            skill_status = "failed"
                        usage = {
                            **skill_invocation,
                            "status": skill_status,
                            "duration_ms": result.duration_ms,
                            "exit_code": exit_code,
                        }
                        usage_id = self.store.add_skill_usage(usage)
                        self.store.trace(cycle_id, "SKILL_RESULT", {
                            "usage_id": usage_id,
                            "invocation_id": usage["invocation_id"],
                            "skill_name": usage["skill_name"],
                            "skill_version": usage["skill_version"],
                            "status": skill_status,
                            "duration_ms": result.duration_ms,
                            "exit_code": exit_code,
                        })
                    if action.call_id:
                        protocol_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": action.call_id,
                                "content": self._tool_result_content(result),
                            }
                        )

                for action in deferred_actions:
                    deferred_result = ActionResult(
                        tool=action.tool,
                        ok=False,
                        error=(
                            "BudgetDeferred: tool call was not executed; stop broad inspection and use "
                            "the reserved call to complete the requested artifact"
                        ),
                    )
                    if action.call_id:
                        protocol_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": action.call_id,
                                "content": self._tool_result_content(deferred_result),
                            }
                        )
                    observations.append(
                        {
                            "round": round_number,
                            "action": asdict(action),
                            "result": asdict(deferred_result),
                            "deferred": True,
                        }
                    )

                observations.extend(
                    {
                        "round": round_number,
                        "action": asdict(action),
                        "result": asdict(result),
                    }
                    for action, result in zip(actions, round_results, strict=False)
                )
                has_final_action = any(action.tool in {"write", "edit", "echo", "write_file", "append_file"} for action in actions)
                task_done = bool(plan.done and (has_final_action or not actions))
                if task_done:
                    break
                if not actions and not deferred_actions:
                    break
                if len(all_actions) >= action_cap:
                    break

            final_output = self._final_output(final_summary, all_actions, all_results)
            verification = self.verifier.verify(
                all_actions,
                all_results,
                planned_count=planned_count,
                task_done=task_done,
                request=task.request,
                contract=contract,
                final_output=final_output,
            )
            ok = bool(verification["passed"])
            self.store.finalize_skill_usage(
                cycle_id,
                verifier_passed=ok,
                task_outcome=str(verification.get("outcome", "unknown")),
                model_calls_after=model_calls_used,
                tokens_after=model_tokens_total,
            )
            evidence = {
                "success": ok,
                "model_rounds": len(rounds),
                "model_api_calls": model_calls_used,
                "model_tokens": model_tokens_total,
                "planned_actions": planned_count,
                "executed_actions": len(all_results),
                "failed_actions": sum(not result.ok for result in all_results),
                "budget_truncated": budget_truncated,
                "budget_deferred_actions": budget_deferred,
                "verification": verification,
            }
            self.store.trace(cycle_id, "evaluation", evidence)
            task_result = {
                "cycle_id": cycle_id,
                "summary": final_summary,
                "final_output": final_output,
                "rounds": rounds,
                "actions": [asdict(action) for action in all_actions],
                "action_results": [asdict(result) for result in all_results],
                "evidence": evidence,
            }
            if ok:
                committed = self.sandbox.commit(self.settings.workspace)
                task_result["committed_files"] = committed
                task_result["final_output"] = self._published_output(final_output, snapshot)
                skill_candidates = (
                    self.skills.ingest_workspace_candidates(
                        self.settings.workspace,
                        self.sandbox,
                        source_task_id=int(task.id),
                        source_trace_ids=[
                            item["id"] for item in self.store.recent_traces(limit=500)
                            if item["cycle_id"] == cycle_id
                        ],
                    )
                    if self.settings.skills.enabled else []
                )
                if skill_candidates:
                    task_result["skill_candidates"] = skill_candidates
                    self.store.trace(cycle_id, "skill_candidates_ingested", {"candidates": skill_candidates})
                self.store.finish_events(event_ids)
                self.store.update_task(int(task.id), TaskStatus.COMPLETED, result=task_result)
                self.store.add_checkpoint(int(task.id), "completed", task_result)
                self.memories.remember(
                    MemoryType.EPISODIC,
                    f"Task '{task.title}' completed: {final_summary}",
                    key=f"task:{task.id}",
                    importance=0.6,
                    metadata={"task_id": task.id, "cycle_id": cycle_id},
                )
                LOGGER.info("Task %s completed: %s", task.id, final_summary)
            elif verification.get("outcome") == "degraded":
                committed = self.sandbox.commit(self.settings.workspace)
                task_result["committed_files"] = committed
                task_result["final_output"] = self._published_output(final_output, snapshot)
                self.store.finish_events(event_ids)
                self.store.update_task(int(task.id), TaskStatus.DEGRADED, result=task_result, error="Goal was only partially/substitutively satisfied")
                self.store.add_checkpoint(int(task.id), "degraded", task_result)
                LOGGER.warning("Task %s completed in degraded state; memory write suppressed", task.id)
            else:
                self.sandbox.discard()
                error = "; ".join(result.error or "unknown error" for result in all_results if not result.ok)
                if not error:
                    failed_checks = [
                        check["name"] for check in verification["checks"] if not check["passed"]
                    ]
                    error = "Verification failed: " + ", ".join(failed_checks)
                self._handle_failure(
                    task, event, event_ids, error, cycle_id, task_result,
                    model_calls_after=model_calls_used,
                    tokens_after=model_tokens_total,
                )
            return True
        except ControllerError as exc:
            self.sandbox.discard()
            LOGGER.warning("Cycle %s rejected model response: %s", cycle_id, exc)
            self.store.trace(cycle_id, "cycle_failed", {"error": f"{type(exc).__name__}: {exc}"})
            error = f"{type(exc).__name__}: {exc}"
            if task is None:
                self.store.finish_events(event_ids, error=error)
            else:
                self._handle_failure(
                    task, event, event_ids, error, cycle_id,
                    model_calls_after=model_calls_used,
                    tokens_after=model_tokens_total,
                )
            return True
        except Exception as exc:
            self.sandbox.discard()
            LOGGER.exception("Cycle %s failed", cycle_id)
            self.store.trace(cycle_id, "cycle_failed", {"error": f"{type(exc).__name__}: {exc}"})
            error = f"{type(exc).__name__}: {exc}"
            if task is None:
                self.store.finish_events(event_ids, error=error)
            else:
                self._handle_failure(
                    task, event, event_ids, error, cycle_id,
                    model_calls_after=model_calls_used,
                    tokens_after=model_tokens_total,
                )
            return True

    @staticmethod
    def _skill_candidate_written(actions: list, results: list) -> bool:
        packages: dict[str, set[str]] = {}
        for action, result in zip(actions, results, strict=False):
            if action.tool not in {"write", "write_file"} or not result.ok:
                continue
            raw_path = str(action.arguments.get("path", "")).replace("\\", "/").strip("/")
            match = re.fullmatch(
                r"skill_candidates/([a-z][a-z0-9_]{1,63})/(manifest\.json|skill\.py)",
                raw_path,
            )
            if match:
                packages.setdefault(match.group(1), set()).add(match.group(2))
        return any({"manifest.json", "skill.py"} <= files for files in packages.values())

    @staticmethod
    def _final_output(summary: str, actions: list, results: list) -> str:
        for action, result in reversed(list(zip(actions, results, strict=False))):
            if not result.ok:
                continue
            if action.tool == "echo" and isinstance(result.output, dict):
                message = result.output.get("message")
                if isinstance(message, str) and message:
                    return message
            if action.tool in {"write", "edit", "write_file"} and isinstance(result.output, dict):
                path = result.output.get("path")
                if path:
                    return str(path)
            if action.tool == "append_file" and isinstance(result.output, dict):
                path = result.output.get("path")
                if path:
                    return str(path)
        return summary

    def _published_output(self, output: str, snapshot: object) -> str:
        from pathlib import Path

        try:
            relative = Path(output).resolve().relative_to(Path(snapshot).resolve())
        except (OSError, ValueError):
            return output
        return str((self.settings.workspace / relative).resolve())

    @staticmethod
    def _tool_result_content(result: object, limit: int = 30_000) -> str:
        import json

        content = json.dumps(asdict(result), ensure_ascii=False, default=str)
        if len(content) <= limit:
            return content
        return content[:limit] + "\n[tool result truncated by AIOS]"

    def _load_or_create_task(self, event: Event) -> Task:
        task_id = event.payload.get("task_id")
        if task_id is not None:
            task = self.store.get_task(int(task_id))
            if task is None:
                raise KeyError(f"Event references unknown task: {task_id}")
            return task
        message = str(event.payload.get("message") or event.type)
        task = Task(title=message[:120], request=message, priority=event.priority)
        task.id = self.store.create_task(task)
        event.payload["task_id"] = task.id
        return task

    @staticmethod
    def _select_actions(
        requested: list,
        remaining: int,
        reserved_completion_calls: int,
    ) -> tuple[list, list]:
        if remaining <= 0:
            return [], list(requested)
        completion_tools = {"write", "edit", "write_file", "append_file", "echo"}
        completion = [action for action in requested if action.tool in completion_tools]
        inspection = [action for action in requested if action.tool not in completion_tools]
        selected: list = []
        selected.extend(completion[:remaining])
        inspection_slots = max(
            0,
            remaining - len(selected) - (reserved_completion_calls if not completion else 0),
        )
        selected.extend(inspection[:inspection_slots])
        selected_ids = {id(action) for action in selected}
        deferred = [action for action in requested if id(action) not in selected_ids]
        return selected, deferred

    def _handle_failure(
        self,
        task: Task,
        event: Event,
        event_ids: list[int],
        error: str,
        cycle_id: str,
        result: dict | None = None,
        model_calls_after: int | None = None,
        tokens_after: int | None = None,
    ) -> None:
        task_id = int(task.id)
        self.store.finalize_skill_usage(
            cycle_id,
            verifier_passed=False,
            task_outcome="failed_attempt",
            model_calls_after=model_calls_after,
            tokens_after=tokens_after,
        )
        self.store.finish_events(event_ids, error=error)
        self.store.add_checkpoint(task_id, "failed_attempt", {"error": error, "cycle_id": cycle_id})
        evolution_result = self.evolution.observe_failure(task, error, result)
        if evolution_result.get("triggered"):
            self.store.trace(cycle_id, "evolution_triggered", evolution_result)
            self.store.add_checkpoint(task_id, "evolution", evolution_result)
        if evolution_result.get("changed") or evolution_result.get("rolled_back_tools"):
            self._reload_generated_tools()
        terminal_protocol_failure = "Model protocol repair failed:" in error
        if task.attempts < task.max_attempts and not terminal_protocol_failure:
            self.store.update_task(task_id, TaskStatus.RETRYING, result=result, error=error)
            payload = dict(event.payload)
            payload["task_id"] = task_id
            retry_id = self.store.add_event(Event(event.type, payload, max(1, event.priority - 1)))
            self.store.trace(
                cycle_id,
                "retry_scheduled",
                {"task_id": task_id, "attempt": task.attempts, "max_attempts": task.max_attempts, "event_id": retry_id},
            )
            LOGGER.warning(
                "Task %s failed attempt %s/%s; retry event %s queued",
                task_id,
                task.attempts,
                task.max_attempts,
                retry_id,
            )
        else:
            if (
                not terminal_protocol_failure
                and evolution_result.get("changed")
                and self.settings.evolution.retry_after_evolution
            ):
                retry_id = self.store.retry_task(task_id)
                self.store.trace(
                    cycle_id,
                    "retry_after_evolution",
                    {"task_id": task_id, "event_id": retry_id, "evolution": evolution_result},
                )
                LOGGER.warning(
                    "Task %s received new capabilities and was re-queued as event %s",
                    task_id,
                    retry_id,
                )
                return
            self.store.update_task(task_id, TaskStatus.DEAD_LETTER, result=result, error=error)
            dead_id = self.store.add_dead_letter(task_id, event, error)
            self.store.trace(cycle_id, "dead_lettered", {"task_id": task_id, "dead_letter_id": dead_id})
            if terminal_protocol_failure and task.attempts < task.max_attempts:
                LOGGER.error("Task %s entered dead letter %s after terminal protocol repair failure", task_id, dead_id)
            else:
                LOGGER.error("Task %s exhausted retries and entered dead letter %s", task_id, dead_id)

    def _reload_generated_tools(self) -> None:
        registry = ToolRegistry(self.settings.permissions, self.plugins, self.sandbox)
        self.executor.registry = registry
        self.controller.set_tool_schemas(registry.schemas())
