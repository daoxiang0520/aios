from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aios.config import Settings
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

    def test_bootstrap_does_not_expose_model_secret(self) -> None:
        data = self.app.bootstrap()
        self.assertTrue(data["csrf_token"])
        self.assertNotIn("api_key", str(data))
        self.assertEqual(data["workspace_path"], str(self.settings.workspace))

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


if __name__ == "__main__":
    unittest.main()
