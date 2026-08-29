"""Host-owned semantic gate for Task 77: TaskBudget lifetime equals Attempt."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from aios.config import Settings
from aios.runtime import AIOSRuntime
from aios.types import Event, Task, TaskStatus


def _runtime(root: Path) -> AIOSRuntime:
    config = root / "config.json"
    config.write_text(json.dumps({
        "database": "data/gate.db",
        "workspace": "workspace",
        "model": {"provider": "mock"},
        "sandbox": {"backend": "docker", "root": "sandbox"},
        "skills": {"enabled": False, "root": "skills"},
        "experiments": {"root": "experiments"},
        "evolution": {"enabled": False, "retry_after_evolution": False},
        "budget": {
            "max_model_calls_per_cycle": 2,
            "max_tool_calls_per_cycle": 2,
            "max_model_calls_per_task": 24,
            "max_tool_calls_per_task": 32,
            "max_tokens_per_task": 300000,
            "max_cycles_per_task": 6,
        },
    }), encoding="utf-8")
    settings = Settings.load(config)
    settings.ensure_directories()
    return AIOSRuntime(settings)


def _spent(runtime: AIOSRuntime, task_id: int, *, near_exhaustion: bool = False) -> dict:
    config = runtime.settings.budget
    budget = {
        "max_model_calls": config.max_model_calls_per_task,
        "max_tool_calls": config.max_tool_calls_per_task,
        "max_tokens": config.max_tokens_per_task,
        "max_cycles": config.max_cycles_per_task,
        "used_model_calls": config.max_model_calls_per_task - (1 if near_exhaustion else 7),
        "used_tool_calls": config.max_tool_calls_per_task - (1 if near_exhaustion else 9),
        "used_tokens": config.max_tokens_per_task - (1 if near_exhaustion else 50000),
        "used_cycles": config.max_cycles_per_task - (1 if near_exhaustion else 2),
    }
    runtime.store.add_checkpoint(task_id, "budget_deferred", {
        "cycle_id": f"spent-{task_id}", "budget": budget,
        "working_state": {"objective": "gate"},
    })
    return budget


def _is_fresh(runtime: AIOSRuntime, task_id: int) -> bool:
    budget = runtime._task_budget(task_id)
    remaining = budget.remaining()
    return (
        budget.used_model_calls == 0
        and budget.used_tool_calls == 0
        and budget.used_tokens == 0
        and budget.used_cycles == 0
        and remaining == {
            "model_calls": budget.max_model_calls,
            "tool_calls": budget.max_tool_calls,
            "tokens": budget.max_tokens,
            "cycles": budget.max_cycles,
        }
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        runtime = _runtime(Path(directory))

        continuation_id = runtime.store.create_task(Task("continuation", "continue work"))
        spent = _spent(runtime, continuation_id)
        carried = runtime._task_budget(continuation_id)
        same_attempt_carries = (
            carried.used_model_calls == spent["used_model_calls"]
            and carried.used_tool_calls == spent["used_tool_calls"]
            and carried.used_tokens == spent["used_tokens"]
            and carried.used_cycles == spent["used_cycles"]
        )

        manual_id = runtime.store.create_task(Task("manual retry", "retry work"))
        _spent(runtime, manual_id)
        runtime.store.retry_task(manual_id)
        manual_retry_fresh = _is_fresh(runtime, manual_id)

        automatic_id = runtime.store.create_task(Task("automatic retry", "retry work"))
        automatic_task = runtime.store.start_task_attempt(automatic_id)
        _spent(runtime, automatic_id, near_exhaustion=True)
        before_retry_events = runtime.store.count_pending_events()
        runtime._handle_failure(
            automatic_task,
            Event("TASK_REQUEST", {"task_id": automatic_id, "message": automatic_task.request}),
            [], "Verification failed: task_declared_done", "automatic-failure", {},
        )
        automatic_retry_fresh = _is_fresh(runtime, automatic_id)
        automatic_retry_scheduled = (
            runtime.store.count_pending_events() == before_retry_events + 1
            and runtime.store.get_task(automatic_id).status == TaskStatus.RETRYING
        )

        terminal_id = runtime.store.create_task(Task(
            "terminal retry limit", "stop after limit", max_attempts=1,
        ))
        terminal_task = runtime.store.start_task_attempt(terminal_id)
        _spent(runtime, terminal_id, near_exhaustion=True)
        before_terminal_events = runtime.store.count_pending_events()
        runtime._handle_failure(
            terminal_task,
            Event("TASK_REQUEST", {"task_id": terminal_id, "message": terminal_task.request}),
            [], "Verification failed: task_declared_done", "terminal-failure", {},
        )
        terminal_limit_honored = (
            runtime.store.count_pending_events() == before_terminal_events
            and runtime.store.get_task(terminal_id).status == TaskStatus.DEAD_LETTER
        )

        checks = {
            "same_attempt_continuation_carries_budget": same_attempt_carries,
            "manual_retry_starts_fresh_budget": manual_retry_fresh,
            "automatic_retry_starts_fresh_budget": automatic_retry_fresh,
            "near_exhausted_attempt_gets_full_retry_budget": automatic_retry_fresh,
            "automatic_retry_is_scheduled_once": automatic_retry_scheduled,
            "terminal_retry_limit_is_honored": terminal_limit_honored,
        }
        passed = all(checks.values())
        print(json.dumps({
            "passed": passed,
            "semantic_invariant": "BudgetLifetime = Attempt",
            "checks": checks,
        }, ensure_ascii=False))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
