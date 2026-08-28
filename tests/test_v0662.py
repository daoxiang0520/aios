from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from aios.config import Settings
from aios.controller import LLMController
from aios.runtime import AIOSRuntime
from aios.types import Action, Event, Intent, Plan, Task


class V0662ContextEfficiencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = root / "config.json"
        config.write_text(json.dumps({
            "database": "data/test.db", "workspace": "workspace",
            "model": {"provider": "mock"},
            "permissions": {"allowed_tools": ["read", "write", "edit", "bash"]},
            "sandbox": {"backend": "docker", "root": "sandbox"},
            "budget": {
                "max_model_calls_per_cycle": 1, "max_model_calls_per_task": 4,
                "max_tool_calls_per_cycle": 4, "max_tool_calls_per_task": 8,
                "soft_model_calls_per_task": 1,
            },
        }), encoding="utf-8")
        self.settings = Settings.load(config)
        self.settings.ensure_directories()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_controller_attributes_every_prompt_block_to_actual_tokens(self) -> None:
        config = self.settings.model
        config.provider = "deepseek"
        config.api_key_env = "TEST_CONTEXT_KEY"
        controller = LLMController(config)
        response = MagicMock()
        response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "done", "tool_calls": []}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010},
        }).encode("utf-8")
        response.__enter__.return_value = response
        context = {
            "workspace_inventory": {"files": [{"path": "x.xlsx"}]},
            "retrieved_memories": [{"content": "memory"}],
            "task_working_state": {"objective": "analyse", "pending": ["fit"]},
            "skills": [{"name": "workspace_search"}],
            "capabilities": {"resource.read": {"state": "available"}},
            "_protocol_messages": [{"role": "assistant", "content": "prior"}],
        }
        with patch.dict(os.environ, {"TEST_CONTEXT_KEY": "test-only"}), patch(
            "urllib.request.urlopen", return_value=response
        ):
            plan = controller.plan(Intent("task", "test", None, []), [], context)
        attribution = plan.model_attributions[0]
        self.assertEqual(attribution["actual_prompt_tokens"], 1000)
        self.assertEqual(
            sum(item["attributed_tokens"] for item in attribution["blocks"].values()), 1000
        )
        self.assertIn("working_state", attribution["blocks"])
        self.assertIn("history", attribution["blocks"])

    def test_continuation_restores_state_with_fresh_protocol_context(self) -> None:
        runtime = AIOSRuntime(self.settings)
        (self.settings.workspace / "input.txt").write_text("evidence", encoding="utf-8")
        seen = []

        def plan(intent, goals, context):
            seen.append(context)
            if len(seen) == 1:
                return Plan(
                    "read evidence", [Action("read", {"path": "input.txt"}, call_id="read1")],
                    done=False,
                    protocol_message={"role": "assistant", "content": None, "tool_calls": [{"id": "read1", "type": "function", "function": {"name": "read", "arguments": '{"path":"input.txt"}'}}]},
                )
            state = context["task_working_state"]
            self.assertEqual(context["_protocol_messages"], [])
            self.assertIn("input.txt", state["accessed_resources"])
            self.assertTrue(context["continuation"]["fresh_context"])
            self.assertIn("relevant_resources", context["workspace_inventory"])
            return Plan("complete", [], done=True)

        runtime.controller.plan = plan
        runtime.store.add_event(Event("USER_REQUEST", {"message": "分析 input.txt"}))
        runtime.run_once()
        runtime.run_once()
        self.assertEqual(runtime.store.list_tasks()[0].status.value, "completed")

    def test_hot_context_discards_old_protocol_rounds(self) -> None:
        messages = []
        for number in range(4):
            messages.extend([
                {"role": "assistant", "tool_calls": [{"id": f"c{number}"}]},
                {"role": "tool", "tool_call_id": f"c{number}", "content": str(number)},
            ])
        AIOSRuntime._retain_hot_protocol_rounds(messages, rounds=2)
        self.assertEqual([item.get("tool_call_id") for item in messages if item["role"] == "tool"], ["c2", "c3"])

    def test_retry_reset_starts_a_fresh_task_budget(self) -> None:
        runtime = AIOSRuntime(self.settings)
        task_id = runtime.store.create_task(Task("x", "x"))
        runtime.store.add_checkpoint(task_id, "budget_deferred", {
            "budget": {"used_model_calls": 3, "used_tool_calls": 2, "used_tokens": 9000, "used_cycles": 2},
            "working_state": {"objective": "old"},
        })
        runtime.store.retry_task(task_id)
        budget = runtime._task_budget(task_id)
        state = runtime._task_working_state(task_id, "new")
        self.assertEqual(budget.used_model_calls, 0)
        self.assertEqual(state["objective"], "new")


if __name__ == "__main__":
    unittest.main()
