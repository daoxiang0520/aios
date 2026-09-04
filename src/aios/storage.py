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
    error TEXT,
    task_id INTEGER,
    checkpoint_id INTEGER,
    continuation_generation INTEGER
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
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    current_checkpoint_id INTEGER,
    continuation_generation INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS evolution_lineages (
    lineage_id TEXT PRIMARY KEY,
    parent_lineage_id TEXT,
    generation INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'system',
    status TEXT NOT NULL DEFAULT 'living',
    settings TEXT NOT NULL DEFAULT '{}',
    component_set TEXT NOT NULL DEFAULT '{}',
    mutation TEXT NOT NULL DEFAULT '{}',
    source_candidate_id INTEGER,
    source_component_candidate_id TEXT,
    created_by TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(parent_lineage_id) REFERENCES evolution_lineages(lineage_id),
    FOREIGN KEY(source_candidate_id) REFERENCES evolution_candidates(id)
);
CREATE INDEX IF NOT EXISTS idx_evolution_lineages_parent
    ON evolution_lineages(parent_lineage_id, generation, created_at);

CREATE TABLE IF NOT EXISTS task_lineage_bindings (
    task_id INTEGER PRIMARY KEY,
    lineage_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(lineage_id) REFERENCES evolution_lineages(lineage_id)
);
CREATE INDEX IF NOT EXISTS idx_task_lineage_bindings_lineage
    ON task_lineage_bindings(lineage_id, task_id DESC);

CREATE TABLE IF NOT EXISTS lineage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lineage_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(lineage_id) REFERENCES evolution_lineages(lineage_id)
);
CREATE INDEX IF NOT EXISTS idx_lineage_events_lineage
    ON lineage_events(lineage_id, id DESC);

CREATE TABLE IF NOT EXISTS skill_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invocation_id TEXT NOT NULL UNIQUE,
    cycle_id TEXT NOT NULL,
    task_id INTEGER,
    model_round INTEGER NOT NULL,
    sequence_index INTEGER NOT NULL,
    model_calls_before INTEGER NOT NULL DEFAULT 0,
    model_calls_after INTEGER,
    tokens_before INTEGER NOT NULL DEFAULT 0,
    tokens_after INTEGER,
    skill_name TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    status TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    input_keys TEXT NOT NULL DEFAULT '[]',
    required_capabilities TEXT NOT NULL DEFAULT '[]',
    capability_assessment TEXT NOT NULL DEFAULT '{}',
    duration_ms REAL NOT NULL DEFAULT 0,
    exit_code INTEGER,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    verifier_passed INTEGER,
    task_outcome TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_skill_usage_name ON skill_usage(skill_name, id DESC);
CREATE INDEX IF NOT EXISTS idx_skill_usage_task ON skill_usage(task_id, id ASC);
CREATE INDEX IF NOT EXISTS idx_skill_usage_cycle ON skill_usage(cycle_id, id ASC);

CREATE TABLE IF NOT EXISTS skill_replay_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    evidence_level TEXT NOT NULL,
    passed INTEGER NOT NULL,
    negative_transfer INTEGER NOT NULL DEFAULT 0,
    utility_delta REAL,
    report TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_skill_replay_candidate
ON skill_replay_reports(candidate_id,id DESC);
CREATE INDEX IF NOT EXISTS idx_skill_replay_name
ON skill_replay_reports(skill_name,id DESC);

CREATE TABLE IF NOT EXISTS components (
    component_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    active_version TEXT NOT NULL,
    trust_class TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(kind,name)
);
CREATE INDEX IF NOT EXISTS idx_components_kind_status ON components(kind,status,name);

CREATE TABLE IF NOT EXISTS component_versions (
    version_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL,
    version TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(component_id,version),
    FOREIGN KEY(component_id) REFERENCES components(component_id)
);

CREATE TABLE IF NOT EXISTS component_capabilities (
    component_id TEXT NOT NULL,
    version TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('requires','provides')),
    capability TEXT NOT NULL,
    PRIMARY KEY(component_id,version,direction,capability),
    FOREIGN KEY(component_id) REFERENCES components(component_id)
);
CREATE INDEX IF NOT EXISTS idx_component_capability
ON component_capabilities(capability,direction,component_id);

CREATE TABLE IF NOT EXISTS capability_implications (
    stronger TEXT NOT NULL,
    weaker TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(stronger,weaker)
);

CREATE TABLE IF NOT EXISTS task_capsules (
    capsule_id TEXT PRIMARY KEY,
    source_task_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    fidelity TEXT NOT NULL,
    manifest TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(source_task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_capsules_task ON task_capsules(source_task_id,created_at DESC);

CREATE TABLE IF NOT EXISTS capsule_objects (
    object_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    capsule_id TEXT NOT NULL,
    mutation_type TEXT NOT NULL,
    status TEXT NOT NULL,
    spec TEXT NOT NULL,
    report TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(capsule_id) REFERENCES task_capsules(capsule_id)
);

CREATE TABLE IF NOT EXISTS experiment_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    name TEXT NOT NULL,
    mutation TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(experiment_id,name),
    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
);

CREATE TABLE IF NOT EXISTS experiment_runs (
    run_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    variant TEXT NOT NULL,
    replicate INTEGER NOT NULL,
    evidence TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
);
CREATE INDEX IF NOT EXISTS idx_experiment_runs_experiment
ON experiment_runs(experiment_id,variant,replicate);

CREATE TABLE IF NOT EXISTS counterfactual_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL UNIQUE,
    promotion_state TEXT NOT NULL,
    report TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
);

CREATE TABLE IF NOT EXISTS semantic_judgements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    judgement TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
);

CREATE TABLE IF NOT EXISTS runtime_metrics (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
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
            self._migrate_runtime_correctness(connection)
            existing = connection.execute("SELECT id FROM harness_versions LIMIT 1").fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO harness_versions(version,settings,status) VALUES(1,'{}','active')"
                )

    def set_state(self, key: str, value: Any) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO state(key,value) VALUES(?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                   updated_at=CURRENT_TIMESTAMP""",
                (key, json.dumps(value, ensure_ascii=False, default=str)),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return default

    @staticmethod
    def _migrate_runtime_correctness(connection: sqlite3.Connection) -> None:
        event_columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(events)")
        }
        for name, declaration in (
            ("task_id", "INTEGER"),
            ("checkpoint_id", "INTEGER"),
            ("continuation_generation", "INTEGER"),
        ):
            if name not in event_columns:
                connection.execute(f"ALTER TABLE events ADD COLUMN {name} {declaration}")
        task_columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(tasks)")
        }
        if "current_checkpoint_id" not in task_columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN current_checkpoint_id INTEGER")
        if "continuation_generation" not in task_columns:
            connection.execute(
                "ALTER TABLE tasks ADD COLUMN continuation_generation INTEGER NOT NULL DEFAULT 0"
            )
        lineage_columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(evolution_lineages)")
        }
        if "component_set" not in lineage_columns:
            connection.execute(
                "ALTER TABLE evolution_lineages ADD COLUMN component_set TEXT NOT NULL DEFAULT '{}'"
            )
        if "source_component_candidate_id" not in lineage_columns:
            connection.execute(
                "ALTER TABLE evolution_lineages ADD COLUMN source_component_candidate_id TEXT"
            )
        rows = connection.execute(
            "SELECT id,payload FROM events WHERE type='TASK_CONTINUE'"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            connection.execute(
                """UPDATE events SET task_id=?,checkpoint_id=?,continuation_generation=?
                   WHERE id=?""",
                (
                    payload.get("task_id"), payload.get("checkpoint_id"),
                    payload.get("generation", 0), int(row["id"]),
                ),
            )
        duplicate_tasks = connection.execute(
            """SELECT task_id FROM events
               WHERE type='TASK_CONTINUE' AND status IN ('pending','processing')
                 AND task_id IS NOT NULL
               GROUP BY task_id HAVING COUNT(*) > 1"""
        ).fetchall()
        for row in duplicate_tasks:
            active = connection.execute(
                """SELECT id FROM events WHERE type='TASK_CONTINUE' AND task_id=?
                   AND status IN ('pending','processing') ORDER BY id DESC""",
                (row["task_id"],),
            ).fetchall()
            stale_ids = [int(item["id"]) for item in active[1:]]
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                connection.execute(
                    f"UPDATE events SET status='stale',error='duplicate_continuation_migration' "
                    f"WHERE id IN ({placeholders})",
                    tuple(stale_ids),
                )
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_active_continuation_task
               ON events(task_id)
               WHERE type='TASK_CONTINUE' AND status IN ('pending','processing')"""
        )

    def add_event(self, event: Event) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO events(
                       type,payload,priority,status,task_id,checkpoint_id,continuation_generation
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    event.type, json.dumps(event.payload, ensure_ascii=False),
                    event.priority, event.status.value,
                    event.payload.get("task_id"), event.payload.get("checkpoint_id"),
                    event.payload.get("generation"),
                ),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _increment_metric(
        connection: sqlite3.Connection, name: str, amount: int = 1,
    ) -> None:
        connection.execute(
            """INSERT INTO runtime_metrics(name,value) VALUES(?,?)
               ON CONFLICT(name) DO UPDATE SET
                 value=value+excluded.value,updated_at=CURRENT_TIMESTAMP""",
            (name, amount),
        )

    def runtime_metrics(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute("SELECT name,value FROM runtime_metrics").fetchall()
        return {str(row["name"]): int(row["value"]) for row in rows}

    def enqueue_continuation(
        self, task_id: int, checkpoint_id: int, message: str, priority: int,
    ) -> tuple[int | None, str]:
        terminal = {
            TaskStatus.COMPLETED.value, TaskStatus.FAILED.value,
            TaskStatus.DEAD_LETTER.value, TaskStatus.DEGRADED.value,
            TaskStatus.BLOCKED_CAPABILITY.value, TaskStatus.NEEDS_AUTHORITY.value,
            TaskStatus.TERMINAL_FAILURE.value, TaskStatus.NEEDS_REVIEW.value,
            TaskStatus.STOPPED.value, TaskStatus.YIELDED.value,
            TaskStatus.ABANDONED.value,
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT status,current_checkpoint_id,continuation_generation FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError(f"Unknown task: {task_id}")
            if str(task["status"]) in terminal:
                self._increment_metric(connection, "stale_continuations_discarded")
                return None, "terminal_task"
            existing = connection.execute(
                """SELECT id,checkpoint_id FROM events WHERE type='TASK_CONTINUE'
                   AND task_id=? AND status IN ('pending','processing') ORDER BY id DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
            if existing is not None and int(existing["checkpoint_id"] or -1) == checkpoint_id:
                self._increment_metric(connection, "continuation_duplicates_suppressed")
                return int(existing["id"]), "duplicate_suppressed"
            if existing is not None:
                connection.execute(
                    """UPDATE events SET status='stale',error='superseded_checkpoint',
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (int(existing["id"]),),
                )
                self._increment_metric(connection, "stale_continuations_discarded")
            generation = int(task["continuation_generation"] or 0) + 1
            payload = {
                "task_id": task_id, "message": message, "continuation": True,
                "checkpoint_id": checkpoint_id, "generation": generation,
            }
            cursor = connection.execute(
                """INSERT INTO events(
                       type,payload,priority,status,task_id,checkpoint_id,continuation_generation
                   ) VALUES('TASK_CONTINUE',?,?,'pending',?,?,?)""",
                (
                    json.dumps(payload, ensure_ascii=False), priority,
                    task_id, checkpoint_id, generation,
                ),
            )
            connection.execute(
                """UPDATE tasks SET current_checkpoint_id=?,continuation_generation=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (checkpoint_id, generation, task_id),
            )
            return int(cursor.lastrowid), "queued"

    def fence_continuation(self, event: Event) -> tuple[bool, str]:
        task_id = int(event.payload.get("task_id"))
        checkpoint_id = event.payload.get("checkpoint_id")
        generation = event.payload.get("generation")
        terminal = {
            TaskStatus.COMPLETED.value, TaskStatus.FAILED.value,
            TaskStatus.DEAD_LETTER.value, TaskStatus.DEGRADED.value,
            TaskStatus.BLOCKED_CAPABILITY.value, TaskStatus.NEEDS_AUTHORITY.value,
            TaskStatus.TERMINAL_FAILURE.value, TaskStatus.NEEDS_REVIEW.value,
            TaskStatus.STOPPED.value, TaskStatus.YIELDED.value,
            TaskStatus.ABANDONED.value,
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT status,current_checkpoint_id,continuation_generation FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if task is None:
                reason = "unknown_task"
            elif str(task["status"]) in terminal:
                reason = "terminal_task"
            elif checkpoint_id is None or int(checkpoint_id) != int(task["current_checkpoint_id"] or -1):
                reason = "stale_checkpoint"
            elif generation is None or int(generation) != int(task["continuation_generation"] or 0):
                reason = "stale_generation"
            else:
                return True, "current"
            if event.id is not None:
                connection.execute(
                    """UPDATE events SET status='stale',error=?,updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (reason, int(event.id)),
                )
            self._increment_metric(connection, "stale_continuations_discarded")
            return False, reason

    def discard_event(self, event: Event, reason: str) -> None:
        if event.id is None:
            return
        with self.connect() as connection:
            connection.execute(
                """UPDATE events SET status='stale',error=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (reason, int(event.id)),
            )

    def quarantine_task(self, task_id: int, reason: str) -> int:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT result FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(f"Unknown task: {task_id}")
            result = json.loads(task["result"]) if task["result"] else {}
            result["experience_validity"] = {
                "agent_behavior": "invalid_for_learning",
                "runtime_regression": True,
                "reason": reason,
                "cost_metrics": "contaminated",
            }
            cursor = connection.execute(
                """UPDATE events SET status='stale',error=?,updated_at=CURRENT_TIMESTAMP
                   WHERE type='TASK_CONTINUE' AND task_id=?
                     AND status IN ('pending','processing')""",
                (reason, task_id),
            )
            if cursor.rowcount:
                self._increment_metric(
                    connection, "stale_continuations_discarded", int(cursor.rowcount)
                )
            connection.execute(
                """UPDATE tasks SET status=?,result=?,error=?,current_checkpoint_id=NULL,
                   continuation_generation=continuation_generation+1,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    TaskStatus.NEEDS_REVIEW.value,
                    json.dumps(result, ensure_ascii=False, default=str), reason, task_id,
                ),
            )
            return int(cursor.rowcount)

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

    def trace(self, cycle_id: str, kind: str, data: dict[str, Any]) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO traces(cycle_id,kind,data) VALUES(?,?,?)",
                (cycle_id, kind, json.dumps(data, ensure_ascii=False, default=str)),
            )
            return int(cursor.lastrowid)

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

    def add_skill_usage(self, record: dict[str, Any]) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO skill_usage(
                       invocation_id,cycle_id,task_id,model_round,sequence_index,
                       model_calls_before,tokens_before,
                       skill_name,skill_version,status,input_digest,input_keys,
                       required_capabilities,capability_assessment,duration_ms,
                       exit_code,fallback_used
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record["invocation_id"], record["cycle_id"], record.get("task_id"),
                    int(record["model_round"]), int(record["sequence_index"]),
                    int(record.get("model_calls_before", 0)), int(record.get("tokens_before", 0)),
                    record["skill_name"], record["skill_version"], record["status"],
                    record["input_digest"], json.dumps(record.get("input_keys", []), ensure_ascii=False),
                    json.dumps(record.get("required_capabilities", []), ensure_ascii=False),
                    json.dumps(record.get("capability_assessment", {}), ensure_ascii=False, default=str),
                    float(record.get("duration_ms", 0)), record.get("exit_code"),
                    int(bool(record.get("fallback_used", False))),
                ),
            )
            return int(cursor.lastrowid)

    def finalize_skill_usage(
        self,
        cycle_id: str,
        *,
        verifier_passed: bool | None,
        task_outcome: str,
        model_calls_after: int | None = None,
        tokens_after: int | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE skill_usage
                   SET verifier_passed=?,task_outcome=?,model_calls_after=?,tokens_after=?
                   WHERE cycle_id=? AND verifier_passed IS NULL""",
                (None if verifier_passed is None else int(verifier_passed), task_outcome, model_calls_after, tokens_after, cycle_id),
            )

    def list_skill_usage(self, limit: int = 50, skill_name: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM skill_usage"
        params: tuple[Any, ...]
        if skill_name:
            query += " WHERE skill_name=?"
            params = (skill_name, limit)
        else:
            params = (limit,)
        query += " ORDER BY id DESC LIMIT ?"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                "id": row["id"], "invocation_id": row["invocation_id"],
                "cycle_id": row["cycle_id"], "task_id": row["task_id"],
                "model_round": row["model_round"], "sequence_index": row["sequence_index"],
                "model_calls_before": row["model_calls_before"],
                "model_calls_after": row["model_calls_after"],
                "tokens_before": row["tokens_before"], "tokens_after": row["tokens_after"],
                "skill_name": row["skill_name"], "skill_version": row["skill_version"],
                "status": row["status"], "input_digest": row["input_digest"],
                "input_keys": json.loads(row["input_keys"]),
                "required_capabilities": json.loads(row["required_capabilities"]),
                "capability_assessment": json.loads(row["capability_assessment"]),
                "duration_ms": row["duration_ms"], "exit_code": row["exit_code"],
                "fallback_used": bool(row["fallback_used"]),
                "verifier_passed": None if row["verifier_passed"] is None else bool(row["verifier_passed"]),
                "task_outcome": row["task_outcome"], "created_at": row["created_at"],
            }
            for row in rows
        ]

    def add_skill_replay_report(self, report: dict[str, Any]) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO skill_replay_reports(
                       candidate_id,skill_name,skill_version,evidence_level,passed,
                       negative_transfer,utility_delta,report
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    report["candidate_id"], report["skill"], report["version"],
                    report["evidence_level"], int(bool(report["passed"])),
                    int(bool(report.get("negative_transfer", False))),
                    report.get("utility_delta"),
                    json.dumps(report, ensure_ascii=False, default=str),
                ),
            )
            return int(cursor.lastrowid)

    def list_skill_replay_reports(
        self, limit: int = 50, *, candidate_id: str | None = None,
        skill_name: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if candidate_id:
            conditions.append("candidate_id=?")
            params.append(candidate_id)
        if skill_name:
            conditions.append("skill_name=?")
            params.append(skill_name)
        query = "SELECT * FROM skill_replay_reports"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [
            {"id": int(row["id"]), **json.loads(row["report"]), "created_at": row["created_at"]}
            for row in rows
        ]

    def traces_for_cycles(self, cycle_ids: list[str]) -> list[dict[str, Any]]:
        if not cycle_ids:
            return []
        placeholders = ",".join("?" for _ in cycle_ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM traces WHERE cycle_id IN ({placeholders}) ORDER BY id",
                tuple(cycle_ids),
            ).fetchall()
        return [
            {
                "id": int(row["id"]), "cycle_id": str(row["cycle_id"]),
                "kind": str(row["kind"]), "data": json.loads(row["data"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def task_behavior_traces(self, task_id: int, limit: int = 400) -> dict[str, Any]:
        """Read bounded operational fields, never materialize full tool output/context.

        Checkpoints associate all attempts/cycles with a task; lineage_bound also
        covers an in-flight cycle. IN prevents duplicate checkpoint attribution.
        """
        limit = max(1, min(int(limit), 400))
        with self.connect() as connection:
            rows = connection.execute(
                """WITH cycles AS (
                    SELECT json_extract(data, '$.cycle_id') AS cycle_id
                    FROM checkpoints WHERE task_id=?
                    UNION
                    SELECT cycle_id FROM traces WHERE kind='lineage_bound'
                        AND json_extract(data, '$.task_id')=?
                ), sampled AS (
                    SELECT id,cycle_id,kind,data FROM traces
                    WHERE cycle_id IN (SELECT cycle_id FROM cycles WHERE cycle_id IS NOT NULL)
                        AND kind IN ('plan_created','action_result')
                    ORDER BY id LIMIT ?
                )
                SELECT id,cycle_id,kind,
                    json_extract(data, '$.round') AS round,
                    CASE WHEN kind='plan_created' THEN (
                        SELECT json_group_array(json_object(
                            'tool', substr(json_extract(value,'$.tool'),1,64),
                            'arguments', json_object(
                                'path', substr(json_extract(value,'$.arguments.path'),1,384),
                                'command', substr(json_extract(value,'$.arguments.command'),1,4096)),
                            'arguments_truncated',
                                coalesce(length(json_extract(value,'$.arguments.command')),0)>4096
                                OR coalesce(length(json_extract(value,'$.arguments.path')),0)>384
                        )) FROM json_each(data,'$.actions') WHERE CAST(key AS INTEGER)<100
                    ) ELSE NULL END AS actions,
                    CASE WHEN kind='action_result' THEN json_object(
                        'tool', substr(json_extract(data,'$.tool'),1,64),
                        'ok', json_extract(data,'$.ok'),
                        'error', substr(json_extract(data,'$.error'),1,256),
                        'output', json_object(
                            'exit_code', json_extract(data,'$.output.exit_code'),
                            'kind', substr(json_extract(data,'$.output.kind'),1,64),
                            'observation_cache', json_object(
                                'hit',json_extract(data,'$.output.observation_cache.hit')))
                    ) ELSE NULL END AS result
                FROM sampled ORDER BY id""",
                (int(task_id), int(task_id), limit + 1),
            ).fetchall()
        traces = []
        for row in rows[:limit]:
            data = json.loads(row['result']) if row['result'] else {}
            data['round'] = row['round']
            if row['actions'] is not None:
                data['actions'] = json.loads(row['actions'])
            traces.append({
                'id': int(row['id']), 'cycle_id': str(row['cycle_id']),
                'kind': str(row['kind']), 'data': data,
            })
        return {'traces': traces, 'truncated': len(rows) > limit, 'limit': limit}

    def upsert_component(self, manifest: dict[str, Any]) -> str:
        component_id = str(manifest["component_id"])
        version_id = str(manifest["version_id"])
        metadata = manifest["metadata"]
        version = str(metadata["version"])
        payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT version_id,manifest_json FROM component_versions WHERE component_id=? AND version=?",
                (component_id, version),
            ).fetchone()
            if existing is not None and str(existing["version_id"]) != version_id:
                previous = json.loads(existing["manifest_json"])
                if previous.get("manifest_schema") == "component/v1.1":
                    raise ValueError("A Component version is immutable once registered")
                # One-time migration from the pre-v1.1 manifest shape. Once migrated,
                # normal same-version immutability is enforced again.
                connection.execute(
                    "DELETE FROM component_capabilities WHERE component_id=? AND version=?",
                    (component_id, version),
                )
                connection.execute(
                    "DELETE FROM component_versions WHERE component_id=? AND version=?",
                    (component_id, version),
                )
            connection.execute(
                """INSERT INTO components(component_id,kind,name,status,active_version,trust_class,metadata)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(component_id) DO UPDATE SET
                     status=excluded.status,active_version=excluded.active_version,
                     trust_class=excluded.trust_class,metadata=excluded.metadata,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    component_id, manifest["kind"], metadata["name"], metadata["status"],
                    version, manifest["trust_policy"]["trust_class"],
                    json.dumps(metadata, ensure_ascii=False, default=str),
                ),
            )
            connection.execute(
                """INSERT OR IGNORE INTO component_versions(
                       version_id,component_id,version,manifest_json,content_digest
                   ) VALUES(?,?,?,?,?)""",
                (version_id, component_id, version, payload, manifest["content_digest"]),
            )
            connection.execute(
                "DELETE FROM component_capabilities WHERE component_id=? AND version=?",
                (component_id, version),
            )
            for direction in ("requires", "provides"):
                connection.executemany(
                    """INSERT INTO component_capabilities(component_id,version,direction,capability)
                       VALUES(?,?,?,?)""",
                    [
                        (component_id, version, direction, capability)
                        for capability in manifest["capabilities"][direction]
                    ],
                )
        return component_id

    def get_component(self, component_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT v.manifest_json,c.status FROM components c
                   JOIN component_versions v ON v.component_id=c.component_id AND v.version=c.active_version
                   WHERE c.component_id=?""",
                (component_id,),
            ).fetchone()
        if row is None:
            return None
        result = json.loads(row["manifest_json"])
        result["metadata"] = {**result["metadata"], "status": str(row["status"])}
        return result

    def list_components(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        query = """SELECT v.manifest_json,c.status FROM components c
                   JOIN component_versions v ON v.component_id=c.component_id AND v.version=c.active_version"""
        params: tuple[Any, ...] = ()
        if kind is not None:
            query += " WHERE c.kind=?"
            params = (kind,)
        query += " ORDER BY c.kind,c.name"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        result = []
        for row in rows:
            manifest = json.loads(row["manifest_json"])
            manifest["metadata"] = {**manifest["metadata"], "status": str(row["status"])}
            result.append(manifest)
        return result

    def update_component_status(self, component_id: str, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE components SET status=?,updated_at=CURRENT_TIMESTAMP WHERE component_id=?",
                (status, component_id),
            )

    def list_component_versions(self, component_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT manifest_json FROM component_versions WHERE component_id=? ORDER BY created_at,version",
                (component_id,),
            ).fetchall()
        return [json.loads(row["manifest_json"]) for row in rows]

    def add_capability_implication(self, stronger: str, weaker: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO capability_implications(stronger,weaker) VALUES(?,?)",
                (stronger, weaker),
            )

    def list_capability_implications(self) -> list[tuple[str, str]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT stronger,weaker FROM capability_implications ORDER BY stronger,weaker"
            ).fetchall()
        return [(str(row["stronger"]), str(row["weaker"])) for row in rows]

    def add_task_capsule(self, manifest: dict[str, Any]) -> str:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO task_capsules(
                       capsule_id,source_task_id,status,fidelity,manifest
                   ) VALUES(?,?,?,?,?)""",
                (
                    manifest["capsule_id"], int(manifest["source_task_id"]), manifest["status"],
                    manifest["fidelity"], json.dumps(manifest, ensure_ascii=False, default=str),
                ),
            )
        return str(manifest["capsule_id"])

    def get_task_capsule(self, capsule_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_capsules WHERE capsule_id=?", (capsule_id,)
            ).fetchone()
        if row is None:
            return None
        manifest = json.loads(row["manifest"])
        manifest["status"] = str(row["status"])
        manifest["fidelity"] = str(row["fidelity"])
        manifest["created_at"] = str(row["created_at"])
        return manifest

    def list_task_capsules(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT capsule_id FROM task_capsules ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self.get_task_capsule(str(row["capsule_id"])) for row in rows]

    def update_task_capsule_status(self, capsule_id: str, status: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE task_capsules SET status=?,updated_at=CURRENT_TIMESTAMP WHERE capsule_id=?",
                (status, capsule_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown capsule: {capsule_id}")

    def register_capsule_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO capsule_objects(object_hash,kind,size_bytes,metadata)
                   VALUES(?,?,?,?)""",
                (
                    snapshot["manifest_hash"], snapshot.get("kind", "content_addressed_tree_v1"),
                    int(snapshot.get("total_bytes", 0)),
                    json.dumps(snapshot, ensure_ascii=False, default=str),
                ),
            )

    def create_experiment(self, spec: dict[str, Any]) -> str:
        variants = spec.get("variants") or []
        mutation_type = variants[0].get("mutation_type", "skill") if variants else "skill"
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO experiments(
                       experiment_id,capsule_id,mutation_type,status,spec
                   ) VALUES(?,?,?,'running',?)""",
                (
                    spec["experiment_id"], spec["capsule_id"], mutation_type,
                    json.dumps(spec, ensure_ascii=False, default=str),
                ),
            )
        return str(spec["experiment_id"])

    def add_experiment_variant(self, experiment_id: str, variant: dict[str, Any]) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO experiment_variants(experiment_id,name,mutation) VALUES(?,?,?)",
                (
                    experiment_id, variant["name"],
                    json.dumps(variant, ensure_ascii=False, default=str),
                ),
            )
            return int(cursor.lastrowid)

    def add_experiment_run(self, experiment_id: str, evidence: dict[str, Any]) -> str:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO experiment_runs(
                       run_id,experiment_id,variant,replicate,evidence
                   ) VALUES(?,?,?,?,?)""",
                (
                    evidence["run_id"], experiment_id, evidence["variant"], int(evidence["replicate"]),
                    json.dumps(evidence, ensure_ascii=False, default=str),
                ),
            )
        return str(evidence["run_id"])

    def add_semantic_judgement(self, experiment_id: str, judgement: dict[str, Any]) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO semantic_judgements(experiment_id,judgement) VALUES(?,?)",
                (experiment_id, json.dumps(judgement, ensure_ascii=False, default=str)),
            )
            return int(cursor.lastrowid)

    def add_counterfactual_report(self, experiment_id: str, report: dict[str, Any]) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO counterfactual_reports(experiment_id,promotion_state,report)
                   VALUES(?,?,?)""",
                (
                    experiment_id, report["promotion_state"],
                    json.dumps(report, ensure_ascii=False, default=str),
                ),
            )
            return int(cursor.lastrowid)

    def update_experiment(self, experiment_id: str, status: str, report: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE experiments SET status=?,report=?,updated_at=CURRENT_TIMESTAMP
                   WHERE experiment_id=?""",
                (status, json.dumps(report, ensure_ascii=False, default=str), experiment_id),
            )

    def get_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,)
            ).fetchone()
            if row is None:
                return None
            variants = connection.execute(
                "SELECT mutation FROM experiment_variants WHERE experiment_id=? ORDER BY id", (experiment_id,)
            ).fetchall()
            runs = connection.execute(
                "SELECT evidence FROM experiment_runs WHERE experiment_id=? ORDER BY variant,replicate",
                (experiment_id,),
            ).fetchall()
        return {
            "experiment_id": str(row["experiment_id"]), "capsule_id": str(row["capsule_id"]),
            "mutation_type": str(row["mutation_type"]), "status": str(row["status"]),
            "spec": json.loads(row["spec"]),
            "report": json.loads(row["report"]) if row["report"] else None,
            "variants": [json.loads(item["mutation"]) for item in variants],
            "runs": [json.loads(item["evidence"]) for item in runs],
            "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"]),
        }

    def latest_counterfactual_report(self, candidate_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT report FROM counterfactual_reports ORDER BY id DESC"
            ).fetchall()
        for row in rows:
            report = json.loads(row["report"])
            variants = report.get("variants", {})
            candidate_runs = variants.get("candidate", []) if isinstance(variants, dict) else []
            if any(
                run.get("skills", {}).get("candidate_id") == candidate_id
                or candidate_id in run.get("skills", {}).get("variant_skills", [])
                for run in candidate_runs
            ):
                return report
            if report.get("candidate_id") == candidate_id:
                return report
        return None

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

    def start_task_attempt(self, task_id: int, *, increment_attempt: bool = True) -> Task:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if increment_attempt:
                connection.execute(
                    """UPDATE tasks SET status=?,attempts=attempts+1,result=NULL,error=NULL,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (TaskStatus.RUNNING.value, task_id),
                )
            else:
                connection.execute(
                    """UPDATE tasks SET status=?,error=NULL,updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
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
        terminal = status in {
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.DEAD_LETTER,
            TaskStatus.DEGRADED, TaskStatus.BLOCKED_CAPABILITY,
            TaskStatus.NEEDS_AUTHORITY, TaskStatus.TERMINAL_FAILURE,
            TaskStatus.NEEDS_REVIEW, TaskStatus.STOPPED,
            TaskStatus.YIELDED, TaskStatus.ABANDONED,
        }
        with self.connect() as connection:
            connection.execute(
                """UPDATE tasks SET status=?,result=?,error=?,
                   current_checkpoint_id=CASE WHEN ? THEN NULL ELSE current_checkpoint_id END,
                   continuation_generation=continuation_generation+CASE WHEN ? THEN 1 ELSE 0 END,
                   updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    status.value,
                    json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                    error,
                    int(terminal), int(terminal),
                    task_id,
                ),
            )
            if terminal:
                continuation_row = connection.execute(
                    """SELECT COUNT(*) AS n FROM events WHERE type='TASK_CONTINUE'
                       AND task_id=? AND status IN ('pending','processing')""",
                    (task_id,),
                ).fetchone()
                connection.execute(
                    """UPDATE events SET status='stale',error='terminal_task',
                       updated_at=CURRENT_TIMESTAMP WHERE task_id=?
                       AND status IN ('pending','processing')""",
                    (task_id,),
                )
                stale_continuations = int(continuation_row["n"])
                if stale_continuations:
                    self._increment_metric(
                        connection, "stale_continuations_discarded", stale_continuations
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
                """UPDATE events SET status='stale',error='manual_retry_reset',
                   updated_at=CURRENT_TIMESTAMP WHERE type='TASK_CONTINUE' AND task_id=?
                   AND status IN ('pending','processing')""",
                (task_id,),
            )
            connection.execute(
                """UPDATE tasks SET status=?,attempts=0,result=NULL,error=NULL,
                   current_checkpoint_id=NULL,
                   continuation_generation=continuation_generation+1,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (TaskStatus.QUEUED.value, task_id),
            )
        self.add_checkpoint(task_id, "retry_reset", {"previous_status": task.status.value})
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

    def create_lineage(self, lineage: dict[str, Any]) -> str:
        lineage_id = str(lineage["lineage_id"])
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO evolution_lineages(
                       lineage_id,parent_lineage_id,generation,kind,status,settings,component_set,
                       mutation,source_candidate_id,source_component_candidate_id,created_by,decision
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lineage_id, lineage.get("parent_lineage_id"), int(lineage["generation"]),
                    str(lineage.get("kind", "system")), str(lineage.get("status", "living")),
                    json.dumps(lineage.get("settings", {}), ensure_ascii=False),
                    json.dumps(lineage.get("component_set", {}), ensure_ascii=False),
                    json.dumps(lineage.get("mutation", {}), ensure_ascii=False),
                    lineage.get("source_candidate_id"), lineage.get("source_component_candidate_id"),
                    str(lineage.get("created_by", "agent")),
                    json.dumps(lineage.get("decision", {}), ensure_ascii=False, default=str),
                ),
            )
        return lineage_id

    def update_lineage_component_set(
        self, lineage_id: str, component_set: dict[str, Any], *, kind: str = "system",
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE evolution_lineages
                   SET component_set=?,kind=?,updated_at=CURRENT_TIMESTAMP
                   WHERE lineage_id=?""",
                (json.dumps(component_set, ensure_ascii=False), kind, lineage_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown lineage: {lineage_id}")

    def get_lineage(self, lineage_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM evolution_lineages WHERE lineage_id=?", (lineage_id,)
            ).fetchone()
        return self._lineage_from_row(row) if row else None

    def list_lineages(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM evolution_lineages ORDER BY generation,created_at,lineage_id"
            ).fetchall()
        return [self._lineage_from_row(row) for row in rows]

    def add_lineage_event(
        self, lineage_id: str, action: str, actor: str, data: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO lineage_events(lineage_id,action,actor,data) VALUES(?,?,?,?)",
                (lineage_id, action, actor, json.dumps(data or {}, ensure_ascii=False, default=str)),
            )
            return int(cursor.lastrowid)

    def list_lineage_events(self, lineage_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM lineage_events WHERE lineage_id=? ORDER BY id DESC LIMIT ?",
                (lineage_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [{
            "id": int(row["id"]), "lineage_id": str(row["lineage_id"]),
            "action": str(row["action"]), "actor": str(row["actor"]),
            "data": json.loads(row["data"]), "created_at": str(row["created_at"]),
        } for row in rows]

    def bind_task_lineage(self, task_id: int, lineage_id: str) -> None:
        if self.get_task(task_id) is None:
            raise KeyError(f"Unknown task: {task_id}")
        if self.get_lineage(lineage_id) is None:
            raise KeyError(f"Unknown lineage: {lineage_id}")
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO task_lineage_bindings(task_id,lineage_id) VALUES(?,?)",
                (task_id, lineage_id),
            )

    def task_lineage(self, task_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT l.* FROM evolution_lineages l
                   JOIN task_lineage_bindings b ON b.lineage_id=l.lineage_id
                   WHERE b.task_id=?""",
                (task_id,),
            ).fetchone()
        return self._lineage_from_row(row) if row else None

    def lineage_tasks(self, lineage_id: str, limit: int = 50) -> list[Task]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT t.* FROM tasks t JOIN task_lineage_bindings b ON b.task_id=t.id
                   WHERE b.lineage_id=? ORDER BY t.id DESC LIMIT ?""",
                (lineage_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

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
    def _lineage_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "lineage_id": str(row["lineage_id"]),
            "parent_lineage_id": (
                str(row["parent_lineage_id"]) if row["parent_lineage_id"] else None
            ),
            "generation": int(row["generation"]), "kind": str(row["kind"]),
            "status": str(row["status"]), "settings": json.loads(row["settings"]),
            "component_set": json.loads(row["component_set"] or "{}"),
            "mutation": json.loads(row["mutation"]),
            "source_candidate_id": row["source_candidate_id"],
            "source_component_candidate_id": row["source_component_candidate_id"],
            "created_by": str(row["created_by"]), "decision": json.loads(row["decision"]),
            "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"]),
        }

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
