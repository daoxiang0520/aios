from __future__ import annotations

import hashlib
import json
import re
import shlex
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

    def describe_skill_invocation(self, command: str) -> dict[str, Any] | None:
        parsed = _parse_skill_command(command)
        if parsed is None or parsed["operation"] != "run" or self.skills_root is None:
            return None
        payload = parsed.get("input", {})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        input_metadata = {
            "input_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "input_keys": sorted(str(key) for key in payload),
        }
        package = self.skills_root / "active" / parsed["name"]
        manifest_path = package / "manifest.json"
        if not manifest_path.is_file():
            return {
                **parsed,
                "version": "unknown",
                "required_capabilities": [],
                "manifest_found": False,
                **input_metadata,
            }
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return {
            **parsed,
            "version": str(manifest.get("version", "unknown")),
            "required_capabilities": [str(item) for item in manifest.get("required_capabilities", [])],
            **input_metadata,
            "manifest_found": True,
        }

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
        _parse_skill_command(command)
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


def _is_json_object(value: str) -> bool:
    try:
        return isinstance(json.loads(value), dict)
    except (TypeError, json.JSONDecodeError):
        return False


def _parse_skill_command(command: str) -> dict[str, Any] | None:
    if "/skills" not in command:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        raise SandboxPolicyError("Invalid shell quoting in Skill invocation") from exc
    operators = {token for token in tokens if token and set(token) <= set(";&|<>")}
    skill_paths = [token for token in tokens if "/skills" in token]
    if operators or not skill_paths or any(path != "/skills/skill.py" for path in skill_paths):
        raise SandboxPolicyError(
            "Skill commands cannot be combined with shell operators or direct /skills access"
        )
    if len(tokens) < 3 or tokens[0] not in {"python", "python3"} or tokens[1] != "/skills/skill.py":
        raise SandboxPolicyError("Skills must be invoked through python /skills/skill.py")
    operation = tokens[2]
    name = tokens[3] if len(tokens) >= 4 else None
    payload: dict[str, Any] = {}
    valid = operation == "list" and len(tokens) == 3
    if operation == "show" and len(tokens) == 4 and isinstance(name, str):
        valid = re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name) is not None
    if operation == "run" and isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
        if len(tokens) == 4:
            valid = True
        elif len(tokens) == 6 and tokens[4] == "--input-json" and _is_json_object(tokens[5]):
            payload = json.loads(tokens[5])
            valid = True
    if not valid:
        raise SandboxPolicyError("Invalid or unsafe Skill dispatcher invocation")
    return {"operation": operation, "name": name, "input": payload}
