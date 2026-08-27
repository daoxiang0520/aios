from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from .protocol import is_skill_authoring_request


class CapabilityState(StrEnum):
    AVAILABLE = "available"
    COMPOSABLE = "composable"
    MISSING = "missing"
    NEEDS_AUTHORITY = "needs_authority"
    FORBIDDEN = "forbidden"


@dataclass(slots=True)
class Capability:
    name: str
    state: CapabilityState
    interface: str
    detail: str = ""
    policy: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CapabilityRequirement:
    name: str
    reason: str


@dataclass(slots=True)
class EvidenceRequirement:
    kind: str
    value: str | None = None
    minimum: int = 1


@dataclass(slots=True)
class EvidenceContract:
    request: str
    capabilities: list[CapabilityRequirement] = field(default_factory=list)
    evidence: list[EvidenceRequirement] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_request(cls, request: str, artifacts: list[str] | None = None) -> "EvidenceContract":
        text = request.casefold()
        capabilities: list[CapabilityRequirement] = []
        evidence: list[EvidenceRequirement] = []

        outside_workspace = (
            re.search(r"\.\.[\\/]", request) is not None
            or re.search(r"[A-Za-z]:[\\/]", request) is not None
            or re.search(r"/(?:etc|root|home|proc|sys|var|run|dev)(?:/|\b)", request) is not None
        )
        if outside_workspace:
            capabilities.append(
                CapabilityRequirement(
                    "filesystem.outside_workspace",
                    "Task explicitly requests a path outside the configured workspace",
                )
            )

        network_markers = (
            "网络", "联网", "网页", "在线", "最新", "实时", "arxiv", "github", "internet",
            "online", "web", "latest", "current",
        )
        if any(marker in text for marker in network_markers):
            capabilities.append(CapabilityRequirement("network.external", "Task requires external/current information"))
            evidence.append(EvidenceRequirement("network_request"))
        domains = re.findall(r"(?:https?://)?(?:www\.)?([a-z0-9-]+\.(?:org|com|net|io|cn))", text)
        if "arxiv" in text and "arxiv.org" not in domains:
            domains.append("arxiv.org")
        for domain in dict.fromkeys(domains):
            evidence.append(EvidenceRequirement("source_domain", domain))

        state_requirements = (
            (("trace", "追踪", "轨迹"), "state.trace_read", "trace_query"),
            (("死信", "dead letter", "dead-letter", "dlq"), "state.dead_letter_read", "dead_letter_query"),
            (("失败任务", "任务记录", "task history"), "state.task_read", "task_query"),
            (("记忆", "memory"), "state.memory_read", "memory_query"),
        )
        for markers, capability, evidence_kind in state_requirements:
            if any(marker in text for marker in markers):
                capabilities.append(CapabilityRequirement(capability, f"Task explicitly requires {evidence_kind}"))
                capabilities.append(CapabilityRequirement("process.sandbox_exec", "State CLI is accessed through sandbox bash"))
                evidence.append(EvidenceRequirement(evidence_kind))

        # A request about Python source files is still a filesystem task. Requiring
        # Docker merely because the language name appears creates a false preflight
        # block when read/list primitives are sufficient. Execution verbs retain
        # the strong-sandbox requirement.
        process_markers = ("运行", "执行", "测试", "pytest", "shell", "bash", "命令", "run ", "execute")
        if any(marker in text for marker in process_markers):
            capabilities.append(CapabilityRequirement("process.sandbox_exec", "Task requires executable commands"))
            skill_authoring = is_skill_authoring_request(request)
            if any(marker in text for marker in ("测试", "pytest", "test")) and not skill_authoring:
                evidence.append(EvidenceRequirement("command_success"))

        scientific_markers = (
            "建模", "数学模型", "优化模型", "数据分析", "回归", "拟合", "统计分析",
            "numpy", "pandas", "scipy", "statsmodels", "scientific python",
        )
        if any(marker in text for marker in scientific_markers):
            capabilities.append(CapabilityRequirement(
                "execution.python.scientific",
                "Task requires the governed scientific Python environment",
            ))
            capabilities.append(CapabilityRequirement("process.sandbox_exec", "Scientific computation runs in Docker"))

        output_artifacts = list(dict.fromkeys(artifacts or []))
        for artifact in output_artifacts:
            evidence.append(EvidenceRequirement("artifact", artifact))
        return cls(
            request=request,
            capabilities=_dedupe_capabilities(capabilities),
            evidence=_dedupe_evidence(evidence),
            artifacts=output_artifacts,
        )


class CapabilityRegistry:
    def __init__(self, capabilities: list[Capability]):
        self._capabilities = {capability.name: capability for capability in capabilities}

    @classmethod
    def default(
        cls,
        *,
        sandbox_available: bool,
        network_enabled: bool,
        allowed_domains: list[str] | None = None,
        scientific_available: bool | None = None,
    ) -> "CapabilityRegistry":
        domains = allowed_domains or []
        network_state = CapabilityState.AVAILABLE if network_enabled else CapabilityState.NEEDS_AUTHORITY
        sandbox_state = CapabilityState.AVAILABLE if sandbox_available else CapabilityState.MISSING
        scientific_state = (
            CapabilityState.AVAILABLE
            if sandbox_available and (network_enabled or scientific_available)
            else CapabilityState.NEEDS_AUTHORITY if sandbox_available else CapabilityState.MISSING
        )
        return cls(
            [
                Capability("filesystem.read", CapabilityState.AVAILABLE, "read", "Workspace snapshot only"),
                Capability(
                    "resource.read", CapabilityState.AVAILABLE, "read",
                    "Structured directory/text/CSV/ZIP observations; PDF/XLSX adapters run in Docker",
                    {"adapters": ["directory", "text", "csv", "zip", "pdf", "xlsx"]},
                ),
                Capability("filesystem.write", CapabilityState.AVAILABLE, "write/edit", "Workspace snapshot only"),
                Capability(
                    "filesystem.outside_workspace",
                    CapabilityState.FORBIDDEN,
                    "none",
                    "Host and parent paths are outside the Agent authority boundary",
                ),
                Capability("process.sandbox_exec", sandbox_state, "bash", "Docker sandbox required"),
                Capability(
                    "execution.python.scientific", scientific_state, "python",
                    "Pinned reusable numpy/pandas/scipy/statsmodels environment",
                    {"provider": "scientific-py312-v1", "reusable": True},
                ),
                Capability(
                    "network.external",
                    network_state,
                    "docker bridge egress" if network_enabled else "none",
                    (
                        "Unrestricted Docker egress is enabled by host policy"
                        if network_enabled
                        else "Network is disabled and requires host authority"
                    ),
                    {
                        "mode": "unrestricted" if network_enabled else "disabled",
                        "allowed_domains": domains,
                        "domain_allowlist_enforced": False,
                    },
                ),
                Capability("state.task_read", CapabilityState.COMPOSABLE, "python /aios-state/aiosctl.py tasks", "Read-only snapshot"),
                Capability("state.trace_read", CapabilityState.COMPOSABLE, "python /aios-state/aiosctl.py traces", "Read-only snapshot"),
                Capability("state.dead_letter_read", CapabilityState.COMPOSABLE, "python /aios-state/aiosctl.py dead-letters", "Read-only snapshot"),
                Capability("state.memory_read", CapabilityState.COMPOSABLE, "python /aios-state/aiosctl.py memory", "Read-only snapshot"),
                Capability("state.skill_usage_read", CapabilityState.COMPOSABLE, "python /aios-state/aiosctl.py skill-usage", "Read-only Skill telemetry snapshot"),
                Capability("credentials.agent_visible", CapabilityState.FORBIDDEN, "credential broker", "Secrets never enter sandbox"),
                Capability("deployment.production_promote", CapabilityState.NEEDS_AUTHORITY, "host promotion", "Host approval required"),
            ]
        )

    def get(self, name: str) -> Capability:
        return self._capabilities.get(
            name,
            Capability(name, CapabilityState.MISSING, "none", "Capability is not registered"),
        )

    def assess(self, contract: EvidenceContract) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for requirement in contract.capabilities:
            capability = self.get(requirement.name)
            effective_state = capability.state
            if capability.state == CapabilityState.COMPOSABLE:
                sandbox = self.get("process.sandbox_exec")
                if sandbox.state != CapabilityState.AVAILABLE:
                    effective_state = CapabilityState.MISSING
            items.append(
                {
                    "name": requirement.name,
                    "reason": requirement.reason,
                    "state": effective_state.value,
                    "registered_state": capability.state.value,
                    "interface": capability.interface,
                    "detail": capability.detail,
                }
            )
        blocking = [item for item in items if item["state"] in {"missing", "forbidden"}]
        authority = [item for item in items if item["state"] == "needs_authority"]
        return {
            "satisfied": not blocking and not authority,
            "requirements": items,
            "blocking": blocking,
            "needs_authority": authority,
        }

    def as_dict(self) -> dict[str, Any]:
        return {name: asdict(capability) for name, capability in self._capabilities.items()}


def _dedupe_capabilities(items: list[CapabilityRequirement]) -> list[CapabilityRequirement]:
    result: list[CapabilityRequirement] = []
    seen: set[str] = set()
    for item in items:
        if item.name not in seen:
            seen.add(item.name)
            result.append(item)
    return result


def _dedupe_evidence(items: list[EvidenceRequirement]) -> list[EvidenceRequirement]:
    result: list[EvidenceRequirement] = []
    seen: set[tuple[str, str | None]] = set()
    for item in items:
        key = (item.kind, item.value)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
