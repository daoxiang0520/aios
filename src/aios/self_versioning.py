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
        self.open_draft_file = self.root / "OPEN_DRAFT.json"
        self.history_file = self.root / "history.jsonl"
        self.schema_file = self.root / "SCHEMA"
        self._task_id: int | None = None
        self._mutable_task_id: int | None = None
        self._draft_version: str | None = None
        self._draft_parent_version: str | None = None
        self._execution_version: str | None = None

    def initialize(self) -> None:
        self.versions.mkdir(parents=True, exist_ok=True)
        if not self.current_file.is_file():
            version = "v000001"
            initial = self.versions / version
            initial.mkdir()
            (initial / "tools").mkdir()
            (initial / "components").mkdir()
            (initial / "SYSTEM.md").write_text(self._initial_system_map(), encoding="utf-8")
            self._write_default_harness(initial)
            self._write_default_agent(initial)
            self._write_current(version)
            self._append_history({
                "event": "initialized",
                "version": version,
                "parent": None,
                "task_id": None,
                "reason": None,
            })
        self.current_version()  # validate the persisted pointer
        if not self.schema_file.is_file():
            # One-time alpha.1 -> alpha.2 migration. Once marked, a descendant
            # may deliberately break/delete its Harness and the Host will not
            # silently repair the experimental consequence.
            self._ensure_bootstrap_layout(self.current_path)
            self.schema_file.write_text("3\n", encoding="utf-8")

    def begin_task(self, task_id: int, *, execution_version: str | None = None) -> None:
        task_id = int(task_id)
        if self._task_id != task_id:
            self._task_id = task_id
            self._mutable_task_id = None
            self._draft_version = None
            self._draft_parent_version = None
            self._restore_open_draft(task_id)
        if execution_version is not None:
            self._version_path(execution_version)
        self._execution_version = execution_version

    @property
    def exposed(self) -> bool:
        return self._task_id is not None

    @property
    def writable(self) -> bool:
        return (
            self._task_id is not None
            and self._mutable_task_id == self._task_id
            and self._draft_version is not None
        )

    @property
    def current_path(self) -> Path:
        return (self.versions / self.current_version()).resolve()

    @property
    def self_path(self) -> Path:
        version = (
            self._draft_version
            if self.writable
            else self._execution_version or self.current_version()
        )
        return (self.versions / str(version)).resolve()

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

    def system_prompt(self, version: str | None = None) -> str:
        selected = version or self.current_version()
        if self.VERSION_PATTERN.fullmatch(selected) is None:
            raise SelfModificationError("Invalid self version")
        root = (self.versions / selected).resolve()
        if root.parent != self.versions.resolve() or not root.is_dir():
            raise SelfModificationError(f"Unknown self version: {selected}")
        path = root / "SYSTEM.md"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[
            : max(0, int(self.config.max_system_prompt_characters))
        ]

    def self_goal(self, version: str | None = None) -> str:
        root = self._version_path(version or self.current_version())
        path = root / "SELF_GOAL.md"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[
            : max(0, int(self.config.max_system_prompt_characters))
        ]

    def architecture(self, version: str | None = None) -> dict[str, Any]:
        selected = version or self.current_version()
        root = self._version_path(selected)
        if (root / "agent" / "main.py").is_file():
            return {
                "schema": "self_agent_runtime/v1",
                "kind": "agent",
                "entrypoint": "agent/main.py",
                "legacy_fallback": (root / "harness" / "runner.py").is_file(),
            }
        return {
            "schema": "self_agent_runtime/v1",
            "kind": "legacy_harness",
            "entrypoint": "harness/runner.py",
            "legacy_fallback": False,
        }

    def migrate_agent_architecture(self) -> dict[str, Any]:
        """Create an explicitly Host-approved descendant with the generic Agent entrypoint."""
        if not self.config.enabled:
            raise SelfModificationError("Self modification is disabled")
        if self.writable:
            raise SelfModificationError("Cannot migrate while a task-owned Self draft is open")
        parent = self.current_version()
        source = self._version_path(parent)
        if (source / "agent" / "main.py").is_file():
            return {
                "schema": "self_agent_migration/v1",
                "changed": False,
                "previous_version": parent,
                "current_version": parent,
                "architecture": self.architecture(parent),
            }
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
            self._write_default_agent(temporary)
            system = temporary / "SYSTEM.md"
            content = system.read_text(encoding="utf-8", errors="replace") if system.is_file() else "# Self\n"
            if "## Mutable Agent architecture" not in content:
                system.write_text(
                    content.rstrip() + "\n\n" + self._agent_system_appendix(),
                    encoding="utf-8",
                )
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        self._write_current(version)
        event = {
            "event": "host_approved_agent_architecture_migration",
            "version": version,
            "parent": parent,
            "task_id": None,
            "reason": "explicit human-approved mutable Agent baseline",
        }
        self._append_history(event)
        self._write_history_map(destination, event, current=True)
        self.schema_file.write_text("3\n", encoding="utf-8")
        return {
            "schema": "self_agent_migration/v1",
            "changed": True,
            "previous_version": parent,
            "current_version": version,
            "production_activated": True,
            "created_by": "human_confirmed_host_migration",
            "architecture": self.architecture(version),
        }

    def open(self, reason: str | None = None, base_version: str | None = None) -> dict[str, Any]:
        if not self.config.enabled:
            raise SelfModificationError("Self modification is disabled")
        if self._task_id is None:
            raise SelfModificationError("No active task can own the self-modification transaction")
        if self.writable:
            raise SelfModificationError("A self-modification draft is already open")
        persisted = self._persisted_or_inferred_open_draft()
        if persisted is not None:
            owner = persisted.get("task_id")
            version = persisted.get("version")
            raise SelfModificationError(
                f"Self draft {version} is already owned by task {owner}; "
                "resume that task and commit or abort it before opening another draft"
            )
        parent = base_version or self._execution_version or self.current_version()
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
        self._mutable_task_id = self._task_id
        self._draft_version = version
        self._draft_parent_version = parent
        event = {
            "event": "opened",
            "version": version,
            "parent": parent,
            "task_id": self._task_id,
            "reason": str(reason)[:1000] if reason else None,
        }
        self._append_history(event)
        self._write_history_map(destination, event, current=False)
        self._write_open_draft({
            "schema": "self_open_draft/v1",
            "task_id": self._task_id,
            "version": version,
            "parent_version": parent,
        })
        return {
            "schema": "self_modification_transaction/v1",
            "version": version,
            "parent_version": parent,
            "mutable_root": "/self",
            "history_root": "/self-history",
            "same_agent_continues": True,
            "model_calls_started": 0,
            "candidate_pipeline_used": False,
            "current_version_switched": False,
            "committed": False,
            "effect": "A draft descendant is writable at /self. Call evolve(operation='commit') to make it the next active Self.",
        }

    def commit(self) -> dict[str, Any]:
        if not self.writable or self._draft_version is None:
            raise SelfModificationError("No open self-modification draft")
        version = self._draft_version
        parent = self._draft_parent_version or self.current_version()
        destination = (self.versions / version).resolve()
        self._write_current(version)
        self._append_history({
            "event": "committed", "version": version, "parent": parent,
            "task_id": self._task_id, "reason": None,
        })
        self._draft_version = None
        self._draft_parent_version = None
        self._mutable_task_id = None
        self._execution_version = None
        self._write_history_map(destination, {
            "version": version, "parent": parent,
        }, current=True)
        self._clear_open_draft(version)
        return {
            "schema": "self_modification_transaction/v1",
            "operation": "commit",
            "version": version,
            "parent_version": parent,
            "committed": True,
            "current_version_switched": True,
            "restart_required": True,
            "effect": "The task must checkpoint; the next cycle/task loads this Self version.",
        }

    def abort(self) -> dict[str, Any]:
        if not self.writable or self._draft_version is None:
            raise SelfModificationError("No open self-modification draft")
        version = self._draft_version
        parent = self._draft_parent_version or self.current_version()
        self._append_history({
            "event": "abandoned", "version": version,
            "parent": parent, "task_id": self._task_id, "reason": None,
        })
        self._draft_version = None
        self._draft_parent_version = None
        self._mutable_task_id = None
        self._clear_open_draft(version)
        return {
            "schema": "self_modification_transaction/v1",
            "operation": "abort",
            "version": version,
            "committed": False,
            "current_version": self.current_version(),
            "restart_required": False,
        }

    def parent_version(self, version: str | None = None) -> str | None:
        """Return the recorded parent without treating recency as fitness."""
        selected = version or self.current_version()
        self._version_path(selected)
        if not self.history_file.is_file():
            return None
        parent: str | None = None
        for line in self.history_file.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("version") != selected:
                continue
            candidate = item.get("parent")
            if (
                isinstance(candidate, str)
                and self.VERSION_PATTERN.fullmatch(candidate)
                and (self.versions / candidate).is_dir()
            ):
                parent = candidate
        return parent

    def activate(self, version: str) -> dict[str, Any]:
        if self.VERSION_PATTERN.fullmatch(version) is None:
            raise SelfModificationError("Invalid self version")
        path = (self.versions / version).resolve()
        if path.parent != self.versions.resolve() or not path.is_dir():
            raise SelfModificationError(f"Unknown self version: {version}")
        previous = self.current_version()
        self._write_current(version)
        self._append_history({
            "event": "host_activated", "version": version, "parent": previous,
            "task_id": None, "reason": "explicit Host recovery",
        })
        return {"previous_version": previous, "current_version": version}

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
        resolved = (self.self_path / relative).resolve()
        try:
            resolved.relative_to(self.self_path)
        except ValueError as exc:
            raise SelfModificationError("Path escapes the mutable self root") from exc
        if write and not self.writable:
            raise SelfModificationError("The current self version is read-only until evolve is called")
        return resolved

    def versions_summary(self) -> list[dict[str, Any]]:
        current = self.current_version()
        draft = self._persisted_or_inferred_open_draft()
        draft_version = str(draft.get("version")) if isinstance(draft, dict) else None
        draft_task_id = draft.get("task_id") if isinstance(draft, dict) else None
        result = []
        for path in sorted(self.versions.iterdir()):
            if self.VERSION_PATTERN.fullmatch(path.name):
                is_current = path.name == current
                is_draft = path.name == draft_version and not is_current
                result.append({
                    "version": path.name,
                    "current": is_current,
                    "draft": is_draft,
                    "draft_task_id": draft_task_id if is_draft else None,
                    "status": "current" if is_current else "draft" if is_draft else "historical",
                })
        return result

    def _restore_open_draft(self, task_id: int) -> None:
        draft = self._persisted_or_inferred_open_draft()
        if not isinstance(draft, dict) or draft.get("task_id") != int(task_id):
            return
        version = draft.get("version")
        parent = draft.get("parent_version") or draft.get("parent")
        if not isinstance(version, str) or not isinstance(parent, str):
            return
        if self.VERSION_PATTERN.fullmatch(version) is None:
            return
        if self.VERSION_PATTERN.fullmatch(parent) is None:
            return
        if not (self.versions / version).is_dir() or not (self.versions / parent).is_dir():
            return
        current = self.current_version()
        if current == version:
            # A process may have exited after publishing CURRENT but before removing
            # the durable transaction pointer.  The commit already took effect.
            self._clear_open_draft(version)
            return
        if current != parent:
            # Never resume a draft against a different production ancestor.
            return
        self._mutable_task_id = int(task_id)
        self._draft_version = version
        self._draft_parent_version = parent
        if not self.open_draft_file.is_file():
            self._write_open_draft({
                "schema": "self_open_draft/v1",
                "task_id": int(task_id),
                "version": version,
                "parent_version": parent,
            })

    def _persisted_or_inferred_open_draft(self) -> dict[str, Any] | None:
        if self.open_draft_file.is_file():
            try:
                value = json.loads(self.open_draft_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                value = None
            if isinstance(value, dict):
                version = value.get("version")
                parent = value.get("parent_version")
                if (
                    isinstance(version, str)
                    and isinstance(parent, str)
                    and (self.versions / version).is_dir()
                    and (self.versions / parent).is_dir()
                ):
                    return value

        # Compatibility recovery for drafts opened before OPEN_DRAFT.json existed,
        # or for a process interrupted between history append and pointer creation.
        open_by_version: dict[str, dict[str, Any]] = {}
        if self.history_file.is_file():
            for line in self.history_file.read_text(
                encoding="utf-8", errors="replace",
            ).splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                version = item.get("version")
                if not isinstance(version, str):
                    continue
                if item.get("event") == "opened":
                    open_by_version[version] = item
                elif item.get("event") in {"committed", "abandoned"}:
                    open_by_version.pop(version, None)
        current = self.current_version()
        candidates = [
            item for version, item in open_by_version.items()
            if (self.versions / version).is_dir()
            and item.get("parent") == current
            and isinstance(item.get("task_id"), int)
        ]
        if not candidates:
            return None
        latest = max(
            candidates,
            key=lambda item: int(self.VERSION_PATTERN.fullmatch(str(item["version"])).group(1)),
        )
        return {
            "schema": "self_open_draft/v1",
            "task_id": int(latest["task_id"]),
            "version": str(latest["version"]),
            "parent_version": str(latest["parent"]),
        }

    def _write_open_draft(self, value: dict[str, Any]) -> None:
        temporary = self.root / f".OPEN_DRAFT.{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.open_draft_file)

    def _clear_open_draft(self, version: str) -> None:
        if not self.open_draft_file.is_file():
            return
        try:
            value = json.loads(self.open_draft_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict) or value.get("version") == version:
            self.open_draft_file.unlink(missing_ok=True)

    def _version_path(self, version: str) -> Path:
        if self.VERSION_PATTERN.fullmatch(version) is None:
            raise SelfModificationError("Invalid self version")
        root = (self.versions / version).resolve()
        if root.parent != self.versions.resolve() or not root.is_dir():
            raise SelfModificationError(f"Unknown self version: {version}")
        return root

    def _write_current(self, version: str) -> None:
        temporary = self.root / f".CURRENT.{uuid.uuid4().hex}.tmp"
        temporary.write_text(version + "\n", encoding="utf-8")
        os.replace(temporary, self.current_file)

    def _append_history(self, value: dict[str, Any]) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), **value}
        with self.history_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _write_history_map(
        self, destination: Path, event: dict[str, Any], *, current: bool,
    ) -> None:
        lines = [
            "# Self history",
            "",
            "This file is a convenience map. Immutable version contents are mounted read-only at `/self-history`.",
            "",
            f"This version: `{event['version']}`",
            f"Current: `{str(current).lower()}`",
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

The active mutable Agent architecture is under `agent/`; `agent/main.py` is the
generic lifecycle entrypoint. Legacy cognition Harness code remains under
`harness/` as a rollback-compatible fallback. Changes take effect only after
`evolve(operation="commit")` checkpoints the task and starts the next cycle.

Immutable Host substrate:

- sandbox and authority enforcement
- credentials
- version history and rollback source
- audit records and hard resource limits
- the `evolve` primitive

Immutable prior versions are available read-only at `/self-history` after `evolve`.
"""

    def _ensure_bootstrap_layout(self, root: Path) -> None:
        (root / "tools").mkdir(exist_ok=True)
        (root / "components").mkdir(exist_ok=True)
        system = root / "SYSTEM.md"
        if system.is_file():
            content = system.read_text(encoding="utf-8", errors="replace")
            if "`harness/`" not in content:
                system.write_text(
                    content.rstrip() +
                    "\n\nAgent cognition Harness code is under `harness/`. Changes take effect "
                    "after `evolve(operation=\"commit\")` checkpoints into the next cycle.\n",
                    encoding="utf-8",
                )
        if not (root / "harness" / "runner.py").is_file():
            self._write_default_harness(root)
        if not (root / "agent" / "main.py").is_file():
            self._write_default_agent(root)

    @staticmethod
    def _agent_system_appendix() -> str:
        return """## Mutable Agent architecture

The active cognition entrypoint is `/self/agent/main.py`. The immutable Host invokes it with
`before_model`, `after_plan`, and `after_round` lifecycle events, supplies model and tool gateways,
and validates only the external JSON envelope and authority boundary. You may replace `main.py`,
split it into arbitrary modules, create components, change context/memory/tool/continuation policy,
or remove the legacy `harness/` layout. Changes become active only after
`evolve(operation=\"commit\")` restarts the task on the descendant.

`/self/SELF_GOAL.md` is your mutable persistent objective. It is loaded every model round. There is
no mutation quota and no Host fitness verdict; change architecture only when your own observations
support a reusable improvement, and observe later consequences as evidence for further revision.
"""

    @classmethod
    def _write_default_agent(cls, root: Path) -> None:
        agent = root / "agent"
        tests = root / "tests"
        agent.mkdir(exist_ok=True)
        tests.mkdir(exist_ok=True)
        self_goal = root / "SELF_GOAL.md"
        if not self_goal.is_file():
            self_goal.write_text(
                """# Persistent Self objective

Complete ordinary user tasks while maintaining the reusable system that produces your behavior.
When observed consequences reveal a recurring limitation in your context, memory, tool strategy,
continuation, or internal organization, you may investigate and revise your versioned Agent
architecture. Prefer changes whose purpose and later consequences you can observe. No change is
required when evidence is insufficient.
""",
                encoding="utf-8",
            )
        (agent / "ARCHITECTURE.json").write_text(json.dumps({
            "schema": "self_agent/v1",
            "entrypoint": "agent/main.py",
            "lifecycle_events": ["before_model", "after_plan", "after_round"],
            "host_services": ["model_gateway", "tool_gateway", "versioning", "audit", "rollback"],
            "mutable_scope": "/self",
        }, indent=2), encoding="utf-8")
        (agent / "main.py").write_text('''from __future__ import annotations

import json
import sys
from pathlib import Path


def transition(event, payload):
    """Default Agent architecture; descendants may replace or reorganize it."""
    if event == "before_model":
        context = payload.get("context", {})
        if not isinstance(context, dict):
            raise TypeError("before_model context must be an object")
        return {"context": context}
    if event == "after_plan":
        actions = payload.get("actions", [])
        if not isinstance(actions, list):
            raise TypeError("after_plan actions must be an array")
        return {"actions": actions, "autonomous_actions": []}
    if event == "after_round":
        return {
            "loop": {"allow_another_round": True},
            "continuation": {"checkpoint": False},
        }
    raise ValueError(f"Unknown Agent lifecycle event: {event}")


if __name__ == "__main__":
    event, input_path, output_path = sys.argv[1:]
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    output = transition(event, payload)
    if not isinstance(output, dict):
        raise TypeError("Agent transition must return an object")
    Path(output_path).write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
''', encoding="utf-8")
        (tests / "test_agent.py").write_text('''import json
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as temporary:
    temp = Path(temporary)
    source = temp / "input.json"
    target = temp / "output.json"
    source.write_text(json.dumps({"context": {}, "state": {}}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(root / "agent" / "main.py"), "before_model", str(source), str(target)])
    assert result.returncode == 0
    assert isinstance(json.loads(target.read_text(encoding="utf-8"))["context"], dict)
print("PASS")
''', encoding="utf-8")

    @staticmethod
    def _write_default_harness(root: Path) -> None:
        harness = root / "harness"
        tests = root / "tests"
        harness.mkdir(exist_ok=True)
        tests.mkdir(exist_ok=True)
        files = {
            "context.py": """def build(context, state):
    return context
""",
            "memory_policy.py": """def select(memories, state):
    return memories
""",
            "tool_policy.py": """def select(actions, state):
    return actions
""",
            "loop.py": """def after_round(state):
    return {"allow_another_round": True}
""",
            "continuation.py": """def decide(state):
    return {"checkpoint": False}
""",
            "runner.py": '''from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load(name):
    path = ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"self_harness_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load harness module: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(stage, payload):
    state = payload.get("state", {})
    if stage == "before_model":
        context = load("context").build(payload.get("context", {}), state)
        if not isinstance(context, dict):
            raise TypeError("context.build must return an object")
        memories = load("memory_policy").select(context.get("retrieved_memories", []), state)
        if not isinstance(memories, list):
            raise TypeError("memory_policy.select must return an array")
        context["retrieved_memories"] = memories
        return {"context": context}
    if stage == "after_plan":
        actions = load("tool_policy").select(payload.get("actions", []), state)
        if not isinstance(actions, list):
            raise TypeError("tool_policy.select must return an array")
        return {"actions": actions}
    if stage == "after_round":
        loop = load("loop").after_round(state)
        continuation = load("continuation").decide(state)
        if not isinstance(loop, dict) or not isinstance(continuation, dict):
            raise TypeError("round policies must return objects")
        return {"loop": loop, "continuation": continuation}
    raise ValueError(f"Unknown Harness stage: {stage}")


if __name__ == "__main__":
    stage, input_path, output_path = sys.argv[1:]
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    output = run(stage, payload)
    Path(output_path).write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
''',
        }
        for name, content in files.items():
            (harness / name).write_text(content, encoding="utf-8")
        (tests / "test_harness.py").write_text('''import json
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as temporary:
    temp = Path(temporary)
    source = temp / "input.json"
    target = temp / "output.json"
    source.write_text(json.dumps({"context": {"retrieved_memories": []}, "state": {}}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(root / "harness" / "runner.py"), "before_model", str(source), str(target)])
    assert result.returncode == 0
    assert isinstance(json.loads(target.read_text(encoding="utf-8"))["context"], dict)
print("PASS")
''', encoding="utf-8")
