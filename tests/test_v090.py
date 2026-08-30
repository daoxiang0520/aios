from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aios.cli import _parser
from aios.config import ModelConfig
from aios.controller import LLMController
from aios.experiments.counterfactual import CounterfactualEvaluator
from aios.self_evolution import (
    SelfEvolutionLoop, SoftFrictionExperienceBuilder, StrategyOptimizationReasoner,
)
from aios.storage import StateStore
from aios.types import Task, TaskStatus


class V090SoftFrictionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temporary.name) / "aios.db")
        self.store.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _task(self, status: TaskStatus, success: bool, task_id_suffix: str) -> int:
        task_id = self.store.create_task(Task(f"task-{task_id_suffix}", "analyze repository and report"))
        self.store.add_checkpoint(task_id, "started", {"cycle_id": f"cycle-{task_id_suffix}"})
        first_trace = self.store.trace(f"cycle-{task_id_suffix}", "action_result", {
            "round": 1, "tool": "read", "ok": False, "error": "FileNotFoundError: missing",
        })
        evidence_trace = self.store.trace(f"cycle-{task_id_suffix}", "action_result", {
            "round": 2, "tool": "bash", "ok": True, "output": "found",
        })
        self.store.trace(f"cycle-{task_id_suffix}", "action_result", {
            "round": 3, "tool": "write", "ok": True, "output": "report.md",
        })
        self.store.update_task(task_id, status, result={
            "evidence": {
                "success": success, "model_tokens": 12000, "model_api_calls": 5,
                "task_cycles": 3, "executed_actions": 3, "failed_actions": 1,
                "repeated_resource_reads": 2, "repeated_resource_executions": 0,
                "observation_reuse_hits": 1, "context_reuse_ratio": 0.25,
                "established_evidence": [{
                    "kind": "artifact", "value": "found", "source_ref": f"trace:{evidence_trace}",
                }],
            },
        })
        return task_id

    def test_projects_only_successful_tasks_and_does_not_label_friction(self) -> None:
        completed = self._task(TaskStatus.COMPLETED, True, "ok")
        self._task(TaskStatus.DEGRADED, False, "bad")
        experience = SoftFrictionExperienceBuilder(self.store).analyze(task_limit=10)
        self.assertEqual(experience["schema"], "soft_friction_experience/v1")
        self.assertEqual([item["task_id"] for item in experience["tasks"]], [completed])
        self.assertFalse(experience["fact_policy"]["diagnosis_provided"])
        self.assertFalse(experience["fact_policy"]["bad_behavior_labels_provided"])
        self.assertNotIn("patterns", experience)
        self.assertNotIn("recommended_strategy", experience)

    def test_records_bounded_tail_cost_and_course_change_as_facts(self) -> None:
        self._task(TaskStatus.COMPLETED, True, "ok")
        task = SoftFrictionExperienceBuilder(self.store).analyze()["tasks"][0]
        execution = task["execution"]
        self.assertEqual(execution["failed_action_results"], 1)
        self.assertEqual(execution["after_first_effective_evidence"]["additional_action_results"], 1)
        self.assertIsNone(execution["after_first_effective_evidence"]["additional_model_tokens"])
        self.assertEqual(len(execution["failure_then_success_transitions"]), 1)
        self.assertTrue(execution["failure_then_success_transitions"][0]["tool_changed"])

    def test_empty_success_sample_remains_valid_observation(self) -> None:
        self._task(TaskStatus.DEAD_LETTER, False, "bad")
        experience = SoftFrictionExperienceBuilder(self.store).analyze()
        self.assertEqual(experience["tasks"], [])
        self.assertTrue(experience["signature"])

    def test_explicit_dead_letter_projects_adaptation_facts_without_diagnosis(self) -> None:
        task_id = self.store.create_task(Task("strategy failure", "inspect successful traces"))
        cycle = "cycle-adaptation"
        self.store.add_checkpoint(task_id, "started", {"cycle_id": cycle})
        commands = [
            "python /aios-state/aiosctl.py traces 2>&1 | head -100",
            "python /aios-state/aiosctl.py traces 2>/dev/null | head -200",
        ]
        for round_number, command in enumerate(commands, 1):
            self.store.trace(cycle, "plan_created", {
                "round": round_number, "done": False,
                "actions": [{"tool": "bash", "arguments": {"command": command}}],
            })
            self.store.trace(cycle, "action_result", {
                "round": round_number, "tool": "bash", "ok": False,
                "error": "Command exited with 120",
            })
        final_text = "I need another approach. Let me check if there is a different interface."
        self.store.update_task(task_id, TaskStatus.DEAD_LETTER, result={
            "summary": final_text, "final_output": final_text,
            "rounds": [{"round": 3, "summary": final_text, "done": True, "actions": []}],
            "task_working_state": {
                "unresolved_failures": [
                    {"tool": "bash", "error": "Command exited with 120"},
                    {"tool": "bash", "error": "Command exited with 120"},
                ],
            },
            "evidence": {
                "success": False, "model_tokens": 140000, "model_api_calls": 24,
                "task_cycles": 5, "planned_actions": 0, "executed_actions": 0,
                "failed_actions": 0,
            },
        })

        automatic = SoftFrictionExperienceBuilder(self.store).analyze()
        self.assertNotIn(task_id, [item["task_id"] for item in automatic["tasks"]])
        experience = SoftFrictionExperienceBuilder(
            self.store, included_task_ids=[task_id],
        ).analyze()
        task = next(item for item in experience["tasks"] if item["task_id"] == task_id)
        execution = task["execution"]
        self.assertEqual(task["outcome_class"], "strategy_adaptation_sample")
        self.assertFalse(task["runtime_regression_confirmed"])
        self.assertEqual(execution["observed_exit_codes"], {"120": 2})
        self.assertFalse(execution["timeout_error_signature_observed"])
        self.assertEqual(execution["repeated_command_families"][0]["occurrences"], 2)
        self.assertEqual(execution["unresolved_failure_count"], 2)
        self.assertTrue(execution["final_cycle"]["controller_declared_done"])
        self.assertFalse(execution["final_cycle"]["host_observed_completion"])
        self.assertTrue(execution["final_cycle"]["continuation_like_text_signal"])
        self.assertEqual(execution["final_cycle"]["executed_actions"], 0)
        self.assertNotIn("broken_pipe", task)
        self.assertNotIn("recommended_strategy", experience)

    def test_mock_reasoner_abstains_without_inventing_strategy(self) -> None:
        reasoner = StrategyOptimizationReasoner(LLMController(ModelConfig(provider="mock")))
        result = reasoner.reason({"schema": "soft_friction_experience/v1", "tasks": []})
        self.assertEqual(result["decision"], "NO_ACTION")

    def test_counterfactual_vector_includes_tools_cycles_and_robustness(self) -> None:
        baseline = [{
            "outcome": {"task_status": "completed", "verifier_pass": True, "true_completion": True},
            "cost": {"model_calls": 3, "tool_calls": 6, "cycles": 3, "tokens": 1000, "wall_time_ms": 100},
            "security": {"violations": []},
        } for _ in range(3)]
        candidate = [{
            "outcome": {"task_status": "completed", "verifier_pass": True, "true_completion": True},
            "cost": {"model_calls": 2, "tool_calls": 4, "cycles": 2, "tokens": 800, "wall_time_ms": 80},
            "security": {"violations": []},
        } for _ in range(3)]
        report = CounterfactualEvaluator().evaluate(baseline, candidate, fidelity="full")
        self.assertEqual(report["promotion_state"], "PROMOTABLE")
        self.assertEqual(report["candidate"]["median_tool_calls"], 4)
        self.assertEqual(report["candidate"]["median_cycles"], 2)
        self.assertEqual(report["candidate"]["completion_consistency"], 1.0)
        self.assertFalse(report["selection_rule"]["single_scalar_reward"])

    def test_pareto_cost_tradeoff_requires_review(self) -> None:
        def run(tokens: int, calls: int) -> dict:
            return {
                "outcome": {"task_status": "completed", "verifier_pass": True, "true_completion": True},
                "cost": {
                    "model_calls": calls, "tool_calls": 4, "cycles": 2,
                    "tokens": tokens, "wall_time_ms": 80,
                },
                "security": {"violations": []},
            }
        report = CounterfactualEvaluator().evaluate(
            [run(1000, 3)] * 3, [run(800, 4)] * 3, fidelity="full",
        )
        self.assertEqual(report["promotion_state"], "NEEDS_REVIEW")
        self.assertIn("Pareto trade-off", report["reason"])

    def test_cli_exposes_observe_and_run_without_runtime_repair_changes(self) -> None:
        parser = _parser()
        observed = parser.parse_args(["evolution", "optimize-observe", "--task-id", "85"])
        run = parser.parse_args([
            "evolution", "optimize-run", "--capsule", "cap_1", "--task-id", "85",
        ])
        self.assertEqual(observed.evolution_command, "optimize-observe")
        self.assertEqual(run.evolution_command, "optimize-run")
        self.assertEqual(run.capsule, ["cap_1"])
        self.assertEqual(observed.task_id, [85])
        self.assertEqual(run.task_id, [85])

    def test_strategy_replay_rejects_capsules_outside_observed_task_batch(self) -> None:
        class Capsules:
            @staticmethod
            def show(capsule_id: str) -> dict:
                return {"source_task_id": 1 if capsule_id == "cap_1" else 2}

            @staticmethod
            def verify_integrity(capsule_id: str) -> dict:
                return {"valid": True, "status": "replayable"}

        loop = SelfEvolutionLoop(
            self.store, None, None, None, SimpleNamespace(capsules=Capsules()),  # type: ignore[arg-type]
        )
        selected = loop._capsules(["cap_1", "cap_2"], source_task_ids={1})
        self.assertEqual(selected, ["cap_1"])


if __name__ == "__main__":
    unittest.main()
