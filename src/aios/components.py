from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable

from .capabilities import (
    CapabilityRegistry,
    CapabilityRequirement,
    CapabilityState,
    EvidenceContract,
)


class ComponentValidationError(ValueError):
    pass


class ComponentPolicyError(PermissionError):
    pass


class ComponentKind(StrEnum):
    PRIMITIVE = "primitive"
    SKILL = "skill"
    WORKFLOW = "workflow"
    RESOURCE_ADAPTER = "resource_adapter"
    ENVIRONMENT_PROVIDER = "environment_provider"
    PLUGIN = "plugin"
    KERNEL_COMPONENT = "kernel_component"


@dataclass(frozen=True, slots=True)
class TrustPolicy:
    runtime_plane: str
    isolation: str
    trust_class: str
    agent_create: bool
    agent_promote: bool
    mutable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "runtime_plane": self.runtime_plane,
            "isolation": self.isolation,
            "trust_class": self.trust_class,
            "agent_create": self.agent_create,
            "agent_promote": self.agent_promote,
            "mutable": self.mutable,
        }


@dataclass(frozen=True, slots=True)
class ComponentLineage:
    parent_version: str | None = None
    hypothesis: str | None = None
    mutation_reason: str | None = None
    source_task_ids: tuple[int, ...] = ()
    source_trace_ids: tuple[int, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ComponentLineage":
        return cls(
            parent_version=str(value["parent_version"]) if value.get("parent_version") else None,
            hypothesis=str(value["hypothesis"]) if value.get("hypothesis") else None,
            mutation_reason=str(value["mutation_reason"]) if value.get("mutation_reason") else None,
            source_task_ids=tuple(int(item) for item in value.get("source_task_ids", [])),
            source_trace_ids=tuple(int(item) for item in value.get("source_trace_ids", [])),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "parent_version": self.parent_version,
            "hypothesis": self.hypothesis,
            "mutation_reason": self.mutation_reason,
            "source_task_ids": list(self.source_task_ids),
            "source_trace_ids": list(self.source_trace_ids),
        }


@dataclass(frozen=True, slots=True)
class ComponentEvaluation:
    benchmark_delta: dict[str, Any] = field(default_factory=dict)
    replay_task_ids: tuple[int, ...] = ()
    cost: float | None = None
    historical_utility: float | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ComponentEvaluation":
        return cls(
            benchmark_delta=dict(value.get("benchmark_delta") or {}),
            replay_task_ids=tuple(int(item) for item in value.get("replay_task_ids", [])),
            cost=float(value["cost"]) if value.get("cost") is not None else None,
            historical_utility=(
                float(value["historical_utility"])
                if value.get("historical_utility") is not None else None
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "benchmark_delta": dict(self.benchmark_delta),
            "replay_task_ids": list(self.replay_task_ids),
            "cost": self.cost,
            "historical_utility": self.historical_utility,
        }


TRUST_POLICIES: dict[ComponentKind, TrustPolicy] = {
    ComponentKind.PRIMITIVE: TrustPolicy("kernel", "gateway", "kernel", False, False, False),
    ComponentKind.SKILL: TrustPolicy("agent", "sandbox", "sandboxed_agent", True, False, True),
    ComponentKind.WORKFLOW: TrustPolicy("agent", "sandbox", "governed", False, False, False),
    ComponentKind.RESOURCE_ADAPTER: TrustPolicy("environment", "sandbox", "host_managed", False, False, False),
    ComponentKind.ENVIRONMENT_PROVIDER: TrustPolicy("environment", "sandbox", "host_managed", False, False, False),
    ComponentKind.PLUGIN: TrustPolicy("host", "sidecar", "plugin_sandbox", False, False, False),
    ComponentKind.KERNEL_COMPONENT: TrustPolicy("kernel", "trusted", "kernel", False, False, False),
}

RUNNER_KINDS = {
    ComponentKind.PRIMITIVE: "primitive",
    ComponentKind.SKILL: "skill",
    ComponentKind.WORKFLOW: "workflow",
    ComponentKind.RESOURCE_ADAPTER: "adapter",
    ComponentKind.ENVIRONMENT_PROVIDER: "provider",
    ComponentKind.PLUGIN: "plugin",
    ComponentKind.KERNEL_COMPONENT: "kernel",
}


CAPABILITY_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")
COMPONENT_NAME = re.compile(r"[a-z][a-z0-9_-]{1,63}")
SEMVER = re.compile(r"\d+\.\d+\.\d+")


@dataclass(frozen=True, slots=True)
class ComponentManifest:
    kind: ComponentKind
    name: str
    version: str
    description: str = ""
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    runtime: dict[str, Any] = field(default_factory=dict)
    spec: dict[str, Any] = field(default_factory=dict)
    interface: dict[str, Any] = field(default_factory=dict)
    evolution: dict[str, Any] = field(default_factory=dict)
    lineage: dict[str, Any] = field(default_factory=dict)
    evaluation: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    content_digest: str = ""
    api_version: str = "aios/v1"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ComponentManifest":
        metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
        capabilities = value.get("capabilities") if isinstance(value.get("capabilities"), dict) else {}
        kind_value = value.get("kind", metadata.get("kind", ""))
        try:
            kind = ComponentKind(str(kind_value))
        except ValueError as exc:
            raise ComponentValidationError(f"Unknown Component kind: {kind_value}") from exc
        runtime = dict(value.get("runtime") or {})
        spec = dict(value.get("spec") or {})
        if "entrypoint" in runtime and "entrypoint" not in spec:
            spec["entrypoint"] = runtime["entrypoint"]
        return cls(
            api_version=str(value.get("api_version", "aios/v1")),
            kind=kind,
            name=str(metadata.get("name", value.get("name", ""))),
            version=str(metadata.get("version", value.get("version", ""))),
            description=str(metadata.get("description", value.get("description", ""))),
            status=str(metadata.get("status", value.get("status", "active"))),
            requires=tuple(str(item) for item in capabilities.get("requires", value.get("requires", []))),
            provides=tuple(str(item) for item in capabilities.get("provides", value.get("provides", []))),
            runtime=runtime,
            spec=spec,
            interface=dict(value.get("interface") or {}),
            evolution=dict(value.get("evolution") or {}),
            lineage=dict(value.get("lineage") or {}),
            evaluation=dict(value.get("evaluation") or {}),
            content_digest=str(value.get("content_digest", "")),
        )

    @property
    def component_id(self) -> str:
        raw = f"{self.kind.value}:{self.name}".encode("utf-8")
        return "cmp_" + hashlib.sha256(raw).hexdigest()[:24]

    @property
    def version_id(self) -> str:
        return "cv_" + hashlib.sha256(self._canonical_material()).hexdigest()[:24]

    def validate(self) -> "ComponentManifest":
        if self.api_version != "aios/v1":
            raise ComponentValidationError("Component api_version must be aios/v1")
        if COMPONENT_NAME.fullmatch(self.name) is None:
            raise ComponentValidationError("Component name must be lowercase and stable")
        if SEMVER.fullmatch(self.version) is None:
            raise ComponentValidationError("Component version must use MAJOR.MINOR.PATCH")
        if self.status not in {"active", "candidate", "history", "deprecated", "disabled"}:
            raise ComponentValidationError(f"Unsupported Component status: {self.status}")
        invalid = [item for item in (*self.requires, *self.provides) if CAPABILITY_NAME.fullmatch(item) is None]
        if invalid:
            raise ComponentValidationError(f"Invalid capability names: {sorted(set(invalid))}")
        if not self.provides:
            raise ComponentValidationError("Component must provide at least one capability")
        return self

    def as_dict(self) -> dict[str, Any]:
        policy = TRUST_POLICIES[self.kind]
        declared_evolution = dict(self.evolution)
        effective_evolution = {
            "mutable": policy.mutable,
            "auto_candidate": policy.mutable and self.kind == ComponentKind.SKILL,
            "auto_promote": False,
        }
        return {
            "api_version": self.api_version,
            "manifest_schema": "component/v1.1",
            "kind": self.kind.value,
            "component_id": self.component_id,
            "version_id": self.version_id,
            "metadata": {
                "name": self.name,
                "version": self.version,
                "description": self.description,
                "status": self.status,
            },
            "capabilities": {"requires": list(self.requires), "provides": list(self.provides)},
            "runtime": {
                "plane": policy.runtime_plane,
                "isolation": policy.isolation,
                "runner_kind": RUNNER_KINDS[self.kind],
            },
            "spec": dict(self.spec),
            "declared_runtime": dict(self.runtime),
            "interface": dict(self.interface),
            "evolution": {"declared": declared_evolution, "effective": effective_evolution},
            "trust_policy": policy.as_dict(),
            "lineage": dict(self.lineage),
            "evaluation": dict(self.evaluation),
            "content_digest": self.content_digest or hashlib.sha256(self._canonical_material()).hexdigest(),
        }

    def _canonical_material(self) -> bytes:
        value = {
            "api_version": self.api_version,
            "kind": self.kind.value,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "requires": list(self.requires),
            "provides": list(self.provides),
            "runtime": self.runtime,
            "spec": self.spec,
            "interface": self.interface,
            "evolution": self.evolution,
            "lineage": self.lineage,
            "evaluation": self.evaluation,
            "status": self.status,
            "content_digest": self.content_digest,
        }
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


DEFAULT_IMPLICATIONS = {
    "resource.pdf.read": {"resource.read"},
    "resource.xlsx.read": {"resource.read"},
    "resource.csv.read": {"resource.read"},
    "resource.text.read": {"resource.read"},
    "resource.http.read": {"resource.read"},
    "execution.python.scientific": {"execution.python"},
}


class ComponentRegistry:
    """Host-owned Component index. Registration never grants runtime authority."""

    def __init__(self, authority: CapabilityRegistry, store: Any | None = None):
        self.authority = authority
        self.store = store
        self._versions: dict[tuple[str, str], dict[str, Any]] = {}
        self._active: dict[str, dict[str, Any]] = {}
        self._implications = {name: set(values) for name, values in DEFAULT_IMPLICATIONS.items()}
        if store is not None:
            for stronger, weaker in store.list_capability_implications():
                self._implications.setdefault(stronger, set()).add(weaker)
            for record in store.list_components():
                self._remember(record)

    def register(self, manifest: ComponentManifest | dict[str, Any], *, source: str = "host") -> dict[str, Any]:
        item = (manifest if isinstance(manifest, ComponentManifest) else ComponentManifest.from_dict(manifest)).validate()
        policy = TRUST_POLICIES[item.kind]
        if source == "agent" and not policy.agent_create:
            raise ComponentPolicyError(f"Agent cannot create Component kind={item.kind.value}")
        if source == "agent" and item.status == "active" and not policy.agent_promote:
            raise ComponentPolicyError(f"Agent cannot promote Component kind={item.kind.value}")
        if item.kind == ComponentKind.SKILL and item.status == "active" and source != "skill_registry":
            raise ComponentPolicyError("Active Skill Components must be projected from SkillRegistry")
        if source == "skill_registry" and item.kind != ComponentKind.SKILL:
            raise ComponentPolicyError("SkillRegistry may project only kind=skill")
        record = item.as_dict()
        record["projection"] = {
            "canonical_source": "skill_registry" if item.kind == ComponentKind.SKILL else "component_registry",
            "read_only": item.kind == ComponentKind.SKILL,
        }
        key = (item.component_id, item.version)
        existing = self._versions.get(key)
        if existing is not None and existing["version_id"] != record["version_id"]:
            if existing.get("manifest_schema") == "component/v1.1":
                raise ComponentValidationError("A Component version is immutable once registered")
            self._versions.pop(key, None)
            if self._active.get(item.component_id) is existing:
                self._active.pop(item.component_id, None)
        self._remember(record)
        if self.store is not None:
            self.store.upsert_component(record)
            for stronger, implied in self._implications.items():
                for weaker in implied:
                    self.store.add_capability_implication(stronger, weaker)
        return record

    def project_skills(self, manifests: Iterable[ComponentManifest]) -> None:
        """Reconcile the read-only Component view with canonical SkillRegistry state."""
        projected = list(manifests)
        current_ids = {manifest.component_id for manifest in projected}
        stale_ids = [
            component_id for component_id, record in self._active.items()
            if record["kind"] == ComponentKind.SKILL.value and component_id not in current_ids
        ]
        for component_id in stale_ids:
            record = self._active.pop(component_id)
            record["metadata"] = {**record["metadata"], "status": "deprecated"}
            if self.store is not None:
                self.store.update_component_status(component_id, "deprecated")
        for manifest in projected:
            self.register(manifest, source="skill_registry")

    def get(
        self,
        identifier: str | None = None,
        version: str | None = None,
        *,
        kind: ComponentKind | str | None = None,
        name: str | None = None,
    ) -> dict[str, Any] | None:
        kind_value = kind.value if isinstance(kind, ComponentKind) else kind
        if name is not None:
            matches = [
                value for value in self._active.values()
                if value["metadata"]["name"] == name
                and (kind_value is None or value["kind"] == kind_value)
            ]
            if len(matches) != 1:
                return None
            component_id = matches[0]["component_id"]
        elif identifier is not None:
            component_id = identifier
            if identifier not in self._active:
                matches = [
                    value for value in self._active.values()
                    if value["metadata"]["name"] == identifier
                    or f"{value['kind']}:{value['metadata']['name']}" == identifier
                ]
                if len(matches) != 1:
                    return None
                component_id = matches[0]["component_id"]
        else:
            return None
        if version is None:
            return self._active.get(component_id)
        return self._versions.get((component_id, version))

    def list(self, *, kind: ComponentKind | str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        kind_value = kind.value if isinstance(kind, ComponentKind) else kind
        values = list(self._active.values())
        if kind_value is not None:
            values = [item for item in values if item["kind"] == kind_value]
        if status is not None:
            values = [item for item in values if item["metadata"]["status"] == status]
        return sorted(values, key=lambda item: (item["kind"], item["metadata"]["name"]))

    def list_providers(self, capability: str, *, effective_only: bool = False) -> list[dict[str, Any]]:
        providers = []
        authority_known = capability in self.authority.as_dict()
        capability_granted = (
            not authority_known
            or self.authority.get(capability).state in {CapabilityState.AVAILABLE, CapabilityState.COMPOSABLE}
        )
        for item in self._active.values():
            provided = set(item["capabilities"]["provides"])
            matches = capability in provided or any(
                capability in self._closure(candidate) for candidate in provided
            )
            if not matches:
                continue
            requirements = [
                CapabilityRequirement(name, f"Required by Component {item['component_id']}")
                for name in item["capabilities"]["requires"]
            ]
            assessment = self.authority.assess(EvidenceContract(request="component resolution", capabilities=requirements))
            effective = bool(
                item["metadata"]["status"] == "active"
                and capability_granted
                and assessment["satisfied"]
            )
            provider = {
                **item,
                "resolution": {
                    "requested_capability": capability,
                    "exact": capability in provided,
                    "authority_granted": capability_granted,
                    "requirements": assessment,
                    "effective": effective,
                },
            }
            if effective or not effective_only:
                providers.append(provider)
        return sorted(providers, key=self._provider_rank)

    def resolve_provider(self, capability: str) -> dict[str, Any] | None:
        """Resolve declared supply without granting or implying runtime authority."""
        values = self.list_providers(capability, effective_only=False)
        return values[0] if values else None

    def resolve_available_provider(self, capability: str) -> dict[str, Any] | None:
        """Resolve a provider only after current Authority and requirements allow use."""
        values = self.list_providers(capability, effective_only=True)
        return values[0] if values else None

    def dependencies(self, identifier: str) -> list[dict[str, Any]]:
        component = self.get(identifier)
        if component is None:
            return []
        return [
            {"capability": capability, "providers": self.list_providers(capability, effective_only=False)}
            for capability in component["capabilities"]["requires"]
        ]

    def dependents(self, identifier: str) -> list[dict[str, Any]]:
        component = self.get(identifier)
        if component is None:
            return []
        provided = set(component["capabilities"]["provides"])
        return [
            item for item in self._active.values()
            if provided & set(item["capabilities"]["requires"])
        ]

    def graph(self) -> dict[str, Any]:
        edges = []
        for item in self.list():
            edges.extend(
                {"component_id": item["component_id"], "direction": direction, "capability": capability}
                for direction in ("requires", "provides")
                for capability in item["capabilities"][direction]
            )
        return {"components": self.list(), "edges": edges, "implications": self.implications()}

    def snapshot(self) -> dict[str, Any]:
        active = [
            f"{item['kind']}:{item['metadata']['name']}@{item['metadata']['version']}"
            for item in self.list(status="active")
        ]
        raw = json.dumps(active, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {"active": active, "active_set_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest()}

    def runtime_map(self) -> dict[str, Any]:
        capabilities = self.authority.as_dict()
        available = sorted(
            name for name, value in capabilities.items()
            if str(value.get("state")) in {CapabilityState.AVAILABLE.value, CapabilityState.COMPOSABLE.value}
        )
        constraints = {
            name: str(value.get("state")) for name, value in capabilities.items()
            if str(value.get("state")) not in {CapabilityState.AVAILABLE.value, CapabilityState.COMPOSABLE.value}
        }
        procedures = [
            capability for item in self.list(kind=ComponentKind.SKILL, status="active")
            for capability in item["capabilities"]["provides"] if capability.startswith("procedure.")
        ]
        return {
            "capabilities": {"available": available, "constraints": constraints},
            "relevant_procedures": sorted(procedures),
            "component_set_hash": self.snapshot()["active_set_hash"],
        }

    def add_implication(self, stronger: str, weaker: str) -> None:
        if CAPABILITY_NAME.fullmatch(stronger) is None or CAPABILITY_NAME.fullmatch(weaker) is None:
            raise ComponentValidationError("Invalid capability implication")
        self._implications.setdefault(stronger, set()).add(weaker)
        if self.store is not None:
            self.store.add_capability_implication(stronger, weaker)

    def implications(self) -> list[dict[str, str]]:
        return [
            {"stronger": stronger, "weaker": weaker}
            for stronger in sorted(self._implications)
            for weaker in sorted(self._implications[stronger])
        ]

    def _closure(self, capability: str) -> set[str]:
        seen: set[str] = set()
        pending = [capability]
        while pending:
            current = pending.pop()
            for implied in self._implications.get(current, set()):
                if implied not in seen:
                    seen.add(implied)
                    pending.append(implied)
        return seen

    def _remember(self, record: dict[str, Any]) -> None:
        component_id = str(record["component_id"])
        version = str(record["metadata"]["version"])
        self._versions[(component_id, version)] = record
        if record["metadata"]["status"] == "active":
            current = self._active.get(component_id)
            if current is None or self._version_key(version) >= self._version_key(current["metadata"]["version"]):
                self._active[component_id] = record

    @staticmethod
    def _provider_rank(item: dict[str, Any]) -> tuple[Any, ...]:
        policy = item["trust_policy"]
        trust = {"kernel": 0, "host_managed": 1, "plugin_sandbox": 2, "governed": 3, "sandboxed_agent": 4}
        evaluation = item.get("evaluation") or {}
        version = ComponentRegistry._version_key(item["metadata"]["version"])
        return (
            not item["resolution"]["exact"],
            trust.get(policy.get("trust_class"), 9),
            tuple(-part for part in version),
            float(evaluation.get("cost", 0.0) or 0.0),
            -float(evaluation.get("historical_utility", 0.0) or 0.0),
            item["component_id"],
        )

    @staticmethod
    def _version_key(value: str) -> tuple[int, int, int]:
        return tuple(int(item) for item in value.split("."))  # type: ignore[return-value]


def host_component_manifests() -> list[ComponentManifest]:
    values = [
        ComponentManifest(
            ComponentKind.PRIMITIVE, name, "1.0.0", f"Kernel-gated {name} primitive",
            provides=provides,
            runtime={"plane": "kernel", "isolation": "gateway", "runner_kind": "primitive"},
            spec={"entrypoint": name},
            evolution={"mutable": False},
        )
        for name, provides in (
            ("read", ("filesystem.read", "resource.read")),
            ("write", ("filesystem.write",)),
            ("edit", ("filesystem.write",)),
            ("bash", ("process.sandbox_exec", "execution.shell")),
        )
    ]
    values.extend([
        ComponentManifest(
            ComponentKind.RESOURCE_ADAPTER, "pdf_reader", "1.0.0", "Host-managed PDF observation adapter",
            requires=("process.sandbox_exec",), provides=("resource.pdf.read",),
            runtime={"plane": "environment", "isolation": "sandbox", "runner_kind": "adapter"},
            spec={"adapter": "pdf"},
            evolution={"mutable": False},
        ),
        ComponentManifest(
            ComponentKind.RESOURCE_ADAPTER, "xlsx_reader", "1.0.0", "Host-managed XLSX observation adapter",
            requires=("process.sandbox_exec",), provides=("resource.xlsx.read",),
            runtime={"plane": "environment", "isolation": "sandbox", "runner_kind": "adapter"},
            spec={"adapter": "xlsx"},
            evolution={"mutable": False},
        ),
        ComponentManifest(
            ComponentKind.RESOURCE_ADAPTER, "csv_reader", "1.0.0", "Host-managed CSV observation adapter",
            requires=("filesystem.read",), provides=("resource.csv.read",),
            runtime={"plane": "environment", "isolation": "sandbox", "runner_kind": "adapter"},
            spec={"adapter": "csv"},
            evolution={"mutable": False},
        ),
        ComponentManifest(
            ComponentKind.RESOURCE_ADAPTER, "http_reader", "1.0.0",
            "Host-managed governed HTTP observation adapter",
            requires=("network.external", "process.sandbox_exec"),
            provides=("resource.http.read",),
            runtime={"plane": "environment", "isolation": "sandbox", "runner_kind": "adapter"},
            spec={"adapter": "http", "schemes": ["http", "https"]},
            interface={"primitive": "read", "argument": "path", "form": "absolute_url"},
            evolution={"mutable": False},
        ),
        ComponentManifest(
            ComponentKind.ENVIRONMENT_PROVIDER, "scientific-py312-v1", "1.0.0",
            "Pinned reusable scientific Python environment",
            requires=("process.sandbox_exec",),
            provides=("execution.python", "execution.python.scientific"),
            runtime={"plane": "environment", "isolation": "sandbox", "runner_kind": "provider"},
            spec={"environment": "scientific-py312-v1"},
            evolution={"mutable": False},
        ),
        ComponentManifest(
            ComponentKind.ENVIRONMENT_PROVIDER, "docker_network_bridge", "1.0.0",
            "Host-governed external network transport",
            provides=("network.external",),
            runtime={"plane": "environment", "isolation": "sandbox", "runner_kind": "provider"},
            spec={"network_mode": "bridge"},
            evolution={"mutable": False},
        ),
    ])
    return values


def build_component_registry(
    authority: CapabilityRegistry,
    *,
    store: Any | None = None,
    skill_manifests: Iterable[ComponentManifest] = (),
    plugin_manifests: Iterable[ComponentManifest] = (),
) -> ComponentRegistry:
    registry = ComponentRegistry(authority, store)
    for manifest in host_component_manifests():
        registry.register(manifest, source="host")
    registry.project_skills(skill_manifests)
    for manifest in plugin_manifests:
        registry.register(manifest, source="host")
    return registry
