from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from aios.answers import CanonicalAnswer
from aios.capabilities import EvidenceContract
from aios.cli import _historical_contract_evidence
from aios.config import Settings
from aios.runtime import AIOSRuntime
from aios.situation import SituationResolver
from aios.storage import StateStore
from aios.types import Action, ActionResult, Event, EventStatus, Task, TaskStatus


class V0711RuntimeCorrectnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = self.root / "config.json"
        config.write_text(json.dumps({
            "database": "data/test.db", "workspace": "workspace",
            "model": {"provider": "mock"},
            "permissions": {"allowed_tools": ["read", "write", "edit", "bash"]},
            "sandbox": {"backend": "docker", "root": "sandbox"},
            "skills": {"enabled": False, "root": "skills"},
            "evolution": {"enabled": False},
        }), encoding="utf-8")
        self.settings = Settings.load(config)
        self.settings.ensure_directories()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _situation() -> dict:
        return {
            "coverage_targets": [{"id": "A题", "evidence_paths": ["A题.pdf"]}],
            "resources": [{
                "path": "A题.pdf", "complete": True, "evidence_ref": "trace:1",
            }],
        }

    def test_artifact_backed_answer_uses_body_not_path(self) -> None:
        path = self.settings.workspace / "summary.md"
        path.write_text("A题讨论城市绿色物流路径优化。", encoding="utf-8")
        action = Action("write", {"path": "summary.md", "content": path.read_text(encoding="utf-8")})
        result = ActionResult("write", True, {"path": str(path)})
        contract = EvidenceContract.from_request("生成 summary.md", ["summary.md"])
        answer = CanonicalAnswer.bind("已保存到 summary.md", [action], [result], contract)
        coverage = SituationResolver.assess_coverage(self._situation(), answer.body)
        self.assertTrue(coverage["passed"])
        self.assertIn("A题讨论城市绿色物流路径优化", answer.body)
        self.assertEqual(answer.artifacts[0].role, "final_deliverable")

    def test_artifact_existence_without_semantic_content_fails(self) -> None:
        path = self.settings.workspace / "summary.md"
        path.write_text("只有一个空泛结论。", encoding="utf-8")
        action = Action("write", {"path": "summary.md", "content": path.read_text(encoding="utf-8")})
        result = ActionResult("write", True, {"path": str(path)})
        contract = EvidenceContract.from_request("生成 summary.md", ["summary.md"])
        answer = CanonicalAnswer.bind("已保存到 summary.md", [action], [result], contract)
        coverage = SituationResolver.assess_coverage(self._situation(), answer.body)
        self.assertFalse(coverage["passed"])
        self.assertEqual(coverage["missing_answer_topics"], ["A题"])

    def test_https_url_is_network_not_windows_drive_path(self) -> None:
        contract = EvidenceContract.from_request(
            "完成 https://www.luogu.com.cn/problem/P1593 中的题目"
        )
        capabilities = {item.name for item in contract.capabilities}
        domains = {
            item.value for item in contract.evidence if item.kind == "source_domain"
        }
        self.assertIn("resource.http.read", capabilities)
        self.assertNotIn("network.external", capabilities)
        self.assertNotIn("filesystem.outside_workspace", capabilities)
        self.assertIn("luogu.com.cn", domains)

    def test_url_explanation_does_not_require_network_or_filesystem(self) -> None:
        contract = EvidenceContract.from_request(
            "解释字符串 https://example.com/foo 的 URL 结构"
        )
        capabilities = {item.name for item in contract.capabilities}
        self.assertNotIn("network.external", capabilities)
        self.assertNotIn("filesystem.outside_workspace", capabilities)
        self.assertFalse(any(item.kind == "source_domain" for item in contract.evidence))

    def test_real_windows_drive_path_remains_outside_workspace(self) -> None:
        contract = EvidenceContract.from_request(r"读取 C:\\Users\\person\\secret.txt")
        capabilities = {item.name for item in contract.capabilities}
        self.assertIn("filesystem.outside_workspace", capabilities)

    def test_posix_host_path_remains_outside_workspace(self) -> None:
        contract = EvidenceContract.from_request("读取 /etc/passwd")
        capabilities = {item.name for item in contract.capabilities}
        self.assertIn("filesystem.outside_workspace", capabilities)

    def test_mixed_url_and_host_path_preserves_both_capabilities(self) -> None:
        contract = EvidenceContract.from_request(
            r"读取 C:\\tmp\\a.txt，并参考 https://example.com/source"
        )
        capabilities = {item.name for item in contract.capabilities}
        self.assertIn("filesystem.outside_workspace", capabilities)
        self.assertIn("resource.http.read", capabilities)

    def test_network_evidence_survives_continuation_without_refetch(self) -> None:
        runtime = AIOSRuntime(self.settings)
        contract = EvidenceContract.from_request(
            "读取 https://www.luogu.com.cn/problem/P1593 中的题目"
        )
        state = runtime._task_working_state(1, contract.request)
        network_action = Action("bash", {
            "command": (
                "python -c \"import urllib.request; "
                "print(urllib.request.urlopen('https://www.luogu.com.cn/problem/P1593').status)\""
            ),
        })
        network_result = ActionResult("bash", True, {
            "exit_code": 0, "stdout": "200 luogu.com.cn P1593", "stderr": "",
            "changes": [],
        })
        runtime._record_contract_evidence(
            state, contract, network_action, network_result, 4532,
        )
        runtime.store.create_task(Task("P1593", contract.request))
        runtime.store.add_checkpoint(1, "budget_deferred", {"working_state": state})
        restored = runtime._task_working_state(1, contract.request)
        local_action = Action("bash", {"command": "python P1593.py"})
        local_result = ActionResult("bash", True, {
            "exit_code": 0, "stdout": "15", "stderr": "", "changes": [],
        })
        verification = runtime.verifier.verify(
            [local_action], [local_result], planned_count=1, task_done=True,
            request=contract.request, contract=contract,
            final_output="P1593 solution completed",
            established_evidence=restored["evidence_ledger"],
        )
        self.assertTrue(verification["passed"])
        self.assertEqual(
            {item["kind"] for item in restored["evidence_ledger"]},
            {"network_request", "source_domain"},
        )

    def test_reconcile_rebuilds_evidence_ledger_from_historical_traces(self) -> None:
        store = StateStore(self.settings.database)
        store.initialize()
        task_id = store.create_task(Task("P1593", "读取 https://luogu.com.cn/problem/P1593"))
        cycle_id = "historical-network-cycle"
        store.add_checkpoint(task_id, "started", {"cycle_id": cycle_id})
        action = Action("bash", {
            "command": (
                "python -c \"import urllib.request; "
                "print(urllib.request.urlopen('https://luogu.com.cn/problem/P1593').status)\""
            ),
        })
        result = ActionResult("bash", True, {
            "exit_code": 0, "stdout": "200 luogu.com.cn", "stderr": "", "changes": [],
        })
        store.trace(cycle_id, "plan_created", {
            "round": 1, "summary": "fetch", "done": False,
            "actions": [{"tool": action.tool, "arguments": action.arguments,
                         "reason": action.reason, "call_id": action.call_id}],
        })
        result_trace = store.trace(cycle_id, "action_result", {
            "round": 1, "tool": result.tool, "ok": result.ok,
            "output": result.output, "error": result.error,
            "duration_ms": result.duration_ms,
        })
        contract = EvidenceContract.from_request(store.get_task(task_id).request)
        ledger = _historical_contract_evidence(store, task_id, contract)
        self.assertEqual(
            {(item["kind"], item["evidence_ref"]) for item in ledger},
            {("network_request", f"trace:{result_trace}"),
             ("source_domain", f"trace:{result_trace}")},
        )

    def test_caught_network_error_with_zero_exit_is_not_evidence(self) -> None:
        contract = EvidenceContract.from_request(
            "读取 https://luogu.com.cn/problem/P1593"
        )
        action = Action("bash", {
            "command": (
                "python -c \"import urllib.request; "
                "print('ERR HTTP Error 302')\""
            ),
        })
        result = ActionResult("bash", True, {
            "exit_code": 0, "stdout": "ERR HTTP Error 302\n", "stderr": "",
            "changes": [],
        })
        self.assertEqual(
            AIOSRuntime(self.settings).verifier.collect_evidence(
                contract, action, result, "trace:failed",
            ),
            [],
        )

    def test_same_checkpoint_enqueue_is_idempotent(self) -> None:
        store = StateStore(self.settings.database)
        store.initialize()
        task_id = store.create_task(Task("two cycles", "two cycles"))
        store.update_task(task_id, TaskStatus.DEFERRED, result={})
        checkpoint_id = store.add_checkpoint(task_id, "budget_deferred", {})
        ids = [
            store.enqueue_continuation(task_id, checkpoint_id, "two cycles", 50)[0]
            for _ in range(10)
        ]
        self.assertEqual(len(set(ids)), 1)
        with store.connect() as connection:
            active = connection.execute(
                """SELECT COUNT(*) AS n FROM events WHERE task_id=? AND type='TASK_CONTINUE'
                   AND status IN ('pending','processing')""",
                (task_id,),
            ).fetchone()
        self.assertEqual(int(active["n"]), 1)
        self.assertEqual(store.runtime_metrics()["continuation_duplicates_suppressed"], 9)

    def test_stale_checkpoint_is_discarded_without_model_call(self) -> None:
        runtime = AIOSRuntime(self.settings)
        task_id = runtime.store.create_task(Task("two cycles", "two cycles"))
        runtime.store.update_task(task_id, TaskStatus.DEFERRED, result={})
        checkpoint_a = runtime.store.add_checkpoint(task_id, "budget_deferred", {})
        event_a, _ = runtime.store.enqueue_continuation(task_id, checkpoint_a, "two cycles", 50)
        checkpoint_b = runtime.store.add_checkpoint(task_id, "budget_deferred", {})
        event_b, _ = runtime.store.enqueue_continuation(task_id, checkpoint_b, "two cycles", 50)
        with runtime.store.connect() as connection:
            connection.execute("UPDATE events SET status='done' WHERE id=?", (event_b,))
            connection.execute("UPDATE events SET status='pending' WHERE id=?", (event_a,))
        runtime.controller.plan = Mock()
        runtime.run_once()
        runtime.controller.plan.assert_not_called()
        with runtime.store.connect() as connection:
            row = connection.execute("SELECT status FROM events WHERE id=?", (event_a,)).fetchone()
        self.assertEqual(row["status"], EventStatus.STALE.value)

    def test_terminal_task_cannot_be_resurrected(self) -> None:
        runtime = AIOSRuntime(self.settings)
        task_id = runtime.store.create_task(Task("done", "done"))
        runtime.store.update_task(task_id, TaskStatus.COMPLETED, result={"summary": "done"})
        event_id = runtime.store.add_event(Event("TASK_CONTINUE", {
            "task_id": task_id, "message": "done", "continuation": True,
            "checkpoint_id": 1, "generation": 0,
        }))
        runtime.controller.plan = Mock()
        runtime.run_once()
        runtime.controller.plan.assert_not_called()
        self.assertEqual(runtime.store.get_task(task_id).status, TaskStatus.COMPLETED)
        with runtime.store.connect() as connection:
            row = connection.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
        self.assertEqual(row["status"], EventStatus.STALE.value)

    def test_terminal_task_retry_event_cannot_be_resurrected(self) -> None:
        runtime = AIOSRuntime(self.settings)
        task_id = runtime.store.create_task(Task("done", "done"))
        runtime.store.update_task(task_id, TaskStatus.COMPLETED, result={"summary": "done"})
        event_id = runtime.store.add_event(Event(
            "TASK_REQUEST", {"task_id": task_id, "message": "done"},
        ))
        runtime.controller.plan = Mock()
        runtime.run_once()
        runtime.controller.plan.assert_not_called()
        self.assertEqual(runtime.store.get_task(task_id).status, TaskStatus.COMPLETED)
        with runtime.store.connect() as connection:
            row = connection.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
        self.assertEqual(row["status"], EventStatus.STALE.value)


if __name__ == "__main__":
    unittest.main()
