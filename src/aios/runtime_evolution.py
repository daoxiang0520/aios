from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Protocol

from .config import Settings
from .controller import ControllerError, LLMController
from .storage import StateStore


class RuntimeMutationPolicyError(RuntimeError):
    pass


class RuntimeMutationReasoner(Protocol):
    def propose(
        self, facts: dict[str, Any], source_index: list[dict[str, Any]], source_root: Path,
    ) -> dict[str, Any]: ...


class RuntimeMutationPolicy:
    """Candidate-only mutation boundary. Production and fitness authority stay outside it."""

    MUTABLE_FILES = {
        "src/aios/answers.py",
        "src/aios/components.py",
        "src/aios/controller.py",
        "src/aios/evaluation.py",
        "src/aios/memory.py",
        "src/aios/resources.py",
        "src/aios/runtime.py",
        "src/aios/situation.py",
        "src/aios/tools.py",
    }
    ROOT_OF_TRUST = {
        "src/aios/capabilities.py": "authority and capability contract",
        "src/aios/security.py": "security kernel",
        "src/aios/sandbox.py": "sandbox isolation",
        "src/aios/storage.py": "audit and durable state",
        "src/aios/cli.py": "deployment and human-control interface",
        "src/aios/evolution.py": "candidate promotion policy",
        "src/aios/self_evolution.py": "production evolution controller",
        "src/aios/runtime_evolution.py": "external candidate boundary",
        "src/aios/runtime_provenance.py": "failure-time provenance and repair eligibility",
    }
    TEST_PREFIX = "candidate_tests/"
    MAX_EDIT_FILES = 2
    MAX_EDITS = 4
    MAX_TEST_BYTES = 32_768

    @classmethod
    def mutable(cls, path: str) -> bool:
        return Path(path).as_posix() in cls.MUTABLE_FILES

    @classmethod
    def validate_proposal(cls, proposal: dict[str, Any]) -> None:
        if str(proposal.get("decision", "NO_ACTION")).upper() != "PROPOSE":
            return
        patch = proposal.get("patch")
        if not isinstance(patch, dict):
            raise RuntimeMutationPolicyError("Runtime proposal must contain a patch object")
        edits = patch.get("edits", [])
        tests = patch.get("new_tests", [])
        if not isinstance(edits, list) or not edits or len(edits) > cls.MAX_EDITS:
            raise RuntimeMutationPolicyError("Runtime proposal must contain 1-4 exact edits")
        edited_files = set()
        for edit in edits:
            if not isinstance(edit, dict):
                raise RuntimeMutationPolicyError("Runtime edit must be an object")
            path = Path(str(edit.get("path", ""))).as_posix()
            if not cls.mutable(path):
                raise RuntimeMutationPolicyError(f"Runtime path is outside mutable surface: {path}")
            if not isinstance(edit.get("old_text"), str) or not edit["old_text"]:
                raise RuntimeMutationPolicyError("Runtime edits require non-empty old_text")
            if not isinstance(edit.get("new_text"), str):
                raise RuntimeMutationPolicyError("Runtime edits require string new_text")
            edited_files.add(path)
        if len(edited_files) > cls.MAX_EDIT_FILES:
            raise RuntimeMutationPolicyError("A candidate may edit at most two Runtime files")
        if not isinstance(tests, list):
            raise RuntimeMutationPolicyError("new_tests must be a list")
        for test in tests:
            if not isinstance(test, dict):
                raise RuntimeMutationPolicyError("Candidate test must be an object")
            path = Path(str(test.get("path", ""))).as_posix()
            content = test.get("content")
            if not path.startswith(cls.TEST_PREFIX) or not path.endswith(".py"):
                raise RuntimeMutationPolicyError("Candidate tests must live under candidate_tests/*.py")
            if not isinstance(content, str) or len(content.encode("utf-8")) > cls.MAX_TEST_BYTES:
                raise RuntimeMutationPolicyError("Candidate test is missing or too large")


class RuntimeExperienceBuilder:
    """Project cross-layer facts without diagnosing them or recommending a mutation."""

    def __init__(self, store: StateStore):
        self.store = store

    def build(self, task_id: int) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        checkpoints = self.store.task_checkpoints(task_id)
        cycle_ids = list(dict.fromkeys(
            str(item["data"].get("cycle_id"))
            for item in checkpoints
            if item["data"].get("cycle_id")
        ))
        traces = self.store.traces_for_cycles(cycle_ids)
        executions = self._execution_facts(traces)
        evaluations = []
        final_claims = []
        for trace in traces:
            data = trace["data"]
            if trace["kind"] == "plan_created" and data.get("done"):
                final_claims.append({
                    "trace_id": trace["id"], "cycle_id": trace["cycle_id"],
                    "text": self._bounded(data.get("summary"), 4000),
                    "completion_metadata": data.get("completion_metadata"),
                })
            if trace["kind"] == "evaluation":
                verification = data.get("verification") if isinstance(data.get("verification"), dict) else {}
                evaluations.append({
                    "trace_id": trace["id"], "cycle_id": trace["cycle_id"],
                    "success": bool(data.get("success")),
                    "failed_actions": int(data.get("failed_actions", 0) or 0),
                    "model_calls": int(data.get("model_api_calls", 0) or 0),
                    "tokens": int(data.get("model_tokens", 0) or 0),
                    "checks": verification.get("checks", []),
                    "result_vector": verification.get("result_vector"),
                })
        result = task.result if isinstance(task.result, dict) else {}
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        facts: dict[str, Any] = {
            "schema": "runtime_experience/v1",
            "task": {
                "id": task_id, "request": task.request, "status": task.status.value,
                "attempts": task.attempts, "error": task.error,
            },
            "cycles": cycle_ids,
            "executions": executions,
            "final_claims": final_claims,
            "evaluations": evaluations,
            "host_decisions": self._host_decision_facts(traces),
            "runtime_provenance": [
                {
                    "checkpoint_id": item["id"],
                    "cycle_id": item["data"].get("cycle_id"),
                    "snapshot_id": item["data"].get("snapshot_id"),
                    "execution_manifest_hash": item["data"].get(
                        "execution_runtime_snapshot", {}
                    ).get("manifest_hash"),
                    "evaluation_manifest_hash": item["data"].get(
                        "evaluation_snapshot", {}
                    ).get("manifest_hash"),
                    "root_of_trust_policy_digest": item["data"].get(
                        "root_of_trust_policy_digest"
                    ),
                }
                for item in checkpoints if item["phase"] == "runtime_provenance"
            ],
            "task_metrics": {
                name: evidence.get(name)
                for name in (
                    "model_api_calls", "model_tokens", "task_cycles", "failed_actions",
                    "repeated_resource_reads", "repeated_resource_executions",
                    "protocol_repair_calls", "context_reuse_ratio",
                )
            },
            "observation_contract": {
                "host_role": "compress facts only",
                "reasoner_role": "interpret contradictions and attribute faults",
                "no_host_recommended_fix": True,
            },
            "mutation_boundary": {
                "candidate_only": True,
                "production_immutable": True,
                "mutable_files": sorted(RuntimeMutationPolicy.MUTABLE_FILES),
                "root_of_trust": dict(RuntimeMutationPolicy.ROOT_OF_TRUST),
                "production_activation": "forbidden",
                "fitness_authority": "external_host_evaluator",
            },
        }
        facts["fact_digest"] = self._digest(facts)
        return facts

    def _host_decision_facts(self, traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kinds = {
            "runtime_provenance_bound", "runtime_provenance_capture_failed",
            "intent_selected", "capability_preflight", "dependency_environment",
            "budget_deferred", "stale_continuation_discarded",
            "terminal_task_event_discarded", "retry_scheduled", "cycle_failed",
            "task_terminal_decision", "dead_lettered",
        }
        output = []
        for trace in traces:
            if trace["kind"] not in kinds:
                continue
            output.append({
                "trace_id": trace["id"], "cycle_id": trace["cycle_id"],
                "kind": trace["kind"], "decision": self._bounded_structure(trace["data"]),
            })
        return output

    @classmethod
    def _bounded_structure(cls, value: Any, depth: int = 0) -> Any:
        if depth >= 6:
            return cls._bounded(value, 600)
        if isinstance(value, dict):
            return {
                str(key): cls._bounded_structure(item, depth + 1)
                for key, item in list(value.items())[:40]
            }
        if isinstance(value, list):
            return [cls._bounded_structure(item, depth + 1) for item in value[:40]]
        if isinstance(value, str):
            return cls._bounded(value, 1200)
        return value

    def _execution_facts(self, traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
        planned: dict[tuple[str, int], list[dict[str, Any]]] = {}
        plan_refs: dict[tuple[str, int], int] = {}
        offsets: dict[tuple[str, int], int] = {}
        output = []
        for trace in traces:
            data = trace["data"]
            key = (str(trace["cycle_id"]), int(data.get("round", 0) or 0))
            if trace["kind"] == "plan_created":
                planned[key] = [item for item in data.get("actions", []) if isinstance(item, dict)]
                plan_refs[key] = int(trace["id"])
                continue
            if trace["kind"] != "action_result":
                continue
            index = offsets.get(key, 0)
            offsets[key] = index + 1
            action = planned.get(key, [])[index] if index < len(planned.get(key, [])) else {}
            result_output = data.get("output")
            compact_output: Any
            if isinstance(result_output, dict):
                resource = result_output.get("resource") if isinstance(result_output.get("resource"), dict) else None
                compact_output = {
                    "exit_code": result_output.get("exit_code"),
                    "stdout": self._bounded(result_output.get("stdout"), 1200),
                    "stderr": self._bounded(result_output.get("stderr"), 1200),
                    "changes": result_output.get("changes"),
                    "resource": ({
                        "path": resource.get("path"), "type": resource.get("type"),
                        "metadata": resource.get("metadata"),
                        "text_excerpt": self._resource_excerpt(resource),
                    } if resource else None),
                }
            else:
                compact_output = self._bounded(result_output, 1200)
            output.append({
                "action_trace_id": plan_refs.get(key),
                "result_trace_id": trace["id"],
                "cycle_id": trace["cycle_id"], "round": key[1], "sequence": index,
                "tool": data.get("tool") or action.get("tool"),
                "arguments": self._compact_arguments(action.get("arguments", {})),
                "ok": bool(data.get("ok")), "error": data.get("error"),
                "output": compact_output,
            })
        return output

    @staticmethod
    def source_index(source_root: Path) -> list[dict[str, Any]]:
        values = []
        for path in sorted((source_root / "src" / "aios").rglob("*.py")):
            relative = path.relative_to(source_root).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            symbols = []
            try:
                tree = ast.parse(text)
                for node in tree.body:
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(node.name)
                        if isinstance(node, ast.ClassDef):
                            symbols.extend(
                                f"{node.name}.{child.name}"
                                for child in node.body
                                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                            )
            except SyntaxError:
                symbols = ["<syntax_error>"]
            values.append({
                "path": relative, "characters": len(text),
                "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "symbols": symbols[:80],
                "mutable": RuntimeMutationPolicy.mutable(relative),
                "root_of_trust_reason": RuntimeMutationPolicy.ROOT_OF_TRUST.get(relative),
            })
        return values

    @staticmethod
    def _compact_arguments(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        compact = {}
        for key, item in value.items():
            if str(key) == "content" and isinstance(item, str):
                compact["content"] = {
                    "characters": len(item),
                    "digest": hashlib.sha256(item.encode("utf-8")).hexdigest(),
                }
            else:
                compact[str(key)] = RuntimeExperienceBuilder._bounded(item, 1800)
        return compact

    @staticmethod
    def _resource_excerpt(resource: dict[str, Any]) -> str:
        texts = [
            str(item.get("text"))
            for item in resource.get("representations", [])
            if isinstance(item, dict) and item.get("text")
        ]
        return RuntimeExperienceBuilder._bounded("\n".join(texts), 1200)

    @staticmethod
    def _bounded(value: Any, limit: int) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, default=str)
        value = RuntimeExperienceBuilder._redact(value)
        return value if len(value) <= limit else value[:limit] + "…"

    @staticmethod
    def _redact(value: str) -> str:
        substitutions = (
            (r'(?i)(csrf-token[^>]{0,120}?content=["\'])[^"\']+', r'\1[REDACTED]'),
            (r'(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]+', r'\1[REDACTED]'),
            (r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}', '[REDACTED_TOKEN]'),
            (r'(https?://)[^/@\s:]+:[^/@\s]+@', r'\1[REDACTED]@'),
        )
        for pattern, replacement in substitutions:
            value = re.sub(pattern, replacement, value)
        return value

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()


class ModelRuntimeMutationReasoner:
    """Two-stage model attribution: choose evidence/source first, then author a patch."""

    def __init__(self, controller: LLMController):
        self.controller = controller

    def propose(
        self, facts: dict[str, Any], source_index: list[dict[str, Any]], source_root: Path,
    ) -> dict[str, Any]:
        if self.controller.config.provider == "mock":
            return {
                "decision": "NO_ACTION",
                "reason": "mock model cannot author a Runtime mutation",
                "attribution": {"hypotheses": [], "selected": None},
                "model_usage": {"model_calls": 0},
            }
        attribution = self._request_json(
            (
                "You are the AIOS Runtime evolution reasoner. Analyze only the supplied observed facts. "
                "The Host has not diagnosed the defect. Compare tool reality, evaluation, final claims, and cost; "
                "form competing hypotheses and select the strongest. Choose up to four source files to inspect. "
                "You may inspect root-of-trust files but must never propose modifying them. Return JSON only: "
                "{decision:'INVESTIGATE'|'NO_ACTION',hypotheses:[{id,claim,evidence_refs,counterevidence}],"
                "selected_hypothesis,inspect_files,reason}."
            ),
            {"facts": facts, "source_index": source_index},
        )
        if str(attribution.get("decision", "NO_ACTION")).upper() != "INVESTIGATE":
            return {
                "decision": "NO_ACTION", "reason": attribution.get("reason"),
                "attribution": attribution, "model_usage": {"model_calls": 1},
            }
        indexed = {item["path"] for item in source_index}
        selected = [
            Path(str(path)).as_posix() for path in attribution.get("inspect_files", [])
            if Path(str(path)).as_posix() in indexed
        ][:4]
        if not selected:
            raise ControllerError("Runtime reasoner did not select any valid source files")
        sources: dict[str, str] = {}
        delivery: list[dict[str, Any]] = []
        remaining = 90_000
        for relative in selected:
            text = (source_root / relative).read_text(encoding="utf-8", errors="replace")
            take = min(len(text), max(0, remaining))
            sources[relative] = text[:take]
            delivery.append({
                "path": relative,
                "source_characters": len(text),
                "delivered_characters": take,
                "complete": take == len(text),
            })
            remaining -= take
            if remaining <= 0:
                break
        proposal = self._request_json(
            (
                "You are authoring one minimal AIOS Runtime candidate in an isolated source snapshot. "
                "Use the observed facts and your prior attribution. Do not weaken verification to improve fitness. "
                "Do not modify authority, SecurityKernel, sandbox isolation, audit/storage, deployment/CLI, "
                "experiment/evaluator code, or production. Edits use exact unique old_text replacement. Add focused "
                "candidate tests when useful. Return JSON only: {decision:'PROPOSE'|'NO_ACTION',attribution_summary,"
                "mutation_target,expected_effects,risks,patch:{edits:[{path,old_text,new_text}],"
                "new_tests:[{path:'candidate_tests/test_*.py',content}]}}."
            ),
            {
                "facts": facts,
                "attribution": attribution,
                "selected_sources": sources,
                "mutable_files": sorted(RuntimeMutationPolicy.MUTABLE_FILES),
            },
        )
        proposal["attribution"] = attribution
        proposal["proposed_files"] = selected
        proposal["inspected_files"] = list(sources)
        proposal["source_delivery"] = {
            "budget_characters": 90_000,
            "admitted_files": list(sources),
            "files": delivery,
            "budget_truncated": len(sources) < len(selected) or any(not item["complete"] for item in delivery),
        }
        proposal["model_usage"] = {"model_calls": 2}
        RuntimeMutationPolicy.validate_proposal(proposal)
        return proposal

    def _request_json(self, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.controller.config
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config.api_key_env):
            raise ControllerError("model.api_key_env must be an environment-variable name")
        key = os.environ.get(config.api_key_env)
        if not key:
            raise ControllerError(f"Missing API key environment variable: {config.api_key_env}")
        request = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "max_tokens": min(max(2048, config.max_tokens), 6000),
            "response_format": {"type": "json_object"},
        }
        if config.provider == "deepseek":
            request["thinking"] = {"type": config.thinking if config.thinking in {"enabled", "disabled"} else "disabled"}
        response = self.controller._send_request(request, key)
        try:
            value = json.loads(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ControllerError("Runtime evolution reasoner returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ControllerError("Runtime evolution reasoner returned a non-object")
        return value


class RuntimeDiagnosisBenchmark:
    """Score blind Runtime reasoning after inference; annotations never enter model input."""

    BENCHMARK_ID = "historical_runtime_regression/v2"
    CASES: dict[int, dict[str, Any]] = {
        64: {
            "title": "continuation duplication and resurrection",
            "diagnosis_signals": [
                ["continuation", "checkpoint", "续传", "检查点"],
                ["duplicate", "resurrect", "stale", "重复", "复活", "过期"],
            ],
            "relevant_files": ["src/aios/runtime.py", "src/aios/storage.py"],
            "expected_disposition": "PROPOSE",
            "diagnosis_evaluable": False,
            "missing_fact_surfaces": ["event_queue", "checkpoint_generation", "terminal_transition"],
            "mutation_source_fidelity": "unavailable",
            "source_baseline_commit": None,
        },
        67: {
            "title": "canonical answer and artifact binding",
            "diagnosis_signals": [
                ["canonical answer", "canonicalanswer", "最终答案", "规范答案"],
                ["artifact", "answer body", "产物", "正文"],
            ],
            "relevant_files": [
                "src/aios/answers.py", "src/aios/evaluation.py", "src/aios/runtime.py",
            ],
            "expected_disposition": "PROPOSE",
            "diagnosis_evaluable": False,
            "missing_fact_surfaces": ["canonical_answer", "artifact_body_binding"],
            "mutation_source_fidelity": "unavailable",
            "source_baseline_commit": None,
        },
        70: {
            "title": "URL misclassified as an outside-workspace path",
            "diagnosis_signals": [
                ["url", "https", "网址"],
                ["path", "windows drive", "filesystem", "路径", "盘符", "文件系统"],
                ["misclass", "false positive", "误判", "混淆"],
            ],
            "relevant_files": ["src/aios/capabilities.py"],
            "expected_disposition": "NO_ACTION",
            "disposition_reason": "known causal file is inside the alpha root of trust",
            "diagnosis_evaluable": False,
            "missing_fact_surfaces": ["historical_capability_preflight_assessment"],
            "mutation_source_fidelity": "unavailable",
            "source_baseline_commit": None,
        },
        72: {
            "title": "advertised capability without an operational provider",
            "diagnosis_signals": [
                ["capability", "能力"],
                ["provider", "affordance", "operation", "提供者", "操作接口"],
                ["curl", "http", "network", "网络"],
            ],
            "relevant_files": [
                "src/aios/capabilities.py", "src/aios/components.py",
                "src/aios/resources.py", "src/aios/tools.py",
            ],
            "expected_disposition": "PROPOSE",
            "diagnosis_evaluable": False,
            "missing_fact_surfaces": ["historical_effective_capability_and_provider_binding"],
            "mutation_source_fidelity": "unavailable",
            "source_baseline_commit": None,
        },
        74: {
            "title": "unresolved tool failure accepted as full completion",
            "diagnosis_signals": [
                ["compile", "g++", "编译"],
                ["claim", "complete and correct", "声明", "完整正确"],
                ["evidence", "unverified", "not compiled", "证据", "未验证", "未编译"],
            ],
            "relevant_files": ["src/aios/evaluation.py", "src/aios/answers.py"],
            "expected_disposition": "PROPOSE",
            "diagnosis_evaluable": True,
            "missing_fact_surfaces": [],
            "mutation_source_fidelity": "matched",
            "source_baseline_commit": "f14bbd6",
        },
    }

    def __init__(self, store: StateStore):
        self.store = store

    @classmethod
    def annotation_digest(cls) -> str:
        return hashlib.sha256(
            json.dumps(cls.CASES, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @classmethod
    def score(
        cls,
        task_id: int,
        proposal: dict[str, Any],
        evaluation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        annotation = cls.CASES.get(task_id)
        if annotation is None:
            raise KeyError(f"Task {task_id} is not in the Runtime diagnosis benchmark")
        attribution = proposal.get("attribution") if isinstance(proposal.get("attribution"), dict) else {}
        hypotheses = attribution.get("hypotheses") if isinstance(attribution.get("hypotheses"), list) else []
        diagnostic_text = "\n".join(
            str(value) for value in (
                attribution.get("reason"), attribution.get("selected_hypothesis"),
                proposal.get("attribution_summary"), proposal.get("reason"),
                *(item.get("claim") for item in hypotheses if isinstance(item, dict)),
            ) if value
        ).casefold()
        signal_results = []
        for alternatives in annotation["diagnosis_signals"]:
            matches = [term for term in alternatives if term.casefold() in diagnostic_text]
            signal_results.append({"alternatives": alternatives, "matched": matches, "passed": bool(matches)})
        diagnosis_success = bool(signal_results) and all(item["passed"] for item in signal_results)

        proposed = [
            Path(str(path)).as_posix()
            for path in (
                proposal.get("proposed_files")
                or attribution.get("inspect_files")
                or proposal.get("inspected_files")
                or []
            )
        ]
        source_delivery = proposal.get("source_delivery") if isinstance(proposal.get("source_delivery"), dict) else {}
        admitted = [
            Path(str(path)).as_posix()
            for path in (source_delivery.get("admitted_files") or proposal.get("inspected_files") or proposed)
        ]
        relevant = list(annotation["relevant_files"])
        relevant_proposed = sorted(set(proposed).intersection(relevant))
        relevant_admitted = sorted(set(admitted).intersection(relevant))
        proposed_precision = len(relevant_proposed) / len(set(proposed)) if proposed else 0.0
        proposed_recall = len(relevant_proposed) / len(relevant) if relevant else 1.0
        admitted_precision = len(relevant_admitted) / len(set(admitted)) if admitted else 0.0
        admitted_recall = len(relevant_admitted) / len(relevant) if relevant else 1.0
        relevant_ranks = [index + 1 for index, path in enumerate(proposed) if path in relevant]
        first_relevant_rank = min(relevant_ranks) if relevant_ranks else None

        decision = str(proposal.get("decision", "NO_ACTION")).upper()
        generated = decision == "PROPOSE"
        policy_valid: bool | None = None
        policy_error = None
        if generated:
            try:
                RuntimeMutationPolicy.validate_proposal(proposal)
                policy_valid = True
            except RuntimeMutationPolicyError as exc:
                policy_valid = False
                policy_error = str(exc)
        patch = proposal.get("patch") if isinstance(proposal.get("patch"), dict) else {}
        edits = patch.get("edits") if isinstance(patch.get("edits"), list) else []
        edited_files = sorted({Path(str(item.get("path", ""))).as_posix() for item in edits if isinstance(item, dict)})
        mutation_precision = bool(
            generated and policy_valid and edited_files
            and set(edited_files).issubset(set(relevant))
        )
        expected = str(annotation["expected_disposition"])
        diagnosis_evaluable = bool(annotation.get("diagnosis_evaluable", True))
        mutation_evaluable = annotation.get("mutation_source_fidelity") == "matched"
        no_action = decision == "NO_ACTION"
        no_action_correct = no_action and expected == "NO_ACTION"
        no_action_safe = no_action and not generated
        gate_passed = evaluation.get("passed") if isinstance(evaluation, dict) else None
        return {
            "schema": "runtime_diagnosis_benchmark_case/v1",
            "benchmark_id": cls.BENCHMARK_ID,
            "annotation_digest": cls.annotation_digest(),
            "task_id": task_id,
            "blind_annotation": {
                "title": annotation["title"],
                "relevant_files": relevant,
                "expected_disposition": expected,
                "disposition_reason": annotation.get("disposition_reason"),
                "not_exposed_to_reasoner": True,
            },
            "eligibility": {
                "diagnosis_evaluable": diagnosis_evaluable,
                "missing_fact_surfaces": list(annotation.get("missing_fact_surfaces", [])),
                "mutation_evaluable": mutation_evaluable,
                "mutation_source_fidelity": annotation.get("mutation_source_fidelity"),
                "source_baseline_commit": annotation.get("source_baseline_commit"),
                "reason": (
                    None if diagnosis_evaluable and mutation_evaluable
                    else "Historical fact/source capsule is not temporally complete for this stage"
                ),
            },
            "diagnosis": {
                "success": diagnosis_success,
                "signal_recall": sum(item["passed"] for item in signal_results) / len(signal_results),
                "signals": signal_results,
            },
            "localization": {
                "success": bool(relevant_admitted),
                "causal_success": bool(diagnosis_success and relevant_admitted),
                "selection_success": bool(relevant_proposed),
                "delivery_success": bool(relevant_admitted),
                "proposed_files": proposed,
                "admitted_files": admitted,
                "relevant_in_proposed": relevant_proposed,
                "relevant_in_admitted": relevant_admitted,
                "first_relevant_rank": first_relevant_rank,
                "rank_state": "ranked" if first_relevant_rank is not None else "not_selected",
                "proposed_precision": proposed_precision,
                "proposed_recall": proposed_recall,
                "admitted_precision": admitted_precision,
                "admitted_recall": admitted_recall,
                "host_budget_truncated": bool(
                    source_delivery.get("budget_truncated", len(admitted) < len(proposed))
                ),
            },
            "mutation": {
                "decision": decision,
                "generated": generated,
                "policy_valid": policy_valid,
                "policy_error": policy_error,
                "edited_files": edited_files,
                "precision": mutation_precision,
                "causal_precision": bool(diagnosis_success and mutation_precision),
            },
            "no_action": {
                "predicted": no_action,
                "precision_eligible": no_action,
                "correct_for_known_disposition": no_action_correct,
                "causally_correct": bool(diagnosis_success and no_action_correct),
                "epistemically_safe": no_action_safe,
            },
            "external_gate": {
                "passed": gate_passed,
                "evaluated": gate_passed is not None,
            },
        }

    def latest(self, task_id: int) -> dict[str, Any]:
        run = next(
            (
                item for item in self.store.list_evolution_runs(1000)
                if item["trigger"] == f"runtime:{task_id}"
            ),
            None,
        )
        if run is None:
            return {
                "schema": "runtime_diagnosis_benchmark_case/v1",
                "task_id": task_id,
                "status": "missing_experiment",
            }
        report = run.get("report") if isinstance(run.get("report"), dict) else {}
        proposal = report.get("proposal") if isinstance(report.get("proposal"), dict) else {}
        evaluation = report.get("evaluation") if isinstance(report.get("evaluation"), dict) else None
        scored = self.score(task_id, proposal, evaluation)
        scored["status"] = "scored"
        scored["source_evolution_run_id"] = run["id"]
        return scored

    def suite(self, task_ids: list[int] | None = None) -> dict[str, Any]:
        selected = task_ids or sorted(self.CASES)
        cases = [self.latest(task_id) for task_id in selected]
        scored = [item for item in cases if item.get("status") == "scored"]

        def rate(predicate: Any) -> float | None:
            if not scored:
                return None
            return sum(bool(predicate(item)) for item in scored) / len(scored)

        no_action_cases = [item for item in scored if item["no_action"]["precision_eligible"]]
        correctly_diagnosed = [item for item in scored if item["diagnosis"]["success"]]
        diagnosis_eligible = [item for item in scored if item["eligibility"]["diagnosis_evaluable"]]
        mutation_eligible = [item for item in scored if item["eligibility"]["mutation_evaluable"]]
        gated = [item for item in scored if item["external_gate"]["evaluated"]]
        report = {
            "schema": "runtime_diagnosis_benchmark_suite/v1",
            "benchmark_id": self.BENCHMARK_ID,
            "annotation_digest": self.annotation_digest(),
            "benchmark_tasks": selected,
            "scored_tasks": [item["task_id"] for item in scored],
            "missing_tasks": [item["task_id"] for item in cases if item.get("status") != "scored"],
            "metrics": {
                "diagnosis_accuracy": rate(lambda item: item["diagnosis"]["success"]),
                "diagnosis_accuracy_eligible": (
                    sum(item["diagnosis"]["success"] for item in diagnosis_eligible)
                    / len(diagnosis_eligible) if diagnosis_eligible else None
                ),
                "localization_selection_accuracy": rate(
                    lambda item: item["localization"]["selection_success"]
                ),
                "localization_delivery_accuracy": rate(
                    lambda item: item["localization"]["delivery_success"]
                ),
                "localization_accuracy": rate(lambda item: item["localization"]["success"]),
                "localization_given_correct_diagnosis": (
                    sum(item["localization"]["success"] for item in correctly_diagnosed)
                    / len(correctly_diagnosed) if correctly_diagnosed else None
                ),
                "diagnosis_localization_joint_rate": rate(
                    lambda item: item["localization"]["causal_success"]
                ),
                "mutation_generation_rate": rate(lambda item: item["mutation"]["generated"]),
                "mutation_precision": rate(lambda item: item["mutation"]["precision"]),
                "mutation_precision_eligible": (
                    sum(item["mutation"]["precision"] for item in mutation_eligible)
                    / len(mutation_eligible) if mutation_eligible else None
                ),
                "no_action_precision": (
                    sum(item["no_action"]["correct_for_known_disposition"] for item in no_action_cases)
                    / len(no_action_cases) if no_action_cases else None
                ),
                "no_action_causal_precision": (
                    sum(item["no_action"]["causally_correct"] for item in no_action_cases)
                    / len(no_action_cases) if no_action_cases else None
                ),
                "external_gate_pass_rate": (
                    sum(bool(item["external_gate"]["passed"]) for item in gated) / len(gated)
                    if gated else None
                ),
            },
            "metric_contract": {
                "not_a_scalar_reward": True,
                "stages_remain_separate": ["diagnosis", "localization", "mutation", "external_gate"],
                "annotations_are_post_inference_only": True,
                "historical_trace_and_source_fidelity_gate_denominators": True,
            },
            "eligibility_summary": {
                "diagnosis_evaluable_tasks": [item["task_id"] for item in diagnosis_eligible],
                "mutation_evaluable_tasks": [item["task_id"] for item in mutation_eligible],
            },
            "cases": cases,
        }
        self.store.add_evolution_run(
            "runtime-diagnosis-benchmark", {"task_ids": selected}, [], "measured", report,
        )
        return report


class RuntimeCandidateManager:
    """Create and mutate source snapshots; production source is never a write target."""

    def __init__(
        self, settings: Settings, store: StateStore,
        reasoner: RuntimeMutationReasoner | None = None,
    ):
        self.settings = settings
        self.store = store
        self.reasoner = reasoner or ModelRuntimeMutationReasoner(LLMController(settings.model))
        self.root = (settings.experiments_root / "runtime_candidates").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.source_root = settings.root.resolve()
        self.experience = RuntimeExperienceBuilder(store)

    def observe(self, task_id: int) -> dict[str, Any]:
        return self.experience.build(task_id)

    def propose(self, task_id: int) -> dict[str, Any]:
        facts = self.observe(task_id)
        index = self.experience.source_index(self.source_root)
        proposal = self.reasoner.propose(facts, index, self.source_root)
        if str(proposal.get("decision", "NO_ACTION")).upper() != "PROPOSE":
            report = {
                "status": "observed", "changed": False, "production_activated": False,
                "task_id": task_id, "facts": facts, "proposal": proposal,
            }
            self.store.add_evolution_run(f"runtime:{task_id}", facts, [], "observed", report)
            return report
        RuntimeMutationPolicy.validate_proposal(proposal)
        candidate_id = f"rtc_{uuid.uuid4().hex}"
        candidate_root = self.root / candidate_id
        repository = candidate_root / "repo"
        candidate_root.mkdir(parents=True)
        self._copy_repository(repository)
        baseline = self._manifest(repository)
        self._apply(repository, proposal)
        changed = self._changed_paths(repository, baseline)
        forbidden = [
            path for path in changed
            if not RuntimeMutationPolicy.mutable(path)
            and not path.startswith(RuntimeMutationPolicy.TEST_PREFIX)
        ]
        if forbidden:
            shutil.rmtree(candidate_root)
            raise RuntimeMutationPolicyError(f"Candidate modified forbidden paths: {forbidden}")
        metadata = {
            "schema": "runtime_candidate/v1", "candidate_id": candidate_id,
            "source_task_id": task_id, "status": "proposed",
            "production_activated": False, "facts_digest": facts["fact_digest"],
            "baseline_manifest": baseline, "changed_paths": changed,
            "proposal": proposal, "evaluation": None,
        }
        self._write_json(candidate_root / "facts.json", facts)
        self._write_json(candidate_root / "candidate.json", metadata)
        report = {
            "status": "proposed", "changed": True, "production_activated": False,
            "candidate_id": candidate_id, "task_id": task_id,
            "changed_paths": changed, "proposal": proposal,
        }
        self.store.add_evolution_run(f"runtime:{task_id}", facts, [], "candidate_proposed", report)
        return report

    def show(self, candidate_id: str) -> dict[str, Any]:
        path = self._candidate_path(candidate_id) / "candidate.json"
        if not path.is_file():
            raise KeyError(f"Unknown Runtime candidate: {candidate_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        values = []
        for path in sorted(self.root.glob("rtc_*/candidate.json"), reverse=True):
            try:
                values.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return values

    def update_evaluation(self, candidate_id: str, evaluation: dict[str, Any]) -> dict[str, Any]:
        root = self._candidate_path(candidate_id)
        metadata = self.show(candidate_id)
        metadata["evaluation"] = evaluation
        metadata["status"] = evaluation["status"]
        metadata["production_activated"] = False
        self._write_json(root / "candidate.json", metadata)
        return metadata

    def repository(self, candidate_id: str) -> Path:
        root = self._candidate_path(candidate_id)
        self.show(candidate_id)
        return root / "repo"

    def _candidate_path(self, candidate_id: str) -> Path:
        if re.fullmatch(r"rtc_[0-9a-f]{32}", candidate_id) is None:
            raise RuntimeMutationPolicyError("Invalid Runtime candidate id")
        path = (self.root / candidate_id).resolve()
        if path.parent != self.root:
            raise RuntimeMutationPolicyError("Runtime candidate path escapes managed root")
        return path

    def _copy_repository(self, destination: Path) -> None:
        destination.mkdir(parents=True)
        shutil.copytree(self.source_root / "src", destination / "src")
        shutil.copytree(self.source_root / "tests", destination / "tests")
        for name in ("pyproject.toml", "README.md"):
            source = self.source_root / name
            if source.is_file():
                shutil.copy2(source, destination / name)

    @staticmethod
    def _apply(repository: Path, proposal: dict[str, Any]) -> None:
        patch = proposal["patch"]
        for edit in patch.get("edits", []):
            target = repository / Path(edit["path"])
            content = target.read_text(encoding="utf-8")
            count = content.count(edit["old_text"])
            if count != 1:
                raise RuntimeMutationPolicyError(
                    f"Candidate old_text must match exactly once: {edit['path']} matches={count}"
                )
            target.write_text(content.replace(edit["old_text"], edit["new_text"], 1), encoding="utf-8")
        for test in patch.get("new_tests", []):
            target = repository / Path(test["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise RuntimeMutationPolicyError(f"Candidate test already exists: {test['path']}")
            target.write_text(test["content"], encoding="utf-8")

    @staticmethod
    def _manifest(root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()
        }

    @classmethod
    def _changed_paths(cls, root: Path, baseline: dict[str, str]) -> list[str]:
        current = cls._manifest(root)
        return sorted(path for path in set(baseline) | set(current) if baseline.get(path) != current.get(path))

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class ExternalRuntimeEvaluator:
    """Host-owned, candidate-inaccessible fitness gate. It never activates production."""

    def __init__(self, settings: Settings, manager: RuntimeCandidateManager):
        self.settings = settings
        self.manager = manager
        self.external_root = (settings.root / "external_evaluators").resolve()

    def evaluate(self, candidate_id: str) -> dict[str, Any]:
        metadata = self.manager.show(candidate_id)
        repository = self.manager.repository(candidate_id)
        baseline = metadata.get("baseline_manifest", {})
        changed = RuntimeCandidateManager._changed_paths(repository, baseline)
        forbidden = [
            path for path in changed
            if not RuntimeMutationPolicy.mutable(path)
            and not path.startswith(RuntimeMutationPolicy.TEST_PREFIX)
        ]
        syntax = self._syntax_gate(repository, changed)
        source_task_id = int(metadata.get("source_task_id", 0) or 0)
        if source_task_id != 74:
            evaluation = {
                "schema": "external_runtime_evaluation/v1",
                "candidate_id": candidate_id,
                "source_task_id": source_task_id,
                "status": "rejected",
                "passed": False,
                "production_activated": False,
                "selection": "unsupported_external_gate",
                "mutable_system_is_fitness_authority": False,
                "changed_paths": changed,
                "policy_gate": {"passed": not forbidden, "forbidden_changes": forbidden},
                "syntax_gate": syntax,
                "external_gate": {
                    "supported": False,
                    "reason": f"No Host-owned immutable evaluator is registered for Task {source_task_id}",
                },
                "candidate_tests": {
                    "passed": False, "skipped": True,
                    "reason": "candidate tests cannot replace a Host-owned external gate",
                },
            }
            self.manager.update_evaluation(candidate_id, evaluation)
            facts = json.loads(
                (self.manager._candidate_path(candidate_id) / "facts.json").read_text(encoding="utf-8")
            )
            self.manager.store.add_evolution_run(
                f"runtime-eval:{candidate_id}", facts, [], "rejected", evaluation,
            )
            return evaluation
        docker_ready = self._docker_ready()
        baseline_gate = self._run_gate(self.settings.root) if docker_ready else self._blocked("docker unavailable")
        candidate_gate = self._run_gate(repository) if docker_ready else self._blocked("docker unavailable")
        candidate_tests = (
            self._run_candidate_tests(repository)
            if docker_ready and (repository / "candidate_tests").is_dir()
            else {"passed": True, "skipped": True, "reason": "no candidate tests"}
        )
        passed = bool(
            not forbidden
            and syntax["passed"]
            and docker_ready
            and not baseline_gate["passed"]
            and candidate_gate["passed"]
            and candidate_tests["passed"]
        )
        evaluation = {
            "schema": "external_runtime_evaluation/v1",
            "candidate_id": candidate_id,
            "status": "needs_review" if passed else "rejected",
            "passed": passed,
            "production_activated": False,
            "selection": "human_review_required",
            "mutable_system_is_fitness_authority": False,
            "changed_paths": changed,
            "policy_gate": {"passed": not forbidden, "forbidden_changes": forbidden},
            "syntax_gate": syntax,
            "task74_external_gate": {
                "baseline": baseline_gate,
                "candidate": candidate_gate,
                "required_transition": "baseline FAIL -> candidate PASS",
            },
            "candidate_tests": candidate_tests,
        }
        self.manager.update_evaluation(candidate_id, evaluation)
        facts = json.loads((self.manager._candidate_path(candidate_id) / "facts.json").read_text(encoding="utf-8"))
        self.manager.store.add_evolution_run(
            f"runtime-eval:{candidate_id}", facts, [], evaluation["status"], evaluation,
        )
        return evaluation

    @staticmethod
    def _syntax_gate(repository: Path, changed: list[str]) -> dict[str, Any]:
        errors = []
        for relative in changed:
            if not relative.endswith(".py"):
                continue
            try:
                ast.parse((repository / relative).read_text(encoding="utf-8"), filename=relative)
            except (OSError, SyntaxError) as exc:
                errors.append(f"{relative}: {type(exc).__name__}: {exc}")
        return {"passed": not errors, "errors": errors}

    def _run_gate(self, repository: Path) -> dict[str, Any]:
        return self._docker_python(
            repository,
            ["python", "/external/task74_recovery_gate.py"],
            mount_external=True,
        )

    def _run_candidate_tests(self, repository: Path) -> dict[str, Any]:
        return self._docker_python(
            repository,
            ["python", "-m", "unittest", "discover", "-s", "/candidate/candidate_tests", "-p", "test_*.py"],
            mount_external=False,
        )

    def _docker_python(
        self, repository: Path, command: list[str], *, mount_external: bool,
    ) -> dict[str, Any]:
        args = [
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", f"{self.settings.sandbox.memory_mb}m",
            "--cpus", str(self.settings.sandbox.cpus),
            "--pids-limit", str(self.settings.sandbox.pids_limit),
            "--mount", f"type=bind,src={repository.resolve()},dst=/candidate,readonly",
            "--env", "PYTHONPATH=/candidate/src", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--tmpfs", "/tmp:rw,nosuid,size=64m", "--workdir", "/candidate",
        ]
        if mount_external:
            args.extend(["--mount", f"type=bind,src={self.external_root},dst=/external,readonly"])
        args.extend([self.settings.sandbox.image, *command])
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.settings.sandbox.max_timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {"passed": False, "exit_code": None, "error": f"TimeoutError: {exc}"}
        return {
            "passed": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:],
        }

    @staticmethod
    def _docker_ready() -> bool:
        if shutil.which("docker") is None:
            return False
        try:
            result = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=8, check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _blocked(reason: str) -> dict[str, Any]:
        return {"passed": False, "blocked": True, "reason": reason}
