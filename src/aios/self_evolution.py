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
        selected_capsules = self._capsules(capsule_ids)
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

    def _capsules(self, requested: list[str] | None) -> list[str]:
        if requested:
            values = requested
        else:
            values = [
                str(item["capsule_id"]) for item in self.store.list_task_capsules(limit=20)
                if item and item.get("status") == CapsuleStatus.REPLAYABLE.value
            ][:3]
        result = []
        for capsule_id in values:
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
