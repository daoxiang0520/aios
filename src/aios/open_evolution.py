from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from .config import ModelConfig, Settings
from .controller import ControllerError, LLMController, _normalized_usage
from .runtime_evolution import (
    RuntimeCandidateManager,
    RuntimeMutationPolicy,
    RuntimeMutationPolicyError,
)
from .storage import StateStore


OPEN_EVOLUTION_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "inspect_experience", "description": "Inspect one section of the immutable failure/friction experience.", "parameters": {"type": "object", "properties": {"section": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["section"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "read_observation", "description": "Reload a bounded range of a prior addressable Tool Result by its observation reference.", "parameters": {"type": "object", "properties": {"ref": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["ref"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "read_source", "description": "Read a bounded range from a candidate source file. Root-of-Trust files are readable but not editable.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_source", "description": "Search the failure-time candidate source for a literal or regular-expression pattern.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "regex": {"type": "boolean"}, "limit": {"type": "integer"}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "run_diagnostic", "description": "Run a diagnostic or test command in a networkless Docker container with the candidate repository mounted read-only.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout_seconds": {"type": "integer"}}, "required": ["command"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "edit_candidate", "description": "Replace one exact text occurrence in an allowed mutable Runtime file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "write_candidate_test", "description": "Create a new Python test under candidate_tests/.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "inspect_diff", "description": "Inspect the current bounded candidate diff summary and changed file contents.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "inspect_mutation_boundary", "description": "Inspect the immutable authority and mutation boundary.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "submit_candidate", "description": "End the session and submit the current candidate to the unchanged Host-owned external evaluator.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "no_action", "description": "End the session without a candidate when mutation is unsupported, unsafe, unnecessary, or outside authority.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"], "additionalProperties": False}}},
]


class OpenEvolutionBackend(Protocol):
    def respond(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


class ModelOpenEvolutionBackend:
    """Native tool-calling backend; no structured diagnosis or patch form is requested."""

    def __init__(self, config: ModelConfig):
        self.config = config
        self.controller = LLMController(config)

    def respond(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.config.provider == "mock":
            return {
                "content": None,
                "tool_calls": [{
                    "id": "mock_no_action", "type": "function",
                    "function": {"name": "no_action", "arguments": json.dumps({
                        "reason": "mock backend cannot perform an open evolution experiment",
                    })},
                }],
                "usage": {"model_calls": 1},
            }
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.config.api_key_env):
            raise ControllerError("model.api_key_env must be an environment-variable name")
        key = os.environ.get(self.config.api_key_env)
        if not key:
            raise ControllerError(f"Missing API key environment variable: {self.config.api_key_env}")
        request: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if self.config.provider == "deepseek":
            thinking = self.config.thinking if self.config.thinking in {"enabled", "disabled"} else "disabled"
            request["thinking"] = {"type": thinking}
        response = self.controller._send_request(request, key)
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ControllerError("Open evolution model returned an invalid response") from exc
        return {
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls") or [],
            "reasoning_content": message.get("reasoning_content"),
            "usage": _normalized_usage(response.get("usage")),
        }


class OpenEvolutionAgent:
    """Bounded observe-act-observe loop over an isolated Runtime candidate."""

    SYSTEM_PROMPT = """You are the Open Evolution Agent for AIOS.
Work interactively: inspect evidence and source, run diagnostic experiments, revise your hypothesis,
edit only the candidate when evidence supports a change, and run tests before submission. You are not
required to fill a diagnosis contract. Use submit_candidate only when the current diff is evidence-backed;
otherwise use no_action. Production, credentials, Root-of-Trust files, benchmark annotations, and external
evaluators are outside your authority. You cannot activate production or decide fitness. Do not merely
describe a tool call: use the native tools. A failed experiment is evidence and may justify changing course.
"""

    def __init__(
        self, settings: Settings, store: StateStore,
        backend: OpenEvolutionBackend | None = None,
    ):
        self.settings = settings
        self.store = store
        self.backend = backend or ModelOpenEvolutionBackend(settings.model)
        self.manager = RuntimeCandidateManager(settings, store)

    def run(
        self, task_id: int, *, max_rounds: int = 12,
        benchmark_role: str = "mechanism_regression",
    ) -> dict[str, Any]:
        max_rounds = max(1, min(int(max_rounds), 24))
        if benchmark_role not in {"mechanism_regression", "capability_holdout"}:
            raise ValueError("benchmark_role must be mechanism_regression or capability_holdout")
        facts = self.manager.observe(task_id)
        temporary, source_root, source_provenance = self.manager._failure_time_source(task_id)
        session_id = f"rtc_{uuid.uuid4().hex}"
        candidate_root = self.manager.root / session_id
        repository = candidate_root / "repo"
        candidate_root.mkdir(parents=True)
        self._copy_open_world(repository, source_root)
        baseline = self.manager._manifest(repository)
        world = self._world_integrity(task_id, repository, source_root, source_provenance)
        transcript: list[dict[str, Any]] = []
        observations: dict[str, dict[str, Any]] = {}
        totals = {"model_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        diagnostic_experiments = 0
        observation_compactions = 0
        observation_reloads = 0
        started = time.perf_counter()
        original_task = facts.get("task", {})
        evolution_objective = {
            "target_system": "AIOS Runtime",
            "objective": (
                "Diagnose whether the observed execution outcome was caused by a mutable "
                "AIOS Runtime defect. If evidence supports it, modify and validate only the "
                "candidate Runtime. Otherwise submit NO_ACTION. Do not perform the original task."
            ),
            "original_task_role": "evidence_source_not_current_goal",
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "mode": "open_evolution/v1",
                "original_task": original_task,
                "evolution_objective": evolution_objective,
                "current_goal": "evolution_objective",
                "fact_digest": facts.get("fact_digest"),
                "available_experience_sections": sorted(facts),
                "source_provenance": source_provenance,
                "mutation_boundary": self._boundary(),
                "world_integrity": world,
                "budget": {"max_rounds": max_rounds},
            }, ensure_ascii=False)},
        ]
        disposition: dict[str, Any] | None = None
        try:
            for round_number in range(1, max_rounds + 1):
                response = self.backend.respond(messages, OPEN_EVOLUTION_TOOLS)
                observation_compactions += self._compact_seen_observations(messages)
                usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
                for key in totals:
                    totals[key] += int(usage.get(key, 0) or 0)
                calls = response.get("tool_calls") if isinstance(response.get("tool_calls"), list) else []
                assistant_message: dict[str, Any] = {
                    "role": "assistant", "content": response.get("content"),
                }
                if calls:
                    assistant_message["tool_calls"] = calls
                messages.append(assistant_message)
                round_record = {
                    "round": round_number,
                    "content": self._bounded(response.get("content"), 2000),
                    "tools": [],
                }
                if not calls:
                    observation = {
                        "ok": False,
                        "error": "A terminal decision requires submit_candidate or no_action",
                    }
                    messages.append({"role": "user", "content": json.dumps(observation)})
                    round_record["tools"].append({"name": None, "result": observation})
                    transcript.append(round_record)
                    continue
                for raw_call in calls:
                    call_id, name, arguments = self._parse_call(raw_call)
                    if name == "run_diagnostic":
                        diagnostic_experiments += 1
                    if name == "read_observation":
                        observation_reloads += 1
                    result = self._execute(
                        name, arguments, facts=facts, repository=repository, baseline=baseline,
                        observations=observations,
                    )
                    canonical_payload = result.pop("_canonical_payload", None)
                    if name not in {"read_observation", "submit_candidate", "no_action"}:
                        observation_ref = f"R{len(observations) + 1:04d}"
                        payload = (
                            canonical_payload if isinstance(canonical_payload, str)
                            else json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
                        )
                        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                        observations[observation_ref] = {
                            "ref": observation_ref,
                            "kind": name,
                            "payload": payload,
                            "digest": digest,
                            "metadata": {"producer_tool": name},
                        }
                        result = {
                            **result, "observation_ref": observation_ref,
                            "observation_digest": digest,
                            "total_characters": len(payload),
                            "offset_unit": "characters",
                        }
                    round_record["tools"].append({
                        "call_id": call_id, "name": name,
                        "arguments": self._redact_arguments(name, arguments),
                        "result": self._persistent_tool_result(name, result),
                    })
                    messages.append({
                        "role": "tool", "tool_call_id": call_id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    })
                    if name in {"submit_candidate", "no_action"} and result.get("ok"):
                        disposition = {"action": name, "reason": arguments.get("reason", "")}
                        break
                transcript.append(round_record)
                if disposition is not None:
                    break
            terminal_decision_missing = disposition is None
            model_intended_disposition = (
                "PROPOSE" if disposition is not None and disposition["action"] == "submit_candidate"
                else "NO_ACTION" if disposition is not None
                else "missing"
            )
            if disposition is None:
                disposition = {
                    "action": "no_action",
                    "reason": "open evolution round budget exhausted without terminal submission",
                }
            metrics = {
                **totals,
                "rounds": len(transcript),
                "tool_calls": sum(len(item["tools"]) for item in transcript),
                "diagnostic_experiments_run": diagnostic_experiments,
                "observation_compactions": observation_compactions,
                "observation_reloads": observation_reloads,
                "observation_objects_created": len(observations),
                "observation_store_characters": sum(
                    len(item["payload"]) for item in observations.values()
                ),
                "wall_time_ms": (time.perf_counter() - started) * 1000,
            }
            measurement = self._measurement(
                world=world, benchmark_role=benchmark_role,
                model_intended_disposition=model_intended_disposition,
                effective_host_disposition=(
                    "PROPOSE" if disposition["action"] == "submit_candidate" else "NO_ACTION"
                ),
                terminal_decision_missing=terminal_decision_missing,
            )
            if disposition["action"] == "submit_candidate":
                return self._submit(
                    session_id, task_id, facts, repository, baseline, source_provenance,
                    disposition, transcript, metrics, measurement,
                )
            report = {
                "schema": "open_evolution_run/v1", "mode": "open",
                "status": "observed", "changed": False,
                "production_activated": False, "task_id": task_id,
                "disposition": {"action": "NO_ACTION", "reason": disposition["reason"]},
                "metrics": metrics, "transcript": transcript,
                "source_provenance": source_provenance,
                **measurement,
            }
            self.store.add_evolution_run(
                f"runtime-open:{task_id}", facts, [], "observed", report,
            )
            shutil.rmtree(candidate_root, ignore_errors=True)
            return report
        finally:
            if temporary is not None:
                temporary.cleanup()

    def _copy_open_world(self, destination: Path, source_root: Path) -> None:
        """Construct only the execution-time Runtime world; never add current convenience files."""
        source_root = source_root.resolve()
        source_package = source_root / "src" / "aios"
        if not source_package.is_dir():
            raise FileNotFoundError("Failure-time Runtime source is missing src/aios")
        shutil.copytree(source_package, destination / "src" / "aios")
        build = source_root / "pyproject.toml"
        if not build.is_file():
            raise FileNotFoundError("Failure-time Runtime source is missing pyproject.toml")
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(build, destination / "pyproject.toml")

    def _world_integrity(
        self, task_id: int, repository: Path, source_root: Path,
        source_provenance: dict[str, Any],
    ) -> dict[str, Any]:
        from .runtime_provenance import RuntimeProvenanceManager

        paths = sorted(self.manager._manifest(repository))
        leakage_paths = [
            path for path in paths
            if path == "README.md"
            or path.startswith(("tests/", "external_evaluators/", "experiments/"))
        ]
        mode = str(source_provenance.get("mode", ""))
        time_aligned = bool(source_provenance.get("source_time_aligned"))
        assessment: dict[str, Any] = {}
        if mode == "failure_time_snapshot":
            try:
                assessment = RuntimeProvenanceManager(self.settings, self.store).assess(task_id)
            except (KeyError, OSError, ValueError):
                assessment = {}
        provenance_valid = bool(
            mode == "failure_time_snapshot"
            and time_aligned
            and assessment.get("source_time_aligned")
            and assessment.get("source_integrity")
        )
        return {
            "schema": "open_evolution_world_integrity/v1",
            "construction": "failure_time_whitelist",
            "visible_roots": ["src/aios/", "pyproject.toml", "candidate_tests/ (agent-created only)"],
            "current_repo_fallback": mode != "failure_time_snapshot",
            "source_time_aligned": time_aligned,
            "source_integrity": assessment.get("source_integrity"),
            "provenance_valid": provenance_valid,
            "visible_file_count": len(paths),
            "visible_manifest_digest": hashlib.sha256(
                json.dumps(paths, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "benchmark_leakage_detected": bool(leakage_paths),
            "leakage_paths": leakage_paths,
            "docker_ready_at_start": self._docker_ready(),
        }

    @staticmethod
    def _measurement(
        *, world: dict[str, Any], benchmark_role: str,
        model_intended_disposition: str, effective_host_disposition: str,
        terminal_decision_missing: bool,
    ) -> dict[str, Any]:
        invalid_reasons = []
        if world.get("benchmark_leakage_detected"):
            invalid_reasons.append("benchmark_leakage")
        if not world.get("provenance_valid"):
            invalid_reasons.append("invalid_provenance")
        if not world.get("docker_ready_at_start"):
            invalid_reasons.append("invalid_environment")
        invalid_states = {
            "benchmark_leakage": "invalid_leakage",
            "invalid_provenance": "invalid_provenance",
            "invalid_environment": "invalid_environment",
        }
        state = "valid" if not invalid_reasons else invalid_states[invalid_reasons[0]]
        return {
            "mechanism_validity": "PASS",
            "benchmark_role": benchmark_role,
            "goal_binding": {
                "schema": "open_evolution_goal_binding/v1",
                "original_task_role": "evidence_source_not_current_goal",
                "current_goal": "evolution_objective",
                "target_system": "AIOS Runtime",
                "host_framing_valid": True,
            },
            "world_integrity": world,
            "benchmark_leakage_detected": bool(world.get("benchmark_leakage_detected")),
            "model_intended_disposition": model_intended_disposition,
            "effective_host_disposition": effective_host_disposition,
            "terminal_decision_missing": terminal_decision_missing,
            "experimental_validity": {
                "state": state,
                "valid": not invalid_reasons,
                "invalid_reasons": invalid_reasons,
            },
            "capability_evaluation_eligible": bool(
                not invalid_reasons and benchmark_role == "capability_holdout"
            ),
        }

    @classmethod
    def _compact_seen_observations(cls, messages: list[dict[str, Any]]) -> int:
        compacted = 0
        for message in messages:
            if message.get("role") != "tool" or not isinstance(message.get("content"), str):
                continue
            try:
                payload = json.loads(message["content"])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("compacted"):
                continue
            ref = payload.get("observation_ref")
            if not isinstance(ref, str):
                continue
            message["content"] = json.dumps(
                cls._factual_observation_projection(ref, payload),
                ensure_ascii=False, separators=(",", ":"), default=str,
            )
            compacted += 1
        return compacted

    @staticmethod
    def _factual_observation_projection(ref: str, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        projection: dict[str, Any] = {
            "compacted": True,
            "observation_ref": ref,
            "result_digest": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "ok": payload.get("ok"),
        }
        for key in (
            "error", "path", "offset", "truncated", "exit_code", "changed_paths",
            "next", "next_offset", "returned_characters", "total_characters",
            "offset_unit", "kind", "digest", "observation_digest",
        ):
            if key in payload:
                projection[key] = payload[key]
        if isinstance(payload.get("text"), str):
            projection["text_characters"] = len(payload["text"])
        if isinstance(payload.get("stdout"), str):
            projection["stdout_characters"] = len(payload["stdout"])
        if isinstance(payload.get("stderr"), str):
            projection["stderr_characters"] = len(payload["stderr"])
        if isinstance(payload.get("matches"), list):
            projection["match_count"] = len(payload["matches"])
            projection["matched_paths"] = sorted({
                str(item.get("path")) for item in payload["matches"]
                if isinstance(item, dict) and item.get("path")
            })[:20]
        return projection

    @staticmethod
    def _docker_ready() -> bool:
        if shutil.which("docker") is None:
            return False
        try:
            result = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=8, check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _execute(
        self, name: str, arguments: dict[str, Any], *, facts: dict[str, Any],
        repository: Path, baseline: dict[str, str],
        observations: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            if name == "inspect_experience":
                section = str(arguments.get("section", ""))
                if section not in facts:
                    raise KeyError(f"Unknown experience section: {section}")
                text = json.dumps(facts[section], ensure_ascii=False, indent=2, default=str)
                return {"ok": True, "_canonical_payload": text, **self._slice(text, arguments)}
            if name == "read_observation":
                ref = str(arguments.get("ref", ""))
                if ref not in observations:
                    raise KeyError(f"Unknown observation reference: {ref}")
                return {"ok": True, **self._observation_view(observations[ref], arguments)}
            if name == "read_source":
                target, relative = self._source_target(repository, arguments.get("path"))
                text = target.read_text(encoding="utf-8", errors="replace")
                return {
                    "ok": True, "path": relative, "_canonical_payload": text,
                    **self._slice(text, arguments),
                }
            if name == "search_source":
                return {"ok": True, "matches": self._search(repository, arguments)}
            if name == "run_diagnostic":
                return self._diagnostic(repository, arguments)
            if name == "edit_candidate":
                path = Path(str(arguments.get("path", ""))).as_posix()
                if not RuntimeMutationPolicy.mutable(path):
                    raise RuntimeMutationPolicyError(f"Runtime path is outside mutable surface: {path}")
                target, _ = self._source_target(repository, path)
                old = arguments.get("old_text")
                new = arguments.get("new_text")
                if not isinstance(old, str) or not old or not isinstance(new, str):
                    raise ValueError("edit_candidate requires non-empty old_text and string new_text")
                count = target.read_text(encoding="utf-8").count(old)
                if count != 1:
                    raise ValueError(f"old_text must match exactly once; matches={count}")
                target.write_text(
                    target.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8",
                )
                self._validate_changed_surface(repository, baseline)
                return {"ok": True, "path": path, "replacements": 1}
            if name == "write_candidate_test":
                path = Path(str(arguments.get("path", ""))).as_posix()
                content = arguments.get("content")
                if not path.startswith(RuntimeMutationPolicy.TEST_PREFIX) or not path.endswith(".py"):
                    raise RuntimeMutationPolicyError("Candidate tests must live under candidate_tests/*.py")
                if not isinstance(content, str) or len(content.encode("utf-8")) > RuntimeMutationPolicy.MAX_TEST_BYTES:
                    raise RuntimeMutationPolicyError("Candidate test is missing or too large")
                target = (repository / path).resolve()
                target.relative_to(repository.resolve())
                if target.exists():
                    raise FileExistsError(f"Candidate test already exists: {path}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                self._validate_changed_surface(repository, baseline)
                return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}
            if name == "inspect_diff":
                return {"ok": True, **self._diff(repository, baseline)}
            if name == "inspect_mutation_boundary":
                return {"ok": True, **self._boundary()}
            if name == "submit_candidate":
                changed = self._validate_changed_surface(repository, baseline)
                if not changed:
                    raise RuntimeMutationPolicyError("Cannot submit an empty candidate")
                return {"ok": True, "changed_paths": changed, "next": "external_host_gate"}
            if name == "no_action":
                return {"ok": True, "changed_paths_discarded": self.manager._changed_paths(repository, baseline)}
            raise KeyError(f"Unknown Open Evolution tool: {name}")
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _submit(
        self, candidate_id: str, task_id: int, facts: dict[str, Any], repository: Path,
        baseline: dict[str, str], source_provenance: dict[str, Any],
        disposition: dict[str, Any], transcript: list[dict[str, Any]], metrics: dict[str, Any],
        measurement: dict[str, Any],
    ) -> dict[str, Any]:
        changed = self._validate_changed_surface(repository, baseline)
        metadata = {
            "schema": "runtime_candidate/v1", "candidate_id": candidate_id,
            "source_task_id": task_id, "status": "proposed",
            "production_activated": False, "facts_digest": facts.get("fact_digest"),
            "baseline_manifest": baseline, "changed_paths": changed,
            "source_provenance": source_provenance,
            "proposal": {
                "decision": "PROPOSE", "mode": "open_evolution/v1",
                "reason": disposition["reason"], "interaction_metrics": metrics,
            },
            "evaluation": None,
        }
        self.manager._write_json(repository.parent / "facts.json", facts)
        self.manager._write_json(repository.parent / "candidate.json", metadata)
        self.manager._write_json(repository.parent / "transcript.json", {
            "schema": "open_evolution_transcript/v1", "transcript": transcript,
        })
        report = {
            "schema": "open_evolution_run/v1", "mode": "open",
            "status": "proposed", "changed": True,
            "production_activated": False, "candidate_id": candidate_id,
            "task_id": task_id, "changed_paths": changed,
            "disposition": {"action": "PROPOSE", "reason": disposition["reason"]},
            "metrics": metrics, "transcript": transcript,
            "source_provenance": source_provenance,
            **measurement,
        }
        self.store.add_evolution_run(
            f"runtime-open:{task_id}", facts, [], "candidate_proposed", report,
        )
        return report

    @staticmethod
    def _parse_call(raw: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        name = str(function.get("name", ""))
        encoded = function.get("arguments", "{}")
        arguments = json.loads(encoded) if isinstance(encoded, str) else encoded
        if not isinstance(arguments, dict):
            raise ControllerError("Open evolution tool arguments must be an object")
        return str(raw.get("id") or uuid.uuid4().hex), name, arguments

    @staticmethod
    def _source_target(repository: Path, value: Any) -> tuple[Path, str]:
        relative = Path(str(value or "")).as_posix().lstrip("/")
        target = (repository / relative).resolve()
        target.relative_to(repository.resolve())
        if not target.is_file() or target.is_symlink():
            raise FileNotFoundError(relative)
        return target, relative

    @staticmethod
    def _slice(text: str, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = max(0, int(arguments.get("offset", 0) or 0))
        limit = max(1, min(int(arguments.get("limit", 12_000) or 12_000), 24_000))
        selected = text[offset:offset + limit]
        return {"offset": offset, "text": selected, "truncated": offset + len(selected) < len(text)}

    @staticmethod
    def _observation_view(
        observation: dict[str, Any], arguments: dict[str, Any],
    ) -> dict[str, Any]:
        payload = str(observation["payload"])
        offset = max(0, int(arguments.get("offset", 0) or 0))
        requested_limit = int(arguments.get("limit", 8_000) if arguments.get("limit") is not None else 8_000)
        limit = 0 if requested_limit == 0 else max(1, min(requested_limit, 24_000))
        end = min(len(payload), offset + limit)
        chunk = payload[offset:end]
        return {
            "observation_ref": observation["ref"],
            "kind": observation["kind"],
            "digest": observation["digest"],
            "offset_unit": "characters",
            "offset": offset,
            "text": chunk,
            "returned_characters": len(chunk),
            "total_characters": len(payload),
            "truncated": end < len(payload),
            "next_offset": end if end < len(payload) else None,
        }

    @staticmethod
    def _persistent_tool_result(name: str, result: dict[str, Any]) -> dict[str, Any]:
        if name != "read_observation":
            return result
        return {
            key: value for key, value in result.items()
            if key in {
                "ok", "error", "observation_ref", "kind", "digest", "offset_unit",
                "offset", "returned_characters", "total_characters", "truncated",
                "next_offset",
            }
        }

    def _search(self, repository: Path, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        query = str(arguments.get("query", ""))
        if not query:
            raise ValueError("search query is empty")
        root_value = str(arguments.get("path", ".") or ".").lstrip("/")
        root = (repository / root_value).resolve()
        root.relative_to(repository.resolve())
        if not root.exists():
            raise FileNotFoundError(root_value)
        pattern = re.compile(query) if arguments.get("regex") else None
        limit = max(1, min(int(arguments.get("limit", 50) or 50), 200))
        matches: list[dict[str, Any]] = []
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if len(matches) >= limit:
                break
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 1_000_000:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(lines, 1):
                hit = pattern.search(line) is not None if pattern else query in line
                if hit:
                    matches.append({
                        "path": path.relative_to(repository).as_posix(),
                        "line": number, "text": line[:500],
                    })
                    if len(matches) >= limit:
                        break
        return matches

    def _diagnostic(self, repository: Path, arguments: dict[str, Any]) -> dict[str, Any]:
        command = str(arguments.get("command", "")).strip()
        if not command or "\x00" in command:
            raise ValueError("diagnostic command is empty or invalid")
        timeout = max(1, min(
            int(arguments.get("timeout_seconds", self.settings.sandbox.default_timeout_seconds) or 1),
            self.settings.sandbox.max_timeout_seconds,
        ))
        args = [
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", f"{self.settings.sandbox.memory_mb}m",
            "--cpus", str(self.settings.sandbox.cpus),
            "--pids-limit", str(self.settings.sandbox.pids_limit),
            "--mount", f"type=bind,src={repository.resolve()},dst=/candidate,readonly",
            "--env", "PYTHONPATH=/candidate/src", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--tmpfs", "/tmp:rw,nosuid,size=64m", "--workdir", "/candidate",
            self.settings.sandbox.image, "/bin/sh", "-c", command,
        ]
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, check=False,
            )
            return {
                "ok": result.returncode == 0, "exit_code": result.returncode,
                "stdout": result.stdout[-12_000:], "stderr": result.stderr[-12_000:],
            }
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "exit_code": None, "error": f"TimeoutError: {exc}"}

    def _validate_changed_surface(self, repository: Path, baseline: dict[str, str]) -> list[str]:
        changed = self.manager._changed_paths(repository, baseline)
        forbidden = [
            path for path in changed
            if not RuntimeMutationPolicy.mutable(path)
            and not path.startswith(RuntimeMutationPolicy.TEST_PREFIX)
        ]
        if forbidden:
            raise RuntimeMutationPolicyError(f"Candidate modified forbidden paths: {forbidden}")
        mutable = {path for path in changed if RuntimeMutationPolicy.mutable(path)}
        if len(mutable) > RuntimeMutationPolicy.MAX_EDIT_FILES:
            raise RuntimeMutationPolicyError("A candidate may edit at most two Runtime files")
        return changed

    def _diff(self, repository: Path, baseline: dict[str, str]) -> dict[str, Any]:
        changed = self.manager._changed_paths(repository, baseline)
        files = []
        for relative in changed[:8]:
            path = repository / relative
            if path.is_file() and path.stat().st_size <= 64_000:
                files.append({
                    "path": relative,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "content": path.read_text(encoding="utf-8", errors="replace")[:16_000],
                })
            else:
                files.append({"path": relative, "content": None})
        return {"changed_paths": changed, "files": files, "truncated": len(changed) > len(files)}

    @staticmethod
    def _boundary() -> dict[str, Any]:
        return {
            "candidate_only": True, "production_activation": False,
            "external_evaluator_mutable": False, "credentials_visible": False,
            "mutable_files": sorted(RuntimeMutationPolicy.MUTABLE_FILES),
            "candidate_test_prefix": RuntimeMutationPolicy.TEST_PREFIX,
            "root_of_trust": dict(RuntimeMutationPolicy.ROOT_OF_TRUST),
        }

    @staticmethod
    def _bounded(value: Any, limit: int) -> str | None:
        return value[:limit] if isinstance(value, str) else None

    @staticmethod
    def _redact_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = dict(arguments)
        for key in ("content", "old_text", "new_text"):
            if isinstance(result.get(key), str) and len(result[key]) > 2000:
                value = result[key]
                result[key] = value[:2000] + f"... <{len(value) - 2000} chars omitted>"
        return result


def compare_evolution_modes(store: StateStore, task_ids: list[int]) -> dict[str, Any]:
    """Report observed Structured/Open outcomes without inventing missing scores."""
    runs = store.list_evolution_runs(5000)
    evaluations = {
        str(item.get("trigger", "")).removeprefix("runtime-eval:"): item.get("report", {})
        for item in runs if str(item.get("trigger", "")).startswith("runtime-eval:")
    }
    cases = []
    for task_id in task_ids:
        structured = next((
            item for item in runs
            if item.get("trigger") == f"runtime:{task_id}"
        ), None)
        opened = next((
            item for item in runs
            if item.get("trigger") == f"runtime-open:{task_id}"
        ), None)
        def projection(item: dict[str, Any] | None) -> dict[str, Any] | None:
            if item is None:
                return None
            report = item.get("report") if isinstance(item.get("report"), dict) else {}
            proposal = report.get("proposal") if isinstance(report.get("proposal"), dict) else {}
            candidate_id = report.get("candidate_id")
            evaluation = evaluations.get(str(candidate_id), {}) if candidate_id else {}
            metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
            model_usage = proposal.get("model_usage") if isinstance(proposal.get("model_usage"), dict) else {}
            experimental_validity = report.get("experimental_validity")
            if (
                not isinstance(experimental_validity, dict)
                and str(item.get("trigger", "")).startswith("runtime-open:")
            ):
                invalid_reasons = ["evolution_objective_misbinding"]
                leakage = False
                for round_item in report.get("transcript", []) if isinstance(report.get("transcript"), list) else []:
                    for tool in round_item.get("tools", []) if isinstance(round_item, dict) else []:
                        arguments = tool.get("arguments") if isinstance(tool.get("arguments"), dict) else {}
                        path = Path(str(arguments.get("path", ""))).as_posix()
                        if path == "README.md" or path.startswith(("tests/", "external_evaluators/")):
                            leakage = True
                        result = tool.get("result") if isinstance(tool.get("result"), dict) else {}
                        for match in result.get("matches", []) if isinstance(result.get("matches"), list) else []:
                            matched = Path(str(match.get("path", ""))).as_posix() if isinstance(match, dict) else ""
                            if matched == "README.md" or matched.startswith(("tests/", "external_evaluators/")):
                                leakage = True
                if leakage:
                    invalid_reasons.insert(0, "benchmark_leakage")
                legacy_states = {
                    "benchmark_leakage": "invalid_leakage",
                    "evolution_objective_misbinding": "invalid_objective_binding",
                }
                experimental_validity = {
                    "state": legacy_states[invalid_reasons[0]],
                    "valid": False,
                    "invalid_reasons": invalid_reasons,
                    "legacy_run_assessment": True,
                }
            return {
                "run_id": item.get("id"), "status": item.get("status"),
                "decision": proposal.get("decision") or report.get("disposition", {}).get("action"),
                "candidate_id": candidate_id,
                "valid_candidate_generated": bool(candidate_id),
                "external_gate_passed": evaluation.get("passed") if evaluation else None,
                "regression_passed": (
                    evaluation.get("candidate_tests", {}).get("passed")
                    if isinstance(evaluation.get("candidate_tests"), dict) else None
                ),
                "model_calls": metrics.get("model_calls", model_usage.get("model_calls")),
                "tokens": metrics.get("total_tokens", model_usage.get("total_tokens")),
                "wall_time_ms": metrics.get("wall_time_ms"),
                "diagnostic_experiments_run": metrics.get("diagnostic_experiments_run"),
                "mechanism_validity": report.get("mechanism_validity"),
                "experimental_validity": experimental_validity,
                "benchmark_role": report.get("benchmark_role"),
                "model_intended_disposition": report.get("model_intended_disposition"),
                "effective_host_disposition": report.get("effective_host_disposition"),
                "terminal_decision_missing": report.get("terminal_decision_missing"),
                "capability_evaluation_eligible": report.get("capability_evaluation_eligible"),
                "correct_diagnosis": None,
                "correct_abstention": None,
            }
        cases.append({"task_id": task_id, "structured": projection(structured), "open": projection(opened)})
    return {
        "schema": "structured_open_comparison/v1", "cases": cases,
        "metrics": [
            "correct_diagnosis", "correct_abstention", "valid_candidate_generated",
            "external_gate_passed", "regression_passed", "model_calls", "tokens",
            "wall_time", "diagnostic_experiments_run",
        ],
        "missing_values_are_not_scored": True,
        "mutable_system_is_fitness_authority": False,
    }
