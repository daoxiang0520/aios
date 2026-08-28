from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aios.capabilities import CapabilityRegistry, EvidenceContract
from aios.components import build_component_registry
from aios.config import Settings
from aios.situation import SituationResolver


class V0682GoalOrientedCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = root / "config.json"
        config.write_text(json.dumps({
            "database": "data/test.db", "workspace": "workspace",
            "model": {"provider": "mock"},
            "permissions": {"allowed_tools": ["read", "write", "edit", "bash"]},
            "sandbox": {"backend": "docker", "root": "sandbox"},
            "skills": {"enabled": False, "root": "skills"},
            "evolution": {"enabled": False},
        }), encoding="utf-8")
        settings = Settings.load(config)
        settings.ensure_directories()
        authority = CapabilityRegistry.default(
            sandbox_available=True, network_enabled=False, scientific_available=True,
        )
        self.resolver = SituationResolver(build_component_registry(authority))

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def inventory() -> dict[str, object]:
        paths = (
            "MathModeling/A题/A题.pdf",
            "MathModeling/B题/B题.pdf",
            "MathModeling/C题/C题.pdf",
            "MathModeling/题目分析.md",
            "MathModeling/A题/问题1_建模与求解.md",
            "MathModeling/A题分析.md",
            "MathModeling/第一题建模.md",
            "about.html",
        )
        return {"files": [{"path": path, "size": 100} for path in paths], "total_files": len(paths)}

    @staticmethod
    def state() -> dict[str, object]:
        resources = {}
        for label in ("A题", "B题", "C题"):
            path = f"MathModeling/{label}/{label}.pdf"
            resources[path] = {
                "status": "read_complete", "complete": True,
                "representation": ["text"], "evidence_ref": f"trace:{label[0]}",
                "coverage_labels": [label],
            }
        return {"operational": {"resources": resources, "environment": {}, "artifacts": []}}

    def test_task65_selects_one_primary_evidence_per_semantic_target(self) -> None:
        request = "总结数模文件夹里的题目，背景与原理"
        situation = self.resolver.resolve(
            request, self.inventory(), EvidenceContract.from_request(request), self.state(), [],
        )
        targets = {item["id"]: item["evidence_paths"] for item in situation["coverage_targets"]}
        self.assertEqual(targets, {
            "A题": ["MathModeling/A题/A题.pdf"],
            "B题": ["MathModeling/B题/B题.pdf"],
            "C题": ["MathModeling/C题/C题.pdf"],
        })
        selected = {item["path"] for item in situation["resources"] if item["selected_for_evidence"]}
        self.assertEqual(selected, {path[0] for path in targets.values()})
        self.assertFalse(any(
            item["required_for_coverage"]
            for item in situation["resources"]
            if item["path"].endswith(".md")
        ))

    def test_answer_coverage_rejects_task65_b_topic_disclaimer(self) -> None:
        request = "总结数模文件夹里的题目，背景与原理"
        situation = self.resolver.resolve(
            request, self.inventory(), EvidenceContract.from_request(request), self.state(), [],
        )
        partial = self.resolver.assess_coverage(
            situation,
            "A题讨论烟幕优化的背景与原理。\n"
            "B题正文未完整呈现，如需细节可重新读取。\n"
            "C题讨论 NIPT 时点选择的背景与原理。",
        )
        self.assertFalse(partial["passed"])
        self.assertEqual(partial["missing_evidence_targets"], [])
        self.assertEqual(partial["missing_answer_topics"], ["B题"])

        complete = self.resolver.assess_coverage(
            situation,
            "A题讨论烟幕优化的背景与运动学原理。\n"
            "B题讨论薄膜厚度测量的背景与光学干涉原理。\n"
            "C题讨论 NIPT 时点选择的背景与统计建模原理。",
        )
        self.assertTrue(complete["passed"])
        self.assertEqual(complete["coverage_model"], "goal_oriented")

    def test_task66_b_topic_deferral_is_not_semantic_coverage(self) -> None:
        request = "总结数模文件夹里的题目，背景与原理"
        situation = self.resolver.resolve(
            request, self.inventory(), EvidenceContract.from_request(request), self.state(), [],
        )
        result = self.resolver.assess_coverage(
            situation,
            "A题总结了烟幕投放的背景、五个问题与运动学原理。\n"
            "B题PDF已读取完成，但具体文字内容未能在此轮完整呈现，内容待下一轮补充。\n"
            "C题总结了NIPT背景、四个问题与统计建模原理。",
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["missing_evidence_targets"], [])
        self.assertEqual(result["missing_answer_topics"], ["B题"])
        target = next(item for item in result["targets"] if item["target"] == "B题")
        self.assertTrue(target["has_evidence"])
        self.assertFalse(target["covered_in_answer"])


if __name__ == "__main__":
    unittest.main()
