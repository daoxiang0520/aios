from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import PermissionConfig
from .plugins import PluginManager
from .resources import ResourceAdapter
from .sandbox import DockerSandboxBroker
from .security import SecurityKernel
from .types import Action, ActionResult

Tool = Callable[..., Any]

CORE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "read", "description": "Read a resource through the Resource Adapter. Supports workspace paths plus governed http/https URLs when resource.http.read is available, structured directory listings, UTF-8 text/code, CSV previews, ZIP listings, PDF text, and XLSX sheet previews. Use a relative path, /workspace/..., or a complete http/https URL. offset/limit select text characters, CSV/XLSX rows, or PDF text characters depending on representation.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "write", "description": "Create or replace a UTF-8 file in the task workspace. Use a relative path or /workspace/....", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "edit", "description": "Replace exact text in an existing UTF-8 workspace file. Use a relative path or /workspace/....", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["path", "old_text", "new_text"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "bash", "description": "Run a command inside the configured strong sandbox with /workspace as its working directory. Never runs on the host and must not scan container root.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout_seconds": {"type": "integer"}}, "required": ["command"], "additionalProperties": False}}},
]

EVOLVE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "evolve",
        "description": "Open a reversible, versioned modification transaction for your own mutable system. This starts no model, diagnosis, benchmark, or adoption process. After it returns, use read/write/edit/bash on /self; prior versions remain read-only at /self-history.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}, "base_version": {"type": "string"}},
            "additionalProperties": False,
        },
    },
}


class ToolRegistry:
    def __init__(self, permissions: PermissionConfig, plugins: PluginManager | None = None, sandbox: DockerSandboxBroker | None = None, self_versions: Any = None):
        self.permissions = permissions
        self.sandbox = sandbox
        self.resources = ResourceAdapter(permissions, sandbox)
        self.self_versions = self_versions
        self._tools: dict[str, Tool] = {}
        self._schemas = {item["function"]["name"]: item for item in CORE_TOOL_SCHEMAS}
        if self_versions is not None:
            self._schemas["evolve"] = EVOLVE_TOOL_SCHEMA
        for name, tool in (("read", self.read), ("write", self.write), ("edit", self.edit), ("bash", self.bash), ("evolve", self.evolve), ("echo", self.echo), ("list_files", self.list_files), ("read_file", self.read_file), ("write_file", self.write_file), ("append_file", self.append_file)):
            self.register(name, tool)
        if plugins is not None:
            self.load_plugins(plugins)

    def register(self, name: str, tool: Tool) -> None:
        self._tools[name] = tool

    def load_plugins(self, manager: PluginManager) -> None:
        # v0.4 generated plugins remain executable but are frozen and model-hidden.
        for plugin in manager.active_plugins():
            self.register(plugin.name, manager.bind(plugin))

    def schemas(self) -> list[dict[str, Any]]:
        visible = ["read", "write", "edit", "bash"]
        if self.self_versions is not None:
            visible.append("evolve")
        return [self._schemas[name] for name in visible if name in self.permissions.allowed_tools]

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    @staticmethod
    def echo(message: str = "") -> dict[str, str]:
        return {"message": message}

    def read(self, path: str, offset: int = 0, limit: int | None = None) -> Any:
        return self.resources.read(path, offset, limit)

    @staticmethod
    def write(path: str, content: str) -> dict[str, Any]:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": str(target), "bytes": len(content.encode("utf-8"))}

    @staticmethod
    def edit(path: str, old_text: str, new_text: str, replace_all: bool = False) -> dict[str, Any]:
        target = Path(path)
        content = target.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count == 0:
            raise ValueError("old_text was not found")
        if count > 1 and not replace_all:
            raise ValueError("old_text is not unique; set replace_all=true")
        target.write_text(content.replace(old_text, new_text, -1 if replace_all else 1), encoding="utf-8")
        return {"path": str(target), "replacements": count if replace_all else 1}

    def bash(self, command: str, timeout_seconds: int | None = None) -> dict[str, Any]:
        if self.sandbox is None:
            raise RuntimeError("Sandbox broker is not configured")
        return self.sandbox.run(command, timeout_seconds)

    def evolve(self, reason: str | None = None, base_version: str | None = None) -> dict[str, Any]:
        if self.self_versions is None:
            raise RuntimeError("Self modification is not configured")
        return self.self_versions.open(reason=reason, base_version=base_version)

    def list_files(self, path: str = ".") -> Any:
        return self.resources.legacy_list(path)

    def read_file(self, path: str) -> str:
        return self.resources.legacy_text(path)

    @staticmethod
    def write_file(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
        target = Path(path)
        if target.exists() and not overwrite:
            raise FileExistsError("Refusing to overwrite an existing file without overwrite=true")
        return ToolRegistry.write(path, content)

    @staticmethod
    def append_file(path: str, content: str) -> dict[str, Any]:
        target = Path(path)
        if not target.exists():
            raise FileNotFoundError("append_file requires an existing file")
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)
        return {"path": str(target), "appended_bytes": len(content.encode("utf-8"))}


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, security: SecurityKernel):
        self.registry = registry
        self.security = security

    def execute(self, action: Action) -> ActionResult:
        started = time.perf_counter()
        try:
            arguments = self.security.authorize(action)
            output = self.registry.get(action.tool)(**arguments)
            ok = not (action.tool == "bash" and isinstance(output, dict) and output.get("exit_code") != 0)
            if ok:
                error = None
            elif output.get("exit_code") == 127:
                error = "MissingExecutable: shell command was not found (exit 127)"
            else:
                error = f"Command exited with {output.get('exit_code')}"
            return ActionResult(tool=action.tool, ok=ok, output=output, error=error, duration_ms=(time.perf_counter() - started) * 1000)
        except Exception as exc:
            return ActionResult(tool=action.tool, ok=False, error=f"{type(exc).__name__}: {exc}", duration_ms=(time.perf_counter() - started) * 1000)
