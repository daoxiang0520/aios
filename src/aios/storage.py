from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .types import (
    Event,
    EventStatus,
    Goal,
    GoalStatus,
    GoalType,
    Memory,
    MemoryType,
    Task,
    TaskStatus,
)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 50,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_queue
    ON events(status, priority DESC, id ASC);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    status TEXT NOT NULL DEFAULT 'active',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_goals_active
    ON goals(status, priority DESC, id ASC);

CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_traces_cycle ON traces(cycle_id, id);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    request TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tasks_status
    ON tasks(status, priority DESC, id DESC);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    phase TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id, id DESC);

CREATE TABLE IF NOT EXISTS dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    key TEXT,
    content TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type, importance DESC, id DESC);

CREATE TABLE IF NOT EXISTS harness_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL UNIQUE,
    settings TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    parent_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(parent_id) REFERENCES harness_versions(id)
);

CREATE TABLE IF NOT EXISTS evolution_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mutation TEXT NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    benchmark TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS evolution_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    diagnosis TEXT NOT NULL DEFAULT '{}',
    candidate_ids TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    report TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_evolution_runs_status ON evolution_runs(status, id DESC);
"""


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            existing = connection.execute("SELECT id FROM harness_versions LIMIT 1").fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO harness_versions(version,settings,status) VALUES(1,'{}','active')"
                )

    def add_event(self, event: Event) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO events(type,payload,priority,status) VALUES(?,?,?,?)",
                (event.type, json.dumps(event.payload, ensure_ascii=False), event.priority, event.status.value),
            )
            return int(cursor.lastrowid)

    def claim_events(self, limit: int = 20) -> list[Event]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM events WHERE status=? ORDER BY priority DESC,id ASC LIMIT ?",
                (EventStatus.PENDING.value, limit),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE events SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
                    (EventStatus.PROCESSING.value, *ids),
                )
        return [self._event_from_row(row, EventStatus.PROCESSING) for row in rows]

    def finish_events(self, ids: list[int], *, error: str | None = None) -> None:
        if not ids:
            return
        status = EventStatus.FAILED if error else EventStatus.DONE
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE events SET status=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
                (status.value, error, *ids),
            )

    def recover_processing_events(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE events SET status=?,updated_at=CURRENT_TIMESTAMP WHERE status=?",
                (EventStatus.PENDING.value, EventStatus.PROCESSING.value),
            )
            return cursor.rowcount

    def count_pending_events(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM events WHERE status=?", (EventStatus.PENDING.value,)
            ).fetchone()
            return int(row["n"])

    def add_goal(self, goal: Goal) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO goals(title,type,priority,status,metadata) VALUES(?,?,?,?,?)",
                (
                    goal.title,
                    goal.type.value,
                    goal.priority,
                    goal.status.value,
                    json.dumps(goal.metadata, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def list_goals(self, *, active_only: bool = False) -> list[Goal]:
        query = "SELECT * FROM goals"
        params: tuple[Any, ...] = ()
        if active_only:
            query += " WHERE status=?"
            params = (GoalStatus.ACTIVE.value,)
        query += " ORDER BY priority DESC,id ASC"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._goal_from_row(row) for row in rows]

    def update_goal_status(self, goal_id: int, status: GoalStatus) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE goals SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status.value, goal_id),
            )

    def trace(self, cycle_id: str, kind: str, data: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO traces(cycle_id,kind,data) VALUES(?,?,?)",
                (cycle_id, kind, json.dumps(data, ensure_ascii=False, default=str)),
            )

    def recent_traces(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM traces ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "cycle_id": row["cycle_id"],
                "kind": row["kind"],
                "data": json.loads(row["data"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def create_task(self, task: Task) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO tasks(title,request,priority,status,attempts,max_attempts)
                   VALUES(?,?,?,?,?,?)""",
                (
                    task.title,
                    task.request,
                    task.priority,
                    task.status.value,
                    task.attempts,
                    task.max_attempts,
                ),
            )
            return int(cursor.lastrowid)

    def get_task(self, task_id: int) -> Task | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task_from_row(row) if row else None

    def list_tasks(self, limit: int = 50, status: TaskStatus | None = None) -> list[Task]:
        query = "SELECT * FROM tasks"
        params: tuple[Any, ...]
        if status:
            query += " WHERE status=?"
            params = (status.value, limit)
        else:
            params = (limit,)
        query += " ORDER BY id DESC LIMIT ?"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._task_from_row(row) for row in rows]

    def start_task_attempt(self, task_id: int) -> Task:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE tasks SET status=?,attempts=attempts+1,result=NULL,error=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (TaskStatus.RUNNING.value, task_id),
            )
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        return self._task_from_row(row)

    def update_task(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE tasks SET status=?,result=?,error=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    status.value,
                    json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                    error,
                    task_id,
                ),
            )

    def add_checkpoint(self, task_id: int, phase: str, data: dict[str, Any]) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO checkpoints(task_id,phase,data) VALUES(?,?,?)",
                (task_id, phase, json.dumps(data, ensure_ascii=False, default=str)),
            )
            return int(cursor.lastrowid)

    def task_checkpoints(self, task_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM checkpoints WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        return [
            {"id": row["id"], "phase": row["phase"], "data": json.loads(row["data"]), "created_at": row["created_at"]}
            for row in rows
        ]

    def add_dead_letter(self, task_id: int | None, event: Event, error: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO dead_letters(task_id,event_type,payload,error) VALUES(?,?,?,?)",
                (task_id, event.type, json.dumps(event.payload, ensure_ascii=False), error),
            )
            return int(cursor.lastrowid)

    def list_dead_letters(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM dead_letters ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload"]),
                "error": row["error"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def add_memory(self, memory: Memory) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO memories(type,key,content,importance,metadata)
                   VALUES(?,?,?,?,?)""",
                (
                    memory.type.value,
                    memory.key,
                    memory.content,
                    memory.importance,
                    json.dumps(memory.metadata, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def list_memories(self, limit: int = 50, type: MemoryType | None = None) -> list[Memory]:
        query = "SELECT * FROM memories"
        params: tuple[Any, ...]
        if type:
            query += " WHERE type=?"
            params = (type.value, limit)
        else:
            params = (limit,)
        query += " ORDER BY importance DESC,id DESC LIMIT ?"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def retry_task(self, task_id: int) -> int:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        with self.connect() as connection:
            connection.execute(
                """UPDATE tasks SET status=?,attempts=0,result=NULL,error=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (TaskStatus.QUEUED.value, task_id),
            )
        return self.add_event(
            Event("TASK_REQUEST", {"task_id": task_id, "message": task.request}, task.priority)
        )

    def active_harness(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM harness_versions WHERE status='active' ORDER BY version DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return {"id": None, "version": 0, "settings": {}}
        return {
            "id": int(row["id"]),
            "version": int(row["version"]),
            "settings": json.loads(row["settings"]),
            "status": str(row["status"]),
            "parent_id": row["parent_id"],
            "created_at": str(row["created_at"]),
        }

    def list_harness_versions(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM harness_versions ORDER BY version DESC").fetchall()
        return [
            {
                "id": int(row["id"]),
                "version": int(row["version"]),
                "settings": json.loads(row["settings"]),
                "status": str(row["status"]),
                "parent_id": row["parent_id"],
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def add_candidate(self, mutation: dict[str, Any], rationale: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO evolution_candidates(mutation,rationale) VALUES(?,?)",
                (json.dumps(mutation, ensure_ascii=False), rationale),
            )
            return int(cursor.lastrowid)

    def get_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM evolution_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
        return self._candidate_from_row(row) if row else None

    def list_candidates(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM evolution_candidates ORDER BY id DESC"
            ).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def add_evolution_run(
        self,
        trigger: str,
        diagnosis: dict[str, Any],
        candidate_ids: list[int],
        status: str,
        report: dict[str, Any],
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO evolution_runs(trigger,diagnosis,candidate_ids,status,report)
                   VALUES(?,?,?,?,?)""",
                (
                    trigger,
                    json.dumps(diagnosis, ensure_ascii=False, default=str),
                    json.dumps(candidate_ids),
                    status,
                    json.dumps(report, ensure_ascii=False, default=str),
                ),
            )
            return int(cursor.lastrowid)

    def list_evolution_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM evolution_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "trigger": str(row["trigger"]),
                "diagnosis": json.loads(row["diagnosis"]),
                "candidate_ids": json.loads(row["candidate_ids"]),
                "status": str(row["status"]),
                "report": json.loads(row["report"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def update_candidate(
        self,
        candidate_id: int,
        status: str,
        benchmark: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE evolution_candidates SET status=?,benchmark=COALESCE(?,benchmark),
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    status,
                    json.dumps(benchmark, ensure_ascii=False, default=str) if benchmark is not None else None,
                    candidate_id,
                ),
            )

    def promote_candidate(self, candidate_id: int) -> dict[str, Any]:
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError(f"Unknown candidate: {candidate_id}")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT * FROM harness_versions WHERE status='active' ORDER BY version DESC LIMIT 1"
            ).fetchone()
            parent_settings = json.loads(active["settings"]) if active else {}
            merged = {**parent_settings, **candidate["mutation"]}
            version = int(
                connection.execute("SELECT COALESCE(MAX(version),0)+1 AS n FROM harness_versions").fetchone()["n"]
            )
            if active:
                connection.execute("UPDATE harness_versions SET status='retired' WHERE id=?", (active["id"],))
            cursor = connection.execute(
                """INSERT INTO harness_versions(version,settings,status,parent_id)
                   VALUES(?,?,'active',?)""",
                (version, json.dumps(merged, ensure_ascii=False), active["id"] if active else None),
            )
            connection.execute(
                "UPDATE evolution_candidates SET status='promoted',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (candidate_id,),
            )
            version_id = int(cursor.lastrowid)
        return {"id": version_id, "version": version, "settings": merged, "status": "active"}

    def rollback_harness(self, version: int) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                "SELECT * FROM harness_versions WHERE version=?", (version,)
            ).fetchone()
            if target is None:
                raise KeyError(f"Unknown harness version: {version}")
            connection.execute("UPDATE harness_versions SET status='retired' WHERE status='active'")
            connection.execute("UPDATE harness_versions SET status='active' WHERE id=?", (target["id"],))
        return self.active_harness()

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "mutation": json.loads(row["mutation"]),
            "rationale": str(row["rationale"]),
            "status": str(row["status"]),
            "benchmark": json.loads(row["benchmark"]) if row["benchmark"] else None,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _event_from_row(row: sqlite3.Row, status: EventStatus | None = None) -> Event:
        return Event(
            id=int(row["id"]),
            type=str(row["type"]),
            payload=json.loads(row["payload"]),
            priority=int(row["priority"]),
            status=status or EventStatus(row["status"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _goal_from_row(row: sqlite3.Row) -> Goal:
        return Goal(
            id=int(row["id"]),
            title=str(row["title"]),
            type=GoalType(row["type"]),
            priority=int(row["priority"]),
            status=GoalStatus(row["status"]),
            metadata=json.loads(row["metadata"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> Task:
        return Task(
            id=int(row["id"]),
            title=str(row["title"]),
            request=str(row["request"]),
            priority=int(row["priority"]),
            status=TaskStatus(row["status"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            result=json.loads(row["result"]) if row["result"] else None,
            error=str(row["error"]) if row["error"] else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> Memory:
        return Memory(
            id=int(row["id"]),
            type=MemoryType(row["type"]),
            key=str(row["key"]) if row["key"] else None,
            content=str(row["content"]),
            importance=float(row["importance"]),
            metadata=json.loads(row["metadata"]),
            created_at=str(row["created_at"]),
        )
