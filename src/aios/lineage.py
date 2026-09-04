from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from typing import Any, Callable, Protocol

from .controller import ControllerError, LLMController
from .config import Settings
from .components import ComponentKind, ComponentRegistry
from .evolution import (
    ALLOWED_MUTATIONS, HARNESS_PROFILES, EvolutionManager, EvolutionPolicyError,
)
from .storage import StateStore
from .sandbox import DockerSandboxBroker
from .skills import SkillManager, SkillValidationError
from .types import TaskStatus
from .lineage_behavior import (
    BEHAVIOR_POLICY, bound_digest, cross_task_patterns, summarize_behavior,
)


LINEAGE_ACTIONS = {
    "CONTINUE", "ADOPT_CANDIDATE", "ADOPT_COMPONENT_CANDIDATE",
    "AUTHOR_COMPONENT_CANDIDATE", "REQUEST_COUNTERFACTUAL_EVALUATION",
    "FORK_MUTATION", "RETURN",
}

TASK_STATUS_SEMANTICS = {
    TaskStatus.QUEUED.value: "Waiting to be claimed by the Runtime; no execution is active yet.",
    TaskStatus.RUNNING.value: "Currently claimed and executing in the Runtime.",
    TaskStatus.RETRYING.value: "A prior attempt failed and another attempt is scheduled.",
    TaskStatus.COMPLETED.value: "The online Host Verifier accepted completion in verified mode.",
    TaskStatus.FAILED.value: "Execution failed; this status does not imply active processing.",
    TaskStatus.DEAD_LETTER.value: "The verified Runtime exhausted retries; execution is terminal.",
    TaskStatus.DEGRADED.value: "The Host Verifier observed only partial or substitute satisfaction.",
    TaskStatus.DEFERRED.value: "The task was checkpointed at a cycle budget boundary for continuation.",
    TaskStatus.BLOCKED_CAPABILITY.value: "A required capability is unavailable; no execution is active.",
    TaskStatus.NEEDS_AUTHORITY.value: "Execution is paused pending required authority.",
    TaskStatus.RETRYABLE_FAILURE.value: "The current failure is classified as recoverable by a later attempt.",
    TaskStatus.TERMINAL_FAILURE.value: "Execution ended with a non-retryable failure.",
    TaskStatus.NEEDS_REVIEW.value: "Execution is paused for an external review decision.",
    TaskStatus.STOPPED.value: "In free mode the Agent declared stop; true completion was not judged.",
    TaskStatus.YIELDED.value: "In free mode execution ended without an Agent stop declaration; this status alone does not establish voluntary yield or the exit cause.",
    TaskStatus.ABANDONED.value: "The Runtime stopped investing after execution or protocol exhaustion.",
}


class LineageReasoner(Protocol):
    def reason(self, facts: dict[str, Any]) -> dict[str, Any]: ...


class ComponentCandidateAuthor(Protocol):
    def author(
        self, decision: dict[str, Any], facts: dict[str, Any], lineage_id: str,
    ) -> dict[str, Any]: ...


class LineageManager:
    """Versioned Harness heredity without production promotion or Host fitness selection."""

    ROOT_ID = "lin_root"
    HEAD_STATE_KEY = "experimental_lineage_head"

    def __init__(
        self, store: StateStore, components: ComponentRegistry | None = None,
        skills: SkillManager | None = None,
    ):
        self.store = store
        self.components = components
        self.skills = skills

    def ensure_root(self) -> dict[str, Any]:
        existing = self.store.get_lineage(self.ROOT_ID)
        if existing is not None:
            return self._ensure_component_set(existing)
        harness = self.store.active_harness()
        self.store.create_lineage({
            "lineage_id": self.ROOT_ID, "parent_lineage_id": None, "generation": 0,
            "kind": "system", "status": "living", "settings": harness.get("settings", {}),
            "component_set": self._active_component_set(),
            "mutation": {}, "source_candidate_id": None, "created_by": "system",
            "decision": {
                "action": "ROOT_SNAPSHOT", "production_harness_version": harness.get("version"),
                "production_default_changed": False,
            },
        })
        self.store.add_lineage_event(
            self.ROOT_ID, "root_created", "system",
            {"production_harness_version": harness.get("version")},
        )
        if self.store.get_state(self.HEAD_STATE_KEY) is None:
            self.store.set_state(self.HEAD_STATE_KEY, self.ROOT_ID)
        return self.store.get_lineage(self.ROOT_ID) or {}

    def current(self) -> dict[str, Any]:
        root = self.ensure_root()
        lineage_id = str(self.store.get_state(self.HEAD_STATE_KEY, root["lineage_id"]))
        lineage = self.store.get_lineage(lineage_id)
        if lineage is None:
            self.store.set_state(self.HEAD_STATE_KEY, root["lineage_id"])
            return root
        return self._ensure_component_set(lineage)

    def select_head(self, lineage_id: str) -> dict[str, Any]:
        lineage = self._lineage(lineage_id)
        self.store.set_state(self.HEAD_STATE_KEY, lineage_id)
        return lineage

    def fork(
        self, parent_lineage_id: str, mutation: dict[str, Any], *, actor: str,
        decision: dict[str, Any], source_candidate_id: int | None = None,
    ) -> dict[str, Any]:
        parent = self._lineage(parent_lineage_id)
        if len(mutation) != 1:
            raise EvolutionPolicyError("An autonomous fork must contain exactly one mutation")
        EvolutionManager._validate_mutation(mutation)
        lineage_id = "lin_" + uuid.uuid4().hex[:16]
        child = {
            "lineage_id": lineage_id, "parent_lineage_id": parent_lineage_id,
            "generation": int(parent["generation"]) + 1, "kind": "system", "status": "living",
            "settings": {**parent["settings"], **mutation}, "mutation": mutation,
            "component_set": dict(parent.get("component_set") or {}),
            "source_candidate_id": source_candidate_id, "created_by": actor,
            "decision": decision,
        }
        self.store.create_lineage(child)
        event = {
            "child_lineage_id": lineage_id, "mutation": mutation,
            "source_candidate_id": source_candidate_id, "production_default_changed": False,
        }
        self.store.add_lineage_event(parent_lineage_id, "forked_child", actor, event)
        self.store.add_lineage_event(lineage_id, "forked_from_parent", actor, {
            **event, "parent_lineage_id": parent_lineage_id, "decision": decision,
        })
        return self._lineage(lineage_id)

    def adopt_candidate(
        self, parent_lineage_id: str, candidate_id: int, *, actor: str,
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError(f"Unknown candidate: {candidate_id}")
        if candidate["status"] == "promoted":
            raise EvolutionPolicyError("A production-promoted candidate is not a branch mutation")
        return self.fork(
            parent_lineage_id, candidate["mutation"], actor=actor, decision=decision,
            source_candidate_id=candidate_id,
        )

    def adopt_component_candidate(
        self, parent_lineage_id: str, candidate_id: str, *, actor: str,
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        """Adopt a benchmarked mutable Component into an isolated child lineage."""
        if self.skills is None or self.components is None:
            raise EvolutionPolicyError("Component evolution services are unavailable")
        candidate = next(
            (
                item for item in self.skills.list_candidates()
                if str(item["candidate_id"]) == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise EvolutionPolicyError(f"Unknown Skill Component candidate: {candidate_id}")
        benchmark = candidate.get("benchmark")
        if not isinstance(benchmark, dict) or benchmark.get("passed") is not True:
            raise EvolutionPolicyError("Skill Component candidate must pass its sandbox benchmark")
        record = self.components.register(
            self.skills.candidate_component(candidate_id), source="agent",
        )
        if record["kind"] != ComponentKind.SKILL.value:
            raise EvolutionPolicyError("Only Skill Components are open for lineage adoption")
        parent = self._lineage(parent_lineage_id)
        component_set = self._replace_component(
            dict(parent.get("component_set") or {}), record,
            {"type": "skill_candidate", "candidate_id": candidate_id},
        )
        operation = (
            "replace" if any(
                item.get("component_id") == record["component_id"]
                for item in parent.get("component_set", {}).get("members", [])
            ) else "add"
        )
        lineage_id = "lin_" + uuid.uuid4().hex[:16]
        component_mutation = {
            "operation": operation, "kind": "skill",
            "component_id": record["component_id"],
            "name": record["metadata"]["name"],
            "version": record["metadata"]["version"],
            "candidate_id": candidate_id,
        }
        child = {
            "lineage_id": lineage_id, "parent_lineage_id": parent_lineage_id,
            "generation": int(parent["generation"]) + 1, "kind": "system",
            "status": "living", "settings": dict(parent.get("settings") or {}),
            "component_set": component_set,
            "mutation": {"component": component_mutation},
            "source_candidate_id": None,
            "source_component_candidate_id": candidate_id,
            "created_by": actor, "decision": decision,
        }
        self.store.create_lineage(child)
        event = {
            "child_lineage_id": lineage_id, "component_mutation": component_mutation,
            "component_set_hash": component_set["active_set_hash"],
            "production_default_changed": False,
        }
        self.store.add_lineage_event(parent_lineage_id, "forked_component_child", actor, event)
        self.store.add_lineage_event(lineage_id, "component_adopted_from_parent", actor, {
            **event, "parent_lineage_id": parent_lineage_id, "decision": decision,
        })
        return self._lineage(lineage_id)

    def continue_lineage(
        self, lineage_id: str, *, actor: str, decision: dict[str, Any],
    ) -> dict[str, Any]:
        lineage = self._lineage(lineage_id)
        self.store.add_lineage_event(lineage_id, "continued", actor, {"decision": decision})
        return lineage

    def return_to(
        self, current_lineage_id: str, target_lineage_id: str, *, actor: str,
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        self._lineage(current_lineage_id)
        target = self._lineage(target_lineage_id)
        if target_lineage_id not in self.ancestor_ids(current_lineage_id):
            raise EvolutionPolicyError("RETURN target must be an ancestor of the current lineage")
        self.store.add_lineage_event(current_lineage_id, "returned_to_ancestor", actor, {
            "target_lineage_id": target_lineage_id, "decision": decision,
            "production_default_changed": False,
        })
        self.store.add_lineage_event(target_lineage_id, "resumed_from_descendant", actor, {
            "source_lineage_id": current_lineage_id,
        })
        return target

    def ancestor_ids(self, lineage_id: str) -> list[str]:
        ancestors: list[str] = []
        current = self._lineage(lineage_id)
        while current.get("parent_lineage_id"):
            parent_id = str(current["parent_lineage_id"])
            ancestors.append(parent_id)
            current = self._lineage(parent_id)
        return ancestors

    def describe(self, lineage_id: str) -> dict[str, Any]:
        lineage = self._lineage(lineage_id)
        lineage["ancestors"] = self.ancestor_ids(lineage_id)
        lineage["children"] = [
            item["lineage_id"] for item in self.store.list_lineages()
            if item.get("parent_lineage_id") == lineage_id
        ]
        lineage["events"] = self.store.list_lineage_events(lineage_id)
        return lineage

    def _lineage(self, lineage_id: str) -> dict[str, Any]:
        lineage = self.store.get_lineage(lineage_id)
        if lineage is None:
            raise KeyError(f"Unknown lineage: {lineage_id}")
        return self._ensure_component_set(lineage)

    def _ensure_component_set(self, lineage: dict[str, Any]) -> dict[str, Any]:
        component_set = lineage.get("component_set")
        if component_set and (
            self.components is None or bool(component_set.get("members"))
        ):
            return lineage
        parent_id = lineage.get("parent_lineage_id")
        component_set = (
            dict(self._lineage(str(parent_id)).get("component_set") or {})
            if parent_id else self._active_component_set()
        )
        self.store.update_lineage_component_set(str(lineage["lineage_id"]), component_set)
        return self.store.get_lineage(str(lineage["lineage_id"])) or lineage

    def _active_component_set(self) -> dict[str, Any]:
        records = self.components.list(status="active") if self.components is not None else []
        members = []
        for record in records:
            source = (
                {"type": "skill_version"}
                if record["kind"] == ComponentKind.SKILL.value
                else {"type": "registry_version"}
            )
            members.append(self._component_member(record, source))
        return self._component_set(members)

    @staticmethod
    def _component_member(record: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        return {
            "component_id": record["component_id"], "version_id": record["version_id"],
            "kind": record["kind"], "name": record["metadata"]["name"],
            "version": record["metadata"]["version"],
            "content_digest": record.get("content_digest", ""),
            "provides": list(record.get("capabilities", {}).get("provides", [])),
            "source": source,
        }

    @classmethod
    def _replace_component(
        cls, component_set: dict[str, Any], record: dict[str, Any], source: dict[str, Any],
    ) -> dict[str, Any]:
        members = [
            dict(item) for item in component_set.get("members", [])
            if item.get("component_id") != record["component_id"]
        ]
        members.append(cls._component_member(record, source))
        return cls._component_set(members)

    @staticmethod
    def _component_set(members: list[dict[str, Any]]) -> dict[str, Any]:
        ordered = sorted(members, key=lambda item: (item["kind"], item["name"], item["version"]))
        material = json.dumps(ordered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "schema": "component_set/v1", "members": ordered,
            "active_set_hash": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        }


class ModelLineageReasoner:
    """Let the configured model choose heredity; the Host validates only authority and shape."""

    def __init__(self, controller: LLMController):
        self.controller = controller

    def reason(self, facts: dict[str, Any]) -> dict[str, Any]:
        config = self.controller.config
        if config.provider == "mock":
            return {"action": "CONTINUE", "reason": "mock model keeps the current lineage"}
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config.api_key_env):
            raise ControllerError("model.api_key_env must be an environment-variable name")
        key = os.environ.get(config.api_key_env)
        if not key:
            raise ControllerError(f"Missing API key environment variable: {config.api_key_env}")
        request = {
            "model": config.model,
            "messages": [{
                "role": "system", "content": (
                    "You control one experimental AIOS System lineage containing Harness settings and a "
                    "versioned Component Set. Decide how your own lineage continues from immutable "
                    "observations. Allowed actions: CONTINUE; ADOPT_CANDIDATE with candidate_id for a "
                    "Harness candidate; ADOPT_COMPONENT_CANDIDATE with component_candidate_id; "
                    "AUTHOR_COMPONENT_CANDIDATE with kind=skill and a concrete objective to create, "
                    "validate, and benchmark a new variation without adopting it; "
                    "REQUEST_COUNTERFACTUAL_EVALUATION with capsule_ids and runs_per_variant to "
                    "re-execute the direct parent and current Harness in identical isolated worlds; "
                    "FORK_MUTATION with exactly one allowed Harness mutation; RETURN "
                    "to an ancestor lineage. A fork affects only its child lineage, never production. "
                    "CONTINUE means only keep the current lineage unchanged for future evidence. It does "
                    "not execute, resume, or mark any task active. Task statuses in lineage_tasks are "
                    "authoritative; do not describe a task as actively processing unless its supplied "
                    "status says so. Interpret every status using task_status_semantics. "
                    "current_lineage and lineage_history contain stored lineage state, not necessarily "
                    "effective execution limits. Use current_execution_config for loaded Host policy: "
                    "configured values can be inactive; an unlimited limit is not zero. That policy "
                    "does not describe earlier tasks or prove what another running process loaded. "
                    "Historical task execution_policy is separate; null historical facts are unknown. Historical "
                    "model reasons are intentionally withheld; infer from current evidence instead of "
                    "repeating a prior narrative. "
                    "behavior_digest and cross_task_patterns are bounded operational observations, "
                    "not recommendations or proof of equivalent intent. Inspect source/truncation "
                    "metadata: missing history is unknown, not zero activity. Successful repeated "
                    "procedures can be relevant too; neither repetition, failure nor cost requires "
                    "a mutation. You alone decide whether any reusable abstraction is warranted. "
                    "Available candidates may be empty: FORK_MUTATION authors a new candidate directly "
                    "from mutation_contract and does not require a pre-existing candidate. "
                    "When you claim evidence is inconclusive and eligible replay capsules exist, consider "
                    "requesting a controlled evaluation instead of passively waiting. Evaluation only "
                    "measures outcomes; it does not select, adopt, promote, or change the lineage. "
                    "The Host does not judge which lineage is better. Return one JSON object with action, "
                    "reason, evidence_task_ids, and the fields required by the chosen action. Keep the final "
                    "JSON concise; do not spend the response restating the supplied facts. Example JSON: "
                    "{\"action\":\"CONTINUE\",\"reason\":\"More evidence is needed.\","
                    "\"evidence_task_ids\":[101,102]}."
                ),
            }, {"role": "user", "content": json.dumps(
                ModelComponentCandidateAuthor._redact_outbound(facts), ensure_ascii=False,
            )}],
            "temperature": 0.1,
            "max_tokens": min(config.max_tokens, 4096),
            "response_format": {"type": "json_object"},
        }
        if config.provider == "deepseek":
            thinking = config.thinking if config.thinking in {"enabled", "disabled"} else "disabled"
            request["thinking"] = {"type": thinking}
            if thinking == "enabled":
                # DeepSeek counts hidden reasoning and the final answer against the same
                # output budget. Preserve the configured ceiling instead of squeezing both
                # into the non-thinking 4K allowance. Temperature is ignored in this mode.
                request["max_tokens"] = config.max_tokens
                request.pop("temperature", None)
        response = self.controller._request_with_recovery(request, key)
        choices = response.get("choices") if isinstance(response, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") if isinstance(choice, dict) else {}
        message = message if isinstance(message, dict) else {}
        content = message.get("content")
        reasoning_content = message.get("reasoning_content")
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        try:
            decision = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            content_chars = len(content) if isinstance(content, str) else 0
            reasoning_chars = len(reasoning_content) if isinstance(reasoning_content, str) else 0
            raise ControllerError(
                "Lineage reasoner returned no valid final JSON; "
                f"finish_reason={finish_reason or 'unknown'}; "
                f"content_type={type(content).__name__}; content_chars={content_chars}; "
                f"reasoning_chars={reasoning_chars}; output_budget={request['max_tokens']}"
            ) from exc
        if not isinstance(decision, dict):
            raise ControllerError("Lineage reasoner returned an invalid object")
        decision["model_usage"] = response.get("usage", {"model_calls": 1})
        return decision


class ModelComponentCandidateAuthor:
    """Turn one lineage-authored objective into an isolated, benchmarked Skill variation."""

    def __init__(
        self, controller: LLMController, skills: SkillManager, broker: DockerSandboxBroker,
    ):
        self.controller = controller
        self.skills = skills
        self.broker = broker

    def author(
        self, decision: dict[str, Any], facts: dict[str, Any], lineage_id: str,
    ) -> dict[str, Any]:
        kind = str(decision.get("kind") or "").lower()
        if kind != ComponentKind.SKILL.value:
            raise EvolutionPolicyError("Only kind=skill is open for component candidate authoring")
        objective = str(decision.get("objective") or "").strip()
        if not objective or len(objective) > 2000:
            raise EvolutionPolicyError("AUTHOR_COMPONENT_CANDIDATE requires an objective of 1-2000 characters")
        evidence_ids = decision.get("evidence_task_ids") or []
        if not isinstance(evidence_ids, list) or any(
            not isinstance(item, int) or item <= 0 for item in evidence_ids
        ):
            raise EvolutionPolicyError("evidence_task_ids must be a list of positive task IDs")
        task_index = {
            int(item["task_id"]): item for item in facts.get("lineage_tasks", [])
            if isinstance(item, dict) and isinstance(item.get("task_id"), int)
        }
        unsupported = sorted(set(evidence_ids) - set(task_index))
        if unsupported:
            raise EvolutionPolicyError(
                f"Candidate authoring evidence is outside the supplied lineage experience: {unsupported}"
            )
        evidence = []
        for task_id in evidence_ids:
            item = dict(task_index[task_id])
            summary = item.get("summary")
            if isinstance(summary, str):
                item["summary"] = summary[:4000]
            evidence.append(self._redact_outbound(item))

        config = self.controller.config
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config.api_key_env):
            raise ControllerError("model.api_key_env must be an environment-variable name")
        key = os.environ.get(config.api_key_env)
        if not key:
            raise ControllerError(f"Missing API key environment variable: {config.api_key_env}")
        request = {
            "model": config.model,
            "messages": [{
                "role": "system",
                "content": (
                    "Author one reusable AIOS Skill candidate from the supplied lineage objective and "
                    "evidence. Return one JSON object with kind=skill, manifest, and source. The manifest "
                    "must use a lowercase_snake_case name, semantic version, entrypoint=skill.py, declare "
                    "process.sandbox_exec plus only capabilities actually needed, use an object input_schema, "
                    "and contain at least one executable test with input and expect_exit. source must be a "
                    "self-contained Python program accepting --input-json and printing JSON to stdout. "
                    "Do not request secrets, host paths, new authority, or production activation. The Host "
                    "will validate and execute the candidate only inside the benchmark sandbox."
                ),
            }, {
                "role": "user",
                "content": json.dumps(self._redact_outbound({
                    "schema": "component_candidate_authoring_brief/v1",
                    "lineage_id": lineage_id,
                    "kind": kind,
                    "objective": objective,
                    "reason": str(decision.get("reason") or ""),
                    "evidence": evidence,
                    "allowed_capabilities": sorted(SkillManager.SAFE_CAPABILITIES),
                }), ensure_ascii=False),
            }],
            "temperature": 0.1,
            "max_tokens": min(config.max_tokens, 8192),
            "response_format": {"type": "json_object"},
        }
        if config.provider == "deepseek":
            thinking = config.thinking if config.thinking in {"enabled", "disabled"} else "disabled"
            request["thinking"] = {"type": thinking}
            if thinking == "enabled":
                request["max_tokens"] = config.max_tokens
                request.pop("temperature", None)
        response = self.controller._request_with_recovery(request, key)
        choices = response.get("choices") if isinstance(response, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") if isinstance(choice, dict) else {}
        message = message if isinstance(message, dict) else {}
        content = message.get("content")
        try:
            authored = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            reasoning = message.get("reasoning_content")
            raise ControllerError(
                "Component author returned no valid final JSON; "
                f"finish_reason={choice.get('finish_reason') or 'unknown'}; "
                f"content_chars={len(content) if isinstance(content, str) else 0}; "
                f"reasoning_chars={len(reasoning) if isinstance(reasoning, str) else 0}"
            ) from exc
        if not isinstance(authored, dict) or authored.get("kind") != ComponentKind.SKILL.value:
            raise ControllerError("Component author returned an invalid candidate object")
        manifest = authored.get("manifest")
        source = authored.get("source")
        if not isinstance(manifest, dict) or not isinstance(source, str):
            raise ControllerError("Component author must return manifest and source")
        manifest = dict(manifest)
        manifest["entrypoint"] = "skill.py"
        manifest["origin"] = "agent"
        manifest["source_task_ids"] = list(evidence_ids)
        manifest.setdefault("mutation_reason", str(decision.get("reason") or objective)[:1000])
        manifest.setdefault("hypothesis", objective[:1000])
        proposal = self.skills.propose(manifest, source)
        benchmark = self.skills.benchmark(str(proposal["candidate_id"]), self.broker)
        return {
            **proposal,
            "kind": ComponentKind.SKILL.value,
            "objective": objective,
            "benchmark": benchmark,
            "author_model_usage": response.get("usage", {"model_calls": 1}),
            "adopted": False,
            "production_activated": False,
        }

    @classmethod
    def _redact_outbound(cls, value: Any) -> Any:
        """Remove credentials and host-local paths before model delivery."""
        if isinstance(value, dict):
            return {str(key): cls._redact_outbound(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact_outbound(item) for item in value]
        if not isinstance(value, str):
            return value
        substitutions = (
            (r'(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]+', r'\1[REDACTED]'),
            (r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}', '[REDACTED_TOKEN]'),
            (
                r'(?i)((?:api[_-]?key|access[_-]?token|token|password|secret|cookie)\s*[:=]\s*)'
                r'[^\s,;\}\]]+',
                r'\1[REDACTED]',
            ),
            (r'(?i)\b[A-Z]:\\Users\\[^\\\s"\']+(?:\\[^\s"\']*)?', '[REDACTED_HOST_PATH]'),
            (r'(?i)(?:/home|/Users)/[^/\s"\']+(?:/[^\s"\']*)?', '[REDACTED_HOST_PATH]'),
            (r'(https?://)[^/@\s:]+:[^/@\s]+@', r'\1[REDACTED]@'),
        )
        for pattern, replacement in substitutions:
            value = re.sub(pattern, replacement, value)
        return value


class AutonomousLineageController:
    def __init__(
        self, store: StateStore, reasoner: LineageReasoner,
        manager: LineageManager | None = None, *, components: ComponentRegistry | None = None,
        skills: SkillManager | None = None,
        component_authorer: ComponentCandidateAuthor | None = None,
        settings: Settings | None = None,
        lineage_evaluator: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.store = store
        self.reasoner = reasoner
        self.manager = manager or LineageManager(store, components, skills)
        self.component_authorer = component_authorer
        self.settings = settings
        self.lineage_evaluator = lineage_evaluator

    def decide(self, lineage_id: str | None = None, *, task_limit: int = 20) -> dict[str, Any]:
        root = self.manager.ensure_root()
        current_id = lineage_id or str(self.manager.current()["lineage_id"])
        facts = self._facts(current_id, task_limit)
        try:
            decision = self.reasoner.reason(facts)
        except ControllerError as exc:
            report = {
                "schema": "autonomous_lineage_decision/v1",
                "status": "protocol_failed",
                "model_decision": None,
                "error": str(exc),
                "previous_lineage_id": current_id,
                "effective_lineage_id": current_id,
                "production_activated": False,
                "host_fitness_judgment": False,
                "lineage_changed": False,
            }
            run_id = self.store.add_evolution_run(
                f"lineage:{current_id}", facts, [], "protocol_failed", report,
            )
            return {
                "run_id": run_id,
                **report,
                "lineage": self.manager.describe(current_id),
            }
        action = str(decision.get("action", "CONTINUE")).upper()
        if action not in LINEAGE_ACTIONS:
            raise EvolutionPolicyError(f"Unknown lineage action: {action}")
        candidate_ids: list[int] = []
        component_candidate_id: str | None = None
        component_candidate: dict[str, Any] | None = None
        run_status = "lineage_decided"
        if action == "CONTINUE":
            selected = self.manager.continue_lineage(current_id, actor="agent", decision=decision)
        elif action == "ADOPT_CANDIDATE":
            candidate_id = int(decision["candidate_id"])
            candidate_ids.append(candidate_id)
            selected = self.manager.adopt_candidate(
                current_id, candidate_id, actor="agent", decision=decision,
            )
        elif action == "ADOPT_COMPONENT_CANDIDATE":
            component_candidate_id = str(decision["component_candidate_id"])
            selected = self.manager.adopt_component_candidate(
                current_id, component_candidate_id, actor="agent", decision=decision,
            )
        elif action == "AUTHOR_COMPONENT_CANDIDATE":
            if self.component_authorer is None:
                raise EvolutionPolicyError("Component candidate authoring service is unavailable")
            try:
                authored = self.component_authorer.author(decision, facts, current_id)
            except (
                ControllerError, EvolutionPolicyError, SkillValidationError,
                OSError, RuntimeError, KeyError, TypeError, ValueError,
            ) as exc:
                failure = {
                    "schema": "autonomous_lineage_decision/v1",
                    "status": "component_candidate_authoring_failed",
                    "model_decision": decision,
                    "error": f"{type(exc).__name__}: {exc}",
                    "previous_lineage_id": current_id,
                    "effective_lineage_id": current_id,
                    "production_activated": False,
                    "host_fitness_judgment": False,
                    "lineage_changed": False,
                }
                self.store.add_lineage_event(
                    current_id, "component_candidate_authoring_failed", "agent", failure,
                )
                run_id = self.store.add_evolution_run(
                    f"lineage:{current_id}", facts, [],
                    "component_candidate_authoring_failed", failure,
                )
                return {"run_id": run_id, **failure, "lineage": self.manager.describe(current_id)}
            component_candidate_id = str(authored["candidate_id"])
            event = {
                "component_candidate_id": component_candidate_id,
                "kind": authored["kind"],
                "objective": authored["objective"],
                "benchmark": authored["benchmark"],
                "adopted": False,
                "production_activated": False,
            }
            component_candidate = event
            self.store.add_lineage_event(
                current_id, "component_candidate_authored", "agent", event,
            )
            selected = self.manager._lineage(current_id)
            run_status = "component_candidate_authored"
        elif action == "REQUEST_COUNTERFACTUAL_EVALUATION":
            if self.lineage_evaluator is None:
                raise EvolutionPolicyError("Lineage counterfactual evaluation service is unavailable")
            selected = self.manager._lineage(current_id)
            parent_id = selected.get("parent_lineage_id")
            if not parent_id or "component" in (selected.get("mutation") or {}):
                raise EvolutionPolicyError("Harness counterfactual evaluation requires a direct Harness parent")
            capsule_ids = decision.get("capsule_ids")
            if (
                not isinstance(capsule_ids, list) or not capsule_ids
                or len(capsule_ids) > 3
                or any(not isinstance(item, str) or not item for item in capsule_ids)
                or len(set(capsule_ids)) != len(capsule_ids)
            ):
                raise EvolutionPolicyError("REQUEST_COUNTERFACTUAL_EVALUATION requires 1-3 unique capsule_ids")
            eligible = {item["capsule_id"] for item in self._eligible_capsules()}
            unknown = [item for item in capsule_ids if item not in eligible]
            if unknown:
                raise EvolutionPolicyError(f"Capsules are not eligible Full pre-task replays: {unknown}")
            runs = decision.get("runs_per_variant", 1)
            if not isinstance(runs, int) or isinstance(runs, bool) or not 1 <= runs <= 3:
                raise EvolutionPolicyError("runs_per_variant must be an integer from 1 to 3")
            parent = self.manager._lineage(str(parent_id))
            try:
                evaluation = self.lineage_evaluator(parent, selected, decision)
            except Exception as exc:
                failure = {
                    "schema": "autonomous_lineage_decision/v1",
                    "status": "lineage_evaluation_failed", "model_decision": decision,
                    "error": f"{type(exc).__name__}: {exc}",
                    "previous_lineage_id": current_id, "effective_lineage_id": current_id,
                    "production_activated": False, "host_fitness_judgment": False,
                    "lineage_changed": False,
                }
                self.store.add_lineage_event(current_id, "counterfactual_evaluation_failed", "system", failure)
                run_id = self.store.add_evolution_run(
                    f"lineage:{current_id}", facts, [], "lineage_evaluation_failed", failure,
                )
                return {"run_id": run_id, **failure, "lineage": self.manager.describe(current_id)}
            event = self._evaluation_summary(evaluation)
            self.store.add_lineage_event(current_id, "counterfactual_evaluated", "system", event)
            run_status = "lineage_evaluated"
        elif action == "FORK_MUTATION":
            mutation = decision.get("mutation")
            if not isinstance(mutation, dict) or len(mutation) != 1:
                raise EvolutionPolicyError("FORK_MUTATION requires exactly one mutation")
            candidate_id = EvolutionManager(self.store).propose(
                mutation, str(decision.get("reason") or "Agent-authored lineage mutation"),
            )
            candidate_ids.append(candidate_id)
            selected = self.manager.adopt_candidate(
                current_id, candidate_id, actor="agent", decision=decision,
            )
        else:
            selected = self.manager.return_to(
                current_id, str(decision["target_lineage_id"]), actor="agent", decision=decision,
            )
        report = {
            "schema": "autonomous_lineage_decision/v1", "model_decision": decision,
            "previous_lineage_id": current_id, "effective_lineage_id": selected["lineage_id"],
            "production_activated": False, "host_fitness_judgment": False,
            "component_candidate_id": component_candidate_id,
            "component_candidate": component_candidate,
            "component_candidate_adopted": action == "ADOPT_COMPONENT_CANDIDATE",
        }
        if action == "REQUEST_COUNTERFACTUAL_EVALUATION":
            report["lineage_evaluation"] = evaluation
        self.manager.select_head(str(selected["lineage_id"]))
        run_id = self.store.add_evolution_run(
            f"lineage:{current_id}", facts, candidate_ids, run_status, report,
        )
        return {"run_id": run_id, **report, "lineage": selected}

    def _facts(self, lineage_id: str, task_limit: int) -> dict[str, Any]:
        lineage, lineage_history = self._lineage_experience(
            self.manager.describe(lineage_id)
        )
        tasks = []
        observations = []
        selected_tasks = self.store.lineage_tasks(
            lineage_id, max(1, min(int(task_limit), BEHAVIOR_POLICY["max_tasks"])),
        )
        digest_budget = min(3000, BEHAVIOR_POLICY["total_digest_characters"] // max(1, len(selected_tasks)))
        for task in selected_tasks:
            result = task.result if isinstance(task.result, dict) else {}
            evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
            digest, patterns = summarize_behavior(
                int(task.id), self.store.task_behavior_traces(int(task.id)),
            )
            observations.append((int(task.id), task.status.value, patterns))
            tasks.append({
                "task_id": task.id, "status": task.status.value,
                "error": ModelComponentCandidateAuthor._redact_outbound(str(task.error)[:256]) if task.error else None,
                "status_explanation": TASK_STATUS_SEMANTICS[task.status.value],
                "execution_policy": {
                    "source": "task_result_evidence",
                    "budget_limits_enabled": evidence.get("budget_limits_enabled"),
                    "agent_declared_stop": evidence.get("agent_declared_stop"),
                    "host_observed_completion": evidence.get("host_observed_completion"),
                    "missing_values_mean": "unknown; do not infer from current configuration",
                },
                "summary": ModelComponentCandidateAuthor._redact_outbound(str(result["summary"])[:1200]) if result.get("summary") else None,
                "summary_truncated": len(str(result.get("summary") or "")) > 1200,
                "behavior_digest": bound_digest(
                    ModelComponentCandidateAuthor._redact_outbound(digest), digest_budget,
                ),
                "metrics": {key: evidence.get(key) for key in (
                    "model_tokens", "model_api_calls", "task_tool_calls", "task_cycles", "failed_actions",
                    "repeated_resource_reads", "observation_reuse_hits",
                )},
            })
        cross_task = cross_task_patterns(observations)
        eligible_capsules = self._eligible_capsules()
        candidates = [{
            "candidate_id": item["id"], "mutation": item["mutation"],
            "rationale": item["rationale"], "status": item["status"],
        } for item in self.store.list_candidates() if item["status"] != "promoted"]
        component_candidates = []
        if self.manager.skills is not None:
            for item in self.manager.skills.list_candidates():
                benchmark = item.get("benchmark")
                component = self.manager.skills.candidate_component(str(item["candidate_id"]))
                component_candidates.append({
                    "component_candidate_id": str(item["candidate_id"]),
                    "kind": component["kind"],
                    "component_id": component["component_id"],
                    "name": component["metadata"]["name"],
                    "version": component["metadata"]["version"],
                    "provides": component["capabilities"]["provides"],
                    "benchmark": benchmark,
                    "eligible": isinstance(benchmark, dict) and benchmark.get("passed") is True,
                })
        ancestors = list(lineage.get("ancestors", []))
        return {
            "schema": "lineage_experience/v1", "current_lineage": lineage,
            "current_execution_config": (
                self.settings.execution_config_facts(lineage.get("settings") or {})
                if self.settings is not None else {
                    "available": False, "reason": "loaded Host settings were not supplied; effective policy is unknown",
                }
            ),
            "lineage_history": lineage_history,
            "projection_policy": {
                "historical_model_reason_visible": False,
                "task_limit_requested": task_limit, "max_tasks": BEHAVIOR_POLICY["max_tasks"],
                "summary_max_characters": 1200, "per_task_digest_characters": digest_budget,
            },
            "behavior_evidence_policy": dict(BEHAVIOR_POLICY),
            "cross_task_patterns": cross_task["patterns"],
            "cross_task_patterns_omitted": cross_task["omitted"],
            "task_status_semantics": TASK_STATUS_SEMANTICS,
            "lineage_tasks": tasks, "available_candidates": candidates[:20],
            "available_replay_capsules": eligible_capsules,
            "available_component_candidates": component_candidates[:20],
            "allowed_actions": sorted(LINEAGE_ACTIONS),
            "action_availability": {
                "CONTINUE": {
                    "available": True, "requires": [],
                    "effect": "keep the current lineage unchanged; does not execute or resume tasks",
                },
                "FORK_MUTATION": {
                    "available": True, "requires": ["mutation_contract"],
                    "depends_on_available_candidates": False,
                    "effect": "author a new candidate and create one child lineage",
                },
                "ADOPT_CANDIDATE": {
                    "available": bool(candidates), "requires": ["candidate_id"],
                    "reason_if_unavailable": "no unpromoted candidate",
                },
                "ADOPT_COMPONENT_CANDIDATE": {
                    "available": any(item["eligible"] for item in component_candidates),
                    "requires": ["component_candidate_id"],
                    "eligible_ids": [
                        item["component_candidate_id"]
                        for item in component_candidates if item["eligible"]
                    ],
                    "effect": "replace one Skill Component in an isolated child Component Set",
                },
                "AUTHOR_COMPONENT_CANDIDATE": {
                    "available": self.component_authorer is not None,
                    "requires": ["kind", "objective", "evidence_task_ids"],
                    "allowed_kinds": ["skill"],
                    "effect": (
                        "create and sandbox-benchmark one isolated Component candidate; "
                        "do not adopt it or change the lineage Component Set"
                    ),
                },
                "REQUEST_COUNTERFACTUAL_EVALUATION": {
                    "available": bool(
                        self.lineage_evaluator is not None
                        and lineage.get("parent_lineage_id")
                        and "component" not in (lineage.get("mutation") or {})
                        and eligible_capsules
                    ),
                    "requires": ["capsule_ids", "runs_per_variant"],
                    "eligible_capsule_ids": [item["capsule_id"] for item in eligible_capsules],
                    "bounds": {"capsules": [1, 3], "runs_per_variant": [1, 3]},
                    "effect": (
                        "re-execute direct-parent and current Harness variants from each identical "
                        "Full pre-task capsule; persist measured reports; no selection or lineage change"
                    ),
                    "host_fitness_judgment": False,
                    "production_activation": False,
                },
                "RETURN": {
                    "available": bool(ancestors), "requires": ["ancestor lineage id"],
                    "valid_targets": ancestors,
                },
            },
            "mutation_contract": {
                "surface": "harness", "exactly_one_field": True,
                "allowed_fields": sorted(ALLOWED_MUTATIONS),
                "fields": {
                    "prompt_append": {
                        "type": "string", "max_characters": 4000,
                        "controls": "persistent additional instruction appended to the controller prompt",
                        "effect_stage": "model planning and final-response generation",
                        "does_not_control": [
                            "tool permissions", "authority", "network access",
                            "model context window", "task token budget",
                        ],
                    },
                    "max_actions_per_cycle": {
                        "type": "integer", "minimum": 1, "maximum": 20,
                        "controls": "maximum executable tool actions admitted in one Runtime cycle",
                        "effect_stage": "per-cycle action execution",
                        "activation_condition": "budget.enabled=true; otherwise stored for inheritance but ignored by Runtime",
                        "does_not_control": [
                            "model calls per task", "task cycles", "task token budget",
                            "tool permissions", "external service access",
                        ],
                    },
                    "memory_context_characters": {
                        "type": "integer", "minimum": 500, "maximum": 20000,
                        "controls": "maximum characters of retrieved episodic-memory text added to task context",
                        "effect_stage": "pre-planning memory retrieval",
                        "does_not_control": [
                            "model context window", "reasoning output tokens",
                            "task token budget", "tool-result context", "cycle continuation budget",
                        ],
                    },
                    "harness_profile": {
                        "type": "enum", "values": sorted(HARNESS_PROFILES),
                        "controls": "controller scaffold profile used for task execution",
                        "effect_stage": "controller prompt and execution-policy selection",
                        "value_semantics": {
                            "minimal_open": "minimal scaffold with the widest model discretion",
                            "reduced": "reduced scaffold retaining selected execution guidance",
                            "structured": "full structured controller scaffold",
                        },
                        "does_not_control": [
                            "tool permissions", "authority", "network access",
                            "model context window", "task token budget",
                        ],
                    },
                },
                "preexisting_candidate_required_for_fork": False,
                "production_activation": False,
            },
            "component_mutation_contract": {
                "schema": "component_mutation/v1",
                "operations": ["author_candidate", "adopt_existing_candidate"],
                "component_kinds": {
                    "skill": {
                        "mutable": True, "agent_candidate": True,
                        "candidate_authoring": True, "lineage_adoption": True,
                        "authoring_requirements": {
                            "decision_fields": ["kind", "objective", "evidence_task_ids"],
                            "package_files": ["manifest.json", "skill.py"],
                            "minimum_tests": 1,
                            "execution": "isolated sandbox benchmark",
                            "adoption": "separate later lineage decision",
                        },
                        "requirements": ["validated package", "passed sandbox benchmark"],
                    },
                    "workflow": {
                        "mutable": False, "lineage_adoption": False,
                        "reason": "no governed executable Workflow candidate lifecycle",
                    },
                    "plugin": {
                        "mutable": False, "lineage_adoption": False,
                        "reason": "host-managed sidecar and authority boundary",
                    },
                    "resource_adapter": {
                        "mutable": False, "lineage_adoption": False,
                        "reason": "host-managed evidence boundary",
                    },
                    "environment_provider": {
                        "mutable": False, "lineage_adoption": False,
                        "reason": "host-managed execution environment boundary",
                    },
                    "primitive": {
                        "mutable": False, "lineage_adoption": False,
                        "reason": "kernel gateway",
                    },
                    "kernel_component": {
                        "mutable": False, "lineage_adoption": False,
                        "reason": "Root of Trust",
                    },
                },
                "production_activation": False,
            },
            "authority": {
                "mutable_surface": [
                    "harness_child_lineage", "skill_component_candidate_authoring",
                    "skill_component_child_lineage",
                ],
                "production_activation": False, "root_of_trust_mutation": False,
                "host_fitness_judgment": False,
            },
        }

    def _eligible_capsules(self) -> list[dict[str, Any]]:
        values = []
        for item in self.store.list_task_capsules(limit=20):
            if not isinstance(item, dict):
                continue
            if (
                item.get("status") != "replayable" or item.get("fidelity") != "full"
                or item.get("capture_phase") != "pre_task"
            ):
                continue
            task = item.get("task") if isinstance(item.get("task"), dict) else {}
            values.append({
                "capsule_id": item.get("capsule_id"),
                "source_task_id": item.get("source_task_id"),
                "title": ModelComponentCandidateAuthor._redact_outbound(str(task.get("title") or "")[:300]),
                "request": ModelComponentCandidateAuthor._redact_outbound(str(task.get("request") or "")[:700]),
                "fidelity": "full", "capture_phase": "pre_task", "status": "replayable",
            })
        return values[:10]

    @staticmethod
    def _evaluation_summary(evaluation: dict[str, Any]) -> dict[str, Any]:
        cases = []
        for item in evaluation.get("cases", [])[:3]:
            cases.append({key: item.get(key) for key in (
                "experiment_id", "capsule_id", "selection", "semantic_measurement",
                "baseline", "candidate", "delta",
            )})
        return {
            "schema": "lineage_counterfactual_evaluation_summary/v1",
            "parent_lineage_id": evaluation.get("parent_lineage_id"),
            "candidate_lineage_id": evaluation.get("candidate_lineage_id"),
            "cases": cases,
            "selection": "none_measurement_only",
            "production_activated": False, "host_fitness_judgment": False,
        }

    @staticmethod
    def _lineage_experience(
        described: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Project effective lineage facts without historical model narratives."""
        lineage = dict(described)
        events = list(lineage.pop("events", []))
        lineage.pop("decision", None)
        history: list[dict[str, Any]] = []
        for event in events:
            data = dict(event.get("data") or {})
            data.pop("decision", None)
            history.append({
                "event_id": event.get("id"),
                "action": event.get("action"),
                "actor": event.get("actor"),
                "effective_data": data,
                "created_at": event.get("created_at"),
            })
        return lineage, history
