from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any

from ..capabilities import CapabilityRegistry, EvidenceContract
from ..components import ComponentRegistry, build_component_registry
from ..config import Settings
from ..skills import SkillManager
from ..storage import StateStore
from ..types import TaskStatus
from .models import CapsuleFidelity, CapsuleStatus
from .snapshot import ContentAddressedSnapshotStore


class CapsuleError(RuntimeError):
    pass


class CapsuleManager:
    """Capture and restore task execution state through immutable references."""

    def __init__(
        self, settings: Settings, store: StateStore, skills: SkillManager,
        capabilities: CapabilityRegistry, root: Path | None = None,
        components: ComponentRegistry | None = None,
    ):
        self.settings = settings
        self.store = store
        self.skills = skills
        self.capabilities = capabilities
        self.components = components or build_component_registry(
            capabilities, store=store, skill_manifests=skills.component_manifests()
        )
        self.root = (root or settings.experiments_root).resolve()
        self.snapshots = ContentAddressedSnapshotStore(self.root / "capsule_objects")
        self.worlds = self.root / "worlds"
        self.worlds.mkdir(parents=True, exist_ok=True)

    def capture(self, task_id: int) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        workspace = self.snapshots.capture(self.settings.workspace)
        active_skills = self.snapshots.capture(self.skills.active)
        contract = EvidenceContract.from_request(task.request)
        capture_phase = "pre_task" if task.status == TaskStatus.QUEUED and task.attempts == 0 else "post_hoc_current"
        image_digest = self._sandbox_image_digest()
        fidelity = (
            CapsuleFidelity.FULL
            if capture_phase == "pre_task" and image_digest is not None
            else CapsuleFidelity.PARTIAL
        )
        workspace["git"] = self._git_metadata(self.settings.workspace)
        manifest: dict[str, Any] = {
            "kind": "execution_capsule", "capsule_version": "0.6.7",
            "capsule_id": f"cap_{uuid.uuid4().hex}", "source_task_id": task_id,
            "capture_phase": capture_phase, "status": CapsuleStatus.CAPTURED.value,
            "fidelity": fidelity.value,
            "task": {
                "title": task.title, "request": task.request, "priority": task.priority,
                "max_attempts": task.max_attempts,
                "evidence_contract_hash": self._hash(contract.as_dict()),
            },
            "workspace": workspace,
            "skills": {
                "snapshot": active_skills,
                "active_set_hash": active_skills["manifest_hash"],
                "versions": {item.name: item.version for item in self.skills.active_skills()},
            },
            "capabilities": self.capabilities.as_dict(),
            "components": self.components.snapshot(),
            "harness": {
                "version": self.store.active_harness().get("version"),
                "config_hash": self._hash(self.store.active_harness().get("settings", {})),
                "config": self.store.active_harness().get("settings", {}),
            },
            "model": {
                "provider": self.settings.model.provider, "base_url": self.settings.model.base_url,
                "model": self.settings.model.model, "temperature": self.settings.model.temperature,
                "max_tokens": self.settings.model.max_tokens, "protocol": self.settings.model.protocol,
            },
            "environment": {
                "python": platform.python_version(), "platform": platform.platform(),
                "sandbox_backend": self.settings.sandbox.backend,
                "sandbox_image": self.settings.sandbox.image,
                "sandbox_image_digest": image_digest,
                "network_enabled": self.settings.capabilities.network_enabled,
            },
            "external_dependency_fingerprint": self._hash({
                "model_provider": self.settings.model.provider,
                "model": self.settings.model.model,
                "network_enabled": self.settings.capabilities.network_enabled,
            }),
        }
        manifest["initial_state_hash"] = self._hash({
            "task": manifest["task"],
            "workspace_hash": manifest["workspace"]["manifest_hash"],
            "active_skill_hash": manifest["skills"]["active_set_hash"],
            "component_set_hash": manifest["components"]["active_set_hash"],
            "capabilities": manifest["capabilities"],
            "harness": manifest["harness"],
            "model": manifest["model"],
            "environment": manifest["environment"],
        })
        manifest["integrity_hash"] = self._hash({key: value for key, value in manifest.items() if key != "integrity_hash"})
        check = self._verify_manifest(manifest)
        manifest["status"] = CapsuleStatus.REPLAYABLE.value if check["valid"] else CapsuleStatus.INVALIDATED.value
        self.store.add_task_capsule(manifest)
        self.store.register_capsule_snapshot(workspace)
        self.store.register_capsule_snapshot(active_skills)
        return manifest

    def show(self, capsule_id: str) -> dict[str, Any]:
        capsule = self.store.get_task_capsule(capsule_id)
        if capsule is None:
            raise KeyError(f"Unknown capsule: {capsule_id}")
        return capsule

    def verify_integrity(self, capsule_id: str) -> dict[str, Any]:
        capsule = self.show(capsule_id)
        check = self._verify_manifest(capsule)
        current = capsule.get("status")
        if current in {CapsuleStatus.ARCHIVED.value, CapsuleStatus.EXPIRED.value} and check["valid"]:
            status = current
        else:
            status = CapsuleStatus.REPLAYABLE.value if check["valid"] else CapsuleStatus.INVALIDATED.value
        self.store.update_task_capsule_status(capsule_id, status)
        return {"capsule_id": capsule_id, "status": status, **check}

    def fork(self, capsule_id: str, world_id: str | None = None) -> dict[str, Any]:
        check = self.verify_integrity(capsule_id)
        if not check["valid"] or check["status"] != CapsuleStatus.REPLAYABLE.value:
            raise CapsuleError(f"Capsule is not replayable: {check.get('error', 'integrity failure')}")
        capsule = self.show(capsule_id)
        identifier = world_id or f"world_{uuid.uuid4().hex}"
        if not identifier.replace("_", "").isalnum():
            raise CapsuleError("World id contains unsupported characters")
        world = (self.worlds / identifier).resolve()
        if self.worlds not in world.parents:
            raise CapsuleError("World path escapes experiment root")
        if world.exists():
            raise FileExistsError(f"Experiment world already exists: {identifier}")
        workspace = world / "workspace"
        skill_root = world / "skills"
        self.snapshots.restore(capsule["workspace"]["manifest_hash"], workspace)
        self.snapshots.restore(capsule["skills"]["snapshot"]["manifest_hash"], skill_root / "active")
        workspace_hash = self.snapshots.tree_hash(workspace)
        skill_hash = self.snapshots.tree_hash(skill_root / "active")
        if workspace_hash != capsule["workspace"]["manifest_hash"]:
            raise CapsuleError("Restored workspace does not match capsule")
        if skill_hash != capsule["skills"]["active_set_hash"]:
            raise CapsuleError("Restored active Skill set does not match capsule")
        restored_material = {
            "task": capsule["task"], "workspace_hash": workspace_hash,
            "active_skill_hash": skill_hash, "capabilities": capsule["capabilities"],
            "harness": capsule["harness"], "model": capsule["model"],
            "environment": capsule["environment"],
        }
        if "components" in capsule:
            restored_material["component_set_hash"] = capsule["components"]["active_set_hash"]
        restored_state_hash = self._hash(restored_material)
        if restored_state_hash != capsule["initial_state_hash"]:
            raise CapsuleError("Restored execution state does not match capsule")
        return {
            "world_id": identifier, "root": str(world), "workspace": str(workspace),
            "skills_root": str(skill_root), "workspace_hash": workspace_hash,
            "active_skill_hash": skill_hash, "initial_state_hash": restored_state_hash,
        }

    def delete_world(self, world: dict[str, Any]) -> None:
        root = Path(str(world["root"])).resolve()
        if self.worlds not in root.parents or not root.name.startswith("world_"):
            raise CapsuleError("Refusing to delete a path outside managed experiment worlds")
        if root.exists():
            self._remove_world_tree(root)

    @staticmethod
    def _remove_world_tree(root: Path) -> None:
        """Delete a managed replay world containing Windows read-only snapshot files."""
        def make_writable_and_retry(function: Any, target: str, exc_info: Any) -> None:
            error = exc_info[1]
            if not isinstance(error, PermissionError):
                raise error
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            function(target)

        shutil.rmtree(root, onerror=make_writable_and_retry)

    def archive(self, capsule_id: str) -> dict[str, Any]:
        self.show(capsule_id)
        self.store.update_task_capsule_status(capsule_id, CapsuleStatus.ARCHIVED.value)
        return {"capsule_id": capsule_id, "status": CapsuleStatus.ARCHIVED.value}

    def delete(self, capsule_id: str) -> dict[str, Any]:
        """Expire a capsule reference; shared content objects remain deduplicated."""
        self.show(capsule_id)
        self.store.update_task_capsule_status(capsule_id, CapsuleStatus.EXPIRED.value)
        return {"capsule_id": capsule_id, "status": CapsuleStatus.EXPIRED.value}

    def _verify_manifest(self, capsule: dict[str, Any]) -> dict[str, Any]:
        expected = capsule.get("integrity_hash")
        # Status is lifecycle metadata and is deliberately outside immutable material.
        original_material = {key: value for key, value in capsule.items() if key not in {"integrity_hash", "created_at"}}
        original_material["status"] = CapsuleStatus.CAPTURED.value
        manifest_valid = expected == self._hash(original_material)
        workspace = self.snapshots.verify(capsule.get("workspace", {}).get("manifest_hash", ""))
        skills = self.snapshots.verify(capsule.get("skills", {}).get("snapshot", {}).get("manifest_hash", ""))
        valid = manifest_valid and workspace["valid"] and skills["valid"]
        errors = [item.get("error") for item in (workspace, skills) if not item["valid"]]
        if not manifest_valid:
            errors.insert(0, "Capsule manifest hash mismatch")
        return {
            "valid": valid, "manifest_valid": manifest_valid,
            "workspace": workspace, "skills": skills,
            "error": "; ".join(str(item) for item in errors if item) or None,
        }

    @staticmethod
    def _hash(value: Any) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _sandbox_image_digest(self) -> str | None:
        image = self.settings.sandbox.image
        if "@sha256:" in image:
            return image.rsplit("@", 1)[1]
        if image.startswith("sha256:"):
            return image
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", image],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                check=False,
            )
            value = result.stdout.strip()
            return value if result.returncode == 0 and value.startswith("sha256:") else None
        except (OSError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _git_metadata(workspace: Path) -> dict[str, Any] | None:
        if not (workspace / ".git").exists():
            return None
        try:
            def git(*arguments: str) -> str:
                result = subprocess.run(
                    ["git", "-C", str(workspace), *arguments], capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=10, check=False,
                )
                return result.stdout.strip() if result.returncode == 0 else ""

            diff = git("diff", "--binary", "HEAD")
            return {
                "commit": git("rev-parse", "HEAD") or None,
                "dirty_diff_hash": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                "untracked": [line for line in git("ls-files", "--others", "--exclude-standard").splitlines() if line],
            }
        except (OSError, subprocess.SubprocessError):
            return None
