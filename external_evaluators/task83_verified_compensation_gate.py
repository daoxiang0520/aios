"""Host-owned semantic gate for Task 83: compensation requires verified postconditions."""

from __future__ import annotations

import json

from aios.evaluation import Verifier
from aios.types import Action, ActionResult


REQUEST = "读取、复现并验证一个数据集"


def _failure_key(action: Action) -> str:
    return Verifier._action_key(action)


def _compensation(
    failed: Action,
    replacements: list[Action],
    *,
    required: list[str],
    verified: list[str],
) -> dict:
    return {
        "kind": "verified_action_compensation",
        "failed_action_key": _failure_key(failed),
        "replacement_action_keys": [_failure_key(action) for action in replacements],
        "required_postconditions": required,
        "verified_postconditions": verified,
        "evidence_refs": ["gate:download", "gate:extract", "gate:verify"],
    }


def _verify(
    actions: list[Action],
    results: list[ActionResult],
    *,
    established_evidence: list[dict] | None = None,
    unresolved_failures: list[dict] | None = None,
) -> dict:
    return Verifier().verify(
        actions,
        results,
        planned_count=len(actions),
        task_done=True,
        request=REQUEST,
        final_output="数据集已复现并通过完整性验证。",
        completion_metadata={"claims_complete": True, "quality": "full"},
        established_evidence=established_evidence,
        unresolved_failures=unresolved_failures,
    )


def _check(report: dict, name: str) -> bool:
    return next(
        bool(item["passed"])
        for item in report["checks"]
        if item["name"] == name
    )


def main() -> int:
    clone = Action("bash", {"command": "git clone https://example.invalid/dataset.git"})
    download = Action("bash", {"command": "python download_archive.py"})
    extract = Action("bash", {"command": "python extract_archive.py"})
    verify = Action("bash", {"command": "python verify_dataset.py"})
    unrelated = Action("bash", {"command": "python print_version.py"})

    failed = ActionResult("bash", False, {"exit_code": 1}, "clone failed")
    success = lambda: ActionResult("bash", True, {"exit_code": 0, "stdout": "PASS"})

    same_action = _verify([clone, clone], [failed, success()])
    full_compensation = _compensation(
        clone,
        [download, extract, verify],
        required=["repository_materialized", "dataset_integrity_verified"],
        verified=["repository_materialized", "dataset_integrity_verified"],
    )
    alternate = _verify(
        [clone, download, extract, verify],
        [failed, success(), success(), success()],
        established_evidence=[full_compensation],
    )
    no_proof = _verify([clone, unrelated], [failed, success()])
    partial = _verify(
        [clone, download, extract],
        [failed, success(), success()],
        established_evidence=[_compensation(
            clone,
            [download, extract],
            required=["repository_materialized", "dataset_integrity_verified"],
            verified=["repository_materialized"],
        )],
    )
    carried_failure = Verifier._unresolved_action_failures([clone], [failed])
    cross_cycle = _verify(
        [unrelated],
        [success()],
        unresolved_failures=carried_failure,
    )

    checks = {
        "same_action_success_resolves_failure": _check(same_action, "tool_failures_recovered"),
        "verified_equivalent_postconditions_resolve_failure": _check(
            alternate, "tool_failures_recovered"
        ),
        "unrelated_later_success_does_not_resolve_failure": not _check(
            no_proof, "tool_failures_recovered"
        ),
        "partial_postcondition_proof_does_not_resolve_failure": not _check(
            partial, "tool_failures_recovered"
        ),
        "uncompensated_cross_cycle_failure_remains_unresolved": not _check(
            cross_cycle, "tool_failures_recovered"
        ),
    }
    passed = all(checks.values())
    print(json.dumps({
        "passed": passed,
        "semantic_invariant": (
            "FailureResolved = SameActionSuccess OR VerifiedEquivalentPostconditions"
        ),
        "checks": checks,
        "fitness_boundary": {
            "command_similarity_is_evidence": False,
            "later_success_alone_is_evidence": False,
            "explicit_compensation_evidence_required": True,
            "all_required_postconditions_must_be_verified": True,
        },
    }, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
