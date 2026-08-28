from __future__ import annotations

import logging
import re
import signal
import time
import uuid
from dataclasses import asdict, dataclass

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


@dataclass(slots=True)
class CycleBudget:
    model_calls: int
    tool_calls: int


@dataclass(slots=True)
class TaskBudget:
    max_model_calls: int
    max_tool_calls: int
    max_tokens: int
    max_cycles: int
    used_model_calls: int = 0
    used_tool_calls: int = 0
    used_tokens: int = 0
    used_cycles: int = 0

    def remaining(self) -> dict[str, int]:
        return {
            "model_calls": max(0, self.max_model_calls - self.used_model_calls),
            "tool_calls": max(0, self.max_tool_calls - self.used_tool_calls),
            "tokens": max(0, self.max_tokens - self.used_tokens),
            "cycles": max(0, self.max_cycles - self.used_cycles),
        }


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
            scientific_available=(self.sandbox.scientific_environment / ".aios-environment.json").is_file(),
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
            continuation = event.type == "TASK_CONTINUE" or bool(event.payload.get("continuation"))
            task = self.store.start_task_attempt(int(task.id), increment_attempt=not continuation)
            task_budget = self._task_budget(int(task.id))
            task_metrics = self._task_metrics(int(task.id))
            working_state = self._task_working_state(int(task.id), task.request)
            seen_context_hashes = set(working_state.pop("_seen_context_hashes", []))
            attribution_totals = working_state.pop("_attribution_totals", {
                "prompt_tokens": 0, "repeated_tokens": 0, "calls": 0,
            })
            task_budget.used_cycles += 1
            self.store.add_checkpoint(int(task.id), "continued" if continuation else "started", {
                "cycle_id": cycle_id, "attempt": task.attempts, "task_cycle": task_budget.used_cycles,
            })
            active_goals = self.goals.active()
            intent = self.arbiter.select(events, active_goals)
            if intent is None:
                self.store.finish_events(event_ids)
                return True
            self.store.trace(cycle_id, "intent_selected", asdict(intent))
            self.store.add_checkpoint(int(task.id), "intent", asdict(intent))

            expected_artifacts = self.verifier._expected_artifacts(task.request)
            contract = EvidenceContract.from_request(task.request, expected_artifacts)
            preflight_assessment = self.capabilities.assess(contract)
            preflight = {"contract": contract.as_dict(), "assessment": preflight_assessment}
            self.store.trace(cycle_id, "capability_preflight", preflight)
            self.store.add_checkpoint(int(task.id), "capability_preflight", preflight)
            if not preflight_assessment["satisfied"]:
                status = TaskStatus.NEEDS_AUTHORITY if preflight_assessment["needs_authority"] else TaskStatus.BLOCKED_CAPABILITY
                reason = "Required authority is missing" if preflight_assessment["needs_authority"] else "Required capability is unavailable"
                result = {"cycle_id": cycle_id, "summary": reason, "capability_preflight": preflight, "evidence": {"success": False}}
                self.store.finish_events(event_ids)
                self.store.update_task(int(task.id), status, result=result, error=reason)
                self.store.add_checkpoint(int(task.id), status.value, result)
                LOGGER.warning("Task %s stopped at preflight: %s", task.id, status.value)
                return True

            snapshot = self.sandbox.prepare(int(task.id), self.settings.workspace)
            environment_observation = None
            if any(item.name == "execution.python.scientific" for item in contract.capabilities):
                environment_observation = self.sandbox.ensure_scientific_environment()
                self.store.trace(cycle_id, "dependency_environment", environment_observation)
                self.store.add_checkpoint(int(task.id), "dependency_environment", environment_observation)
                if not environment_observation.get("ready"):
                    raise RuntimeError(str(environment_observation.get("error") or "Scientific environment provisioning failed"))
                task_metrics["dependency_provision_latency_ms"] += float(environment_observation.get("latency_ms", 0.0))
                working_state["execution_environment"] = {"scientific_python": "ready"}
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
            full_workspace_inventory = self._workspace_inventory(snapshot)
            full_capabilities = self.capabilities.as_dict()
            full_skills = self.skills.catalog(self.capabilities) if self.settings.skills.enabled else []
            context["workspace_inventory"] = full_workspace_inventory
            context["evidence_contract"] = contract.as_dict()
            context["capabilities"] = full_capabilities
            context["skills"] = full_skills
            skill_authoring = (
                self.skills.authoring_context(task.request) if self.settings.skills.enabled else None
            )
            if skill_authoring is not None:
                context["skill_authoring"] = skill_authoring
            context["harness"] = harness_settings
            context["harness_version"] = harness.get("version")
            context["continuation"] = self._continuation_context(int(task.id)) if continuation else None
            context["task_working_state"] = self._working_state_projection(working_state)
            if environment_observation is not None:
                context["environment"] = {"scientific_python": environment_observation}
            self.store.trace(cycle_id, "context_composed", context)
            harness_action_cap = harness_settings.get(
                "max_actions_per_cycle", self.settings.max_actions_per_cycle
            )
            cycle_budget = CycleBudget(
                model_calls=max(1, self.settings.budget.max_model_calls_per_cycle),
                tool_calls=max(1, self.settings.budget.max_tool_calls_per_cycle),
            )
            task_remaining = task_budget.remaining()
            action_cap = min(
                int(harness_action_cap),
                cycle_budget.tool_calls,
                task_remaining["tool_calls"],
            )
            model_round_cap = min(cycle_budget.model_calls, task_remaining["model_calls"])
            if model_round_cap <= 0:
                raise RuntimeError("TaskBudget exhausted before a terminal answer")
            all_actions = []
            all_results = []
            skill_sequence = 0
            rounds = []
            observations = []
            protocol_messages = []
            planned_count = 0
            task_done = False
            final_summary = ""
            completion_metadata = None
            budget_truncated = False
            budget_deferred = 0
            for round_number in range(1, model_round_cap + 1):
                first_task_call = task_budget.used_model_calls == 0 and round_number == 1
                if not first_task_call:
                    context["workspace_inventory"] = self._relevant_workspace_map(full_workspace_inventory, working_state)
                    context["capabilities"] = self._differential_capabilities(full_capabilities)
                    context["skills"] = self._differential_skills(full_skills)
                    context["retrieved_memories"] = []
                    context["characters"] = 0
                    if environment_observation is not None:
                        context["environment"] = {"scientific_python": "ready"}
                context["task_working_state"] = self._working_state_projection(working_state)
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
                    "scope": "cycle_and_task",
                    "model_call": round_number,
                    "max_model_calls_this_cycle": model_round_cap,
                    "remaining_model_calls_after_this": model_round_cap - round_number,
                    "used_tool_calls": len(all_actions),
                    "remaining_tool_calls": remaining_before_round,
                    "task": {
                        "used_model_calls_before_cycle": task_budget.used_model_calls,
                        "used_tool_calls_before_cycle": task_budget.used_tool_calls,
                        "used_tokens_before_cycle": task_budget.used_tokens,
                        "cycle": task_budget.used_cycles,
                        "remaining_before_cycle": task_remaining,
                    },
                    "force_final": (
                        task_budget.used_model_calls + model_calls_used + 1 >= task_budget.max_model_calls
                        or task_budget.used_tokens + model_tokens_total >= task_budget.max_tokens
                        or task_budget.used_cycles >= task_budget.max_cycles
                    ),
                    "soft_pressure": (
                        task_budget.used_model_calls + model_calls_used >= self.settings.budget.soft_model_calls_per_task
                        or task_budget.used_tokens + model_tokens_total >= self.settings.budget.soft_tokens_per_task
                    ),
                    "reserved_completion_tool_calls": reserved,
                    "protocol_repairs_remaining": 1,
                    "required_artifacts": expected_artifacts,
                    "instruction": (
                        "Stop broad inspection before the reserved count and create the requested artifact. "
                        "A cycle boundary will checkpoint and continue; only force_final marks a real task terminal budget."
                    ),
                }
                context["_protocol_messages"] = protocol_messages
                plan = self.controller.plan(intent, active_goals, context)
                model_usage = plan.model_usage or {}
                model_calls_used += int(model_usage.get("model_calls", 1))
                model_tokens_total += int(model_usage.get("total_tokens", 0))
                for attribution in plan.model_attributions:
                    repeated_tokens = 0
                    for block in attribution.get("blocks", {}).values():
                        repeated = block.get("sha256") in seen_context_hashes
                        block["repeated"] = repeated
                        if repeated:
                            repeated_tokens += int(block.get("attributed_tokens", block.get("estimated_tokens", 0)))
                        seen_context_hashes.add(str(block.get("sha256")))
                    prompt_tokens = int(attribution.get("actual_prompt_tokens") or attribution.get("estimated_prompt_tokens", 0))
                    attribution["repeated_tokens"] = repeated_tokens
                    attribution["context_reuse_ratio"] = repeated_tokens / prompt_tokens if prompt_tokens else 0.0
                    attribution_totals["prompt_tokens"] += prompt_tokens
                    attribution_totals["repeated_tokens"] += repeated_tokens
                    attribution_totals["calls"] += 1
                    self.store.trace(cycle_id, "model_call_attribution", {
                        "task_id": int(task.id), "task_cycle": task_budget.used_cycles,
                        "round": round_number, **attribution,
                    })
                if plan.protocol_message:
                    protocol_messages.append(plan.protocol_message)
                final_summary = plan.summary
                completion_metadata = plan.completion_metadata
                working_state["semantic_state"] = {"latest_model_summary": plan.summary[:2000]}
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
                    "completion_metadata": completion_metadata,
                }
                rounds.append(plan_data)
                self.store.trace(cycle_id, "plan_created", plan_data)
                self.store.add_checkpoint(int(task.id), f"planned_round_{round_number}", plan_data)

                round_results = []
                for action in actions:
                    if action.tool == "bash" and task_metrics["rounds_to_first_computation"] is None:
                        task_metrics["rounds_to_first_computation"] = task_budget.used_model_calls + round_number
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
                    result_trace_id = self.store.trace(cycle_id, "action_result", result_data)
                    self._update_working_state(working_state, action, result, result_trace_id)
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
                                "content": self._tool_result_content(
                                    result,
                                    limit=self.settings.budget.tool_observation_characters,
                                    reference=result_trace_id,
                                ),
                            }
                        )
                        self._compact_protocol_messages(
                            protocol_messages, self.settings.budget.hot_tool_results
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
                                "content": self._tool_result_content(
                                    deferred_result,
                                    limit=self.settings.budget.tool_observation_characters,
                                ),
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
                self._retain_hot_protocol_rounds(
                    protocol_messages, self.settings.budget.hot_tool_results
                )
                has_final_action = any(action.tool in {"write", "edit", "echo", "write_file", "append_file"} for action in actions)
                task_done = bool(plan.done and (has_final_action or not actions))
                if task_done:
                    working_state["pending"] = []
                if task_done:
                    break
                if not actions and not deferred_actions:
                    break
                if len(all_actions) >= action_cap:
                    break

            task_budget.used_model_calls += model_calls_used
            task_budget.used_tool_calls += len(all_results)
            task_budget.used_tokens += model_tokens_total
            remaining_task_budget = task_budget.remaining()
            written_artifact_names = {
                str(result.output.get("path", "")).replace("\\", "/").rsplit("/", 1)[-1].casefold()
                for action, result in zip(all_actions, all_results, strict=False)
                if action.tool in {"write", "write_file", "edit", "append_file"}
                and result.ok and isinstance(result.output, dict)
            }
            artifact_ready = bool(expected_artifacts) and all(
                name.casefold() in written_artifact_names for name in expected_artifacts
            )
            planner_claimed_done = bool(rounds and rounds[-1].get("done"))
            made_progress = any(result.ok for result in all_results)
            cycle_exhausted = bool(
                not task_done
                and (
                    len(rounds) >= model_round_cap
                    or len(all_actions) >= action_cap
                    or budget_truncated
                )
            )
            can_continue = bool(
                cycle_exhausted
                and made_progress
                and not planner_claimed_done
                and not artifact_ready
                and remaining_task_budget["model_calls"] > 0
                and remaining_task_budget["tool_calls"] > 0
                and remaining_task_budget["tokens"] > 0
                and remaining_task_budget["cycles"] > 0
            )
            if can_continue:
                checkpoint = {
                    "cycle_id": cycle_id,
                    "summary": final_summary,
                    "budget": asdict(task_budget),
                    "remaining": remaining_task_budget,
                    "working_state": {
                        **working_state,
                        "_seen_context_hashes": sorted(seen_context_hashes),
                        "_attribution_totals": attribution_totals,
                    },
                    "metrics": task_metrics,
                    "action_trace_ids": [
                        item["id"] for item in self.store.recent_traces(limit=max(50, len(all_results) * 3))
                        if item["cycle_id"] == cycle_id and item["kind"] == "action_result"
                    ],
                }
                committed = self.sandbox.commit(self.settings.workspace)
                checkpoint["committed_files"] = committed
                checkpoint_id = self.store.add_checkpoint(int(task.id), "budget_deferred", checkpoint)
                deferred_result = {
                    "cycle_id": cycle_id,
                    "summary": "Cycle budget reached; task checkpointed for continuation",
                    "checkpoint_id": checkpoint_id,
                    "task_budget": asdict(task_budget),
                    "committed_files": committed,
                    "evidence": {"success": False, "terminal": False, "budget_deferred": True},
                }
                self.store.finalize_skill_usage(
                    cycle_id, verifier_passed=None, task_outcome="budget_deferred",
                    model_calls_after=task_budget.used_model_calls,
                    tokens_after=task_budget.used_tokens,
                )
                self.store.finish_events(event_ids)
                self.store.update_task(int(task.id), TaskStatus.DEFERRED, result=deferred_result)
                continue_id = self.store.add_event(Event(
                    "TASK_CONTINUE",
                    {"task_id": int(task.id), "message": task.request, "continuation": True, "checkpoint_id": checkpoint_id},
                    task.priority,
                ))
                self.store.trace(cycle_id, "budget_deferred", {
                    "task_id": int(task.id), "checkpoint_id": checkpoint_id,
                    "continuation_event_id": continue_id, "remaining": remaining_task_budget,
                })
                LOGGER.info(
                    "Task %s deferred at cycle budget; checkpoint %s, continuation event %s queued",
                    task.id, checkpoint_id, continue_id,
                )
                return True

            final_output = self._final_output(final_summary, all_actions, all_results)
            verification = self.verifier.verify(
                all_actions,
                all_results,
                planned_count=planned_count,
                task_done=task_done,
                request=task.request,
                contract=contract,
                final_output=final_output,
                capability_assessment=preflight_assessment,
                completion_metadata=completion_metadata,
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
                "model_api_calls": task_budget.used_model_calls,
                "model_tokens": task_budget.used_tokens,
                "cycle_model_api_calls": model_calls_used,
                "cycle_model_tokens": model_tokens_total,
                "task_cycles": task_budget.used_cycles,
                "rounds_to_first_computation": task_metrics["rounds_to_first_computation"],
                "dependency_provision_latency_ms": task_metrics["dependency_provision_latency_ms"],
                "prompt_token_attribution": attribution_totals,
                "context_reuse_ratio": (
                    attribution_totals["repeated_tokens"] / attribution_totals["prompt_tokens"]
                    if attribution_totals["prompt_tokens"] else 0.0
                ),
                "planned_actions": planned_count,
                "executed_actions": len(all_results),
                "failed_actions": sum(not result.ok for result in all_results),
                "budget_truncated": budget_truncated,
                "budget_deferred_actions": budget_deferred,
                "verification": verification,
                "completion_metadata": completion_metadata,
                "result_vector": verification.get("result_vector"),
            }
            self.store.trace(cycle_id, "evaluation", evidence)
            task_result = {
                "cycle_id": cycle_id,
                "summary": final_summary,
                "final_output": final_output,
                "rounds": rounds,
                "actions": [asdict(action) for action in all_actions],
                "action_results": [asdict(result) for result in all_results],
                "task_working_state": self._working_state_projection(working_state),
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
                self.sandbox.purge_task_dependencies(int(task.id))
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
                self.sandbox.purge_task_dependencies(int(task.id))
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
    def _workspace_inventory(snapshot: object, limit: int = 200) -> dict[str, object]:
        """Expose bounded path metadata so model rounds are not wasted rediscovering files."""
        from pathlib import Path

        root = Path(snapshot)
        entries: list[dict[str, object]] = []
        total_files = 0
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative == ".aios" or relative.startswith(".aios/"):
                continue
            total_files += 1
            if len(entries) < limit:
                entries.append({"path": relative, "size": path.stat().st_size})
        return {
            "files": entries,
            "total_files": total_files,
            "truncated": total_files > len(entries),
            "instruction": "Use these paths directly; do not rediscover them with ls/find/file.",
        }

    @staticmethod
    def _tool_result_content(
        result: object, limit: int = 12_000, reference: int | None = None,
    ) -> str:
        import json

        value = asdict(result)
        if reference is not None:
            value["observation_ref"] = f"trace:{reference}"
        content = json.dumps(value, ensure_ascii=False, default=str)
        if len(content) <= limit:
            return content
        return content[:limit] + "\n[tool result truncated by AIOS]"

    @staticmethod
    def _compact_protocol_messages(messages: list[dict], hot_results: int) -> None:
        import json

        tool_indexes = [index for index, item in enumerate(messages) if item.get("role") == "tool"]
        for index in tool_indexes[:-max(1, hot_results)]:
            item = messages[index]
            content = str(item.get("content", ""))
            if '"compacted": true' in content:
                continue
            try:
                value = json.loads(content)
            except json.JSONDecodeError:
                value = {}
            item["content"] = json.dumps({
                "compacted": True,
                "tool": value.get("tool"),
                "ok": value.get("ok"),
                "error": value.get("error"),
                "observation_ref": value.get("observation_ref"),
                "summary": "Large/old tool result moved out of HOT context; query the trace reference if needed.",
            }, ensure_ascii=False)

    @classmethod
    def _compact_observations(cls, observations: list[dict], limit: int = 6) -> list[dict]:
        compact: list[dict] = []
        for item in observations[-limit:]:
            action = item.get("action", {})
            result = item.get("result", {})
            output = result.get("output")
            if isinstance(output, dict):
                output_summary = {
                    key: value for key, value in output.items()
                    if key in {"exit_code", "changes", "path", "bytes", "resource"}
                }
                text = str(output_summary)
            else:
                text = str(output)
            compact.append({
                "round": item.get("round"),
                "tool": action.get("tool"),
                "arguments": cls._bounded_value(action.get("arguments", {}), 1000),
                "ok": result.get("ok"),
                "error": cls._bounded_value(result.get("error"), 1000),
                "output_summary": text[:2000],
            })
        return compact

    @staticmethod
    def _bounded_value(value: object, limit: int) -> object:
        if isinstance(value, str):
            return value[:limit] + ("…" if len(value) > limit else "")
        if isinstance(value, dict):
            return {str(key): AIOSRuntime._bounded_value(item, max(100, limit // max(1, len(value)))) for key, item in value.items()}
        if isinstance(value, list):
            return [AIOSRuntime._bounded_value(item, max(100, limit // max(1, len(value)))) for item in value[:20]]
        return value

    def _task_budget(self, task_id: int) -> TaskBudget:
        config = self.settings.budget
        budget = TaskBudget(
            max_model_calls=config.max_model_calls_per_task,
            max_tool_calls=config.max_tool_calls_per_task,
            max_tokens=config.max_tokens_per_task,
            max_cycles=config.max_cycles_per_task,
        )
        checkpoints = self.store.task_checkpoints(task_id)
        for checkpoint in reversed(checkpoints):
            if checkpoint["phase"] == "retry_reset":
                break
            if checkpoint["phase"] != "budget_deferred":
                continue
            saved = checkpoint["data"].get("budget", {})
            budget.used_model_calls = int(saved.get("used_model_calls", 0))
            budget.used_tool_calls = int(saved.get("used_tool_calls", 0))
            budget.used_tokens = int(saved.get("used_tokens", 0))
            budget.used_cycles = int(saved.get("used_cycles", 0))
            break
        return budget

    def _continuation_context(self, task_id: int) -> dict | None:
        for checkpoint in reversed(self.store.task_checkpoints(task_id)):
            if checkpoint["phase"] == "retry_reset":
                break
            if checkpoint["phase"] == "budget_deferred":
                data = checkpoint["data"]
                return {
                    "checkpoint_id": checkpoint["id"],
                    "previous_cycle_id": data.get("cycle_id"),
                    "fresh_context": True,
                    "instruction": "Restore TaskWorkingState in a fresh context. DONE/ESTABLISHED work must not be repeated without conflicting evidence.",
                }
        return None

    def _task_metrics(self, task_id: int) -> dict[str, object]:
        metrics: dict[str, object] = {
            "rounds_to_first_computation": None,
            "dependency_provision_latency_ms": 0.0,
        }
        for checkpoint in reversed(self.store.task_checkpoints(task_id)):
            if checkpoint["phase"] == "retry_reset":
                break
            if checkpoint["phase"] == "budget_deferred":
                saved = checkpoint["data"].get("metrics", {})
                if isinstance(saved, dict):
                    metrics.update({key: saved[key] for key in metrics if key in saved})
                break
        return metrics

    def _task_working_state(self, task_id: int, objective: str) -> dict[str, object]:
        for checkpoint in reversed(self.store.task_checkpoints(task_id)):
            if checkpoint["phase"] == "retry_reset":
                break
            if checkpoint["phase"] == "budget_deferred":
                state = checkpoint["data"].get("working_state")
                if isinstance(state, dict):
                    return state
        return {
            "objective": objective,
            "established_facts": [],
            "completed_steps": [],
            "available_artifacts": [],
            "accessed_resources": [],
            "execution_environment": {},
            "pending": ["fulfil_objective_and_verify"],
            "important_evidence_refs": [],
            "semantic_state": {},
        }

    def _working_state_projection(self, state: dict[str, object]) -> dict[str, object]:
        projected = {key: value for key, value in state.items() if not key.startswith("_")}
        import json
        raw = json.dumps(projected, ensure_ascii=False, default=str)
        limit = self.settings.budget.working_state_characters
        if len(raw) <= limit:
            return projected
        return {
            "objective": projected.get("objective"),
            "established_facts": list(projected.get("established_facts", []))[-12:],
            "completed_steps": list(projected.get("completed_steps", []))[-16:],
            "available_artifacts": list(projected.get("available_artifacts", []))[-12:],
            "accessed_resources": list(projected.get("accessed_resources", []))[-12:],
            "execution_environment": projected.get("execution_environment", {}),
            "pending": projected.get("pending", []),
            "important_evidence_refs": list(projected.get("important_evidence_refs", []))[-16:],
            "semantic_state": projected.get("semantic_state", {}),
            "compacted": True,
        }

    @staticmethod
    def _update_working_state(state: dict[str, object], action: object, result: object, trace_id: int) -> None:
        import hashlib

        tool = str(getattr(action, "tool", ""))
        arguments = getattr(action, "arguments", {})
        ok = bool(getattr(result, "ok", False))
        reference = f"trace:{trace_id}"
        refs = state.setdefault("important_evidence_refs", [])
        if reference not in refs:
            refs.append(reference)
            del refs[:-16]
        if not ok:
            return
        path = str(arguments.get("path", ""))
        if tool == "read" and path:
            resources = state.setdefault("accessed_resources", [])
            if path not in resources:
                resources.append(path)
            output = getattr(result, "output", None)
            resource = output.get("resource", {}) if isinstance(output, dict) else {}
            metadata = resource.get("metadata")
            if metadata:
                fact = {"state": "ESTABLISHED", "resource": path, "metadata": metadata, "evidence_ref": reference}
                facts = state.setdefault("established_facts", [])
                facts[:] = [item for item in facts if not (isinstance(item, dict) and item.get("resource") == path)]
                facts.append(fact)
        if tool in {"write", "edit", "write_file", "append_file"} and path:
            artifacts = state.setdefault("available_artifacts", [])
            artifact = {"path": path, "state": "AVAILABLE", "evidence_ref": reference}
            artifacts[:] = [item for item in artifacts if not (isinstance(item, dict) and item.get("path") == path)]
            artifacts.append(artifact)
        if tool == "bash":
            command = str(arguments.get("command", ""))
            step = "bash:" + hashlib.sha256(command.encode("utf-8")).hexdigest()[:12]
        else:
            step = f"{tool}:{path or 'operation'}"
        completed = state.setdefault("completed_steps", [])
        if not any(isinstance(item, dict) and item.get("step") == step for item in completed):
            completed.append({"step": step, "state": "DONE", "evidence_ref": reference})

    @staticmethod
    def _relevant_workspace_map(inventory: dict[str, object], state: dict[str, object]) -> dict[str, object]:
        paths = set(str(item) for item in state.get("accessed_resources", []))
        paths.update(
            str(item.get("path")) for item in state.get("available_artifacts", [])
            if isinstance(item, dict) and item.get("path")
        )
        entries = [item for item in inventory.get("files", []) if isinstance(item, dict) and item.get("path") in paths]
        return {
            "relevant_resources": entries,
            "other_files_available": max(0, int(inventory.get("total_files", 0)) - len(entries)),
            "instruction": "Use relevant resources directly. Request a directory read only if another path is actually needed.",
        }

    @staticmethod
    def _differential_capabilities(capabilities: dict[str, object]) -> dict[str, object]:
        return {
            name: {"state": value.get("state"), "interface": value.get("interface")}
            for name, value in capabilities.items() if isinstance(value, dict)
        }

    @staticmethod
    def _differential_skills(skills: list[dict]) -> list[dict[str, object]]:
        return [
            {key: item.get(key) for key in ("name", "version", "status") if key in item}
            for item in skills
        ]

    @staticmethod
    def _retain_hot_protocol_rounds(messages: list[dict], rounds: int) -> None:
        assistant_indexes = [index for index, item in enumerate(messages) if item.get("role") == "assistant"]
        if len(assistant_indexes) <= max(1, rounds):
            return
        cut = assistant_indexes[-max(1, rounds)]
        del messages[:cut]

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
            self.sandbox.purge_task_dependencies(task_id)
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
