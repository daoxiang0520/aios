from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aios.evolution import EvolutionManager, EvolutionPolicyError
from aios.experiments import ExperimentOrchestrator, ExperimentVariant, PromotionState
from aios.self_evolution import ExperienceAnalyzer, SelfEvolutionLoop
from aios.storage import StateStore
from aios.types import Task, TaskStatus


class FixedReasoner:
    def __init__(self, proposal: dict):
        self.proposal = proposal
        self.experience = None

    def reason(self, experience: dict) -> dict:
        self.experience = experience
        return self.proposal


class FakeCapsules:
    def __init__(self, identifiers: tuple[str, ...] = ("cap_a", "cap_b")):
        self.identifiers = identifiers

    def verify_integrity(self, capsule_id: str) -> dict:
        return {"valid": capsule_id in self.identifiers, "status": "replayable"}

    def show(self, capsule_id: str) -> dict:
        return {
            "capsule_id": capsule_id, "initial_state_hash": "same",
            "fidelity": "full", "task": {"request": "representative task"},
            "workspace": {"manifest_hash": "workspace"}, "capabilities": {},
            "harness": {}, "model": {}, "environment": {}, "components": {},
        }

    def fork(self, capsule_id: str, world_id: str) -> dict:
        return {"root": world_id, "initial_state_hash": "same"}

    def delete_world(self, world: dict) -> None:
        return None


class FakeOrchestrator:
    def __init__(self, states: list[str]):
        self.capsules = FakeCapsules()
        self.states = iter(states)
        self.calls = []

    def run(self, capsule_id, baseline, candidate, *, runs_per_variant):
        self.calls.append((capsule_id, baseline, candidate, runs_per_variant))
        return {
            "experiment_id": f"exp_{capsule_id}",
            "promotion_state": next(self.states),
            "delta": {"median_tokens": -100},
        }


class V070SelfEvolutionLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "aios.db")
        self.store.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_result(self, repeated: int, tokens: int) -> None:
        task_id = self.store.create_task(Task("task", "analyze documents"))
        self.store.update_task(task_id, TaskStatus.COMPLETED, result={
            "evidence": {
                "success": True, "model_api_calls": 4, "model_tokens": tokens,
                "repeated_resource_reads": repeated,
                "verification": {"checks": [{"name": "coverage", "passed": True}]},
            },
        })

    def test_experience_analyzer_reports_cross_task_friction_without_choosing_mutation(self) -> None:
        self.add_result(3, 10000)
        self.add_result(2, 8000)
        experience = ExperienceAnalyzer(self.store).analyze()
        self.assertEqual(experience["sample"]["tasks"], 2)
        self.assertEqual(experience["metrics"]["repeated_resource_reads"], 5)
        self.assertIn(
            {"name": "repeated_resource_requests", "occurrences": 5},
            experience["patterns"],
        )
        self.assertEqual(experience["mutable_environment"]["surface"], "harness")
        self.assertIn("authority", experience["immutable_kernel"])
        self.assertNotIn("recommended_mutation", experience)

    def test_ai_selects_candidate_after_counterfactuals_but_production_is_unchanged(self) -> None:
        self.add_result(2, 9000)
        reasoner = FixedReasoner({
            "decision": "PROPOSE",
            "friction": {"name": "context overhead", "evidence": "observed tokens"},
            "hypothesis": "A smaller memory projection will reduce tokens without reducing completion.",
            "target": {"surface": "harness", "component_id": "harness.runtime"},
            "mutation": {"memory_context_characters": 5000},
            "expected_effects": {"tokens": "lower"}, "risks": ["lost context"],
        })
        orchestrator = FakeOrchestrator([
            PromotionState.PROMOTABLE.value, PromotionState.PROMOTABLE.value,
        ])
        active_before = self.store.active_harness()["version"]
        result = SelfEvolutionLoop(
            self.store, ExperienceAnalyzer(self.store), reasoner,
            EvolutionManager(self.store), orchestrator,  # type: ignore[arg-type]
        ).run(capsule_ids=["cap_a", "cap_b"], runs_per_variant=3)
        self.assertEqual(result["status"], "selected")
        self.assertTrue(result["selected"])
        self.assertFalse(result["production_activated"])
        self.assertEqual(self.store.active_harness()["version"], active_before)
        candidate = self.store.get_candidate(result["candidate_id"])
        self.assertEqual(candidate["status"], "selected")
        self.assertEqual(len(orchestrator.calls), 2)
        self.assertEqual(orchestrator.calls[0][1].mutation_type, "harness")
        promoted = EvolutionManager(self.store).promote(result["candidate_id"], approved=True)
        self.assertEqual(promoted["version"], active_before + 1)

    def test_kernel_mutation_is_rejected_before_candidate_creation(self) -> None:
        reasoner = FixedReasoner({
            "decision": "PROPOSE", "hypothesis": "remove isolation",
            "target": {"surface": "kernel"},
            "mutation": {"sandbox": "disabled"},
        })
        loop = SelfEvolutionLoop(
            self.store, ExperienceAnalyzer(self.store), reasoner,
            EvolutionManager(self.store), FakeOrchestrator([]),  # type: ignore[arg-type]
        )
        with self.assertRaises(EvolutionPolicyError):
            loop.run(capsule_ids=["cap_a"])
        self.assertEqual(self.store.list_candidates(), [])

    def test_harness_is_a_real_counterfactual_mutation_kind(self) -> None:
        capsules = FakeCapsules(("cap_a",))

        def runner(capsule, world, variant, replicate):
            candidate = variant.name == "candidate"
            return {
                "outcome": {
                    "task_status": "completed", "verifier_pass": True,
                    "true_completion": True,
                },
                "cost": {
                    "model_calls": 2 if candidate else 3,
                    "tokens": 800 if candidate else 1000,
                    "wall_time_ms": 80 if candidate else 100,
                },
                "security": {"violations": []}, "skills": {}, "artifacts": {},
                "final_output": "same correct answer",
            }

        report = ExperimentOrchestrator(
            self.store, capsules, runner,  # type: ignore[arg-type]
        ).run(
            "cap_a",
            ExperimentVariant("baseline", mutation_type="harness", mutation={}),
            ExperimentVariant(
                "candidate", mutation_type="harness",
                mutation={"memory_context_characters": 5000},
            ),
            runs_per_variant=3,
        )
        self.assertEqual(report["promotion_state"], PromotionState.PROMOTABLE.value)
        self.assertEqual(report["delta"]["median_tokens"], -200.0)


if __name__ == "__main__":
    unittest.main()
