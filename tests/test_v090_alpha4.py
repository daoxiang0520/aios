from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock

from aios.cli import main
from aios.config import Settings
from aios.controller import ControllerError
from aios.runtime import AIOSRuntime
from aios.types import Action, Event, Plan, Task, TaskStatus


class FreeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({
            "database": "./data/test.db",
            "workspace": "./workspace",
            "runtime": {"completion_mode": "free"},
            "model": {"provider": "mock"},
            "sandbox": {"backend": "local", "root": "./sandbox"},
            "permissions": {
                "allowed_tools": ["read", "write", "edit", "bash"],
                "allow_writes": True,
            },
        }), encoding="utf-8")
        self.settings = Settings.load(self.config_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_old_configs_remain_verified_by_default(self) -> None:
        path = self.root / "legacy.json"
        path.write_text(json.dumps({
            "database": "./data/legacy.db", "workspace": "./legacy-workspace",
        }), encoding="utf-8")
        self.assertEqual(Settings.load(path).runtime.completion_mode, "verified")

    def test_invalid_completion_mode_is_rejected(self) -> None:
        path = self.root / "invalid.json"
        path.write_text(json.dumps({
            "database": "./data/invalid.db", "workspace": "./invalid-workspace",
            "runtime": {"completion_mode": "wishful"},
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            Settings.load(path)

    def test_agent_stop_is_recorded_without_online_verification(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.verifier.verify = Mock(side_effect=AssertionError("online verifier called"))
        runtime.controller.plan = Mock(return_value=Plan("I believe the goal is reached", [], done=True))
        runtime.store.add_event(Event("USER_REQUEST", {"message": "answer freely"}))

        self.assertTrue(runtime.run_once())
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.STOPPED)
        self.assertIsNone(task.result["evidence"]["success"])
        self.assertFalse(task.result["evidence"]["online_verifier_enabled"])
        self.assertTrue(task.result["evidence"]["agent_declared_stop"])
        self.assertIsNone(task.result["evidence"]["host_observed_completion"])
        self.assertEqual(task.result["evidence"]["verification"]["mode"], "disabled")
        self.assertFalse(runtime.store.list_memories())

    def test_agent_can_yield_without_a_completion_judgment(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.controller.plan = Mock(return_value=Plan("Pausing here", [], done=False))
        runtime.store.add_event(Event("USER_REQUEST", {"message": "open-ended work"}))

        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.YIELDED)
        self.assertFalse(task.result["evidence"]["agent_declared_stop"])
        self.assertIsNone(task.result["evidence"]["success"])

    def test_unresolved_tool_failure_does_not_become_host_rejection(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.controller.plan = Mock(side_effect=[
            Plan("try", [Action("read", {"path": "missing.txt"})], done=False),
            Plan("I will stop despite the consequence", [], done=True),
        ])
        runtime.store.add_event(Event("USER_REQUEST", {"message": "inspect missing input"}))

        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.STOPPED)
        self.assertEqual(task.result["evidence"]["failed_actions"], 1)
        self.assertIsNone(task.result["evidence"]["success"])
        self.assertEqual(runtime.store.count_pending_events(), 0)

    def test_protocol_exhaustion_is_abandoned_not_dead_letter(self) -> None:
        runtime = AIOSRuntime(self.settings)
        task_id = runtime.store.create_task(Task("broken protocol", "broken protocol", max_attempts=1))
        runtime.store.add_event(Event(
            "TASK_REQUEST", {"task_id": task_id, "message": "broken protocol"},
        ))
        runtime.controller.plan = Mock(side_effect=ControllerError("invalid response"))

        runtime.run_once()
        task = runtime.store.get_task(task_id)
        self.assertEqual(task.status, TaskStatus.ABANDONED)
        self.assertEqual(runtime.store.list_dead_letters(), [])

    def test_shadow_verification_is_read_only(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.controller.plan = Mock(return_value=Plan("answer", [], done=True))
        runtime.store.add_event(Event("USER_REQUEST", {"message": "answer freely"}))
        runtime.run_once()
        before = runtime.store.list_tasks()[0]
        before_checkpoints = runtime.store.task_checkpoints(int(before.id))
        before_traces = runtime.store.recent_traces(500)

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main([
                "--config", str(self.config_path), "result", "shadow-verify", str(before.id),
            ])
        measured = json.loads(output.getvalue())
        after = runtime.store.get_task(int(before.id))

        self.assertEqual(exit_code, 0)
        self.assertEqual(measured["measurement_mode"], "offline_shadow")
        self.assertFalse(measured["affects_task_state"])
        self.assertEqual(after.status, TaskStatus.STOPPED)
        self.assertEqual(after.result, before.result)
        self.assertEqual(runtime.store.task_checkpoints(int(before.id)), before_checkpoints)
        self.assertEqual(runtime.store.recent_traces(500), before_traces)


if __name__ == "__main__":
    unittest.main()
