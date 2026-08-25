from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .storage import StateStore


class PluginValidationError(RuntimeError):
    pass


@dataclass(slots=True)
class ToolPlugin:
    name: str
    description: str
    parameters: dict[str, Any]
    kind: str
    config: dict[str, Any]
    permissions: dict[str, Any]
    version: str = "0.1.0"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ToolPlugin":
        return cls(
            name=str(value.get("name", "")),
            description=str(value.get("description", "")),
            parameters=dict(value.get("parameters") or {}),
            kind=str(value.get("kind", "")),
            config=dict(value.get("config") or {}),
            permissions=dict(value.get("permissions") or {}),
            version=str(value.get("version", "0.1.0")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "parameters": self.parameters,
            "kind": self.kind,
            "config": self.config,
            "permissions": self.permissions,
        }

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class PluginManager:
    """Loads generated declarative tools without importing generated Python code."""

    ALLOWED_KINDS = {"state_query", "workspace_search"}
    ALLOWED_STATE_SOURCES = {"tasks", "traces", "dead_letters"}

    def __init__(self, root: Path, store: StateStore, workspace: Path):
        self.root = root
        self.store = store
        self.workspace = workspace.resolve()
        self.candidates = root / "candidates"
        self.active = root / "active"
        self.quarantine = root / "quarantine"
        for directory in (self.candidates, self.active, self.quarantine):
            directory.mkdir(parents=True, exist_ok=True)

    def write_candidate(self, candidate_id: int, manifest: dict[str, Any]) -> Path:
        plugin = self.validate(manifest)
        directory = self.candidates / f"candidate_{candidate_id}" / plugin.name
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "manifest.json"
        target.write_text(json.dumps(plugin.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def activate(self, candidate_id: int, name: str) -> Path:
        source = self.candidates / f"candidate_{candidate_id}" / name / "manifest.json"
        if not source.is_file():
            raise FileNotFoundError(f"Candidate manifest not found: {source}")
        plugin = self.validate(json.loads(source.read_text(encoding="utf-8")))
        destination = self.active / plugin.name
        backup = self.quarantine / f"{plugin.name}.previous"
        if destination.exists():
            if backup.exists():
                shutil.rmtree(backup)
            shutil.copytree(destination, backup)
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination / "manifest.json")
        return destination

    def rollback(self, name: str) -> bool:
        destination = self.active / name
        backup = self.quarantine / f"{name}.previous"
        if destination.exists():
            shutil.rmtree(destination)
        if backup.exists():
            shutil.copytree(backup, destination)
            shutil.rmtree(backup)
            return True
        return False

    def active_plugins(self) -> list[ToolPlugin]:
        plugins: list[ToolPlugin] = []
        for manifest_path in sorted(self.active.glob("*/manifest.json")):
            try:
                plugins.append(self.validate(json.loads(manifest_path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, PluginValidationError):
                continue
        return plugins

    def validate(self, manifest: dict[str, Any]) -> ToolPlugin:
        plugin = ToolPlugin.from_dict(manifest)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", plugin.name):
            raise PluginValidationError("Invalid tool name")
        if plugin.kind not in self.ALLOWED_KINDS:
            raise PluginValidationError(f"Unsupported generated tool kind: {plugin.kind}")
        if not plugin.description or len(plugin.description) > 500:
            raise PluginValidationError("Tool description is required and limited to 500 characters")
        if plugin.parameters.get("type") != "object":
            raise PluginValidationError("Tool parameters must be a JSON object schema")
        permissions = plugin.permissions
        if permissions.get("network") or permissions.get("subprocess"):
            raise PluginValidationError("Generated tools cannot request network or subprocess access")
        if permissions.get("filesystem") not in {None, "none", "workspace_read"}:
            raise PluginValidationError("Generated tools may only request workspace_read")
        if permissions.get("database") not in {None, "none", "read_only"}:
            raise PluginValidationError("Generated tools may only request read-only database access")
        if plugin.kind == "state_query":
            source = plugin.config.get("source")
            if source not in self.ALLOWED_STATE_SOURCES:
                raise PluginValidationError("Unsupported state query source")
            if permissions.get("database") != "read_only":
                raise PluginValidationError("State query tools require read-only database access")
        if plugin.kind == "workspace_search" and permissions.get("filesystem") != "workspace_read":
            raise PluginValidationError("Workspace search requires workspace_read")
        return plugin

    def bind(self, plugin: ToolPlugin) -> Callable[..., Any]:
        if plugin.kind == "state_query":
            return self._bind_state_query(plugin)
        if plugin.kind == "workspace_search":
            return self._workspace_search
        raise PluginValidationError(f"Unsupported generated tool kind: {plugin.kind}")

    def smoke_test(self, manifest: dict[str, Any]) -> dict[str, Any]:
        plugin = self.validate(manifest)
        function = self.bind(plugin)
        if plugin.kind == "state_query":
            output = function(limit=1)
        else:
            output = function(query="", limit=1)
        return {"passed": isinstance(output, list), "tool": plugin.name, "sample_count": len(output)}

    def _bind_state_query(self, plugin: ToolPlugin) -> Callable[..., Any]:
        source = str(plugin.config["source"])

        def query(limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
            safe_limit = max(1, min(int(limit), 100))
            if source == "traces":
                return self.store.recent_traces(safe_limit)
            if source == "dead_letters":
                return self.store.list_dead_letters(safe_limit)
            tasks = self.store.list_tasks(safe_limit)
            return [
                {
                    "id": task.id,
                    "title": task.title,
                    "request": task.request,
                    "status": task.status.value,
                    "attempts": task.attempts,
                    "error": task.error,
                    "result": task.result,
                    "updated_at": task.updated_at,
                }
                for task in tasks
                if status is None or task.status.value == status
            ]

        return query

    def _workspace_search(self, query: str = "", limit: int = 20, content: bool = False) -> list[dict[str, Any]]:
        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        for path in sorted(self.workspace.rglob("*")):
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(self.workspace)
            except (OSError, ValueError):
                continue
            relative = resolved.relative_to(self.workspace).as_posix()
            matched = needle in relative.casefold()
            snippet = None
            if content and not matched and path.stat().st_size <= 256_000:
                try:
                    text = resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    text = ""
                index = text.casefold().find(needle)
                matched = index >= 0
                if matched:
                    snippet = text[max(0, index - 80) : index + len(query) + 160]
            if matched:
                matches.append({"path": relative, "size": path.stat().st_size, "snippet": snippet})
                if len(matches) >= max(1, min(int(limit), 100)):
                    break
        return matches


def state_query_manifest(name: str, source: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "version": "0.1.0",
        "description": description,
        "kind": "state_query",
        "config": {"source": source},
        "permissions": {
            "database": "read_only",
            "filesystem": "none",
            "network": False,
            "subprocess": False,
        },
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "status": {"type": "string"},
            },
            "additionalProperties": False,
        },
    }


def workspace_search_manifest() -> dict[str, Any]:
    return {
        "name": "search_files",
        "version": "0.1.0",
        "description": "Search workspace files by path and optionally UTF-8 text content.",
        "kind": "workspace_search",
        "config": {},
        "permissions": {
            "database": "none",
            "filesystem": "workspace_read",
            "network": False,
            "subprocess": False,
        },
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "content": {"type": "boolean"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }
