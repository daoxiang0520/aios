from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aios.capabilities import CapabilityRegistry
from aios.config import Settings
from aios.experiments import (
    CapsuleManager, CounterfactualEvaluator, ExperimentOrchestrator,
    ExperimentVariant, PairwiseSemanticJudge,
)
from aios.skills import SkillManager, SkillPromotionError
from aios.storage import StateStore
from aios.types import Task


class V065CounterfactualTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = root / "config.json"
        config.write_text(json.dumps({
            "database": "data/a.db", "workspace": "workspace",
            "model": {"provider": "mock"},
            "sandbox": {"backend": "docker", "root": "sandbox", "image": "sha256:test-fixture"},
            "skills": {"enabled": True, "root": "skills", "require_human_promotion": True},
            "experiments": {"root": "experiments"},
        }), encoding="utf-8")
        self.settings = Settings.load(config)
        self.settings.ensure_directories()
        (self.settings.workspace / "input.txt").write_text("initial", encoding="utf-8")
        self.store = StateStore(self.settings.database)
        self.store.initialize()
        self.manager = SkillManager(self.settings.skills_root, self.settings.skills)
        self.capabilities = CapabilityRegistry.default(
            sandbox_available=True, network_enabled=False,
        )
        self.capsules = CapsuleManager(
            self.settings, self.store, self.manager, self.capabilities,
        )
        self.task_id = self.store.create_task(Task("fixture", "analyze input.txt"))
        self.capsule = self.capsules.capture(self.task_id)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def evidence(*, success: bool = True, calls: int = 3, tokens: int = 100, latency: float = 10) -> dict:
        return {
            "outcome": {
                "task_status": "completed" if success else "degraded",
                "verifier_pass": success, "true_completion": success,
            },
            "cost": {"model_calls": calls, "tokens": tokens, "wall_time_ms": latency},
            "skills": {}, "security": {"violations": []}, "artifacts": {},
            "final_output": "valid output" if success else "bad output",
        }

    def test_capsule_capture_restore_same_start_and_host_isolation(self) -> None:
        self.assertEqual(self.capsule["fidelity"], "full")
        first = self.capsules.fork(self.capsule["capsule_id"])
        second = self.capsules.fork(self.capsule["capsule_id"])
        self.assertEqual(first["initial_state_hash"], second["initial_state_hash"])
        (Path(first["workspace"]) / "foo.txt").write_text("baseline", encoding="utf-8")
        self.assertFalse((Path(second["workspace"]) / "foo.txt").exists())
        self.assertFalse((self.settings.workspace / "foo.txt").exists())
        self.capsules.delete_world(first)
        self.capsules.delete_world(second)

    def test_only_declared_skill_mutation_differs_and_three_runs_repeat(self) -> None:
        starts: list[str] = []

        def runner(capsule, world, variant, replicate):
            starts.append(world["initial_state_hash"])
            self.assertEqual((Path(world["workspace"]) / "input.txt").read_text(encoding="utf-8"), "initial")
            (Path(world["workspace"]) / f"{variant.name}.txt").write_text(str(replicate), encoding="utf-8")
            return self.evidence(calls=2 if variant.name == "candidate" else 3)

        report = ExperimentOrchestrator(self.store, self.capsules, runner).run(
            self.capsule["capsule_id"],
            ExperimentVariant("baseline", mutation={}),
            ExperimentVariant("candidate", mutation={"candidate_id": "candidate_fixture"}),
            runs_per_variant=3,
        )
        self.assertTrue(report["same_initial_state"])
        self.assertEqual(len(set(starts)), 1)
        self.assertEqual(len(report["variants"]["baseline"]), 3)
        self.assertEqual(len(report["variants"]["candidate"]), 3)
        experiment = self.store.get_experiment(report["experiment_id"])
        controlled = experiment["spec"]["controlled_state"]
        self.assertIn("model", controlled)
        self.assertEqual(experiment["spec"]["allowed_difference"], "skill mutation only")
        self.assertEqual(report["promotion_state"], "PROMOTABLE")
        self.assertEqual(
            self.store.latest_counterfactual_report("candidate_fixture")["experiment_id"],
            report["experiment_id"],
        )
        self.assertFalse((self.settings.workspace / "baseline.txt").exists())

    def test_failed_report_persistence_resumes_without_reexecuting_runs(self) -> None:
        calls: list[tuple[str, int]] = []

        class CountingJudge:
            def __init__(self):
                self.calls = 0

            def evaluate(self, task, baseline, candidate):
                self.calls += 1
                return {"verdict": "equivalent", "confidence": 1.0, "tier": "test"}

        def runner(capsule, world, variant, replicate):
            calls.append((variant.name, replicate))
            return self.evidence(calls=2 if variant.name == "candidate" else 3)

        judge = CountingJudge()
        orchestrator = ExperimentOrchestrator(
            self.store, self.capsules, runner, semantic_judge=judge,
        )
        baseline = ExperimentVariant("baseline", mutation={})
        candidate = ExperimentVariant(
            "candidate", mutation={"candidate_id": "resume_fixture"},
        )
        persist = self.store.add_counterfactual_report
        failed_once = False

        def fail_once(experiment_id, report):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise RuntimeError("simulated report persistence failure")
            return persist(experiment_id, report)

        self.store.add_counterfactual_report = fail_once  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "simulated report persistence failure"):
            orchestrator.run(
                self.capsule["capsule_id"], baseline, candidate, runs_per_variant=2,
            )
        self.assertEqual(len(calls), 4)
        self.assertEqual(judge.calls, 1)

        report = orchestrator.run(
            self.capsule["capsule_id"], baseline, candidate, runs_per_variant=2,
        )
        self.assertEqual(len(calls), 4)
        self.assertEqual(judge.calls, 1)
        self.assertTrue(report["resumed_from_persisted_runs"])
        self.assertEqual(report["reused_run_count"], 4)
        self.assertTrue(report["reused_semantic_judgement"])
        self.assertEqual(len(report["variants"]["baseline"]), 2)
        self.assertEqual(len(report["variants"]["candidate"]), 2)
        experiment = self.store.get_experiment(report["experiment_id"])
        self.assertEqual(experiment["status"], "completed")

    def test_stochastic_aggregation_uses_median_p95_and_success_rate(self) -> None:
        runs = [
            self.evidence(success=True, latency=10),
            self.evidence(success=False, latency=20),
            self.evidence(success=True, latency=100),
        ]
        aggregate = CounterfactualEvaluator.aggregate(runs)
        self.assertAlmostEqual(aggregate["success_rate"], 2 / 3)
        self.assertEqual(aggregate["median_latency_ms"], 20)
        self.assertEqual(aggregate["p95_latency_ms"], 100)

    def test_lower_correctness_rejects_even_when_candidate_is_faster(self) -> None:
        evaluator = CounterfactualEvaluator()
        baseline = [self.evidence(success=True, latency=100) for _ in range(3)]
        candidate = [self.evidence(success=True, latency=1), self.evidence(success=False, latency=1)]
        report = evaluator.evaluate(baseline, candidate, fidelity="full")
        self.assertEqual(report["promotion_state"], "REJECTED")
        self.assertTrue(report["negative_transfer"])

    def test_quality_gain_at_higher_cost_needs_review(self) -> None:
        evaluator = CounterfactualEvaluator()
        baseline = [self.evidence(success=False, calls=1, tokens=10, latency=1)]
        candidate = [self.evidence(success=True, calls=5, tokens=1000, latency=100)]
        report = evaluator.evaluate(baseline, candidate, fidelity="full")
        self.assertEqual(report["promotion_state"], "NEEDS_REVIEW")

    def test_semantic_judge_detects_position_bias(self) -> None:
        judge = PairwiseSemanticJudge(lambda task, a, b: {"winner": "A", "confidence": 0.9})
        result = judge.evaluate("task", "baseline", "candidate")
        self.assertEqual(result["verdict"], "insufficient_evidence")
        self.assertTrue(result["position_bias_detected"])

    def test_capsule_corruption_invalidates_replay(self) -> None:
        digest = self.capsule["workspace"]["manifest_hash"]
        manifest = self.capsules.snapshots.load(digest)
        object_digest = next(iter(manifest["files"].values()))["sha256"]
        self.capsules.snapshots.object_path(object_digest).write_bytes(b"corrupt")
        result = self.capsules.verify_integrity(self.capsule["capsule_id"])
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "invalidated")
        with self.assertRaises(RuntimeError):
            self.capsules.fork(self.capsule["capsule_id"])

    def test_archived_capsule_cannot_be_forked(self) -> None:
        capsule_id = self.capsule["capsule_id"]
        self.capsules.archive(capsule_id)
        verified = self.capsules.verify_integrity(capsule_id)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["status"], "archived")
        with self.assertRaises(RuntimeError):
            self.capsules.fork(capsule_id)

    def test_partial_fidelity_downgrades_an_improvement_to_review(self) -> None:
        baseline = [self.evidence(calls=3)]
        candidate = [self.evidence(calls=2)]
        report = CounterfactualEvaluator().evaluate(baseline, candidate, fidelity="partial")
        self.assertEqual(report["promotion_state"], "NEEDS_REVIEW")

    def test_agent_promotion_requires_promotable_counterfactual_bundle(self) -> None:
        source = (
            "import argparse,json\n"
            "p=argparse.ArgumentParser(); p.add_argument('--input-json',default='{}'); a=p.parse_args()\n"
            "print(json.dumps(json.loads(a.input_json)))\n"
        )
        proposal = self.manager.propose({
            "name": "counterfactual_fixture", "version": "1.0.0",
            "description": "Counterfactual promotion fixture.", "origin": "agent",
            "required_capabilities": ["process.sandbox_exec"],
            "input_schema": {"type": "object"},
            "tests": [{"input": {}, "expect_exit": 0}],
        }, source)
        candidate_id = proposal["candidate_id"]

        class Broker:
            def run_candidate(self, package, command, timeout_seconds):
                return {"exit_code": 0, "stdout": "{}", "stderr": ""}

        self.manager.benchmark(candidate_id, Broker())
        with self.assertRaises(SkillPromotionError):
            self.manager.promote(candidate_id, approved=True)
        report = {
            "candidate_id": candidate_id, "promotion_state": "PROMOTABLE",
            "same_initial_state": True, "negative_transfer": False,
        }
        (self.manager.reports / f"{candidate_id}.counterfactual.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        self.assertEqual(self.manager.promote(candidate_id, approved=True)["status"], "active")


if __name__ == "__main__":
    unittest.main()
