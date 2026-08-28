from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class CapsuleStatus(StrEnum):
    CAPTURED = "captured"
    VALIDATED = "validated"
    REPLAYABLE = "replayable"
    ARCHIVED = "archived"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class CapsuleFidelity(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    METADATA_ONLY = "metadata_only"


class PromotionState(StrEnum):
    REJECTED = "REJECTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    PROMOTABLE = "PROMOTABLE"


class ReplayMode(StrEnum):
    STATE = "state"
    RECORDED = "recorded"
    EXECUTION = "execution"
    COUNTERFACTUAL = "counterfactual"


@dataclass(frozen=True, slots=True)
class ComponentMutation:
    component_id: str | None
    component_kind: str
    from_version: str | None = None
    to_version: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_legacy(cls, mutation_type: str, mutation: dict[str, Any]) -> "ComponentMutation":
        identifier = mutation.get("component_id") or mutation.get("candidate_id")
        return cls(
            component_id=str(identifier) if identifier is not None else None,
            component_kind=mutation_type,
            from_version=str(mutation["from_version"]) if mutation.get("from_version") else None,
            to_version=str(mutation["to_version"]) if mutation.get("to_version") else None,
            payload=dict(mutation),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExperimentVariant:
    name: str
    mutation_type: str = "skill"
    mutation: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["component_mutation"] = ComponentMutation.from_legacy(
            self.mutation_type, self.mutation
        ).as_dict()
        return value


@dataclass(frozen=True, slots=True)
class RunEvidence:
    run_id: str
    capsule_id: str
    variant: str
    replicate: int
    initial_state_hash: str
    outcome: dict[str, Any]
    cost: dict[str, Any]
    skills: dict[str, Any]
    security: dict[str, Any]
    artifacts: dict[str, Any]
    trace_id: str | None = None
    final_output: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
