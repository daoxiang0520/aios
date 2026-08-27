from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from aios.config import Settings
from aios.capabilities import CapabilityRegistry
from aios.controller import ControllerError, LLMController
from aios.evolution import EvolutionManager, EvolutionPolicyError
from aios.evaluation import Verifier
from aios.memory import MemoryManager
from aios.plugins import PluginManager, PluginValidationError, state_query_manifest
from aios.runtime import AIOSRuntime
from aios.security import PermissionDenied, SecurityKernel
from aios.storage import StateStore
from aios.tools import ToolExecutor, ToolRegistry
from aios.types import Action, Event, Goal, GoalType, Intent, MemoryType, Plan, TaskStatus


class AIOSMVPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = {
            "database": "./data/test.db",
            "workspace": "./workspace",
            "model": {"provider": "mock"},
            "permissions": {
                "allowed_tools": ["echo", "list_files", "read_file", "write_file"],
                "allow_writes": True,
            },
        }
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.settings = Settings.load(self.config_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_goal_priority_and_event_cycle(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.store.add_goal(Goal("stay safe", GoalType.SAFETY, 100))
        runtime.store.add_goal(Goal("do work", GoalType.USER, 100))
        runtime.store.add_event(Event("USER_REQUEST", {"message": "hello"}, 90))
        self.assertTrue(runtime.run_once())
        self.assertEqual(runtime.store.count_pending_events(), 0)
        traces = runtime.store.recent_traces(20)
        intent = next(item for item in traces if item["kind"] == "intent_selected")
        self.assertEqual(intent["data"]["goal_id"], 1)
        evaluation = next(item for item in traces if item["kind"] == "evaluation")
        self.assertTrue(evaluation["data"]["success"])

    def test_write_action_is_confined_to_workspace(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.store.add_event(
            Event(
                "LOCAL_PLAN",
                {
                    "actions": [
                        {"tool": "write_file", "arguments": {"path": "result.txt", "content": "ok"}}
                    ]
                },
            )
        )
        runtime.run_once()
        self.assertEqual((self.settings.workspace / "result.txt").read_text(encoding="utf-8"), "ok")

    def test_path_escape_is_denied(self) -> None:
        self.settings.ensure_directories()
        kernel = SecurityKernel(self.settings.workspace, self.settings.permissions)
        with self.assertRaises(PermissionDenied):
            kernel.authorize(Action("read_file", {"path": "../secret.txt"}))

    def test_workspace_mount_alias_matches_primitive_workspace_root(self) -> None:
        self.settings.ensure_directories()
        folder = self.settings.workspace / "MathModeling"
        folder.mkdir()
        (folder / "problem.txt").write_text("problem", encoding="utf-8")
        self.settings.permissions.allowed_tools.extend(["read", "write", "edit"])
        kernel = SecurityKernel(self.settings.workspace, self.settings.permissions)
        root_arguments = kernel.authorize(Action("read", {"path": "/workspace"}))
        nested_arguments = kernel.authorize(
            Action("read", {"path": "/workspace/MathModeling/problem.txt"})
        )
        self.assertEqual(Path(root_arguments["path"]), self.settings.workspace.resolve())
        self.assertEqual(Path(nested_arguments["path"]), (folder / "problem.txt").resolve())

        executor = ToolExecutor(ToolRegistry(self.settings.permissions), kernel)
        result = executor.execute(Action("read", {"path": "/workspace"}))
        self.assertTrue(result.ok)
        entries = result.output["resource"]["representations"][0]["entries"]
        self.assertEqual(result.output["resource"]["type"], "directory")
        self.assertIn("MathModeling", {item["name"] for item in entries})
        written = executor.execute(Action("write", {"path": "/workspace/result.txt", "content": "old"}))
        edited = executor.execute(Action("edit", {
            "path": "/workspace/result.txt", "old_text": "old", "new_text": "new",
        }))
        self.assertTrue(written.ok)
        self.assertTrue(edited.ok)
        self.assertEqual((self.settings.workspace / "result.txt").read_text(encoding="utf-8"), "new")

    def test_workspace_mount_alias_does_not_widen_path_authority(self) -> None:
        self.settings.ensure_directories()
        self.settings.permissions.allowed_tools.append("read")
        kernel = SecurityKernel(self.settings.workspace, self.settings.permissions)
        for path in (
            "/workspace/../secret.txt", "/workspace//etc/passwd",
            "/workspace-shadow/file.txt", "/aios-state/state.json",
        ):
            with self.subTest(path=path), self.assertRaises(PermissionDenied):
                kernel.authorize(Action("read", {"path": path}))

    def test_processing_events_can_be_recovered(self) -> None:
        self.settings.ensure_directories()
        store = StateStore(self.settings.database)
        store.initialize()
        store.add_event(Event("TEST"))
        self.assertEqual(len(store.claim_events()), 1)
        self.assertEqual(store.recover_processing_events(), 1)
        self.assertEqual(store.count_pending_events(), 1)

    def test_deepseek_uses_openai_compatible_transport(self) -> None:
        config = self.settings.model
        config.provider = "deepseek"
        controller = LLMController(config)
        expected = Plan("deepseek", [])
        with patch.object(controller, "_remote_plan", return_value=expected) as remote:
            actual = controller.plan(Intent("test", "test", None, []), [])
        self.assertIs(actual, expected)
        remote.assert_called_once_with(ANY, [], {})

    def test_api_key_env_rejects_literal_secret_without_echoing_it(self) -> None:
        config = self.settings.model
        config.provider = "deepseek"
        config.api_key_env = "sk-sensitive-value"
        controller = LLMController(config)
        with self.assertRaises(ControllerError) as caught:
            controller.plan(Intent("test", "test", None, []), [])
        self.assertNotIn("sk-sensitive-value", str(caught.exception))

    def test_plan_parser_accepts_json_code_fence(self) -> None:
        plan = LLMController._parse_plan_content(
            '```json\n{"summary":"ok","actions":[{"tool":"echo","arguments":{"message":"hi"}}]}\n```'
        )
        self.assertEqual(plan.summary, "ok")
        self.assertEqual(plan.actions[0].tool, "echo")

    def test_plan_parser_accepts_explanatory_text_and_blocks(self) -> None:
        plan = LLMController._parse_plan_content(
            [
                {"type": "text", "text": "Here is the plan:\n"},
                {"type": "text", "text": '{"summary":"blocked","actions":[]}'},
                {"type": "text", "text": "\nDone."},
            ]
        )
        self.assertEqual(plan.summary, "blocked")
        self.assertEqual(plan.actions, [])

    def test_plan_parser_rejects_empty_or_unstructured_content(self) -> None:
        for content in (None, "", "no plan available"):
            with self.subTest(content=content):
                with self.assertRaises(ControllerError):
                    LLMController._parse_plan_content(content)

    def test_plan_parser_accepts_python_literal_nested_plan_and_aliases(self) -> None:
        plan = LLMController._parse_plan_content(
            "{'plan': {'summary': 'ok', 'done': True, 'steps': "
            "[{'name': 'echo', 'args': {'message': 'finished'}}]}}"
        )
        self.assertTrue(plan.done)
        self.assertEqual(plan.actions[0].tool, "echo")
        self.assertEqual(plan.actions[0].arguments["message"], "finished")

    def test_plan_parser_accepts_action_array(self) -> None:
        plan = LLMController._parse_plan_content(
            '[{"tool_name":"read_file","args":{"path":"game.py"}}]'
        )
        self.assertFalse(plan.done)
        self.assertEqual(plan.actions[0].tool, "read_file")

    def test_native_tool_calls_become_typed_actions(self) -> None:
        actions = LLMController._parse_tool_calls(
            [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"game.py"}',
                    },
                }
            ]
        )
        self.assertEqual(actions[0].tool, "read_file")
        self.assertEqual(actions[0].arguments, {"path": "game.py"})
        self.assertEqual(actions[0].call_id, "call_123")

    def test_runtime_returns_tool_result_with_matching_call_id(self) -> None:
        runtime = AIOSRuntime(self.settings)
        (self.settings.workspace / "game.py").write_text("print('ok')", encoding="utf-8")
        seen_contexts = []

        def plan(_intent, _goals, context):
            seen_contexts.append(context.copy())
            if len(seen_contexts) == 1:
                native_call = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_read",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"game.py"}'},
                        }
                    ],
                }
                return Plan(
                    "read source",
                    [Action("read_file", {"path": "game.py"}, call_id="call_read")],
                    done=False,
                    protocol_message=native_call,
                )
            return Plan("finished", [Action("echo", {"message": "Reviewed game.py"})], done=True)

        runtime.controller.plan = plan
        runtime.store.add_event(Event("USER_REQUEST", {"message": "review game.py"}))
        runtime.run_once()
        messages = seen_contexts[1]["_protocol_messages"]
        self.assertEqual(messages[0]["role"], "assistant")
        self.assertEqual(messages[1]["role"], "tool")
        self.assertEqual(messages[1]["tool_call_id"], "call_read")
        self.assertIn("print('ok')", messages[1]["content"])

    def test_completed_task_has_result_checkpoint_and_episode(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.store.add_event(Event("USER_REQUEST", {"message": "remember this"}))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(task.result["summary"], "Mock controller acknowledgement")
        phases = [item["phase"] for item in runtime.store.task_checkpoints(int(task.id))]
        self.assertIn("planned_round_1", phases)
        self.assertIn("completed", phases)
        episodes = runtime.store.list_memories(type=MemoryType.EPISODIC)
        self.assertEqual(episodes[0].metadata["task_id"], task.id)

    def test_failed_task_retries_then_moves_to_dead_letter(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.store.add_event(
            Event("LOCAL_PLAN", {"actions": [{"tool": "not_allowed", "arguments": {}}]})
        )
        self.assertTrue(runtime.run_once())
        self.assertEqual(runtime.store.list_tasks()[0].status, TaskStatus.RETRYING)
        self.assertTrue(runtime.run_once())
        self.assertTrue(runtime.run_once())
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.attempts, 3)
        self.assertEqual(task.status, TaskStatus.DEAD_LETTER)
        self.assertEqual(len(runtime.store.list_dead_letters()), 1)

    def test_memory_retrieval_prefers_related_content(self) -> None:
        runtime = AIOSRuntime(self.settings)
        memories = MemoryManager(runtime.store)
        memories.remember(MemoryType.SEMANTIC, "城市绿色物流需要优化车辆路径", importance=0.5)
        memories.remember(MemoryType.SEMANTIC, "蛋糕烘焙温度是180度", importance=0.9)
        selected = memories.retrieve("绿色物流车辆调度", limit=1)
        self.assertIn("物流", selected[0].content)

    def test_evolution_requires_benchmark_and_human_approval(self) -> None:
        runtime = AIOSRuntime(self.settings)
        evolution = EvolutionManager(runtime.store)
        candidate_id = evolution.propose(
            {"prompt_append": "Always verify written artifacts.", "max_actions_per_cycle": 4},
            "Reduce unverified output",
        )
        with self.assertRaises(EvolutionPolicyError):
            evolution.promote(candidate_id, approved=True)
        report = evolution.benchmark(candidate_id)
        self.assertTrue(report["passed"])
        self.assertGreaterEqual(report["candidate"]["score"], report["baseline"]["score"])
        self.assertEqual(report["kind"], "offline_regression_benchmark")
        with self.assertRaises(EvolutionPolicyError):
            evolution.promote(candidate_id, approved=False)
        promoted = evolution.promote(candidate_id, approved=True)
        self.assertEqual(promoted["version"], 2)
        self.assertEqual(runtime.store.active_harness()["settings"]["max_actions_per_cycle"], 4)
        rolled_back = evolution.rollback(1, approved=True)
        self.assertEqual(rolled_back["version"], 1)

    def test_evolution_cannot_modify_security_kernel(self) -> None:
        runtime = AIOSRuntime(self.settings)
        evolution = EvolutionManager(runtime.store)
        with self.assertRaises(EvolutionPolicyError):
            evolution.propose({"allowed_tools": ["shell"]}, "Need shell")

    def test_multiround_loop_observes_reads_writes_and_verifies_artifact(self) -> None:
        runtime = AIOSRuntime(self.settings)
        (self.settings.workspace / "source.txt").write_text("source material", encoding="utf-8")
        runtime.controller.plan = Mock(
            side_effect=[
                Plan("inspect", [Action("list_files", {"path": "."})], done=False),
                Plan("read", [Action("read_file", {"path": "source.txt"})], done=False),
                Plan(
                    "write summary",
                    [Action("write_file", {"path": "summary.md", "content": "# Summary"})],
                    done=True,
                ),
            ]
        )
        runtime.store.add_event(
            Event("USER_REQUEST", {"message": "读取资料并生成summary\\.md"})
        )
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(task.result["evidence"]["model_rounds"], 3)
        self.assertTrue(task.result["evidence"]["verification"]["passed"])
        self.assertTrue(task.result["final_output"].endswith("summary.md"))
        self.assertEqual(
            (self.settings.workspace / "summary.md").read_text(encoding="utf-8"), "# Summary"
        )

    def test_observation_only_plan_is_not_task_completion(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.controller.plan = Mock(
            return_value=Plan("listed files", [Action("list_files", {"path": "."})], done=True)
        )
        runtime.store.add_event(Event("USER_REQUEST", {"message": "生成summary.md"}))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.RETRYING)
        checks = task.result["evidence"]["verification"]["checks"]
        failed = {check["name"] for check in checks if not check["passed"]}
        self.assertIn("task_declared_done", failed)
        self.assertIn("requested_artifact_created", failed)

    def test_verifier_only_treats_files_after_generation_verb_as_outputs(self) -> None:
        expected = Verifier._expected_artifacts(
            "完整读取game.py并分析，然后生成code_review\\.md。"
        )
        self.assertEqual(expected, ["code_review.md"])

    def test_successful_artifact_write_can_finish_on_last_model_round(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.controller.plan = Mock(
            side_effect=[
                Plan("inspect", [Action("read_file", {"path": "game.py"})], done=False),
                Plan(
                    "write",
                    [Action("write_file", {"path": "review.md", "content": "done"})],
                    done=False,
                ),
            ]
        )
        runtime.settings.budget.max_model_calls_per_cycle = 2
        (runtime.settings.workspace / "game.py").write_text("print('x')", encoding="utf-8")
        runtime.store.add_event(
            Event("USER_REQUEST", {"message": "读取game.py并生成review.md"})
        )
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertTrue((runtime.settings.workspace / "review.md").exists())

    def test_budget_reserves_a_completion_call_instead_of_silently_dropping_goal(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.settings.max_actions_per_cycle = 3
        runtime.settings.budget.max_tool_calls_per_cycle = 3
        runtime.settings.budget.max_model_calls_per_cycle = 2
        runtime.controller.plan = Mock(
            side_effect=[
                Plan(
                    "inspect broadly",
                    [
                        Action("read_file", {"path": "a.txt"}),
                        Action("read_file", {"path": "b.txt"}),
                        Action("read_file", {"path": "c.txt"}),
                    ],
                    done=False,
                ),
                Plan(
                    "converge",
                    [Action("write_file", {"path": "report.md", "content": "# Report"})],
                    done=False,
                ),
            ]
        )
        for name in ("a.txt", "b.txt", "c.txt"):
            (runtime.settings.workspace / name).write_text(name, encoding="utf-8")
        runtime.store.add_event(Event("USER_REQUEST", {"message": "读取资料并生成report.md"}))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(task.result["evidence"]["executed_actions"], 3)
        self.assertEqual(task.result["evidence"]["budget_deferred_actions"], 1)
        self.assertTrue((runtime.settings.workspace / "report.md").is_file())

    def test_generated_plugin_policy_rejects_network_and_executes_read_only_query(self) -> None:
        runtime = AIOSRuntime(self.settings)
        manager = PluginManager(self.settings.extensions, runtime.store, self.settings.workspace)
        manifest = state_query_manifest("query_tasks", "tasks", "Query tasks")
        unsafe = json.loads(json.dumps(manifest))
        unsafe["permissions"]["network"] = True
        with self.assertRaises(PluginValidationError):
            manager.validate(unsafe)
        smoke = manager.smoke_test(manifest)
        self.assertTrue(smoke["passed"])

    def test_v05_preflight_blocks_state_task_without_sandbox_and_freezes_tool_evolution(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.capabilities = CapabilityRegistry.default(sandbox_available=False, network_enabled=False)
        runtime.controller.plan = Mock(
            return_value=Plan("inspection only", [Action("list_files", {"path": "."})], done=True)
        )
        runtime.store.add_event(
            Event(
                "USER_REQUEST",
                {
                    "message": (
                        "分析最近失败的任务和Trace并生成capability_gap_report.md"
                    )
                },
            )
        )
        runtime.run_once()
        self.assertEqual(runtime.store.list_tasks()[0].status, TaskStatus.BLOCKED_CAPABILITY)
        runtime.run_once()
        active = {plugin.name for plugin in runtime.plugins.active_plugins()}
        schemas = {item["function"]["name"] for item in runtime.controller.tool_schemas}
        self.assertTrue(schemas <= {"read", "write", "edit", "bash"})
        self.assertTrue({"read", "write"} <= schemas)
        runs = runtime.store.list_evolution_runs()
        self.assertEqual(runs, [])
        self.assertEqual(runtime.store.list_memories(), [])


if __name__ == "__main__":
    unittest.main()
