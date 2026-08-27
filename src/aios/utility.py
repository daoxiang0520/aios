from __future__ import annotations

import json
import shlex
import statistics
import time
from dataclasses import dataclass
from typing import Any

from .sandbox import DockerSandboxBroker
from .skills import SkillManager
from .storage import StateStore
from .types import TaskStatus


@dataclass(frozen=True, slots=True)
class UtilityWeights:
    success: float = 5.0
    true_completion: float = 3.0
    model_calls: float = 0.5
    tokens: float = 0.0001
    latency_seconds: float = 0.02
    failure: float = 3.0


class SkillUtilityEvaluator:
    """Replay candidate behavior and compare it with explicit historical baselines."""

    def __init__(
        self, store: StateStore, manager: SkillManager,
        broker: DockerSandboxBroker, weights: UtilityWeights | None = None,
    ):
        self.store = store
        self.manager = manager
        self.broker = broker
        self.weights = weights or UtilityWeights()

    def replay(self, candidate_id: str, *, runs: int = 3) -> dict[str, Any]:
        package, manifest, _ = self.manager._candidate(candidate_id)
        benchmark_path = self.manager.reports / f"{candidate_id}.json"
        benchmark = json.loads(benchmark_path.read_text(encoding="utf-8")) if benchmark_path.is_file() else {}
        if not benchmark.get("passed"):
            raise RuntimeError("Candidate must pass the Docker benchmark before replay utility evaluation")
        runs = max(1, min(int(runs), 10))
        cases = manifest.tests or [{"input": {}, "expect_exit": 0}]
        observations: list[dict[str, Any]] = []
        for repetition in range(1, runs + 1):
            for case_index, case in enumerate(cases, 1):
                payload = json.dumps(case.get("input", {}), ensure_ascii=False)
                started = time.perf_counter()
                result = self.broker.run_candidate(
                    package,
                    f"python /candidate/{manifest.entrypoint} --input-json {shlex.quote(payload)}",
                    timeout_seconds=self.manager.config.benchmark_timeout_seconds,
                )
                duration_ms = (time.perf_counter() - started) * 1000
                contains = case.get("stdout_contains")
                passed = result["exit_code"] == int(case.get("expect_exit", 0)) and (
                    not isinstance(contains, str) or contains in result["stdout"]
                )
                observations.append({
                    "repetition": repetition, "case": case_index, "passed": passed,
                    "exit_code": result["exit_code"], "duration_ms": duration_ms,
                    "stdout_digest": self._digest(result.get("stdout", "")),
                })
        replay_metrics = self._replay_metrics(observations)
        baseline = self._historical_baseline(manifest.replay_task_ids, manifest.name)
        if baseline is None:
            utility_delta = None
            negative_transfer = False
            evidence_level = "insufficient_historical_baseline"
            passed = False
        else:
            proxy = {
                "success_rate": replay_metrics["success_rate"],
                "true_completion_rate": replay_metrics["success_rate"],
                "model_calls": 2.0,
                "tokens": baseline["tokens"],
                "latency_ms": replay_metrics["median_latency_ms"],
                "failure_rate": 1.0 - replay_metrics["success_rate"],
            }
            baseline_score = self.score(baseline)
            proxy_score = self.score(proxy)
            utility_delta = proxy_score - baseline_score
            negative_transfer = (
                proxy["success_rate"] < baseline["success_rate"]
                or utility_delta < -0.05
                or not replay_metrics["deterministic_output"]
            )
            evidence_level = "historical_baseline_plus_execution_proxy"
            passed = replay_metrics["all_passed"] and not negative_transfer
        report = {
            "kind": "skill_replay_utility_v1", "candidate_id": candidate_id,
            "skill": manifest.name, "version": manifest.version,
            "replay_task_ids": manifest.replay_task_ids,
            "runs": runs, "replay": replay_metrics, "baseline": baseline,
            "utility_delta": utility_delta, "negative_transfer": negative_transfer,
            "evidence_level": evidence_level, "passed": passed,
            "promotion_gate": "pass" if passed else "blocked",
            "assumptions": {
                "skill_enabled_model_calls": 2,
                "tokens": "not observed by direct replay; held equal to baseline in utility score",
                "causality": "proxy comparison; end-to-end prospective A/B remains required",
            },
            "weights": self.weights.__dict__ if hasattr(self.weights, "__dict__") else {
                name: getattr(self.weights, name) for name in self.weights.__slots__
            },
        }
        report["report_id"] = self.store.add_skill_replay_report(report)
        (self.manager.reports / f"{candidate_id}.replay.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report

    def observed_utility(self, skill_name: str, *, limit: int = 500) -> dict[str, Any]:
        rows = self.store.list_skill_usage(limit=limit, skill_name=skill_name)
        if not rows:
            return {"skill": skill_name, "evidence_level": "no_usage", "utility": None, "samples": 0}
        completed = [row for row in rows if row.get("verifier_passed")]
        failures = [row for row in rows if row.get("status") != "success"]
        metrics = {
            "success_rate": sum(row.get("status") == "success" for row in rows) / len(rows),
            "true_completion_rate": len(completed) / len(rows),
            "model_calls": statistics.fmean(
                max(0, int(row.get("model_calls_after") or 0) - int(row.get("model_calls_before") or 0))
                for row in rows
            ),
            "tokens": statistics.fmean(
                max(0, int(row.get("tokens_after") or 0) - int(row.get("tokens_before") or 0))
                for row in rows
            ),
            "latency_ms": statistics.fmean(float(row.get("duration_ms") or 0) for row in rows),
            "failure_rate": len(failures) / len(rows),
        }
        return {
            "skill": skill_name, "samples": len(rows), "evidence_level": "observed_association",
            "metrics": metrics, "utility": self.score(metrics),
            "negative_transfer_rate": sum(
                row.get("status") == "success" and not row.get("verifier_passed", False)
                for row in rows
            ) / len(rows),
            "causality": "association only; compare matched prospective A/B before automatic promotion",
        }

    def latest_comparison(self, candidate_id: str) -> dict[str, Any] | None:
        reports = self.store.list_skill_replay_reports(limit=1, candidate_id=candidate_id)
        return reports[0] if reports else None

    def score(self, metrics: dict[str, Any]) -> float:
        def value(name: str) -> float:
            raw = metrics.get(name)
            return float(raw) if isinstance(raw, (int, float)) else 0.0
        return round(
            self.weights.success * value("success_rate")
            + self.weights.true_completion * value("true_completion_rate")
            - self.weights.model_calls * value("model_calls")
            - self.weights.tokens * value("tokens")
            - self.weights.latency_seconds * (value("latency_ms") / 1000.0)
            - self.weights.failure * value("failure_rate"),
            6,
        )

    def _historical_baseline(self, task_ids: list[int], skill_name: str) -> dict[str, Any] | None:
        samples: list[dict[str, float]] = []
        contaminated_task_ids = {
            int(row["task_id"]) for row in self.store.list_skill_usage(limit=5000, skill_name=skill_name)
            if row.get("task_id") is not None
        }
        for task_id in task_ids:
            if task_id in contaminated_task_ids:
                continue
            task = self.store.get_task(task_id)
            if task is None or not isinstance(task.result, dict):
                continue
            evidence = task.result.get("evidence") or {}
            action_results = task.result.get("action_results") or []
            samples.append({
                "success_rate": 1.0 if task.status == TaskStatus.COMPLETED else 0.0,
                "true_completion_rate": 1.0 if evidence.get("success") else 0.0,
                "model_calls": float(evidence.get("model_api_calls", evidence.get("model_rounds", 0)) or 0),
                "tokens": float(evidence.get("model_tokens", 0) or 0),
                "latency_ms": sum(float(item.get("duration_ms", 0) or 0) for item in action_results if isinstance(item, dict)),
                "failure_rate": 0.0 if evidence.get("success") else 1.0,
            })
        if not samples:
            return None
        return {
            name: statistics.fmean(sample[name] for sample in samples)
            for name in samples[0]
        } | {"samples": len(samples)}

    @staticmethod
    def _replay_metrics(observations: list[dict[str, Any]]) -> dict[str, Any]:
        latencies = [item["duration_ms"] for item in observations]
        return {
            "samples": len(observations),
            "all_passed": all(item["passed"] for item in observations),
            "success_rate": sum(item["passed"] for item in observations) / len(observations),
            "median_latency_ms": statistics.median(latencies),
            "p95_latency_ms": sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)],
            "deterministic_output": all(
                len({item["stdout_digest"] for item in observations if item["case"] == case}) == 1
                for case in {item["case"] for item in observations}
            ),
            "observations": observations,
        }

    @staticmethod
    def _digest(value: str) -> str:
        import hashlib
        return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
