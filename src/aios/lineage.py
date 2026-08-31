from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Protocol

from .controller import ControllerError, LLMController
from .evolution import (
    ALLOWED_MUTATIONS, HARNESS_PROFILES, EvolutionManager, EvolutionPolicyError,
)
from .storage import StateStore


LINEAGE_ACTIONS = {"CONTINUE", "ADOPT_CANDIDATE", "FORK_MUTATION", "RETURN"}


class LineageReasoner(Protocol):
    def reason(self, facts: dict[str, Any]) -> dict[str, Any]: ...


class LineageManager:
    """Versioned Harness heredity without production promotion or Host fitness selection."""

    ROOT_ID = "lin_root"
    HEAD_STATE_KEY = "experimental_lineage_head"

    def __init__(self, store: StateStore):
        self.store = store

    def ensure_root(self) -> dict[str, Any]:
        existing = self.store.get_lineage(self.ROOT_ID)
        if existing is not None:
            return existing
        harness = self.store.active_harness()
        self.store.create_lineage({
            "lineage_id": self.ROOT_ID, "parent_lineage_id": None, "generation": 0,
            "kind": "harness", "status": "living", "settings": harness.get("settings", {}),
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
        return lineage

    def select_head(self, lineage_id: str) -> dict[str, Any]:
        lineage = self._lineage(lineage_id)
        self.store.set_state(self.HEAD_STATE_KEY, lineage_id)
        return lineage

    def fork(
        self, parent_lineage_id: str, mutation: dict[str, Any], *, actor: str,
        decision: dict[str, Any], source_candidate_id: int | None = None,
    ) -> dict[str, Any]:
        parent = self._lineage(parent_lineage_id)
        if parent["kind"] != "harness":
            raise EvolutionPolicyError("alpha.5 can execute only Harness lineages")
        if len(mutation) != 1:
            raise EvolutionPolicyError("An autonomous fork must contain exactly one mutation")
        EvolutionManager._validate_mutation(mutation)
        lineage_id = "lin_" + uuid.uuid4().hex[:16]
        child = {
            "lineage_id": lineage_id, "parent_lineage_id": parent_lineage_id,
            "generation": int(parent["generation"]) + 1, "kind": "harness", "status": "living",
            "settings": {**parent["settings"], **mutation}, "mutation": mutation,
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
        return lineage


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
                    "You control one experimental AIOS Harness lineage. Decide how your own lineage "
                    "continues from immutable observations. Allowed actions: CONTINUE; ADOPT_CANDIDATE "
                    "with candidate_id; FORK_MUTATION with exactly one allowed Harness mutation; RETURN "
                    "to an ancestor lineage. A fork affects only its child lineage, never production. "
                    "Available candidates may be empty: FORK_MUTATION authors a new candidate directly "
                    "from mutation_contract and does not require a pre-existing candidate. "
                    "The Host does not judge which lineage is better. Return one JSON object with action, "
                    "reason, evidence_task_ids, and the fields required by the chosen action. Keep the final "
                    "JSON concise; do not spend the response restating the supplied facts."
                ),
            }, {"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
            "temperature": 0.1, "max_tokens": min(config.max_tokens, 4096),
            "response_format": {"type": "json_object"},
        }
        if config.provider == "deepseek":
            thinking = config.thinking if config.thinking in {"enabled", "disabled"} else "disabled"
            request["thinking"] = {"type": thinking}
        response = self.controller._send_request(request, key)
        try:
            decision = json.loads(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ControllerError("Lineage reasoner returned invalid JSON") from exc
        if not isinstance(decision, dict):
            raise ControllerError("Lineage reasoner returned an invalid object")
        decision["model_usage"] = response.get("usage", {"model_calls": 1})
        return decision


class AutonomousLineageController:
    def __init__(
        self, store: StateStore, reasoner: LineageReasoner,
        manager: LineageManager | None = None,
    ):
        self.store = store
        self.reasoner = reasoner
        self.manager = manager or LineageManager(store)

    def decide(self, lineage_id: str | None = None, *, task_limit: int = 20) -> dict[str, Any]:
        root = self.manager.ensure_root()
        current_id = lineage_id or str(self.manager.current()["lineage_id"])
        facts = self._facts(current_id, task_limit)
        decision = self.reasoner.reason(facts)
        action = str(decision.get("action", "CONTINUE")).upper()
        if action not in LINEAGE_ACTIONS:
            raise EvolutionPolicyError(f"Unknown lineage action: {action}")
        candidate_ids: list[int] = []
        if action == "CONTINUE":
            selected = self.manager.continue_lineage(current_id, actor="agent", decision=decision)
        elif action == "ADOPT_CANDIDATE":
            candidate_id = int(decision["candidate_id"])
            candidate_ids.append(candidate_id)
            selected = self.manager.adopt_candidate(
                current_id, candidate_id, actor="agent", decision=decision,
            )
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
        }
        self.manager.select_head(str(selected["lineage_id"]))
        run_id = self.store.add_evolution_run(
            f"lineage:{current_id}", facts, candidate_ids, "lineage_decided", report,
        )
        return {"run_id": run_id, **report, "lineage": selected}

    def _facts(self, lineage_id: str, task_limit: int) -> dict[str, Any]:
        lineage = self.manager.describe(lineage_id)
        tasks = []
        for task in self.store.lineage_tasks(lineage_id, task_limit):
            evidence = task.result.get("evidence", {}) if isinstance(task.result, dict) else {}
            tasks.append({
                "task_id": task.id, "status": task.status.value, "error": task.error,
                "summary": task.result.get("summary") if isinstance(task.result, dict) else None,
                "metrics": {key: evidence.get(key) for key in (
                    "model_tokens", "model_api_calls", "task_cycles", "failed_actions",
                    "repeated_resource_reads", "observation_reuse_hits",
                )},
            })
        candidates = [{
            "candidate_id": item["id"], "mutation": item["mutation"],
            "rationale": item["rationale"], "status": item["status"],
        } for item in self.store.list_candidates() if item["status"] != "promoted"]
        ancestors = list(lineage.get("ancestors", []))
        return {
            "schema": "lineage_experience/v1", "current_lineage": lineage,
            "lineage_tasks": tasks, "available_candidates": candidates[:20],
            "allowed_actions": sorted(LINEAGE_ACTIONS),
            "action_availability": {
                "CONTINUE": {"available": True, "requires": []},
                "FORK_MUTATION": {
                    "available": True, "requires": ["mutation_contract"],
                    "depends_on_available_candidates": False,
                    "effect": "author a new candidate and create one child lineage",
                },
                "ADOPT_CANDIDATE": {
                    "available": bool(candidates), "requires": ["candidate_id"],
                    "reason_if_unavailable": "no unpromoted candidate",
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
                    "prompt_append": {"type": "string", "max_characters": 4000},
                    "max_actions_per_cycle": {"type": "integer", "minimum": 1, "maximum": 20},
                    "memory_context_characters": {
                        "type": "integer", "minimum": 500, "maximum": 20000,
                    },
                    "harness_profile": {
                        "type": "enum", "values": sorted(HARNESS_PROFILES),
                    },
                },
                "preexisting_candidate_required_for_fork": False,
                "production_activation": False,
            },
            "authority": {
                "mutable_surface": "harness_child_lineage_only",
                "production_activation": False, "root_of_trust_mutation": False,
                "host_fitness_judgment": False,
            },
        }
