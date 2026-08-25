from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .capabilities import EvidenceContract
from .protocol import contains_serialized_tool_call
from .types import Action, ActionResult


class Verifier:
    """Layered deterministic verifier: execution, artifact, evidence, goal."""

    DEGRADATION_PATTERNS = (
        r"无法(?:真实|实时|执行|访问|完成)?", r"不具备.+能力", r"仅基于.+知识",
        r"非实时", r"未实际", r"建议.+环境", r"\bcannot\b", r"\bunable\b", r"not real[- ]time",
    )

    def verify(self, actions: list[Action], results: list[ActionResult], *, planned_count: int, task_done: bool = True, request: str = "", contract: EvidenceContract | None = None, final_output: str = "") -> dict[str, Any]:
        contract = contract or EvidenceContract.from_request(request, self._expected_artifacts(request))
        checks: list[dict[str, Any]] = []
        self._check(checks, "execution", "all_actions_executed", len(results) == planned_count, f"executed={len(results)} planned={planned_count}")
        failures = sum(not result.ok for result in results)
        recovered = failures > 0 and task_done
        self._check(
            checks,
            "execution",
            "tool_failures_recovered",
            failures == 0 or recovered,
            f"failures={failures}; recovered_by_observed_final_plan={recovered}",
        )

        writes = []
        for action, result in zip(actions, results, strict=False):
            if action.tool not in {"write", "write_file", "append_file", "edit"} or not result.ok or not isinstance(result.output, dict):
                continue
            path = result.output.get("path")
            if path:
                writes.append(Path(str(path)))
                self._check(checks, "artifact", "written_artifact_exists", Path(str(path)).is_file(), str(path))
        written_names = {path.name.lower() for path in writes}
        missing = [name for name in contract.artifacts if name.lower() not in written_names]
        if contract.artifacts:
            self._check(checks, "artifact", "requested_artifact_created", not missing, "missing=" + ",".join(missing) if missing else "all requested artifacts created")

        evidence_ok = True
        for requirement in contract.evidence:
            passed = self._has_evidence(requirement.kind, requirement.value, actions, results)
            evidence_ok = evidence_ok and passed
            self._check(checks, "evidence", requirement.kind, passed, requirement.value or "required")

        degraded = any(re.search(pattern, final_output, re.IGNORECASE | re.DOTALL) for pattern in self.DEGRADATION_PATTERNS)
        protocol_clean = not contains_serialized_tool_call(final_output)
        artifact_goal = bool(contract.artifacts) and not missing
        effective_done = task_done or artifact_goal
        self._check(checks, "goal", "task_declared_done", effective_done, "planner/artifact completion")
        self._check(checks, "goal", "not_degraded_substitute", not degraded, "answer admits a substituted or unavailable capability" if degraded else "no degradation marker")
        self._check(checks, "goal", "no_serialized_tool_protocol", protocol_clean, "final answer contains tool-call markup" if not protocol_clean else "natural-language final answer")

        passed = all(check["passed"] for check in checks)
        outcome = "completed" if passed else "degraded" if degraded else "retryable_failure"
        return {"passed": passed, "outcome": outcome, "degraded": degraded, "evidence_satisfied": evidence_ok, "checks": checks}

    @staticmethod
    def _check(checks: list[dict[str, Any]], layer: str, name: str, passed: bool, detail: str) -> None:
        checks.append({"layer": layer, "name": name, "passed": passed, "detail": detail})

    @staticmethod
    def _has_evidence(kind: str, value: str | None, actions: list[Action], results: list[ActionResult]) -> bool:
        pairs = [(action, result) for action, result in zip(actions, results, strict=False) if result.ok]
        searchable = " ".join(f"{action.arguments} {result.output}" for action, result in pairs).lower()
        if kind == "network_request":
            return any(action.tool == "bash" and re.search(r"\b(curl|wget)\b", str(action.arguments.get("command", ""))) for action, _ in pairs)
        if kind == "source_domain":
            return bool(value) and value.lower() in searchable and Verifier._has_evidence("network_request", None, actions, results)
        mapping = {"trace_query": "aiosctl.py traces", "dead_letter_query": "aiosctl.py dead-letters", "task_query": "aiosctl.py tasks", "memory_query": "aiosctl.py memory"}
        if kind in mapping:
            return mapping[kind] in searchable
        if kind == "command_success":
            return any(action.tool == "bash" and isinstance(result.output, dict) and result.output.get("exit_code") == 0 for action, result in pairs)
        if kind == "artifact":
            return bool(value) and value.lower() in searchable
        return False

    @staticmethod
    def _expected_artifacts(request: str) -> list[str]:
        request = request.replace("\\.", ".")
        generation_pattern = r"生成|创建|写入|保存|输出|create|generate|write|save"
        suffixes = r"(?:md|txt|json|csv|py|html|yaml|yml)"
        names: list[str] = []
        for match in re.finditer(generation_pattern, request, flags=re.IGNORECASE):
            segment = re.split(r"[。；;\n]", request[match.end():], maxsplit=1)[0]
            names.extend(re.findall(rf"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+\.{suffixes})", segment, flags=re.IGNORECASE))
            names.extend(re.findall(rf"[\"“]([^\"”]+\.{suffixes})[\"”]", segment, flags=re.IGNORECASE))
        return list(dict.fromkeys(names))
