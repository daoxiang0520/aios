"""Immutable external gate for Task 74's observed recovery-semantics defect."""

from aios.capabilities import EvidenceContract
from aios.evaluation import Verifier
from aios.types import Action, ActionResult


def main() -> int:
    verifier = Verifier()
    failed_compile = Action("bash", {"command": "g++ -O2 solution.cpp"})
    missing_compiler = ActionResult(
        "bash", False,
        {"exit_code": 127, "stdout": "", "stderr": "g++: command not found", "changes": []},
        "MissingExecutable: shell command was not found (exit 127)",
    )
    contract = EvidenceContract(request="Produce a complete and correct C++ solution")
    metadata = {
        "claims_complete": True, "quality": "full", "capability_degraded": False,
        "missing_capabilities": [], "execution_blocked": False,
        "substitution_used": False, "reason": None,
    }
    unresolved = verifier.verify(
        [failed_compile], [missing_compiler], planned_count=1, task_done=True,
        request=contract.request, contract=contract,
        final_output="The C++ code is complete and correct.",
        completion_metadata=metadata,
    )
    recovery_check = next(
        item for item in unresolved["checks"] if item["name"] == "tool_failures_recovered"
    )
    clean = verifier.verify(
        [], [], planned_count=0, task_done=True,
        request="Return a concise explanation",
        contract=EvidenceContract(request="Return a concise explanation"),
        final_output="Explanation complete.", completion_metadata=metadata,
    )
    passed = not unresolved["passed"] and not recovery_check["passed"] and clean["passed"]
    print({
        "passed": passed,
        "unresolved_failure_rejected": not unresolved["passed"],
        "recovery_check_passed": recovery_check["passed"],
        "clean_completion_preserved": clean["passed"],
    })
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
