from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aios.config import Settings
from aios.runtime_evolution import (
    ExternalRuntimeEvaluator,
    ModelRuntimeMutationReasoner,
    RuntimeCandidateManager,
    RuntimeDiagnosisBenchmark,
    RuntimeExperienceBuilder,
    RuntimeMutationPolicy,
    RuntimeMutationPolicyError,
)
from aios.storage import StateStore
from aios.types import Task, TaskStatus


class StubRuntimeReasoner:
    def propose(self, facts, source_index, source_root):
        return {
            "decision": "PROPOSE",
            "attribution": {
                "selected_hypothesis": "evaluation recovery semantics",
                "evidence_refs": ["trace:2", "trace:4"],
            },
            "mutation_target": "Verifier.verify",
            "expected_effects": ["unresolved tool failure is rejected"],
            "risks": ["may reject genuinely recovered work"],
            "patch": {
                "edits": [{
                    "path": "src/aios/evaluation.py",
                    "old_text": "recovered = failures > 0 and task_done",
                    "new_text": "recovered = False",
                }],
                "new_tests": [{
                    "path": "candidate_tests/test_recovery.py",
                    "content": "import unittest\n\nclass RecoveryTest(unittest.TestCase):\n    def test_candidate(self):\n        self.assertTrue(True)\n",
                }],
            },
            "model_usage": {"model_calls": 2},
        }


class StubTwoStageReasoner(ModelRuntimeMutationReasoner):
    def __init__(self):
        self.calls = []
        self.controller = SimpleNamespace(
            config=SimpleNamespace(provider="openai_compatible")
        )

    def _request_json(self, system, payload):
        self.calls.append((system, payload))
        if len(self.calls) == 1:
            return {
                "decision": "INVESTIGATE",
                "hypotheses": [
                    {"id": "H1", "claim": "model strategy", "evidence_refs": ["trace:2"]},
                    {"id": "H2", "claim": "evaluation semantics", "evidence_refs": ["trace:4"]},
                ],
                "selected_hypothesis": "H2",
                "inspect_files": ["src/aios/evaluation.py"],
                "reason": "cross-layer facts disagree",
            }
        return StubRuntimeReasoner().propose(None, None, None)


class V080CandidateRuntimeMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src" / "aios").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "src" / "aios" / "evaluation.py").write_text(
            "class Verifier:\n"
            "    def verify(self):\n"
            "        failures = 1\n"
            "        task_done = True\n"
            "        recovered = failures > 0 and task_done\n"
            "        return recovered\n",
            encoding="utf-8",
        )
        (self.root / "src" / "aios" / "security.py").write_text(
            "class SecurityKernel:\n    pass\n", encoding="utf-8",
        )
        (self.root / "pyproject.toml").write_text(
            "[project]\nname='fixture'\nversion='0.0.0'\n", encoding="utf-8",
        )
        config = self.root / "config.json"
        config.write_text(json.dumps({
            "database": "data/a.db", "workspace": "workspace",
            "model": {"provider": "mock"},
            "sandbox": {"backend": "docker", "root": "sandbox"},
            "skills": {"enabled": False, "root": "skills"},
            "experiments": {"root": "experiments"},
            "evolution": {"enabled": False},
        }), encoding="utf-8")
        self.settings = Settings.load(config)
        self.settings.ensure_directories()
        self.store = StateStore(self.settings.database)
        self.store.initialize()
        self.task_id = self.store.create_task(Task("Task74", "complete C++ solution"))
        self.store.update_task(self.task_id, TaskStatus.COMPLETED, result={
            "evidence": {"model_api_calls": 16, "model_tokens": 188327, "failed_actions": 1}
        })
        self.cycle = "task74-cycle"
        self.store.add_checkpoint(self.task_id, "started", {"cycle_id": self.cycle})
        self.store.trace(self.cycle, "plan_created", {
            "round": 1, "done": False, "summary": "compile",
            "actions": [{"tool": "bash", "arguments": {"command": "g++ solution.cpp"}}],
        })
        self.store.trace(self.cycle, "action_result", {
            "round": 1, "tool": "bash", "ok": False,
            "output": {"exit_code": 127, "stdout": "", "stderr": "g++: command not found"},
            "error": "MissingExecutable: exit 127",
        })
        self.store.trace(self.cycle, "plan_created", {
            "round": 2, "done": True, "summary": "The C++ code is complete and correct.",
            "actions": [], "completion_metadata": {"claims_complete": True},
        })
        self.store.trace(self.cycle, "evaluation", {
            "success": True, "failed_actions": 1, "model_api_calls": 16, "model_tokens": 188327,
            "verification": {"checks": [{
                "name": "tool_failures_recovered", "passed": True,
                "detail": "failures=1; recovered_by_observed_final_plan=True",
            }], "result_vector": {"outcome": "completed"}},
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_experience_capsule_preserves_cross_layer_facts_without_diagnosis(self):
        facts = RuntimeExperienceBuilder(self.store).build(self.task_id)
        self.assertEqual(facts["executions"][0]["error"], "MissingExecutable: exit 127")
        self.assertIn("complete and correct", facts["final_claims"][0]["text"])
        check = facts["evaluations"][0]["checks"][0]
        self.assertTrue(check["passed"])
        self.assertTrue(facts["observation_contract"]["no_host_recommended_fix"])
        self.assertNotIn("recommended_fix", facts)

    def test_two_stage_reasoner_selects_source_before_receiving_code(self):
        reasoner = StubTwoStageReasoner()
        facts = RuntimeExperienceBuilder(self.store).build(self.task_id)
        proposal = reasoner.propose(
            facts, RuntimeExperienceBuilder.source_index(self.root), self.root,
        )
        self.assertEqual(len(reasoner.calls), 2)
        self.assertNotIn("selected_sources", reasoner.calls[0][1])
        self.assertEqual(proposal["inspected_files"], ["src/aios/evaluation.py"])
        self.assertEqual(proposal["attribution"]["selected_hypothesis"], "H2")
        self.assertNotIn("blind_annotation", json.dumps(reasoner.calls[0][1]))
        self.assertNotIn("relevant_files", json.dumps(reasoner.calls[0][1]))

    def test_task74_benchmark_separates_good_diagnosis_from_incomplete_localization(self):
        proposal = {
            "decision": "NO_ACTION",
            "reason": "insufficient evidence for a safe Runtime mutation",
            "attribution_summary": "The complete and correct claim exceeds compile evidence.",
            "attribution": {
                "reason": "g++ was missing, so the C++ result was not compiled or verified",
                "selected_hypothesis": "claim exceeds evidence",
                "hypotheses": [{"claim": "C++ was not compiled but declared complete and correct"}],
                "inspect_files": [
                    "src/aios/sandbox.py", "src/aios/tools.py", "src/aios/runtime.py",
                ],
            },
            "patch": {"edits": [], "new_tests": []},
        }
        result = RuntimeDiagnosisBenchmark.score(74, proposal)
        self.assertTrue(result["diagnosis"]["success"])
        self.assertFalse(result["localization"]["success"])
        self.assertFalse(result["mutation"]["generated"])
        self.assertTrue(result["no_action"]["epistemically_safe"])
        self.assertFalse(result["no_action"]["correct_for_known_disposition"])

    def test_no_action_precision_distinguishes_root_of_trust_case(self):
        proposal = {
            "decision": "NO_ACTION",
            "reason": "URL was misclassified as a Windows filesystem path; causal code is protected.",
            "attribution": {
                "selected_hypothesis": "URL/path false positive",
                "hypotheses": [{"claim": "https URL was misclassified as an outside workspace path"}],
                "inspect_files": ["src/aios/capabilities.py"],
            },
        }
        result = RuntimeDiagnosisBenchmark.score(70, proposal)
        self.assertTrue(result["diagnosis"]["success"])
        self.assertTrue(result["localization"]["success"])
        self.assertTrue(result["no_action"]["correct_for_known_disposition"])

    def test_benchmark_suite_reports_missing_blind_experiments_without_inventing_scores(self):
        proposal = {
            "decision": "NO_ACTION",
            "reason": "g++ compile unavailable; claim was unverified",
            "attribution_summary": "complete and correct claim exceeds evidence",
            "attribution": {
                "hypotheses": [{"claim": "not compiled but declared complete and correct"}],
                "inspect_files": ["src/aios/runtime.py"],
            },
        }
        self.store.add_evolution_run(
            "runtime:74", {"fact_digest": "blind"}, [], "observed", {"proposal": proposal},
        )
        report = RuntimeDiagnosisBenchmark(self.store).suite([64, 74])
        self.assertEqual(report["scored_tasks"], [74])
        self.assertEqual(report["missing_tasks"], [64])
        self.assertEqual(report["metrics"]["diagnosis_accuracy"], 1.0)
        self.assertIsNone(report["metrics"]["external_gate_pass_rate"])
        self.assertTrue(report["metric_contract"]["not_a_scalar_reward"])

    def test_candidate_patch_never_modifies_production(self):
        production = (self.root / "src" / "aios" / "evaluation.py").read_text(encoding="utf-8")
        manager = RuntimeCandidateManager(self.settings, self.store, StubRuntimeReasoner())
        report = manager.propose(self.task_id)
        candidate = manager.repository(report["candidate_id"])
        self.assertEqual(
            (self.root / "src" / "aios" / "evaluation.py").read_text(encoding="utf-8"),
            production,
        )
        self.assertIn(
            "recovered = False",
            (candidate / "src" / "aios" / "evaluation.py").read_text(encoding="utf-8"),
        )
        self.assertTrue((candidate / "candidate_tests" / "test_recovery.py").is_file())
        self.assertFalse(report["production_activated"])

    def test_root_of_trust_edit_is_rejected_before_snapshot(self):
        proposal = StubRuntimeReasoner().propose(None, None, None)
        proposal["patch"]["edits"][0]["path"] = "src/aios/security.py"
        with self.assertRaises(RuntimeMutationPolicyError):
            RuntimeMutationPolicy.validate_proposal(proposal)

    def test_external_evaluator_rejects_forbidden_post_creation_change(self):
        manager = RuntimeCandidateManager(self.settings, self.store, StubRuntimeReasoner())
        report = manager.propose(self.task_id)
        candidate = manager.repository(report["candidate_id"])
        (candidate / "src" / "aios" / "security.py").write_text(
            "class SecurityKernel:\n    allow_all = True\n", encoding="utf-8",
        )
        evaluator = ExternalRuntimeEvaluator(self.settings, manager)
        with patch.object(evaluator, "_docker_ready", return_value=False):
            result = evaluator.evaluate(report["candidate_id"])
        self.assertFalse(result["policy_gate"]["passed"])
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["production_activated"])


if __name__ == "__main__":
    unittest.main()
