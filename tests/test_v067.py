from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aios.capabilities import CapabilityRegistry
from aios.components import (
    ComponentKind,
    ComponentManifest,
    ComponentPolicyError,
    ComponentRegistry,
    build_component_registry,
)
from aios.config import Settings
from aios.experiments import CapsuleManager, ExperimentOrchestrator, ExperimentVariant
from aios.skills import SkillManager
from aios.storage import StateStore
from aios.tools import CORE_TOOL_SCHEMAS
from aios.types import Task


class V067UnifiedComponentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = root / "config.json"
        config.write_text(json.dumps({
            "database": "data/a.db", "workspace": "workspace",
            "model": {"provider": "mock"},
            "sandbox": {"backend": "docker", "root": "sandbox", "image": "sha256:test-fixture"},
            "skills": {"enabled": True, "root": "skills", "require_human_promotion": True},
            "experiments": {"root": "experiments"},
            "capabilities": {"network_enabled": False},
        }), encoding="utf-8")
        self.settings = Settings.load(config)
        self.settings.ensure_directories()
        self.store = StateStore(self.settings.database)
        self.store.initialize()
        self.skills = SkillManager(self.settings.skills_root, self.settings.skills)
        self.skills.bootstrap_builtins()
        self.authority = CapabilityRegistry.default(
            sandbox_available=True, network_enabled=False, scientific_available=True
        )
        self.registry = build_component_registry(
            self.authority, store=self.store, skill_manifests=self.skills.component_manifests()
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_skill_registry_is_a_compatible_component_projection(self) -> None:
        old_catalog = self.skills.catalog(self.authority)
        provider = self.registry.resolve_available_provider("procedure.workspace_search")
        self.assertIsNotNone(provider)
        self.assertEqual(provider["kind"], "skill")
        self.assertEqual(provider["metadata"]["name"], "workspace_search")
        canonical = next(item for item in self.skills.active_skills() if item.name == "workspace_search")
        projected = self.registry.get(kind="skill", name="workspace_search")
        self.assertEqual(projected["metadata"]["version"], canonical.version)
        self.assertEqual(projected["projection"], {
            "canonical_source": "skill_registry", "read_only": True,
        })
        self.assertTrue(next(item for item in old_catalog if item["name"] == "workspace_search")["available"])
        self.skills.deprecate("workspace_search", approved=True)
        self.registry.project_skills(self.skills.component_manifests())
        self.assertIsNone(self.registry.get(kind="skill", name="workspace_search"))
        restored = ComponentRegistry(self.authority, self.store)
        self.assertIsNone(restored.get(kind="skill", name="workspace_search"))

    def test_resource_and_environment_providers_resolve(self) -> None:
        xlsx = self.registry.resolve_available_provider("resource.xlsx.read")
        scientific = self.registry.resolve_available_provider("execution.python.scientific")
        self.assertEqual(xlsx["metadata"]["name"], "xlsx_reader")
        self.assertEqual(xlsx["kind"], "resource_adapter")
        self.assertEqual(xlsx["runtime"], {
            "plane": "environment", "isolation": "sandbox", "runner_kind": "adapter",
        })
        self.assertEqual(xlsx["spec"], {"adapter": "xlsx"})
        self.assertEqual(scientific["metadata"]["name"], "scientific-py312-v1")
        self.assertEqual(scientific["kind"], "environment_provider")

    def test_provider_existence_does_not_grant_network_authority(self) -> None:
        declared = self.registry.list_providers("network.external", effective_only=False)
        self.assertEqual(declared[0]["metadata"]["name"], "docker_network_bridge")
        self.assertFalse(declared[0]["resolution"]["authority_granted"])
        self.assertEqual(
            self.registry.resolve_provider("network.external")["metadata"]["name"],
            "docker_network_bridge",
        )
        self.assertIsNone(self.registry.resolve_available_provider("network.external"))

    def test_manifest_cannot_override_host_trust_policy(self) -> None:
        plugin = ComponentManifest.from_dict({
            "api_version": "aios/v1", "kind": "plugin",
            "metadata": {"name": "untrusted_plugin", "version": "1.0.0"},
            "capabilities": {"requires": [], "provides": ["procedure.untrusted_plugin"]},
            "runtime": {"plane": "host"},
            "evolution": {"mutable": True, "auto_promote": True},
        })
        record = self.registry.register(plugin, source="host")
        self.assertFalse(record["evolution"]["effective"]["mutable"])
        self.assertFalse(record["evolution"]["effective"]["auto_promote"])
        self.assertEqual(record["runtime"], {
            "plane": "host", "isolation": "sidecar", "runner_kind": "plugin",
        })
        self.assertEqual(record["trust_policy"]["isolation"], "sidecar")
        self.assertEqual(record["declared_runtime"], {"plane": "host"})
        with self.assertRaises(ComponentPolicyError):
            self.registry.register(plugin, source="agent")
        active_skill = ComponentManifest(
            ComponentKind.SKILL, "agent_active_bypass", "1.0.0",
            requires=("process.sandbox_exec",), provides=("procedure.agent_active_bypass",),
            status="active",
        )
        with self.assertRaises(ComponentPolicyError):
            self.registry.register(active_skill, source="agent")
        with self.assertRaises(ComponentPolicyError):
            self.registry.register(active_skill, source="host")
        candidate_skill = ComponentManifest(
            ComponentKind.SKILL, "agent_candidate", "1.0.0",
            requires=("process.sandbox_exec",), provides=("procedure.agent_candidate",),
            status="candidate",
        )
        candidate = self.registry.register(candidate_skill, source="agent")
        self.assertEqual(candidate["metadata"]["status"], "candidate")

    def test_component_registry_persists_graph_and_implications(self) -> None:
        restored = ComponentRegistry(self.authority, self.store)
        self.assertIsNotNone(restored.get("resource_adapter:xlsx_reader"))
        pdf = [
            item for item in restored.list_providers("resource.read", effective_only=False)
            if item["metadata"]["name"] == "pdf_reader"
        ]
        self.assertEqual(len(pdf), 1)
        graph = restored.graph()
        self.assertTrue(any(edge["capability"] == "resource.xlsx.read" for edge in graph["edges"]))

        # Names that merely share a prefix do not imply one another.
        restored.register(ComponentManifest(
            ComponentKind.WORKFLOW, "deep_resource", "1.0.0",
            provides=("resource.custom.deep",),
        ))
        self.assertIsNone(restored.resolve_provider("resource.custom"))

        # Multiple providers are legal; higher semantic version wins after
        # exactness and trust class, with stable tie-breakers after that.
        restored.register(ComponentManifest(
            ComponentKind.ENVIRONMENT_PROVIDER, "scientific_fixture_v2", "2.0.0",
            requires=("process.sandbox_exec",),
            provides=("execution.python.scientific",),
        ))
        self.assertEqual(
            restored.resolve_available_provider("execution.python.scientific")["metadata"]["name"],
            "scientific_fixture_v2",
        )

    def test_agent_tool_surface_remains_exactly_four_primitives(self) -> None:
        names = [item["function"]["name"] for item in CORE_TOOL_SCHEMAS]
        self.assertEqual(names, ["read", "write", "edit", "bash"])

    def test_experiment_schema_is_generic_but_runner_rejects_unopened_component_kind(self) -> None:
        variant = ExperimentVariant(
            "candidate", mutation_type="resource_adapter",
            mutation={"component_id": "cmp_fixture", "from_version": "1.0.0", "to_version": "1.1.0"},
        )
        normalized = variant.as_dict()["component_mutation"]
        self.assertEqual(normalized["component_kind"], "resource_adapter")
        self.assertEqual(normalized["component_id"], "cmp_fixture")
        orchestrator = ExperimentOrchestrator(self.store, None, lambda *args: {})  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "unsupported_mutation_kind:resource_adapter"):
            orchestrator.run(
                "unused", ExperimentVariant("baseline"), variant, runs_per_variant=1
            )

    def test_capsule_component_set_changes_initial_state_hash(self) -> None:
        task_id = self.store.create_task(Task("fixture", "analyze workspace"))
        capsules = CapsuleManager(
            self.settings, self.store, self.skills, self.authority, components=self.registry
        )
        first = capsules.capture(task_id)
        self.registry.register(ComponentManifest(
            ComponentKind.WORKFLOW, "host_workflow_fixture", "1.0.0",
            provides=("workflow.host_fixture",),
            runtime={"plane": "agent", "isolation": "sandbox"},
            evolution={"mutable": True},
        ))
        second = capsules.capture(task_id)
        self.assertNotEqual(
            first["components"]["active_set_hash"], second["components"]["active_set_hash"]
        )
        self.assertNotEqual(first["initial_state_hash"], second["initial_state_hash"])
        self.assertFalse(
            self.registry.get("workflow:host_workflow_fixture")["evolution"]["effective"]["mutable"]
        )


if __name__ == "__main__":
    unittest.main()
