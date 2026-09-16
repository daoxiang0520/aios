from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aios.config import Settings
from aios.lineage import LineageManager
from aios.storage import StateStore
from aios.types import Task, TaskStatus
from aios.webui import AIOSWebApplication, WebUIError


class WebUITest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(root=root, database=root / "data.db", workspace=root / "workspace")
        self.settings.ensure_directories()
        self.store = StateStore(self.settings.database)
        self.store.initialize()
        self.app = AIOSWebApplication(self.settings, self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_submit_projects_task_without_running_it(self) -> None:
        submitted = self.app.submit_task({"request": "read notes and summarize"})
        task = self.store.get_task(submitted["task_id"])
        self.assertIsNotNone(task)
        self.assertEqual(task.status, TaskStatus.QUEUED)
        self.assertEqual(self.store.count_pending_events(), 1)
        detail = self.app.task_detail(submitted["task_id"])
        self.assertEqual(detail["chat"][0]["content"], "read notes and summarize")
        self.assertIsNone(detail["lineage"])

    def test_ui_can_submit_unbound_current_or_specific_lineage(self) -> None:
        bootstrap = self.app.bootstrap()
        self.assertEqual(bootstrap["experimental_lineage_head"], "lin_root")
        self.assertEqual(bootstrap["lineages"][0]["lineage_id"], "lin_root")

        unbound = self.app.submit_task({"request": "production control", "lineage_id": ""})
        current = self.app.submit_task({"request": "current branch", "lineage_id": "current"})
        child = LineageManager(self.store).fork(
            "lin_root", {"max_actions_per_cycle": 3}, actor="agent", decision={},
        )
        explicit = self.app.submit_task({
            "request": "specific branch", "lineage_id": child["lineage_id"],
        })

        self.assertIsNone(unbound["lineage"])
        self.assertEqual(current["lineage"]["lineage_id"], "lin_root")
        self.assertEqual(explicit["lineage"]["lineage_id"], child["lineage_id"])

    def test_invalid_ui_lineage_does_not_create_or_queue_task(self) -> None:
        before = len(self.store.list_tasks())
        with self.assertRaises(WebUIError):
            self.app.submit_task({"request": "bad branch", "lineage_id": "lin_missing"})
        self.assertEqual(len(self.store.list_tasks()), before)
        self.assertEqual(self.store.count_pending_events(), 0)

    def test_bootstrap_does_not_expose_model_secret(self) -> None:
        data = self.app.bootstrap()
        self.assertTrue(data["csrf_token"])
        self.assertNotIn("api_key", str(data))
        self.assertEqual(data["workspace_path"], str(self.settings.workspace))
        self.assertEqual(data["runtime_policy"]["mode"], "verified")
        self.assertTrue(data["runtime_policy"]["online_verifier"])

    def test_free_and_verified_completion_semantics_are_distinct(self) -> None:
        self.settings.runtime.completion_mode = "free"
        free_id = self.store.create_task(Task("free", "free request"))
        self.store.update_task(free_id, TaskStatus.STOPPED, result={
            "summary": "agent says done",
            "evidence": {
                "success": None,
                "online_verifier_enabled": False,
                "agent_declared_stop": True,
                "host_observed_completion": None,
                "verification": {"mode": "disabled", "passed": None},
            },
        })
        verified_id = self.store.create_task(Task("verified", "verified request"))
        self.store.update_task(verified_id, TaskStatus.COMPLETED, result={
            "summary": "verified answer",
            "evidence": {
                "success": True,
                "verification": {"passed": True},
            },
        })

        free = self.app.task_detail(free_id)["completion"]
        verified = self.app.task_detail(verified_id)["completion"]
        self.assertEqual(free["mode"], "free")
        self.assertEqual(free["label"], "FREE LOOP")
        self.assertFalse(free["true_completion_known"])
        self.assertIn("not judged", free["status_meaning"])
        self.assertEqual(verified["mode"], "verified")
        self.assertEqual(verified["label"], "VERIFIED LOOP")
        self.assertTrue(verified["true_completion_known"])
        self.assertTrue(verified["verifier_pass"])

    def test_workspace_preview_rejects_escape(self) -> None:
        with self.assertRaises(WebUIError) as context:
            self.app.file("../secret.txt")
        self.assertEqual(context.exception.status, 403)

    def test_evolution_keeps_model_intent_separate_from_host_decision(self) -> None:
        task_id = self.store.create_task(Task("failed", "request"))
        self.store.add_evolution_run(
            f"runtime:{task_id}", {}, [], "observed",
            {"task_id": task_id, "proposal": {
                "model_intended_decision": "PROPOSE", "decision": "NO_ACTION",
                "reason": "patch_causality_contract_failed",
            }},
        )
        run = self.app.evolution_runs(task_id)[0]
        self.assertEqual(run["model_intended_decision"], "PROPOSE")
        self.assertEqual(run["effective_host_decision"], "NO_ACTION")

    def test_ui_projects_self_recovery_as_a_distinct_runtime_mode(self) -> None:
        task_id = self.store.create_task(Task("recovering", "original request"))
        cycle_id = "failed-cycle"
        self.store.add_checkpoint(task_id, "failed_attempt", {
            "cycle_id": cycle_id, "error": "RuntimeError: failed",
        })
        incident_id = self.store.trace(cycle_id, "self_recovery_incident", {
            "task_id": task_id, "error": "RuntimeError: failed",
        })
        incident = {
            "incident_ref": f"trace:{incident_id}",
            "failed_self_version": "v000004",
            "recovery_base_version": "v000003",
            "parent_fallback_used": True,
        }
        self.store.add_checkpoint(task_id, "self_recovery_scheduled", incident)
        self.store.trace(cycle_id, "self_recovery_scheduled", {
            "task_id": task_id, **incident,
        })
        self.store.update_task(task_id, TaskStatus.RETRYING, error="failed")

        detail = self.app.task_detail(task_id)

        self.assertTrue(detail["self_recovery"]["observed"])
        self.assertEqual(detail["self_recovery"]["state"], "scheduled")
        self.assertEqual(detail["self_recovery"]["recovery_base_version"], "v000003")
        self.assertTrue(detail["self_recovery"]["parent_fallback_used"])
        recovery_activity = [
            item for item in detail["activity"] if item["actor"] == "RECOVERY"
        ]
        self.assertTrue(recovery_activity)
        self.assertIn("Self Recovery", recovery_activity[-1]["label"])

        self.store.trace(cycle_id, "self_recovery_failed", {
            "task_id": task_id,
            "incident_ref": f"trace:{incident_id}",
            "error": "ControllerError: recovery could not continue",
        })
        failed_detail = self.app.task_detail(task_id)
        self.assertEqual(failed_detail["self_recovery"]["state"], "failed")
        self.assertEqual(
            failed_detail["activity"][-1]["label"], "Self Recovery Failed",
        )

    def test_ui_correlates_model_reasoning_repeated_reads_and_self_changes(self) -> None:
        task_id = self.store.create_task(Task("observable", "inspect the same source"))
        cycle_id = "observable-cycle"
        self.store.add_checkpoint(task_id, "started", {
            "cycle_id": cycle_id, "attempt": 1,
        })
        self.store.trace(cycle_id, "plan_created", {
            "round": 1, "summary": "inspect the source", "done": False,
            "actions": [{
                "tool": "read", "arguments": {"path": "notes.md"},
                "reason": "initial inspection",
            }],
        })
        self.store.trace(cycle_id, "model_reasoning", {
            "task_id": task_id, "round": 2,
            "reasoning": "I need to confirm whether the file changed.",
            "reasoning_excerpt": "I need to confirm whether the file changed.",
            "source": "provider_reasoning_content", "truncated": False,
        })
        self.store.trace(cycle_id, "plan_created", {
            "round": 2, "summary": "confirm the source", "done": False,
            "actions": [{
                "tool": "read", "arguments": {"path": "notes.md"},
                "reason": "check for a newer content version",
            }],
        })
        self.store.trace(cycle_id, "repeated_resource_read", {
            "task_id": task_id, "round": 2, "path": "notes.md",
            "prior_evidence_ref": "trace:10",
        })
        self.store.trace(cycle_id, "observation_reused", {
            "task_id": task_id, "round": 2, "path": "notes.md",
            "source_observation_ref": "trace:10",
        })
        self.store.trace(cycle_id, "self_version_opened", {
            "task_id": task_id, "round": 2, "operation": "open",
            "version": "v000005", "parent_version": "v000004",
            "restart_required": False,
        })

        detail = self.app.task_detail(task_id)

        decision = detail["decision_log"]["entries"][-1]
        self.assertTrue(decision["reasoning_available"])
        self.assertIn("file changed", decision["reasoning"])
        repeated = detail["repetition_analysis"]
        self.assertEqual(repeated["repeated_requests"], 1)
        self.assertEqual(repeated["exact_repeated_actions"], 1)
        self.assertEqual(repeated["exact_action_entries"][0]["occurrence"], 2)
        self.assertEqual(repeated["observation_reuse_hits"], 1)
        self.assertEqual(repeated["entries"][0]["outcome"], "observation_reused")
        self.assertIn("newer content", repeated["entries"][0]["action_reason"])
        self.assertTrue(detail["self_changes"]["observed"])
        self.assertEqual(detail["self_changes"]["entries"][0]["version"], "v000005")

    def test_topbar_remains_visible_when_narrow_layout_scrolls(self) -> None:
        css = (self.app.assets / "app.css").read_text(encoding="utf-8")
        self.assertIn(".topbar{position:sticky;top:0;z-index:20}", css)
        self.assertIn("html,body{height:100%;overflow:hidden}", css)
        self.assertIn("height:calc(100dvh - 64px);overflow:hidden", css)
        self.assertIn("grid-template-columns:1fr", css)
        self.assertIn(".tasks-panel,.work-panel,.activity-panel{min-height:0;overflow:hidden}", css)
        self.assertIn(".composer{position:absolute}", css)
        self.assertIn(".task-header{flex:0 0 76px}.tabs{flex:0 0 44px}", css)
        self.assertIn(".tabs{overflow-x:auto;overflow-y:hidden", css)
        self.assertIn(".tabs button{flex:0 0 auto}", css)
        javascript = (self.app.assets / "app.js").read_text(encoding="utf-8")
        self.assertIn("taskTabs.scrollLeft+=e.deltaY", javascript)
        self.assertIn('button.scrollIntoView({behavior:"smooth"', javascript)

    def test_ui_visually_separates_runtime_policy_from_view_mode(self) -> None:
        html = (self.app.assets / "index.html").read_text(encoding="utf-8")
        css = (self.app.assets / "app.css").read_text(encoding="utf-8")
        javascript = (self.app.assets / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="runtime-policy"', html)
        self.assertIn('id="task-runtime-mode"', html)
        self.assertIn("this does not change Runtime policy", html)
        self.assertIn(".runtime-policy.free", css)
        self.assertIn(".runtime-policy.verified", css)
        self.assertIn(".status-pill.stopped", css)
        self.assertIn("Agent declaration, not a Host-verified completion claim", javascript)
        self.assertIn('stopped:"agent stopped"', javascript)
        self.assertIn('id="lineage-target"', javascript)
        self.assertIn('Production / Unbound', javascript)
        self.assertIn('Current experimental', javascript)
        self.assertIn('lineage_id:state.lineageChoice', javascript)
        self.assertIn('id="task-self-recovery"', javascript)
        self.assertIn("SELF RECOVERY", javascript)
        self.assertIn("activity-node ${a.actor}", javascript)
        self.assertIn(".runtime-policy.recovery", css)
        self.assertIn(".actor.RECOVERY", css)
        self.assertIn('data-tab="reasoning"', html)
        self.assertIn("renderReasoning(root)", javascript)
        self.assertIn("PROVIDER-SUPPLIED REASONING", javascript)
        self.assertIn("SELF VERSION ACTIONS", javascript)
        self.assertIn(".reasoning-card", css)


if __name__ == "__main__":
    unittest.main()
