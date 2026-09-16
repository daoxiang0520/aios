from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aios.config import SandboxConfig
from aios.runtime_evolution import (
    ModelRuntimeMutationReasoner,
    RuntimeAttributionContract,
    RuntimeCandidateManager,
    RuntimeDiagnosisBenchmark,
)
from aios.sandbox import DockerSandboxBroker


class SandboxReadonlyCleanupGate(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "source.txt").write_text("production", encoding="utf-8")
        self.broker = DockerSandboxBroker(self.root / "sandbox", SandboxConfig())
        self.broker.available = lambda **_: True  # type: ignore[method-assign]

    def tearDown(self) -> None:
        for path in self.root.rglob("*"):
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
        self.temporary.cleanup()

    @staticmethod
    def _readonly(path: Path) -> None:
        path.chmod(stat.S_IREAD)

    def test_normal_file_cleanup(self) -> None:
        snapshot = self.broker.prepare(1, self.workspace)
        (snapshot / "normal.txt").write_text("x", encoding="utf-8")
        root = snapshot.parent
        self.broker.discard()
        self.assertFalse(root.exists())

    def test_readonly_file_cleanup(self) -> None:
        snapshot = self.broker.prepare(1, self.workspace)
        target = snapshot / "readonly.txt"
        target.write_text("x", encoding="utf-8")
        self._readonly(target)
        root = snapshot.parent
        self.broker.discard()
        self.assertFalse(root.exists())

    def test_nested_readonly_git_object_cleanup(self) -> None:
        snapshot = self.broker.prepare(1, self.workspace)
        target = snapshot / "repo" / ".git" / "objects" / "04" / "deadbeef"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"object")
        self._readonly(target)
        root = snapshot.parent
        self.broker.discard()
        self.assertFalse(root.exists())

    def test_discard_is_idempotent(self) -> None:
        self.broker.prepare(1, self.workspace)
        self.broker.discard()
        self.broker.discard()

    def test_prepare_recovers_existing_failed_task_tree(self) -> None:
        first = self.broker.prepare(1, self.workspace)
        stale = first / "repo" / ".git" / "objects" / "aa" / "stale"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"stale")
        self._readonly(stale)
        second = self.broker.prepare(1, self.workspace)
        self.assertTrue((second / "source.txt").is_file())
        self.assertFalse(stale.exists())

    def test_cleanup_retries_transient_directory_not_empty(self) -> None:
        target = self.root / "sandbox" / "task_9"
        target.mkdir(parents=True)
        (target / "result.txt").write_text("x", encoding="utf-8")
        real_rmtree = __import__("shutil").rmtree
        calls = 0

        def transient_then_remove(path, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                error = OSError(errno.ENOTEMPTY, "directory not empty")
                error.winerror = 145
                raise error
            return real_rmtree(path, *args, **kwargs)

        with patch("aios.sandbox.shutil.rmtree", side_effect=transient_then_remove), \
             patch("aios.sandbox.time.sleep"):
            self.broker._remove_tree(target)

        self.assertEqual(calls, 2)
        self.assertFalse(target.exists())

    def test_discard_preserves_production_workspace_isolation(self) -> None:
        snapshot = self.broker.prepare(1, self.workspace)
        (snapshot / "source.txt").write_text("candidate", encoding="utf-8")
        (snapshot / "new.txt").write_text("candidate", encoding="utf-8")
        self.broker.discard()
        self.assertEqual((self.workspace / "source.txt").read_text(encoding="utf-8"), "production")
        self.assertFalse((self.workspace / "new.txt").exists())

    def test_git_metadata_is_not_a_publishable_workspace_change(self) -> None:
        snapshot = self.broker.prepare(1, self.workspace)
        git_object = snapshot / "repo" / ".git" / "objects" / "bb" / "new"
        git_object.parent.mkdir(parents=True)
        git_object.write_bytes(b"new")
        (snapshot / "result.txt").write_text("result", encoding="utf-8")
        changed = self.broker.commit(self.workspace)
        self.assertEqual(changed, ["result.txt"])
        self.assertFalse((self.workspace / "repo" / ".git").exists())


class RuntimeMeasurementGate(unittest.TestCase):
    @staticmethod
    def _cross_stage_proposal(revisions=None) -> dict:
        stage1 = {
            "decision": "INVESTIGATE",
            "runtime_defect_supported": True,
            "selected_hypothesis": "H1",
            "hypotheses": [{
                "id": "H1", "status": "supported", "runtime_defect": True,
                "claim": "Runtime defect",
            }],
        }
        return {
            "decision": "NO_ACTION",
            "runtime_defect_supported": False,
            "non_mutation_reason": "insufficient_evidence",
            "model_attribution": stage1,
            "model_intended_disposition": {
                "action": "NO_ACTION",
                "runtime_defect_supported": False,
                "hypothesis_revisions": revisions or [],
            },
            "attribution": {
                **stage1,
                "hypotheses": [{
                    "id": "H1", "status": "rejected", "runtime_defect": True,
                    "claim": "Runtime defect",
                }],
            },
            "final_disposition": {"action": "NO_ACTION", "supported_by": []},
        }

    def test_unexplained_cross_stage_defect_flip_is_inconsistent(self) -> None:
        assessment = RuntimeAttributionContract.assess(self._cross_stage_proposal())
        self.assertFalse(assessment["valid"])
        self.assertTrue(assessment["cross_stage"]["changed"])
        self.assertFalse(assessment["cross_stage"]["revision_explains_change"])

    def test_explicit_selected_hypothesis_revision_explains_flip(self) -> None:
        proposal = self._cross_stage_proposal([{
            "id": "H1", "status": "rejected", "reason": "source contradicted H1",
        }])
        assessment = RuntimeAttributionContract.assess(proposal)
        self.assertTrue(assessment["valid"], assessment["errors"])
        self.assertTrue(assessment["cross_stage"]["revision_explains_change"])

    def test_host_disposition_does_not_overwrite_model_records(self) -> None:
        proposal = self._cross_stage_proposal()
        original_attribution = json.loads(json.dumps(proposal["model_attribution"]))
        original_intent = json.loads(json.dumps(proposal["model_intended_disposition"]))
        result = ModelRuntimeMutationReasoner._enforce_consistency(proposal)
        self.assertEqual(result["model_attribution"], original_attribution)
        self.assertEqual(result["model_intended_disposition"], original_intent)
        self.assertEqual(result["effective_host_disposition"]["action"], "NO_ACTION")
        self.assertEqual(result["reason"], "attribution_consistency_failed")

    def test_observational_validation_flags_unobserved_agent_read(self) -> None:
        facts = {
            "executions": [{
                "tool": "bash", "arguments": {"command": "git clone https://example.test/repo"},
            }],
        }
        proposal = {
            "reason": "The Agent attempted to read `.git/objects/04/deadbeef`.",
            "attribution": {"hypotheses": []},
        }
        result = RuntimeCandidateManager._observational_validation(facts, proposal)
        self.assertTrue(result["unsupported_action_claim"])
        self.assertEqual(result["unsupported_claims"][0]["target"], ".git/objects/04/deadbeef")

    def test_observational_validation_accepts_recorded_agent_action(self) -> None:
        facts = {
            "executions": [{
                "tool": "read_file", "arguments": {"path": ".git/objects/04/deadbeef"},
            }],
        }
        proposal = {
            "reason": "The Agent attempted to read `.git/objects/04/deadbeef`.",
            "attribution": {"hypotheses": []},
        }
        result = RuntimeCandidateManager._observational_validation(facts, proposal)
        self.assertFalse(result["unsupported_action_claim"])

    def test_task84_frozen_case_scores_source_support_and_root_of_trust_abstention(self) -> None:
        proposal = {
            "decision": "NO_ACTION",
            "causal_layer": "sandbox_lifecycle",
            "runtime_defect_supported": True,
            "non_mutation_reason": "root_of_trust",
            "reason": "Windows readonly .git object caused shutil.rmtree cleanup PermissionError",
            "attribution": {
                "reason": "sandbox discard cleanup failed on a readonly file",
                "inspect_files": ["src/aios/sandbox.py"],
                "hypotheses": [{"claim": "Sandbox cleanup cannot remove readonly files"}],
            },
            "proposed_files": ["src/aios/sandbox.py"],
            "source_delivery": {"admitted_files": []},
            "final_disposition": {"action": "NO_ACTION", "supported_by": []},
        }
        result = RuntimeDiagnosisBenchmark.score(84, proposal)
        self.assertTrue(result["final_disposition"]["correct"])
        self.assertTrue(result["causal_attribution"]["correct"])
        self.assertTrue(result["source_support"]["relevant_source_selected"])
        self.assertFalse(result["source_support"]["relevant_source_delivered"])
        self.assertEqual(result["source_support"]["state"], "insufficient")


if __name__ == "__main__":
    unittest.main()
