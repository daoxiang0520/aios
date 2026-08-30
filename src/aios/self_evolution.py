from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from typing import Any, Protocol

from .controller import ControllerError, LLMController
from .evolution import ALLOWED_MUTATIONS, EvolutionManager, EvolutionPolicyError
from .experiments import ExperimentOrchestrator, ExperimentVariant
from .experiments.models import CapsuleStatus, PromotionState
from .storage import StateStore


IMMUTABLE_KERNEL = (
    "authority", "credentials", "security_kernel", "sandbox_isolation",
    "audit", "experiment_boundary", "rollback", "human_override",
)


class EvolutionReasoner(Protocol):
    def reason(self, experience: dict[str, Any]) -> dict[str, Any]: ...


class ExperienceAnalyzer:
    """Compress cross-task telemetry into evidence; it does not choose the architecture."""

    def __init__(self, store: StateStore):
        self.store = store

    def analyze(self, *, task_limit: int = 100, trace_limit: int = 1000) -> dict[str, Any]:
        tasks = self.store.list_tasks(max(1, min(task_limit, 500)))
        traces = self.store.recent_traces(max(1, min(trace_limit, 5000)))
        statuses = Counter(task.status.value for task in tasks)
        failed_checks: Counter[str] = Counter()
        failure_types: Counter[str] = Counter()
        metric_totals: Counter[str] = Counter()
        expensive_tasks = []
        metric_names = (
            "model_api_calls", "model_tokens", "repeated_resource_reads",
            "repeated_resource_executions", "observation_reuse_hits",
            "redundant_resource_bypasses", "environment_probe_calls",
            "protocol_repair_calls", "protocol_repair_tokens", "adapter_retries",
        )
        for task in tasks:
            result = task.result if isinstance(task.result, dict) else {}
            evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
            for name in metric_names:
                metric_totals[name] += int(evidence.get(name, 0) or 0)
            verification = evidence.get("verification") if isinstance(evidence.get("verification"), dict) else {}
            for check in verification.get("checks", []):
                if isinstance(check, dict) and not check.get("passed"):
                    failed_checks[str(check.get("name", "unknown"))] += 1
            tokens = int(evidence.get("model_tokens", 0) or 0)
            calls = int(evidence.get("model_api_calls", 0) or 0)
            if tokens or calls:
                expensive_tasks.append({
                    "task_id": task.id, "status": task.status.value,
                    "tokens": tokens, "model_calls": calls,
                })
            if task.error:
                failure_types[str(task.error).split(":", 1)[0]] += 1
        tool_failures: Counter[str] = Counter()
        trace_kinds: Counter[str] = Counter()
        for trace in traces:
            trace_kinds[str(trace.get("kind", "unknown"))] += 1
            data = trace.get("data") if isinstance(trace.get("data"), dict) else {}
            if trace.get("kind") == "action_result" and not data.get("ok", False):
                tool_failures[str(data.get("tool", "unknown"))] += 1
            if trace.get("kind") == "cycle_failed":
                failure_types[str(data.get("error", "unknown")).split(":", 1)[0]] += 1
        expensive_tasks.sort(key=lambda item: (-item["tokens"], -item["model_calls"], item["task_id"] or 0))
        patterns = []
        for name, count in (
            ("repeated_resource_requests", metric_totals["repeated_resource_reads"]),
            ("redundant_resource_execution", metric_totals["repeated_resource_executions"]),
            ("environment_probing", metric_totals["environment_probe_calls"]),
            ("protocol_repair", metric_totals["protocol_repair_calls"]),
            ("adapter_retry", metric_totals["adapter_retries"]),
        ):
            if count:
                patterns.append({"name": name, "occurrences": count})
        signature_material = {
            "statuses": dict(statuses), "failed_checks": dict(failed_checks),
            "failure_types": dict(failure_types), "metrics": dict(metric_totals),
        }
        signature = hashlib.sha256(
            json.dumps(signature_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return {
            "schema": "experience/v1", "signature": signature,
            "sample": {"tasks": len(tasks), "traces": len(traces)},
            "task_statuses": dict(statuses), "failed_checks": dict(failed_checks),
            "failure_types": dict(failure_types), "tool_failures": dict(tool_failures),
            "metrics": dict(metric_totals), "patterns": patterns,
            "highest_cost_tasks": expensive_tasks[:10],
            "mutable_environment": {
                "surface": "harness", "allowed_mutations": sorted(ALLOWED_MUTATIONS),
                "max_mutations_per_candidate": 1,
            },
            "immutable_kernel": list(IMMUTABLE_KERNEL),
        }


class SoftFrictionExperienceBuilder:
    """Project bounded facts from successful tasks without naming a friction or fix."""

    METRICS = (
        "model_tokens", "model_api_calls", "task_cycles", "planned_actions",
        "executed_actions", "failed_actions", "repeated_resource_reads",
        "repeated_resource_executions", "observation_reuse_hits",
        "context_reuse_ratio", "protocol_repair_calls",
        "dependency_provision_latency_ms", "rounds_to_first_computation",
    )

    STRATEGY_FAILURE_STATUSES = {"failed", "dead_letter"}

    def __init__(self, store: StateStore, included_task_ids: list[int] | None = None):
        self.store = store
        self.included_task_ids = list(dict.fromkeys(included_task_ids or []))

    def analyze(
        self, *, task_limit: int = 10, trace_limit_per_task: int = 200,
        trace_limit: int | None = None,
    ) -> dict[str, Any]:
        if trace_limit is not None:
            trace_limit_per_task = trace_limit
        successful = [
            task for task in self.store.list_tasks(max(1, min(task_limit * 5, 500)))
            if task.status.value == "completed"
            and isinstance(task.result, dict)
            and isinstance(task.result.get("evidence"), dict)
            and bool(task.result["evidence"].get("success"))
        ][:max(1, min(task_limit, 50))]
        explicit_failures = []
        for task_id in self.included_task_ids:
            task = self.store.get_task(int(task_id))
            if task is None or task.status.value not in self.STRATEGY_FAILURE_STATUSES:
                continue
            confirmation = self._runtime_regression_confirmation(int(task_id))
            if confirmation["confirmed"]:
                continue
            explicit_failures.append(task)
        tasks = list({int(task.id): task for task in [*successful, *explicit_failures]}.values())
        observations = [
            self._task_facts(task, trace_limit=max(20, min(trace_limit_per_task, 1000)))
            for task in tasks
        ]
        material = {
            "task_ids": [item["task_id"] for item in observations],
            "observations": observations,
        }
        signature = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return {
            "schema": "soft_friction_experience/v1",
            "signature": signature,
            "selection": {
                "automatic_source": "completed_and_verifier_success",
                "explicit_source": "failed_or_dead_letter_without_host_confirmed_runtime_regression",
                "explicit_task_ids": self.included_task_ids,
                "maximum_automatic_tasks": max(1, min(task_limit, 50)),
                "maximum_traces_per_task": max(20, min(trace_limit_per_task, 1000)),
            },
            "tasks": observations,
            "fact_policy": {
                "diagnosis_provided": False,
                "bad_behavior_labels_provided": False,
                "recommended_strategy_provided": False,
                "unknown_values_are_null": True,
            },
            "mutable_surface": {
                "kinds": [
                    "prompt_or_procedure", "context_selection", "retrieval_strategy",
                    "tool_use_workflow", "cycle_strategy", "procedural_skill",
                ],
                "executable_harness_mutations": sorted(ALLOWED_MUTATIONS),
                "one_mutation_per_candidate": True,
            },
            "immutable_kernel": list(IMMUTABLE_KERNEL),
        }

    def _task_facts(self, task: Any, *, trace_limit: int) -> dict[str, Any]:
        result = task.result if isinstance(task.result, dict) else {}
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        checkpoints = self.store.task_checkpoints(int(task.id))
        cycle_ids = list(dict.fromkeys(
            str(item.get("data", {}).get("cycle_id"))
            for item in checkpoints if item.get("data", {}).get("cycle_id")
        ))
        traces = self.store.traces_for_cycles(cycle_ids)[:trace_limit]
        plans: dict[tuple[str, int], list[dict[str, Any]]] = {}
        result_offsets: dict[tuple[str, int], int] = {}
        actions = []
        for trace in traces:
            data = trace.get("data") if isinstance(trace.get("data"), dict) else {}
            key = (str(trace.get("cycle_id")), int(data.get("round", 0) or 0))
            if trace.get("kind") == "plan_created":
                plans[key] = [
                    item for item in data.get("actions", []) if isinstance(item, dict)
                ]
                continue
            if trace.get("kind") != "action_result":
                continue
            offset = result_offsets.get(key, 0)
            result_offsets[key] = offset + 1
            planned = plans.get(key, [])[offset] if offset < len(plans.get(key, [])) else {}
            arguments = planned.get("arguments") if isinstance(planned.get("arguments"), dict) else {}
            command = str(arguments.get("command") or "")
            actions.append({
                "trace_id": trace.get("id"), "cycle_id": trace.get("cycle_id"),
                "round": int(data.get("round", 0) or 0),
                "tool": data.get("tool") or planned.get("tool"), "ok": bool(data.get("ok")),
                "error_type": str(data.get("error") or "").split(":", 1)[0] or None,
                "exit_code": self._exit_code(data.get("error")),
                "command_excerpt": command[:800] if command else None,
                "command_family": self._command_family(command) if command else None,
            })
        established = (
            evidence.get("established_evidence")
            if isinstance(evidence.get("established_evidence"), list) else []
        )
        evidence_trace_ids = []
        for item in established:
            if not isinstance(item, dict):
                continue
            match = re.fullmatch(r"trace:(\d+)", str(item.get("source_ref") or ""))
            if match:
                evidence_trace_ids.append(int(match.group(1)))
        first_evidence_trace_id = min(evidence_trace_ids) if evidence_trace_ids else None
        first_effective = next((
            index for index, item in enumerate(actions)
            if first_evidence_trace_id is not None and int(item["trace_id"]) == first_evidence_trace_id
        ), None)
        tail = actions[first_effective + 1:] if first_effective is not None else []
        traces_after_evidence = [
            trace for trace in traces
            if first_evidence_trace_id is not None and int(trace.get("id", 0)) > first_evidence_trace_id
        ]
        transitions = []
        for previous, current in zip(actions, actions[1:]):
            if previous["ok"] or not current["ok"]:
                continue
            transitions.append({
                "failed_trace_id": previous["trace_id"],
                "failed_tool": previous["tool"],
                "next_success_trace_id": current["trace_id"],
                "next_success_tool": current["tool"],
                "tool_changed": previous["tool"] != current["tool"],
            })
        families = Counter(
            str(item["command_family"]) for item in actions if item.get("command_family")
        )
        unresolved = (
            result.get("task_working_state", {}).get("unresolved_failures", [])
            if isinstance(result.get("task_working_state"), dict) else []
        )
        unresolved = unresolved if isinstance(unresolved, list) else []
        error_text = "\n".join(
            str(item.get("error") or "") for item in unresolved if isinstance(item, dict)
        )
        rounds = result.get("rounds") if isinstance(result.get("rounds"), list) else []
        final_text = str(
            result.get("final_output") or result.get("summary") or result.get("user_message") or ""
        )
        continuation_markers = [
            marker for marker in (
                "let me check", "i'll check", "i will check", "let me try",
                "i'll try", "i will try", "next i", "让我检查", "接下来我", "我将尝试",
            ) if marker in final_text.casefold()
        ]
        confirmation = self._runtime_regression_confirmation(int(task.id))
        return {
            "task_id": int(task.id),
            "request_excerpt": str(task.request)[:500],
            "status": task.status.value,
            "outcome_class": (
                "successful_but_potentially_inefficient"
                if task.status.value == "completed" else "strategy_adaptation_sample"
            ),
            "runtime_regression_confirmed": confirmation["confirmed"],
            "runtime_regression_confirmation_basis": confirmation["basis"],
            "attempts": int(task.attempts),
            "metrics": {name: evidence.get(name) for name in self.METRICS},
            "execution": {
                "observed_action_results": len(actions),
                "successful_action_results": sum(item["ok"] for item in actions),
                "failed_action_results": sum(not item["ok"] for item in actions),
                "retry_count": max(0, int(task.attempts) - 1),
                "unresolved_failure_count": len(unresolved),
                "observed_exit_codes": dict(Counter(
                    str(item["exit_code"]) for item in actions if item.get("exit_code") is not None
                )),
                "timeout_error_signature_observed": "timeouterror" in error_text.casefold(),
                "repeated_command_families": [
                    {"family": family, "occurrences": count}
                    for family, count in sorted(families.items()) if count > 1
                ],
                "final_cycle": {
                    "planned_actions": evidence.get("planned_actions"),
                    "executed_actions": evidence.get("executed_actions"),
                    "controller_declared_done": bool(rounds and rounds[-1].get("done")),
                    "host_observed_completion": bool(
                        task.status.value == "completed" and evidence.get("success") is True
                    ),
                    "continuation_like_text_signal": bool(continuation_markers),
                    "continuation_like_markers": continuation_markers,
                },
                "first_effective_evidence_trace_id": first_evidence_trace_id,
                "after_first_effective_evidence": {
                    "additional_action_results": len(tail) if first_effective is not None else None,
                    "additional_model_calls": sum(
                        trace.get("kind") == "plan_created" for trace in traces_after_evidence
                    ) if first_effective is not None else None,
                    "additional_cycles": len({
                        str(trace.get("cycle_id")) for trace in traces_after_evidence
                    }) if first_effective is not None else None,
                    "additional_model_tokens": None,
                    "reason_unavailable": "per-trace token attribution is not recorded"
                    if first_effective is not None else "no evidence-ledger trace reference was recorded",
                },
                "failure_then_success_transitions": transitions[:20],
                "action_timeline": actions[:100],
                "trace_truncated": len(traces) >= trace_limit,
            },
        }

    def _runtime_regression_confirmation(self, task_id: int) -> dict[str, Any]:
        runs = [
            item for item in self.store.list_evolution_runs(1000)
            if item.get("trigger") == f"runtime:{task_id}"
        ]
        confirmed = any(self._contains_confirmation(item.get("report")) for item in runs)
        return {
            "confirmed": confirmed,
            "basis": (
                "host_confirmed_runtime_repair_record"
                if confirmed else "no_host_confirmed_runtime_regression_record"
            ),
        }

    @classmethod
    def _contains_confirmation(cls, value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("human_confirmed_repair") is True:
                return True
            return any(cls._contains_confirmation(item) for item in value.values())
        if isinstance(value, list):
            return any(cls._contains_confirmation(item) for item in value)
        return False

    @staticmethod
    def _exit_code(error: Any) -> int | None:
        match = re.search(r"Command exited with\s+(-?\d+)", str(error or ""), re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _command_family(command: str) -> str:
        normalized = command.casefold()
        normalized = re.sub(r"2\s*>\s*/dev/null|2\s*>\s*&1", "", normalized)
        normalized = re.sub(r"\bhead\s+(?:-n\s*)?-?\d+", "head <limit>", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized[:500]


class StrategyOptimizationReasoner:
    """Ask the model what it would change about successful but costly work."""

    def __init__(self, controller: LLMController):
        self.controller = controller

    def reason(self, experience: dict[str, Any]) -> dict[str, Any]:
        config = self.controller.config
        if config.provider == "mock":
            return {
                "decision": "NO_ACTION",
                "reason": "mock model cannot author a strategy candidate",
                "model_usage": {"model_calls": 0},
            }
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config.api_key_env):
            raise ControllerError("model.api_key_env must be an environment-variable name")
        key = os.environ.get(config.api_key_env)
        if not key:
            raise ControllerError(f"Missing API key environment variable: {config.api_key_env}")
        request = {
            "model": config.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are optimizing your own AIOS working strategy. The supplied records are facts from "
                        "tasks that completed successfully; the Host has not labelled any behavior as bad and has "
                        "not recommended a solution. Ask: which behavior patterns limit your efficiency or "
                        "reliability, and what would you change in your workflow, context selection, retrieval, "
                        "tool-use strategy, cycle strategy, or procedural skill? Choose NO_ACTION when evidence is "
                        "insufficient. Otherwise propose exactly one testable candidate using exactly one executable "
                        "harness mutation listed in mutable_surface. Never modify authority, security, sandbox, "
                        "audit, storage, external evaluation, credentials, or production activation. Return JSON: "
                        "{decision:'PROPOSE'|'NO_ACTION',observed_pattern:{description,evidence_task_ids},"
                        "hypothesis,strategy_surface,candidate_strategy,mutation:{one_allowed_key:value},"
                        "expected_effects,risks,experiment_tasks,reason}."
                    ),
                },
                {"role": "user", "content": json.dumps(experience, ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "max_tokens": min(config.max_tokens, 2200),
            "response_format": {"type": "json_object"},
        }
        response = self.controller._send_request(request, key)
        try:
            proposal = json.loads(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ControllerError("Strategy optimization reasoner returned invalid JSON") from exc
        if not isinstance(proposal, dict):
            raise ControllerError("Strategy optimization reasoner returned an invalid object")
        proposal["target"] = {"surface": "harness", "component_id": "harness.strategy"}
        proposal["model_usage"] = response.get("usage", {"model_calls": 1})
        return proposal


class ModelEvolutionReasoner:
    """Ask the configured model to choose one evidence-backed environment mutation."""

    def __init__(self, controller: LLMController):
        self.controller = controller

    def reason(self, experience: dict[str, Any]) -> dict[str, Any]:
        config = self.controller.config
        if config.provider == "mock":
            return {
                "decision": "NO_ACTION", "reason": "mock model cannot author an evolution hypothesis",
                "model_usage": {"model_calls": 0},
            }
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config.api_key_env):
            raise ControllerError("model.api_key_env must be an environment-variable name")
        key = os.environ.get(config.api_key_env)
        if not key:
            raise ControllerError(f"Missing API key environment variable: {config.api_key_env}")
        request = {
            "model": config.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are the slow evolution agent for AIOS. The human supplies goals and immutable "
                        "constraints; you choose whether and how the mutable environment should change. "
                        "Use only observed evidence. Choose exactly one recurring friction and at most one "
                        "allowed harness mutation. Never modify authority, credentials, isolation, audit, "
                        "rollback, experiment boundaries, or human override. Return JSON only: "
                        "{decision:'PROPOSE'|'NO_ACTION', friction:{name,evidence}, hypothesis, "
                        "target:{surface:'harness',component_id:'harness.runtime'}, mutation:{...}, "
                        "expected_effects, risks, experiment_tasks}."
                    ),
                },
                {"role": "user", "content": json.dumps(experience, ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "max_tokens": min(config.max_tokens, 1800),
            "response_format": {"type": "json_object"},
        }
        response = self.controller._send_request(request, key)
        try:
            content = response["choices"][0]["message"]["content"]
            proposal = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ControllerError("Evolution reasoner returned invalid JSON") from exc
        if not isinstance(proposal, dict):
            raise ControllerError("Evolution reasoner returned an invalid object")
        proposal["model_usage"] = response.get("usage", {"model_calls": 1})
        return proposal


class SelfEvolutionLoop:
    """Slow loop: experience -> AI hypothesis -> mutation -> experiment -> selection."""

    def __init__(
        self, store: StateStore, analyzer: ExperienceAnalyzer,
        reasoner: EvolutionReasoner, manager: EvolutionManager,
        orchestrator: ExperimentOrchestrator,
    ):
        self.store = store
        self.analyzer = analyzer
        self.reasoner = reasoner
        self.manager = manager
        self.orchestrator = orchestrator

    def run(
        self, *, capsule_ids: list[str] | None = None, runs_per_variant: int = 3,
        task_limit: int = 100, trace_limit: int = 1000,
    ) -> dict[str, Any]:
        experience = self.analyzer.analyze(task_limit=task_limit, trace_limit=trace_limit)
        proposal = self.reasoner.reason(experience)
        if str(proposal.get("decision", "NO_ACTION")).upper() != "PROPOSE":
            report = {"changed": False, "selected": False, "experience": experience, "proposal": proposal}
            run_id = self.store.add_evolution_run(
                experience["signature"], experience, [], "observed", report,
            )
            return {"run_id": run_id, "status": "observed", **report}
        mutation = self._validate_proposal(proposal)
        rationale = str(proposal.get("hypothesis") or "AI-authored environment hypothesis").strip()
        candidate_id = self.manager.propose(mutation, rationale)
        policy_benchmark = self.manager.benchmark(candidate_id)
        if not policy_benchmark.get("passed"):
            return self._finish(
                experience, proposal, candidate_id, "rejected",
                {"policy_benchmark": policy_benchmark, "experiments": []},
            )
        observed_task_ids = (
            {
                int(item["task_id"]) for item in experience.get("tasks", [])
                if isinstance(item, dict) and item.get("task_id") is not None
            }
            if experience.get("schema") == "soft_friction_experience/v1"
            else None
        )
        selected_capsules = self._capsules(
            capsule_ids, source_task_ids=observed_task_ids,
        )
        if not selected_capsules:
            self.store.update_candidate(candidate_id, "needs_evidence", policy_benchmark)
            return self._finish(
                experience, proposal, candidate_id, "insufficient_evidence",
                {"policy_benchmark": policy_benchmark, "experiments": [], "reason": "no replayable capsules"},
            )
        reports = []
        for capsule_id in selected_capsules:
            reports.append(self.orchestrator.run(
                capsule_id,
                ExperimentVariant("baseline", mutation_type="harness", mutation={}),
                ExperimentVariant("candidate", mutation_type="harness", mutation=mutation),
                runs_per_variant=runs_per_variant,
            ))
        states = [str(report.get("promotion_state")) for report in reports]
        if PromotionState.REJECTED.value in states:
            status = "rejected"
        elif states and all(state == PromotionState.PROMOTABLE.value for state in states):
            status = "selected"
        elif PromotionState.INSUFFICIENT_EVIDENCE.value in states:
            status = "insufficient_evidence"
        else:
            status = "needs_review"
        combined = {
            "passed": status == "selected",
            "policy_benchmark": policy_benchmark, "experiments": reports,
            "selection_rule": "correctness/security constraints, then Pareto cost/latency",
            "production_activated": False,
        }
        self.store.update_candidate(candidate_id, status, combined)
        return self._finish(experience, proposal, candidate_id, status, combined)

    @staticmethod
    def _validate_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
        target = proposal.get("target")
        if not isinstance(target, dict) or target.get("surface") != "harness":
            raise EvolutionPolicyError("v0.7 MVP permits only the harness mutable environment surface")
        mutation = proposal.get("mutation")
        if not isinstance(mutation, dict) or len(mutation) != 1:
            raise EvolutionPolicyError("Evolution must propose exactly one mutation")
        EvolutionManager._validate_mutation(mutation)
        return mutation

    def _capsules(
        self, requested: list[str] | None,
        source_task_ids: set[int] | None = None,
    ) -> list[str]:
        if requested:
            values = requested
        else:
            values = [
                str(item["capsule_id"]) for item in self.store.list_task_capsules(limit=20)
                if item and item.get("status") == CapsuleStatus.REPLAYABLE.value
                and (
                    source_task_ids is None
                    or int(item.get("source_task_id", -1)) in source_task_ids
                )
            ][:3]
        result = []
        for capsule_id in values:
            capsule = self.orchestrator.capsules.show(capsule_id)
            if (
                source_task_ids is not None
                and int(capsule.get("source_task_id", -1)) not in source_task_ids
            ):
                continue
            check = self.orchestrator.capsules.verify_integrity(capsule_id)
            if check.get("valid") and check.get("status") == CapsuleStatus.REPLAYABLE.value:
                result.append(capsule_id)
        return list(dict.fromkeys(result))

    def _finish(
        self, experience: dict[str, Any], proposal: dict[str, Any], candidate_id: int,
        status: str, report: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "changed": False, "selected": status == "selected",
            "production_activated": False, "candidate_id": candidate_id,
            "experience": experience, "proposal": proposal, **report,
        }
        run_id = self.store.add_evolution_run(
            experience["signature"], experience, [candidate_id], status, payload,
        )
        return {"run_id": run_id, "status": status, **payload}
