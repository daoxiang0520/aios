from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aios.config import Settings
from aios.cli import _parser
from aios.open_evolution import OpenEvolutionAgent, compare_evolution_modes
from aios.runtime_evolution import ExternalRuntimeEvaluator, RuntimeCandidateManager, RuntimeMutationPolicy
from aios.storage import StateStore
from aios.types import Task, TaskStatus


def call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class ScriptedBackend:
    def __init__(self, responses: list[list[dict]]):
        self.responses = list(responses)
        self.messages = []

    def respond(self, messages, tools):
        self.messages.append(json.loads(json.dumps(messages)))
        return {
            "content": None,
            "tool_calls": self.responses.pop(0),
            "usage": {"model_calls": 1, "total_tokens": 100},
        }


class V090Alpha2OpenEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src" / "aios").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "src" / "aios" / "evaluation.py").write_text(
            "def completion(failures, done):\n    return done\n", encoding="utf-8",
        )
        for relative in RuntimeMutationPolicy.ROOT_OF_TRUST:
            path = self.root / relative
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# immutable fixture\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text(
            "[project]\nname='open-evolution-fixture'\nversion='0.0.0'\n", encoding="utf-8",
        )
        (self.root / "README.md").write_text("fixture\n", encoding="utf-8")
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
        self.task_id = self.store.create_task(Task("fixture", "repair completion semantics"))
        self.store.update_task(
            self.task_id, TaskStatus.DEAD_LETTER,
            result={"evidence": {"model_api_calls": 3, "model_tokens": 5000}},
            error="unresolved failure accepted",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_open_agent_experiments_edits_tests_and_submits_candidate(self):
        backend = ScriptedBackend([
            [call("c1", "search_source", {"query": "return done", "path": "src"})],
            [call("c2", "run_diagnostic", {"command": "python -m unittest discover"})],
            [call("c3", "edit_candidate", {
                "path": "src/aios/evaluation.py",
                "old_text": "    return done\n",
                "new_text": "    return done and not failures\n",
            })],
            [call("c4", "write_candidate_test", {
                "path": "candidate_tests/test_completion.py",
                "content": "import unittest\nclass T(unittest.TestCase):\n    def test_x(self): self.assertTrue(True)\n",
            })],
            [call("c5", "run_diagnostic", {"command": "python -m unittest discover"})],
            [call("c6", "submit_candidate", {"reason": "diagnostic changed from fail to pass"})],
        ])
        agent = OpenEvolutionAgent(self.settings, self.store, backend)
        with patch.object(agent, "_diagnostic", side_effect=[
            {"ok": False, "exit_code": 1, "stderr": "FAIL"},
            {"ok": True, "exit_code": 0, "stdout": "OK"},
        ]):
            report = agent.run(self.task_id, max_rounds=8)
        self.assertEqual(report["status"], "proposed")
        self.assertEqual(report["metrics"]["diagnostic_experiments_run"], 2)
        self.assertEqual(report["metrics"]["model_calls"], 6)
        self.assertEqual(report["metrics"]["total_tokens"], 600)
        self.assertFalse(report["production_activated"])
        manager = RuntimeCandidateManager(self.settings, self.store)
        candidate = manager.show(report["candidate_id"])
        self.assertEqual(candidate["proposal"]["mode"], "open_evolution/v1")
        self.assertIn("src/aios/evaluation.py", candidate["changed_paths"])
        self.assertTrue((manager.repository(report["candidate_id"]) / "candidate_tests" / "test_completion.py").is_file())
        self.assertTrue((manager._candidate_path(report["candidate_id"]) / "transcript.json").is_file())

    def test_root_of_trust_edit_is_observed_and_cannot_escape_boundary(self):
        backend = ScriptedBackend([
            [call("c1", "edit_candidate", {
                "path": "src/aios/security.py", "old_text": "# immutable fixture\n",
                "new_text": "allow_all = True\n",
            })],
            [call("c2", "no_action", {"reason": "required change is outside authority"})],
        ])
        report = OpenEvolutionAgent(self.settings, self.store, backend).run(self.task_id)
        self.assertEqual(report["disposition"]["action"], "NO_ACTION")
        failure = report["transcript"][0]["tools"][0]["result"]
        self.assertFalse(failure["ok"])
        self.assertIn("outside mutable surface", failure["error"])
        self.assertEqual(RuntimeCandidateManager(self.settings, self.store).list(), [])

    def test_no_action_discards_experimental_edits(self):
        backend = ScriptedBackend([
            [call("c1", "edit_candidate", {
                "path": "src/aios/evaluation.py", "old_text": "    return done\n",
                "new_text": "    return False\n",
            })],
            [call("c2", "no_action", {"reason": "experiment falsified the hypothesis"})],
        ])
        report = OpenEvolutionAgent(self.settings, self.store, backend).run(self.task_id)
        self.assertFalse(report["changed"])
        self.assertEqual(report["disposition"]["action"], "NO_ACTION")
        self.assertEqual(RuntimeCandidateManager(self.settings, self.store).list(), [])
        production = (self.root / "src" / "aios" / "evaluation.py").read_text(encoding="utf-8")
        self.assertIn("return done", production)

    def test_external_gate_remains_host_owned_for_open_candidate(self):
        backend = ScriptedBackend([
            [call("c1", "edit_candidate", {
                "path": "src/aios/evaluation.py", "old_text": "    return done\n",
                "new_text": "    return done and not failures\n",
            })],
            [call("c2", "submit_candidate", {"reason": "candidate ready"})],
        ])
        report = OpenEvolutionAgent(self.settings, self.store, backend).run(self.task_id)
        manager = RuntimeCandidateManager(self.settings, self.store)
        evaluator = ExternalRuntimeEvaluator(self.settings, manager)
        with patch.object(evaluator, "_docker_ready", side_effect=AssertionError("no gate means no Docker")):
            result = evaluator.evaluate(report["candidate_id"])
        self.assertEqual(result["selection"], "unsupported_external_gate")
        self.assertFalse(result["passed"])
        self.assertFalse(result["production_activated"])

    def test_comparison_preserves_missing_values(self):
        self.store.add_evolution_run(
            f"runtime:{self.task_id}", {}, [], "observed",
            {"proposal": {"decision": "NO_ACTION"}},
        )
        report = compare_evolution_modes(self.store, [self.task_id, 999])
        self.assertEqual(report["cases"][0]["structured"]["decision"], "NO_ACTION")
        self.assertIsNone(report["cases"][0]["open"])
        self.assertIsNone(report["cases"][1]["structured"])
        self.assertTrue(report["missing_values_are_not_scored"])

    def test_cli_exposes_open_run_and_structured_comparison(self):
        parser = _parser()
        opened = parser.parse_args([
            "evolution", "runtime-open", "77", "--max-rounds", "9",
        ])
        compared = parser.parse_args([
            "evolution", "runtime-compare", "--task-id", "77", "--task-id", "79",
        ])
        self.assertEqual(opened.evolution_command, "runtime-open")
        self.assertEqual(opened.max_rounds, 9)
        self.assertEqual(opened.benchmark_role, "mechanism_regression")
        self.assertEqual(compared.task_id, [77, 79])

    def test_open_world_excludes_current_docs_tests_and_annotations(self):
        (self.root / "tests" / "test_task77_answer.py").write_text(
            "KNOWN_ANSWER = 'evaluation.py'\n", encoding="utf-8",
        )
        backend = ScriptedBackend([
            [call("c1", "search_source", {"query": "KNOWN_ANSWER", "path": "."})],
            [call("c2", "read_source", {"path": "README.md"})],
            [call("c3", "no_action", {"reason": "no supported mutable defect"})],
        ])
        report = OpenEvolutionAgent(self.settings, self.store, backend).run(self.task_id)
        search = report["transcript"][0]["tools"][0]["result"]
        read = report["transcript"][1]["tools"][0]["result"]
        self.assertEqual(search["matches"], [])
        self.assertFalse(read["ok"])
        self.assertFalse(report["benchmark_leakage_detected"])
        self.assertEqual(
            report["world_integrity"]["visible_roots"],
            ["src/aios/", "pyproject.toml", "candidate_tests/ (agent-created only)"],
        )

    def test_original_task_is_evidence_and_evolution_objective_is_current_goal(self):
        backend = ScriptedBackend([
            [call("c1", "no_action", {"reason": "insufficient runtime evidence"})],
        ])
        OpenEvolutionAgent(self.settings, self.store, backend).run(self.task_id)
        framing = json.loads(backend.messages[0][1]["content"])
        self.assertEqual(framing["current_goal"], "evolution_objective")
        self.assertEqual(
            framing["evolution_objective"]["original_task_role"],
            "evidence_source_not_current_goal",
        )
        self.assertEqual(framing["original_task"]["id"], self.task_id)
        self.assertIn("Do not perform the original task", framing["evolution_objective"]["objective"])

    def test_observation_is_compacted_then_reloadable_by_reference(self):
        backend = ScriptedBackend([
            [call("c1", "inspect_experience", {"section": "task"})],
            [call("c2", "read_observation", {"ref": "R0001"})],
            [call("c3", "no_action", {"reason": "observation checked"})],
        ])
        report = OpenEvolutionAgent(self.settings, self.store, backend).run(self.task_id)
        first_tool_for_round_two = json.loads(backend.messages[1][3]["content"])
        first_tool_for_round_three = json.loads(backend.messages[2][3]["content"])
        reload_for_round_three = json.loads(backend.messages[2][5]["content"])
        self.assertFalse(first_tool_for_round_two.get("compacted", False))
        self.assertTrue(first_tool_for_round_three["compacted"])
        self.assertEqual(first_tool_for_round_three["observation_ref"], "R0001")
        self.assertNotIn("text", first_tool_for_round_three)
        self.assertEqual(reload_for_round_three["observation_ref"], "R0001")
        self.assertEqual(reload_for_round_three["offset_unit"], "characters")
        self.assertNotIn("source_observation_ref", reload_for_round_three)
        persisted_reload = report["transcript"][1]["tools"][0]["result"]
        self.assertNotIn("text", persisted_reload)
        self.assertEqual(persisted_reload["observation_ref"], "R0001")
        self.assertGreater(report["metrics"]["observation_compactions"], 0)
        self.assertEqual(report["metrics"]["observation_reloads"], 1)
        self.assertEqual(report["metrics"]["observation_objects_created"], 1)

    def test_observation_view_is_idempotent_and_pages_canonical_payload(self):
        agent = OpenEvolutionAgent(self.settings, self.store, ScriptedBackend([]))
        payload = json.dumps(
            {"中文": "内容" * 5000, "items": list(range(100))},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        import hashlib
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        observations = {"R0042": {
            "ref": "R0042", "kind": "runtime_experience", "payload": payload,
            "digest": digest, "metadata": {"producer_tool": "inspect_experience"},
        }}
        before = json.loads(json.dumps(observations, ensure_ascii=False))
        for _ in range(100):
            repeated = agent._execute(
                "read_observation", {"ref": "R0042", "offset": 0, "limit": 1000},
                facts={}, repository=self.root, baseline={}, observations=observations,
            )
            self.assertEqual(repeated["observation_ref"], "R0042")
        metadata_only = agent._execute(
            "read_observation", {"ref": "R0042", "limit": 0},
            facts={}, repository=self.root, baseline={}, observations=observations,
        )
        self.assertEqual(metadata_only["text"], "")
        self.assertEqual(metadata_only["total_characters"], len(payload))
        self.assertEqual(metadata_only["next_offset"], 0)
        parts = []
        offset = 0
        while True:
            result = agent._execute(
                "read_observation", {"ref": "R0042", "offset": offset, "limit": 4096},
                facts={}, repository=self.root, baseline={}, observations=observations,
            )
            self.assertEqual(result["observation_ref"], "R0042")
            self.assertEqual(result["digest"], digest)
            parts.append(result["text"])
            if not result["truncated"]:
                break
            offset = result["next_offset"]
        self.assertEqual("".join(parts), payload)
        self.assertEqual(observations, before)
        self.assertEqual(len(observations), 1)
        self.assertFalse(any(
            item["metadata"].get("producer_tool") == "read_observation"
            for item in observations.values()
        ))

    def test_repeated_reload_persists_only_bounded_access_metadata(self):
        reloads = 20
        responses = [[call("c1", "inspect_experience", {"section": "evaluations"})]]
        responses.extend([
            [call(f"r{index}", "read_observation", {
                "ref": "R0001", "offset": 0, "limit": 8000,
            })]
            for index in range(reloads)
        ])
        responses.append([call("done", "no_action", {"reason": "reload stability checked"})])
        report = OpenEvolutionAgent(
            self.settings, self.store, ScriptedBackend(responses),
        ).run(self.task_id, max_rounds=24)
        self.assertEqual(report["metrics"]["observation_reloads"], reloads)
        self.assertEqual(report["metrics"]["observation_objects_created"], 1)
        for round_item in report["transcript"][1:-1]:
            persisted = round_item["tools"][0]["result"]
            self.assertEqual(persisted["observation_ref"], "R0001")
            self.assertNotIn("text", persisted)
            self.assertLess(len(json.dumps(persisted)), 1000)

    def test_measurement_keeps_missing_model_terminal_separate_from_host_fallback(self):
        backend = ScriptedBackend([[], []])
        agent = OpenEvolutionAgent(self.settings, self.store, backend)
        clean_world = {
            "provenance_valid": True,
            "docker_ready_at_start": True,
            "benchmark_leakage_detected": False,
        }
        with patch.object(agent, "_world_integrity", return_value=clean_world):
            report = agent.run(
                self.task_id, max_rounds=2, benchmark_role="capability_holdout",
            )
        self.assertEqual(report["model_intended_disposition"], "missing")
        self.assertEqual(report["effective_host_disposition"], "NO_ACTION")
        self.assertTrue(report["terminal_decision_missing"])
        self.assertEqual(report["experimental_validity"]["state"], "valid")
        self.assertTrue(report["capability_evaluation_eligible"])

    def test_legacy_open_run_with_current_tests_is_marked_invalid_not_rescored(self):
        self.store.add_evolution_run(
            f"runtime-open:{self.task_id}", {}, [], "observed", {
                "schema": "open_evolution_run/v1",
                "disposition": {"action": "NO_ACTION"},
                "transcript": [{"tools": [{
                    "name": "search_source", "arguments": {"path": "tests"},
                    "result": {"matches": [{"path": "tests/test_task77.py"}]},
                }]}],
            },
        )
        opened = compare_evolution_modes(self.store, [self.task_id])["cases"][0]["open"]
        validity = opened["experimental_validity"]
        self.assertEqual(validity["state"], "invalid_leakage")
        self.assertIn("benchmark_leakage", validity["invalid_reasons"])
        self.assertIn("evolution_objective_misbinding", validity["invalid_reasons"])
        self.assertTrue(validity["legacy_run_assessment"])
        self.assertIsNone(opened["correct_diagnosis"])


if __name__ == "__main__":
    unittest.main()
