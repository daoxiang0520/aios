from __future__ import annotations

import logging
import re
import signal
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .answers import CanonicalAnswer
from .config import Settings
from .capabilities import CapabilityRegistry, EvidenceContract
from .components import build_component_registry
from .controller import ControllerError, LLMController
from .evolution import AutonomousEvolutionEngine
from .evaluation import Verifier
from .goals import GoalManager, IntentArbiter
from .lineage import LineageManager
from .memory import ContextComposer, MemoryManager
from .plugins import PluginManager
from .runtime_provenance import RuntimeProvenanceManager
from .runtime_lock import RuntimeProcessLock
from .security import SecurityKernel
from .self_versioning import SelfVersionManager
from .situation import SituationResolver, coverage_labels, normalize_resource_path
from .sandbox import DockerSandboxBroker, SandboxPolicyError
from .skills import SkillManager
from .storage import StateStore
from .tools import ToolExecutor, ToolRegistry
from .types import Action, ActionResult, Event, MemoryType, Task, TaskStatus

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
    enabled: bool = True

    def remaining(self) -> dict[str, int | None]:
        if not self.enabled:
            return dict.fromkeys(("model_calls", "tool_calls", "tokens", "cycles"))
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
        self.self_versions = (
            SelfVersionManager(settings.self_root, settings.self_modification)
            if settings.self_modification.enabled else None
        )
        if self.self_versions is not None:
            self.self_versions.initialize()
        self.plugins = PluginManager(settings.extensions, self.store, settings.workspace)
        self.skills = SkillManager(settings.skills_root, settings.skills)
        if settings.skills.enabled and settings.skills.bootstrap_builtins:
            self.skills.bootstrap_builtins()
        self.sandbox = DockerSandboxBroker(
            settings.sandbox_root, settings.sandbox,
            None if self.self_versions is not None else self.skills.runtime,
            network_enabled=settings.capabilities.network_enabled,
            self_versions=self.self_versions,
        )
        self.capabilities = CapabilityRegistry.default(
            sandbox_available=self.sandbox.available(),
            network_enabled=settings.capabilities.network_enabled,
            allowed_domains=settings.capabilities.allowed_domains,
            scientific_available=(self.sandbox.scientific_environment / ".aios-environment.json").is_file(),
            http_read_available=self.sandbox.available(),
        )
        self.components = build_component_registry(
            self.capabilities,
            store=self.store,
            skill_manifests=self.skills.component_manifests() if settings.skills.enabled else (),
            plugin_manifests=self.plugins.component_manifests(),
        )
        self.lineages = LineageManager(self.store, self.components, self.skills)
        self.lineages.ensure_root()
        self.situations = SituationResolver(self.components)
        registry = ToolRegistry(
            settings.permissions,
            None if self.self_versions is not None else self.plugins,
            self.sandbox,
            self.self_versions,
            self._agent_observe,
        )
        self.controller.set_tool_schemas(registry.schemas())
        self.security = SecurityKernel(
            settings.workspace, settings.permissions, self.self_versions,
        )
        self.executor = ToolExecutor(registry, self.security)
        self.evolution = AutonomousEvolutionEngine(
            self.store, self.plugins, settings.evolution
        )
        self.runtime_provenance = RuntimeProvenanceManager(settings, self.store)
        self.shutdown_requested = False
        self._active_observation_task_id: int | None = None

    def request_shutdown(self, *_: object) -> None:
        self.shutdown_requested = True

    def run_forever(self) -> None:
        with RuntimeProcessLock(self.settings.database):
            recovered = self.store.recover_processing_events()
            if recovered:
                LOGGER.warning("Recovered %s interrupted runtime event(s)", recovered)
            signal.signal(signal.SIGINT, self.request_shutdown)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, self.request_shutdown)
            LOGGER.info("AIOS started; database=%s workspace=%s", self.settings.database, self.settings.workspace)
            while not self.shutdown_requested:
                worked = self.run_once()
                if not worked:
                    time.sleep(self.settings.poll_interval_seconds)
            LOGGER.info("AIOS stopped")

    def run_single(self) -> bool:
        """Run one owned cycle with the same recovery and signal semantics as the daemon."""
        with RuntimeProcessLock(self.settings.database):
            recovered = self.store.recover_processing_events()
            if recovered:
                LOGGER.warning("Recovered %s interrupted runtime event(s)", recovered)
            watched = [signal.SIGINT]
            if hasattr(signal, "SIGTERM"):
                watched.append(signal.SIGTERM)
            previous: dict[signal.Signals, object] = {}
            try:
                for watched_signal in watched:
                    previous[watched_signal] = signal.getsignal(watched_signal)
                    signal.signal(watched_signal, self.request_shutdown)
                return self.run_once()
            finally:
                for watched_signal, handler in previous.items():
                    signal.signal(watched_signal, handler)

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
        recovery_context = (
            dict(event.payload.get("self_recovery"))
            if event.type == "SELF_RECOVERY"
            and isinstance(event.payload.get("self_recovery"), dict)
            else None
        )
        recovery_invocation = recovery_context is not None
        task: Task | None = None
        # Keep enough live state available to the failure path to create a
        # resumable private checkpoint.  These values are deliberately not a
        # completion claim; they are only observed task-world state.
        task_budget: TaskBudget | None = None
        task_metrics: dict[str, object] = {}
        working_state: dict[str, object] | None = None
        all_actions: list[Action] = []
        all_results: list[ActionResult] = []
        final_summary = ""
        try:
            task = self._load_or_create_task(event)
            self._active_observation_task_id = int(task.id) if task.id is not None else None
            continuation = event.type == "TASK_CONTINUE" or bool(event.payload.get("continuation"))
            terminal_statuses = {
                TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.DEAD_LETTER,
                TaskStatus.DEGRADED, TaskStatus.BLOCKED_CAPABILITY,
                TaskStatus.NEEDS_AUTHORITY, TaskStatus.TERMINAL_FAILURE,
                TaskStatus.NEEDS_REVIEW, TaskStatus.STOPPED,
                TaskStatus.YIELDED, TaskStatus.ABANDONED,
            }
            if not continuation and task.status in terminal_statuses:
                self.store.discard_event(event, "terminal_task")
                self.store.trace(cycle_id, "terminal_task_event_discarded", {
                    "task_id": int(task.id), "event_id": event.id,
                    "event_type": event.type,
                })
                LOGGER.warning(
                    "Task %s event %s discarded before execution: terminal_task",
                    task.id, event.id,
                )
                return True
            if continuation:
                current, reason = self.store.fence_continuation(event)
                if not current:
                    self.store.trace(cycle_id, "stale_continuation_discarded", {
                        "task_id": int(task.id), "event_id": event.id, "reason": reason,
                        "checkpoint_id": event.payload.get("checkpoint_id"),
                        "generation": event.payload.get("generation"),
                    })
                    LOGGER.warning(
                        "Task %s continuation event %s discarded before execution: %s",
                        task.id, event.id, reason,
                    )
                    return True
            task = self.store.start_task_attempt(
                int(task.id), increment_attempt=not continuation and not recovery_invocation,
            )
            try:
                self.runtime_provenance.capture_cycle(int(task.id), cycle_id)
            except Exception as provenance_error:
                failure = {
                    "task_id": int(task.id),
                    "error": f"{type(provenance_error).__name__}: {provenance_error}",
                    "execution_continued": True,
                }
                self.store.trace(cycle_id, "runtime_provenance_capture_failed", failure)
                self.store.add_checkpoint(int(task.id), "runtime_provenance_failed", failure)
            task_budget = self._task_budget(int(task.id))
            task_metrics = self._task_metrics(int(task.id))
            working_state = self._task_working_state(int(task.id), task.request)
            seen_context_hashes = set(working_state.pop("_seen_context_hashes", []))
            attribution_totals = working_state.pop("_attribution_totals", {
                "prompt_tokens": 0, "repeated_tokens": 0, "calls": 0,
            })
            task_budget.used_cycles += 1
            cycle_phase = (
                "self_recovery_started" if recovery_invocation
                else "continued" if continuation else "started"
            )
            self.store.add_checkpoint(int(task.id), cycle_phase, {
                "cycle_id": cycle_id, "attempt": task.attempts, "task_cycle": task_budget.used_cycles,
                "recovery_invocation": recovery_invocation,
            })
            active_goals = self.goals.active()
            intent = self.arbiter.select(events, active_goals)
            if intent is None:
                self.store.finish_events(event_ids)
                return True
            self.store.trace(cycle_id, "intent_selected", asdict(intent))
            self.store.add_checkpoint(int(task.id), "intent", asdict(intent))

            expected_artifacts = (
                self.verifier._expected_artifacts(task.request)
                if self.settings.runtime.completion_mode == "verified"
                else []
            )
            contract = EvidenceContract.from_request(task.request, expected_artifacts)
            preflight_assessment = self.capabilities.assess(contract)
            preflight = {"contract": contract.as_dict(), "assessment": preflight_assessment}
            self.store.trace(cycle_id, "capability_preflight", preflight)
            self.store.add_checkpoint(int(task.id), "capability_preflight", preflight)
            if not preflight_assessment["satisfied"]:
                status = TaskStatus.NEEDS_AUTHORITY if preflight_assessment["needs_authority"] else TaskStatus.BLOCKED_CAPABILITY
                reason = "Required authority is missing" if preflight_assessment["needs_authority"] else "Required capability is unavailable"
                result = {"cycle_id": cycle_id, "summary": reason, "capability_preflight": preflight, "evidence": {"success": False}}
                self.store.trace(cycle_id, "task_terminal_decision", {
                    "task_id": int(task.id), "status": status.value,
                    "reason": reason, "decision_input_ref": "capability_preflight",
                })
                self.store.finish_events(event_ids)
                self.store.update_task(int(task.id), status, result=result, error=reason)
                self.store.add_checkpoint(int(task.id), status.value, result)
                LOGGER.warning("Task %s stopped at preflight: %s", task.id, status.value)
                return True

            lineage = (
                None if self.self_versions is not None
                else self.store.task_lineage(int(task.id)) if task.id is not None else None
            )
            if lineage is not None:
                lineage = self.lineages._lineage(str(lineage["lineage_id"]))
                lineage_component_set = dict(lineage.get("component_set") or {})
                self.sandbox.skills_root = self.skills.materialize_lineage_runtime(
                    str(lineage["lineage_id"]), lineage_component_set,
                )
                lineage_skills = self.skills.catalog_for_component_set(
                    lineage_component_set, self.capabilities,
                ) if self.settings.skills.enabled else []
            else:
                self.sandbox.skills_root = (
                    None if self.self_versions is not None else self.skills.runtime
                )
                lineage_skills = (
                    self.skills.catalog(self.capabilities)
                    if self.settings.skills.enabled and self.self_versions is None else []
                )

            cycle_health_probe_start = self.sandbox.health_probe_count
            active_self_version = None
            if self.self_versions is not None:
                recovery_base = (
                    recovery_context.get("recovery_base_version")
                    if recovery_context is not None else None
                )
                recovery_base = recovery_base if isinstance(recovery_base, str) else None
                self.self_versions.begin_task(
                    int(task.id), execution_version=recovery_base,
                )
                active_self_version = recovery_base or self.self_versions.current_version()
                if recovery_invocation:
                    started = {
                        "task_id": int(task.id),
                        "attempt": task.attempts,
                        "active_self_version": active_self_version,
                        "current_self_version": self.self_versions.current_version(),
                        "incident_ref": recovery_context.get("incident_ref"),
                        "trigger": recovery_context.get("trigger"),
                        "same_self_lineage": True,
                        "host_diagnosis": None,
                    }
                    self.store.trace(cycle_id, "self_recovery_started", started)
            snapshot = self.sandbox.prepare(int(task.id), self.settings.workspace)
            task_metrics["sandbox_sessions"] = int(task_metrics.get("sandbox_sessions", 0)) + 1
            self.store.trace(cycle_id, "task_workspace_prepared", {
                "task_id": int(task.id),
                "resumed_private_checkpoint": self.sandbox.last_prepare_resumed,
                "published_to_workspace": False,
            })
            environment_observation = None
            if any(item.name == "execution.python.scientific" for item in contract.capabilities):
                environment_observation = self.sandbox.ensure_scientific_environment()
                self.store.trace(cycle_id, "dependency_environment", environment_observation)
                self.store.add_checkpoint(int(task.id), "dependency_environment", environment_observation)
                if not environment_observation.get("ready"):
                    raise RuntimeError(str(environment_observation.get("error") or "Scientific environment provisioning failed"))
                task_metrics["dependency_provision_latency_ms"] += float(environment_observation.get("latency_ms", 0.0))
                working_state["execution_environment"] = {"scientific_python": "ready"}
                operational = working_state.setdefault("operational", {})
                if isinstance(operational, dict):
                    operational["environment"] = {"scientific_python": "ready"}
            self.sandbox.expose_read_only_state({
                "tasks": [asdict(item) for item in self.store.list_tasks(limit=100)],
                "traces": self.store.recent_traces(limit=100),
                "dead-letters": self.store.list_dead_letters(limit=100),
                "memory": [asdict(item) for item in self.store.list_memories(limit=100)],
                "skill-usage": self.store.list_skill_usage(limit=100),
                "capabilities": self.capabilities.as_dict(),
                "skills": lineage_skills,
            })
            self.security.workspace = snapshot.resolve()

            harness = (
                {
                    "version": f"lineage:{lineage['lineage_id']}",
                    "settings": lineage.get("settings", {}),
                }
                if lineage is not None else self.store.active_harness()
            )
            harness_settings = dict(harness.get("settings", {}))
            if self.self_versions is not None:
                harness_settings = {"harness_profile": "minimal_self"}
            memory_limit = harness_settings.get("memory_context_characters")
            if isinstance(memory_limit, int):
                self.context.max_characters = memory_limit
            context = self.context.compose(task.request)
            full_workspace_inventory = self._workspace_inventory(snapshot)
            full_capabilities = self.capabilities.as_dict()
            full_skills = lineage_skills
            context["workspace_inventory"] = full_workspace_inventory
            context["evidence_contract"] = contract.as_dict()
            skill_authoring = (
                self.skills.authoring_context(task.request)
                if self.settings.skills.enabled and self.self_versions is None else None
            )
            if skill_authoring is not None:
                context["skill_authoring"] = skill_authoring
            context["harness"] = harness_settings
            context["harness_version"] = harness.get("version")
            context["lineage"] = lineage
            if self.self_versions is not None:
                context["self_system_prompt"] = self.self_versions.system_prompt(active_self_version)
                context["self_goal"] = self.self_versions.self_goal(active_self_version)
                context["self_experiment_condition"] = (
                    self.settings.self_modification.experiment_condition
                )
                active_architecture = self.self_versions.architecture(active_self_version)
                context["self_state"] = {
                    "current_version": self.self_versions.current_version(),
                    "active_cycle_version": active_self_version,
                    "mutable_root": "/self",
                    "exposed_after_evolve": True,
                    "transaction_open": self.self_versions.writable,
                    "architecture": active_architecture,
                }
                context["self_observation"] = {
                    "schema": "agent_observation_entrypoint/v1",
                    "current_task_id": int(task.id),
                    "current_cycle_id": cycle_id,
                    "tool": "observe",
                    "available_views": [
                        "tasks", "task_events", "trace", "self_versions",
                    ],
                    "host_interpretation": None,
                }
                if recovery_context is not None:
                    context["self_recovery"] = recovery_context
            if lineage is not None:
                self.store.trace(cycle_id, "lineage_bound", {
                    "task_id": task.id, "lineage_id": lineage["lineage_id"],
                    "parent_lineage_id": lineage.get("parent_lineage_id"),
                    "generation": lineage.get("generation"),
                    "component_set_hash": lineage.get("component_set", {}).get("active_set_hash"),
                    "production_default_changed": False,
                })
            context["continuation"] = self._continuation_context(int(task.id)) if continuation else None
            context["task_working_state"] = self._working_state_projection(working_state)
            if environment_observation is not None:
                context["environment"] = {"scientific_python": environment_observation}
            context["situation_map"] = self.situations.resolve(
                task.request, full_workspace_inventory, contract, working_state, full_skills,
                context.get("environment") if isinstance(context.get("environment"), dict) else None,
            )
            self.store.trace(cycle_id, "context_composed", context)
            execution_config = self.settings.execution_config_facts(harness_settings)
            self.store.trace(cycle_id, "execution_config_resolved", execution_config)
            effective_limits = execution_config["budget"]["effective"]
            task_remaining = task_budget.remaining()
            action_cap = min(
                effective_limits["max_actions_per_cycle"],
                task_remaining["tool_calls"],
            ) if task_budget.enabled else None
            model_round_cap = (
                min(effective_limits["max_model_calls_per_cycle"], task_remaining["model_calls"])
                if task_budget.enabled else None
            )
            if model_round_cap is not None and model_round_cap <= 0:
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
            round_number = 0
            self_restart_requested = False
            harness_checkpoint_requested = False
            liveness_checkpoint_requested = False
            harness_stop_requested = False
            self_reflection_offered = bool(
                working_state.get("_self_reflection_offered", False)
            )
            in_task_reflection_rounds = working_state.setdefault(
                "_in_task_reflection_rounds", []
            )
            if not isinstance(in_task_reflection_rounds, list):
                in_task_reflection_rounds = []
                working_state["_in_task_reflection_rounds"] = in_task_reflection_rounds
            while not self.shutdown_requested and (
                model_round_cap is None or model_calls_used < model_round_cap
            ):
                round_number += 1
                # A reflection opportunity is a one-model-turn observation, never a
                # persistent policy injection.  Post-answer opportunities are queued
                # by the previous round; long-task opportunities are offered at the
                # configured factual checkpoints without forcing any mutation.
                context.pop("self_reflection", None)
                pending_reflection = working_state.pop("_pending_self_reflection", None)
                active_reflection = working_state.get("_active_self_reflection")
                if (
                    not isinstance(active_reflection, dict)
                    and isinstance(pending_reflection, dict)
                    and pending_reflection.get("trigger") == "runtime_liveness_checkpoint"
                ):
                    active_reflection = {
                        **pending_reflection,
                        "phase": "dedicated_self_decision",
                        "no_change_signal": "SELF_UNCHANGED",
                        "instruction": (
                            "This is a dedicated decision phase for your reusable Self, not an "
                            "ordinary task-execution turn. Use observe if you choose to inspect "
                            "the immutable record, and interpret the selected events yourself. "
                            "If your observation supports a reusable change, use evolve, inspect/edit /self, test, "
                            "and commit the descendant. If it does not, return a concise final answer "
                            "stating SELF_UNCHANGED; the Host will then resume the ordinary task. "
                            "Do not claim that the ordinary user task is complete in this phase."
                        ),
                    }
                    working_state["_active_self_reflection"] = active_reflection
                reflection_payload = (
                    active_reflection if isinstance(active_reflection, dict)
                    else pending_reflection if isinstance(pending_reflection, dict) else None
                )
                if (
                    reflection_payload is None
                    and self.self_versions is not None
                    and round_number in self.settings.self_modification.in_task_reflection_rounds
                    and round_number not in in_task_reflection_rounds
                    and not bool(working_state.get("_self_evolution_observed", False))
                ):
                    in_task_reflection_rounds.append(round_number)
                    reflection_payload = {
                        "schema": "self_reflection_opportunity/v1",
                        "trigger": "in_task_checkpoint",
                        "host_fitness_judgment": None,
                        "mutation_required": False,
                        "decision_owner": "ordinary_agent",
                        "instruction": (
                            "The ordinary task is still active. You may use observe to inspect "
                            "immutable events and draw your own conclusions. Continue with a "
                            "useful next action, return a final answer only if the task is "
                            "actually complete, or use evolve only if you independently "
                            "identify a reusable Self change. This checkpoint imposes no "
                            "execution budget or stop."
                        ),
                    }
                    self.store.trace(cycle_id, "self_reflection_offered", {
                        "task_id": int(task.id),
                        "round": round_number,
                        "active_self_version": active_self_version,
                        "trigger": "in_task_checkpoint",
                        "host_fitness_judgment": None,
                        "mutation_required": False,
                    })
                if reflection_payload is not None:
                    context["self_reflection"] = reflection_payload
                first_task_call = task_budget.used_model_calls == 0 and round_number == 1
                if not first_task_call:
                    context["workspace_inventory"] = self._relevant_workspace_map(full_workspace_inventory, working_state)
                    context["retrieved_memories"] = []
                    context["characters"] = 0
                    if environment_observation is not None:
                        context["environment"] = {"scientific_python": "ready"}
                context["task_working_state"] = self._working_state_projection(working_state)
                if self.self_versions is not None:
                    context["self_system_prompt"] = self.self_versions.system_prompt(active_self_version)
                    context["self_goal"] = self.self_versions.self_goal(active_self_version)
                    active_architecture = self.self_versions.architecture(active_self_version)
                    context["self_state"] = {
                        "current_version": self.self_versions.current_version(),
                        "active_cycle_version": active_self_version,
                        "mutable_root": "/self",
                        "exposed_after_evolve": True,
                        "transaction_open": self.self_versions.writable,
                        "architecture": active_architecture,
                        "in_task_reflection_rounds": list(
                            self.settings.self_modification.in_task_reflection_rounds
                        ),
                    }
                    # The Host exposes only stable observation coordinates.  It does
                    # not preselect metrics or turn them into an efficiency diagnosis.
                    # The ordinary Agent may call `observe` whenever it chooses and
                    # decide which immutable events are relevant.
                    context["self_observation"] = {
                        "schema": "agent_observation_entrypoint/v1",
                        "current_task_id": int(task.id),
                        "current_cycle_id": cycle_id,
                        "tool": "observe",
                        "available_views": [
                            "tasks", "task_events", "trace", "self_versions",
                        ],
                        "host_interpretation": None,
                    }
                context["situation_map"] = self.situations.resolve(
                    task.request, full_workspace_inventory, contract, working_state, full_skills,
                    context.get("environment") if isinstance(context.get("environment"), dict) else None,
                )
                remaining_before_round = (
                    max(0, action_cap - len(all_actions)) if action_cap is not None else None
                )
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
                    if task_budget.enabled and (
                        (expected_artifacts and not artifact_written)
                        or (skill_authoring is not None and not skill_candidate_written)
                    )
                    else 0
                )
                context["observations"] = observations
                context["round"] = round_number
                context["budget"] = {
                    "enabled": task_budget.enabled,
                    "scope": "cycle_and_task",
                    "model_call": round_number,
                    "max_model_calls_this_cycle": model_round_cap,
                    "remaining_model_calls_after_this": (
                        max(0, model_round_cap - model_calls_used - 1) if model_round_cap is not None else None
                    ),
                    "used_tool_calls": len(all_actions),
                    "remaining_tool_calls": remaining_before_round,
                    "task": {
                        "used_model_calls_before_cycle": task_budget.used_model_calls,
                        "used_tool_calls_before_cycle": task_budget.used_tool_calls,
                        "used_tokens_before_cycle": task_budget.used_tokens,
                        "cycle": task_budget.used_cycles,
                        "remaining_before_cycle": task_remaining,
                    },
                    "force_final": task_budget.enabled and (
                        task_budget.used_model_calls + model_calls_used + 1 >= task_budget.max_model_calls
                        or task_budget.used_tokens + model_tokens_total >= task_budget.max_tokens
                        or task_budget.used_cycles >= task_budget.max_cycles
                    ),
                    "soft_pressure": task_budget.enabled and (
                        task_budget.used_model_calls + model_calls_used >= self.settings.budget.soft_model_calls_per_task
                        or task_budget.used_tokens + model_tokens_total >= self.settings.budget.soft_tokens_per_task
                    ),
                    "reserved_completion_tool_calls": reserved,
                    "protocol_repairs_remaining": 1,
                    "required_artifacts": expected_artifacts,
                    "instruction": (
                        "Stop broad inspection before the reserved count and create the requested artifact. "
                        "A cycle boundary will checkpoint and continue; only force_final marks a real task terminal budget."
                    ) if task_budget.enabled else (
                        "Execution budgets are disabled. Null remaining values mean unlimited, not zero. "
                        "No cycle, task, tool-call or token quota requires you to stop. "
                        "Usage is still recorded. Finish when appropriate; permissions, command timeouts "
                        "and context/output size limits still apply."
                    ),
                }
                context["_protocol_messages"] = protocol_messages
                model_context = self._harness_context_projection(context, harness_settings)
                if self.self_versions is not None and active_self_version is not None:
                    harness_run = self.sandbox.run_self_harness(
                        active_self_version, "before_model", {
                            "context": model_context,
                            "state": {
                                "task_id": int(task.id), "round": round_number,
                                "cycle_id": cycle_id,
                            },
                        },
                    )
                    candidate_context = harness_run["output"].get("context")
                    if not isinstance(candidate_context, dict):
                        raise RuntimeError("Self Harness before_model returned no context object")
                    model_context = candidate_context
                    model_context["harness"] = {"harness_profile": "minimal_self"}
                    self.store.trace(cycle_id, self._self_runtime_trace_kind(harness_run), {
                        "task_id": int(task.id), "round": round_number,
                        "stage": "before_model", "self_version": active_self_version,
                        "entrypoint": harness_run.get("entrypoint"),
                        "context_keys": sorted(str(key) for key in model_context),
                    })
                plan = self.controller.plan(intent, active_goals, model_context)
                model_usage = plan.model_usage or {}
                model_calls_used += int(model_usage.get("model_calls", 1))
                model_tokens_total += int(model_usage.get("total_tokens", 0))
                if isinstance(plan.reasoning, str) and plan.reasoning.strip():
                    raw_reasoning = plan.reasoning.strip()
                    redacted_reasoning = self._redact_incident_text(raw_reasoning)
                    reasoning_limit = 16_000
                    reasoning_text = redacted_reasoning[:reasoning_limit]
                    self.store.trace(cycle_id, "model_reasoning", {
                        "task_id": int(task.id),
                        "task_cycle": task_budget.used_cycles,
                        "round": round_number,
                        "reasoning": reasoning_text,
                        "reasoning_excerpt": reasoning_text[:500],
                        "original_characters": len(raw_reasoning),
                        "recorded_characters": len(reasoning_text),
                        "truncated": len(redacted_reasoning) > reasoning_limit,
                        "source": "provider_reasoning_content",
                        "interpretation": "model_self_report_not_host_diagnosis",
                    })
                for attribution in plan.model_attributions:
                    repeated_tokens = 0
                    attribution_blocks = attribution.get("blocks", {})
                    for block in attribution_blocks.values():
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
                    if "protocol_repair" in attribution_blocks:
                        task_metrics["protocol_repair_calls"] = int(
                            task_metrics.get("protocol_repair_calls", 0)
                        ) + 1
                        task_metrics["protocol_repair_tokens"] = int(
                            task_metrics.get("protocol_repair_tokens", 0)
                        ) + prompt_tokens
                    self.store.trace(cycle_id, "model_call_attribution", {
                        "task_id": int(task.id), "task_cycle": task_budget.used_cycles,
                        "round": round_number, **attribution,
                    })
                if plan.protocol_message:
                    protocol_messages.append(plan.protocol_message)
                dedicated_self_decision = bool(
                    isinstance(reflection_payload, dict)
                    and reflection_payload.get("phase") == "dedicated_self_decision"
                )
                if dedicated_self_decision and plan.done and not plan.actions:
                    if self.self_versions is not None and self.self_versions.writable:
                        working_state["_active_self_reflection"] = {
                            **reflection_payload,
                            "instruction": (
                                "A Self transaction is still open. Commit it with evolve after "
                                "testing, or abort it explicitly; a prose answer cannot close the "
                                "transaction or complete the ordinary task."
                            ),
                        }
                        self.store.trace(cycle_id, "self_reflection_decision_deferred", {
                            "task_id": int(task.id), "round": round_number,
                            "reason": "self_transaction_open",
                            "model_summary": plan.summary[:2000],
                        })
                        continue
                    working_state.pop("_active_self_reflection", None)
                    working_state.pop("liveness", None)
                    working_state["_self_reflection_cooldown_remaining"] = int(
                        self.settings.self_modification.reflection_cooldown_checkpoints
                    )
                    self.store.trace(cycle_id, "self_reflection_resolved", {
                        "task_id": int(task.id),
                        "round": round_number,
                        "trigger": "runtime_liveness_checkpoint",
                        "self_evolution_observed": False,
                        "agent_stopped": False,
                        "ordinary_task_resumed": True,
                        "model_summary": plan.summary[:2000],
                        "host_fitness_judgment": None,
                    })
                    continue
                final_summary = plan.summary
                completion_metadata = plan.completion_metadata
                working_state["semantic_state"] = {"latest_model_summary": plan.summary[:2000]}
                semantic = working_state.setdefault("semantic", {})
                if isinstance(semantic, dict):
                    semantic["latest_model_summary"] = plan.summary[:2000]
                actions, deferred_actions = self._select_actions(
                    plan.actions,
                    remaining_before_round,
                    reserved,
                )
                if self.self_versions is not None and active_self_version is not None and actions:
                    original_actions = [asdict(action) for action in actions]
                    harness_run = self.sandbox.run_self_harness(
                        active_self_version, "after_plan", {
                            "actions": original_actions,
                            "state": {
                                "task_id": int(task.id), "round": round_number,
                                "cycle_id": cycle_id, "summary": plan.summary,
                                "objective": task.request[:4000],
                            },
                        },
                    )
                    agent_output = harness_run["output"]
                    selected_actions = agent_output.get("actions")
                    autonomous_actions = agent_output.get("autonomous_actions", [])
                    autonomous_limit = (
                        max(0, remaining_before_round - len(actions))
                        if remaining_before_round is not None else 8
                    )
                    actions = self._self_harness_actions(
                        actions, selected_actions, autonomous_actions,
                        max_autonomous_actions=min(8, autonomous_limit),
                    )
                    self.store.trace(cycle_id, self._self_runtime_trace_kind(harness_run), {
                        "task_id": int(task.id), "round": round_number,
                        "stage": "after_plan", "self_version": active_self_version,
                        "entrypoint": harness_run.get("entrypoint"),
                        "changed": original_actions != [asdict(action) for action in actions],
                        "autonomous_action_count": max(0, len(actions) - len(original_actions)),
                        "model_actions": original_actions,
                        "effective_actions": [asdict(action) for action in actions],
                    })
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
                    "model_usage": model_usage,
                }
                rounds.append(plan_data)
                self.store.trace(cycle_id, "plan_created", plan_data)
                self.store.add_checkpoint(int(task.id), f"planned_round_{round_number}", plan_data)

                round_results = []
                for action in actions:
                    routing_signal = self.situations.classify_action(
                        action.tool, action.arguments, working_state
                    )
                    if routing_signal is not None:
                        signal_kind = str(routing_signal["kind"])
                        self.store.trace(cycle_id, signal_kind, {
                            "task_id": int(task.id), "round": round_number, **routing_signal,
                        })
                        metric_name = {
                            "repeated_resource_read": "repeated_resource_reads",
                            "redundant_resource_bypass": "redundant_resource_bypasses",
                            "environment_probe": "environment_probe_calls",
                        }.get(signal_kind)
                        if metric_name is not None:
                            task_metrics[metric_name] = int(task_metrics.get(metric_name, 0)) + 1
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
                    if action.tool == "evolve" and result.ok:
                        # Persist this fact across a commit/restart so a task which
                        # already chose Self modification is not given a redundant
                        # post-answer reflection turn on its descendant.
                        working_state["_self_evolution_observed"] = True
                        operation = (
                            str(result.output.get("operation", "open"))
                            if isinstance(result.output, dict) else "open"
                        )
                        restart_required = bool(
                            isinstance(result.output, dict)
                            and result.output.get("restart_required")
                        )
                        if operation == "commit":
                            working_state.pop("_active_self_reflection", None)
                            working_state.pop("liveness", None)
                            working_state["_self_reflection_cooldown_remaining"] = int(
                                self.settings.self_modification.reflection_cooldown_checkpoints
                            )
                        self_restart_requested = self_restart_requested or restart_required
                        trace_kind = {
                            "commit": "self_version_committed",
                            "abort": "self_version_aborted",
                        }.get(operation, "self_version_opened")
                        self.store.trace(cycle_id, trace_kind, {
                            "task_id": int(task.id), "round": round_number,
                            "result_ref": f"trace:{result_trace_id}",
                            "version": result.output.get("version")
                            if isinstance(result.output, dict) else None,
                            "parent_version": result.output.get("parent_version")
                            if isinstance(result.output, dict) else None,
                            "host_evaluation_started": False,
                            "operation": operation,
                            "restart_required": restart_required,
                        })
                    cache_metadata = (
                        result.output.get("observation_cache")
                        if result.ok and isinstance(result.output, dict) else None
                    )
                    if isinstance(cache_metadata, dict):
                        self.executor.registry.resources.attach_observation_ref(
                            result.output, result_trace_id
                        )
                        if cache_metadata.get("hit"):
                            task_metrics["observation_reuse_hits"] = int(
                                task_metrics.get("observation_reuse_hits", 0)
                            ) + 1
                            self.store.trace(cycle_id, "observation_reused", {
                                "task_id": int(task.id), "round": round_number,
                                "path": cache_metadata.get("path"),
                                "cache_key": cache_metadata.get("key"),
                                "source_observation_ref": cache_metadata.get("source_observation_ref"),
                                "request_observation_ref": f"trace:{result_trace_id}",
                            })
                        elif routing_signal is not None and routing_signal.get("kind") == "repeated_resource_read":
                            task_metrics["repeated_resource_executions"] = int(
                                task_metrics.get("repeated_resource_executions", 0)
                            ) + 1
                    elif routing_signal is not None and routing_signal.get("kind") == "repeated_resource_read":
                        task_metrics["repeated_resource_executions"] = int(
                            task_metrics.get("repeated_resource_executions", 0)
                        ) + 1
                    adapter_runtime = (
                        result.output.get("adapter_runtime")
                        if result.ok and isinstance(result.output, dict) else None
                    )
                    if isinstance(adapter_runtime, dict) and not (
                        isinstance(cache_metadata, dict) and cache_metadata.get("hit")
                    ):
                        task_metrics["adapter_retries"] = int(
                            task_metrics.get("adapter_retries", 0)
                        ) + int(adapter_runtime.get("retry_count", 0))
                        if adapter_runtime.get("transient_recovered"):
                            task_metrics["adapter_transient_recoveries"] = int(
                                task_metrics.get("adapter_transient_recoveries", 0)
                            ) + 1
                    self._update_working_state(working_state, action, result, result_trace_id)
                    self._record_contract_evidence(
                        working_state, contract, action, result, result_trace_id,
                    )
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
                if any(
                    self._is_workspace_change(action, result)
                    for action, result in zip(actions, round_results, strict=False)
                ):
                    task_metrics["workspace_change_actions"] = int(
                        task_metrics.get("workspace_change_actions", 0)
                    ) + sum(
                        self._is_workspace_change(action, result)
                        for action, result in zip(actions, round_results, strict=False)
                    )
                    task_metrics["rounds_since_workspace_change"] = 0
                else:
                    task_metrics["rounds_since_workspace_change"] = int(
                        task_metrics.get("rounds_since_workspace_change", 0)
                    ) + 1
                has_final_action = any(action.tool in {"write", "edit", "echo", "write_file", "append_file"} for action in actions)
                task_done = bool(plan.done and (has_final_action or not actions))
                if (
                    task_done
                    and self.self_versions is not None
                    and not self_reflection_offered
                    and int(working_state.get(
                        "_self_reflection_cooldown_remaining", 0
                    ) or 0) <= 0
                ):
                    # The ordinary Agent, not a second Evolution Agent, gets one
                    # post-answer opportunity to inspect factual consequences and
                    # either stop unchanged or create a reversible descendant.
                    # This is deliberately offered for every eligible task: the
                    # Host does not diagnose which observations deserve mutation.
                    self_reflection_offered = True
                    working_state["_self_reflection_offered"] = True
                    working_state["_pending_agent_answer"] = plan.summary[:20_000]
                    working_state["pending"] = ["decide_self_change_or_stop"]
                    working_state["_pending_self_reflection"] = {
                        "schema": "self_reflection_opportunity/v1",
                        "trigger": "agent_declared_stop",
                        "host_fitness_judgment": None,
                        "mutation_required": False,
                        "decision_owner": "ordinary_agent",
                        "instruction": (
                            "The ordinary task answer is pending. You may inspect immutable "
                            "task events with observe and interpret them yourself. Return a "
                            "final answer to stop unchanged, or use evolve only if you independently identify a reusable "
                            "change worth carrying into later tasks. Do not repeat completed "
                            "task work merely to gain certainty."
                        ),
                    }
                    self.store.trace(cycle_id, "self_reflection_offered", {
                        "task_id": int(task.id),
                        "round": round_number,
                        "active_self_version": active_self_version,
                        "trigger": "agent_declared_stop",
                        "host_fitness_judgment": None,
                        "mutation_required": False,
                    })
                    task_done = False
                    continue
                if task_done and self.settings.runtime.completion_mode == "verified":
                    provisional_situation = self.situations.resolve(
                        task.request, full_workspace_inventory, contract, working_state, full_skills,
                        context.get("environment") if isinstance(context.get("environment"), dict) else None,
                    )
                    provisional_coverage = self.situations.assess_coverage(
                        provisional_situation, plan.summary
                    )
                    if provisional_coverage.get("passed"):
                        context.pop("coverage_feedback", None)
                    if (
                        provisional_coverage.get("required")
                        and not provisional_coverage.get("passed")
                        and (model_round_cap is None or model_calls_used < model_round_cap)
                    ):
                        task_done = False
                        context["coverage_feedback"] = {
                            **provisional_coverage,
                            "instruction": (
                                "Completion was withheld: read the selected evidence for any missing-evidence "
                                "target, then revise the final answer to cover every missing topic. "
                                "Do not reread complete resources."
                            ),
                        }
                        working_state["pending"] = ["repair_goal_coverage"]
                        self.store.trace(cycle_id, "coverage_repair_requested", {
                            "task_id": int(task.id), "round": round_number,
                            "coverage": provisional_coverage,
                        })
                        continue
                if (
                    self.self_versions is not None
                    and active_self_version is not None
                    and not task_done
                    and not self_restart_requested
                ):
                    harness_run = self.sandbox.run_self_harness(
                        active_self_version, "after_round", {
                            "state": {
                                "task_id": int(task.id), "round": round_number,
                                "cycle_id": cycle_id, "plan_done": plan.done,
                                "actions": [asdict(action) for action in actions],
                                "results": [asdict(result) for result in round_results],
                                "unresolved_failures": list(
                                    working_state.get("unresolved_failures", [])
                                ),
                            },
                        },
                    )
                    harness_output = harness_run["output"]
                    loop_decision = harness_output.get("loop", {})
                    continuation_decision = harness_output.get("continuation", {})
                    if not isinstance(loop_decision, dict) or not isinstance(
                        continuation_decision, dict
                    ):
                        raise RuntimeError("Self Harness after_round returned invalid decisions")
                    harness_stop_requested = loop_decision.get("allow_another_round") is False
                    harness_checkpoint_requested = bool(
                        continuation_decision.get("checkpoint", False)
                    )
                    self.store.trace(cycle_id, self._self_runtime_trace_kind(harness_run), {
                        "task_id": int(task.id), "round": round_number,
                        "stage": "after_round", "self_version": active_self_version,
                        "entrypoint": harness_run.get("entrypoint"),
                        "loop": loop_decision, "continuation": continuation_decision,
                    })
                if task_done:
                    working_state["pending"] = []
                    if self_reflection_offered:
                        self.store.trace(cycle_id, "self_reflection_resolved", {
                            "task_id": int(task.id),
                            "round": round_number,
                            "self_evolution_observed": bool(
                                working_state.get("_self_evolution_observed", False)
                            ),
                            "agent_stopped": True,
                        })
                if task_done:
                    break
                liveness_rounds = self.settings.runtime.liveness_checkpoint_rounds
                if (
                    not task_budget.enabled
                    and liveness_rounds > 0
                    and round_number >= liveness_rounds
                    and not self_restart_requested
                    and not harness_checkpoint_requested
                    and not harness_stop_requested
                ):
                    liveness_checkpoint_requested = True
                    task_metrics["liveness_checkpoints"] = int(
                        task_metrics.get("liveness_checkpoints", 0)
                    ) + 1
                    liveness_facts = {
                        "rounds_in_cycle": round_number,
                        "model_calls_in_cycle": model_calls_used,
                        "tool_calls_in_cycle": len(all_results),
                        "successful_tool_calls_in_cycle": sum(item.ok for item in all_results),
                        "failed_tool_calls_in_cycle": sum(not item.ok for item in all_results),
                        "workspace_change_actions_in_cycle": sum(
                            self._is_workspace_change(action, result)
                            for action, result in zip(all_actions, all_results, strict=False)
                        ),
                        "rounds_since_workspace_change": task_metrics.get(
                            "rounds_since_workspace_change", 0
                        ),
                    }
                    working_state["liveness"] = liveness_facts
                    liveness_trace_id = self.store.trace(
                        cycle_id, "liveness_checkpoint_requested", {
                            "task_id": int(task.id),
                            "round": round_number,
                            "observed": liveness_facts,
                            "budget_limits_enabled": False,
                        },
                    )
                    if dedicated_self_decision:
                        if not (
                            self.self_versions is not None
                            and self.self_versions.writable
                        ):
                            # A dedicated reflection is a bounded opportunity, not
                            # a second task that can recursively schedule itself.
                            # Without a concrete draft, return control to ordinary
                            # task execution at the next fresh-context cycle.
                            working_state.pop("_active_self_reflection", None)
                            working_state.pop("_pending_self_reflection", None)
                            working_state.pop("liveness", None)
                            working_state["_self_reflection_cooldown_remaining"] = int(
                                self.settings.self_modification.reflection_cooldown_checkpoints
                            )
                            self.store.trace(
                                cycle_id, "self_reflection_window_exhausted", {
                                    "task_id": int(task.id),
                                    "round": round_number,
                                    "ordinary_task_resumed_next_cycle": True,
                                    "self_evolution_observed": False,
                                    "host_fitness_judgment": None,
                                },
                            )
                        # If a draft is open, _active_self_reflection and the
                        # durable Self transaction remain available next cycle.
                        # Never create a nested reflection request here.
                    else:
                        cooldown_remaining = int(working_state.get(
                            "_self_reflection_cooldown_remaining", 0
                        ) or 0)
                        if cooldown_remaining > 0:
                            working_state["_self_reflection_cooldown_remaining"] = (
                                cooldown_remaining - 1
                            )
                            self.store.trace(
                                cycle_id, "self_reflection_cooldown_advanced", {
                                    "task_id": int(task.id),
                                    "remaining_before": cooldown_remaining,
                                    "remaining_after": cooldown_remaining - 1,
                                    "host_fitness_judgment": None,
                                },
                            )
                        else:
                            working_state["_pending_self_reflection"] = {
                                "schema": "self_reflection_opportunity/v1",
                                "trigger": "runtime_liveness_checkpoint",
                                "host_fitness_judgment": None,
                                "mutation_required": False,
                                "decision_owner": "ordinary_agent",
                                "observation_entrypoint": {
                                    "tool": "observe", "task_id": int(task.id),
                                    "checkpoint_ref": f"trace:{liveness_trace_id}",
                                },
                                "instruction": (
                                    "A fresh-context continuation boundary was reached. Use the read-only "
                                    "observe tool if you choose to inspect what happened; select and "
                                    "interpret the immutable events yourself. Continue the task, stop if "
                                    "the objective is actually satisfied, or use evolve only if your own "
                                    "observation supports a reusable Self change."
                                ),
                            }
                    break
                if self_restart_requested or harness_checkpoint_requested or harness_stop_requested:
                    break
                if not actions and not deferred_actions:
                    break
                if action_cap is not None and len(all_actions) >= action_cap:
                    break

            task_budget.used_model_calls += model_calls_used
            task_budget.used_tool_calls += len(all_results)
            task_budget.used_tokens += model_tokens_total
            task_metrics["failed_tool_calls"] = int(
                task_metrics.get("failed_tool_calls", 0)
            ) + sum(not result.ok for result in all_results)
            task_metrics["post_failure_tool_changes"] = int(
                task_metrics.get("post_failure_tool_changes", 0)
            ) + sum(
                not result.ok and all_actions[index].tool != all_actions[index + 1].tool
                for index, result in enumerate(all_results[:-1])
                if index + 1 < len(all_actions)
            )
            task_metrics["sandbox_health_probes"] = int(
                task_metrics.get("sandbox_health_probes", 0)
            ) + max(0, self.sandbox.health_probe_count - cycle_health_probe_start)
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
                    (model_round_cap is not None and model_calls_used >= model_round_cap)
                    or (action_cap is not None and len(all_actions) >= action_cap)
                    or (task_budget.enabled and budget_truncated)
                )
            )
            can_continue = bool(
                task_budget.enabled
                and cycle_exhausted
                and made_progress
                and not planner_claimed_done
                and not artifact_ready
                and remaining_task_budget["model_calls"] > 0
                and remaining_task_budget["tool_calls"] > 0
                and remaining_task_budget["tokens"] > 0
                and remaining_task_budget["cycles"] > 0
            )
            can_continue = (
                can_continue
                or self_restart_requested
                or harness_checkpoint_requested
                or liveness_checkpoint_requested
            )
            shutdown_deferred = self.shutdown_requested and not task_done
            can_continue = can_continue or shutdown_deferred
            if can_continue:
                if self_restart_requested:
                    continuation_reason = "self_version_commit"
                elif harness_checkpoint_requested:
                    continuation_reason = "self_harness_checkpoint"
                elif liveness_checkpoint_requested:
                    continuation_reason = "runtime_liveness_checkpoint"
                else:
                    continuation_reason = "shutdown_requested" if shutdown_deferred else "cycle_budget"
                checkpoint = {
                    "continuation_reason": continuation_reason,
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
                    "summary": (
                        "Runtime shutdown requested; task checkpointed for continuation"
                        if shutdown_deferred else
                        "Self version committed; task checkpointed to restart on the descendant"
                        if self_restart_requested else
                        "Self Harness requested a checkpoint continuation"
                        if harness_checkpoint_requested else
                        "Runtime liveness boundary reached; task checkpointed for fresh-context continuation"
                        if liveness_checkpoint_requested else
                        "Cycle budget reached; task checkpointed for continuation"
                    ),
                    "continuation_reason": continuation_reason,
                    "checkpoint_id": checkpoint_id,
                    "task_budget": asdict(task_budget),
                    "committed_files": committed,
                    "evidence": {
                        "success": False, "terminal": False,
                        "budget_limits_enabled": task_budget.enabled,
                        "budget_deferred": continuation_reason == "cycle_budget",
                    },
                }
                self.store.finalize_skill_usage(
                    cycle_id, verifier_passed=None, task_outcome="budget_deferred",
                    model_calls_after=task_budget.used_model_calls,
                    tokens_after=task_budget.used_tokens,
                )
                self.store.finish_events(event_ids)
                self.store.update_task(int(task.id), TaskStatus.DEFERRED, result=deferred_result)
                continue_id, continuation_decision = self.store.enqueue_continuation(
                    int(task.id), checkpoint_id, task.request, task.priority,
                )
                self.store.trace(cycle_id, "budget_deferred", {
                    "continuation_reason": continuation_reason,
                    "task_id": int(task.id), "checkpoint_id": checkpoint_id,
                    "continuation_event_id": continue_id,
                    "continuation_decision": continuation_decision,
                    "remaining": remaining_task_budget,
                })
                if recovery_invocation:
                    self.store.trace(cycle_id, "self_recovery_checkpointed", {
                        "task_id": int(task.id),
                        "active_self_version": active_self_version,
                        "current_self_version": (
                            self.self_versions.current_version()
                            if self.self_versions is not None else None
                        ),
                        "continuation_reason": continuation_reason,
                        "self_evolution_observed": bool(
                            working_state.get("_self_evolution_observed", False)
                        ),
                    })
                LOGGER.info(
                    "Task %s deferred (%s); checkpoint %s, continuation event %s queued",
                    task.id, continuation_reason, checkpoint_id, continue_id,
                )
                return True

            final_output = self._final_output(final_summary, all_actions, all_results)
            canonical_answer = CanonicalAnswer.bind(
                final_summary, all_actions, all_results, contract, Path(snapshot),
            )
            final_situation = self.situations.resolve(
                task.request, full_workspace_inventory, contract, working_state, full_skills,
                context.get("environment") if isinstance(context.get("environment"), dict) else None,
            )
            if self.settings.runtime.completion_mode == "free":
                status = TaskStatus.STOPPED if task_done else TaskStatus.YIELDED
                outcome = "agent_declared_stop" if task_done else "agent_yielded"
                measurement = {
                    "mode": "disabled", "passed": None,
                    "outcome": "not_evaluated", "checks": [],
                }
                self.store.finalize_skill_usage(
                    cycle_id, verifier_passed=None, task_outcome=outcome,
                    model_calls_after=model_calls_used, tokens_after=model_tokens_total,
                )
                evidence = {
                    "success": None,
                    "online_verifier_enabled": False,
                    "agent_declared_stop": bool(task_done),
                    "host_observed_completion": None,
                    "model_rounds": len(rounds),
                    "model_api_calls": task_budget.used_model_calls,
                    "budget_limits_enabled": task_budget.enabled,
                    "model_tokens": task_budget.used_tokens,
                    "task_tool_calls": task_budget.used_tool_calls,
                    "cycle_model_api_calls": model_calls_used,
                    "cycle_model_tokens": model_tokens_total,
                    "task_cycles": task_budget.used_cycles,
                    "rounds_to_first_computation": task_metrics["rounds_to_first_computation"],
                    "dependency_provision_latency_ms": task_metrics["dependency_provision_latency_ms"],
                    "repeated_resource_reads": task_metrics["repeated_resource_reads"],
                    "repeated_resource_executions": task_metrics["repeated_resource_executions"],
                    "observation_reuse_hits": task_metrics["observation_reuse_hits"],
                    "redundant_resource_bypasses": task_metrics["redundant_resource_bypasses"],
                    "environment_probe_calls": task_metrics["environment_probe_calls"],
                    "sandbox_sessions": task_metrics["sandbox_sessions"],
                    "sandbox_health_probes": task_metrics["sandbox_health_probes"],
                    "adapter_retries": task_metrics["adapter_retries"],
                    "adapter_transient_recoveries": task_metrics["adapter_transient_recoveries"],
                    "protocol_repair_calls": task_metrics["protocol_repair_calls"],
                    "protocol_repair_tokens": task_metrics["protocol_repair_tokens"],
                    "failed_tool_calls": task_metrics["failed_tool_calls"],
                    "post_failure_tool_changes": task_metrics["post_failure_tool_changes"],
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
                    "verification": measurement,
                    "completion_metadata": completion_metadata,
                    "result_vector": None,
                    "coverage_assessment": {
                        "mode": "not_evaluated", "required": None, "passed": None,
                    },
                    "established_evidence": list(working_state.get("evidence_ledger", [])),
                }
                self.store.trace(cycle_id, "agent_stop_observation", evidence)
                if recovery_invocation:
                    self.store.trace(cycle_id, "self_recovery_resolved", {
                        "task_id": int(task.id),
                        "outcome": "agent_declared_stop" if task_done else "agent_yielded",
                        "active_self_version": active_self_version,
                        "current_self_version": self.self_versions.current_version(),
                        "self_evolution_observed": bool(
                            working_state.get("_self_evolution_observed", False)
                        ),
                        "host_fitness_judgment": None,
                    })
                task_result = {
                    "cycle_id": cycle_id, "summary": final_summary,
                    "user_message": canonical_answer.user_message,
                    "artifacts": [asdict(item) for item in canonical_answer.artifacts],
                    "canonical_answer": canonical_answer.as_dict(),
                    "final_output": final_output, "rounds": rounds,
                    "actions": [asdict(action) for action in all_actions],
                    "action_results": [asdict(result) for result in all_results],
                    "task_working_state": self._working_state_projection(working_state),
                    "situation_map": final_situation, "evidence": evidence,
                    "lineage": lineage,
                }
                if self.self_versions is not None:
                    task_result["self_modification"] = {
                        "current_version": self.self_versions.current_version(),
                        "active_cycle_version": active_self_version,
                        "transaction_opened": self.self_versions.writable,
                        "experiment_condition": self.settings.self_modification.experiment_condition,
                        "reflection_offered": self_reflection_offered,
                        "in_task_reflection_rounds": list(in_task_reflection_rounds),
                        "evolution_observed": bool(
                            working_state.get("_self_evolution_observed", False)
                        ),
                        "recovery_invocation": recovery_invocation,
                        "recovery_incident_ref": (
                            recovery_context.get("incident_ref")
                            if recovery_context is not None else None
                        ),
                    }
                committed = self.sandbox.commit(self.settings.workspace)
                canonical_answer.mark_committed()
                task_result["committed_files"] = committed
                task_result["artifacts"] = [asdict(item) for item in canonical_answer.artifacts]
                task_result["canonical_answer"] = canonical_answer.as_dict()
                task_result["final_output"] = self._published_output(final_output, snapshot)
                current_task_authored_candidate = bool(
                    skill_authoring is not None
                    and (
                        self._skill_candidate_written(all_actions, all_results)
                        or self._working_state_skill_candidate(working_state)
                    )
                )
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
                    if self.settings.skills.enabled and current_task_authored_candidate
                    else []
                )
                if skill_candidates:
                    task_result["skill_candidates"] = skill_candidates
                    self.store.trace(
                        cycle_id, "skill_candidates_ingested", {"candidates": skill_candidates}
                    )
                self.store.trace(cycle_id, "task_terminal_decision", {
                    "task_id": int(task.id), "status": status.value,
                    "reason": outcome, "decision_input_ref": "agent_declaration",
                    "true_completion": None,
                })
                self.store.finish_events(event_ids)
                self.store.update_task(int(task.id), status, result=task_result)
                self.store.add_checkpoint(int(task.id), status.value, task_result)
                self.sandbox.purge_task_dependencies(int(task.id))
                LOGGER.info(
                    "Task %s %s without online verification: %s",
                    task.id, status.value, final_summary,
                )
                return True
            coverage_assessment = self.situations.assess_coverage(
                final_situation, canonical_answer.body,
            )
            verification = self.verifier.verify(
                all_actions,
                all_results,
                planned_count=planned_count,
                task_done=task_done,
                request=task.request,
                contract=contract,
                final_output=canonical_answer.body,
                capability_assessment=preflight_assessment,
                completion_metadata=completion_metadata,
                coverage_assessment=coverage_assessment,
                established_evidence=list(working_state.get("evidence_ledger", [])),
                established_execution_evidence=bool(working_state.get("completed_steps")),
                unresolved_failures=list(working_state.get("unresolved_failures", [])),
            )
            ok = bool(verification["passed"])
            if ok:
                working_state.pop("verification_gap", None)
            else:
                missing_evidence = [
                    {"kind": check["name"], "detail": check.get("detail")}
                    for check in verification.get("checks", [])
                    if not check.get("passed") and check.get("layer") == "evidence"
                ]
                working_state["verification_gap"] = {
                    "required_evidence": missing_evidence,
                    "instruction": "Repair only the unresolved evidence gap; preserve valid established work.",
                    "operational_affordances": (
                        ["read(absolute http/https URL)"]
                        if self.capabilities.get("resource.http.read").state.value == "available"
                        else []
                    ),
                }
                working_state["pending"] = ["repair_verification_gap"]
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
                "budget_limits_enabled": task_budget.enabled,
                "model_tokens": task_budget.used_tokens,
                "task_tool_calls": task_budget.used_tool_calls,
                "cycle_model_api_calls": model_calls_used,
                "cycle_model_tokens": model_tokens_total,
                "task_cycles": task_budget.used_cycles,
                "rounds_to_first_computation": task_metrics["rounds_to_first_computation"],
                "dependency_provision_latency_ms": task_metrics["dependency_provision_latency_ms"],
                "repeated_resource_reads": task_metrics["repeated_resource_reads"],
                "repeated_resource_executions": task_metrics["repeated_resource_executions"],
                "observation_reuse_hits": task_metrics["observation_reuse_hits"],
                "redundant_resource_bypasses": task_metrics["redundant_resource_bypasses"],
                "environment_probe_calls": task_metrics["environment_probe_calls"],
                "sandbox_sessions": task_metrics["sandbox_sessions"],
                "sandbox_health_probes": task_metrics["sandbox_health_probes"],
                "adapter_retries": task_metrics["adapter_retries"],
                "adapter_transient_recoveries": task_metrics["adapter_transient_recoveries"],
                "protocol_repair_calls": task_metrics["protocol_repair_calls"],
                "protocol_repair_tokens": task_metrics["protocol_repair_tokens"],
                "failed_tool_calls": task_metrics["failed_tool_calls"],
                "post_failure_tool_changes": task_metrics["post_failure_tool_changes"],
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
                "coverage_assessment": coverage_assessment,
                "established_evidence": list(working_state.get("evidence_ledger", [])),
            }
            self.store.trace(cycle_id, "evaluation", evidence)
            task_result = {
                "cycle_id": cycle_id,
                "summary": final_summary,
                "user_message": canonical_answer.user_message,
                "artifacts": [asdict(item) for item in canonical_answer.artifacts],
                "canonical_answer": canonical_answer.as_dict(),
                "final_output": final_output,
                "rounds": rounds,
                "actions": [asdict(action) for action in all_actions],
                "action_results": [asdict(result) for result in all_results],
                "task_working_state": self._working_state_projection(working_state),
                "lineage": lineage,
                "situation_map": final_situation,
                "evidence": evidence,
            }
            if ok:
                committed = self.sandbox.commit(self.settings.workspace)
                canonical_answer.mark_committed()
                task_result["committed_files"] = committed
                task_result["artifacts"] = [asdict(item) for item in canonical_answer.artifacts]
                task_result["canonical_answer"] = canonical_answer.as_dict()
                task_result["final_output"] = self._published_output(final_output, snapshot)
                workspace_candidate_names = self._workspace_skill_candidate_names()
                current_task_authored_candidate = bool(
                    skill_authoring is not None
                    and (
                        self._skill_candidate_written(all_actions, all_results)
                        or self._working_state_skill_candidate(working_state)
                    )
                )
                if workspace_candidate_names and not current_task_authored_candidate:
                    self.store.trace(cycle_id, "skill_candidate_ingest_suppressed", {
                        "task_id": int(task.id),
                        "candidates": workspace_candidate_names,
                        "decision": "NO_ACTION",
                        "reason": "no_explicit_skill_authoring_attribution",
                    })
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
                    if (
                        self.settings.skills.enabled
                        and current_task_authored_candidate
                    ) else []
                )
                if skill_candidates:
                    task_result["skill_candidates"] = skill_candidates
                    self.store.trace(cycle_id, "skill_candidates_ingested", {"candidates": skill_candidates})
                self.store.trace(cycle_id, "task_terminal_decision", {
                    "task_id": int(task.id), "status": TaskStatus.COMPLETED.value,
                    "reason": "verification_passed", "decision_input_ref": "evaluation",
                })
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
                canonical_answer.mark_committed()
                task_result["committed_files"] = committed
                task_result["artifacts"] = [asdict(item) for item in canonical_answer.artifacts]
                task_result["canonical_answer"] = canonical_answer.as_dict()
                task_result["final_output"] = self._published_output(final_output, snapshot)
                self.store.trace(cycle_id, "task_terminal_decision", {
                    "task_id": int(task.id), "status": TaskStatus.DEGRADED.value,
                    "reason": "verification_degraded", "decision_input_ref": "evaluation",
                })
                self.store.finish_events(event_ids)
                self.store.update_task(int(task.id), TaskStatus.DEGRADED, result=task_result, error="Goal was only partially/substitutively satisfied")
                self.store.add_checkpoint(int(task.id), "degraded", task_result)
                self.sandbox.purge_task_dependencies(int(task.id))
                LOGGER.warning("Task %s completed in degraded state; memory write suppressed", task.id)
            else:
                workspace_checkpoint = self._checkpoint_failure_workspace(
                    int(task.id), cycle_id
                )
                task_result["workspace_checkpoint"] = workspace_checkpoint
                task_result["task_metrics"] = dict(task_metrics)
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
            model_calls_used += int(exc.model_usage.get("model_calls", 0))
            model_tokens_total += int(exc.model_usage.get("total_tokens", 0))
            failure_result = self._partial_failure_result(
                task, cycle_id, final_summary, working_state, task_metrics,
                all_actions, all_results, task_budget,
                model_calls_used=model_calls_used,
                model_tokens_total=model_tokens_total,
            )
            LOGGER.warning("Cycle %s rejected model response: %s", cycle_id, exc)
            self.store.trace(cycle_id, "cycle_failed", {
                "error": f"{type(exc).__name__}: {exc}",
                "failed_call_usage": dict(exc.model_usage),
                "workspace_checkpoint": failure_result.get("workspace_checkpoint"),
            })
            error = f"{type(exc).__name__}: {exc}"
            if task is None:
                self.store.finish_events(event_ids, error=error)
            else:
                self._handle_failure(
                    task, event, event_ids, error, cycle_id, failure_result,
                    model_calls_after=model_calls_used,
                    tokens_after=model_tokens_total,
                )
            return True
        except Exception as exc:
            failure_result = self._partial_failure_result(
                task, cycle_id, final_summary, working_state, task_metrics,
                all_actions, all_results, task_budget,
                model_calls_used=model_calls_used,
                model_tokens_total=model_tokens_total,
            )
            LOGGER.exception("Cycle %s failed", cycle_id)
            self.store.trace(cycle_id, "cycle_failed", {
                "error": f"{type(exc).__name__}: {exc}",
                "workspace_checkpoint": failure_result.get("workspace_checkpoint"),
            })
            error = f"{type(exc).__name__}: {exc}"
            if task is None:
                self.store.finish_events(event_ids, error=error)
            else:
                self._handle_failure(
                    task, event, event_ids, error, cycle_id, failure_result,
                    model_calls_after=model_calls_used,
                    tokens_after=model_tokens_total,
                )
            return True

    def _partial_failure_result(
        self,
        task: Task | None,
        cycle_id: str,
        summary: str,
        working_state: dict[str, object] | None,
        metrics: dict[str, object],
        actions: list[Action],
        results: list[ActionResult],
        budget: TaskBudget | None,
        *,
        model_calls_used: int,
        model_tokens_total: int,
    ) -> dict[str, object]:
        if budget is not None:
            budget.used_model_calls += model_calls_used
            budget.used_tool_calls += len(results)
            budget.used_tokens += model_tokens_total
        workspace_checkpoint = (
            self._checkpoint_failure_workspace(int(task.id), cycle_id)
            if task is not None and task.id is not None
            else {"preserved": False, "reason": "no_task"}
        )
        state = (
            self._working_state_projection(working_state)
            if isinstance(working_state, dict)
            else None
        )
        return {
            "cycle_id": cycle_id,
            "summary": summary,
            "actions": [asdict(action) for action in actions],
            "action_results": [asdict(result) for result in results],
            "task_working_state": state,
            "task_metrics": dict(metrics),
            "task_budget": asdict(budget) if budget is not None else None,
            "workspace_checkpoint": workspace_checkpoint,
            "evidence": {
                "success": False,
                "terminal": False,
                "cycle_model_api_calls": model_calls_used,
                "cycle_model_tokens": model_tokens_total,
                "executed_actions": len(results),
                "failed_actions": sum(not result.ok for result in results),
            },
        }

    def _checkpoint_failure_workspace(
        self, task_id: int, cycle_id: str,
    ) -> dict[str, object]:
        try:
            checkpoint = self.sandbox.checkpoint_task_workspace(task_id)
        except Exception as exc:
            self.sandbox.discard()
            checkpoint = {
                "preserved": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        self.store.trace(cycle_id, "task_workspace_checkpointed", {
            "task_id": task_id,
            **checkpoint,
            "published_to_production": False,
        })
        return checkpoint

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
    def _working_state_skill_candidate(state: dict[str, object]) -> bool:
        packages: dict[str, set[str]] = {}
        for item in state.get("available_artifacts", []):
            if not isinstance(item, dict):
                continue
            path = normalize_resource_path(item.get("path", ""))
            match = re.fullmatch(
                r"skill_candidates/([a-z][a-z0-9_]{1,63})/(manifest\.json|skill\.py)",
                path,
            )
            if match:
                packages.setdefault(match.group(1), set()).add(match.group(2))
        return any({"manifest.json", "skill.py"} <= files for files in packages.values())

    def _workspace_skill_candidate_names(self) -> list[str]:
        root = self.settings.workspace / "skill_candidates"
        if not root.is_dir():
            return []
        return sorted(
            item.name for item in root.iterdir()
            if item.is_dir()
            and (item / "manifest.json").is_file()
            and (item / "skill.py").is_file()
        )

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

    @staticmethod
    def _is_workspace_change(action: object, result: object) -> bool:
        """Return a factual durable-world change signal without judging its quality."""
        if not bool(getattr(result, "ok", False)):
            return False
        tool = str(getattr(action, "tool", ""))
        if tool in {"write", "write_file", "edit", "append_file", "evolve"}:
            return True
        output = getattr(result, "output", None)
        return bool(
            tool == "bash"
            and isinstance(output, dict)
            and isinstance(output.get("changes"), list)
            and output["changes"]
        )

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
            entry = {
                "round": item.get("round"),
                "tool": action.get("tool"),
                "arguments": cls._bounded_value(action.get("arguments", {}), 1000),
                "ok": result.get("ok"),
                "error": cls._bounded_value(result.get("error"), 1000),
                "output_summary": text[:2000],
            }
            if action.get("tool") == "observe" and isinstance(output, dict):
                # `observe` is already bounded at its source.  Preserve its factual
                # payload so the Agent can reason from what it chose to inspect.
                entry["observation"] = cls._bounded_value(output, 8000)
            compact.append(entry)
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
            enabled=config.enabled,
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
            "repeated_resource_reads": 0,
            "repeated_resource_executions": 0,
            "observation_reuse_hits": 0,
            "redundant_resource_bypasses": 0,
            "environment_probe_calls": 0,
            "sandbox_sessions": 0,
            "sandbox_health_probes": 0,
            "adapter_retries": 0,
            "adapter_transient_recoveries": 0,
            "protocol_repair_calls": 0,
            "protocol_repair_tokens": 0,
            "failed_tool_calls": 0,
            "post_failure_tool_changes": 0,
            "workspace_change_actions": 0,
            "rounds_since_workspace_change": 0,
            "liveness_checkpoints": 0,
        }
        for checkpoint in reversed(self.store.task_checkpoints(task_id)):
            if checkpoint["phase"] == "retry_reset":
                if checkpoint["data"].get("preserve_working_state"):
                    continue
                break
            if checkpoint["phase"] in {"budget_deferred", "failed_attempt"}:
                saved = checkpoint["data"].get("metrics", {})
                if isinstance(saved, dict):
                    metrics.update({key: saved[key] for key in metrics if key in saved})
                break
        return metrics

    def _task_working_state(self, task_id: int, objective: str) -> dict[str, object]:
        for checkpoint in reversed(self.store.task_checkpoints(task_id)):
            if checkpoint["phase"] == "retry_reset":
                if checkpoint["data"].get("preserve_working_state"):
                    continue
                break
            if checkpoint["phase"] in {"budget_deferred", "failed_attempt"}:
                state = checkpoint["data"].get("working_state")
                if isinstance(state, dict):
                    upgraded = self._upgrade_working_state(state)
                    if checkpoint["phase"] == "failed_attempt":
                        workspace_checkpoint = checkpoint["data"].get(
                            "workspace_checkpoint", {}
                        )
                        if (
                            isinstance(workspace_checkpoint, dict)
                            and workspace_checkpoint.get("preserved")
                        ):
                            return upgraded
                        return self._sanitize_retry_state(upgraded)
                    return upgraded
        return self._upgrade_working_state({
            "objective": objective,
            "established_facts": [],
            "completed_steps": [],
            "available_artifacts": [],
            "accessed_resources": [],
            "execution_environment": {},
            "pending": ["fulfil_objective_and_verify"],
            "important_evidence_refs": [],
            "evidence_ledger": [],
            "semantic_state": {},
            "unresolved_failures": [],
        })

    def _sanitize_retry_state(self, state: dict[str, object]) -> dict[str, object]:
        """Carry only evidence that remains valid after a failed sandbox is discarded."""
        import hashlib

        operational = state.get("operational", {})
        resources = operational.get("resources", {}) if isinstance(operational, dict) else {}
        valid_resources: dict[str, object] = {}
        if isinstance(resources, dict):
            for path, item in resources.items():
                if not isinstance(item, dict):
                    continue
                if str(path).casefold().startswith(("http://", "https://")):
                    valid_resources[str(path)] = item
                    continue
                digest = item.get("content_digest")
                target = (self.settings.workspace / str(path)).resolve()
                try:
                    target.relative_to(self.settings.workspace.resolve())
                except ValueError:
                    continue
                if not digest or not target.is_file():
                    continue
                current = hashlib.sha256(target.read_bytes()).hexdigest()
                if current == digest:
                    valid_resources[str(path)] = item
        if isinstance(operational, dict):
            operational["resources"] = valid_resources
            operational["artifacts"] = []
        state["accessed_resources"] = list(valid_resources)
        state["available_artifacts"] = []
        state["established_facts"] = [
            item for item in state.get("established_facts", [])
            if isinstance(item, dict) and str(item.get("resource")) in valid_resources
        ]
        # Network observations are immutable trace facts for this task attempt.
        # Command-success evidence is not carried because the failed snapshot was discarded.
        state["evidence_ledger"] = [
            item for item in state.get("evidence_ledger", [])
            if isinstance(item, dict) and item.get("kind") in {"network_request", "source_domain"}
        ]
        state["completed_steps"] = [
            item for item in state.get("completed_steps", [])
            if isinstance(item, dict) and str(item.get("step", "")).startswith("read:")
        ]
        state["unresolved_failures"] = []
        return state

    @staticmethod
    def _upgrade_working_state(state: dict[str, object]) -> dict[str, object]:
        """Upgrade v0.6.6 checkpoints into split semantic/operational state."""
        semantic = state.setdefault("semantic", {})
        if isinstance(semantic, dict):
            semantic.setdefault("established_facts", list(state.get("established_facts", [])))
            old_semantic = state.get("semantic_state")
            if isinstance(old_semantic, dict):
                semantic.setdefault("latest_model_summary", old_semantic.get("latest_model_summary", ""))
        operational = state.setdefault("operational", {})
        if not isinstance(operational, dict):
            operational = {}
            state["operational"] = operational
        resources = operational.setdefault("resources", {})
        if not isinstance(resources, dict):
            resources = {}
            operational["resources"] = resources
        for fact in state.get("established_facts", []):
            if not isinstance(fact, dict) or not fact.get("resource"):
                continue
            path = normalize_resource_path(fact["resource"])
            resources.setdefault(path, {
                "status": "read_complete", "complete": True, "representation": [],
                "metadata": fact.get("metadata", {}), "evidence_ref": fact.get("evidence_ref"),
                "last_request_ref": fact.get("evidence_ref"), "content_digest": None, "range": {},
                "access_count": 1, "execution_count": 1, "reuse_count": 0,
                "coverage_labels": coverage_labels(path),
            })
        operational.setdefault("environment", state.get("execution_environment", {}))
        operational.setdefault("artifacts", state.get("available_artifacts", []))
        ledger = state.setdefault("evidence_ledger", [])
        if not isinstance(ledger, list):
            state["evidence_ledger"] = []
        unresolved = state.setdefault("unresolved_failures", [])
        if not isinstance(unresolved, list):
            state["unresolved_failures"] = []
        return state

    @staticmethod
    def _harness_context_projection(
        context: dict[str, object], harness_settings: dict[str, object],
    ) -> dict[str, object]:
        profile = str(harness_settings.get("harness_profile", "structured"))
        if profile == "structured":
            return context
        common = {
            key: context[key] for key in (
                "workspace_inventory", "harness", "harness_version", "lineage", "budget",
                "observations", "round", "_protocol_messages",
            ) if key in context
        }
        if profile == "reduced":
            for key in (
                "evidence_contract", "continuation", "task_working_state", "environment",
            ):
                if key in context:
                    common[key] = context[key]
            return common
        if profile == "minimal_open":
            state = context.get("task_working_state")
            if isinstance(state, dict):
                common["task_working_state"] = {
                    key: state[key] for key in (
                        "completed_steps", "pending", "unresolved_failures",
                    ) if key in state
                }
            return common
        if profile == "minimal_self":
            projected = {
                key: context[key] for key in (
                    "workspace_inventory", "retrieved_memories", "harness", "budget",
                    "round", "_protocol_messages", "self_system_prompt",
                    "self_goal", "self_experiment_condition", "self_state",
                    "self_observation", "self_reflection", "self_recovery",
                ) if key in context
            }
            observations = context.get("observations")
            if isinstance(observations, list):
                # Raw action results are immutable trace evidence, not bounded model
                # context.  In particular, a command may legitimately emit several
                # megabytes; never make a pass-through Self Harness copy that payload.
                projected["observations"] = AIOSRuntime._compact_observations(observations)
            state = context.get("task_working_state")
            if isinstance(state, dict):
                projected["task_working_state"] = {
                    key: state[key] for key in (
                        "completed_steps", "pending", "unresolved_failures",
                        "available_artifacts", "established_facts",
                    ) if key in state
                }
            return projected
        return context

    def _agent_observe(
        self, *, view: str, task_id: int | None = None, ref: str | None = None,
        after_id: int = 0, offset: int = 0, limit: int | None = None,
    ) -> dict[str, object]:
        """Expose bounded stored observations while leaving all interpretation to Self."""
        selected_task_id = task_id or self._active_observation_task_id
        if view == "tasks":
            bounded = max(1, min(int(limit or 20), 50))
            return {
                "schema": "agent_observation/v1", "view": view,
                "host_interpretation": None,
                "tasks": [
                    {
                        "task_id": item.id, "title": item.title,
                        "request_prefix": item.request[:1000],
                        "request_characters": len(item.request),
                        "status": item.status.value, "attempts": item.attempts,
                        "created_at": item.created_at, "updated_at": item.updated_at,
                    }
                    for item in self.store.list_tasks(limit=bounded)
                ],
            }
        if view == "self_versions":
            versions = (
                self.self_versions.versions_summary()
                if self.self_versions is not None else []
            )
            return {
                "schema": "agent_observation/v1", "view": view,
                "host_interpretation": None, "versions": versions,
            }
        if selected_task_id is None:
            raise ValueError("task_id is required outside an active task")
        if self.store.get_task(int(selected_task_id)) is None:
            raise ValueError(f"Unknown task: {selected_task_id}")
        if view == "task_events":
            catalog = self.store.task_trace_catalog(
                int(selected_task_id), after_id=after_id,
                limit=max(1, min(int(limit or 50), 100)),
            )
            return {
                "schema": "agent_observation/v1", "view": view,
                "host_interpretation": None, **catalog,
            }
        if view == "trace":
            if not isinstance(ref, str) or not ref.startswith("trace:"):
                raise ValueError("trace view requires ref='trace:<integer>'")
            try:
                trace_id = int(ref.split(":", 1)[1])
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid trace reference") from exc
            record = self.store.task_trace_record(int(selected_task_id), trace_id)
            if record is None:
                raise ValueError("Trace does not belong to the selected task")
            raw = str(record.pop("data"))
            bounded_offset = max(0, int(offset))
            bounded_limit = max(1, min(int(limit or 8000), 12_000))
            content = raw[bounded_offset:bounded_offset + bounded_limit]
            return {
                "schema": "agent_observation/v1", "view": view,
                "host_interpretation": None, "task_id": int(selected_task_id),
                "ref": f"trace:{trace_id}", **record,
                "offset": bounded_offset, "limit": bounded_limit,
                "total_characters": len(raw), "content": content,
                "truncated": bounded_offset + len(content) < len(raw),
                "next_offset": bounded_offset + len(content),
            }
        raise ValueError(f"Unknown observation view: {view}")

    @staticmethod
    def _self_harness_actions(
        original: list[Action], selected: object, autonomous: object = None,
        *, max_autonomous_actions: int = 0,
    ) -> list[Action]:
        """Route model calls and admit bounded actions authored by Self code."""
        if not isinstance(selected, list) or len(selected) != len(original):
            raise RuntimeError(
                "Self Harness tool_policy must return exactly one action per model tool call"
            )
        result: list[Action] = []
        for source, value in zip(original, selected, strict=True):
            if not isinstance(value, dict):
                raise RuntimeError("Self Harness tool_policy actions must be objects")
            tool = value.get("tool")
            arguments = value.get("arguments", {})
            if not isinstance(tool, str) or not isinstance(arguments, dict):
                raise RuntimeError("Self Harness tool_policy returned an invalid action")
            result.append(Action(
                tool=tool,
                arguments=arguments,
                reason=str(value.get("reason") or source.reason)[:2000],
                call_id=source.call_id,
            ))
        additional = [] if autonomous is None else autonomous
        if not isinstance(additional, list):
            raise RuntimeError("Self Agent autonomous_actions must be an array")
        if len(additional) > max(0, int(max_autonomous_actions)):
            raise RuntimeError("Self Agent autonomous_actions exceed the Host hard per-round ceiling")
        for value in additional:
            if not isinstance(value, dict):
                raise RuntimeError("Self Agent autonomous actions must be objects")
            tool = value.get("tool")
            arguments = value.get("arguments", {})
            if not isinstance(tool, str) or not isinstance(arguments, dict):
                raise RuntimeError("Self Agent returned an invalid autonomous action")
            result.append(Action(
                tool=tool,
                arguments=arguments,
                reason=("[self-agent] " + str(value.get("reason") or "architecture-authored action"))[:2000],
                call_id=None,
            ))
        return result

    @staticmethod
    def _self_runtime_trace_kind(run: dict[str, object]) -> str:
        return (
            "self_agent_applied"
            if run.get("runtime_kind") == "agent"
            else "self_harness_applied"
        )

    def _working_state_projection(self, state: dict[str, object]) -> dict[str, object]:
        projected = {key: value for key, value in state.items() if not key.startswith("_")}
        import json
        raw = json.dumps(projected, ensure_ascii=False, default=str)
        limit = self.settings.budget.working_state_characters
        if len(raw) <= limit:
            return projected
        return {
            "objective": projected.get("objective"),
            "semantic": projected.get("semantic", {}),
            "operational": projected.get("operational", {}),
            "established_facts": list(projected.get("established_facts", []))[-12:],
            "completed_steps": list(projected.get("completed_steps", []))[-16:],
            "available_artifacts": list(projected.get("available_artifacts", []))[-12:],
            "accessed_resources": list(projected.get("accessed_resources", []))[-12:],
            "execution_environment": projected.get("execution_environment", {}),
            "pending": projected.get("pending", []),
            "important_evidence_refs": list(projected.get("important_evidence_refs", []))[-16:],
            "evidence_ledger": list(projected.get("evidence_ledger", []))[-32:],
            "semantic_state": projected.get("semantic_state", {}),
            "unresolved_failures": list(projected.get("unresolved_failures", []))[-16:],
            "compacted": True,
        }

    def _update_working_state(
        self, state: dict[str, object], action: object, result: object, trace_id: int,
    ) -> None:
        import hashlib

        tool = str(getattr(action, "tool", ""))
        arguments = getattr(action, "arguments", {})
        ok = bool(getattr(result, "ok", False))
        reference = f"trace:{trace_id}"
        refs = state.setdefault("important_evidence_refs", [])
        if reference not in refs:
            refs.append(reference)
            del refs[:-16]
        action_key = Verifier._action_key(action)
        unresolved = state.setdefault("unresolved_failures", [])
        if not isinstance(unresolved, list):
            unresolved = []
            state["unresolved_failures"] = unresolved
        unresolved[:] = [
            item for item in unresolved
            if not (isinstance(item, dict) and item.get("action_key") == action_key)
        ]
        if not ok:
            unresolved.append({
                "action_key": action_key,
                "tool": tool,
                "error": getattr(result, "error", None),
                "evidence_ref": reference,
            })
            del unresolved[:-16]
            return
        path = normalize_resource_path(arguments.get("path", ""))
        if tool == "read" and path:
            resources = state.setdefault("accessed_resources", [])
            if path not in resources:
                resources.append(path)
            output = getattr(result, "output", None)
            resource = output.get("resource", {}) if isinstance(output, dict) else {}
            metadata = resource.get("metadata")
            representations = resource.get("representations", []) if isinstance(resource, dict) else []
            cache_metadata = output.get("observation_cache", {}) if isinstance(output, dict) else {}
            representation_kinds = [
                str(item.get("kind")) for item in representations
                if isinstance(item, dict) and item.get("kind")
            ]
            complete = bool(representations) and not any(
                bool(item.get("truncated")) for item in representations if isinstance(item, dict)
            )
            operational = state.setdefault("operational", {})
            if not isinstance(operational, dict):
                operational = {}
                state["operational"] = operational
            resource_states = operational.setdefault("resources", {})
            if not isinstance(resource_states, dict):
                resource_states = {}
                operational["resources"] = resource_states
            previous = resource_states.get(path, {})
            reused = bool(cache_metadata.get("hit")) if isinstance(cache_metadata, dict) else False
            source_reference = (
                cache_metadata.get("source_observation_ref")
                if reused and isinstance(cache_metadata, dict) else None
            ) or reference
            resource_states[path] = {
                "status": "read_complete" if complete else "read_partial",
                "complete": complete,
                "representation": representation_kinds,
                "metadata": metadata or {},
                "content_digest": cache_metadata.get("content_digest") if isinstance(cache_metadata, dict) else None,
                "range": {
                    "offset": cache_metadata.get("offset", 0),
                    "limit": cache_metadata.get("limit"),
                } if isinstance(cache_metadata, dict) else {},
                "evidence_ref": source_reference,
                "last_request_ref": reference,
                "access_count": int(previous.get("access_count", 0)) + 1 if isinstance(previous, dict) else 1,
                "execution_count": int(previous.get("execution_count", 0)) + (0 if reused else 1) if isinstance(previous, dict) else (0 if reused else 1),
                "reuse_count": int(previous.get("reuse_count", 0)) + (1 if reused else 0) if isinstance(previous, dict) else (1 if reused else 0),
                "coverage_labels": coverage_labels(path, resource if isinstance(resource, dict) else None),
                "semantic_residue": (
                    self._semantic_residue(representations)
                    or (previous.get("semantic_residue", "") if isinstance(previous, dict) else "")
                ),
            }
            self._bound_semantic_residues(resource_states)
            if metadata:
                fact = {"state": "ESTABLISHED", "resource": path, "metadata": metadata, "evidence_ref": source_reference}
                facts = state.setdefault("established_facts", [])
                facts[:] = [item for item in facts if not (isinstance(item, dict) and item.get("resource") == path)]
                facts.append(fact)
                semantic = state.setdefault("semantic", {})
                if isinstance(semantic, dict):
                    semantic["established_facts"] = list(facts)[-12:]
        if tool in {"write", "edit", "write_file", "append_file"} and path:
            artifacts = state.setdefault("available_artifacts", [])
            artifact = {"path": path, "state": "AVAILABLE", "evidence_ref": reference}
            artifacts[:] = [item for item in artifacts if not (isinstance(item, dict) and item.get("path") == path)]
            artifacts.append(artifact)
            operational = state.setdefault("operational", {})
            if isinstance(operational, dict):
                operational["artifacts"] = list(artifacts)[-12:]
        if tool == "bash":
            command = str(arguments.get("command", ""))
            step = "bash:" + hashlib.sha256(command.encode("utf-8")).hexdigest()[:12]
        else:
            step = f"{tool}:{path or 'operation'}"
        completed = state.setdefault("completed_steps", [])
        if not any(isinstance(item, dict) and item.get("step") == step for item in completed):
            completed.append({"step": step, "state": "DONE", "evidence_ref": reference})

    def _record_contract_evidence(
        self,
        state: dict[str, object],
        contract: EvidenceContract,
        action: object,
        result: object,
        trace_id: int,
    ) -> None:
        if not isinstance(action, Action) or not isinstance(result, ActionResult):
            return
        collected = self.verifier.collect_evidence(
            contract, action, result, f"trace:{trace_id}",
        )
        if not collected:
            return
        ledger = state.setdefault("evidence_ledger", [])
        if not isinstance(ledger, list):
            ledger = []
            state["evidence_ledger"] = ledger
        for item in collected:
            ledger[:] = [
                previous for previous in ledger
                if not (
                    isinstance(previous, dict)
                    and previous.get("kind") == item["kind"]
                    and previous.get("value") == item["value"]
                )
            ]
            ledger.append(item)
        del ledger[:-32]

    @staticmethod
    def _semantic_residue(representations: list[object], *, limit: int = 1200) -> str:
        """Carry bounded resource meaning across cycles when rereading costs more."""
        texts = [
            str(item.get("text")) for item in representations
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("text").strip()
        ]
        value = re.sub(r"\s+", " ", "\n".join(texts)).strip()
        if len(value) <= limit:
            return value
        head = max(1, int(limit * 0.72))
        tail = max(1, limit - head - 3)
        return value[:head].rstrip() + " … " + value[-tail:].lstrip()

    @staticmethod
    def _bound_semantic_residues(
        resources: dict[str, object], *, total_limit: int = 4000,
    ) -> None:
        """Keep semantic carry cheaper than repeated reads for large resource sets."""
        remaining = total_limit
        ordered = sorted(
            resources.items(),
            key=lambda item: (
                -int(item[1].get("access_count", 0)) if isinstance(item[1], dict) else 0,
                item[0],
            ),
        )
        for _, state in ordered:
            if not isinstance(state, dict):
                continue
            residue = str(state.get("semantic_residue", ""))
            if not residue:
                continue
            if remaining <= 0:
                state["semantic_residue"] = ""
                continue
            state["semantic_residue"] = residue[:remaining]
            remaining -= len(str(state["semantic_residue"]))

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
        remaining: int | None,
        reserved_completion_calls: int,
    ) -> tuple[list, list]:
        if remaining is None:
            return list(requested), []
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
            verifier_passed=(
                None if self.settings.runtime.completion_mode == "free" else False
            ),
            task_outcome="failed_attempt",
            model_calls_after=model_calls_after,
            tokens_after=tokens_after,
        )
        self.store.finish_events(event_ids, error=error)
        failure_class = (
            "missing_executable"
            if "MissingExecutable:" in error
            else (
                "execution_or_runtime"
                if self.settings.runtime.completion_mode == "free"
                else "execution_or_verification"
            )
        )
        working_state = result.get("task_working_state") if isinstance(result, dict) else None
        workspace_checkpoint = (
            result.get("workspace_checkpoint") if isinstance(result, dict) else None
        )
        preserved_workspace = bool(
            isinstance(workspace_checkpoint, dict)
            and workspace_checkpoint.get("preserved")
        )
        self.store.add_checkpoint(task_id, "failed_attempt", {
            "error": error,
            "cycle_id": cycle_id,
            "failure_class": failure_class,
            "verification_gap": (
                working_state.get("verification_gap") if isinstance(working_state, dict) else None
            ),
            "working_state": working_state if isinstance(working_state, dict) else None,
            "metrics": (
                result.get("task_metrics") if isinstance(result, dict)
                and isinstance(result.get("task_metrics"), dict) else None
            ),
            "workspace_checkpoint": workspace_checkpoint,
        })
        evolution_result = self.evolution.observe_failure(task, error, result)
        if evolution_result.get("triggered"):
            self.store.trace(cycle_id, "evolution_triggered", evolution_result)
            self.store.add_checkpoint(task_id, "evolution", evolution_result)
        if evolution_result.get("changed") or evolution_result.get("rolled_back_tools"):
            self._reload_generated_tools()
        terminal_protocol_failure = "Model protocol repair failed:" in error
        missing_executable_attempts = sum(
            checkpoint["phase"] == "failed_attempt"
            and checkpoint["data"].get("failure_class") == "missing_executable"
            for checkpoint in self.store.task_checkpoints(task_id)
        )
        capabilities = (
            result.get("situation_map", {}).get("capabilities", [])
            if isinstance(result, dict) else []
        )
        alternative_provider_available = any(
            isinstance(item, dict)
            and item.get("name") == "resource.http.read"
            and item.get("state") == "available"
            and item.get("interface") == "read(URL)"
            for item in capabilities
        )
        guided_missing_executable_retry = (
            failure_class != "missing_executable"
            or (alternative_provider_available and missing_executable_attempts == 1)
        )
        if (
            event.type != "SELF_RECOVERY"
            and
            task.attempts < task.max_attempts
            and not terminal_protocol_failure
            and guided_missing_executable_retry
        ):
            reset = {
                "previous_status": task.status.value,
                "reason": "automatic_retry",
                "failed_cycle_id": cycle_id,
                "completed_attempt": task.attempts,
                "next_attempt": task.attempts + 1,
                "preserve_working_state": preserved_workspace,
            }
            reset_id = self.store.add_checkpoint(task_id, "retry_reset", reset)
            self.store.trace(cycle_id, "retry_reset", {
                "task_id": task_id, "checkpoint_id": reset_id, **reset,
            })
            self.store.update_task(task_id, TaskStatus.RETRYING, result=result, error=error)
            payload = dict(event.payload)
            payload["task_id"] = task_id
            retry_type = event.type
            if event.type == "TASK_CONTINUE" or payload.get("continuation"):
                # A continuation identifies one fenced checkpoint. Replaying it
                # as a retry would never increment task.attempts.
                retry_type = "TASK_REQUEST"
                for key in ("continuation", "checkpoint_id", "generation"):
                    payload.pop(key, None)
            retry_id = self.store.add_event(
                Event(retry_type, payload, max(1, event.priority - 1))
            )
            self.store.trace(
                cycle_id,
                "retry_scheduled",
                {
                    "task_id": task_id, "attempt": task.attempts,
                    "max_attempts": task.max_attempts, "event_id": retry_id,
                    "event_type": retry_type,
                },
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
            if self._schedule_self_recovery(
                task, event, error, cycle_id, result, failure_class,
            ):
                return
            if event.type == "SELF_RECOVERY":
                recovery = (
                    event.payload.get("self_recovery")
                    if isinstance(event.payload.get("self_recovery"), dict)
                    else {}
                )
                self.store.trace(cycle_id, "self_recovery_failed", {
                    "task_id": task_id,
                    "incident_ref": recovery.get("incident_ref"),
                    "recovery_base_version": recovery.get("recovery_base_version"),
                    "same_self_lineage": True,
                    "failure_class": failure_class,
                    "error": self._redact_incident_text(error[:4000]),
                    "further_recovery_scheduled": False,
                })
            final_status = (
                TaskStatus.ABANDONED
                if self.settings.runtime.completion_mode == "free"
                else TaskStatus.DEAD_LETTER
            )
            self.store.trace(cycle_id, "task_terminal_decision", {
                "task_id": task_id, "status": final_status.value,
                "reason": error, "decision_input_ref": "failed_attempt",
            })
            self.store.update_task(task_id, final_status, result=result, error=error)
            self.sandbox.purge_task_dependencies(task_id)
            dead_id = None
            if final_status == TaskStatus.DEAD_LETTER:
                dead_id = self.store.add_dead_letter(task_id, event, error)
                self.store.trace(cycle_id, "dead_lettered", {"task_id": task_id, "dead_letter_id": dead_id})
            else:
                self.store.trace(cycle_id, "task_abandoned", {
                    "task_id": task_id, "reason": error, "true_completion": None,
                })
            if terminal_protocol_failure and task.attempts < task.max_attempts:
                LOGGER.error("Task %s entered %s after terminal protocol repair failure", task_id, final_status.value)
            else:
                LOGGER.error("Task %s exhausted retries and entered %s", task_id, final_status.value)

    def _schedule_self_recovery(
        self,
        task: Task,
        event: Event,
        error: str,
        cycle_id: str,
        result: dict | None,
        failure_class: str,
    ) -> bool:
        """Give the same Self lineage one bounded invocation after terminal failure.

        The Host records and forwards observable facts.  It neither diagnoses
        the failure nor requires a mutation.  A lifecycle failure may boot the
        recorded parent solely so broken mutable code cannot prevent recovery.
        """
        config = self.settings.self_modification
        if (
            self.self_versions is None
            or not config.failure_recovery_enabled
            or config.max_failure_recovery_invocations <= 0
            or event.type == "SELF_RECOVERY"
        ):
            return False
        prior = sum(
            checkpoint["phase"] == "self_recovery_scheduled"
            for checkpoint in self.store.task_checkpoints(int(task.id))
        )
        if prior >= config.max_failure_recovery_invocations:
            return False

        current_version = self.self_versions.current_version()
        self_lifecycle_failure = bool(re.search(
            r"Self (?:Agent|Harness)|Self version has no .*entrypoint|"
            r"lifecycle event",
            error,
            flags=re.IGNORECASE,
        ))
        parent_version = (
            self.self_versions.parent_version(current_version)
            if self_lifecycle_failure else None
        )
        recovery_base = parent_version or current_version
        failed_actions: list[dict[str, object]] = []
        if isinstance(result, dict):
            action_results = result.get("action_results", [])
            if not isinstance(action_results, list):
                action_results = []
            for item in action_results[-8:]:
                if not isinstance(item, dict) or item.get("ok") is not False:
                    continue
                failed_actions.append({
                    "tool": str(item.get("tool") or "unknown")[:80],
                    "error": self._redact_incident_text(
                        str(item.get("error") or "unknown error")[:1000]
                    ),
                })
        capsule: dict[str, object] = {
            "schema": "self_recovery_incident/v1",
            "trigger": "terminal_execution_failure",
            "task_id": int(task.id),
            "failed_cycle_id": cycle_id,
            "ordinary_attempt": task.attempts,
            "ordinary_max_attempts": task.max_attempts,
            "failure_class": failure_class,
            "error": self._redact_incident_text(error[:4000]),
            "failed_actions": failed_actions,
            "failed_self_version": current_version,
            "recovery_base_version": recovery_base,
            "parent_fallback_used": recovery_base != current_version,
            "self_transaction_open": self.self_versions.writable,
            "same_self_lineage": True,
            "host_diagnosis": None,
            "mutation_required": False,
            "allowed_interpretations": (
                "Continue the original task with a revised strategy, or create a "
                "reversible Self descendant when the observed defect is reusable."
            ),
        }
        trace_id = self.store.trace(cycle_id, "self_recovery_incident", capsule)
        capsule["incident_ref"] = f"trace:{trace_id}"
        checkpoint_id = self.store.add_checkpoint(
            int(task.id), "self_recovery_scheduled", capsule,
        )
        self.store.update_task(
            int(task.id), TaskStatus.RETRYING, result=result, error=error,
        )
        recovery_event_id = self.store.add_event(Event(
            "SELF_RECOVERY",
            {
                "task_id": int(task.id),
                "message": task.request,
                "self_recovery": capsule,
                "recovery_checkpoint_id": checkpoint_id,
            },
            task.priority,
        ))
        self.store.trace(cycle_id, "self_recovery_scheduled", {
            "task_id": int(task.id),
            "event_id": recovery_event_id,
            "checkpoint_id": checkpoint_id,
            "incident_ref": capsule["incident_ref"],
            "recovery_base_version": recovery_base,
            "parent_fallback_used": recovery_base != current_version,
            "same_self_lineage": True,
            "host_fitness_judgment": None,
        })
        LOGGER.warning(
            "Task %s scheduled bounded Self recovery event %s from %s",
            task.id, recovery_event_id, recovery_base,
        )
        return True

    @staticmethod
    def _redact_incident_text(value: str) -> str:
        text = re.sub(
            r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", value,
        )
        text = re.sub(
            r"(?i)((?:api[_-]?key|access[_-]?token|token|password|secret|cookie)"
            r"\s*[:=]\s*)[^\s,;]+",
            r"\1<redacted>",
            text,
        )
        return re.sub(
            r"(?i)\b[A-Z]:\\Users\\[^\\\s]+", "<host-user-path>", text,
        )

    def _reload_generated_tools(self) -> None:
        registry = ToolRegistry(
            self.settings.permissions,
            None if self.self_versions is not None else self.plugins,
            self.sandbox, self.self_versions, self._agent_observe,
        )
        self.executor.registry = registry
        self.controller.set_tool_schemas(registry.schemas())
