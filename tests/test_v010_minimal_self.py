from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aios.config import PermissionConfig, SandboxConfig, SelfModificationConfig, Settings
from aios.controller import LLMController
from aios.runtime import AIOSRuntime
from aios.sandbox import DockerSandboxBroker
from aios.security import PermissionDenied, SecurityKernel
from aios.self_versioning import SelfVersionManager
from aios.tools import ToolExecutor, ToolRegistry
from aios.types import Action, Event, Plan, TaskStatus


class MinimalSelfModificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = SelfModificationConfig(enabled=True, root=str(self.root / "self"))
        self.manager = SelfVersionManager(self.root / "self", self.config)
        self.manager.initialize()
        self.manager.begin_task(7)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_evolve_forks_without_reasoner_and_preserves_parent(self) -> None:
        parent = self.manager.current_path
        original = (parent / "SYSTEM.md").read_text(encoding="utf-8")

        opened = self.manager.open(reason="ordinary-task observation")
        (self.manager.current_path / "SYSTEM.md").write_text("changed", encoding="utf-8")

        self.assertEqual(opened["model_calls_started"], 0)
        self.assertEqual(opened["parent_version"], "v000001")
        self.assertEqual(self.manager.current_version(), "v000002")
        self.assertEqual((parent / "SYSTEM.md").read_text(encoding="utf-8"), original)
        self.assertEqual(self.manager.system_prompt(), "changed")

    def test_self_is_read_only_until_evolve_then_primitive_tools_can_edit_it(self) -> None:
        permissions = PermissionConfig(
            allowed_tools=["read", "write", "edit", "bash", "evolve"]
        )
        kernel = SecurityKernel(self.root / "workspace", permissions, self.manager)
        registry = ToolRegistry(permissions, self_versions=self.manager)
        executor = ToolExecutor(registry, kernel)

        initial_read = executor.execute(Action("read", {"path": "/self/SYSTEM.md"}))
        denied_write = executor.execute(Action("write", {
            "path": "/self/components/early.py", "content": "bad\n",
        }))
        self.assertTrue(initial_read.ok)
        self.assertFalse(denied_write.ok)
        opened = executor.execute(Action("evolve", {"reason": "need mutable self"}))
        written = executor.execute(Action("write", {
            "path": "/self/components/helper.py", "content": "VALUE = 1\n",
        }))
        read_back = executor.execute(Action("read", {"path": "/self/components/helper.py"}))

        self.assertTrue(opened.ok)
        self.assertTrue(written.ok)
        self.assertTrue(read_back.ok)
        self.assertIn("VALUE = 1", str(read_back.output))

    def test_self_path_cannot_escape_and_history_is_not_a_primitive_write_root(self) -> None:
        self.manager.open()
        permissions = PermissionConfig(allowed_tools=["write", "evolve"])
        kernel = SecurityKernel(self.root / "workspace", permissions, self.manager)
        for value in ("/self/../versions/v000001/SYSTEM.md", "/self-history/v000001/SYSTEM.md"):
            with self.subTest(value=value), self.assertRaises(PermissionDenied):
                kernel.authorize(Action("write", {"path": value, "content": "bad"}))

    def test_tool_surface_and_natural_prompt_are_minimal(self) -> None:
        permissions = PermissionConfig(
            allowed_tools=["read", "write", "edit", "bash", "evolve"]
        )
        schemas = ToolRegistry(permissions, self_versions=self.manager).schemas()
        self.assertEqual(
            [item["function"]["name"] for item in schemas],
            ["read", "write", "edit", "bash", "evolve"],
        )
        prompt = LLMController._tool_system_prompt({
            "harness": {"harness_profile": "minimal_self"}
        })
        appendix = LLMController._prompt_append({
            "self_system_prompt": "# Self\nMap only.",
            "self_experiment_condition": "natural",
        })
        self.assertIn("capability, not a requirement", prompt)
        self.assertNotIn("POSITIVE CONTROL", appendix)
        self.assertIn("Map only", appendix)

    def test_positive_control_is_explicit_and_separate(self) -> None:
        appendix = LLMController._prompt_append({
            "self_experiment_condition": "positive_control",
        })
        self.assertIn("POSITIVE CONTROL", appendix)

    def test_bash_mounts_only_current_self_and_read_only_history_after_evolve(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        broker = DockerSandboxBroker(
            self.root / "sandbox", SandboxConfig(), self_versions=self.manager,
        )
        with patch.object(broker, "available", return_value=True):
            broker.prepare(7, workspace)
            with patch("aios.sandbox.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout="ok", stderr="",
            )) as read_only_run:
                broker.run("true")
            read_only_args = " ".join(str(item) for item in read_only_run.call_args.args[0])
            self.assertIn("dst=/self,readonly", read_only_args)
            self.manager.open()
            with patch("aios.sandbox.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout="ok", stderr="",
            )) as run:
                broker.run("true")
        args = run.call_args.args[0]
        joined = " ".join(str(item) for item in args)
        self.assertIn("dst=/self", joined)
        self.assertIn("dst=/self-history,readonly", joined)

    def test_settings_require_free_runtime_and_known_experiment_condition(self) -> None:
        path = self.root / "config.json"
        path.write_text(json.dumps({
            "runtime": {"completion_mode": "verified"},
            "self_modification": {"enabled": True},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "requires runtime"):
            Settings.load(path)
        path.write_text(json.dumps({
            "runtime": {"completion_mode": "free"},
            "self_modification": {"enabled": True, "experiment_condition": "unknown"},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "experiment_condition"):
            Settings.load(path)

    def test_same_runtime_agent_continues_with_changed_system_prompt(self) -> None:
        config_path = self.root / "runtime.json"
        config_path.write_text(json.dumps({
            "database": "./data/test.db",
            "workspace": "./workspace",
            "runtime": {"completion_mode": "free"},
            "self_modification": {
                "enabled": True, "root": "./agent-self",
                "experiment_condition": "natural",
            },
            "skills": {"enabled": False, "bootstrap_builtins": False},
            "model": {"provider": "mock"},
            "permissions": {
                "allowed_tools": ["read", "write", "edit", "bash", "evolve"],
            },
        }), encoding="utf-8")
        settings = Settings.load(config_path)
        with patch("aios.sandbox.DockerSandboxBroker._probe_health", return_value=True):
            runtime = AIOSRuntime(settings)
            old_system = runtime.self_versions.system_prompt()
            plans = [
                Plan("open self", [Action("evolve", {"reason": "ordinary task"}, call_id="c1")]),
                Plan("change self", [Action("write", {
                    "path": "/self/SYSTEM.md", "content": "# Self\nPersistent changed behavior.\n",
                }, call_id="c2")]),
                Plan("stop", [], done=True),
            ]
            runtime.controller.plan = Mock(side_effect=plans)
            runtime.store.add_event(Event("TASK_REQUEST", {"message": "ordinary task"}))
            runtime.run_once()

        task = runtime.store.list_tasks(1)[0]
        self.assertEqual(task.status, TaskStatus.STOPPED)
        self.assertNotEqual(runtime.self_versions.system_prompt(), old_system)
        self.assertIn(
            "Persistent changed behavior",
            runtime.controller.plan.call_args_list[2].args[2]["self_system_prompt"],
        )
        self.assertEqual(runtime.self_versions.current_version(), "v000002")


if __name__ == "__main__":
    unittest.main()
