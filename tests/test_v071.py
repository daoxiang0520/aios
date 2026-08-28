from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from aios.runtime import AIOSRuntime
from aios.types import Action, ActionResult


class V071SemanticFitnessTests(unittest.TestCase):
    def test_complete_resource_carries_bounded_semantic_residue_across_context_projection(self) -> None:
        runtime = object.__new__(AIOSRuntime)
        runtime.settings = SimpleNamespace(
            budget=SimpleNamespace(working_state_characters=8000),
        )
        state = {
            "objective": "总结 B 题",
            "semantic": {}, "operational": {"resources": {}},
            "established_facts": [], "completed_steps": [],
            "available_artifacts": [], "accessed_resources": [],
            "execution_environment": {}, "pending": [],
            "important_evidence_refs": [], "semantic_state": {},
        }
        b_text = (
            "B题 碳化硅外延层厚度的确定。背景是第三代半导体材料的厚度检测。"
            "核心原理是红外光在外延层表面和衬底界面反射形成干涉条纹。"
            "问题1建立单次反射厚度模型；问题2用附件1和2反演厚度；"
            "问题3研究多光束干涉、附件3和4，并消除其对计算精度的影响。"
        )
        result = ActionResult("read", True, output={
            "resource": {
                "path": "MathModeling/B题/B题.pdf",
                "metadata": {"pages": 2, "characters": len(b_text)},
                "representations": [{"kind": "text", "text": b_text, "truncated": False}],
            },
            "observation_cache": {"hit": False, "content_digest": "fixture"},
        })
        runtime._update_working_state(
            state, Action("read", {"path": "MathModeling/B题/B题.pdf"}), result, 4088,
        )
        projection = runtime._working_state_projection(state)
        restored = json.loads(json.dumps(projection, ensure_ascii=False))
        resource = restored["operational"]["resources"]["MathModeling/B题/B题.pdf"]
        self.assertTrue(resource["complete"])
        self.assertEqual(resource["evidence_ref"], "trace:4088")
        self.assertIn("碳化硅外延层厚度", resource["semantic_residue"])
        self.assertIn("多光束干涉", resource["semantic_residue"])
        self.assertLessEqual(len(resource["semantic_residue"]), 1200)

    def test_semantic_residue_budget_is_bounded_across_many_resources(self) -> None:
        resources = {
            f"doc-{index}.pdf": {"access_count": 20 - index, "semantic_residue": "证据" * 500}
            for index in range(10)
        }
        AIOSRuntime._bound_semantic_residues(resources)
        carried = sum(len(str(item["semantic_residue"])) for item in resources.values())
        self.assertLessEqual(carried, 4000)
        self.assertTrue(resources["doc-0.pdf"]["semantic_residue"])


if __name__ == "__main__":
    unittest.main()
