from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aios.config import Settings
from aios.skills import SkillManager, SkillPromotionError
from aios.storage import StateStore
from aios.types import Task, TaskStatus
from aios.utility import SkillUtilityEvaluator


SOURCE = '''import argparse,json
p=argparse.ArgumentParser(); p.add_argument("--input-json",default="{}"); a=p.parse_args()
d=json.loads(a.input_json); print(json.dumps({"value":d.get("value","pass")}))
'''


class Broker:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def run_candidate(self, package: Path, command: str, timeout_seconds: int) -> dict:
        return {
            "exit_code": 1 if self.fail else 0,
            "stdout": "failed" if self.fail else '{"value":"pass"}',
            "stderr": "",
        }


class V064UtilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = root / "config.json"
        config.write_text(json.dumps({
            "database": "data/a.db", "workspace": "workspace",
            "model": {"provider": "mock"},
            "sandbox": {"backend": "docker", "root": "sandbox"},
            "skills": {"enabled": True, "root": "skills", "require_human_promotion": True},
        }), encoding="utf-8")
        self.settings = Settings.load(config)
        self.settings.ensure_directories()
        self.store = StateStore(self.settings.database)
        self.store.initialize()
        self.manager = SkillManager(self.settings.skills_root, self.settings.skills)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def baseline_task(self, *, success: bool = True) -> int:
        task = Task("baseline", "historical procedure")
        task_id = self.store.create_task(task)
        self.store.update_task(
            task_id, TaskStatus.COMPLETED if success else TaskStatus.DEGRADED,
            result={
                "cycle_id": f"baseline-{task_id}",
                "actions": [{"tool": "bash", "arguments": {"command": "rg value ."}}],
                "action_results": [{"tool": "bash", "ok": success, "duration_ms": 1000}],
                "evidence": {
                    "success": success, "model_api_calls": 6,
                    "model_tokens": 4000,
                },
            },
        )
        return task_id

    def candidate(self, replay_task_ids: list[int]) -> str:
        proposal = self.manager.propose({
            "name": "sample_replay", "version": "1.0.0",
            "description": "Replay utility fixture.", "origin": "agent",
            "required_capabilities": ["process.sandbox_exec"],
            "input_schema": {"type": "object"},
            "tests": [{"input": {"value": "pass"}, "expect_exit": 0, "stdout_contains": "pass"}],
            "replay_task_ids": replay_task_ids,
        }, SOURCE)
        candidate_id = proposal["candidate_id"]
        self.manager.benchmark(candidate_id, Broker())
        return candidate_id

    def test_replay_compares_real_baseline_and_opens_promotion_gate(self) -> None:
        candidate_id = self.candidate([self.baseline_task()])
        evaluator = SkillUtilityEvaluator(self.store, self.manager, Broker())
        report = evaluator.replay(candidate_id, runs=3)
        self.assertTrue(report["passed"])
        self.assertGreater(report["utility_delta"], 0)
        self.assertEqual(report["evidence_level"], "historical_baseline_plus_execution_proxy")
        promoted = self.manager.promote(candidate_id, approved=True)
        self.assertEqual(promoted["status"], "active")

    def test_missing_historical_baseline_blocks_agent_promotion(self) -> None:
        candidate_id = self.candidate([])
        report = SkillUtilityEvaluator(self.store, self.manager, Broker()).replay(candidate_id)
        self.assertFalse(report["passed"])
        self.assertEqual(report["evidence_level"], "insufficient_historical_baseline")
        with self.assertRaises(SkillPromotionError):
            self.manager.promote(candidate_id, approved=True)

    def test_negative_transfer_is_detected_and_persisted(self) -> None:
        candidate_id = self.candidate([self.baseline_task()])
        report = SkillUtilityEvaluator(self.store, self.manager, Broker(fail=True)).replay(candidate_id)
        self.assertTrue(report["negative_transfer"])
        self.assertFalse(report["passed"])
        stored = self.store.list_skill_replay_reports(candidate_id=candidate_id)
        self.assertTrue(stored[0]["negative_transfer"])

    def test_observed_utility_separates_execution_from_task_completion(self) -> None:
        self.store.add_skill_usage({
            "invocation_id": "one", "cycle_id": "c", "task_id": None,
            "model_round": 1, "sequence_index": 1, "model_calls_before": 1,
            "tokens_before": 100, "skill_name": "observed", "skill_version": "1.0.0",
            "status": "success", "input_digest": "digest", "duration_ms": 10,
        })
        self.store.finalize_skill_usage(
            "c", verifier_passed=False, task_outcome="degraded",
            model_calls_after=3, tokens_after=500,
        )
        report = SkillUtilityEvaluator(self.store, self.manager, Broker()).observed_utility("observed")
        self.assertEqual(report["samples"], 1)
        self.assertEqual(report["metrics"]["success_rate"], 1.0)
        self.assertEqual(report["metrics"]["true_completion_rate"], 0.0)
        self.assertEqual(report["negative_transfer_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
