from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock

from aios.cli import main
from aios.cli import _load as load_cli_state
from aios.config import Settings
from aios.evolution import EvolutionManager, EvolutionPolicyError
from aios.lineage import AutonomousLineageController, LineageManager, ModelLineageReasoner
from aios.runtime import AIOSRuntime
from aios.storage import StateStore
from aios.types import Event, Plan, Task


class FixedReasoner:
    def __init__(self, decision):
        self.decision = decision

    def reason(self, _facts):
        return dict(self.decision)


class CapturingReasoner(FixedReasoner):
    def __init__(self, decision):
        super().__init__(decision)
        self.facts = None

    def reason(self, facts):
        self.facts = facts
        return super().reason(facts)


class AutonomousLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({
            "database": "./data/test.db", "workspace": "./workspace",
            "runtime": {"completion_mode": "free"}, "model": {"provider": "mock"},
            "sandbox": {"backend": "local", "root": "./sandbox"},
            "permissions": {"allowed_tools": ["read", "write", "edit", "bash"]},
        }), encoding="utf-8")
        self.settings = Settings.load(self.config_path)
        self.store = StateStore(self.settings.database)
        self.store.initialize()
        self.manager = LineageManager(self.store)
        self.root_lineage = self.manager.ensure_root()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_root_is_stable_snapshot(self) -> None:
        self.assertEqual(self.manager.ensure_root()["lineage_id"], "lin_root")
        self.assertEqual(len(self.store.list_lineages()), 1)
        self.assertEqual(self.root_lineage["generation"], 0)

    def test_fork_inherits_settings_without_production_promotion(self) -> None:
        before = self.store.active_harness()
        child = self.manager.fork(
            "lin_root", {"max_actions_per_cycle": 3}, actor="agent",
            decision={"action": "FORK_MUTATION", "reason": "try a smaller action batch"},
        )
        self.assertEqual(child["generation"], 1)
        self.assertEqual(child["settings"]["max_actions_per_cycle"], 3)
        self.assertEqual(self.store.active_harness(), before)

    def test_candidate_can_be_adopted_only_into_child_lineage(self) -> None:
        before = self.store.active_harness()
        candidate_id = EvolutionManager(self.store).propose(
            {"prompt_append": "Inspect evidence before repeating an action."}, "adapt strategy",
        )
        child = self.manager.adopt_candidate(
            "lin_root", candidate_id, actor="agent",
            decision={"action": "ADOPT_CANDIDATE", "candidate_id": candidate_id},
        )
        self.assertEqual(child["source_candidate_id"], candidate_id)
        self.assertEqual(self.store.get_candidate(candidate_id)["status"], "proposed")
        self.assertEqual(self.store.active_harness(), before)

    def test_return_requires_an_ancestor_not_a_sibling(self) -> None:
        a = self.manager.fork(
            "lin_root", {"max_actions_per_cycle": 2}, actor="agent", decision={},
        )
        b = self.manager.fork(
            "lin_root", {"max_actions_per_cycle": 4}, actor="agent", decision={},
        )
        returned = self.manager.return_to(
            a["lineage_id"], "lin_root", actor="agent", decision={"action": "RETURN"},
        )
        self.assertEqual(returned["lineage_id"], "lin_root")
        with self.assertRaises(EvolutionPolicyError):
            self.manager.return_to(
                a["lineage_id"], b["lineage_id"], actor="agent", decision={},
            )

    def test_agent_fork_mutation_creates_candidate_and_child(self) -> None:
        controller = AutonomousLineageController(self.store, FixedReasoner({
            "action": "FORK_MUTATION", "mutation": {"memory_context_characters": 900},
            "reason": "bound the inherited context", "evidence_task_ids": [],
        }))
        result = controller.decide("lin_root")
        self.assertNotEqual(result["effective_lineage_id"], "lin_root")
        self.assertFalse(result["production_activated"])
        self.assertFalse(result["host_fitness_judgment"])
        self.assertEqual(len(self.store.list_candidates()), 1)
        self.assertEqual(
            self.manager.current()["lineage_id"], result["effective_lineage_id"],
        )

    def test_mock_agent_continues_current_lineage(self) -> None:
        controller = AutonomousLineageController(
            self.store, ModelLineageReasoner(AIOSRuntime(self.settings).controller),
        )
        result = controller.decide("lin_root")
        self.assertEqual(result["effective_lineage_id"], "lin_root")
        self.assertEqual(result["model_decision"]["action"], "CONTINUE")

    def test_reasoner_receives_executable_mutation_contract_without_candidate(self) -> None:
        reasoner = CapturingReasoner({"action": "CONTINUE", "reason": "observe"})
        AutonomousLineageController(self.store, reasoner).decide("lin_root")
        contract = reasoner.facts["mutation_contract"]
        self.assertTrue(contract["exactly_one_field"])
        self.assertFalse(contract["preexisting_candidate_required_for_fork"])
        self.assertEqual(set(contract["allowed_fields"]), {
            "prompt_append", "max_actions_per_cycle",
            "memory_context_characters", "harness_profile",
        })
        self.assertEqual(contract["fields"]["max_actions_per_cycle"]["maximum"], 20)
        availability = reasoner.facts["action_availability"]
        self.assertTrue(availability["FORK_MUTATION"]["available"])
        self.assertFalse(availability["FORK_MUTATION"]["depends_on_available_candidates"])
        self.assertFalse(availability["ADOPT_CANDIDATE"]["available"])
        self.assertFalse(availability["RETURN"]["available"])

    def test_bound_task_executes_with_lineage_harness(self) -> None:
        child = self.manager.fork(
            "lin_root", {"prompt_append": "LINEAGE_MARKER"}, actor="agent", decision={},
        )
        runtime = AIOSRuntime(self.settings)
        task_id = runtime.store.create_task(Task("branch task", "branch task"))
        runtime.store.bind_task_lineage(task_id, child["lineage_id"])
        runtime.store.add_event(Event(
            "TASK_REQUEST", {"task_id": task_id, "message": "branch task"},
        ))
        seen = []

        def plan(_intent, _goals, context):
            seen.append(context)
            return Plan("branch stopped", [], done=True)

        runtime.controller.plan = Mock(side_effect=plan)
        runtime.run_once()
        task = runtime.store.get_task(task_id)
        self.assertEqual(seen[0]["harness"]["prompt_append"], "LINEAGE_MARKER")
        self.assertEqual(seen[0]["lineage"]["lineage_id"], child["lineage_id"])
        self.assertEqual(task.result["lineage"]["lineage_id"], child["lineage_id"])
        trace = next(item for item in runtime.store.recent_traces(100) if item["kind"] == "lineage_bound")
        self.assertFalse(trace["data"]["production_default_changed"])

    def test_cli_submit_binds_and_show_projects_lineage(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            main([
                "--config", str(self.config_path), "task", "submit", "branch work",
                "--lineage", "lin_root",
            ])
        submitted = json.loads(output.getvalue())
        self.assertEqual(submitted["lineage"]["lineage_id"], "lin_root")
        output = io.StringIO()
        with redirect_stdout(output):
            main([
                "--config", str(self.config_path), "task", "show", str(submitted["task_id"]),
            ])
        shown = json.loads(output.getvalue())
        self.assertEqual(shown["lineage"]["lineage_id"], "lin_root")

    def test_immutable_surface_cannot_enter_a_lineage(self) -> None:
        with self.assertRaises(EvolutionPolicyError):
            self.manager.fork(
                "lin_root", {"sandbox": "disabled"}, actor="agent", decision={},
            )

    def test_cli_loads_ignored_local_api_key_without_printing_it(self) -> None:
        secret = "local-test-secret-value"
        (self.root / "api.key").write_text(secret, encoding="utf-8")
        previous = os.environ.pop("OPENAI_API_KEY", None)
        try:
            settings, _store = load_cli_state(str(self.config_path))
            self.assertEqual(settings.model.api_key_env, "OPENAI_API_KEY")
            self.assertEqual(os.environ.get("OPENAI_API_KEY"), secret)
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
            if previous is not None:
                os.environ["OPENAI_API_KEY"] = previous

    def test_lineage_reasoner_reserves_enough_tokens_for_reasoning_and_final_json(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.controller.config.provider = "deepseek"
        runtime.controller.config.max_tokens = 31_296
        os.environ["OPENAI_API_KEY"] = "test-only"
        captured = {}

        def response(request, _key):
            captured.update(request)
            return {"choices": [{"message": {"content": '{"action":"CONTINUE"}'}}]}

        runtime.controller._send_request = response
        try:
            decision = ModelLineageReasoner(runtime.controller).reason({"schema": "test"})
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
        self.assertEqual(captured["max_tokens"], 4096)
        self.assertEqual(captured["thinking"], {"type": "disabled"})
        self.assertEqual(decision["action"], "CONTINUE")


if __name__ == "__main__":
    unittest.main()
