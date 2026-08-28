from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .capabilities import EvidenceContract
from .protocol import contains_serialized_tool_call, is_skill_authoring_request
from .types import Action, ActionResult


class CompletionArbiter:
    """Resolve task outcome from measured, verified, declared, then heuristic signals."""

    AGENT_DEGRADATION_PATTERNS = (
        r"(?:我|本系统|本环境|当前系统|当前环境|该系统|该环境|运行环境)"
        r"[^\n。！？.!?]{0,32}(?:无法|不能|不具备|缺少)"
        r"[^\n。！？.!?]{0,32}(?:访问|读取|执行|连接|联网|完成|使用|获取|提供|能力|权限)",
        r"(?:由于|因为)[^\n。！？.!?]{0,48}(?:无法|不能)[^\n。！？.!?]{0,48}"
        r"(?:只能|改用|仅能|仅基于|近似|替代)",
        r"\b(?:i|we)\s+(?:cannot|can't|could not|am unable to|are unable to)\s+"
        r"(?:access|read|execute|connect|complete|use|retrieve|provide)\b",
        r"\b(?:current\s+)?(?:system|environment|runtime)\s+(?:cannot|can't|is unable to|lacks?)\b",
        r"\bunable to complete the task\b",
    )

    def decide(
        self,
        *,
        execution_satisfied: bool,
        evidence_satisfied: bool,
        task_declared_done: bool,
        protocol_valid: bool,
        capability_assessment: dict[str, Any] | None,
        completion_metadata: dict[str, Any] | None,
        final_output: str,
    ) -> dict[str, Any]:
        metadata = completion_metadata if isinstance(completion_metadata, dict) else {}
        assessment = capability_assessment if isinstance(capability_assessment, dict) else {}
        blocking = assessment.get("blocking") if isinstance(assessment.get("blocking"), list) else []
        authority = assessment.get("needs_authority") if isinstance(assessment.get("needs_authority"), list) else []
        declared_missing = metadata.get("missing_capabilities")
        if not isinstance(declared_missing, list):
            declared_missing = []
        missing = [
            str(item.get("name", item)) if isinstance(item, dict) else str(item)
            for item in [*blocking, *authority, *declared_missing]
        ]
        structured_degraded = bool(
            metadata.get("capability_degraded")
            or metadata.get("execution_blocked")
            or metadata.get("substitution_used")
            or metadata.get("quality") == "degraded"
            or missing
        )
        heuristic_matches = [
            match.group(0)
            for pattern in self.AGENT_DEGRADATION_PATTERNS
            for match in re.finditer(pattern, final_output, re.IGNORECASE)
        ]
        heuristic_degraded = bool(heuristic_matches)
        degraded = structured_degraded or heuristic_degraded
        claims_complete = bool(metadata.get("claims_complete", task_declared_done))
        completed = bool(
            task_declared_done
            and claims_complete
            and execution_satisfied
            and evidence_satisfied
            and protocol_valid
        )
        if completed and not degraded:
            outcome = "completed"
        elif degraded:
            outcome = "degraded"
        else:
            outcome = "retryable_failure"
        return {
            "completion": "complete" if completed else "incomplete",
            "evidence": "satisfied" if evidence_satisfied else "unsatisfied",
            "capability": "full" if not missing and not metadata.get("capability_degraded") else "degraded",
            "quality": "degraded" if degraded else "full",
            "protocol": "valid" if protocol_valid else "invalid",
            "outcome": outcome,
            "controller_claims_complete": claims_complete,
            "missing_capabilities": missing,
            "execution_blocked": bool(metadata.get("execution_blocked")),
            "substitution_used": bool(metadata.get("substitution_used")),
            "degradation_signal": (
                "structured" if structured_degraded else "language_heuristic" if heuristic_degraded else "none"
            ),
            "heuristic_matches": heuristic_matches,
            "reason": metadata.get("reason"),
        }


class Verifier:
    """Layered deterministic verifier: execution, artifact, evidence, goal."""

    def __init__(self, completion_arbiter: CompletionArbiter | None = None):
        self.completion_arbiter = completion_arbiter or CompletionArbiter()

    def verify(self, actions: list[Action], results: list[ActionResult], *, planned_count: int, task_done: bool = True, request: str = "", contract: EvidenceContract | None = None, final_output: str = "", capability_assessment: dict[str, Any] | None = None, completion_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
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
        if self._is_skill_authoring_request(request):
            valid_candidate, candidate_detail = self._valid_skill_candidate(actions, results)
            self._check(
                checks, "artifact", "valid_skill_candidate_package",
                valid_candidate, candidate_detail,
            )

        evidence_ok = True
        for requirement in contract.evidence:
            passed = self._has_evidence(requirement.kind, requirement.value, actions, results)
            evidence_ok = evidence_ok and passed
            self._check(checks, "evidence", requirement.kind, passed, requirement.value or "required")

        protocol_clean = not contains_serialized_tool_call(final_output)
        artifact_goal = bool(contract.artifacts) and not missing
        effective_done = task_done or artifact_goal
        execution_ok = len(results) == planned_count and (failures == 0 or recovered)
        decision = self.completion_arbiter.decide(
            execution_satisfied=execution_ok,
            evidence_satisfied=evidence_ok and not missing,
            task_declared_done=effective_done,
            protocol_valid=protocol_clean,
            capability_assessment=capability_assessment,
            completion_metadata=completion_metadata,
            final_output=final_output,
        )
        self._check(checks, "goal", "task_declared_done", effective_done, "planner/artifact completion")
        self._check(
            checks, "goal", "not_degraded_substitute", decision["quality"] == "full",
            "structured completion state reports degraded execution/substitution"
            if decision["degradation_signal"] == "structured"
            else "agent/environment degradation language detected"
            if decision["degradation_signal"] == "language_heuristic"
            else "no degradation signal",
        )
        self._check(checks, "goal", "no_serialized_tool_protocol", protocol_clean, "final answer contains tool-call markup" if not protocol_clean else "natural-language final answer")

        passed = decision["outcome"] == "completed" and all(check["passed"] for check in checks)
        return {
            "passed": passed,
            "outcome": decision["outcome"],
            "degraded": decision["quality"] == "degraded",
            "evidence_satisfied": evidence_ok and not missing,
            "result_vector": decision,
            "checks": checks,
        }

    @staticmethod
    def _is_skill_authoring_request(request: str) -> bool:
        return is_skill_authoring_request(request)

    @staticmethod
    def _valid_skill_candidate(
        actions: list[Action], results: list[ActionResult]
    ) -> tuple[bool, str]:
        packages: dict[str, dict[str, str]] = {}
        for action, result in zip(actions, results, strict=False):
            if action.tool not in {"write", "write_file"} or not result.ok:
                continue
            raw_path = str(action.arguments.get("path", "")).replace("\\", "/").strip("/")
            match = re.fullmatch(
                r"skill_candidates/([a-z][a-z0-9_]{1,63})/(manifest\.json|skill\.py)",
                raw_path,
            )
            content = action.arguments.get("content")
            if match and isinstance(content, str):
                packages.setdefault(match.group(1), {})[match.group(2)] = content
        errors: list[str] = []
        for name, files in packages.items():
            if {"manifest.json", "skill.py"} - files.keys():
                errors.append(f"{name}: missing manifest.json or skill.py")
                continue
            try:
                manifest = json.loads(files["manifest.json"])
                compile(files["skill.py"], "skill.py", "exec")
            except (json.JSONDecodeError, SyntaxError) as exc:
                errors.append(f"{name}: {type(exc).__name__}")
                continue
            valid = (
                isinstance(manifest, dict)
                and manifest.get("name") == name
                and manifest.get("entrypoint", "skill.py") == "skill.py"
                and re.fullmatch(r"\d+\.\d+\.\d+", str(manifest.get("version", ""))) is not None
                and isinstance(manifest.get("input_schema"), dict)
                and manifest["input_schema"].get("type") == "object"
                and "process.sandbox_exec" in manifest.get("required_capabilities", [])
                and isinstance(manifest.get("tests"), list)
                and len(manifest["tests"]) >= 1
            )
            if valid:
                return True, f"validated skill_candidates/{name}"
            errors.append(f"{name}: manifest contract failed")
        return False, "; ".join(errors) if errors else "no complete skill candidate package was written"

    @staticmethod
    def _check(checks: list[dict[str, Any]], layer: str, name: str, passed: bool, detail: str) -> None:
        checks.append({"layer": layer, "name": name, "passed": passed, "detail": detail})

    @staticmethod
    def _has_evidence(kind: str, value: str | None, actions: list[Action], results: list[ActionResult]) -> bool:
        pairs = [(action, result) for action, result in zip(actions, results, strict=False) if result.ok]
        searchable = " ".join(f"{action.arguments} {result.output}" for action, result in pairs).lower()
        if kind == "network_request":
            clients = r"\b(?:curl|wget)\b|urllib\.request|http\.client|requests\.(?:get|post|request)"
            return any(
                action.tool == "bash"
                and isinstance(result.output, dict)
                and result.output.get("exit_code") == 0
                and re.search(clients, str(action.arguments.get("command", "")), re.IGNORECASE)
                for action, result in pairs
            )
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
