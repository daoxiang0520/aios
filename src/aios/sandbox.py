from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import SandboxConfig


class SandboxUnavailable(RuntimeError):
    pass


class SandboxPolicyError(RuntimeError):
    pass


@dataclass(slots=True)
class SandboxSession:
    task_id: int
    path: Path
    state_path: Path


class DockerSandboxBroker:
    """Transactional workspace plus Docker-only command execution.

    There is deliberately no host-shell fallback. A missing Docker daemon is a
    capability failure, not permission to execute model-generated commands on
    the host.
    """

    def __init__(self, root: Path, config: SandboxConfig, skills_root: Path | None = None):
        self.root = root.resolve()
        self.config = config
        self.skills_root = skills_root.resolve() if skills_root is not None else None
        self.session: SandboxSession | None = None

    def available(self) -> bool:
        if self.config.backend != "docker" or shutil.which("docker") is None:
            return False
        try:
            result = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def prepare(self, task_id: int, workspace: Path) -> Path:
        session_root = (self.root / f"task_{task_id}").resolve()
        if session_root.parent != self.root:
            raise ValueError("Invalid sandbox session path")
        if session_root.exists():
            shutil.rmtree(session_root)
        session_root.mkdir(parents=True)
        snapshot = session_root / "workspace"
        state_path = session_root / "state"
        shutil.copytree(workspace, snapshot, dirs_exist_ok=True)
        state_path.mkdir()
        self.session = SandboxSession(task_id, snapshot, state_path)
        return snapshot

    def run(self, command: str, timeout_seconds: int | None = None) -> dict[str, Any]:
        if self.session is None:
            raise SandboxUnavailable("No active sandbox session")
        self._validate_command_scope(command)
        if not self.available():
            raise SandboxUnavailable("Docker sandbox is unavailable; host execution is forbidden")
        before = self._manifest(self.session.path)
        timeout = min(timeout_seconds or self.config.timeout_seconds, self.config.timeout_seconds)
        args = [
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", f"{self.config.memory_mb}m", "--cpus", str(self.config.cpus),
            "--pids-limit", str(self.config.pids_limit),
            "--mount", f"type=bind,src={self.session.path},dst=/workspace",
            "--mount", f"type=bind,src={self.session.state_path},dst=/aios-state,readonly",
        ]
        if self.skills_root is not None and self.skills_root.exists():
            args.extend(["--mount", f"type=bind,src={self.skills_root},dst=/skills,readonly"])
        args.extend(["--workdir", "/workspace", self.config.image, "sh", "-lc", command])
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Sandbox command exceeded {timeout}s") from exc
        after = self._manifest(self.session.path)
        changes = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "changes": changes,
        }

    def run_candidate(self, package: Path, command: str, timeout_seconds: int) -> dict[str, Any]:
        self._validate_command_scope(command)
        if not self.available():
            raise SandboxUnavailable("Docker sandbox is unavailable; candidate code cannot run on host")
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="skill_benchmark_", dir=self.root) as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir()
            state.mkdir()
            (state / "state.json").write_text(
                json.dumps({"tasks": [], "traces": [], "dead-letters": [], "memory": [], "capabilities": {}}, ensure_ascii=False),
                encoding="utf-8",
            )
            timeout = min(timeout_seconds, self.config.timeout_seconds)
            args = [
                "docker", "run", "--rm", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--memory", f"{self.config.memory_mb}m", "--cpus", str(self.config.cpus),
                "--pids-limit", str(self.config.pids_limit),
                "--mount", f"type=bind,src={workspace},dst=/workspace",
                "--mount", f"type=bind,src={state},dst=/aios-state,readonly",
                "--mount", f"type=bind,src={package.resolve()},dst=/candidate,readonly",
                "--workdir", "/workspace", self.config.image, "sh", "-lc", command,
            ]
            try:
                result = subprocess.run(
                    args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=timeout, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(f"Skill benchmark exceeded {timeout}s") from exc
            return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}

    @staticmethod
    def _validate_command_scope(command: str) -> None:
        if "/skills/" in command and "/skills/skill.py" not in command:
            raise SandboxPolicyError("Skills must be invoked through python /skills/skill.py run")
        broad_root_patterns = (
            r"(?:^|[;&|]\s*)cd\s+/(?:\s|[;&|]|$)",
            r"\bfind\s+/(?:\s|$)",
            r"\bls\s+(?:-[A-Za-z]+\s+)?/(?:\s|[;&|]|$)",
            r"\b(?:grep|rg|du)\b[^;&|]*\s/(?:\s|[;&|]|$)",
        )
        if any(re.search(pattern, command) for pattern in broad_root_patterns):
            raise SandboxPolicyError(
                "Broad container-root access is forbidden; inspect /workspace or /aios-state only"
            )

    def expose_read_only_state(self, state: dict[str, Any]) -> None:
        if self.session is None:
            raise SandboxUnavailable("No active sandbox session")
        internal = self.session.state_path
        (internal / "state.json").write_text(json.dumps(state, ensure_ascii=False, default=str), encoding="utf-8")
        (internal / "aiosctl.py").write_text(
            "import json,sys\n"
            "data=json.load(open('/aios-state/state.json',encoding='utf-8'))\n"
            "name=sys.argv[1] if len(sys.argv)>1 else 'capabilities'\n"
            "print(json.dumps(data.get(name,[]),ensure_ascii=False,indent=2))\n",
            encoding="utf-8",
        )

    def commit(self, workspace: Path) -> list[str]:
        if self.session is None:
            return []
        destination = workspace.resolve()
        changed: list[str] = []
        current = self._manifest(destination)
        snapshot = self._manifest(self.session.path)
        for relative, digest in snapshot.items():
            if relative == ".aios" or relative.startswith(".aios/"):
                continue
            if current.get(relative) == digest:
                continue
            source = self.session.path / relative
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            changed.append(relative)
        self.discard()
        return sorted(changed)

    def discard(self) -> None:
        if self.session is None:
            return
        session_root = self.session.path.parent
        self.session = None
        if session_root.parent == self.root and session_root.exists():
            shutil.rmtree(session_root)

    @staticmethod
    def _manifest(root: Path) -> dict[str, str]:
        if not root.exists():
            return {}
        manifest: dict[str, str] = {}
        for path in root.rglob("*"):
            if path.is_file():
                manifest[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return manifest
