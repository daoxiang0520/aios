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
from aios.controller import ControllerError
from aios.evolution import EvolutionManager, EvolutionPolicyError
from aios.lineage import (
    TASK_STATUS_SEMANTICS, AutonomousLineageController, LineageManager,
    ModelComponentCandidateAuthor, ModelLineageReasoner,
)
from aios.runtime import AIOSRuntime
from aios.storage import StateStore
from aios.types import Event, Plan, Task, TaskStatus


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


class SuccessfulCandidateBroker:
    def __init__(self) -> None:
        self.calls = []

    def run_candidate(self, package, command, timeout_seconds):
        self.calls.append((package, command, timeout_seconds))
        return {"exit_code": 0, "stdout": '{"ok": true}\n', "stderr": ""}


class FixedComponentAuthor:
    def author(self, decision, facts, lineage_id):
        return {
            "candidate_id": "candidate_test",
            "kind": "skill",
            "objective": decision["objective"],
            "benchmark": {"passed": True},
            "adopted": False,
            "production_activated": False,
        }


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

    def test_author_component_candidate_does_not_adopt_or_fork(self) -> None:
        before = self.store.list_lineages()
        controller = AutonomousLineageController(
            self.store,
            FixedReasoner({
                "action": "AUTHOR_COMPONENT_CANDIDATE",
                "kind": "skill",
                "objective": "Create a reusable bounded text counter.",
                "reason": "Repeated tasks need the same procedure.",
                "evidence_task_ids": [],
            }),
            self.manager,
            component_authorer=FixedComponentAuthor(),
        )
        result = controller.decide("lin_root")
        self.assertEqual(result["effective_lineage_id"], "lin_root")
        self.assertEqual(result["component_candidate_id"], "candidate_test")
        self.assertTrue(result["component_candidate"]["benchmark"]["passed"])
        self.assertFalse(result["component_candidate_adopted"])
        self.assertFalse(result["production_activated"])
        self.assertEqual(self.store.list_lineages(), before)
        run = self.store.list_evolution_runs(1)[0]
        self.assertEqual(run["status"], "component_candidate_authored")
        event = self.manager.describe("lin_root")["events"][0]
        self.assertEqual(event["action"], "component_candidate_authored")
        self.assertFalse(event["data"]["adopted"])

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
        self.assertIn(
            "executable tool actions",
            contract["fields"]["max_actions_per_cycle"]["controls"],
        )
        memory_field = contract["fields"]["memory_context_characters"]
        self.assertIn("episodic-memory", memory_field["controls"])
        self.assertIn("model context window", memory_field["does_not_control"])
        self.assertIn("task token budget", memory_field["does_not_control"])
        prompt_field = contract["fields"]["prompt_append"]
        self.assertIn("network access", prompt_field["does_not_control"])
        profile_field = contract["fields"]["harness_profile"]
        self.assertEqual(
            set(profile_field["value_semantics"]),
            {"minimal_open", "reduced", "structured"},
        )
        availability = reasoner.facts["action_availability"]
        self.assertTrue(availability["FORK_MUTATION"]["available"])
        self.assertFalse(availability["FORK_MUTATION"]["depends_on_available_candidates"])
        self.assertFalse(availability["ADOPT_CANDIDATE"]["available"])
        self.assertFalse(availability["RETURN"]["available"])
        self.assertEqual(
            set(reasoner.facts["task_status_semantics"]),
            {status.value for status in TaskStatus},
        )
        self.assertIn("true completion was not judged", TASK_STATUS_SEMANTICS["stopped"])

    def test_historical_model_reason_is_hidden_from_next_model_call(self) -> None:
        repeated = {
            "action": "CONTINUE",
            "reason": "tasks are actively processing",
            "evidence_task_ids": [],
        }
        controller = AutonomousLineageController(self.store, FixedReasoner(repeated))
        controller.decide("lin_root")
        controller.decide("lin_root")

        capture = CapturingReasoner({"action": "CONTINUE", "reason": "reassess"})
        AutonomousLineageController(self.store, capture).decide("lin_root")
        facts = capture.facts

        self.assertNotIn("events", facts["current_lineage"])
        self.assertNotIn("decision", facts["current_lineage"])
        self.assertTrue(all(
            "decision" not in event["effective_data"]
            for event in facts["lineage_history"]
        ))
        self.assertNotIn("prior_model_rationales", facts)
        self.assertFalse(facts["projection_policy"]["historical_model_reason_visible"])
        self.assertNotIn(repeated["reason"], json.dumps(facts, ensure_ascii=False))

    def test_benchmarked_skill_component_can_enter_child_lineage_only(self) -> None:
        runtime = AIOSRuntime(self.settings)
        proposal = runtime.skills.propose({
            "name": "lineage_probe", "version": "1.0.0",
            "description": "Expose a test-only lineage procedure.",
            "required_capabilities": ["process.sandbox_exec"],
            "input_schema": {"type": "object"},
            "tests": [{"input": {}, "expect_exit": 0}],
            "origin": "agent",
        }, "print('{}')")
        candidate_id = proposal["candidate_id"]
        (runtime.skills.reports / f"{candidate_id}.json").write_text(json.dumps({
            "candidate_id": candidate_id, "skill": "lineage_probe",
            "version": "1.0.0", "passed": True, "checks": [],
        }), encoding="utf-8")
        controller = AutonomousLineageController(
            runtime.store,
            FixedReasoner({
                "action": "ADOPT_COMPONENT_CANDIDATE",
                "component_candidate_id": candidate_id,
                "reason": "test an isolated reusable procedure",
                "evidence_task_ids": [],
            }),
            manager=runtime.lineages,
        )
        result = controller.decide("lin_root")
        child = result["lineage"]
        member = next(
            item for item in child["component_set"]["members"]
            if item["name"] == "lineage_probe"
        )
        self.assertEqual(member["source"], {
            "type": "skill_candidate", "candidate_id": candidate_id,
        })
        self.assertEqual(child["mutation"]["component"]["operation"], "add")
        self.assertNotIn("lineage_probe", {item.name for item in runtime.skills.active_skills()})
        projection = runtime.skills.materialize_lineage_runtime(
            child["lineage_id"], child["component_set"],
        )
        self.assertTrue((projection / "active" / "lineage_probe" / "skill.py").is_file())
        catalog = runtime.skills.catalog_for_component_set(
            child["component_set"], runtime.capabilities,
        )
        self.assertIn("lineage_probe", {item["name"] for item in catalog})
        task_id = runtime.store.create_task(Task(
            "use lineage_probe", "use lineage_probe",
        ))
        runtime.store.bind_task_lineage(task_id, child["lineage_id"])
        runtime.store.add_event(Event(
            "TASK_REQUEST", {"task_id": task_id, "message": "use lineage_probe"},
        ))
        seen = []

        def plan(_intent, _goals, context):
            seen.append(context)
            return Plan("lineage component observed", [], done=True)

        runtime.controller.plan = Mock(side_effect=plan)
        runtime.run_once()
        procedures = seen[0]["situation_map"]["procedures"]
        self.assertIn("lineage_probe", {item["name"] for item in procedures})
        self.assertEqual(runtime.sandbox.skills_root, projection)

    def test_unbenchmarked_skill_component_cannot_enter_lineage(self) -> None:
        runtime = AIOSRuntime(self.settings)
        proposal = runtime.skills.propose({
            "name": "unsafe_probe", "version": "1.0.0",
            "description": "Candidate without a Host benchmark.",
            "required_capabilities": ["process.sandbox_exec"],
            "input_schema": {"type": "object"}, "tests": [], "origin": "agent",
        }, "print('{}')")
        with self.assertRaises(EvolutionPolicyError):
            runtime.lineages.adopt_component_candidate(
                "lin_root", proposal["candidate_id"], actor="agent",
                decision={"action": "ADOPT_COMPONENT_CANDIDATE"},
            )

    def test_component_contract_lists_open_and_closed_kinds(self) -> None:
        runtime = AIOSRuntime(self.settings)
        capture = CapturingReasoner({"action": "CONTINUE", "reason": "observe"})
        AutonomousLineageController(
            runtime.store, capture, manager=runtime.lineages,
        ).decide("lin_root")
        kinds = capture.facts["component_mutation_contract"]["component_kinds"]
        self.assertTrue(kinds["skill"]["lineage_adoption"])
        for kind in (
            "workflow", "plugin", "resource_adapter", "environment_provider",
            "primitive", "kernel_component",
        ):
            self.assertFalse(kinds[kind]["lineage_adoption"])

    def test_model_authors_redacted_isolated_skill_and_benchmarks_it(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.controller.config.provider = "deepseek"
        runtime.controller.config.max_tokens = 4096
        broker = SuccessfulCandidateBroker()
        captured = {}
        authored = {
            "kind": "skill",
            "manifest": {
                "name": "bounded_text_counter",
                "version": "1.0.0",
                "description": "Count a bounded text value.",
                "required_capabilities": ["process.sandbox_exec"],
                "input_schema": {"type": "object"},
                "tests": [{"input": {"text": "hello"}, "expect_exit": 0}],
            },
            "source": (
                "import argparse, json\n"
                "p=argparse.ArgumentParser()\n"
                "p.add_argument('--input-json', required=True)\n"
                "a=p.parse_args()\n"
                "v=json.loads(a.input_json)\n"
                "print(json.dumps({'count': len(v.get('text', ''))}))\n"
            ),
        }

        def response(request, _key):
            captured.update(request)
            return {
                "choices": [{"message": {"content": json.dumps(authored)}}],
                "usage": {"total_tokens": 123},
            }

        runtime.controller._send_request = response
        previous = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "test-only"
        try:
            result = ModelComponentCandidateAuthor(
                runtime.controller, runtime.skills, broker,
            ).author({
                "kind": "skill",
                "objective": "Create a reusable text counter.",
                "reason": "token=secret-value seen at C:\\Users\\15959\\private\\trace.json",
                "evidence_task_ids": [7],
            }, {
                "lineage_tasks": [{
                    "task_id": 7,
                    "status": "stopped",
                    "error": "Authorization: Bearer abc.def.ghi",
                    "summary": "Read C:\\Users\\15959\\private\\input.txt with sk-abcdefghijklmnop",
                    "metrics": {"model_tokens": 100},
                    "behavior_digest": {
                        "schema": "task_behavior_digest/v1",
                        "action_families": {"resource_read": 2},
                        "sequence_trace_refs": ["trace:21", "trace:23"],
                    },
                }],
            }, "lin_root")
        finally:
            if previous is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous

        outbound = captured["messages"][1]["content"]
        self.assertNotIn("secret-value", outbound)
        self.assertNotIn("abc.def.ghi", outbound)
        self.assertNotIn("sk-abcdefghijklmnop", outbound)
        self.assertNotIn("C:\\Users\\15959", outbound)
        self.assertIn("[REDACTED", outbound)
        self.assertEqual(
            json.loads(outbound)["evidence"][0]["behavior_digest"]["action_families"],
            {"resource_read": 2},
        )
        self.assertTrue(result["benchmark"]["passed"])
        self.assertFalse(result["adopted"])
        self.assertFalse(result["production_activated"])
        self.assertEqual(len(broker.calls), 1)
        candidate = runtime.skills.candidate_component(result["candidate_id"])
        stored = next(
            item for item in runtime.skills.list_candidates()
            if item["candidate_id"] == result["candidate_id"]
        )
        self.assertEqual(stored["manifest"]["origin"], "agent")
        self.assertEqual(candidate["lineage"]["source_task_ids"], [7])
        self.assertNotIn(
            "bounded_text_counter",
            {manifest.name for manifest in runtime.skills.active_skills()},
        )

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
        system_prompt = captured["messages"][0]["content"]
        self.assertIn("CONTINUE means only keep the current lineage unchanged", system_prompt)
        self.assertIn("does not execute, resume, or mark any task active", system_prompt)
        self.assertEqual(captured["max_tokens"], 4096)
        self.assertEqual(captured["thinking"], {"type": "disabled"})
        self.assertEqual(decision["action"], "CONTINUE")

    def test_thinking_lineage_reasoner_uses_configured_output_budget(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.controller.config.provider = "deepseek"
        runtime.controller.config.thinking = "enabled"
        runtime.controller.config.max_tokens = 31_296
        os.environ["OPENAI_API_KEY"] = "test-only"
        captured = {}

        def response(request, _key):
            captured.update(request)
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {
                        "reasoning_content": "private reasoning",
                        "content": '{"action":"CONTINUE"}',
                    },
                }],
            }

        runtime.controller._send_request = response
        try:
            decision = ModelLineageReasoner(runtime.controller).reason({"schema": "test"})
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
        self.assertEqual(captured["max_tokens"], 31_296)
        self.assertNotIn("temperature", captured)
        self.assertEqual(captured["thinking"], {"type": "enabled"})
        self.assertEqual(decision["action"], "CONTINUE")

    def test_empty_thinking_response_is_sanitized_protocol_failure(self) -> None:
        runtime = AIOSRuntime(self.settings)
        runtime.controller.config.provider = "deepseek"
        runtime.controller.config.thinking = "enabled"
        runtime.controller.config.max_tokens = 31_296
        os.environ["OPENAI_API_KEY"] = "test-only"
        runtime.controller._send_request = lambda _request, _key: {
            "choices": [{
                "finish_reason": "length",
                "message": {"reasoning_content": "secret chain", "content": ""},
            }],
        }
        try:
            with self.assertRaises(ControllerError) as raised:
                ModelLineageReasoner(runtime.controller).reason({"schema": "test"})
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
        error = str(raised.exception)
        self.assertIn("finish_reason=length", error)
        self.assertIn("content_chars=0", error)
        self.assertIn("reasoning_chars=12", error)
        self.assertNotIn("secret chain", error)

    def test_lineage_protocol_failure_is_recorded_without_state_change(self) -> None:
        class FailingReasoner:
            def reason(self, _facts):
                raise ControllerError("Lineage reasoner returned no valid final JSON")

        controller = AutonomousLineageController(self.store, FailingReasoner(), self.manager)
        result = controller.decide("lin_root")
        self.assertEqual(result["status"], "protocol_failed")
        self.assertEqual(result["effective_lineage_id"], "lin_root")
        self.assertFalse(result["lineage_changed"])
        self.assertIsNone(result["model_decision"])
        run = self.store.list_evolution_runs(1)[0]
        self.assertEqual(run["status"], "protocol_failed")
        self.assertEqual(len(self.store.list_lineages()), 1)


if __name__ == "__main__":
    unittest.main()
