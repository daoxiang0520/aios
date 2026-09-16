from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aios.config import PermissionConfig, SandboxConfig, SelfModificationConfig, Settings
from aios.controller import ControllerError, LLMController
from aios.runtime import AIOSRuntime
from aios.sandbox import DockerSandboxBroker
from aios.security import PermissionDenied, SecurityKernel
from aios.self_versioning import SelfModificationError, SelfVersionManager
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
        (self.manager.self_path / "SYSTEM.md").write_text("changed", encoding="utf-8")

        self.assertEqual(opened["model_calls_started"], 0)
        self.assertEqual(opened["parent_version"], "v000001")
        self.assertEqual(self.manager.current_version(), "v000001")
        self.assertEqual((parent / "SYSTEM.md").read_text(encoding="utf-8"), original)
        self.assertEqual(self.manager.system_prompt(), original)
        committed = self.manager.commit()
        self.assertTrue(committed["restart_required"])
        self.assertEqual(self.manager.current_version(), "v000002")
        self.assertEqual(self.manager.system_prompt(), "changed")

    def test_open_self_draft_survives_process_restart_and_can_be_committed(self) -> None:
        opened = self.manager.open(reason="cross-cycle edit")
        (self.manager.self_path / "SYSTEM.md").write_text(
            "persisted draft\n", encoding="utf-8",
        )

        restarted = SelfVersionManager(self.root / "self", self.config)
        restarted.initialize()
        restarted.begin_task(7)

        self.assertTrue(restarted.writable)
        self.assertEqual(restarted.self_path.name, opened["version"])
        self.assertEqual(
            (restarted.self_path / "SYSTEM.md").read_text(encoding="utf-8"),
            "persisted draft\n",
        )
        committed = restarted.commit()
        self.assertEqual(committed["version"], opened["version"])
        self.assertEqual(restarted.current_version(), opened["version"])
        self.assertFalse(restarted.open_draft_file.exists())

    def test_open_self_draft_remains_owned_by_original_task(self) -> None:
        opened = self.manager.open(reason="owned draft")
        restarted = SelfVersionManager(self.root / "self", self.config)
        restarted.initialize()
        restarted.begin_task(8)

        self.assertFalse(restarted.writable)
        with self.assertRaisesRegex(SelfModificationError, "owned by task 7"):
            restarted.open(reason="conflicting task")

        restarted.begin_task(7)
        self.assertTrue(restarted.writable)
        self.assertEqual(restarted.self_path.name, opened["version"])

    def test_legacy_open_draft_without_pointer_is_recovered_from_history(self) -> None:
        opened = self.manager.open(reason="legacy recovery")
        self.manager.open_draft_file.unlink()

        restarted = SelfVersionManager(self.root / "self", self.config)
        restarted.initialize()
        restarted.begin_task(7)

        self.assertTrue(restarted.writable)
        self.assertEqual(restarted.self_path.name, opened["version"])
        self.assertTrue(restarted.open_draft_file.is_file())

    def test_recovery_execution_can_fork_recorded_parent_without_rewriting_it(self) -> None:
        self.manager.open(reason="make current descendant")
        (self.manager.self_path / "SYSTEM.md").write_text(
            "# Self\nBroken current behavior.\n", encoding="utf-8",
        )
        self.manager.commit()
        broken = self.manager.current_version()
        parent = self.manager.parent_version(broken)

        self.assertEqual(parent, "v000001")
        self.manager.begin_task(8, execution_version=parent)
        self.assertEqual(self.manager.self_path.name, "v000001")
        opened = self.manager.open(reason="recover from parent")
        self.assertEqual(opened["parent_version"], "v000001")
        (self.manager.self_path / "SYSTEM.md").write_text(
            "# Self\nRecovered descendant.\n", encoding="utf-8",
        )
        committed = self.manager.commit()

        self.assertEqual(committed["parent_version"], "v000001")
        self.assertEqual(self.manager.current_version(), "v000003")
        self.assertIn("Recovered descendant", self.manager.system_prompt())
        self.assertIn("Broken current behavior", (
            self.manager.versions / broken / "SYSTEM.md"
        ).read_text(encoding="utf-8"))

    def test_fresh_self_owns_a_generic_agent_entrypoint_and_persistent_goal(self) -> None:
        architecture = self.manager.architecture()
        self.assertEqual(architecture["kind"], "agent")
        self.assertEqual(architecture["entrypoint"], "agent/main.py")
        self.assertTrue((self.manager.current_path / "agent" / "main.py").is_file())
        self.assertIn("maintaining the reusable system", self.manager.self_goal())

    def test_explicit_agent_migration_creates_descendant_without_modifying_parent(self) -> None:
        parent = self.manager.current_path
        (parent / "agent" / "main.py").unlink()
        self.assertEqual(self.manager.architecture()["kind"], "legacy_harness")

        result = self.manager.migrate_agent_architecture()

        self.assertTrue(result["changed"])
        self.assertEqual(result["previous_version"], "v000001")
        self.assertEqual(result["current_version"], "v000002")
        self.assertEqual(result["created_by"], "human_confirmed_host_migration")
        self.assertFalse((parent / "agent" / "main.py").exists())
        self.assertTrue((self.manager.current_path / "agent" / "main.py").is_file())
        self.assertIn("Mutable Agent architecture", self.manager.system_prompt())

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

    def test_minimal_self_projection_bounds_raw_action_results(self) -> None:
        context = {
            "observations": [{
                "round": 3,
                "action": {"tool": "bash", "arguments": {"command": "generate"}},
                "result": {"ok": True, "output": "x" * 5_700_000, "error": None},
            }],
            "_protocol_messages": [],
        }

        projected = AIOSRuntime._harness_context_projection(
            context, {"harness_profile": "minimal_self"},
        )

        self.assertLess(len(json.dumps(projected)), 10_000)
        self.assertEqual(len(projected["observations"]), 1)
        self.assertLessEqual(len(projected["observations"][0]["output_summary"]), 2_000)

    def test_mutable_tool_policy_can_route_but_not_break_protocol_cardinality(self) -> None:
        original = [Action("read", {"path": "a.txt"}, call_id="call-1")]
        routed = AIOSRuntime._self_harness_actions(original, [
            {"tool": "write", "arguments": {"path": "b.txt", "content": "x"}},
        ])
        self.assertEqual(routed[0].tool, "write")
        self.assertEqual(routed[0].call_id, "call-1")
        with self.assertRaisesRegex(RuntimeError, "exactly one action"):
            AIOSRuntime._self_harness_actions(original, [])

    def test_self_agent_can_author_bounded_additional_actions(self) -> None:
        original = [Action("read", {"path": "a.txt"}, call_id="call-1")]
        selected = [{"tool": "read", "arguments": {"path": "a.txt"}}]
        actions = AIOSRuntime._self_harness_actions(
            original,
            selected,
            [{"tool": "write", "arguments": {"path": "note.txt", "content": "x"}}],
            max_autonomous_actions=1,
        )
        self.assertEqual([item.tool for item in actions], ["read", "write"])
        self.assertEqual(actions[0].call_id, "call-1")
        self.assertIsNone(actions[1].call_id)
        self.assertIn("self-agent", actions[1].reason)
        with self.assertRaisesRegex(RuntimeError, "hard per-round ceiling"):
            AIOSRuntime._self_harness_actions(
                original, selected,
                [{"tool": "read", "arguments": {}}, {"tool": "read", "arguments": {}}],
                max_autonomous_actions=1,
            )

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

    def test_mutable_harness_executes_in_dedicated_host_isolated_container(self) -> None:
        broker = DockerSandboxBroker(
            self.root / "sandbox", SandboxConfig(), self_versions=self.manager,
        )

        def completed(args, **_kwargs):
            mount = next(
                item for item in args
                if isinstance(item, str) and "dst=/exchange" in item
            )
            source = mount.split("src=", 1)[1].split(",dst=", 1)[0]
            Path(source, "output.json").write_text(
                json.dumps({"context": {"selected": True}}), encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(broker, "available", return_value=True), patch(
            "aios.sandbox.subprocess.run", side_effect=completed,
        ) as run:
            result = broker.run_self_harness(
                "v000001", "before_model", {"context": {}, "state": {}},
            )
        args = run.call_args.args[0]
        joined = " ".join(str(item) for item in args)
        self.assertEqual(result["output"]["context"], {"selected": True})
        self.assertEqual(result["runtime_kind"], "agent")
        self.assertEqual(result["entrypoint"], "agent/main.py")
        self.assertIn("--network none", joined)
        self.assertIn("dst=/self,readonly", joined)
        self.assertIn("/self/agent/main.py", joined)
        self.assertNotIn("dst=/workspace", joined)

    def test_host_does_not_silently_repair_a_committed_broken_harness(self) -> None:
        self.manager.open()
        (self.manager.self_path / "harness" / "runner.py").unlink()
        self.manager.commit()

        restarted = SelfVersionManager(self.root / "self", self.config)
        restarted.initialize()

        self.assertEqual(restarted.current_version(), "v000002")
        self.assertFalse((restarted.current_path / "harness" / "runner.py").exists())

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
        path.write_text(json.dumps({
            "runtime": {"completion_mode": "free"},
            "self_modification": {
                "enabled": True, "in_task_reflection_rounds": [12, 0],
            },
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "positive integers"):
            Settings.load(path)
        path.write_text(json.dumps({
            "runtime": {"completion_mode": "free"},
            "self_modification": {
                "enabled": True, "in_task_reflection_rounds": [36, 12, 12],
            },
        }), encoding="utf-8")
        self.assertEqual(
            Settings.load(path).self_modification.in_task_reflection_rounds,
            [12, 36],
        )
        path.write_text(json.dumps({
            "runtime": {"completion_mode": "free"},
            "self_modification": {
                "enabled": True, "max_failure_recovery_invocations": -1,
            },
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            Settings.load(path)
        path.write_text(json.dumps({
            "runtime": {"completion_mode": "free"},
            "self_modification": {
                "enabled": True, "reflection_cooldown_checkpoints": -1,
            },
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "reflection_cooldown_checkpoints"):
            Settings.load(path)

    def test_terminal_failure_starts_same_lineage_recovery_without_new_attempt(self) -> None:
        config_path = self.root / "self-recovery.json"
        config_path.write_text(json.dumps({
            "database": "./data/self-recovery.db",
            "workspace": "./workspace-self-recovery",
            "runtime": {"completion_mode": "free"},
            "self_modification": {
                "enabled": True,
                "root": "./agent-self-recovery",
                "failure_recovery_enabled": True,
                "max_failure_recovery_invocations": 1,
            },
            "budget": {"enabled": False},
            "skills": {"enabled": False, "bootstrap_builtins": False},
            "model": {"provider": "mock"},
            "permissions": {
                "allowed_tools": ["read", "write", "edit", "bash", "observe", "evolve"],
            },
        }), encoding="utf-8")
        settings = Settings.load(config_path)
        settings.workspace.mkdir(parents=True)
        seen_recovery: list[dict[str, object]] = []

        with patch("aios.sandbox.DockerSandboxBroker._probe_health", return_value=True):
            runtime = AIOSRuntime(settings)

            def plan(_intent, _goals, context):
                call = runtime.controller.plan.call_count
                if call == 1:
                    raise ControllerError("Model protocol repair failed: test incident")
                recovery = context.get("self_recovery")
                self.assertIsInstance(recovery, dict)
                seen_recovery.append(dict(recovery))
                return Plan(
                    "original user task is now addressed", [], done=True,
                    reasoning="Retry with known facts; api_key=secret-value",
                )

            runtime.controller.plan = Mock(side_effect=plan)

            def harness(version, stage, payload, **_kwargs):
                if stage == "before_model":
                    output = {"context": payload["context"]}
                elif stage == "after_plan":
                    output = {"actions": payload["actions"]}
                else:
                    output = {
                        "loop": {"allow_another_round": True},
                        "continuation": {"checkpoint": False},
                    }
                return {
                    "output": output, "version": version, "stage": stage,
                    "runtime_kind": "agent", "entrypoint": "agent/main.py",
                }

            runtime.sandbox.run_self_harness = Mock(side_effect=harness)
            runtime.store.add_event(Event(
                "TASK_REQUEST", {"message": "ordinary task"}, priority=90,
            ))
            runtime.run_once()
            scheduled = runtime.store.list_tasks(1)[0]
            self.assertEqual(scheduled.status, TaskStatus.RETRYING)
            self.assertEqual(scheduled.attempts, 1)
            runtime.run_once()

        task = runtime.store.list_tasks(1)[0]
        self.assertEqual(task.status, TaskStatus.STOPPED)
        self.assertEqual(task.attempts, 1)
        self.assertTrue(seen_recovery)
        self.assertEqual(seen_recovery[0]["host_diagnosis"], None)
        self.assertFalse(seen_recovery[0]["mutation_required"])
        self.assertTrue(task.result["self_modification"]["recovery_invocation"])
        trace_kinds = {
            item["kind"] for item in runtime.store.recent_traces(limit=500)
        }
        self.assertTrue({
            "self_recovery_incident", "self_recovery_scheduled",
            "self_recovery_started", "self_recovery_resolved", "model_reasoning",
        } <= trace_kinds)
        reasoning_trace = next(
            item for item in runtime.store.recent_traces(limit=500)
            if item["kind"] == "model_reasoning"
        )
        self.assertIn("<redacted>", reasoning_trace["data"]["reasoning"])
        self.assertNotIn("secret-value", reasoning_trace["data"]["reasoning"])

    def test_failed_self_recovery_is_terminal_and_never_recursively_scheduled(self) -> None:
        config_path = self.root / "bounded-self-recovery.json"
        config_path.write_text(json.dumps({
            "database": "./data/bounded-self-recovery.db",
            "workspace": "./workspace-bounded-self-recovery",
            "runtime": {"completion_mode": "free"},
            "self_modification": {
                "enabled": True,
                "root": "./agent-bounded-self-recovery",
                "failure_recovery_enabled": True,
                "max_failure_recovery_invocations": 1,
            },
            "budget": {"enabled": False},
            "skills": {"enabled": False, "bootstrap_builtins": False},
            "model": {"provider": "mock"},
            "permissions": {
                "allowed_tools": ["read", "write", "edit", "bash", "evolve"],
            },
        }), encoding="utf-8")
        settings = Settings.load(config_path)
        settings.workspace.mkdir(parents=True)

        with patch("aios.sandbox.DockerSandboxBroker._probe_health", return_value=True):
            runtime = AIOSRuntime(settings)
            runtime.controller.plan = Mock(side_effect=ControllerError(
                "Model protocol repair failed: persistent test incident"
            ))
            def harness(version, stage, payload, **_kwargs):
                if stage == "before_model":
                    output = {"context": payload["context"]}
                elif stage == "after_plan":
                    output = {"actions": payload["actions"]}
                else:
                    output = {
                        "loop": {"allow_another_round": True},
                        "continuation": {"checkpoint": False},
                    }
                return {
                    "output": output, "version": version, "stage": stage,
                    "runtime_kind": "agent", "entrypoint": "agent/main.py",
                }
            runtime.sandbox.run_self_harness = Mock(side_effect=harness)
            runtime.store.add_event(Event(
                "TASK_REQUEST", {"message": "ordinary task"}, priority=90,
            ))
            runtime.run_once()
            runtime.run_once()

        task = runtime.store.list_tasks(1)[0]
        self.assertEqual(task.status, TaskStatus.ABANDONED)
        self.assertEqual(task.attempts, 1)
        checkpoints = runtime.store.task_checkpoints(int(task.id))
        self.assertEqual(
            sum(item["phase"] == "self_recovery_scheduled" for item in checkpoints),
            1,
        )
        trace_kinds = [
            item["kind"] for item in runtime.store.recent_traces(limit=500)
        ]
        self.assertEqual(trace_kinds.count("self_recovery_scheduled"), 1)
        self.assertEqual(trace_kinds.count("self_recovery_failed"), 1)
        self.assertEqual(trace_kinds.count("task_abandoned"), 1)

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
                "allowed_tools": ["read", "write", "edit", "bash", "observe", "evolve"],
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
                Plan("commit self", [Action("evolve", {"operation": "commit"}, call_id="c3")]),
                Plan("stop", [], done=True),
            ]
            runtime.controller.plan = Mock(side_effect=plans)
            def harness(version, stage, payload, **_kwargs):
                if stage == "before_model":
                    output = {"context": payload["context"]}
                elif stage == "after_plan":
                    output = {"actions": payload["actions"]}
                else:
                    output = {
                        "loop": {"allow_another_round": True},
                        "continuation": {"checkpoint": False},
                    }
                return {"output": output, "version": version, "stage": stage}
            runtime.sandbox.run_self_harness = Mock(side_effect=harness)
            runtime.store.add_event(Event("TASK_REQUEST", {"message": "ordinary task"}))
            runtime.run_once()
            deferred = runtime.store.list_tasks(1)[0]
            self.assertEqual(deferred.status, TaskStatus.DEFERRED)
            self.assertEqual(deferred.result["continuation_reason"], "self_version_commit")
            self.assertFalse(deferred.result["evidence"]["budget_deferred"])
            runtime.run_once()

        task = runtime.store.list_tasks(1)[0]
        self.assertEqual(task.status, TaskStatus.STOPPED)
        self.assertNotEqual(runtime.self_versions.system_prompt(), old_system)
        first_context = runtime.controller.plan.call_args_list[0].args[2]
        self.assertEqual(
            first_context["self_observation"]["schema"],
            "agent_observation_entrypoint/v1",
        )
        self.assertIsNone(first_context["self_observation"]["host_interpretation"])
        self.assertNotIn("self_experience", first_context)
        self.assertNotIn("task_progress", first_context)
        self.assertIn("Persistent Self objective", first_context["self_goal"])
        self.assertEqual(
            first_context["self_state"]["architecture"]["entrypoint"],
            "agent/main.py",
        )
        self.assertEqual(first_context["self_observation"]["tool"], "observe")
        self.assertIn(
            "Persistent changed behavior",
            runtime.controller.plan.call_args_list[3].args[2]["self_system_prompt"],
        )
        self.assertEqual(runtime.self_versions.current_version(), "v000002")

    def test_agent_gets_one_bounded_post_answer_self_reflection(self) -> None:
        config_path = self.root / "reflection.json"
        config_path.write_text(json.dumps({
            "database": "./data/reflection.db",
            "workspace": "./workspace-reflection",
            "runtime": {"completion_mode": "free"},
            "self_modification": {
                "enabled": True, "root": "./agent-self-reflection",
                "experiment_condition": "natural",
            },
            "skills": {"enabled": False, "bootstrap_builtins": False},
            "model": {"provider": "mock"},
            "permissions": {
                "allowed_tools": ["read", "write", "edit", "bash", "observe", "evolve"],
            },
        }), encoding="utf-8")
        settings = Settings.load(config_path)
        with patch("aios.sandbox.DockerSandboxBroker._probe_health", return_value=True):
            runtime = AIOSRuntime(settings)
            runtime.controller.plan = Mock(side_effect=[
                Plan("ordinary answer", [], done=True),
                Plan("ordinary answer; Self left unchanged", [], done=True),
            ])

            def harness(version, stage, payload, **_kwargs):
                if stage == "before_model":
                    output = {"context": payload["context"]}
                elif stage == "after_plan":
                    output = {"actions": payload["actions"]}
                else:
                    output = {
                        "loop": {"allow_another_round": True},
                        "continuation": {"checkpoint": False},
                    }
                return {"output": output, "version": version, "stage": stage}

            runtime.sandbox.run_self_harness = Mock(side_effect=harness)
            runtime.store.add_event(Event("TASK_REQUEST", {"message": "ordinary task"}))
            runtime.run_once()

        task = runtime.store.list_tasks(1)[0]
        self.assertEqual(task.status, TaskStatus.STOPPED)
        self.assertEqual(runtime.controller.plan.call_count, 2)
        first_context = runtime.controller.plan.call_args_list[0].args[2]
        reflection_context = runtime.controller.plan.call_args_list[1].args[2]
        self.assertNotIn("self_reflection", first_context)
        self.assertEqual(
            reflection_context["self_reflection"]["decision_owner"], "ordinary_agent",
        )
        self.assertFalse(reflection_context["self_reflection"]["mutation_required"])
        self.assertTrue(task.result["self_modification"]["reflection_offered"])
        self.assertFalse(task.result["self_modification"]["evolution_observed"])
        self.assertEqual(runtime.self_versions.current_version(), "v000001")

    def test_active_task_gets_one_turn_reflection_and_can_commit_a_descendant(self) -> None:
        config_path = self.root / "in-task-reflection.json"
        config_path.write_text(json.dumps({
            "database": "./data/in-task-reflection.db",
            "workspace": "./workspace-in-task-reflection",
            "runtime": {"completion_mode": "free"},
            "self_modification": {
                "enabled": True,
                "root": "./agent-self-in-task-reflection",
                "experiment_condition": "natural",
                "in_task_reflection_rounds": [12],
            },
            "budget": {"enabled": False},
            "skills": {"enabled": False, "bootstrap_builtins": False},
            "model": {"provider": "mock"},
            "permissions": {
                "allowed_tools": ["read", "write", "edit", "bash", "evolve"],
            },
        }), encoding="utf-8")
        settings = Settings.load(config_path)
        settings.workspace.mkdir(parents=True)
        (settings.workspace / "evidence.txt").write_text("evidence\n", encoding="utf-8")
        seen_reflections: list[dict[str, object]] = []

        with patch("aios.sandbox.DockerSandboxBroker._probe_health", return_value=True):
            runtime = AIOSRuntime(settings)

            def plan(_intent, _goals, context):
                call = runtime.controller.plan.call_count
                reflection = context.get("self_reflection")
                if isinstance(reflection, dict):
                    seen_reflections.append(dict(reflection))
                if call <= 11:
                    self.assertIsNone(reflection)
                    return Plan(
                        f"inspect {call}",
                        [Action("read", {"path": "evidence.txt"}, call_id=f"c{call}")],
                    )
                if call == 12:
                    self.assertEqual(reflection["trigger"], "in_task_checkpoint")
                    self.assertFalse(reflection["mutation_required"])
                    return Plan("open reusable change", [
                        Action("evolve", {"reason": "observed reusable task behavior"}, call_id="e1"),
                    ])
                if call == 13:
                    self.assertIsNone(reflection)
                    return Plan("edit descendant", [Action("write", {
                        "path": "/self/SYSTEM.md",
                        "content": "# Self\nIn-task reflection-authored behavior.\n",
                    }, call_id="e2")])
                if call == 14:
                    self.assertIsNone(reflection)
                    return Plan("commit descendant", [
                        Action("evolve", {"operation": "commit"}, call_id="e3"),
                    ])
                self.assertIsNone(reflection)
                return Plan("ordinary answer after descendant restart", [], done=True)

            runtime.controller.plan = Mock(side_effect=plan)

            def harness(version, stage, payload, **_kwargs):
                if stage == "before_model":
                    output = {"context": payload["context"]}
                elif stage == "after_plan":
                    output = {"actions": payload["actions"]}
                else:
                    output = {
                        "loop": {"allow_another_round": True},
                        "continuation": {"checkpoint": False},
                    }
                return {"output": output, "version": version, "stage": stage}

            runtime.sandbox.run_self_harness = Mock(side_effect=harness)
            runtime.store.add_event(Event("TASK_REQUEST", {"message": "long ordinary task"}))
            runtime.run_once()
            deferred = runtime.store.list_tasks(1)[0]
            self.assertEqual(deferred.status, TaskStatus.DEFERRED)
            self.assertEqual(deferred.result["continuation_reason"], "self_version_commit")
            runtime.run_once()

        task = runtime.store.list_tasks(1)[0]
        self.assertEqual(task.status, TaskStatus.STOPPED)
        self.assertEqual(len(seen_reflections), 1)
        self.assertEqual(seen_reflections[0]["trigger"], "in_task_checkpoint")
        self.assertEqual(runtime.self_versions.current_version(), "v000002")
        self.assertIn("In-task reflection-authored", runtime.self_versions.system_prompt())
        self.assertEqual(task.result["self_modification"]["in_task_reflection_rounds"], [12])
        self.assertTrue(task.result["self_modification"]["evolution_observed"])
        traces = runtime.store.recent_traces(limit=500)
        self.assertTrue(any(
            item["kind"] == "self_reflection_offered"
            and item["data"].get("trigger") == "in_task_checkpoint"
            for item in traces
        ))

    def test_liveness_continuation_gets_dedicated_self_decision_without_stopping_task(self) -> None:
        config_path = self.root / "liveness-self-decision.json"
        config_path.write_text(json.dumps({
            "database": "./data/liveness-self-decision.db",
            "workspace": "./workspace-liveness-self-decision",
            "runtime": {
                "completion_mode": "free", "liveness_checkpoint_rounds": 2,
            },
            "self_modification": {
                "enabled": True, "root": "./agent-self-liveness-decision",
                "experiment_condition": "natural", "in_task_reflection_rounds": [],
            },
            "budget": {"enabled": False},
            "skills": {"enabled": False, "bootstrap_builtins": False},
            "model": {"provider": "mock"},
            "permissions": {
                "allowed_tools": ["read", "write", "edit", "bash", "observe", "evolve"],
            },
        }), encoding="utf-8")
        settings = Settings.load(config_path)
        settings.workspace.mkdir(parents=True)
        (settings.workspace / "evidence.txt").write_text("evidence\n", encoding="utf-8")
        seen: list[dict[str, object] | None] = []

        with patch("aios.sandbox.DockerSandboxBroker._probe_health", return_value=True):
            runtime = AIOSRuntime(settings)

            def plan(_intent, _goals, context):
                call = runtime.controller.plan.call_count
                reflection = context.get("self_reflection")
                seen.append(dict(reflection) if isinstance(reflection, dict) else None)
                if call <= 2:
                    return Plan("initial work", [
                        Action("read", {"path": "evidence.txt"}, call_id=f"a{call}"),
                    ])
                if call == 3:
                    self.assertEqual(reflection["phase"], "dedicated_self_decision")
                    self.assertNotIn("observed", reflection)
                    self.assertNotIn("self_experience", context)
                    self.assertNotIn("task_progress", context)
                    return Plan("inspect my own immutable record", [
                        Action("observe", {"view": "task_events"}, call_id="o1"),
                    ])
                if call == 4:
                    self.assertEqual(reflection["phase"], "dedicated_self_decision")
                    self.assertEqual(
                        context["observations"][-1]["tool"], "observe",
                    )
                    self.assertEqual(
                        context["observations"][-1]["observation"]["view"],
                        "task_events",
                    )
                    return Plan("SELF_UNCHANGED: insufficient reusable evidence", [], done=True)
                if call == 5:
                    self.assertIsNone(reflection)
                    return Plan("ordinary task complete", [], done=True)
                self.assertEqual(reflection["trigger"], "agent_declared_stop")
                return Plan("ordinary task complete", [], done=True)

            runtime.controller.plan = Mock(side_effect=plan)

            def harness(version, stage, payload, **_kwargs):
                if stage == "before_model":
                    output = {"context": payload["context"]}
                elif stage == "after_plan":
                    output = {"actions": payload["actions"]}
                else:
                    output = {
                        "loop": {"allow_another_round": True},
                        "continuation": {"checkpoint": False},
                    }
                return {"output": output, "version": version, "stage": stage}

            runtime.sandbox.run_self_harness = Mock(side_effect=harness)
            runtime.store.add_event(Event("TASK_REQUEST", {"message": "ordinary task"}))
            runtime.run_once()
            self.assertEqual(runtime.store.list_tasks(1)[0].status, TaskStatus.DEFERRED)
            runtime.run_once()

        task = runtime.store.list_tasks(1)[0]
        self.assertEqual(task.status, TaskStatus.STOPPED)
        self.assertEqual(task.result["summary"], "ordinary task complete")
        self.assertEqual(seen[2]["no_change_signal"], "SELF_UNCHANGED")
        self.assertTrue(any(
            item["kind"] == "action_result"
            and item["data"].get("tool") == "observe"
            and item["data"].get("ok")
            for item in runtime.store.recent_traces(limit=500)
        ))
        resolution = [
            item for item in runtime.store.recent_traces(limit=500)
            if item["kind"] == "self_reflection_resolved"
            and item["data"].get("ordinary_task_resumed")
        ]
        self.assertEqual(len(resolution), 1)
        self.assertFalse(resolution[0]["data"]["self_evolution_observed"])

    def test_dedicated_self_decision_cannot_recursively_starve_ordinary_task(self) -> None:
        config_path = self.root / "bounded-self-decision.json"
        config_path.write_text(json.dumps({
            "database": "./data/bounded-self-decision.db",
            "workspace": "./workspace-bounded-self-decision",
            "runtime": {
                "completion_mode": "free", "liveness_checkpoint_rounds": 2,
            },
            "self_modification": {
                "enabled": True, "root": "./agent-self-bounded-decision",
                "experiment_condition": "natural", "in_task_reflection_rounds": [],
            },
            "budget": {"enabled": False},
            "skills": {"enabled": False, "bootstrap_builtins": False},
            "model": {"provider": "mock"},
            "permissions": {
                "allowed_tools": ["read", "write", "edit", "bash", "observe", "evolve"],
            },
        }), encoding="utf-8")
        settings = Settings.load(config_path)
        settings.workspace.mkdir(parents=True)
        (settings.workspace / "evidence.txt").write_text("evidence\n", encoding="utf-8")
        phases: list[str | None] = []

        with patch("aios.sandbox.DockerSandboxBroker._probe_health", return_value=True):
            runtime = AIOSRuntime(settings)

            def plan(_intent, _goals, context):
                reflection = context.get("self_reflection")
                phases.append(
                    reflection.get("phase") if isinstance(reflection, dict) else None
                )
                call = runtime.controller.plan.call_count
                if call <= 4:
                    return Plan("keep inspecting", [
                        Action("read", {"path": "evidence.txt"}, call_id=f"r{call}"),
                    ])
                return Plan("ordinary work resumed", [], done=True)

            runtime.controller.plan = Mock(side_effect=plan)

            def harness(version, stage, payload, **_kwargs):
                if stage == "before_model":
                    output = {"context": payload["context"]}
                elif stage == "after_plan":
                    output = {"actions": payload["actions"]}
                else:
                    output = {
                        "loop": {"allow_another_round": True},
                        "continuation": {"checkpoint": False},
                    }
                return {"output": output, "version": version, "stage": stage}

            runtime.sandbox.run_self_harness = Mock(side_effect=harness)
            runtime.store.add_event(Event("TASK_REQUEST", {"message": "ordinary task"}))
            runtime.run_once()
            runtime.run_once()
            second = runtime.store.list_tasks(1)[0]
            checkpoint = runtime.store.task_checkpoints(second.id)[-1]["data"]
            self.assertNotIn("_active_self_reflection", checkpoint["working_state"])
            self.assertNotIn("_pending_self_reflection", checkpoint["working_state"])
            runtime.run_once()

        self.assertEqual(
            phases[:5],
            [None, None, "dedicated_self_decision", "dedicated_self_decision", None],
        )
        self.assertTrue(any(
            item["kind"] == "self_reflection_window_exhausted"
            for item in runtime.store.recent_traces(limit=500)
        ))

    def test_long_task_gets_another_self_decision_after_cooldown(self) -> None:
        config_path = self.root / "recurring-self-decision.json"
        config_path.write_text(json.dumps({
            "database": "./data/recurring-self-decision.db",
            "workspace": "./workspace-recurring-self-decision",
            "runtime": {
                "completion_mode": "free", "liveness_checkpoint_rounds": 1,
            },
            "self_modification": {
                "enabled": True, "root": "./agent-self-recurring-decision",
                "experiment_condition": "natural", "in_task_reflection_rounds": [],
                "reflection_cooldown_checkpoints": 2,
            },
            "budget": {"enabled": False},
            "skills": {"enabled": False, "bootstrap_builtins": False},
            "model": {"provider": "mock"},
            "permissions": {
                "allowed_tools": ["read", "write", "edit", "bash", "observe", "evolve"],
            },
        }), encoding="utf-8")
        settings = Settings.load(config_path)
        settings.workspace.mkdir(parents=True)
        (settings.workspace / "evidence.txt").write_text("evidence\n", encoding="utf-8")
        phases: list[str | None] = []
        second_decision_seen = False

        with patch("aios.sandbox.DockerSandboxBroker._probe_health", return_value=True):
            runtime = AIOSRuntime(settings)

            def plan(_intent, _goals, context):
                nonlocal second_decision_seen
                reflection = context.get("self_reflection")
                phase = reflection.get("phase") if isinstance(reflection, dict) else None
                phases.append(phase)
                if phase == "dedicated_self_decision":
                    if runtime.self_versions.writable:
                        marker = runtime.self_versions.self_path / "cooldown-marker.txt"
                        if not marker.exists():
                            return Plan("write reusable change", [Action("write", {
                                "path": "/self/cooldown-marker.txt", "content": "changed\n",
                            }, call_id="write-self")])
                        return Plan("commit reusable change", [Action(
                            "evolve", {"operation": "commit"}, call_id="commit-self",
                        )])
                    if runtime.self_versions.current_version() == "v000001":
                        return Plan("open reusable change", [Action(
                            "evolve", {"reason": "first observed pattern"}, call_id="open-self",
                        )])
                    second_decision_seen = True
                    return Plan("SELF_UNCHANGED: no new reusable evidence", [], done=True)
                if second_decision_seen:
                    return Plan("ordinary task complete", [], done=True)
                return Plan("ordinary work", [Action(
                    "read", {"path": "evidence.txt"}, call_id=f"read-{len(phases)}",
                )])

            runtime.controller.plan = Mock(side_effect=plan)

            def harness(version, stage, payload, **_kwargs):
                if stage == "before_model":
                    output = {"context": payload["context"]}
                elif stage == "after_plan":
                    output = {"actions": payload["actions"]}
                else:
                    output = {
                        "loop": {"allow_another_round": True},
                        "continuation": {"checkpoint": False},
                    }
                return {"output": output, "version": version, "stage": stage}

            runtime.sandbox.run_self_harness = Mock(side_effect=harness)
            runtime.store.add_event(Event("TASK_REQUEST", {"message": "long ordinary task"}))
            for _ in range(8):
                runtime.run_once()
                if runtime.store.list_tasks(1)[0].status == TaskStatus.STOPPED:
                    break

        task = runtime.store.list_tasks(1)[0]
        self.assertEqual(task.status, TaskStatus.STOPPED)
        self.assertEqual(runtime.self_versions.current_version(), "v000002")
        self.assertTrue(second_decision_seen)
        self.assertGreaterEqual(phases.count("dedicated_self_decision"), 4)
        cooldown = [
            item for item in runtime.store.recent_traces(limit=1000)
            if item["kind"] == "self_reflection_cooldown_advanced"
        ]
        self.assertEqual(
            [(item["data"]["remaining_before"], item["data"]["remaining_after"])
             for item in reversed(cooldown)],
            [(2, 1), (1, 0)],
        )

    def test_agent_can_spontaneously_commit_self_change_from_reflection(self) -> None:
        config_path = self.root / "reflection-evolve.json"
        config_path.write_text(json.dumps({
            "database": "./data/reflection-evolve.db",
            "workspace": "./workspace-reflection-evolve",
            "runtime": {"completion_mode": "free"},
            "self_modification": {
                "enabled": True, "root": "./agent-self-reflection-evolve",
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
            runtime.controller.plan = Mock(side_effect=[
                Plan("ordinary answer", [], done=True),
                Plan("open after reflection", [
                    Action("evolve", {"reason": "reusable observation"}, call_id="r1"),
                ]),
                Plan("edit descendant", [Action("write", {
                    "path": "/self/SYSTEM.md",
                    "content": "# Self\nReflection-authored behavior.\n",
                }, call_id="r2")]),
                Plan("commit descendant", [
                    Action("evolve", {"operation": "commit"}, call_id="r3"),
                ]),
                Plan("ordinary answer after restart", [], done=True),
            ])

            def harness(version, stage, payload, **_kwargs):
                if stage == "before_model":
                    output = {"context": payload["context"]}
                elif stage == "after_plan":
                    output = {"actions": payload["actions"]}
                else:
                    output = {
                        "loop": {"allow_another_round": True},
                        "continuation": {"checkpoint": False},
                    }
                return {"output": output, "version": version, "stage": stage}

            runtime.sandbox.run_self_harness = Mock(side_effect=harness)
            runtime.store.add_event(Event("TASK_REQUEST", {"message": "ordinary task"}))
            runtime.run_once()
            self.assertEqual(runtime.store.list_tasks(1)[0].status, TaskStatus.DEFERRED)
            runtime.run_once()

        task = runtime.store.list_tasks(1)[0]
        self.assertEqual(task.status, TaskStatus.STOPPED)
        self.assertEqual(runtime.self_versions.current_version(), "v000002")
        self.assertIn("Reflection-authored", runtime.self_versions.system_prompt())
        self.assertTrue(task.result["self_modification"]["evolution_observed"])
        self.assertTrue(task.result["self_modification"]["reflection_offered"])


if __name__ == "__main__":
    unittest.main()
