"""Host-owned semantic gate for Task 80: completion evidence is Attempt-scoped."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

from aios.config import Settings
from aios.evaluation import Verifier
from aios.runtime import AIOSRuntime
from aios.types import Action, ActionResult, Event, Plan, TaskStatus


ACTION_REQUEST = "根据 README.md 完成一个项目原型"


def _verify(
    request: str, actions: list[Action], results: list[ActionResult],
    final_output: str,
) -> dict:
    return Verifier().verify(
        actions,
        results,
        planned_count=len(actions),
        task_done=True,
        request=request,
        final_output=final_output,
        completion_metadata={"claims_complete": True, "quality": "full"},
    )


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
            "max_model_calls_per_cycle": 1,
            "max_tool_calls_per_cycle": 4,
            "max_model_calls_per_task": 4,
            "max_tool_calls_per_task": 8,
            "max_tokens_per_task": 300000,
            "max_cycles_per_task": 3
        }
    }), encoding="utf-8")
    settings = Settings.load(config)
    settings.ensure_directories()
    return AIOSRuntime(settings)


def _cross_cycle_unresolved_failure_is_not_completion() -> tuple[bool, dict]:
    with tempfile.TemporaryDirectory() as directory:
        runtime = _runtime(Path(directory))
        runtime.controller.plan = Mock(side_effect=[
            Plan(
                "Inspection found a failing prototype check.",
                [
                    Action("read", {"path": "README.md"}),
                    Action("bash", {"command": "python smoke_test.py"}),
                ],
                done=False,
            ),
            Plan(
                "I will verify the prototype now.\n```bash\npython smoke_test.py\n```",
                [],
                done=True,
                completion_metadata={"claims_complete": True, "quality": "full"},
            ),
        ])
        runtime.executor.execute = Mock(side_effect=[
            ActionResult("read", True, "README observed"),
            ActionResult(
                "bash", False,
                {"exit_code": 1, "stdout": "", "stderr": "AttributeError: missing API"},
                "Command exited with 1",
            ),
        ])
        runtime.store.add_event(Event("USER_REQUEST", {"message": ACTION_REQUEST}))
        runtime.run_once()
        first = runtime.store.list_tasks()[0]
        first_deferred = first.status == TaskStatus.DEFERRED
        runtime.run_once()
        final = runtime.store.get_task(int(first.id))
        traces = runtime.store.traces_for_cycles([
            str(item["data"].get("cycle_id"))
            for item in runtime.store.task_checkpoints(int(first.id))
            if item["data"].get("cycle_id")
        ])
        failure_observed = any(
            trace["kind"] == "action_result" and not trace["data"].get("ok")
            for trace in traces
        )
        passed = bool(
            first_deferred and failure_observed and final.status != TaskStatus.COMPLETED
        )
        return passed, {
            "first_cycle_deferred": first_deferred,
            "failure_path_observed": failure_observed,
            "final_status": final.status.value,
        }


def main() -> int:
    pure_language = _verify(
        "解释什么是事件驱动架构", [], [],
        "事件驱动架构通过事件的产生、路由和处理来解耦组件。",
    )
    zero_zero = _verify(
        ACTION_REQUEST, [], [], "项目原型已经完成。",
    )
    plan_only = _verify(
        ACTION_REQUEST, [], [],
        "接下来执行验证：\n```bash\npython smoke_test.py\n```",
    )
    observed_execution = _verify(
        ACTION_REQUEST,
        [Action("bash", {"command": "python smoke_test.py"})],
        [ActionResult("bash", True, {"exit_code": 0, "stdout": "ok", "stderr": ""})],
        "项目原型已经完成并通过验证。",
    )
    recovered_execution = _verify(
        ACTION_REQUEST,
        [
            Action("bash", {"command": "python smoke_test.py"}),
            Action("bash", {"command": "python smoke_test.py"}),
        ],
        [
            ActionResult("bash", False, {"exit_code": 1}, "Command exited with 1"),
            ActionResult("bash", True, {"exit_code": 0, "stdout": "ok", "stderr": ""}),
        ],
        "项目原型已经完成并通过验证。",
    )
    cross_cycle, cross_cycle_detail = _cross_cycle_unresolved_failure_is_not_completion()

    checks = {
        "pure_language_task_may_complete_without_actions": pure_language["passed"],
        "action_required_zero_zero_is_not_execution_evidence": not zero_zero["passed"],
        "planned_command_is_not_execution_evidence": not plan_only["passed"],
        "observed_successful_action_can_complete": observed_execution["passed"],
        "later_success_for_same_action_resolves_failure": recovered_execution["passed"],
        "same_attempt_unresolved_failure_survives_cycle_boundary": cross_cycle,
    }
    passed = all(checks.values())
    print(json.dumps({
        "passed": passed,
        "semantic_invariant": "CompletionEvidenceLifetime = Attempt",
        "checks": checks,
        "cross_cycle": cross_cycle_detail,
        "patch_reachability": {
            "public_paths_exercised": [
                "Verifier.verify",
                "CompletionArbiter.decide",
                "AIOSRuntime.run_once continuation"
            ],
            "failure_path_exercised": cross_cycle_detail["failure_path_observed"],
            "candidate_changes_failure_outcome": passed,
        },
    }, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
