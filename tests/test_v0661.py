from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from aios.config import Settings
from aios.runtime import AIOSRuntime
from aios.tools import ToolExecutor, ToolRegistry
from aios.types import Action, Event, Plan, TaskStatus


class V0661RuntimeHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = {
            "database": "data/test.db",
            "workspace": "workspace",
            "model": {"provider": "mock"},
            "permissions": {"allowed_tools": ["read", "write", "edit", "bash"]},
            "sandbox": {"backend": "docker", "root": "sandbox", "timeout_seconds": 60},
            "budget": {
                "max_model_calls_per_cycle": 1,
                "max_tool_calls_per_cycle": 4,
                "max_model_calls_per_task": 3,
                "max_tool_calls_per_task": 8,
                "max_cycles_per_task": 3,
            },
        }
        path = self.root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        self.settings = Settings.load(path)
        self.settings.ensure_directories()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_legacy_timeout_migrates_to_default_without_clamping_max(self) -> None:
        self.assertEqual(self.settings.sandbox.default_timeout_seconds, 60)
        self.assertEqual(self.settings.sandbox.max_timeout_seconds, 300)

    def test_bash_uses_pipefail_and_honors_requested_timeout_up_to_max(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.sandbox.prepare(1, self.settings.workspace)
        runtime.sandbox.available = Mock(return_value=True)
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch("aios.sandbox.subprocess.run", return_value=completed) as run:
            runtime.sandbox.run("python -c \"print('ok')\" | head -5", timeout_seconds=180)
        args = run.call_args.args[0]
        self.assertEqual(run.call_args.kwargs["timeout"], 180)
        self.assertEqual(args[-5:-1], ["bash", "-o", "pipefail", "-lc"])

    def test_cycle_budget_defers_and_continues_without_new_attempt(self) -> None:
        runtime = AIOSRuntime(self.settings)
        (self.settings.workspace / "input.txt").write_text("evidence", encoding="utf-8")
        runtime.controller.plan = Mock(side_effect=[
            Plan("evidence collected", [Action("read", {"path": "input.txt"})], done=False),
            Plan("analysis complete", [], done=True),
        ])
        runtime.store.add_event(Event("USER_REQUEST", {"message": "分析 input.txt"}))

        self.assertTrue(runtime.run_once())
        deferred = runtime.store.list_tasks()[0]
        self.assertEqual(deferred.status, TaskStatus.DEFERRED)
        self.assertEqual(deferred.attempts, 1)
        self.assertEqual(runtime.store.count_pending_events(), 1)

        self.assertTrue(runtime.run_once())
        completed = runtime.store.list_tasks()[0]
        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        self.assertEqual(completed.attempts, 1)
        self.assertEqual(completed.result["evidence"]["task_tool_calls"], 1)
        self.assertEqual(completed.result["evidence"]["executed_actions"], 0)
        self.assertEqual(completed.result["evidence"]["failed_tool_calls"], 0)
        phases = [item["phase"] for item in runtime.store.task_checkpoints(int(completed.id))]
        self.assertIn("budget_deferred", phases)
        self.assertIn("continued", phases)

    def test_old_tool_results_are_compacted_to_trace_references(self) -> None:
        messages = [
            {"role": "tool", "content": json.dumps({"tool": "read", "ok": True, "observation_ref": "trace:1", "output": "x" * 5000})},
            {"role": "tool", "content": json.dumps({"tool": "bash", "ok": True, "observation_ref": "trace:2", "output": "hot"})},
        ]
        AIOSRuntime._compact_protocol_messages(messages, hot_results=1)
        cold = json.loads(messages[0]["content"])
        self.assertTrue(cold["compacted"])
        self.assertEqual(cold["observation_ref"], "trace:1")
        self.assertIn("hot", messages[1]["content"])

    def test_python_pipeline_failure_is_not_hidden_when_docker_available(self) -> None:
        runtime = AIOSRuntime(self.settings)
        if not runtime.sandbox.available():
            self.skipTest("Docker engine unavailable")
        runtime.sandbox.prepare(1, self.settings.workspace)
        runtime.security.workspace = runtime.sandbox.session.path
        executor = ToolExecutor(ToolRegistry(self.settings.permissions, sandbox=runtime.sandbox), runtime.security)
        result = executor.execute(Action("bash", {
            "command": "python -c \"import sys; sys.exit(7)\" | head -5",
        }))
        self.assertFalse(result.ok)
        self.assertEqual(result.output["exit_code"], 7)


if __name__ == "__main__":
    unittest.main()
