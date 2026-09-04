from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from aios.capabilities import CapabilityRegistry
from aios.config import Settings, SkillConfig
from aios.runtime import AIOSRuntime
from aios.sandbox import SandboxPolicyError
from aios.skills import SkillManager, SkillPromotionError, SkillValidationError
from aios.types import Action, Event, Plan, TaskStatus


SKILL_SOURCE = '''import argparse,json
p=argparse.ArgumentParser(); p.add_argument("--input-json",default="{}"); a=p.parse_args()
data=json.loads(a.input_json); print(json.dumps({"value":data.get("value","ok")},ensure_ascii=False))
'''


def manifest(version: str = "1.0.0", capabilities: list[str] | None = None) -> dict:
    value = {
        "name": "sample_skill",
        "version": version,
        "description": "A sandboxed reusable test skill.",
        "required_capabilities": capabilities or ["process.sandbox_exec"],
        "input_schema": {"type": "object"},
        "tests": [{"input": {"value": "pass"}, "expect_exit": 0, "stdout_contains": "pass"}],
    }
    if version != "1.0.0":
        value.update({
            "parent_version": "1.0.0",
            "mutation_reason": "Improve the reusable test behavior.",
            "source_task_ids": [7],
            "source_trace_ids": [70],
            "hypothesis": "The mutation should preserve behavior while improving reuse.",
        })
    return value


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

    def test_builtin_skill_bootstrap_can_be_disabled_without_disabling_authoring(self) -> None:
        self.settings.skills.bootstrap_builtins = False
        runtime = AIOSRuntime(self.settings)
        self.assertTrue(self.settings.skills.enabled)
        self.assertEqual(runtime.skills.active_skills(), [])
        proposal = runtime.skills.propose(manifest(), SKILL_SOURCE)
        self.assertEqual(proposal["status"], "candidate")

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
        self.assertEqual(manager.versions("sample_skill")[0]["parent_version"], "1.0.0")
        self.assertEqual(manager.versions("sample_skill")[0]["source_task_ids"], [7])
        self.assertEqual(manager.rollback("sample_skill", approved=True)["version"], "1.0.0")
        self.assertEqual(manager.deprecate("sample_skill", approved=True)["status"], "deprecated")
        self.assertEqual(manager.active_skills(), [])
        self.assertFalse((manager.runtime_active / "sample_skill").exists())

    def test_mutation_requires_explicit_parent_lineage(self) -> None:
        manager = SkillManager(self.settings.skills_root, self.settings.skills)
        first = manager.propose(manifest(), SKILL_SOURCE)
        manager.benchmark(first["candidate_id"], FakeBroker())
        manager.promote(first["candidate_id"], approved=True)
        missing_parent = manifest("1.1.0")
        missing_parent.pop("parent_version")
        candidate = manager.propose(missing_parent, SKILL_SOURCE.replace('"ok"', '"new"'))
        manager.benchmark(candidate["candidate_id"], FakeBroker())
        with self.assertRaises(SkillPromotionError):
            manager.promote(candidate["candidate_id"], approved=True)

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

    def test_agent_candidate_inherits_source_task_and_trace_lineage(self) -> None:
        manager = SkillManager(self.settings.skills_root, self.settings.skills)
        candidate_root = self.settings.workspace / "skill_candidates" / "sample_skill"
        candidate_root.mkdir(parents=True)
        (candidate_root / "manifest.json").write_text(json.dumps(manifest()), encoding="utf-8")
        (candidate_root / "skill.py").write_text(SKILL_SOURCE, encoding="utf-8")
        records = manager.ingest_workspace_candidates(
            self.settings.workspace, FakeBroker(), source_task_id=41, source_trace_ids=[401, 402]
        )
        lineage = records[0]["manifest"]
        self.assertEqual(lineage["source_task_ids"], [41])
        self.assertEqual(lineage["source_trace_ids"], [401, 402])

    def test_runtime_records_standard_skill_telemetry_and_traces(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.sandbox.run = Mock(return_value={
            "exit_code": 0, "stdout": "[]", "stderr": "", "changes": []
        })
        runtime.controller.plan = Mock(side_effect=[
            Plan(
                "use reusable search",
                [Action("bash", {
                    "command": "python /skills/skill.py run workspace_search --input-json '{\"query\":\"needle\"}'"
                }, call_id="skill_call")],
                done=False,
                model_usage={"model_calls": 1, "total_tokens": 100},
            ),
            Plan(
                "No matching files were found.", [], done=True,
                model_usage={"model_calls": 1, "total_tokens": 50},
            ),
        ])
        runtime.store.add_event(Event("USER_REQUEST", {"message": "inspect files for needle"}))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        usage = runtime.store.list_skill_usage()
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["skill_name"], "workspace_search")
        self.assertEqual(usage[0]["skill_version"], "1.0.0")
        self.assertEqual(usage[0]["input_keys"], ["query"])
        self.assertEqual(usage[0]["status"], "success")
        self.assertTrue(usage[0]["verifier_passed"])
        self.assertEqual(usage[0]["model_calls_before"], 1)
        self.assertEqual(usage[0]["model_calls_after"], 2)
        self.assertEqual(usage[0]["tokens_before"], 100)
        self.assertEqual(usage[0]["tokens_after"], 150)
        kinds = {item["kind"] for item in runtime.store.recent_traces(limit=50)}
        self.assertTrue({"SKILL_INVOKE", "SKILL_CAPABILITY_CHECK", "SKILL_RESULT"} <= kinds)

    def test_skill_authoring_contract_defers_inspection_and_requires_valid_package(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.capabilities = CapabilityRegistry.default(
            sandbox_available=True, network_enabled=False
        )
        runtime.sandbox.run = Mock(side_effect=AssertionError("authoring inspection must be deferred"))
        skill_manifest = {
            "name": "python_file_stats",
            "version": "1.0.0",
            "description": "Count Python files and source lines.",
            "entrypoint": "skill.py",
            "required_capabilities": ["filesystem.read", "process.sandbox_exec"],
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            "tests": [{
                "input": {"path": "."}, "expect_exit": 0,
                "stdout_contains": "python_files",
            }],
        }
        runtime.controller.plan = Mock(side_effect=[
            Plan(
                "inspect internals",
                [Action("bash", {"command": "ls -la /skills"}, call_id="bad_scan")],
                done=False,
            ),
            Plan(
                "write candidate",
                [
                    Action("write", {
                        "path": "skill_candidates/python_file_stats/manifest.json",
                        "content": json.dumps(skill_manifest),
                    }, call_id="manifest"),
                    Action("write", {
                        "path": "skill_candidates/python_file_stats/skill.py",
                        "content": SKILL_SOURCE,
                    }, call_id="source"),
                ],
                done=False,
            ),
            Plan("Candidate package created and queued for review.", [], done=True),
        ])
        request = (
            "请设计一个可复用 Skill，接收目录路径并输出 Python 文件与总行数，"
            "同时写基础测试。"
        )
        runtime.store.add_event(Event("TASK_REQUEST", {"message": request}))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(runtime.sandbox.run.call_count, 0)
        self.assertIn("skill_authoring", runtime.controller.plan.call_args_list[0].args[2])
        self.assertEqual(task.result["skill_candidates"][0]["manifest"]["name"], "python_file_stats")
        check = next(
            item for item in task.result["evidence"]["verification"]["checks"]
            if item["name"] == "valid_skill_candidate_package"
        )
        self.assertTrue(check["passed"])

    def test_skill_authoring_verifier_rejects_incomplete_package(self) -> None:
        from aios.evaluation import Verifier
        from aios.types import ActionResult

        action = Action("write", {
            "path": "skill_candidates/incomplete_skill/skill.py",
            "content": SKILL_SOURCE,
        })
        result = ActionResult("write", True, {"path": "unused"})
        verified = Verifier().verify(
            [action], [result], planned_count=1, task_done=True,
            request="创建一个 Skill 并编写基础测试。", final_output="done",
        )
        check = next(
            item for item in verified["checks"]
            if item["name"] == "valid_skill_candidate_package"
        )
        self.assertFalse(check["passed"])
        self.assertFalse(verified["passed"])

    def test_short_skill_capability_question_does_not_trigger_authoring(self) -> None:
        manager = SkillManager(self.settings.skills_root, self.settings.skills)
        self.assertIsNone(manager.authoring_context("你能产生skill吗？"))
        self.assertIsNotNone(manager.authoring_context("请设计一个统计 Python 文件的可复用 Skill。"))

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
