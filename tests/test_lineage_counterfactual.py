from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aios.config import Settings
from aios.cli import _LineageMeasurementEvaluator
from aios.evolution import EvolutionPolicyError
from aios.lineage import AutonomousLineageController, LineageManager
from aios.storage import StateStore


class FixedReasoner:
    def __init__(self, decision):
        self.decision = decision

    def reason(self, facts):
        self.facts = facts
        return self.decision


class FixedEvaluator:
    def __init__(self):
        self.calls = []

    def __call__(self, parent, candidate, decision):
        self.calls.append((parent, candidate, decision))
        return {
            "schema": "lineage_counterfactual_evaluation/v1",
            "parent_lineage_id": parent["lineage_id"],
            "candidate_lineage_id": candidate["lineage_id"],
            "cases": [{
                "experiment_id": "exp_fixed", "capsule_id": decision["capsule_ids"][0],
                "baseline": {"median_tokens": 100.0},
                "candidate": {"median_tokens": 80.0},
                "delta": {"median_tokens": -20.0},
                "selection": "none_measurement_only", "promotion_state": None,
            }],
            "selection": "none_measurement_only",
            "production_activated": False, "host_fitness_judgment": False,
        }


class LineageCounterfactualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(root=root, database=root / "test.db", workspace=root / "workspace")
        self.settings.ensure_directories()
        self.store = StateStore(self.settings.database)
        self.store.initialize()
        self.manager = LineageManager(self.store)
        self.manager.ensure_root()
        self.child = self.manager.fork(
            "lin_root", {"prompt_append": "avoid repeat reads"}, actor="agent", decision={},
        )
        self.full_id = self._capsule("full", fidelity="full", phase="pre_task")
        self._capsule("partial", fidelity="partial", phase="post_hoc_current")

    def _capsule(self, name, *, fidelity, phase):
        capsule_id = f"cap_{name}"
        self.store.add_task_capsule({
            "capsule_id": capsule_id, "source_task_id": 1, "status": "replayable",
            "fidelity": fidelity, "capture_phase": phase,
            "task": {"title": name, "request": f"request {name}"},
        })
        return capsule_id

    def test_facts_offer_only_full_pre_task_capsules(self):
        evaluator = FixedEvaluator()
        reasoner = FixedReasoner({"action": "CONTINUE", "reason": "inspect"})
        controller = AutonomousLineageController(
            self.store, reasoner, self.manager, settings=self.settings,
            lineage_evaluator=evaluator,
        )
        controller.decide(self.child["lineage_id"])
        self.assertEqual(
            [item["capsule_id"] for item in reasoner.facts["available_replay_capsules"]],
            [self.full_id],
        )
        action = reasoner.facts["action_availability"]["REQUEST_COUNTERFACTUAL_EVALUATION"]
        self.assertTrue(action["available"])
        self.assertFalse(action["host_fitness_judgment"])
        self.assertEqual(action["bounds"]["runs_per_variant"], [1, 3])

    def test_agent_requests_measurement_without_changing_lineage(self):
        evaluator = FixedEvaluator()
        decision = {
            "action": "REQUEST_COUNTERFACTUAL_EVALUATION",
            "reason": "current observations are not causal evidence",
            "evidence_task_ids": [], "capsule_ids": [self.full_id],
            "runs_per_variant": 2,
        }
        before = self.manager.current()["lineage_id"]
        result = AutonomousLineageController(
            self.store, FixedReasoner(decision), self.manager, settings=self.settings,
            lineage_evaluator=evaluator,
        ).decide(self.child["lineage_id"])
        self.assertEqual(result["effective_lineage_id"], self.child["lineage_id"])
        self.assertEqual(self.manager.current()["lineage_id"], self.child["lineage_id"])
        self.assertNotEqual(before, self.manager.current()["lineage_id"])
        self.assertEqual(result["lineage_evaluation"]["selection"], "none_measurement_only")
        self.assertFalse(result["host_fitness_judgment"])
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(evaluator.calls[0][0]["lineage_id"], "lin_root")
        self.assertEqual(evaluator.calls[0][1]["lineage_id"], self.child["lineage_id"])
        run = self.store.list_evolution_runs(1)[0]
        self.assertEqual(run["status"], "lineage_evaluated")
        event = self.manager.describe(self.child["lineage_id"])["events"][0]
        self.assertEqual(event["action"], "counterfactual_evaluated")
        self.assertNotIn("variants", event["data"]["cases"][0])

    def test_ineligible_capsule_and_invalid_bounds_are_rejected_before_execution(self):
        evaluator = FixedEvaluator()
        for capsule_ids, runs in [(["cap_partial"], 1), ([self.full_id], 4), ([], 1)]:
            with self.assertRaises(EvolutionPolicyError):
                AutonomousLineageController(
                    self.store,
                    FixedReasoner({
                        "action": "REQUEST_COUNTERFACTUAL_EVALUATION", "reason": "measure",
                        "capsule_ids": capsule_ids, "runs_per_variant": runs,
                    }),
                    self.manager, settings=self.settings, lineage_evaluator=evaluator,
                ).decide(self.child["lineage_id"])
        self.assertEqual(evaluator.calls, [])

    def test_root_and_component_mutation_cannot_request_harness_comparison(self):
        evaluator = FixedEvaluator()
        root_facts = AutonomousLineageController(
            self.store, FixedReasoner({"action": "CONTINUE"}), self.manager,
            lineage_evaluator=evaluator,
        )._facts("lin_root", 20)
        self.assertFalse(root_facts["action_availability"]["REQUEST_COUNTERFACTUAL_EVALUATION"]["available"])

    def test_evaluator_failure_is_a_record_not_a_lineage_change(self):
        def fail(parent, candidate, decision):
            raise RuntimeError("isolated experiment failed")

        result = AutonomousLineageController(
            self.store,
            FixedReasoner({
                "action": "REQUEST_COUNTERFACTUAL_EVALUATION", "reason": "measure",
                "capsule_ids": [self.full_id], "runs_per_variant": 1,
            }),
            self.manager, settings=self.settings, lineage_evaluator=fail,
        ).decide(self.child["lineage_id"])
        self.assertEqual(result["status"], "lineage_evaluation_failed")
        self.assertEqual(result["effective_lineage_id"], self.child["lineage_id"])
        self.assertFalse(result["lineage_changed"])
        self.assertIn("RuntimeError", result["error"])

    def test_measurement_preserves_target_adaptation_metric_without_host_selection(self):
        run = lambda tokens, repeats: {
            "outcome": {"task_status": "stopped", "verifier_pass": False, "true_completion": False},
            "cost": {"model_calls": 2, "tool_calls": 3, "cycles": 1,
                     "tokens": tokens, "wall_time_ms": 10},
            "security": {"violations": []},
            "adaptation": {"repeated_resource_actions": repeats, "failed_tool_calls": 0,
                           "post_failure_tool_changes": 0, "failure_recovered": False},
        }
        measured = _LineageMeasurementEvaluator.evaluate(
            [run(100, 5)], [run(80, 2)], fidelity="full",
        )
        self.assertEqual(measured["baseline"]["median_repeated_resource_actions"], 5)
        self.assertEqual(measured["candidate"]["median_repeated_resource_actions"], 2)
        self.assertEqual(measured["delta"]["median_repeated_resource_actions"], -3)
        self.assertEqual(measured["selection"], "none_measurement_only")
        self.assertEqual(measured["promotion_state"], "MEASUREMENT_ONLY")
        self.assertFalse(measured["host_fitness_judgment"])

    def test_measurement_report_is_persistable_without_a_promotion_decision(self):
        run = {
            "outcome": {"task_status": "stopped", "verifier_pass": False,
                        "true_completion": False},
            "cost": {"model_calls": 1, "tool_calls": 1, "cycles": 1,
                     "tokens": 10, "wall_time_ms": 1},
            "security": {"violations": []},
            "adaptation": {},
        }
        report = _LineageMeasurementEvaluator.evaluate(
            [run], [run], fidelity="full",
        )
        report_id = self.store.add_counterfactual_report("exp_measurement", report)
        self.assertGreater(report_id, 0)
        self.assertEqual(report["selection"], "none_measurement_only")
        self.assertEqual(report["promotion_state"], "MEASUREMENT_ONLY")


if __name__ == "__main__":
    unittest.main()
