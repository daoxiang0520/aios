from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aios.cli import _parser
from aios.controller import (
    LLMController,
    MINIMAL_OPEN_TOOL_SYSTEM_PROMPT,
    REDUCED_TOOL_SYSTEM_PROMPT,
    TOOL_SYSTEM_PROMPT,
)
from aios.evolution import EvolutionManager, EvolutionPolicyError
from aios.experiments import ExperimentOrchestrator
from aios.experiments.capsule import CapsuleManager
from aios.runtime import AIOSRuntime


class FakeCapsules:
    def __init__(self, root: Path):
        self.root = root
        self.deleted = []
        self.capsule = {
            "capsule_id": "cap_fixed",
            "initial_state_hash": "same-world",
            "fidelity": "full",
            "task": {"title": "fixture", "request": "produce result", "priority": 50},
            "workspace": {"manifest_hash": "workspace-hash"},
            "capabilities": {"available": ["filesystem.workspace"]},
            "model": {
                "provider": "mock", "model": "fixed-model", "temperature": 0.1,
                "max_tokens": 2048,
            },
            "environment": {"sandbox_backend": "docker", "network_enabled": False},
            "components": {"active_set_hash": "components-hash"},
        }

    def show(self, capsule_id):
        if capsule_id != "cap_fixed":
            raise KeyError(capsule_id)
        return self.capsule

    def verify_integrity(self, capsule_id):
        return {"valid": capsule_id == "cap_fixed"}

    def fork(self, capsule_id, world_id):
        world = self.root / world_id
        world.mkdir(parents=True, exist_ok=True)
        return {
            "root": str(world), "workspace": str(world / "workspace"),
            "skills_root": str(world / "skills"), "initial_state_hash": "same-world",
        }

    def delete_world(self, world):
        self.deleted.append(world["root"])


class ProfileRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, capsule, world, variant, replicate):
        profile = variant.mutation["harness_profile"]
        self.calls.append((profile, replicate, world["initial_state_hash"]))
        values = {
            "structured": (1000, 5, 3),
            "reduced": (800, 4, 2),
            "minimal_open": (1200, 6, 4),
        }
        tokens, calls, tools = values[profile]
        return {
            "outcome": {
                "task_status": "completed", "verifier_pass": True,
                "true_completion": True,
            },
            "cost": {
                "model_calls": calls, "tool_calls": tools, "cycles": 1,
                "tokens": tokens, "wall_time_ms": float(tokens),
            },
            "security": {"violations": []},
            "artifacts": {"workspace_hash": f"output-{profile}"},
            "final_output": f"result from {profile}",
            "adaptation": {
                "failed_tool_calls": 1, "failure_recovered": True,
                "post_failure_tool_changes": 1,
                "repeated_resource_actions": 0,
            },
        }


class V090Alpha3HarnessSensitivityTests(unittest.TestCase):
    def test_profiles_change_prompt_density_without_changing_tool_schema(self):
        self.assertIs(
            LLMController._tool_system_prompt({"harness": {"harness_profile": "structured"}}),
            TOOL_SYSTEM_PROMPT,
        )
        self.assertIs(
            LLMController._tool_system_prompt({"harness": {"harness_profile": "reduced"}}),
            REDUCED_TOOL_SYSTEM_PROMPT,
        )
        self.assertIs(
            LLMController._tool_system_prompt({"harness": {"harness_profile": "minimal_open"}}),
            MINIMAL_OPEN_TOOL_SYSTEM_PROMPT,
        )
        self.assertEqual(
            LLMController._prompt_append({
                "harness": {"harness_profile": "reduced", "prompt_append": "hidden scaffold"},
            }),
            "",
        )

    def test_context_profiles_are_deterministic_projections(self):
        context = {
            "workspace_inventory": ["a.py"], "harness": {}, "harness_version": 1,
            "budget": {"remaining": 1}, "observations": [], "round": 1,
            "_protocol_messages": [], "evidence_contract": {"artifacts": []},
            "continuation": {"active": True},
            "task_working_state": {
                "completed_steps": ["read"], "pending": ["write"],
                "unresolved_failures": [], "semantic": {"answer": "x"},
            },
            "environment": {"python": "ready"}, "situation_map": {"large": True},
            "retrieved_memories": [{"text": "old"}], "skill_authoring": {"required": True},
        }
        self.assertIs(
            AIOSRuntime._harness_context_projection(context, {"harness_profile": "structured"}),
            context,
        )
        reduced = AIOSRuntime._harness_context_projection(
            context, {"harness_profile": "reduced"},
        )
        minimal = AIOSRuntime._harness_context_projection(
            context, {"harness_profile": "minimal_open"},
        )
        self.assertIn("evidence_contract", reduced)
        self.assertIn("task_working_state", reduced)
        self.assertNotIn("situation_map", reduced)
        self.assertNotIn("retrieved_memories", reduced)
        self.assertEqual(
            minimal["task_working_state"],
            {"completed_steps": ["read"], "pending": ["write"], "unresolved_failures": []},
        )
        self.assertNotIn("evidence_contract", minimal)
        self.assertNotIn("environment", minimal)

    def test_only_named_harness_profiles_are_accepted(self):
        for profile in ("structured", "reduced", "minimal_open"):
            EvolutionManager._validate_mutation({"harness_profile": profile})
        with self.assertRaises(EvolutionPolicyError):
            EvolutionManager._validate_mutation({"harness_profile": "critic_swarm"})

    def test_sensitivity_matrix_uses_same_world_and_never_selects_winner(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MagicMock()
            capsules = FakeCapsules(Path(temp))
            runner = ProfileRunner()
            report = ExperimentOrchestrator(store, capsules, runner).run_harness_sensitivity(
                "cap_fixed", runs_per_profile=2,
            )
        self.assertTrue(report["same_initial_state"])
        self.assertEqual(report["only_variable"], "harness_profile")
        self.assertEqual(set(report["profiles"]), {
            "H0_structured", "H1_reduced", "H2_minimal_open",
        })
        self.assertEqual(len(runner.calls), 6)
        self.assertEqual({item[2] for item in runner.calls}, {"same-world"})
        self.assertIsNone(report["selection"]["winner"])
        self.assertIsNone(report["selection"]["promotion_state"])
        self.assertEqual(report["promotion_state"], "MEASUREMENT_ONLY")
        self.assertFalse(report["selection"]["single_scalar_reward"])
        self.assertEqual(report["aggregates"]["H1_reduced"]["median_tokens"], 800)
        self.assertEqual(report["deltas_vs_H0"]["H1_reduced"]["median_tokens"], -200)
        self.assertEqual(report["deltas_vs_H0"]["H2_minimal_open"]["median_tokens"], 200)
        summary = ExperimentOrchestrator.summarize_harness_sensitivity([report, report])
        self.assertEqual(summary["cases"], 2)
        self.assertEqual(
            summary["signed_effects_vs_H0"]["H1_reduced"]["median_tokens"]["negative"],
            2,
        )
        self.assertIsNone(summary["winner"])
        store.create_experiment.assert_called_once()
        store.add_experiment_variant.assert_called()
        store.add_experiment_run.assert_called()
        store.update_experiment.assert_called()

    def test_cli_exposes_capsule_bound_sensitivity_command(self):
        parsed = _parser().parse_args([
            "evolution", "harness-sensitivity",
            "--capsule", "cap_a", "--capsule", "cap_b", "--runs", "2",
        ])
        self.assertEqual(parsed.evolution_command, "harness-sensitivity")
        self.assertEqual(parsed.capsule, ["cap_a", "cap_b"])
        self.assertEqual(parsed.runs, 2)

    def test_experiment_world_cleanup_handles_nested_readonly_git_objects(self):
        with tempfile.TemporaryDirectory() as temp:
            worlds = Path(temp) / "worlds"
            target = worlds / "world_fixture" / "workspace" / ".git" / "objects" / "aa" / "object"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"snapshot")
            target.chmod(stat.S_IREAD)
            manager = object.__new__(CapsuleManager)
            manager.worlds = worlds.resolve()
            manager.delete_world({"root": str(worlds / "world_fixture")})
            self.assertFalse((worlds / "world_fixture").exists())


if __name__ == "__main__":
    unittest.main()
