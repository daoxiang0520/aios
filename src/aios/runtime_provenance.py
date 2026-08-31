from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .config import Settings
from .runtime_evolution import RuntimeMutationPolicy
from .storage import StateStore
from .types import TaskStatus


class RuntimeProvenanceManager:
    """Bind each execution cycle to immutable failure-time source and evaluator state."""

    SCHEMA = "runtime_provenance/v1"
    TERMINAL_STATUSES = {
        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.DEAD_LETTER,
        TaskStatus.DEGRADED, TaskStatus.BLOCKED_CAPABILITY,
        TaskStatus.NEEDS_AUTHORITY, TaskStatus.TERMINAL_FAILURE,
        TaskStatus.NEEDS_REVIEW, TaskStatus.STOPPED,
        TaskStatus.YIELDED, TaskStatus.ABANDONED,
    }

    def __init__(self, settings: Settings, store: StateStore):
        # Import lazily: experiments.__init__ exposes the replay runner, which imports Runtime.
        from .experiments.snapshot import ContentAddressedSnapshotStore

        self.settings = settings
        self.store = store
        self.root = (settings.experiments_root / "runtime_provenance").resolve()
        self.snapshots = ContentAddressedSnapshotStore(self.root / "source_objects")

    def capture_cycle(self, task_id: int, cycle_id: str) -> dict[str, Any]:
        source_paths = self._source_paths()
        evaluator_paths = self._evaluator_paths(source_paths)
        execution = self.snapshots.capture_paths(self.settings.root, source_paths)
        evaluator = self.snapshots.capture_paths(self.settings.root, evaluator_paths)
        root_of_trust = {
            path: reason for path, reason in RuntimeMutationPolicy.ROOT_OF_TRUST.items()
            if path in source_paths
        }
        material = {
            "schema": self.SCHEMA,
            "task_id": task_id,
            "cycle_id": cycle_id,
            "execution_runtime_snapshot": execution,
            "evaluation_snapshot": evaluator,
            "source_roles": {
                "execution_runtime": source_paths,
                "evaluation": evaluator_paths,
                "root_of_trust": root_of_trust,
            },
            "root_of_trust_policy_digest": self._digest(root_of_trust),
            "git": self._git_identity(),
            "capture_contract": {
                "phase": "before_intent_and_preflight",
                "content_addressed": True,
                "production_activation": "forbidden",
                "candidate_is_fitness_authority": False,
            },
        }
        material["snapshot_id"] = "rps_" + self._digest(material)
        checkpoint_id = self.store.add_checkpoint(task_id, "runtime_provenance", material)
        self.store.trace(cycle_id, "runtime_provenance_bound", {
            "task_id": task_id,
            "checkpoint_id": checkpoint_id,
            "snapshot_id": material["snapshot_id"],
            "execution_manifest_hash": execution["manifest_hash"],
            "evaluation_manifest_hash": evaluator["manifest_hash"],
            "root_of_trust_policy_digest": material["root_of_trust_policy_digest"],
        })
        return material

    def bindings(self, task_id: int) -> list[dict[str, Any]]:
        return [
            checkpoint["data"] for checkpoint in self.store.task_checkpoints(task_id)
            if checkpoint["phase"] == "runtime_provenance"
        ]

    def restore_execution(
        self, task_id: int, destination: Path, *, cycle_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore the immutable execution-time tree for an eligible historical cycle."""
        bindings = self.bindings(task_id)
        if cycle_id is not None:
            bindings = [item for item in bindings if str(item.get("cycle_id")) == cycle_id]
        if not bindings:
            target = f" cycle {cycle_id}" if cycle_id is not None else ""
            raise FileNotFoundError(f"Task {task_id} has no Runtime provenance binding for{target}")
        binding = bindings[-1]
        manifest_hash = str(binding["execution_runtime_snapshot"]["manifest_hash"])
        restored = self.snapshots.restore(manifest_hash, destination)
        return {
            **restored,
            "task_id": task_id,
            "cycle_id": binding.get("cycle_id"),
            "snapshot_id": binding.get("snapshot_id"),
            "execution_manifest_hash": manifest_hash,
        }

    def assess(self, task_id: int) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        checkpoints = self.store.task_checkpoints(task_id)
        bindings = self.bindings(task_id)
        cycle_ids = sorted({
            str(item["data"].get("cycle_id")) for item in checkpoints
            if item["data"].get("cycle_id")
        })
        bound_cycles = {str(item.get("cycle_id")) for item in bindings}
        traces = self.store.traces_for_cycles(cycle_ids)
        kinds = {str(item["kind"]) for item in traces}
        integrity = [self._verify_binding(item) for item in bindings]
        source_time_aligned = bool(cycle_ids) and set(cycle_ids).issubset(bound_cycles)
        source_integrity = bool(integrity) and all(item["valid"] for item in integrity)

        missing_decisions = []
        if "runtime_provenance_bound" not in kinds:
            missing_decisions.append("runtime_provenance_bound")
        if "intent_selected" not in kinds:
            missing_decisions.append("intent_selected")
        if "capability_preflight" not in kinds:
            missing_decisions.append("capability_preflight")
        preflight_blocked = task.status in {TaskStatus.BLOCKED_CAPABILITY, TaskStatus.NEEDS_AUTHORITY}
        if not preflight_blocked and "evaluation" not in kinds and "cycle_failed" not in kinds:
            missing_decisions.append("evaluation_or_cycle_failed")
        if task.status in self.TERMINAL_STATUSES and "task_terminal_decision" not in kinds:
            missing_decisions.append("task_terminal_decision")
        continuation_seen = any(
            item["phase"] in {"budget_deferred", "continued"} for item in checkpoints
        )
        if continuation_seen and not kinds.intersection({
            "budget_deferred", "stale_continuation_discarded", "terminal_task_event_discarded",
        }):
            missing_decisions.append("continuation_decision")

        trace_sufficient = not missing_decisions
        root_of_trust_known = bool(bindings) and all(
            self._root_of_trust_binding_known(item) for item in bindings
        )
        evaluator_snapshot_known = source_integrity and all(
            item.get("evaluation_snapshot", {}).get("manifest_hash")
            and int(item.get("evaluation_snapshot", {}).get("file_count", 0)) > 0
            for item in bindings
        )
        external_gates = sorted(
            path.name for path in (self.settings.root / "external_evaluators").glob(
                f"task{task_id}_*.py"
            ) if path.is_file()
        )
        external_gate_available = len(external_gates) == 1
        diagnosis_eligible = trace_sufficient
        repair_eligible = bool(
            diagnosis_eligible
            and source_time_aligned
            and source_integrity
            and evaluator_snapshot_known
            and external_gate_available
            and root_of_trust_known
        )
        return {
            "schema": "runtime_repair_eligibility/v1",
            "task_id": task_id,
            "task_status": task.status.value,
            "diagnosis_eligible": diagnosis_eligible,
            "repair_eligible": repair_eligible,
            "trace_complete": trace_sufficient,
            "source_time_aligned": source_time_aligned,
            "source_integrity": source_integrity,
            "evaluator_snapshot_known": evaluator_snapshot_known,
            "external_gate_available": external_gate_available,
            "root_of_trust_known": root_of_trust_known,
            "missing_causal_decisions": missing_decisions,
            "cycle_ids": cycle_ids,
            "bound_cycle_ids": sorted(bound_cycles),
            "snapshot_ids": [item.get("snapshot_id") for item in bindings],
            "external_gates": external_gates,
            "external_gate_state": (
                "available" if len(external_gates) == 1
                else "missing" if not external_gates else "ambiguous"
            ),
            "integrity": integrity,
            "contract": {
                "diagnosis_eligible": "TraceSufficient",
                "repair_eligible": (
                    "TraceComplete AND SourceTimeAligned AND SourceIntegrity AND "
                    "EvaluatorSnapshotKnown AND ExternalGateAvailable AND RootOfTrustKnown"
                ),
            },
        }

    def _verify_binding(self, binding: dict[str, Any]) -> dict[str, Any]:
        execution_hash = str(binding.get("execution_runtime_snapshot", {}).get("manifest_hash", ""))
        evaluator_hash = str(binding.get("evaluation_snapshot", {}).get("manifest_hash", ""))
        execution = self.snapshots.verify(execution_hash)
        evaluator = self.snapshots.verify(evaluator_hash)
        return {
            "snapshot_id": binding.get("snapshot_id"),
            "valid": bool(execution["valid"] and evaluator["valid"]),
            "execution": execution,
            "evaluation": evaluator,
        }

    def _root_of_trust_binding_known(self, binding: dict[str, Any]) -> bool:
        roles = binding.get("source_roles", {})
        root_of_trust = roles.get("root_of_trust") if isinstance(roles, dict) else None
        if not isinstance(root_of_trust, dict):
            return False
        if set(root_of_trust) != set(RuntimeMutationPolicy.ROOT_OF_TRUST):
            return False
        return binding.get("root_of_trust_policy_digest") == self._digest(root_of_trust)

    def _source_paths(self) -> list[str]:
        paths = [
            path.relative_to(self.settings.root).as_posix()
            for path in (self.settings.root / "src" / "aios").rglob("*.py")
            if path.is_file() and "__pycache__" not in path.parts
        ]
        if (self.settings.root / "pyproject.toml").is_file():
            paths.append("pyproject.toml")
        return sorted(paths)

    def _evaluator_paths(self, source_paths: list[str]) -> list[str]:
        explicit = {
            "src/aios/answers.py", "src/aios/evaluation.py", "src/aios/situation.py",
            "src/aios/runtime_evolution.py", "src/aios/runtime_provenance.py",
        }
        paths = [path for path in source_paths if path in explicit]
        external = self.settings.root / "external_evaluators"
        if external.is_dir():
            paths.extend(
                path.relative_to(self.settings.root).as_posix()
                for path in external.rglob("*.py")
                if path.is_file() and "__pycache__" not in path.parts
            )
        return sorted(set(paths))

    def _git_identity(self) -> dict[str, Any] | None:
        try:
            commit = subprocess.run(
                ["git", "-C", str(self.settings.root), "rev-parse", "HEAD"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=8, check=False,
            )
            status = subprocess.run(
                ["git", "-C", str(self.settings.root), "status", "--porcelain"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=8, check=False,
            )
            if commit.returncode != 0:
                return None
            status_text = status.stdout if status.returncode == 0 else ""
            return {
                "commit": commit.stdout.strip(),
                "dirty": bool(status_text.strip()),
                "dirty_state_digest": hashlib.sha256(status_text.encode("utf-8")).hexdigest(),
            }
        except (OSError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
