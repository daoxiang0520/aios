from __future__ import annotations

from pathlib import Path
from typing import Any

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

    def __init__(self, workspace: Path, config: PermissionConfig):
        self.workspace = workspace.resolve()
        self.config = config

    def authorize(self, action: Action) -> dict[str, Any]:
        if action.tool not in self.config.allowed_tools:
            raise PermissionDenied(f"Tool is not allowed: {action.tool}")
        if action.tool in {"write", "edit", "write_file", "append_file"} and not self.config.allow_writes:
            raise PermissionDenied("Workspace writes are disabled")

        arguments = dict(action.arguments)
        path_key = self.PATH_ARGUMENTS.get(action.tool)
        if path_key:
            arguments[path_key] = str(self.resolve_workspace_path(arguments.get(path_key, ".")))

        if action.tool in {"write", "edit", "write_file", "append_file"}:
            size = len(str(arguments.get("content", "")).encode("utf-8"))
            if size > self.config.max_write_bytes:
                raise PermissionDenied(f"Write exceeds {self.config.max_write_bytes} bytes")
        return arguments

    def resolve_workspace_path(self, value: str) -> Path:
        candidate = Path(value)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.workspace / candidate).resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise PermissionDenied("Path escapes the configured workspace") from exc
        return resolved
