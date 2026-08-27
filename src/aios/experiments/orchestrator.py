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
from ..runtime import AIOSRuntime
from ..skills import SkillManager
from ..storage import StateStore
from ..types import Event, Task
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
        if baseline.mutation_type != "skill" or candidate.mutation_type != "skill":
            raise ValueError("v0.6.5 supports only mutation_type=skill")
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
            },
            "allowed_difference": "skill mutation only",
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
        ).as_dict()
        evidence["experiment_id"] = experiment_id
        return evidence

    @staticmethod
    def _representative_output(runs: list[dict[str, Any]]) -> str:
        successful = [run for run in runs if run.get("outcome", {}).get("true_completion")]
        selected = successful or runs
        return str(selected[0].get("final_output", "")) if selected else ""


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
        if candidate_id:
            package, manifest, _ = self.source_skills._candidate(str(candidate_id))
            destination = skill_root / "active" / manifest.name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(package, destination)
            invoked.append(f"{manifest.name}@{manifest.version}")
        elif variant.mutation:
            raise ValueError("Skill variant may only declare candidate_id")

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
        while runtime.store.count_pending_events() and cycles < task.max_attempts + 1:
            runtime.run_once()
            cycles += 1
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
        workspace_hash = self._tree_hash(Path(world["workspace"]))
        return {
            "outcome": {
                "task_status": finished.status.value,
                "verifier_pass": bool(verification.get("passed")),
                "true_completion": bool(measured.get("success")),
            },
            "cost": {
                "model_calls": int(measured.get("model_api_calls", 0) or 0),
                "input_tokens": int(measured.get("input_tokens", 0) or 0),
                "output_tokens": int(measured.get("output_tokens", 0) or 0),
                "tokens": int(measured.get("model_tokens", 0) or 0),
                "wall_time_ms": wall_time_ms,
            },
            "skills": {"candidate_id": candidate_id, "variant_skills": invoked},
            "security": {"violations": violations},
            "artifacts": {"workspace_hash": workspace_hash, "committed_files": result.get("committed_files", [])},
            "trace_id": result.get("cycle_id"), "final_output": result.get("final_output", ""),
        }

    @staticmethod
    def _tree_hash(root: Path) -> str:
        values = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_file() and not path.is_symlink():
                values.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode("utf-8")).hexdigest()
