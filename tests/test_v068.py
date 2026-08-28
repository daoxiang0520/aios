from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from aios.capabilities import CapabilityRegistry, EvidenceContract
from aios.components import build_component_registry
from aios.config import Settings
from aios.evaluation import Verifier
from aios.runtime import AIOSRuntime
from aios.situation import SituationResolver
from aios.types import Action, Event, Plan, TaskStatus


class V068RuntimeSituationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = root / "config.json"
        config.write_text(json.dumps({
            "database": "data/test.db", "workspace": "workspace",
            "model": {"provider": "mock"},
            "permissions": {"allowed_tools": ["read", "write", "edit", "bash"]},
            "sandbox": {"backend": "docker", "root": "sandbox"},
            "skills": {"enabled": True, "root": "skills", "require_human_promotion": True},
            "evolution": {"enabled": False},
        }), encoding="utf-8")
        self.settings = Settings.load(config)
        self.settings.ensure_directories()
        authority = CapabilityRegistry.default(
            sandbox_available=True, network_enabled=False, scientific_available=True,
        )
        self.resolver = SituationResolver(build_component_registry(authority))

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def inventory() -> dict[str, object]:
        return {
            "files": [
                {"path": "MathModeling/A题分析.md", "size": 100},
                {"path": "MathModeling/B题/B题.pdf", "size": 200},
                {"path": "MathModeling/题目分析.md", "size": 300},
                {"path": "MathModeling/raw.csv", "size": 400},
                {"path": "about.html", "size": 500},
                {"path": "aios_info.md", "size": 600},
            ],
            "total_files": 6,
        }

    @staticmethod
    def state() -> dict[str, object]:
        return {
            "objective": "总结数模文件夹里的题目，背景与原理",
            "operational": {"resources": {}, "environment": {}, "artifacts": []},
            "accessed_resources": [], "available_artifacts": [], "execution_environment": {},
        }

    def test_dynamic_situation_map_resolves_resources_capabilities_and_procedures(self) -> None:
        request = "总结数模文件夹里的题目，背景与原理"
        state = self.state()
        contract = EvidenceContract.from_request(request)
        first = self.resolver.resolve(
            request, self.inventory(), contract, state,
            [{"name": "workspace_search", "version": "1.0.0", "description": "Search workspace files", "available": True}],
        )
        paths = {item["path"] for item in first["resources"]}
        self.assertEqual(paths, {
            "MathModeling/A题分析.md", "MathModeling/B题/B题.pdf", "MathModeling/题目分析.md",
        })
        self.assertEqual(first["coverage_scope"]["root"], "MathModeling")
        self.assertEqual(first["coverage_scope"]["outside_root_files_excluded"], 2)
        self.assertEqual(
            {item["id"] for item in first["coverage_targets"]}, {"A题", "B题"},
        )
        selected = {item["path"] for item in first["resources"] if item["selected_for_evidence"]}
        self.assertEqual(selected, {"MathModeling/A题分析.md", "MathModeling/B题/B题.pdf"})
        self.assertFalse(next(
            item for item in first["resources"] if item["path"] == "MathModeling/题目分析.md"
        )["required_for_coverage"])
        self.assertTrue(any(item["name"] == "resource.read" for item in first["capabilities"]))

        state["operational"]["resources"]["MathModeling/A题分析.md"] = {
            "status": "read_complete", "complete": True, "representation": ["text"],
            "evidence_ref": "trace:1", "coverage_labels": ["A题"],
        }
        second = self.resolver.resolve(request, self.inventory(), contract, state, [])
        a_resource = next(item for item in second["resources"] if item["path"].endswith("A题分析.md"))
        self.assertEqual(a_resource["status"], "read_complete")
        self.assertNotEqual(first, second)

    def test_task63_coverage_gate_rejects_missing_c_topic(self) -> None:
        request = "总结数模文件夹里的题目，背景与原理"
        state = self.state()
        resources = state["operational"]["resources"]
        for path, label in (
            ("MathModeling/A题分析.md", "A题"),
            ("MathModeling/B题/B题.pdf", "B题"),
            ("MathModeling/题目分析.md", "C题"),
        ):
            resources[path] = {
                "status": "read_complete", "complete": True, "representation": ["text"],
                "evidence_ref": "trace:1", "coverage_labels": [label],
            }
        situation = self.resolver.resolve(
            request, self.inventory(), EvidenceContract.from_request(request), state, [],
        )
        coverage = self.resolver.assess_coverage(
            situation, "A题讨论烟幕优化；B题讨论薄膜干涉。",
        )
        self.assertFalse(coverage["passed"])
        self.assertEqual(coverage["missing_answer_topics"], ["C题"])
        verified = Verifier().verify(
            [], [], planned_count=0, task_done=True, request=request,
            final_output="A题讨论烟幕优化；B题讨论薄膜干涉。",
            completion_metadata={"claims_complete": True, "quality": "full"},
            coverage_assessment=coverage,
        )
        self.assertFalse(verified["passed"])
        check = next(item for item in verified["checks"] if item["name"] == "required_resource_and_answer_coverage")
        self.assertFalse(check["passed"])

    def test_resource_routing_friction_is_measured_but_not_forbidden(self) -> None:
        state = self.state()
        state["operational"]["resources"]["notes.md"] = {
            "status": "read_complete", "complete": True, "evidence_ref": "trace:9",
        }
        repeated = self.resolver.classify_action("read", {"path": "/workspace/notes.md"}, state)
        self.assertEqual(repeated["kind"], "repeated_resource_read")
        self.assertIsNone(self.resolver.classify_action("read", {"path": "notes.md", "offset": 100}, state))
        bypass = self.resolver.classify_action("bash", {"command": "cat notes.md"}, state)
        self.assertEqual(bypass["kind"], "redundant_resource_bypass")
        self.assertTrue(bypass["already_read"])
        self.assertEqual(
            self.resolver.classify_action("bash", {"command": "find . -name '*.md'"}, state)["kind"],
            "environment_probe",
        )

    def test_runtime_records_repeated_resource_read_metric(self) -> None:
        (self.settings.workspace / "input.txt").write_text("evidence", encoding="utf-8")
        runtime = AIOSRuntime(self.settings)
        runtime.controller.plan = Mock(side_effect=[
            Plan("read", [Action("read", {"path": "input.txt"})], done=False),
            Plan("reread", [Action("read", {"path": "input.txt"})], done=False),
            Plan("complete", [], done=True),
        ])
        runtime.store.add_event(Event("USER_REQUEST", {"message": "分析 input.txt"}))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(task.result["evidence"]["repeated_resource_reads"], 1)
        self.assertEqual(task.result["evidence"]["repeated_resource_executions"], 0)
        self.assertEqual(task.result["evidence"]["observation_reuse_hits"], 1)
        self.assertTrue(any(
            item["kind"] == "repeated_resource_read"
            for item in runtime.store.recent_traces(limit=100)
        ))

    def test_task63_style_runtime_repairs_coverage_without_rereading(self) -> None:
        model_root = self.settings.workspace / "MathModeling"
        (model_root / "B题").mkdir(parents=True)
        (model_root / "A题分析.md").write_text("A题：烟幕优化", encoding="utf-8")
        (model_root / "B题" / "B题说明.md").write_text("B题：薄膜干涉", encoding="utf-8")
        (model_root / "题目分析.md").write_text("C题：NIPT 建模", encoding="utf-8")
        runtime = AIOSRuntime(self.settings)
        runtime.controller.plan = Mock(side_effect=[
            Plan("inspect", [
                Action("read", {"path": "MathModeling/A题分析.md"}),
                Action("read", {"path": "MathModeling/B题/B题说明.md"}),
                Action("read", {"path": "MathModeling/题目分析.md"}),
            ], done=False),
            Plan("A题讨论烟幕优化；B题讨论薄膜干涉。", [], done=True),
            Plan("A题讨论烟幕优化；B题讨论薄膜干涉；C题讨论 NIPT 建模。", [], done=True),
        ])
        runtime.store.add_event(Event(
            "USER_REQUEST", {"message": "总结整个数模文件夹里的题目，背景与原理"},
        ))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(task.result["evidence"]["model_api_calls"], 3)
        self.assertEqual(task.result["evidence"]["repeated_resource_reads"], 0)
        self.assertTrue(task.result["evidence"]["coverage_assessment"]["passed"])
        self.assertTrue(any(
            item["kind"] == "coverage_repair_requested"
            for item in runtime.store.recent_traces(limit=100)
        ))

    def test_unrequested_workspace_candidate_is_not_attributed_to_task(self) -> None:
        package = self.settings.workspace / "skill_candidates" / "incidental"
        package.mkdir(parents=True)
        (package / "manifest.json").write_text(json.dumps({
            "name": "incidental", "version": "1.0.0", "description": "incidental",
            "required_capabilities": ["process.sandbox_exec"],
            "input_schema": {"type": "object"},
            "tests": [{"input": {}, "expect_exit": 0}],
        }), encoding="utf-8")
        (package / "skill.py").write_text("print('{}')", encoding="utf-8")
        runtime = AIOSRuntime(self.settings)
        runtime.controller.plan = Mock(return_value=Plan("done", [], done=True))
        runtime.store.add_event(Event("USER_REQUEST", {"message": "回答一个普通问题"}))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertNotIn("skill_candidates", task.result)
        self.assertEqual(runtime.skills.list_candidates(), [])
        suppressed = next(
            item for item in runtime.store.recent_traces(limit=100)
            if item["kind"] == "skill_candidate_ingest_suppressed"
        )
        self.assertEqual(suppressed["data"]["decision"], "NO_ACTION")


if __name__ == "__main__":
    unittest.main()
