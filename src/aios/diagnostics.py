from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .storage import StateStore


class Diagnoser:
    def __init__(self, store: StateStore):
        self.store = store

    def report(self, trace_limit: int = 500) -> dict[str, Any]:
        with self.store.connect() as connection:
            task_rows = connection.execute(
                "SELECT status,COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
            event_rows = connection.execute(
                "SELECT status,COUNT(*) AS n FROM events GROUP BY status"
            ).fetchall()
            dead_letters = int(
                connection.execute("SELECT COUNT(*) AS n FROM dead_letters").fetchone()["n"]
            )
            trace_rows = connection.execute(
                "SELECT kind,data FROM traces ORDER BY id DESC LIMIT ?", (trace_limit,)
            ).fetchall()

        tool_calls: Counter[str] = Counter()
        tool_failures: Counter[str] = Counter()
        cycle_failures: Counter[str] = Counter()
        durations: list[float] = []
        for row in trace_rows:
            data = json.loads(row["data"])
            if row["kind"] == "action_result":
                tool = str(data.get("tool", "unknown"))
                tool_calls[tool] += 1
                durations.append(float(data.get("duration_ms", 0.0)))
                if not data.get("ok"):
                    tool_failures[tool] += 1
            elif row["kind"] == "cycle_failed":
                error = str(data.get("error", "unknown")).split(":", 1)[0]
                cycle_failures[error] += 1

        task_counts = {str(row["status"]): int(row["n"]) for row in task_rows}
        total_terminal = sum(task_counts.get(key, 0) for key in ("completed", "failed", "dead_letter"))
        failure_total = task_counts.get("failed", 0) + task_counts.get("dead_letter", 0)
        recommendations: list[str] = []
        if dead_letters:
            recommendations.append("Inspect dead letters and classify repeated root causes.")
        if tool_failures:
            recommendations.append("Add verifier fixtures or permission rules for the most failing tool.")
        if not recommendations:
            recommendations.append("Collect more completed tasks before proposing a Harness mutation.")
        return {
            "tasks": task_counts,
            "events": {str(row["status"]): int(row["n"]) for row in event_rows},
            "dead_letters": dead_letters,
            "task_failure_rate": failure_total / total_terminal if total_terminal else 0.0,
            "tool_calls": dict(tool_calls),
            "tool_failures": dict(tool_failures),
            "average_tool_duration_ms": sum(durations) / len(durations) if durations else 0.0,
            "cycle_failure_types": dict(cycle_failures),
            "recommendations": recommendations,
        }
