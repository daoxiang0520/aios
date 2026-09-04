from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import PermissionConfig
from .types import Action


class PermissionDenied(RuntimeError):
    pass


class SecurityKernel:
    PATH_ARGUMENTS = {
        "read": "path",
        "write": "path",
        "edit": "path",
        "read_file": "path",
        "write_file": "path",
        "append_file": "path",
        "list_files": "path",
    }

    def __init__(self, workspace: Path, config: PermissionConfig, self_versions: Any = None):
        self.workspace = workspace.resolve()
        self.config = config
        self.self_versions = self_versions

    def authorize(self, action: Action) -> dict[str, Any]:
        if action.tool not in self.config.allowed_tools:
            raise PermissionDenied(f"Tool is not allowed: {action.tool}")
        if action.tool in {"write", "edit", "write_file", "append_file"} and not self.config.allow_writes:
            raise PermissionDenied("Workspace writes are disabled")

        arguments = dict(action.arguments)
        path_key = self.PATH_ARGUMENTS.get(action.tool)
        if path_key:
            raw_path = str(arguments.get(path_key, "."))
            if action.tool == "read" and self._is_http_url(raw_path):
                arguments[path_key] = raw_path
            else:
                if raw_path.replace("\\", "/").startswith("/self"):
                    if self.self_versions is None:
                        raise PermissionDenied("Mutable self is unavailable")
                    try:
                        arguments[path_key] = str(self.self_versions.resolve(
                            raw_path,
                            write=action.tool in {"write", "edit", "write_file", "append_file"},
                        ))
                    except RuntimeError as exc:
                        raise PermissionDenied(str(exc)) from exc
                else:
                    arguments[path_key] = str(self.resolve_workspace_path(raw_path))

        if action.tool in {"write", "edit", "write_file", "append_file"}:
            size = len(str(arguments.get("content", "")).encode("utf-8"))
            if size > self.config.max_write_bytes:
                raise PermissionDenied(f"Write exceeds {self.config.max_write_bytes} bytes")
        return arguments

    @staticmethod
    def _is_http_url(value: str) -> bool:
        parsed = urlsplit(str(value).strip())
        return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.hostname)

    def resolve_workspace_path(self, value: str) -> Path:
        candidate = self.normalize_workspace_path(value)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.workspace / candidate).resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise PermissionDenied("Path escapes the configured workspace") from exc
        return resolved

    @staticmethod
    def normalize_workspace_path(value: str) -> Path:
        """Map the Docker-visible /workspace mount to the primitive-tool workspace.

        Primitive tools execute on the host-side transactional snapshot, while bash
        sees that same snapshot mounted at /workspace. Accepting this one virtual
        absolute prefix gives the model a single path vocabulary without widening
        authority to other container or host paths.
        """
        raw = str(value).strip()
        if raw == "/workspace" or raw == "/workspace/":
            return Path(".")
        if raw.startswith("/workspace/"):
            return Path(raw[len("/workspace/"):])
        return Path(raw)
