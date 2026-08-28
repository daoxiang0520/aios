from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aios.config import PermissionConfig, SandboxConfig, Settings
from aios.resources import ResourceAdapter
from aios.runtime import AIOSRuntime
from aios.sandbox import DockerSandboxBroker, SandboxSession
from aios.types import Action, Event, Plan, TaskStatus


class V0681RuntimeCorrectnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_observation_cache_uses_digest_representation_and_range(self) -> None:
        workspace = self.root / "workspace"
        dependencies = self.root / "dependencies"
        workspace.mkdir()
        dependencies.mkdir()
        target = workspace / "notes.md"
        target.write_text("first observation", encoding="utf-8")
        sandbox = SimpleNamespace(session=SimpleNamespace(
            path=workspace, dependencies_path=dependencies,
        ))
        first_adapter = ResourceAdapter(PermissionConfig(), sandbox)  # type: ignore[arg-type]
        first = first_adapter.read(str(target))
        first_adapter.attach_observation_ref(first, 123)
        self.assertFalse(first["observation_cache"]["hit"])

        # A fresh adapter instance proves reuse is persisted at task scope, not
        # merely held in one Python object.
        second_adapter = ResourceAdapter(PermissionConfig(), sandbox)  # type: ignore[arg-type]
        second = second_adapter.read(str(target))
        self.assertTrue(second["observation_cache"]["hit"])
        self.assertEqual(second["observation_cache"]["source_observation_ref"], "trace:123")
        self.assertEqual(second["resource"], first["resource"])

        ranged = second_adapter.read(str(target), offset=2, limit=4)
        self.assertFalse(ranged["observation_cache"]["hit"])
        self.assertNotEqual(ranged["observation_cache"]["key"], second["observation_cache"]["key"])
        target.write_text("changed observation", encoding="utf-8")
        changed = second_adapter.read(str(target))
        self.assertFalse(changed["observation_cache"]["hit"])
        self.assertNotEqual(changed["observation_cache"]["content_digest"], second["observation_cache"]["content_digest"])

    def test_sandbox_health_is_cached_and_explicitly_invalidated(self) -> None:
        broker = DockerSandboxBroker(self.root / "sandbox", SandboxConfig(health_ttl_seconds=60))
        healthy = SimpleNamespace(returncode=0, stdout="27.0", stderr="")
        with patch("aios.sandbox.shutil.which", return_value="docker"), patch(
            "aios.sandbox.subprocess.run", return_value=healthy,
        ) as run:
            self.assertTrue(broker.available())
            self.assertTrue(broker.available())
            self.assertEqual(run.call_count, 1)
            broker.invalidate_health("test")
            self.assertTrue(broker.available())
            self.assertEqual(run.call_count, 2)
        self.assertEqual(broker.health_probe_count, 2)

    def test_adapter_retries_one_transient_daemon_failure(self) -> None:
        broker = self._ready_broker()
        transient = SimpleNamespace(
            returncode=125, stdout="", stderr="Cannot connect to the Docker daemon",
        )
        healthy = SimpleNamespace(returncode=0, stdout="27.0", stderr="")
        success = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"resource": {"path": "x.pdf", "type": "application/pdf", "metadata": {}, "representations": []}}),
            stderr="",
        )
        with patch("aios.sandbox.shutil.which", return_value="docker"), patch(
            "aios.sandbox.subprocess.run", side_effect=[transient, healthy, success],
        ) as run:
            result = broker.read_resource("x.pdf", kind="pdf")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["retry_count"], 1)
        self.assertTrue(result["transient_recovered"])
        self.assertEqual(run.call_count, 3)

    def test_adapter_does_not_retry_deterministic_parse_failure(self) -> None:
        broker = self._ready_broker()
        corrupt = SimpleNamespace(returncode=1, stdout="", stderr="PdfReadError: corrupt file")
        with patch("aios.sandbox.subprocess.run", return_value=corrupt) as run:
            result = broker.read_resource("x.pdf", kind="pdf")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(run.call_count, 1)

    def test_task64_release_gate_scopes_and_reuses_observations(self) -> None:
        config = self.root / "config.json"
        config.write_text(json.dumps({
            "database": "data/test.db", "workspace": "workspace",
            "model": {"provider": "mock"},
            "permissions": {"allowed_tools": ["read", "write", "edit", "bash"]},
            "sandbox": {"backend": "docker", "root": "sandbox", "health_ttl_seconds": 60},
            "skills": {"enabled": False, "root": "skills"},
            "evolution": {"enabled": False},
        }), encoding="utf-8")
        settings = Settings.load(config)
        settings.ensure_directories()
        model_root = settings.workspace / "MathModeling"
        model_root.mkdir()
        for name, content in (
            ("A题分析.md", "A题：烟幕优化"),
            ("B题分析.md", "B题：薄膜干涉"),
            ("题目分析.md", "C题：NIPT 建模"),
        ):
            (model_root / name).write_text(content, encoding="utf-8")
        (settings.workspace / "about.html").write_text("outside", encoding="utf-8")
        (settings.workspace / "summary.md").write_text("outside", encoding="utf-8")
        runtime = AIOSRuntime(settings)
        reads = [
            Action("read", {"path": "MathModeling/A题分析.md"}),
            Action("read", {"path": "MathModeling/B题分析.md"}),
            Action("read", {"path": "MathModeling/题目分析.md"}),
        ]
        runtime.controller.plan = Mock(side_effect=[
            Plan("initial read", reads, done=False),
            Plan("model repeated the same request", reads, done=False),
            Plan("A题为烟幕优化；B题为薄膜干涉；C题为 NIPT 建模。", [], done=True),
        ])
        runtime.store.add_event(Event(
            "USER_REQUEST", {"message": "总结数模文件夹里的题目，背景与原理"},
        ))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        evidence = task.result["evidence"]
        scope = task.result["situation_map"]["coverage_scope"]
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(scope["root"], "MathModeling")
        self.assertEqual(scope["outside_root_files_excluded"], 2)
        self.assertFalse(any(
            not path.startswith("MathModeling/")
            for path in evidence["coverage_assessment"]["required_resources"]
        ))
        self.assertEqual(evidence["repeated_resource_reads"], 3)
        self.assertEqual(evidence["repeated_resource_executions"], 0)
        self.assertEqual(evidence["observation_reuse_hits"], 3)
        self.assertEqual(evidence["sandbox_health_probes"], evidence["sandbox_sessions"])
        self.assertEqual(evidence["failed_actions"], 0)

    def _ready_broker(self) -> DockerSandboxBroker:
        root = self.root / "sandbox"
        session_root = root / "task_1"
        workspace = session_root / "workspace"
        state = session_root / "state"
        dependencies = root / "task_dependencies" / "task_1"
        workspace.mkdir(parents=True)
        state.mkdir(parents=True)
        (dependencies / "pypdf").mkdir(parents=True)
        (workspace / "x.pdf").write_bytes(b"fixture")
        broker = DockerSandboxBroker(root, SandboxConfig(health_ttl_seconds=60))
        broker.session = SandboxSession(1, workspace, state, dependencies)
        broker._health_state = "ready"
        broker._health_checked_at = time.monotonic()
        return broker


if __name__ == "__main__":
    unittest.main()
