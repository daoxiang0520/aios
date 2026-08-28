from __future__ import annotations

import unittest

from aios.capabilities import EvidenceContract
from aios.evaluation import Verifier
from aios.types import Action, ActionResult


class StructuredCompletionSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = Verifier()
        self.available = {"satisfied": True, "blocking": [], "needs_authority": []}
        self.complete = {
            "claims_complete": True,
            "quality": "full",
            "capability_degraded": False,
            "missing_capabilities": [],
            "execution_blocked": False,
            "substitution_used": False,
            "reason": None,
        }

    def verify_text(self, text: str):
        return self.verifier.verify(
            [], [], planned_count=0, task_done=True, request="分析问题",
            final_output=text, capability_assessment=self.available,
            completion_metadata=self.complete,
        )

    def test_task61_business_conclusion_is_completed(self) -> None:
        result = self.verify_text(
            "云团始终偏离视线至少约 46 m，无法形成有效遮蔽。建模与数值验证均已完成。"
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["result_vector"]["quality"], "full")
        self.assertEqual(result["result_vector"]["degradation_signal"], "none")

    def test_business_negations_are_not_agent_degradation(self) -> None:
        examples = (
            "该约束无法同时满足。",
            "该方程无法得到实数解。",
            "现有证据无法拒绝原假设。",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(self.verify_text(text)["outcome"], "completed")

    def test_real_agent_or_environment_limit_is_degraded(self) -> None:
        examples = (
            "我无法访问附件.xlsx。",
            "当前环境无法执行所需的外部网络请求。",
            "由于无法读取原始文件，我改用用户提供的摘要进行分析。",
        )
        for text in examples:
            with self.subTest(text=text):
                result = self.verify_text(text)
                self.assertEqual(result["outcome"], "degraded")
                self.assertEqual(result["result_vector"]["degradation_signal"], "language_heuristic")

    def test_structured_substitution_is_a_hard_degradation_signal(self) -> None:
        metadata = {**self.complete, "quality": "degraded", "substitution_used": True,
                    "reason": "source_unavailable"}
        result = self.verifier.verify(
            [], [], planned_count=0, task_done=True, final_output="已给出分析。",
            capability_assessment=self.available, completion_metadata=metadata,
        )
        self.assertEqual(result["outcome"], "degraded")
        self.assertEqual(result["result_vector"]["degradation_signal"], "structured")
        self.assertTrue(result["result_vector"]["substitution_used"])

    def test_controller_completed_claim_cannot_override_missing_evidence(self) -> None:
        contract = EvidenceContract.from_request("联网查询最新信息")
        result = self.verifier.verify(
            [], [], planned_count=0, task_done=True,
            request="联网查询最新信息", contract=contract,
            final_output="查询已经完成。", capability_assessment=self.available,
            completion_metadata=self.complete,
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["outcome"], "retryable_failure")
        self.assertEqual(result["result_vector"]["evidence"], "unsatisfied")

    def test_host_capability_state_outranks_completed_claim(self) -> None:
        unavailable = {
            "satisfied": False,
            "blocking": [{"name": "network.external", "state": "missing"}],
            "needs_authority": [],
        }
        result = self.verifier.verify(
            [], [], planned_count=0, task_done=True, final_output="已完成。",
            capability_assessment=unavailable, completion_metadata=self.complete,
        )
        self.assertEqual(result["outcome"], "degraded")
        self.assertEqual(result["result_vector"]["capability"], "degraded")
        self.assertIn("network.external", result["result_vector"]["missing_capabilities"])


if __name__ == "__main__":
    unittest.main()
