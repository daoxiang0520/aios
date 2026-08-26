from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aios.capabilities import CapabilityRegistry
from aios.config import Settings, SkillConfig
from aios.runtime import AIOSRuntime
from aios.sandbox import SandboxPolicyError
from aios.skills import SkillManager, SkillPromotionError, SkillValidationError


SKILL_SOURCE = '''import argparse,json
p=argparse.ArgumentParser(); p.add_argument("--input-json",default="{}"); a=p.parse_args()
data=json.loads(a.input_json); print(json.dumps({"value":data.get("value","ok")},ensure_ascii=False))
'''


def manifest(version: str = "1.0.0", capabilities: list[str] | None = None) -> dict:
    return {
        "name": "sample_skill",
        "version": version,
        "description": "A sandboxed reusable test skill.",
        "required_capabilities": capabilities or ["process.sandbox_exec"],
        "input_schema": {"type": "object"},
        "tests": [{"input": {"value": "pass"}, "expect_exit": 0, "stdout_contains": "pass"}],
    }


class FakeBroker:
    def run_candidate(self, package: Path, command: str, timeout_seconds: int) -> dict:
        self.package = package
        self.command = command
        self.timeout_seconds = timeout_seconds
        return {"exit_code": 0, "stdout": '{"value":"pass"}', "stderr": ""}


class V06SkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = root / "config.json"
        config.write_text(json.dumps({
            "database": "data/a.db", "workspace": "workspace",
            "model": {"provider": "mock"},
            "permissions": {"allowed_tools": ["read", "write", "edit", "bash"]},
            "sandbox": {"backend": "docker", "root": "sandbox"},
            "skills": {"enabled": True, "root": "skills", "require_human_promotion": True},
            "evolution": {"enabled": False},
        }), encoding="utf-8")
        self.settings = Settings.load(config)
        self.settings.ensure_directories()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_v06_keeps_exactly_four_model_tools_and_bootstraps_skills(self) -> None:
        runtime = AIOSRuntime(self.settings)
        schemas = {item["function"]["name"] for item in runtime.controller.tool_schemas}
        self.assertEqual(schemas, {"read", "write", "edit", "bash"})
        names = {item["name"] for item in runtime.skills.catalog(runtime.capabilities)}
        self.assertEqual(names, {"workspace_search", "state_query", "trace_failure_analyzer"})

    def test_skill_manifest_requires_declared_known_capabilities(self) -> None:
        manager = SkillManager(self.settings.skills_root, self.settings.skills)
        invalid = manifest()
        invalid["required_capabilities"] = ["filesystem.superuser"]
        with self.assertRaises(SkillValidationError):
            manager.propose(invalid, SKILL_SOURCE)
        missing_process = manifest()
        missing_process["required_capabilities"] = ["filesystem.read"]
        with self.assertRaises(SkillValidationError):
            manager.propose(missing_process, SKILL_SOURCE)

    def test_candidate_benchmark_promotion_versioning_rollback_and_deprecation(self) -> None:
        manager = SkillManager(self.settings.skills_root, self.settings.skills)
        first = manager.propose(manifest("1.0.0"), SKILL_SOURCE)
        with self.assertRaises(SkillPromotionError):
            manager.promote(first["candidate_id"], approved=True)
        report = manager.benchmark(first["candidate_id"], FakeBroker())
        self.assertTrue(report["passed"])
        with self.assertRaises(SkillPromotionError):
            manager.promote(first["candidate_id"], approved=False)
        self.assertEqual(manager.promote(first["candidate_id"], approved=True)["version"], "1.0.0")
        self.assertTrue((manager.runtime_active / "sample_skill" / "manifest.json").is_file())

        duplicate = manager.propose(manifest("1.0.0"), SKILL_SOURCE)
        self.assertEqual(duplicate["candidate_id"], first["candidate_id"])
        with self.assertRaises(SkillPromotionError):
            manager.promote(duplicate["candidate_id"], approved=True)

        second = manager.propose(manifest("1.1.0"), SKILL_SOURCE.replace('"ok"', '"v2"'))
        manager.benchmark(second["candidate_id"], FakeBroker())
        manager.promote(second["candidate_id"], approved=True)
        self.assertEqual(manager.versions("sample_skill")[0]["version"], "1.1.0")
        self.assertEqual(manager.rollback("sample_skill", approved=True)["version"], "1.0.0")
        self.assertEqual(manager.deprecate("sample_skill", approved=True)["status"], "deprecated")
        self.assertEqual(manager.active_skills(), [])
        self.assertFalse((manager.runtime_active / "sample_skill").exists())

    def test_agent_candidate_is_registered_but_not_executed_or_promoted(self) -> None:
        manager = SkillManager(self.settings.skills_root, self.settings.skills)
        candidate_root = self.settings.workspace / "skill_candidates" / "sample_skill"
        candidate_root.mkdir(parents=True)
        (candidate_root / "manifest.json").write_text(
            json.dumps(manifest(), ensure_ascii=False), encoding="utf-8"
        )
        (candidate_root / "skill.py").write_text(SKILL_SOURCE, encoding="utf-8")

        class MustNotRun:
            def run_candidate(self, *args, **kwargs):
                raise AssertionError("human-gated candidate must not execute automatically")

        records = manager.ingest_workspace_candidates(self.settings.workspace, MustNotRun())
        self.assertEqual(records[0]["status"], "candidate")
        self.assertEqual(records[0]["manifest"]["origin"], "agent")
        self.assertFalse((manager.active / "sample_skill").exists())
        self.assertFalse((manager.runtime_active / "sample_skill").exists())

    def test_runtime_projection_does_not_expose_lifecycle_directories(self) -> None:
        manager = SkillManager(self.settings.skills_root, self.settings.skills)
        manager.bootstrap_builtins()
        self.assertTrue((manager.runtime / "skill.py").is_file())
        self.assertTrue((manager.runtime_active / "workspace_search").is_dir())
        for hidden in ("candidates", "history", "deprecated", "reports"):
            self.assertFalse((manager.runtime / hidden).exists())

    def test_skill_dispatcher_rejects_combined_or_direct_skill_commands(self) -> None:
        from aios.sandbox import DockerSandboxBroker

        DockerSandboxBroker._validate_command_scope("python /skills/skill.py list")
        DockerSandboxBroker._validate_command_scope(
            "python /skills/skill.py run workspace_search --input-json '{\"query\":\"a;b\"}'"
        )
        unsafe = (
            "python /skills/skill.py list; ls /skills/active",
            "python /skills/skill.py list 2>&1",
            "cat /skills/active/workspace_search/skill.py",
            "python -c \"open('/skills/active/workspace_search/skill.py').read()\"",
        )
        for command in unsafe:
            with self.subTest(command=command), self.assertRaises(SandboxPolicyError):
                DockerSandboxBroker._validate_command_scope(command)

    def test_skill_exists_but_is_blocked_without_host_capability(self) -> None:
        manager = SkillManager(self.settings.skills_root, self.settings.skills)
        candidate = manager.propose(
            manifest("1.0.0", ["network.external", "process.sandbox_exec"]), SKILL_SOURCE
        )
        manager.benchmark(candidate["candidate_id"], FakeBroker())
        manager.promote(candidate["candidate_id"], approved=True)
        capabilities = CapabilityRegistry.default(sandbox_available=True, network_enabled=False)
        entry = manager.catalog(capabilities)[0]
        self.assertFalse(entry["available"])
        self.assertEqual(entry["capability_assessment"]["needs_authority"][0]["name"], "network.external")

    def test_active_skill_runs_through_read_only_dispatcher_in_real_docker(self) -> None:
        runtime = AIOSRuntime(self.settings)
        if not runtime.sandbox.available():
            self.skipTest("Docker engine unavailable")
        (self.settings.workspace / "needle.txt").write_text("needle body", encoding="utf-8")
        runtime.sandbox.prepare(600, self.settings.workspace)
        runtime.sandbox.expose_read_only_state({
            "capabilities": runtime.capabilities.as_dict(), "tasks": [], "traces": [],
            "dead-letters": [], "memory": [], "skills": runtime.skills.catalog(runtime.capabilities),
        })
        listed = runtime.sandbox.run("python /skills/skill.py list")
        self.assertEqual(listed["exit_code"], 0)
        self.assertIn("workspace_search", listed["stdout"])
        executed = runtime.sandbox.run(
            "python /skills/skill.py run workspace_search --input-json '{\"query\":\"needle\"}'"
        )
        self.assertEqual(executed["exit_code"], 0)
        self.assertIn("needle.txt", executed["stdout"])
        with self.assertRaises(SandboxPolicyError):
            runtime.sandbox.run("python /skills/active/workspace_search/skill.py --input-json '{}'")
        runtime.sandbox.discard()


if __name__ == "__main__":
    unittest.main()
