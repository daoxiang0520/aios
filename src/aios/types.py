from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    STALE = "stale"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    PAUSED = "paused"


class GoalType(StrEnum):
    SAFETY = "safety"
    USER = "user"
    SYSTEM = "system"
    EVOLUTION = "evolution"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    DEGRADED = "degraded"
    DEFERRED = "budget_deferred"
    BLOCKED_CAPABILITY = "blocked_capability"
    NEEDS_AUTHORITY = "needs_authority"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    NEEDS_REVIEW = "needs_review"
    STOPPED = "stopped"
    YIELDED = "yielded"
    ABANDONED = "abandoned"


class MemoryType(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


@dataclass(slots=True)
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 50
    id: int | None = None
    status: EventStatus = EventStatus.PENDING
    created_at: str | None = None


@dataclass(slots=True)
class Goal:
    title: str
    type: GoalType = GoalType.USER
    priority: int = 50
    metadata: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
    status: GoalStatus = GoalStatus.ACTIVE
    created_at: str | None = None


@dataclass(slots=True)
class Intent:
    name: str
    reason: str
    goal_id: int | None
    event_ids: list[int]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Action:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    call_id: str | None = None


@dataclass(slots=True)
class Plan:
    summary: str
    actions: list[Action]
    done: bool = False
    protocol_message: dict[str, Any] | None = None
    model_usage: dict[str, int] | None = None
    model_attributions: list[dict[str, Any]] = field(default_factory=list)
    completion_metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class ActionResult:
    tool: str
    ok: bool
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0


@dataclass(slots=True)
class Task:
    title: str
    request: str
    priority: int = 50
    max_attempts: int = 3
    id: int | None = None
    status: TaskStatus = TaskStatus.QUEUED
    attempts: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class Memory:
    type: MemoryType
    content: str
    key: str | None = None
    importance: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
    created_at: str | None = None
