from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aios.config import Settings
from aios.lineage import AutonomousLineageController, LineageManager
from aios.storage import StateStore
from aios.types import Task, TaskStatus


class ExecutionFactsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(root=root, database=root / "test.db", workspace=root / "workspace")
        self.store = StateStore(self.settings.database)
        self.store.initialize()
        self.manager = LineageManager(self.store)
        self.manager.ensure_root()

    def test_disabled_budget_preserves_configured_values_but_not_effective_caps(self):
        self.settings.budget.enabled = False
        facts = self.settings.execution_config_facts({"max_actions_per_cycle": 20, "memory_context_characters": 20000})
        self.assertEqual(facts["budget"]["configured"]["max_tool_calls_per_task"], 32)
        self.assertTrue(all(value is None for value in facts["budget"]["effective"].values()))
        action = facts["harness_effects"]["max_actions_per_cycle"]
        self.assertEqual(action["configured"], 20)
        self.assertFalse(action["active"])
        self.assertIsNone(action["effective"])
        self.assertFalse(facts["harness_effects"]["memory_context_characters"]["budget_switch_disables_this"])
        self.assertEqual(facts["retained_limits"]["command_max_timeout_seconds"], 300)

    def test_enabled_budget_composes_host_and_lineage_caps(self):
        facts = self.settings.execution_config_facts({"max_actions_per_cycle": 20})
        self.assertEqual(facts["budget"]["effective"]["max_actions_per_cycle"], 8)
        self.assertEqual(facts["budget"]["effective"]["max_model_calls_per_cycle"], 6)
        self.assertEqual(facts["harness_effects"]["max_actions_per_cycle"]["source"], "lineage")
        self.settings.max_actions_per_cycle = 3
        facts = self.settings.execution_config_facts({})
        self.assertEqual(facts["budget"]["effective"]["max_actions_per_cycle"], 3)
        self.assertEqual(facts["harness_effects"]["max_actions_per_cycle"]["source"], "host_default")

    def test_prompt_profile_and_secret_free_projection(self):
        self.settings.model.api_key_env = "PRIVATE_KEY_ENV"
        self.settings.model.base_url = "https://secret:password@example.invalid"
        facts = self.settings.execution_config_facts({"harness_profile": "minimal_open", "prompt_append": "private wording"})
        self.assertFalse(facts["harness_effects"]["prompt_append"]["active"])
        serialized = json.dumps(facts)
        for forbidden in ("PRIVATE_KEY_ENV", "password", "example.invalid", str(self.settings.root), "private wording"):
            self.assertNotIn(forbidden, serialized)

    def test_missing_settings_is_unknown_not_default_budget(self):
        facts = AutonomousLineageController(self.store, None, self.manager)._facts("lin_root", 20)
        self.assertFalse(facts["current_execution_config"]["available"])
        self.assertNotIn("budget", facts["current_execution_config"])

    def test_decision_receives_current_policy_and_separate_history_without_rewriting_runs(self):
        self.settings.budget.enabled = False
        self.settings.runtime.completion_mode = "free"
        child = self.manager.fork("lin_root", {"max_actions_per_cycle": 20}, actor="agent", decision={})
        for enabled in (None, True, False):
            task_id = self.store.create_task(Task("fixture", "fixture"))
            self.store.bind_task_lineage(task_id, child["lineage_id"])
            evidence = {"task_tool_calls": 137}
            if enabled is not None:
                evidence["budget_limits_enabled"] = enabled
            self.store.update_task(task_id, TaskStatus.STOPPED, result={"evidence": evidence})

        class Capture:
            def reason(self, facts):
                self.facts = facts
                return {"action": "CONTINUE", "reason": "No change selected", "evidence_task_ids": []}

        reasoner = Capture()
        controller = AutonomousLineageController(self.store, reasoner, self.manager, settings=self.settings)
        controller.decide(child["lineage_id"])
        frozen = self.store.list_evolution_runs(1)[0]
        facts = reasoner.facts
        self.assertFalse(facts["current_execution_config"]["budget"]["enabled"])
        self.assertEqual(facts["current_lineage"]["settings"]["max_actions_per_cycle"], 20)
        self.assertEqual([t["execution_policy"]["budget_limits_enabled"] for t in facts["lineage_tasks"]], [False, True, None])
        self.assertTrue(all(t["metrics"]["task_tool_calls"] == 137 for t in facts["lineage_tasks"]))
        self.settings.budget.enabled = True
        self.assertTrue(controller._facts(child["lineage_id"], 20)["current_execution_config"]["budget"]["enabled"])
        self.assertEqual(self.store.list_evolution_runs(1)[0], frozen)
        self.assertEqual(frozen["diagnosis"]["current_execution_config"], facts["current_execution_config"])


if __name__ == "__main__":
    unittest.main()
