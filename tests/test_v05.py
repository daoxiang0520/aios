from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from aios.config import Settings
from aios.capabilities import CapabilityRegistry, EvidenceContract
from aios.controller import ControllerError, LLMController
from aios.evaluation import Verifier
from aios.runtime import AIOSRuntime
from aios.sandbox import SandboxPolicyError, SandboxUnavailable
from aios.types import Action, ActionResult, Event, Intent, MemoryType, Plan, Task, TaskStatus


class V05Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = root / "config.json"
        config.write_text(json.dumps({"database": "data/a.db", "workspace": "workspace", "model": {"provider": "mock"}, "permissions": {"allowed_tools": ["read", "write", "edit", "bash"]}, "evolution": {"enabled": False}, "capabilities": {"network_enabled": False}, "sandbox": {"backend": "docker", "root": "sandbox"}}), encoding="utf-8")
        self.settings = Settings.load(config)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_network_task_stops_before_model_and_memory(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.controller.plan = Mock(side_effect=AssertionError("model must not be called"))
        runtime.store.add_event(Event("USER_REQUEST", {"message": "搜索 arXiv 最新 AI 论文并生成 latest.md"}))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.NEEDS_AUTHORITY)
        self.assertEqual(runtime.store.list_memories(), [])
        runtime.controller.plan.assert_not_called()

    def test_explicit_outside_workspace_path_is_forbidden_before_model(self) -> None:
        for request in ("读取../config.json并告诉我内容", r"读取C:\Users\person\secret.txt", "读取/etc/passwd"):
            with self.subTest(request=request):
                contract = EvidenceContract.from_request(request)
                assessment = CapabilityRegistry.default(sandbox_available=True, network_enabled=False).assess(contract)
                self.assertFalse(assessment["satisfied"])
                self.assertEqual(assessment["blocking"][0]["name"], "filesystem.outside_workspace")

        runtime = AIOSRuntime(self.settings)
        runtime.controller.plan = Mock(side_effect=AssertionError("model must not be called"))
        runtime.store.add_event(Event("USER_REQUEST", {"message": "读取../config.json并告诉我内容"}))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.BLOCKED_CAPABILITY)
        self.assertEqual(task.attempts, 1)
        runtime.controller.plan.assert_not_called()

    def test_degraded_answer_is_not_success_or_memory(self) -> None:
        verifier = Verifier()
        result = verifier.verify([], [], planned_count=0, task_done=True, request="回答问题", final_output="当前环境不具备网络能力，建议在其他环境执行")
        self.assertFalse(result["passed"])
        self.assertEqual(result["outcome"], "degraded")

    def test_bash_never_falls_back_to_host(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.sandbox.prepare(1, self.settings.workspace)
        runtime.sandbox.available = Mock(return_value=False)
        with self.assertRaises(SandboxUnavailable):
            runtime.sandbox.run("echo unsafe")

    def test_bash_rejects_broad_container_root_scans(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.sandbox.prepare(1, self.settings.workspace)
        with self.assertRaises(SandboxPolicyError):
            runtime.sandbox.run("cd / && grep -r hello .")
        with self.assertRaises(SandboxPolicyError):
            runtime.sandbox.run("find / -maxdepth 3 -name '*.md'")
        runtime.sandbox.discard()

    def test_write_snapshot_commits_only_after_success(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.store.add_event(Event("USER_REQUEST", {"message": "生成 hello.md", "actions": [{"tool": "write", "arguments": {"path": "hello.md", "content": "hello"}}]}))
        runtime.run_once()
        self.assertEqual(runtime.store.list_tasks()[0].status, TaskStatus.COMPLETED)
        self.assertEqual((self.settings.workspace / "hello.md").read_text(encoding="utf-8"), "hello")

    def test_network_evidence_cannot_be_replaced_by_file(self) -> None:
        path = self.settings.workspace / "latest.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("model knowledge", encoding="utf-8")
        result = Verifier().verify([Action("write", {"path": "latest.md"})], [ActionResult("write", True, {"path": str(path)})], planned_count=1, task_done=True, request="搜索 arXiv 最新论文并生成 latest.md", final_output=str(path))
        self.assertFalse(result["passed"])
        self.assertFalse(result["evidence_satisfied"])

    def test_native_final_text_is_not_converted_to_removed_echo_tool(self) -> None:
        config = self.settings.model
        config.provider = "deepseek"
        config.api_key_env = "TEST_DEEPSEEK_KEY"
        controller = LLMController(config)
        response = MagicMock()
        response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "task complete", "tool_calls": []}, "finish_reason": "stop"}]
        }).encode("utf-8")
        response.__enter__.return_value = response
        with patch.dict(os.environ, {"TEST_DEEPSEEK_KEY": "test-only"}), patch("urllib.request.urlopen", return_value=response):
            plan = controller.plan(Intent("test", "test", None, []), [])
        self.assertTrue(plan.done)
        self.assertEqual(plan.summary, "task complete")
        self.assertEqual(plan.actions, [])

    def test_internal_state_is_not_inside_searchable_workspace(self) -> None:
        runtime = AIOSRuntime(self.settings)
        snapshot = runtime.sandbox.prepare(7, self.settings.workspace)
        runtime.sandbox.expose_read_only_state({"tasks": [{"request": "hello"}]})
        self.assertFalse((snapshot / ".aios").exists())
        self.assertTrue((runtime.sandbox.session.state_path / "state.json").is_file())
        runtime.sandbox.discard()

    def test_agent_can_recover_from_observed_tool_failure(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.controller.plan = Mock(side_effect=[
            Plan("try inspection", [Action("read", {"path": "missing.txt"}, call_id="failed_read")], done=False),
            Plan("No matching user files were found.", [], done=True),
        ])
        runtime.store.add_event(Event("USER_REQUEST", {"message": "count matches in user files"}))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(task.result["evidence"]["failed_actions"], 1)
        check = next(item for item in task.result["evidence"]["verification"]["checks"] if item["name"] == "tool_failures_recovered")
        self.assertTrue(check["passed"])

    def test_last_model_round_forces_final_answer_without_tools(self) -> None:
        config = self.settings.model
        config.provider = "deepseek"
        config.api_key_env = "TEST_DEEPSEEK_KEY"
        controller = LLMController(config)
        response = MagicMock()
        response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "There are 0 matches.", "tool_calls": []}, "finish_reason": "stop"}]
        }).encode("utf-8")
        response.__enter__.return_value = response
        captured = {}

        def fake_open(request, timeout):
            captured.update(json.loads(request.data.decode("utf-8")))
            return response

        context = {"budget": {"remaining_model_calls_after_this": 0}}
        with patch.dict(os.environ, {"TEST_DEEPSEEK_KEY": "test-only"}), patch("urllib.request.urlopen", side_effect=fake_open):
            plan = controller.plan(Intent("test", "test", None, []), [], context)
        self.assertEqual(captured["tool_choice"], "none")
        self.assertTrue(plan.done)
        self.assertEqual(plan.actions, [])

    def test_new_attempt_does_not_show_stale_previous_result(self) -> None:
        runtime = AIOSRuntime(self.settings)
        task = Task("retry", "retry")
        task_id = runtime.store.create_task(task)
        runtime.store.update_task(task_id, TaskStatus.RETRYING, result={"old_attempt": True}, error="old")
        started = runtime.store.start_task_attempt(task_id)
        self.assertEqual(started.status, TaskStatus.RUNNING)
        self.assertIsNone(started.result)
        self.assertIsNone(started.error)

    def test_serialized_dsml_tool_call_cannot_be_a_final_answer(self) -> None:
        config = self.settings.model
        config.provider = "deepseek"
        config.api_key_env = "TEST_DEEPSEEK_KEY"
        controller = LLMController(config)
        response = MagicMock()
        response.read.return_value = json.dumps({
            "choices": [{"message": {"content": '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="bash">', "tool_calls": []}, "finish_reason": "stop"}]
        }).encode("utf-8")
        response.__enter__.return_value = response
        with patch.dict(os.environ, {"TEST_DEEPSEEK_KEY": "test-only"}), patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(ControllerError) as caught:
                controller.plan(Intent("test", "test", None, []), [])
        self.assertIn("serialized tool-call markup", str(caught.exception))

        verified = Verifier().verify([], [], planned_count=0, task_done=True, request="answer", final_output='<｜｜DSML｜｜tool_calls>')
        self.assertFalse(verified["passed"])
        protocol_check = next(item for item in verified["checks"] if item["name"] == "no_serialized_tool_protocol")
        self.assertFalse(protocol_check["passed"])

    def test_final_dsml_gets_one_no_tools_synthesis_repair(self) -> None:
        config = self.settings.model
        config.provider = "deepseek"
        config.api_key_env = "TEST_DEEPSEEK_KEY"
        controller = LLMController(config)
        payloads = []

        def response(content: str):
            value = MagicMock()
            value.read.return_value = json.dumps({
                "choices": [{"message": {"content": content, "tool_calls": []}, "finish_reason": "stop"}]
            }).encode("utf-8")
            value.__enter__.return_value = value
            return value

        replies = iter([
            response('<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="bash">'),
            response("Skill inspection completed; three active skills are available."),
        ])

        def fake_open(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return next(replies)

        context = {"budget": {"remaining_model_calls_after_this": 0, "protocol_repairs_remaining": 1}}
        with patch.dict(os.environ, {"TEST_DEEPSEEK_KEY": "test-only"}), patch(
            "urllib.request.urlopen", side_effect=fake_open
        ):
            plan = controller.plan(Intent("test", "test", None, []), [], context)

        self.assertTrue(plan.done)
        self.assertEqual(plan.summary, "Skill inspection completed; three active skills are available.")
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["tool_choice"], "none")
        self.assertNotIn("tools", payloads[1])
        self.assertNotIn("tool_choice", payloads[1])

    def test_failed_protocol_repair_is_terminal_without_full_task_retries(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.controller.plan = Mock(
            side_effect=ControllerError("Model protocol repair failed: no valid final answer")
        )
        runtime.store.add_event(Event("USER_REQUEST", {"message": "test skill"}))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.DEAD_LETTER)
        self.assertEqual(task.attempts, 1)
        self.assertEqual(runtime.store.count_pending_events(), 0)

    def test_protocol_polluted_success_memory_is_not_retrieved(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.memories.remember(MemoryType.EPISODIC, "bad <｜｜DSML｜｜tool_calls> trace")
        runtime.memories.remember(MemoryType.EPISODIC, "clean trace summary")
        selected = runtime.memories.retrieve("trace", limit=10)
        self.assertEqual([item.content for item in selected], ["clean trace summary"])

    def test_sandbox_subprocess_uses_utf8_with_replacement(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.sandbox.prepare(2, self.settings.workspace)
        runtime.sandbox.available = Mock(return_value=True)
        completed = __import__("subprocess").CompletedProcess([], 0, stdout="中文输出", stderr="")
        with patch("aios.sandbox.subprocess.run", return_value=completed) as run:
            result = runtime.sandbox.run("printf ok")
        self.assertEqual(result["stdout"], "中文输出")
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")
        runtime.sandbox.discard()


if __name__ == "__main__":
    unittest.main()
