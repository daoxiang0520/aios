from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import PermissionConfig
from .plugins import PluginManager
from .sandbox import DockerSandboxBroker
from .security import SecurityKernel
from .types import Action, ActionResult

Tool = Callable[..., Any]

CORE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "read", "description": "Read a file or list a directory in the task workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "write", "description": "Create or replace a UTF-8 file in the task workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "edit", "description": "Replace exact text in an existing UTF-8 file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["path", "old_text", "new_text"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "bash", "description": "Run a command inside the configured strong sandbox. Never runs on the host.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout_seconds": {"type": "integer"}}, "required": ["command"], "additionalProperties": False}}},
]


class ToolRegistry:
    def __init__(self, permissions: PermissionConfig, plugins: PluginManager | None = None, sandbox: DockerSandboxBroker | None = None):
        self.permissions = permissions
        self.sandbox = sandbox
        self._tools: dict[str, Tool] = {}
        self._schemas = {item["function"]["name"]: item for item in CORE_TOOL_SCHEMAS}
        for name, tool in (("read", self.read), ("write", self.write), ("edit", self.edit), ("bash", self.bash), ("echo", self.echo), ("list_files", self.list_files), ("read_file", self.read_file), ("write_file", self.write_file), ("append_file", self.append_file)):
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
        return [self._schemas[name] for name in ("read", "write", "edit", "bash") if name in self.permissions.allowed_tools]

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    @staticmethod
    def echo(message: str = "") -> dict[str, str]:
        return {"message": message}

    def read(self, path: str, offset: int = 0, limit: int | None = None) -> Any:
        target = Path(path)
        if target.is_dir():
            return [{"name": child.name, "is_dir": child.is_dir(), "size": child.stat().st_size if child.is_file() else None} for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))]
        if target.stat().st_size > self.permissions.max_read_bytes:
            raise ValueError(f"File exceeds {self.permissions.max_read_bytes} bytes")
        text = target.read_text(encoding="utf-8")
        start = max(0, offset)
        return text[start : start + limit if limit is not None else None]

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

    def list_files(self, path: str = ".") -> Any:
        return self.read(path)

    def read_file(self, path: str) -> str:
        return self.read(path)

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
            return ActionResult(tool=action.tool, ok=ok, output=output, error=None if ok else f"Command exited with {output.get('exit_code')}", duration_ms=(time.perf_counter() - started) * 1000)
        except Exception as exc:
            return ActionResult(tool=action.tool, ok=False, error=f"{type(exc).__name__}: {exc}", duration_ms=(time.perf_counter() - started) * 1000)
