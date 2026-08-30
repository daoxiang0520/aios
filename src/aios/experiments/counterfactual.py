from __future__ import annotations

import math
import json
import os
import re
import statistics
from collections.abc import Callable
from typing import Any

from ..config import ModelConfig
from ..controller import ControllerError, LLMController
from .models import CapsuleFidelity, PromotionState


class ModelSemanticJudge:
    """OpenAI-compatible JSON pairwise judge; labels contain no variant identity."""

    def __init__(self, config: ModelConfig):
        self.config = config
        self.controller = LLMController(config)

    def __call__(self, task: str, output_a: str, output_b: str) -> dict[str, Any]:
        if self.config.provider == "mock":
            return {"winner": "tie", "confidence": 0.0, "verdict": "insufficient_evidence"}
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.config.api_key_env):
            raise ControllerError("model.api_key_env must be an environment-variable name")
        key = os.environ.get(self.config.api_key_env)
        if not key:
            raise ControllerError(f"Missing API key environment variable: {self.config.api_key_env}")
        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Blindly compare output A and B against the task. Return one JSON object with "
                        "winner A, B, or tie; confidence 0..1; correctness; completeness; grounding; "
                        "usefulness; and verdict. Refuse with tie and confidence 0 when evidence is insufficient."
                    ),
                },
                {"role": "user", "content": json.dumps({"task": task, "output_A": output_a, "output_B": output_b}, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": min(self.config.max_tokens, 1000),
            "response_format": {"type": "json_object"},
        }
        response = self.controller._send_request(payload, key)
        try:
            content = response["choices"][0]["message"]["content"]
            result = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ControllerError("Semantic judge returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ControllerError("Semantic judge returned an invalid object")
        return result


class PairwiseSemanticJudge:
    """Optional soft-evidence judge with order reversal to expose position bias."""

    def __init__(self, judge: Callable[[str, str, str], dict[str, Any]] | None = None):
        self.judge = judge

    def evaluate(self, task: str, baseline_output: str, candidate_output: str) -> dict[str, Any]:
        if self.judge is None or not baseline_output or not candidate_output:
            return {"verdict": "insufficient_evidence", "confidence": 0.0, "tier": "model_judged"}
        try:
            first = self.judge(task, baseline_output, candidate_output)
            second = self.judge(task, candidate_output, baseline_output)
        except Exception as exc:
            return {
                "verdict": "insufficient_evidence", "confidence": 0.0,
                "judge_error": type(exc).__name__, "tier": "model_judged",
            }
        first_winner = self._map_winner(first.get("winner"), a="baseline", b="candidate")
        second_winner = self._map_winner(second.get("winner"), a="candidate", b="baseline")
        if first_winner != second_winner:
            return {
                "verdict": "insufficient_evidence", "confidence": 0.0,
                "position_bias_detected": True, "orders": [first, second], "tier": "model_judged",
            }
        confidence = min(float(first.get("confidence", 0.0)), float(second.get("confidence", 0.0)))
        return {
            "verdict": f"{first_winner}_better" if first_winner != "tie" else "tie",
            "winner": first_winner, "confidence": max(0.0, min(1.0, confidence)),
            "position_bias_detected": False, "orders": [first, second], "tier": "model_judged",
        }

    @staticmethod
    def _map_winner(value: Any, *, a: str, b: str) -> str:
        normalized = str(value or "").upper()
        if normalized == "A":
            return a
        if normalized == "B":
            return b
        return "tie"


class CounterfactualEvaluator:
    """Compare objective vectors first; semantic judgement remains subordinate soft evidence."""

    def evaluate(
        self, baseline_runs: list[dict[str, Any]], candidate_runs: list[dict[str, Any]],
        *, fidelity: str, semantic: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        semantic = semantic or {"verdict": "insufficient_evidence", "tier": "model_judged"}
        if not baseline_runs or not candidate_runs:
            return self._report(
                {}, {}, semantic, fidelity, PromotionState.INSUFFICIENT_EVIDENCE,
                "Both variants require observed runs",
            )
        baseline = self.aggregate(baseline_runs)
        candidate = self.aggregate(candidate_runs)
        hard_security_failure = candidate["security_violations"] > 0
        lower_correctness = (
            candidate["true_completion_rate"] < baseline["true_completion_rate"]
            or candidate["success_rate"] < baseline["success_rate"]
        )
        if hard_security_failure:
            state, reason = PromotionState.REJECTED, "Candidate produced a security violation"
        elif lower_correctness:
            state, reason = PromotionState.REJECTED, "Candidate reduced objective correctness"
        elif fidelity == CapsuleFidelity.METADATA_ONLY.value:
            state, reason = PromotionState.INSUFFICIENT_EVIDENCE, "Capsule cannot be re-executed"
        else:
            quality_gain = (
                candidate["true_completion_rate"] > baseline["true_completion_rate"]
                or candidate["success_rate"] > baseline["success_rate"]
            )
            cost_improved = any(
                candidate[key] < baseline[key]
                for key in (
                    "median_model_calls", "median_tool_calls", "median_cycles",
                    "median_tokens", "median_latency_ms",
                )
            )
            cost_worse = any(
                candidate[key] > baseline[key]
                for key in (
                    "median_model_calls", "median_tool_calls", "median_cycles",
                    "median_tokens", "median_latency_ms",
                )
            )
            semantic_verdict = semantic.get("verdict")
            if semantic_verdict == "baseline_better":
                state, reason = PromotionState.NEEDS_REVIEW, "Soft semantic evidence conflicts with hard metrics"
            elif quality_gain and cost_worse:
                state, reason = PromotionState.NEEDS_REVIEW, "Quality improved at higher measured cost"
            elif cost_improved and cost_worse:
                state, reason = PromotionState.NEEDS_REVIEW, "Cost vector has a Pareto trade-off"
            elif quality_gain or cost_improved:
                if fidelity == CapsuleFidelity.FULL.value:
                    state, reason = PromotionState.PROMOTABLE, "Candidate is not worse and improves measured utility"
                else:
                    state, reason = PromotionState.NEEDS_REVIEW, "Candidate improved, but capsule fidelity is partial"
            else:
                state, reason = PromotionState.NEEDS_REVIEW, "Objective metrics are tied"
        return self._report(baseline, candidate, semantic, fidelity, state, reason)

    @staticmethod
    def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
        def values(group: str, key: str) -> list[float]:
            return [float(run.get(group, {}).get(key, 0) or 0) for run in runs]

        latencies = values("cost", "wall_time_ms")
        statuses = [run.get("outcome", {}).get("task_status") for run in runs]
        verifier = [bool(run.get("outcome", {}).get("verifier_pass")) for run in runs]
        true_completion = [bool(run.get("outcome", {}).get("true_completion")) for run in runs]
        return {
            "runs": len(runs),
            "success_rate": sum(status == "completed" for status in statuses) / len(runs),
            "verifier_pass_rate": sum(verifier) / len(runs),
            "true_completion_rate": sum(true_completion) / len(runs),
            "completion_consistency": 1.0 if len(set(true_completion)) == 1 else 0.0,
            "median_model_calls": statistics.median(values("cost", "model_calls")),
            "median_tool_calls": statistics.median(values("cost", "tool_calls")),
            "median_cycles": statistics.median(values("cost", "cycles")),
            "median_tokens": statistics.median(values("cost", "tokens")),
            "median_latency_ms": statistics.median(latencies),
            "p95_latency_ms": sorted(latencies)[max(0, math.ceil(0.95 * len(latencies)) - 1)],
            "security_violations": sum(len(run.get("security", {}).get("violations", [])) for run in runs),
        }

    @staticmethod
    def _report(
        baseline: dict[str, Any], candidate: dict[str, Any], semantic: dict[str, Any],
        fidelity: str, state: PromotionState, reason: str,
    ) -> dict[str, Any]:
        comparable = bool(baseline and candidate)
        delta = {}
        if comparable:
            for key in (
                "success_rate", "verifier_pass_rate", "true_completion_rate",
                "completion_consistency",
                "median_model_calls", "median_tool_calls", "median_cycles",
                "median_tokens", "median_latency_ms", "p95_latency_ms",
            ):
                delta[key] = round(float(candidate[key]) - float(baseline[key]), 6)
        return {
            "kind": "counterfactual_report_v1", "evidence_level": "counterfactual_reexecution",
            "selection_rule": {
                "hard_constraints_first": ["security", "success", "verifier", "true_completion"],
                "optimization_vector": [
                    "quality", "model_calls", "tool_calls", "cycles", "tokens",
                    "latency", "completion_consistency",
                ],
                "pareto_comparison": True,
                "single_scalar_reward": False,
            },
            "evidence_tiers": {
                "tier_1_measured": [
                    "verifier", "task_status", "model_calls", "tool_calls", "cycles",
                    "tokens", "latency", "security",
                ],
                "tier_2_model_judged": semantic,
                "tier_3_self_reported": "not_used_for_promotion",
            },
            "fidelity": fidelity, "baseline": baseline, "candidate": candidate,
            "delta": delta, "negative_transfer": state == PromotionState.REJECTED,
            "promotion_state": state.value, "reason": reason,
        }
