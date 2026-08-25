from __future__ import annotations

from .storage import StateStore
from .types import Event, Goal, GoalType, Intent


TYPE_WEIGHT = {
    GoalType.SAFETY: 1_000,
    GoalType.USER: 300,
    GoalType.SYSTEM: 200,
    GoalType.EVOLUTION: 100,
}


class GoalManager:
    def __init__(self, store: StateStore):
        self.store = store

    def add(self, goal: Goal) -> int:
        return self.store.add_goal(goal)

    def active(self) -> list[Goal]:
        return self.store.list_goals(active_only=True)


class IntentArbiter:
    """Deterministic first-pass arbitration; safety and explicit user events win."""

    def select(self, events: list[Event], goals: list[Goal]) -> Intent | None:
        if not events:
            return None
        best_goal = max(
            goals,
            key=lambda goal: TYPE_WEIGHT[goal.type] + goal.priority,
            default=None,
        )
        event = max(events, key=lambda item: item.priority)
        explicit_goal_id = event.payload.get("goal_id")
        goal_id = int(explicit_goal_id) if explicit_goal_id is not None else (best_goal.id if best_goal else None)
        return Intent(
            name=event.type.lower(),
            reason=f"Event {event.type} selected at priority {event.priority}",
            goal_id=goal_id,
            event_ids=[int(item.id) for item in events if item.id is not None],
            payload={"event": event.payload, "event_type": event.type},
        )

