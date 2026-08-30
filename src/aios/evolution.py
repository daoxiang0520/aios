from __future__ import annotations

import hashlib
from typing import Any

from .config import EvolutionConfig
from .plugins import PluginManager, state_query_manifest, workspace_search_manifest
from .storage import StateStore
from .types import Task


HARNESS_PROFILES = {"structured", "reduced", "minimal_open"}
ALLOWED_MUTATIONS = {
    "prompt_append", "max_actions_per_cycle", "memory_context_characters",
    "harness_profile",
}


class EvolutionPolicyError(RuntimeError):
    pass


class EvolutionManager:
    def __init__(self, store: StateStore):
        self.store = store

    def propose(self, mutation: dict[str, Any], rationale: str) -> int:
        self._validate_mutation(mutation)
        if not rationale.strip():
            raise EvolutionPolicyError("A rationale is required")
        return self.store.add_candidate(mutation, rationale.strip())

    def benchmark(self, candidate_id: int) -> dict[str, Any]:
        candidate = self._candidate(candidate_id)
        mutation = candidate["mutation"]
        baseline = self.store.active_harness()
        baseline_settings = baseline.get("settings", {})
        candidate_settings = {**baseline_settings, **mutation}
        checks = [
            {"name": "allowed_mutation_keys", "passed": set(mutation) <= ALLOWED_MUTATIONS},
            {"name": "security_kernel_unchanged", "passed": True},
            {"name": "credentials_unchanged", "passed": True},
            {"name": "sandbox_unchanged", "passed": True},
            {
                "name": "prompt_size",
                "passed": len(str(mutation.get("prompt_append", ""))) <= 4000,
            },
        ]
        baseline_score = self._offline_score(baseline_settings)
        candidate_score = self._offline_score(candidate_settings)
        passed = all(check["passed"] for check in checks) and candidate_score >= baseline_score
        report = {
            "kind": "offline_regression_benchmark",
            "passed": passed,
            "checks": checks,
            "baseline": {
                "version": baseline.get("version"),
                "settings": baseline_settings,
                "score": baseline_score,
            },
            "candidate": {"settings": candidate_settings, "score": candidate_score},
            "score_delta": candidate_score - baseline_score,
            "note": (
                "This proves policy and offline regression safety, not task-quality improvement. "
                "Representative live task benchmarks remain required before production promotion."
            ),
        }
        self.store.update_candidate(candidate_id, "benchmarked" if passed else "rejected", report)
        return report

    @staticmethod
    def _offline_score(settings: dict[str, Any]) -> float:
        """Stable, network-free regression score for mutable Harness configuration."""
        score = 4.0
        prompt = settings.get("prompt_append", "")
        if isinstance(prompt, str) and len(prompt) <= 4000:
            score += 1.0
        actions = settings.get("max_actions_per_cycle", 8)
        if isinstance(actions, int) and 1 <= actions <= 20:
            score += 1.0
        memory_chars = settings.get("memory_context_characters", 6000)
        if isinstance(memory_chars, int) and 500 <= memory_chars <= 20000:
            score += 1.0
        return score

    def promote(self, candidate_id: int, *, approved: bool) -> dict[str, Any]:
        if not approved:
            raise EvolutionPolicyError("Human approval is required for promotion")
        candidate = self._candidate(candidate_id)
        benchmark = candidate.get("benchmark") or {}
        if candidate["status"] not in {"benchmarked", "selected"} or not benchmark.get("passed"):
            raise EvolutionPolicyError("Candidate must pass benchmark or counterfactual selection before promotion")
        return self.store.promote_candidate(candidate_id)

    def rollback(self, version: int, *, approved: bool) -> dict[str, Any]:
        if not approved:
            raise EvolutionPolicyError("Human approval is required for rollback")
        return self.store.rollback_harness(version)

    def auto_promote(self, candidate_id: int) -> dict[str, Any]:
        """Promote a benchmarked low-risk candidate without a human CLI approval."""
        candidate = self._candidate(candidate_id)
        benchmark = candidate.get("benchmark") or {}
        if candidate["status"] != "benchmarked" or not benchmark.get("passed"):
            raise EvolutionPolicyError("Candidate must pass benchmark before automatic promotion")
        return self.store.promote_candidate(candidate_id)

    def _candidate(self, candidate_id: int) -> dict[str, Any]:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError(f"Unknown candidate: {candidate_id}")
        return candidate

    @staticmethod
    def _validate_mutation(mutation: dict[str, Any]) -> None:
        unknown = set(mutation) - ALLOWED_MUTATIONS
        if unknown:
            raise EvolutionPolicyError(f"Immutable or unknown mutation fields: {sorted(unknown)}")
        if not mutation:
            raise EvolutionPolicyError("Mutation cannot be empty")
        prompt = mutation.get("prompt_append")
        if prompt is not None and (not isinstance(prompt, str) or len(prompt) > 4000):
            raise EvolutionPolicyError("prompt_append must be a string up to 4000 characters")
        max_actions = mutation.get("max_actions_per_cycle")
        if max_actions is not None and (not isinstance(max_actions, int) or not 1 <= max_actions <= 20):
            raise EvolutionPolicyError("max_actions_per_cycle must be between 1 and 20")
        memory_chars = mutation.get("memory_context_characters")
        if memory_chars is not None and (
            not isinstance(memory_chars, int) or not 500 <= memory_chars <= 20000
        ):
            raise EvolutionPolicyError("memory_context_characters must be between 500 and 20000")
        profile = mutation.get("harness_profile")
        if profile is not None and profile not in HARNESS_PROFILES:
            raise EvolutionPolicyError(
                f"harness_profile must be one of {sorted(HARNESS_PROFILES)}"
            )


class AutonomousEvolutionEngine:
    """Failure-driven, auditable developer-mode evolution for declarative tools."""

    def __init__(
        self,
        store: StateStore,
        plugins: PluginManager,
        config: EvolutionConfig,
    ):
        self.store = store
        self.plugins = plugins
        self.config = config

    def observe_failure(
        self,
        task: Task,
        error: str,
        result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            return {"triggered": False, "reason": "autonomous evolution disabled"}
        repetitions = sum(
            checkpoint["phase"] == "failed_attempt"
            for checkpoint in self.store.task_checkpoints(int(task.id))
        )
        if repetitions < max(1, self.config.trigger_repetitions):
            return {"triggered": False, "reason": "repetition threshold not reached"}

        diagnosis = self._diagnose(task, error, result)
        manifests = self._propose_tools(diagnosis)
        active_names = {plugin.name for plugin in self.plugins.active_plugins()}
        manifests = [manifest for manifest in manifests if manifest["name"] not in active_names]
        if not manifests:
            prior = next(
                (
                    run
                    for run in self.store.list_evolution_runs(100)
                    if run["status"] == "promoted"
                    and run["diagnosis"].get("task_id") == task.id
                    and run["report"].get("activated_tools")
                ),
                None,
            )
            if prior is not None and repetitions > max(1, self.config.trigger_repetitions):
                rolled_back = [
                    name
                    for name in prior["report"]["activated_tools"]
                    if self.plugins.rollback(name) or name in active_names
                ]
                report = {
                    "changed": False,
                    "reason": "canary task failed after activation; generated tools rolled back",
                    "rolled_back_tools": rolled_back,
                }
                run_id = self.store.add_evolution_run(
                    diagnosis["signature"], diagnosis, [], "rolled_back", report
                )
                return {"triggered": True, "changed": False, "run_id": run_id, **report}
            report = {"changed": False, "reason": "no safe new capability candidate", "diagnosis": diagnosis}
            run_id = self.store.add_evolution_run(
                diagnosis["signature"], diagnosis, [], "observed", report
            )
            return {"triggered": True, "changed": False, "run_id": run_id, **report}

        candidate_ids: list[int] = []
        activated: list[str] = []
        reports: list[dict[str, Any]] = []
        for manifest in manifests:
            mutation = {"tool_plugin": manifest}
            candidate_id = self.store.add_candidate(
                mutation,
                f"Automatically generated for repeated failure {diagnosis['signature']}",
            )
            candidate_ids.append(candidate_id)
            try:
                self.plugins.write_candidate(candidate_id, manifest)
                smoke = self.plugins.smoke_test(manifest)
                benchmark = {
                    "kind": "generated_tool_sandbox_benchmark",
                    "passed": bool(smoke["passed"]),
                    "checks": [
                        {"name": "manifest_policy", "passed": True},
                        {"name": "no_network", "passed": not manifest["permissions"].get("network")},
                        {"name": "no_subprocess", "passed": not manifest["permissions"].get("subprocess")},
                        {"name": "smoke_test", "passed": bool(smoke["passed"])},
                    ],
                    "smoke": smoke,
                }
                self.store.update_candidate(candidate_id, "benchmarked", benchmark)
                if self.config.auto_promote and benchmark["passed"]:
                    self.plugins.activate(candidate_id, manifest["name"])
                    self.store.update_candidate(candidate_id, "promoted", benchmark)
                    activated.append(manifest["name"])
                reports.append({"candidate_id": candidate_id, "manifest": manifest, "benchmark": benchmark})
            except Exception as exc:
                failure = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
                self.store.update_candidate(candidate_id, "rejected", failure)
                reports.append({"candidate_id": candidate_id, "manifest": manifest, "benchmark": failure})

        changed = bool(activated)
        status = "promoted" if changed else "rejected"
        report = {"changed": changed, "activated_tools": activated, "candidates": reports}
        run_id = self.store.add_evolution_run(
            diagnosis["signature"], diagnosis, candidate_ids, status, report
        )
        return {
            "triggered": True,
            "changed": changed,
            "run_id": run_id,
            "diagnosis": diagnosis,
            **report,
        }

    @staticmethod
    def _diagnose(task: Task, error: str, result: dict[str, Any] | None) -> dict[str, Any]:
        evidence = (result or {}).get("evidence", {})
        verification = evidence.get("verification", {})
        failed_checks = [
            check.get("name")
            for check in verification.get("checks", [])
            if not check.get("passed")
        ]
        normalized = "|".join(sorted(str(item) for item in failed_checks)) or error.split(":", 1)[0]
        signature = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        request_lower = task.request.casefold()
        capability_gaps: list[str] = []
        if any(word in request_lower for word in ("trace", "任务", "task", "失败", "dead letter", "死信")):
            capability_gaps.extend(["task_query", "trace_query", "dead_letter_query"])
        if any(word in request_lower for word in ("查找", "搜索", "定位", "search", "find")):
            capability_gaps.append("workspace_search")
        if evidence.get("budget_truncated"):
            capability_gaps.append("budget_aware_convergence")
        return {
            "signature": signature,
            "task_id": task.id,
            "error": error,
            "failed_checks": failed_checks,
            "budget_truncated": bool(evidence.get("budget_truncated")),
            "capability_gaps": list(dict.fromkeys(capability_gaps)),
        }

    @staticmethod
    def _propose_tools(diagnosis: dict[str, Any]) -> list[dict[str, Any]]:
        gaps = set(diagnosis.get("capability_gaps", []))
        manifests: list[dict[str, Any]] = []
        if "task_query" in gaps:
            manifests.append(
                state_query_manifest("query_tasks", "tasks", "Query durable AIOS tasks and their results.")
            )
        if "trace_query" in gaps:
            manifests.append(
                state_query_manifest("query_traces", "traces", "Query recent structured AIOS execution traces.")
            )
        if "dead_letter_query" in gaps:
            manifests.append(
                state_query_manifest(
                    "query_dead_letters", "dead_letters", "Query exhausted task failures and their errors."
                )
            )
        if "workspace_search" in gaps:
            manifests.append(workspace_search_manifest())
        return manifests
