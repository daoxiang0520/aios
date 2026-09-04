from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SelfModificationConfig


class SelfModificationError(RuntimeError):
    pass


class SelfVersionManager:
    """Version the Agent-owned layer without exposing the immutable Host.

    A call to ``open`` is deliberately mechanical: it copies one existing
    version, advances CURRENT, and exposes the new copy to the same task.  It
    performs no diagnosis, generation, evaluation, or selection.
    """

    VERSION_PATTERN = re.compile(r"v(\d{6})$")

    def __init__(self, root: Path, config: SelfModificationConfig):
        self.root = root.resolve()
        self.config = config
        self.versions = self.root / "versions"
        self.current_file = self.root / "CURRENT"
        self.history_file = self.root / "history.jsonl"
        self._task_id: int | None = None
        self._mutable_task_id: int | None = None

    def initialize(self) -> None:
        self.versions.mkdir(parents=True, exist_ok=True)
        if not self.current_file.is_file():
            version = "v000001"
            initial = self.versions / version
            initial.mkdir()
            (initial / "tools").mkdir()
            (initial / "components").mkdir()
            (initial / "SYSTEM.md").write_text(self._initial_system_map(), encoding="utf-8")
            self._write_current(version)
            self._append_history({
                "event": "initialized",
                "version": version,
                "parent": None,
                "task_id": None,
                "reason": None,
            })
        self.current_version()  # validate the persisted pointer

    def begin_task(self, task_id: int) -> None:
        task_id = int(task_id)
        if self._task_id != task_id:
            self._task_id = task_id
            self._mutable_task_id = None

    @property
    def exposed(self) -> bool:
        return self._task_id is not None

    @property
    def writable(self) -> bool:
        return self._task_id is not None and self._mutable_task_id == self._task_id

    @property
    def current_path(self) -> Path:
        return (self.versions / self.current_version()).resolve()

    def current_version(self) -> str:
        if not self.current_file.is_file():
            raise SelfModificationError("Self version store is not initialized")
        version = self.current_file.read_text(encoding="utf-8").strip()
        if self.VERSION_PATTERN.fullmatch(version) is None:
            raise SelfModificationError("Invalid CURRENT self-version pointer")
        path = (self.versions / version).resolve()
        if path.parent != self.versions.resolve() or not path.is_dir():
            raise SelfModificationError("CURRENT self version does not exist")
        return version

    def system_prompt(self) -> str:
        path = self.current_path / "SYSTEM.md"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[
            : max(0, int(self.config.max_system_prompt_characters))
        ]

    def open(self, reason: str | None = None, base_version: str | None = None) -> dict[str, Any]:
        if not self.config.enabled:
            raise SelfModificationError("Self modification is disabled")
        if self._task_id is None:
            raise SelfModificationError("No active task can own the self-modification transaction")
        parent = base_version or self.current_version()
        if self.VERSION_PATTERN.fullmatch(parent) is None:
            raise SelfModificationError("Invalid base self version")
        source = (self.versions / parent).resolve()
        if source.parent != self.versions.resolve() or not source.is_dir():
            raise SelfModificationError(f"Unknown base self version: {parent}")
        next_number = max(
            (int(match.group(1)) for item in self.versions.iterdir()
             if (match := self.VERSION_PATTERN.fullmatch(item.name))),
            default=0,
        ) + 1
        version = f"v{next_number:06d}"
        destination = self.versions / version
        temporary = self.versions / f".{version}.{uuid.uuid4().hex}.tmp"
        try:
            shutil.copytree(source, temporary)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        self._write_current(version)
        self._mutable_task_id = self._task_id
        event = {
            "event": "opened",
            "version": version,
            "parent": parent,
            "task_id": self._task_id,
            "reason": str(reason)[:1000] if reason else None,
        }
        self._append_history(event)
        self._write_history_map(destination, event)
        return {
            "schema": "self_modification_transaction/v1",
            "version": version,
            "parent_version": parent,
            "mutable_root": "/self",
            "history_root": "/self-history",
            "same_agent_continues": True,
            "model_calls_started": 0,
            "candidate_pipeline_used": False,
            "current_version_switched": True,
            "effect": "CURRENT now points to this reversible version; edits under /self affect subsequent rounds and future tasks.",
        }

    def resolve(self, value: str, *, write: bool) -> Path:
        if not self.exposed:
            raise SelfModificationError("No active task can access /self")
        raw = str(value).strip().replace("\\", "/")
        if raw in {"/self", "/self/"}:
            relative = Path(".")
        elif raw.startswith("/self/"):
            relative = Path(raw[len("/self/"):])
        else:
            raise SelfModificationError("Self paths must start with /self")
        resolved = (self.current_path / relative).resolve()
        try:
            resolved.relative_to(self.current_path)
        except ValueError as exc:
            raise SelfModificationError("Path escapes the mutable self root") from exc
        if write and not self.writable:
            raise SelfModificationError("The current self version is read-only until evolve is called")
        return resolved

    def versions_summary(self) -> list[dict[str, Any]]:
        current = self.current_version()
        result = []
        for path in sorted(self.versions.iterdir()):
            if self.VERSION_PATTERN.fullmatch(path.name):
                result.append({"version": path.name, "current": path.name == current})
        return result

    def _write_current(self, version: str) -> None:
        temporary = self.root / f".CURRENT.{uuid.uuid4().hex}.tmp"
        temporary.write_text(version + "\n", encoding="utf-8")
        os.replace(temporary, self.current_file)

    def _append_history(self, value: dict[str, Any]) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), **value}
        with self.history_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _write_history_map(self, destination: Path, event: dict[str, Any]) -> None:
        lines = [
            "# Self history",
            "",
            "This file is a convenience map. Immutable version contents are mounted read-only at `/self-history`.",
            "",
            f"Current version: `{event['version']}`",
            f"Parent version: `{event['parent']}`",
            "",
            "Known versions:",
        ]
        lines.extend(f"- `{item['version']}`" for item in self.versions_summary())
        (destination / "HISTORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _initial_system_map() -> str:
        return """# Self

Mutable root after `evolve`: `/self`

Behavior instructions loaded each model round: `SYSTEM.md`

Persistent executable helpers and reusable code may be placed in:

- `tools/`
- `components/`

Immutable Host substrate:

- sandbox and authority enforcement
- credentials
- version history and rollback source
- audit records and hard resource limits
- the `evolve` primitive

Immutable prior versions are available read-only at `/self-history` after `evolve`.
"""
