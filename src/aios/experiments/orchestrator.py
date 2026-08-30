from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..config import Settings
from ..evolution import EvolutionManager
from ..runtime import AIOSRuntime
from ..skills import SkillManager
from ..storage import StateStore
from ..types import Event, Task, TaskStatus
from .capsule import CapsuleManager
from .counterfactual import CounterfactualEvaluator, PairwiseSemanticJudge
from .models import ExperimentVariant, ReplayMode, RunEvidence


RunExecutor = Callable[[dict[str, Any], dict[str, Any], ExperimentVariant, int], dict[str, Any]]


class ExperimentOrchestrator:
    """Restore, fork, mutate one declared variable, re-execute, and compare."""

    def __init__(
        self, store: StateStore, capsules: CapsuleManager, runner: RunExecutor,
        evaluator: CounterfactualEvaluator | None = None,
        semantic_judge: PairwiseSemanticJudge | None = None,
    ):
        self.store = store
        self.capsules = capsules
        self.runner = runner
        self.evaluator = evaluator or CounterfactualEvaluator()
        self.semantic_judge = semantic_judge or PairwiseSemanticJudge()

    def run(
        self, capsule_id: str, baseline: ExperimentVariant, candidate: ExperimentVariant,
        *, runs_per_variant: int = 3, keep_worlds: bool = False,
    ) -> dict[str, Any]:
        supported = {"skill", "harness"}
        unsupported = next(
            (item.mutation_type for item in (baseline, candidate) if item.mutation_type not in supported),
            None,
        )
        if unsupported is not None:
            raise ValueError(f"unsupported_mutation_kind:{unsupported}")
        if baseline.mutation_type != candidate.mutation_type:
            raise ValueError("Experiment variants must mutate the same component kind")
        runs_per_variant = max(1, min(int(runs_per_variant), 10))
        capsule = self.capsules.show(capsule_id)
        integrity = self.capsules.verify_integrity(capsule_id)
        if not integrity["valid"]:
            raise RuntimeError("Capsule integrity verification failed")
        experiment_id = f"exp_{uuid.uuid4().hex}"
        spec = {
            "experiment_id": experiment_id, "capsule_id": capsule_id,
            "replay_mode": ReplayMode.COUNTERFACTUAL.value,
            "runs_per_variant": runs_per_variant,
            "variants": [baseline.as_dict(), candidate.as_dict()],
            "controlled_state": {
                "task": capsule["task"], "workspace_hash": capsule["workspace"]["manifest_hash"],
                "capabilities": capsule["capabilities"], "harness": capsule["harness"],
                "model": capsule["model"], "environment": capsule["environment"],
                "components": capsule.get("components"),
            },
            "allowed_difference": f"{baseline.mutation_type} mutation only",
            "mutation_schema": "component_mutation/v1",
        }
        self.store.create_experiment(spec)
        all_runs: dict[str, list[dict[str, Any]]] = {baseline.name: [], candidate.name: []}
        initial_hashes: set[str] = set()
        try:
            for variant in (baseline, candidate):
                self.store.add_experiment_variant(experiment_id, variant.as_dict())
                for replicate in range(1, runs_per_variant + 1):
                    world = self.capsules.fork(
                        capsule_id, f"world_{experiment_id[4:16]}_{variant.name}_{replicate}"
                    )
                    initial_hashes.add(world["initial_state_hash"])
                    try:
                        evidence = self.runner(capsule, world, variant, replicate)
                        evidence = self._normalize_evidence(
                            experiment_id, capsule_id, variant, replicate, world, evidence
                        )
                        all_runs[variant.name].append(evidence)
                        self.store.add_experiment_run(experiment_id, evidence)
                    finally:
                        if not keep_worlds:
                            self.capsules.delete_world(world)
            if initial_hashes != {capsule["initial_state_hash"]}:
                raise RuntimeError("Experiment variants did not start from the same workspace state")
            baseline_output = self._representative_output(all_runs[baseline.name])
            candidate_output = self._representative_output(all_runs[candidate.name])
            semantic = self.semantic_judge.evaluate(
                capsule["task"]["request"], baseline_output, candidate_output
            )
            report = self.evaluator.evaluate(
                all_runs[baseline.name], all_runs[candidate.name],
                fidelity=capsule["fidelity"], semantic=semantic,
            )
            report.update({
                "experiment_id": experiment_id, "capsule_id": capsule_id,
                "candidate_id": candidate.mutation.get("candidate_id"),
                "same_initial_state": len(initial_hashes) == 1,
                "variants": {baseline.name: all_runs[baseline.name], candidate.name: all_runs[candidate.name]},
            })
            self.store.add_semantic_judgement(experiment_id, semantic)
            self.store.add_counterfactual_report(experiment_id, report)
            self.store.update_experiment(experiment_id, "completed", report)
            return report
        except Exception as exc:
            self.store.update_experiment(
                experiment_id, "failed", {"error": f"{type(exc).__name__}: {exc}"}
            )
            raise

    def show(self, experiment_id: str) -> dict[str, Any]:
        experiment = self.store.get_experiment(experiment_id)
        if experiment is None:
            raise KeyError(f"Unknown experiment: {experiment_id}")
        return experiment

    @staticmethod
    def _normalize_evidence(
        experiment_id: str, capsule_id: str, variant: ExperimentVariant, replicate: int,
        world: dict[str, Any], value: dict[str, Any],
    ) -> dict[str, Any]:
        evidence = RunEvidence(
            run_id=value.get("run_id") or f"run_{uuid.uuid4().hex}", capsule_id=capsule_id,
            variant=variant.name, replicate=replicate,
            initial_state_hash=world["initial_state_hash"],
            outcome=dict(value.get("outcome") or {}), cost=dict(value.get("cost") or {}),
            skills=dict(value.get("skills") or {}), security=dict(value.get("security") or {"violations": []}),
            artifacts=dict(value.get("artifacts") or {}), trace_id=value.get("trace_id"),
            final_output=str(value.get("final_output") or ""),
            adaptation=dict(value.get("adaptation") or {}),
        ).as_dict()
        evidence["experiment_id"] = experiment_id
        return evidence

    @staticmethod
    def _representative_output(runs: list[dict[str, Any]]) -> str:
        successful = [run for run in runs if run.get("outcome", {}).get("true_completion")]
        selected = successful or runs
        return str(selected[0].get("final_output", "")) if selected else ""

    def run_harness_sensitivity(
        self, capsule_id: str, *, runs_per_profile: int = 3,
        keep_worlds: bool = False,
    ) -> dict[str, Any]:
        """Measure H0/H1/H2 under one immutable world without selecting a winner."""
        profiles = {
            "H0_structured": "structured",
            "H1_reduced": "reduced",
            "H2_minimal_open": "minimal_open",
        }
        runs_per_profile = max(1, min(int(runs_per_profile), 10))
        capsule = self.capsules.show(capsule_id)
        integrity = self.capsules.verify_integrity(capsule_id)
        if not integrity["valid"]:
            raise RuntimeError("Capsule integrity verification failed")
        experiment_id = f"exp_{uuid.uuid4().hex}"
        variants = [
            ExperimentVariant(
                name, mutation_type="harness",
                mutation={"harness_profile": profile},
            )
            for name, profile in profiles.items()
        ]
        spec = {
            "experiment_id": experiment_id, "capsule_id": capsule_id,
            "kind": "harness_sensitivity/v1",
            "replay_mode": ReplayMode.COUNTERFACTUAL.value,
            "runs_per_variant": runs_per_profile,
            "variants": [item.as_dict() for item in variants],
            "controlled_state": {
                "task": capsule["task"],
                "workspace_hash": capsule["workspace"]["manifest_hash"],
                "capabilities": capsule["capabilities"],
                "model": capsule["model"], "environment": capsule["environment"],
                "components": capsule.get("components"),
            },
            "fixed_variables": [
                "model", "task", "initial_workspace", "authority", "tool_capabilities",
                "token_budget", "temperature", "sandbox", "verifier",
            ],
            "allowed_difference": "harness_profile only",
            "selection": "none_measurement_only",
        }
        self.store.create_experiment(spec)
        all_runs: dict[str, list[dict[str, Any]]] = {item.name: [] for item in variants}
        initial_hashes: set[str] = set()
        try:
            for variant in variants:
                self.store.add_experiment_variant(experiment_id, variant.as_dict())
                for replicate in range(1, runs_per_profile + 1):
                    world = self.capsules.fork(
                        capsule_id,
                        f"world_{experiment_id[4:16]}_{variant.name}_{replicate}",
                    )
                    initial_hashes.add(world["initial_state_hash"])
                    try:
                        evidence = self.runner(capsule, world, variant, replicate)
                        evidence = self._normalize_evidence(
                            experiment_id, capsule_id, variant, replicate, world, evidence,
                        )
                        all_runs[variant.name].append(evidence)
                        self.store.add_experiment_run(experiment_id, evidence)
                    finally:
                        if not keep_worlds:
                            self.capsules.delete_world(world)
            if initial_hashes != {capsule["initial_state_hash"]}:
                raise RuntimeError("Harness profiles did not start from the same world state")
            aggregates = {
                name: self._sensitivity_aggregate(values)
                for name, values in all_runs.items()
            }
            baseline = aggregates["H0_structured"]
            deltas = {
                name: self._numeric_delta(baseline, value)
                for name, value in aggregates.items() if name != "H0_structured"
            }
            semantic_quality = {
                name: self.semantic_judge.evaluate(
                    capsule["task"]["request"],
                    self._representative_output(all_runs["H0_structured"]),
                    self._representative_output(all_runs[name]),
                )
                for name in ("H1_reduced", "H2_minimal_open")
            }
            report = {
                "schema": "harness_sensitivity_report/v1",
                "experiment_id": experiment_id, "capsule_id": capsule_id,
                "same_initial_state": len(initial_hashes) == 1,
                "fixed_variables": spec["fixed_variables"],
                "only_variable": "harness_profile",
                "profiles": profiles, "aggregates": aggregates,
                "deltas_vs_H0": deltas, "semantic_quality": semantic_quality,
                "runs": all_runs,
                "promotion_state": "MEASUREMENT_ONLY",
                "selection": {
                    "winner": None, "promotion_state": None,
                    "single_scalar_reward": False,
                    "reason": "Phase 1 measures sensitivity; it does not choose or promote a Harness",
                },
            }
            self.store.add_counterfactual_report(experiment_id, report)
            self.store.update_experiment(experiment_id, "completed", report)
            return report
        except Exception as exc:
            self.store.update_experiment(
                experiment_id, "failed", {"error": f"{type(exc).__name__}: {exc}"},
            )
            raise

    @classmethod
    def _sensitivity_aggregate(cls, runs: list[dict[str, Any]]) -> dict[str, Any]:
        import statistics

        aggregate = CounterfactualEvaluator.aggregate(runs)
        adaptation = [run.get("adaptation", {}) for run in runs]
        aggregate.update({
            "median_repeated_resource_actions": statistics.median(
                float(item.get("repeated_resource_actions", 0) or 0) for item in adaptation
            ),
            "median_failed_tool_calls": statistics.median(
                float(item.get("failed_tool_calls", 0) or 0) for item in adaptation
            ),
            "failure_recovery_rate": (
                sum(bool(item.get("failure_recovered")) for item in adaptation)
                / max(1, sum(int(item.get("failed_tool_calls", 0) or 0) > 0 for item in adaptation))
            ),
            "median_post_failure_tool_changes": statistics.median(
                float(item.get("post_failure_tool_changes", 0) or 0) for item in adaptation
            ),
        })
        return aggregate

    @staticmethod
    def _numeric_delta(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, float]:
        return {
            key: round(float(candidate[key]) - float(value), 6)
            for key, value in baseline.items()
            if isinstance(value, (int, float)) and isinstance(candidate.get(key), (int, float))
        }

    @staticmethod
    def summarize_harness_sensitivity(cases: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate signed effects across tasks without converting them into a reward."""
        import statistics

        profiles = ("H0_structured", "H1_reduced", "H2_minimal_open")
        profile_medians: dict[str, dict[str, float]] = {}
        for profile in profiles:
            values = [case.get("aggregates", {}).get(profile, {}) for case in cases]
            keys = set.intersection(*(
                {key for key, value in item.items() if isinstance(value, (int, float))}
                for item in values
            )) if values else set()
            profile_medians[profile] = {
                key: float(statistics.median(float(item[key]) for item in values))
                for key in sorted(keys)
            }
        signed_effects: dict[str, dict[str, dict[str, float | int]]] = {}
        for profile in ("H1_reduced", "H2_minimal_open"):
            values = [case.get("deltas_vs_H0", {}).get(profile, {}) for case in cases]
            keys = set().union(*(item.keys() for item in values)) if values else set()
            signed_effects[profile] = {}
            for key in sorted(keys):
                observed = [float(item[key]) for item in values if isinstance(item.get(key), (int, float))]
                signed_effects[profile][key] = {
                    "cases": len(observed),
                    "negative": sum(value < 0 for value in observed),
                    "zero": sum(value == 0 for value in observed),
                    "positive": sum(value > 0 for value in observed),
                    "median_delta": float(statistics.median(observed)) if observed else 0.0,
                }
        return {
            "cases": len(cases), "profile_medians": profile_medians,
            "signed_effects_vs_H0": signed_effects,
            "winner": None, "single_scalar_reward": False,
        }


class RuntimeVariantRunner:
    """Execute the normal Agent loop in an isolated restored world."""

    def __init__(self, settings: Settings, source_skills: SkillManager):
        self.settings = settings
        self.source_skills = source_skills

    def __call__(
        self, capsule: dict[str, Any], world: dict[str, Any],
        variant: ExperimentVariant, replicate: int,
    ) -> dict[str, Any]:
        world_root = Path(world["root"])
        skill_root = Path(world["skills_root"])
        invoked: list[str] = []
        candidate_id = variant.mutation.get("candidate_id")
        harness_mutation: dict[str, Any] = {}
        if variant.mutation_type == "skill" and candidate_id:
            package, manifest, _ = self.source_skills._candidate(str(candidate_id))
            destination = skill_root / "active" / manifest.name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(package, destination)
            invoked.append(f"{manifest.name}@{manifest.version}")
        elif variant.mutation_type == "skill" and variant.mutation:
            raise ValueError("Skill variant may only declare candidate_id")
        elif variant.mutation_type == "harness":
            captured_harness = capsule.get("harness", {}).get("config", {})
            if not isinstance(captured_harness, dict):
                raise ValueError("Capsule harness config must be an object")
            harness_mutation = {**captured_harness, **variant.mutation}
            if harness_mutation:
                EvolutionManager._validate_mutation(harness_mutation)

        isolated = replace(
            self.settings,
            root=world_root,
            database=world_root / "state" / "aios.db",
            workspace=Path(world["workspace"]),
            skills=replace(self.settings.skills, root=str(skill_root)),
            sandbox=replace(self.settings.sandbox, root=str(world_root / "sandbox")),
            evolution=replace(self.settings.evolution, extensions_path=str(world_root / "extensions")),
        )
        runtime = AIOSRuntime(isolated)
        if harness_mutation:
            isolated_candidate = runtime.store.add_candidate(
                harness_mutation, "Counterfactual experiment variant",
            )
            runtime.store.update_candidate(
                isolated_candidate, "benchmarked", {"passed": True, "kind": "isolated_experiment"},
            )
            runtime.store.promote_candidate(isolated_candidate)
        task_data = capsule["task"]
        task = Task(
            title=task_data["title"], request=task_data["request"],
            priority=int(task_data.get("priority", 50)),
            max_attempts=int(task_data.get("max_attempts", 3)),
        )
        task_id = runtime.store.create_task(task)
        runtime.store.add_event(Event("TASK_REQUEST", {"task_id": task_id, "message": task.request}, task.priority))
        started = time.perf_counter()
        cycles = 0
        terminal_statuses = {
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.DEAD_LETTER,
            TaskStatus.DEGRADED, TaskStatus.BLOCKED_CAPABILITY,
            TaskStatus.NEEDS_AUTHORITY, TaskStatus.TERMINAL_FAILURE,
        }
        run_call_limit = task.max_attempts * (
            int(self.settings.budget.max_cycles_per_task) + 2
        )
        while runtime.store.count_pending_events() and cycles < run_call_limit:
            runtime.run_once()
            cycles += 1
            observed = runtime.store.get_task(task_id)
            if observed is not None and observed.status in terminal_statuses:
                break
        observed = runtime.store.get_task(task_id)
        if (
            runtime.store.count_pending_events()
            and observed is not None
            and observed.status not in terminal_statuses
        ):
            raise RuntimeError(
                f"Experiment runner safety limit reached with non-terminal task: "
                f"status={observed.status.value} calls={cycles} limit={run_call_limit}"
            )
        wall_time_ms = (time.perf_counter() - started) * 1000
        finished = runtime.store.get_task(task_id)
        if finished is None:
            raise RuntimeError("Experiment task disappeared")
        result = finished.result or {}
        measured = result.get("evidence") or {}
        verification = measured.get("verification") or {}
        violations = []
        for action_result in result.get("action_results", []):
            error = str(action_result.get("error") or "")
            if "PermissionDenied" in error or "SandboxPolicy" in error:
                violations.append(error)
        action_results = [
            item for item in result.get("action_results", []) if isinstance(item, dict)
        ]
        failed_tool_calls = int(
            measured.get("failed_tool_calls", sum(not bool(item.get("ok")) for item in action_results))
            or 0
        )
        post_failure_tool_changes = int(
            measured.get("post_failure_tool_changes", 0) or 0
        )
        working_state = (
            result.get("task_working_state")
            if isinstance(result.get("task_working_state"), dict) else {}
        )
        unresolved_failures = working_state.get("unresolved_failures", [])
        workspace_hash = self._tree_hash(Path(world["workspace"]))
        return {
            "outcome": {
                "task_status": finished.status.value,
                "verifier_pass": bool(verification.get("passed")),
                "true_completion": bool(measured.get("success")),
            },
            "cost": {
                "model_calls": int(measured.get("model_api_calls", 0) or 0),
                "tool_calls": int(
                    measured.get("task_tool_calls", measured.get("executed_actions", 0)) or 0
                ),
                "cycles": int(measured.get("task_cycles", cycles) or cycles),
                "input_tokens": int(measured.get("input_tokens", 0) or 0),
                "output_tokens": int(measured.get("output_tokens", 0) or 0),
                "tokens": int(measured.get("model_tokens", 0) or 0),
                "wall_time_ms": wall_time_ms,
            },
            "skills": {"candidate_id": candidate_id, "variant_skills": invoked},
            "component_mutation": {
                "kind": variant.mutation_type,
                "payload": harness_mutation if variant.mutation_type == "harness" else variant.mutation,
            },
            "security": {"violations": violations},
            "artifacts": {"workspace_hash": workspace_hash, "committed_files": result.get("committed_files", [])},
            "trace_id": result.get("cycle_id"), "final_output": result.get("final_output", ""),
            "adaptation": {
                "failed_tool_calls": failed_tool_calls,
                "unresolved_failures": len(unresolved_failures) if isinstance(unresolved_failures, list) else 0,
                "failure_recovered": bool(failed_tool_calls) and not unresolved_failures,
                "post_failure_tool_changes": post_failure_tool_changes,
                "repeated_resource_actions": int(
                    measured.get("repeated_resource_reads", 0) or 0
                ) + int(measured.get("repeated_resource_executions", 0) or 0),
            },
        }

    @staticmethod
    def _tree_hash(root: Path) -> str:
        values = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_file() and not path.is_symlink():
                values.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode("utf-8")).hexdigest()
