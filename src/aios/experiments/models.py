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
class ExperimentVariant:
    name: str
    mutation_type: str = "skill"
    mutation: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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
