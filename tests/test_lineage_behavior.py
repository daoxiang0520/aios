from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aios.lineage import AutonomousLineageController, LineageManager
from aios.lineage_behavior import action_family, bound_digest, cross_task_patterns, summarize_behavior
from aios.storage import StateStore
from aios.types import Task


class LineageBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "test.db")
        self.store.initialize()
        self.manager = LineageManager(self.store)
        self.manager.ensure_root()

    def tearDown(self):
        self.temp.cleanup()

    def task(self, cycle):
        task_id = self.store.create_task(Task("task", "task"))
        self.store.bind_task_lineage(task_id, "lin_root")
        self.store.add_checkpoint(task_id, "started", {"cycle_id": cycle})
        return task_id

    def actions(self, cycle, actions, results=None, round_number=1):
        self.store.trace(cycle, "plan_created", {"round": round_number, "actions": actions})
        refs = []
        for result in results if results is not None else [{"tool": a["tool"], "ok": True} for a in actions]:
            refs.append(self.store.trace(cycle, "action_result", {"round": round_number, **result}))
        return refs

    def digest(self, task_id, limit=400):
        return summarize_behavior(task_id, self.store.task_behavior_traces(task_id, limit))

    def test_counts_only_results_not_unexecuted_plans_or_duplicate_checkpoints(self):
        tid = self.task("c1")
        self.store.add_checkpoint(tid, "completed", {"cycle_id": "c1"})
        refs = self.actions("c1", [
            {"tool": "read", "arguments": {"path": "input.pdf"}},
            {"tool": "write", "arguments": {"path": "never.md", "content": "SECRET"}},
        ], [{"tool": "read", "ok": True}])
        digest, _ = self.digest(tid)
        self.assertEqual(digest["observed_action_results"], 1)
        self.assertEqual(digest["tool_sequence"], ["read"])
        self.assertEqual(digest["sequence_trace_refs"], [f"trace:{refs[0]}"])
        self.assertEqual(digest["artifact_paths_written"], [])
        self.assertEqual(digest["resource_types_touched"], ["pdf"])

    def test_missing_history_is_explicitly_unknown(self):
        digest, patterns = self.digest(self.task("empty"))
        self.assertFalse(digest["source"]["history_available"])
        self.assertEqual(patterns, {})

    def test_task_cycle_scope_excludes_unrelated_traces(self):
        a = self.task("a")
        self.task("b")
        self.actions("a", [{"tool": "read", "arguments": {"path": "a.csv"}}])
        self.actions("b", [{"tool": "write", "arguments": {"path": "b.md"}}])
        digest, _ = self.digest(a)
        self.assertEqual(digest["tool_sequence"], ["read"])
        self.assertEqual(digest["resource_types_touched"], ["csv"])

    def test_mismatched_plan_is_not_guessed_and_patterns_do_not_span_cycles(self):
        tid = self.task("a")
        self.store.add_checkpoint(tid, "continued", {"cycle_id": "b"})
        self.actions("a", [{"tool": "read", "arguments": {"path": "a.csv"}}])
        self.actions("b", [{"tool": "write", "arguments": {"path": "b.md"}}])
        self.actions("b", [{"tool": "read", "arguments": {"path": "secret.txt"}}],
                     [{"tool": "bash", "ok": False}], round_number=2)
        digest, patterns = self.digest(tid)
        self.assertEqual(digest["source"]["unpaired_results"], 1)
        self.assertEqual(digest["action_families"]["unknown"], 1)
        self.assertEqual(patterns, {})

    def test_operational_normalization_not_text_or_failure_diagnosis(self):
        for command in (
            "curl https://example.test", "wget https://example.test",
            'python -c "import requests; requests.get(\'https://example.test\')"',
            'python -c "import urllib.request; urllib.request.urlopen(\'https://example.test\')"',
        ):
            self.assertEqual(action_family("bash", {"command": command}), "network_fetch")
        self.assertEqual(action_family("read", {"path": "https://example.test"}), "network_fetch")
        self.assertEqual(action_family("bash", {"command": "echo requests.get"}), "shell_exec")
        self.assertEqual(action_family("bash", {"command": "curl --version"}), "shell_exec")
        self.assertEqual(action_family("bash", {"command": 'python -c "print(\'requests.get\')"'}), "python_exec")
        self.assertEqual(action_family("bash", {"command": "python fetch.py"}), "python_exec")
        self.assertEqual(action_family("bash", {"command": "curl ..."}, True), "shell_exec")

    def test_write_evidence_cache_hits_and_failures_remain_distinct(self):
        tid = self.task("a")
        self.actions("a", [
            {"tool": "write", "arguments": {"path": "failed.md"}},
            {"tool": "write", "arguments": {"path": "C:\\Users\\private\\report.txt"}},
            {"tool": "write", "arguments": {"path": "/workspace/report.md"}},
            {"tool": "read", "arguments": {"path": "input.pdf"}},
            {"tool": "bash", "arguments": {"command": "python test.py"}},
        ], [
            {"tool": "write", "ok": False, "error": "PermissionError: denied"},
            {"tool": "write", "ok": True},
            {"tool": "write", "ok": True},
            {"tool": "read", "ok": True, "output": {"observation_cache": {"hit": True}}},
            {"tool": "bash", "ok": False, "error": "Command exited with 120", "output": {"exit_code": 120}},
        ])
        digest, _ = self.digest(tid)
        self.assertEqual(digest["artifact_paths_written"], ["report.md"])
        self.assertEqual(digest["reused_observation_requests"], 1)
        self.assertEqual(digest["failure_families"], {"PermissionError": 1, "tool_failure": 1})
        self.assertEqual(digest["observed_exit_codes"], {"120": 1})
        self.assertNotIn("private", json.dumps(digest))

    def test_cross_task_shape_has_real_references_including_successes(self):
        observations = []
        for cycle in ("a", "b"):
            tid = self.task(cycle)
            self.actions(cycle, [
                {"tool": "read", "arguments": {"path": "in.csv"}},
                {"tool": "write", "arguments": {"path": "out.json"}},
            ] * 3)
            digest, patterns = self.digest(tid)
            observations.append((tid, "stopped", patterns))
            self.assertTrue(digest["repeated_patterns"])
            self.assertEqual(digest["failure_families"], {})
        cross = cross_task_patterns(observations)
        pair = next(p for p in cross["patterns"] if p["action_shape"] == ["resource_read", "file_write"])
        self.assertEqual(pair["observed_in_tasks"], [1, 2])
        self.assertTrue(all(x["count"] == 3 for x in pair["occurrences"]))
        self.assertTrue(all(x["example_trace_refs"] for x in pair["occurrences"]))
        self.assertEqual(cross, cross_task_patterns(observations))
        self.assertEqual(cross_task_patterns(observations[:1])["patterns"], [])

    def test_sql_projection_and_model_digest_exclude_source_and_tool_output(self):
        tid = self.task("a")
        self.actions("a", [{"tool": "bash", "reason": "PRIVATE_REASON", "arguments": {
            "command": "python " + "x" * 10000, "content": "PRIVATE_SOURCE" * 10000,
        }}], [{"tool": "bash", "ok": True, "output": {"stdout": "PRIVATE_OUTPUT" * 10000}}])
        projected = self.store.task_behavior_traces(tid)
        self.assertLess(len(json.dumps(projected)), 5500)
        self.assertNotIn("PRIVATE_", json.dumps(projected))
        digest, _ = summarize_behavior(tid, projected)
        self.assertEqual(digest["source"]["arguments_truncated"], 1)
        self.assertNotIn("command", json.dumps(digest))

    def test_trace_cap_and_projection_cap_are_explicit(self):
        tid = self.task("a")
        for i in range(10):
            self.actions("a", [{"tool": "read", "arguments": {"path": f"{i}.csv"}}], round_number=i)
        digest, _ = self.digest(tid, limit=3)
        self.assertTrue(digest["source"]["trace_truncated"])
        self.assertEqual(digest["source"]["records_observed"], 3)
        complete, _ = self.digest(tid)
        bounded = bound_digest(complete, 1200)
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False)), 1200)
        self.assertEqual(bounded["observed_action_results"], 10)
        self.assertTrue(bounded["projection_truncated"])

    def test_lineage_facts_include_behavior_without_forcing_author_or_fork(self):
        for cycle in ("a", "b"):
            self.task(cycle)
            self.actions(cycle, [
                {"tool": "read", "arguments": {"path": "in.csv"}},
                {"tool": "write", "arguments": {"path": "out.json"}},
            ])

        class Capture:
            def reason(self, facts):
                self.facts = facts
                return {"action": "CONTINUE", "reason": "No persistent variation needed"}

        reasoner = Capture()
        result = AutonomousLineageController(self.store, reasoner, self.manager).decide("lin_root")
        self.assertEqual(result["effective_lineage_id"], "lin_root")
        self.assertEqual(len(self.store.list_lineages()), 1)
        self.assertTrue(reasoner.facts["cross_task_patterns"])
        self.assertTrue(all("behavior_digest" in t for t in reasoner.facts["lineage_tasks"]))
        self.assertNotIn("should_create_skill", json.dumps(reasoner.facts))
        self.assertNotIn("recommended_skill", json.dumps(reasoner.facts))
        self.assertEqual(self.store.list_candidates(), [])
        persisted = self.store.list_evolution_runs(1)[0]["diagnosis"]
        self.assertEqual(persisted["cross_task_patterns"], reasoner.facts["cross_task_patterns"])

    def test_lineage_evidence_task_and_text_budgets(self):
        for i in range(21):
            tid = self.task(f"c{i}")
            with self.store.connect() as connection:
                connection.execute("UPDATE tasks SET result=? WHERE id=?", (
                    json.dumps({"summary": "x" * 10000}), tid,
                ))
        controller = AutonomousLineageController(self.store, None, self.manager)
        facts = controller._facts("lin_root", 1000)
        self.assertEqual(len(facts["lineage_tasks"]), 20)
        self.assertEqual(facts["projection_policy"]["task_limit_requested"], 1000)
        total = 0
        for task in facts["lineage_tasks"]:
            self.assertEqual(len(task["summary"]), 1200)
            self.assertTrue(task["summary_truncated"])
            size = len(json.dumps(task["behavior_digest"], ensure_ascii=False))
            self.assertLessEqual(size, 1200)
            total += size
        self.assertLessEqual(total, 24000)

    def test_projection_does_not_append_trace_or_checkpoint(self):
        tid = self.task("a")
        self.actions("a", [{"tool": "read", "arguments": {"path": "in.csv"}}])
        before = (self.store.recent_traces(100), self.store.task_checkpoints(tid))
        self.assertEqual(self.digest(tid), self.digest(tid))
        self.assertEqual(before, (self.store.recent_traces(100), self.store.task_checkpoints(tid)))

    def test_large_failure_catalog_cannot_exceed_digest_budget(self):
        traces = []
        for i in range(100):
            traces.append({"id": i * 2, "cycle_id": "a", "kind": "plan_created", "data": {
                "round": i, "actions": [{"tool": "bash", "arguments": {"command": "unknown"}}],
            }})
            traces.append({"id": i * 2 + 1, "cycle_id": "a", "kind": "action_result", "data": {
                "round": i, "tool": "bash", "ok": False, "error": f"Class{i}Error: details",
                "output": {"exit_code": i},
            }})
        digest, _ = summarize_behavior(1, {"traces": traces, "limit": 400, "truncated": False})
        bounded = bound_digest(digest, 1200)
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False)), 1200)
        self.assertIn("failure_families", bounded["projection_omitted_fields"])
        self.assertIsNone(bounded["failure_families"])


if __name__ == "__main__":
    unittest.main()
