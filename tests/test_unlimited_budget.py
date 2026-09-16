from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from aios.config import BudgetConfig, Settings
from aios.controller import ControllerError
from aios.lineage import LineageManager
from aios.runtime import AIOSRuntime, TaskBudget
from aios.types import Action, Event, Plan, Task, TaskStatus
from aios.webui import AIOSWebApplication


class UnlimitedBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(root=root, database=root / "test.db", workspace=root / "workspace")
        self.settings.runtime.completion_mode = "free"
        self.settings.sandbox.backend = "local"
        self.settings.skills.enabled = False
        self.settings.budget = BudgetConfig(
            enabled=False, max_model_calls_per_cycle=1, max_tool_calls_per_cycle=1,
            max_model_calls_per_task=1, max_tool_calls_per_task=1,
            max_tokens_per_task=1, max_cycles_per_task=1,
            soft_model_calls_per_task=1, soft_tokens_per_task=1,
        )
        self.settings.max_actions_per_cycle = 1
        probe = patch("aios.sandbox.DockerSandboxBroker.available", return_value=False)
        probe.start()
        self.addCleanup(probe.stop)

    def runtime(self):
        return AIOSRuntime(self.settings)

    def test_switch_defaults_on_and_rejects_non_boolean(self):
        self.assertTrue(BudgetConfig().enabled)
        for value in ("false", 0, None):
            with self.assertRaises(ValueError):
                BudgetConfig(enabled=value)

    def test_unlimited_remaining_is_null_but_usage_is_preserved(self):
        budget = TaskBudget(1, 1, 1, 1, used_tokens=123, enabled=False)
        self.assertEqual(budget.remaining(), dict.fromkeys(("model_calls", "tool_calls", "tokens", "cycles")))
        self.assertEqual(budget.used_tokens, 123)
        budget.enabled = True
        self.assertEqual(budget.remaining()["tokens"], 0)

    def test_unlimited_selection_preserves_order_without_reservation(self):
        actions = [Action("read", {}), Action("write", {}), Action("read", {})]
        self.assertEqual(AIOSRuntime._select_actions(actions, None, 10), (actions, []))
        self.assertEqual(len(AIOSRuntime._select_actions(actions, 1, 0)[0]), 1)

    def test_task_crosses_all_old_limits_and_lineage_cap(self):
        runtime = self.runtime()
        task_id = runtime.store.create_task(Task("long task", "produce outputs"))
        manager = LineageManager(runtime.store)
        child = manager.fork("lin_root", {"max_actions_per_cycle": 1}, actor="agent", decision={})
        # Use the public binding API exactly as task submission does.
        runtime.store.bind_task_lineage(task_id, child["lineage_id"])
        captured = []
        plans = [
            Plan("working", [Action("write", {"path": f"part_{i}_{j}.txt", "content": "ok"}) for j in range(2)],
                 done=False, model_usage={"model_calls": 1, "total_tokens": 12000})
            for i in range(26)
        ] + [Plan("I stop here", [], done=True, model_usage={"model_calls": 1, "total_tokens": 10})]

        def plan(intent, goals, context):
            captured.append(copy.deepcopy(context["budget"]))
            return plans.pop(0)

        runtime.controller.plan = Mock(side_effect=plan)
        runtime.store.add_event(Event("TASK_REQUEST", {"task_id": task_id, "message": "produce outputs"}))
        self.assertTrue(runtime.run_once())
        task = runtime.store.get_task(task_id)
        self.assertEqual(task.status, TaskStatus.STOPPED, task.error)
        evidence = task.result["evidence"]
        self.assertEqual(evidence["task_tool_calls"], 52)
        self.assertEqual(evidence["model_api_calls"], 27)
        self.assertEqual(evidence["model_tokens"], 312010)
        self.assertFalse(evidence["budget_limits_enabled"])
        self.assertFalse(evidence["budget_truncated"])
        self.assertEqual(runtime.store.count_pending_events(), 0)
        for budget in captured:
            self.assertFalse(budget["enabled"])
            self.assertFalse(budget["force_final"])
            self.assertFalse(budget["soft_pressure"])
            self.assertIsNone(budget["remaining_tool_calls"])
            self.assertIsNone(budget["max_model_calls_this_cycle"])
            self.assertEqual(budget["reserved_completion_tool_calls"], 0)

    def test_shutdown_checkpoints_and_resumes_without_replaying_actions(self):
        runtime = self.runtime()

        def pause(*args):
            runtime.request_shutdown()
            return Plan("work before shutdown", [Action("write", {"path": "saved.txt", "content": "kept"})], done=False)

        runtime.controller.plan = Mock(side_effect=pause)
        runtime.store.add_event(Event("USER_REQUEST", {"message": "work"}))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.DEFERRED)
        self.assertEqual(task.result["continuation_reason"], "shutdown_requested")
        self.assertFalse(task.result["evidence"]["budget_deferred"])
        self.assertEqual(runtime.store.count_pending_events(), 1)
        self.assertEqual((self.settings.workspace / "saved.txt").read_text(), "kept")
        resumed = self.runtime()
        resumed.controller.plan = Mock(return_value=Plan("stop", [], done=True))
        resumed.run_once()
        done = resumed.store.get_task(task.id)
        self.assertEqual(done.status, TaskStatus.STOPPED)
        self.assertEqual(done.attempts, 1)
        self.assertEqual(done.result["evidence"]["task_cycles"], 2)
        self.assertEqual(done.result["evidence"]["task_tool_calls"], 1)

    def test_unlimited_cycle_uses_fresh_context_liveness_boundary(self):
        self.settings.runtime.liveness_checkpoint_rounds = 2
        runtime = self.runtime()
        runtime.controller.plan = Mock(side_effect=[
            Plan("step one", [Action("write", {"path": "one.txt", "content": "1"})], done=False),
            Plan("step two", [Action("write", {"path": "two.txt", "content": "2"})], done=False),
        ])
        runtime.store.add_event(Event("USER_REQUEST", {"message": "long work"}))

        runtime.run_once()

        deferred = runtime.store.list_tasks()[0]
        self.assertEqual(deferred.status, TaskStatus.DEFERRED)
        self.assertEqual(deferred.result["continuation_reason"], "runtime_liveness_checkpoint")
        self.assertFalse(deferred.result["evidence"]["budget_limits_enabled"])
        self.assertEqual(runtime.store.count_pending_events(), 1)
        traces = runtime.store.traces_for_cycles([deferred.result["cycle_id"]])
        self.assertTrue(any(item["kind"] == "liveness_checkpoint_requested" for item in traces))
        checkpoint = runtime.store.task_checkpoints(deferred.id)[-1]["data"]
        self.assertEqual(checkpoint["metrics"]["liveness_checkpoints"], 1)
        self.assertIn("_pending_self_reflection", checkpoint["working_state"])
        self.assertTrue((self.settings.workspace / "one.txt").is_file())
        self.assertTrue((self.settings.workspace / "two.txt").is_file())

    def test_failed_cycle_preserves_private_workspace_for_retry(self):
        runtime = self.runtime()
        runtime.controller.plan = Mock(side_effect=[
            Plan("create intermediate", [Action("write", {
                "path": "intermediate.txt", "content": "preserved",
            })], done=False),
            ControllerError("invalid model response"),
        ])
        runtime.store.add_event(Event("USER_REQUEST", {"message": "multi-step work"}))

        runtime.run_once()

        task = runtime.store.list_tasks()[0]
        self.assertEqual(task.status, TaskStatus.RETRYING)
        self.assertFalse((self.settings.workspace / "intermediate.txt").exists())
        private_file = (
            self.settings.sandbox_root / "task_workspaces"
            / f"task_{task.id}" / "intermediate.txt"
        )
        self.assertEqual(private_file.read_text(encoding="utf-8"), "preserved")
        reset = [
            item for item in runtime.store.task_checkpoints(task.id)
            if item["phase"] == "retry_reset"
        ][-1]
        self.assertTrue(reset["data"]["preserve_working_state"])

        resumed = self.runtime()
        resumed.controller.plan = Mock(return_value=Plan("stop", [], done=True))
        resumed.run_once()
        finished = resumed.store.get_task(task.id)
        self.assertEqual(finished.status, TaskStatus.STOPPED)
        self.assertEqual(
            (self.settings.workspace / "intermediate.txt").read_text(encoding="utf-8"),
            "preserved",
        )
        self.assertFalse(private_file.exists())

    def test_old_checkpoint_limits_do_not_reenable_budget(self):
        runtime = self.runtime()
        task_id = runtime.store.create_task(Task("old", "old"))
        runtime.store.add_checkpoint(task_id, "budget_deferred", {"budget": {
            "enabled": True, "used_model_calls": 24, "used_tool_calls": 32,
            "used_tokens": 300000, "used_cycles": 6,
        }})
        budget = runtime._task_budget(task_id)
        self.assertFalse(budget.enabled)
        self.assertEqual(budget.used_cycles, 6)
        self.assertIsNone(budget.remaining()["cycles"])

    def test_no_action_yield_is_still_supported(self):
        runtime = self.runtime()
        runtime.controller.plan = Mock(return_value=Plan("pause", [], done=False))
        runtime.store.add_event(Event("USER_REQUEST", {"message": "work"}))
        runtime.run_once()
        self.assertEqual(runtime.store.list_tasks()[0].status, TaskStatus.YIELDED)

    def test_permissions_are_not_disabled(self):
        runtime = self.runtime()
        runtime.controller.plan = Mock(side_effect=[
            Plan("try", [Action("read", {"path": "../outside.txt"})], done=False),
            Plan("stop", [], done=True),
        ])
        runtime.store.add_event(Event("USER_REQUEST", {"message": "work"}))
        runtime.run_once()
        task = runtime.store.list_tasks()[0]
        self.assertFalse(task.result["action_results"][0]["ok"])
        self.assertIn("PermissionDenied", task.result["action_results"][0]["error"])

    def test_ui_shows_unlimited_and_keeps_historical_usage(self):
        runtime = self.runtime()
        app = AIOSWebApplication(self.settings, runtime.store)
        result = {"evidence": {"budget_limits_enabled": False, "model_tokens": 400000,
                               "model_api_calls": 30, "task_tool_calls": 40}}
        projected = app._budget(result, [])
        self.assertEqual(projected["tokens"], 400000)
        self.assertEqual(projected["tool_calls"], 40)
        self.assertIsNone(projected["token_limit"])
        self.assertIsNone(projected["model_call_limit"])
        self.assertIsNone(projected["tool_call_limit"])
        self.assertIsNone(projected["cycle_limit"])
        old = app._budget({"evidence": {"model_tokens": 99}}, [])
        self.assertTrue(old["enabled"])
        self.assertEqual(old["tokens"], 99)

    def test_ui_aggregates_lifetime_usage_from_all_cycle_traces(self):
        app = AIOSWebApplication(self.settings, self.runtime().store)
        traces = [
            {"cycle_id": "a", "kind": "plan_created", "data": {
                "model_usage": {"model_calls": 2, "total_tokens": 100},
            }},
            {"cycle_id": "a", "kind": "action_result", "data": {}},
            {"cycle_id": "b", "kind": "plan_created", "data": {
                "model_usage": {"model_calls": 1, "total_tokens": 50},
            }},
            {"cycle_id": "b", "kind": "action_result", "data": {}},
        ]
        projected = app._budget({}, [], traces)
        self.assertEqual(projected["scope"], "task_lifetime")
        self.assertEqual(projected["model_calls"], 3)
        self.assertEqual(projected["tokens"], 150)
        self.assertEqual(projected["tool_calls"], 2)
        self.assertEqual(projected["cycles"], 2)


if __name__ == "__main__":
    unittest.main()
