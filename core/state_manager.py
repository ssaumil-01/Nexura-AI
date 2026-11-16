"""The ONLY writer of persistent memory (SQLite). Agents never touch this. Writes are validated through pydantic models."""
import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from core.schemas import FileChange, TaskRecord

SCHEMA_VERSION = 2

MIGRATIONS = {
    1: """
    CREATE TABLE projects (id TEXT PRIMARY KEY, path TEXT NOT NULL, created_at REAL NOT NULL);
    CREATE TABLE runs (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,
        approval_level TEXT, plan_json TEXT, error TEXT, created_at REAL NOT NULL, finished_at REAL);
    CREATE TABLE tasks (
        run_id TEXT NOT NULL, task_id INTEGER NOT NULL, title TEXT NOT NULL, description TEXT, dependencies TEXT,
        acceptance TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, reason TEXT,
        started_at REAL, finished_at REAL, PRIMARY KEY (run_id, task_id));
    CREATE TABLE file_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, task_id INTEGER NOT NULL, file TEXT NOT NULL,
        change_type TEXT NOT NULL, sha256_before TEXT, sha256_after TEXT, ts REAL NOT NULL, approval_source TEXT);
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, task_id INTEGER, ts REAL NOT NULL,
        type TEXT NOT NULL, data TEXT);
    CREATE INDEX idx_events_run ON events(run_id, id);
    CREATE INDEX idx_changes_run ON file_changes(run_id, task_id);
    """,
    2: """
    CREATE TABLE locks (workspace TEXT PRIMARY KEY, run_id TEXT NOT NULL, pid INTEGER NOT NULL, ts REAL NOT NULL);
    """,
}


class WorkspaceBusy(RuntimeError):
    """Another run is active on this workspace."""

RUN_STATUSES = ("running", "completed", "completed_with_failures", "failed", "cancelled")


class RunRow(BaseModel):
    id: str
    project_id: str
    prompt: str
    status: str
    approval_level: Optional[str] = None
    plan: Optional[dict] = None
    error: Optional[str] = None
    created_at: float
    finished_at: Optional[float] = None


class Event(BaseModel):
    run_id: str
    task_id: Optional[int] = None
    type: str
    data: dict = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)


class StateManager:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._migrate()

    # ── plumbing ──
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock, closing(self._conn()) as c:
            cur = c.execute(sql, args)
            c.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with closing(self._conn()) as c:
            return c.execute(sql, args).fetchall()

    def _migrate(self) -> None:
        with self._lock, closing(self._conn()) as c:
            c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = c.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = row[0] or 0
            for v in range(current + 1, SCHEMA_VERSION + 1):
                c.executescript(MIGRATIONS[v])
                c.execute("INSERT INTO schema_version (version) VALUES (?)", (v,))
            c.commit()

    def schema_version(self) -> int:
        return self._all("SELECT MAX(version) AS v FROM schema_version")[0]["v"]

    # ── projects / runs ──
    def upsert_project(self, project_id: str, path: str) -> None:
        self._exec("INSERT OR IGNORE INTO projects (id, path, created_at) VALUES (?,?,?)", (project_id, str(path), time.time()))

    def create_run(self, run_id: str, project_id: str, prompt: str, approval_level: str | None = None) -> None:
        self._exec("INSERT INTO runs (id, project_id, prompt, status, approval_level, created_at) VALUES (?,?,?,?,?,?)",
                   (run_id, project_id, prompt, "running", approval_level, time.time()))

    def update_run(self, run_id: str, status: str | None = None, error: str | None = None) -> None:
        if status is not None and status not in RUN_STATUSES:
            raise ValueError(f"bad run status {status!r}")
        done = status in ("completed", "completed_with_failures", "failed", "cancelled")
        if done:
            self._exec("DELETE FROM locks WHERE run_id=?", (run_id,))
        self._exec("UPDATE runs SET status=COALESCE(?, status), error=COALESCE(?, error), "
                   "finished_at=CASE WHEN ? THEN ? ELSE finished_at END WHERE id=?",
                   (status, error, 1 if done else 0, time.time(), run_id))

    def save_plan(self, run_id: str, plan: dict, tasks: list[TaskRecord]) -> None:
        """Store the plan and (re)create the task rows. A re-plan replaces the previous tasks."""
        self._exec("UPDATE runs SET plan_json=? WHERE id=?", (json.dumps(plan), run_id))
        self._exec("DELETE FROM tasks WHERE run_id=?", (run_id,))
        for t in tasks:
            self._exec("INSERT INTO tasks (run_id, task_id, title, description, dependencies, acceptance, status) VALUES (?,?,?,?,?,?,?)",
                       (run_id, t.task_id, t.title, t.description, json.dumps(t.dependencies),
                        json.dumps([a.model_dump(exclude_none=True) for a in t.acceptance]), "pending"))

    def get_run(self, run_id: str) -> RunRow | None:
        rows = self._all("SELECT * FROM runs WHERE id=?", (run_id,))
        if not rows:
            return None
        r = dict(rows[0])
        plan = r.pop("plan_json")
        return RunRow(plan=json.loads(plan) if plan else None, **r)

    def project_path(self, project_id: str) -> str | None:
        rows = self._all("SELECT path FROM projects WHERE id=?", (project_id,))
        return rows[0]["path"] if rows else None

    # ── tasks ──
    def mark_task(self, run_id: str, task_id: int, status: str, attempts: int | None = None, reason: str | None = None) -> None:
        now = time.time()
        self._exec(
            "UPDATE tasks SET status=?, attempts=COALESCE(?, attempts), reason=?, "
            "started_at=CASE WHEN ?='running' THEN ? ELSE started_at END, "
            "finished_at=CASE WHEN ? IN ('done','failed','skipped') THEN ? ELSE finished_at END "
            "WHERE run_id=? AND task_id=?", (status, attempts, reason, status, now, status, now, run_id, task_id))

    def list_tasks(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM tasks WHERE run_id=? ORDER BY task_id", (run_id,))]

    # ── file changes ──
    def add_file_change(self, run_id: str, fc: FileChange, approval_source: str = "auto") -> None:
        self._exec("INSERT INTO file_changes (run_id, task_id, file, change_type, sha256_before, sha256_after, ts, approval_source) "
                   "VALUES (?,?,?,?,?,?,?,?)", (run_id, fc.task_id, fc.file, fc.change_type, fc.sha256_before, fc.sha256_after,
                                                fc.timestamp, approval_source))

    def list_file_changes(self, run_id: str, task_id: int | None = None) -> list[dict]:
        if task_id is None:
            rows = self._all("SELECT * FROM file_changes WHERE run_id=? ORDER BY id", (run_id,))
        else:
            rows = self._all("SELECT * FROM file_changes WHERE run_id=? AND task_id=? ORDER BY id", (run_id, task_id))
        return [dict(r) for r in rows]

    # ── events ──
    def add_event(self, run_id: str, type: str, task_id: int | None = None, **data: Any) -> None:
        e = Event(run_id=run_id, task_id=task_id, type=type, data=data)
        self._exec("INSERT INTO events (run_id, task_id, ts, type, data) VALUES (?,?,?,?,?)",
                   (e.run_id, e.task_id, e.ts, e.type, json.dumps(e.data, default=str)))

    def list_events(self, run_id: str, after_id: int = 0) -> list[dict]:
        out = []
        for r in self._all("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after_id)):
            d = dict(r)
            d["data"] = json.loads(d["data"] or "{}")
            out.append(d)
        return out


def make_checkpointer(path: Path | str):
    """Durable LangGraph checkpointer (separate DB file from the state DB)."""
    from langgraph.checkpoint.sqlite import SqliteSaver
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def _reopen(self, run_id: str) -> None:
    self._exec("UPDATE runs SET status='running', error=NULL, finished_at=NULL WHERE id=?", (run_id,))


StateManager.reopen_run = _reopen


def _norm(workspace) -> str:
    import os
    return os.path.normcase(os.path.realpath(str(workspace)))


def _acquire(self, workspace, run_id: str) -> None:
    """One active run per workspace. A lock whose process is gone (crash) is stale and is taken over."""
    import os
    import psutil
    key = _norm(workspace)
    with self._lock, closing(self._conn()) as c:
        row = c.execute("SELECT run_id, pid FROM locks WHERE workspace=?", (key,)).fetchone()
        if row and row["run_id"] != run_id and row["pid"] != os.getpid() and psutil.pid_exists(row["pid"]):
            raise WorkspaceBusy(f"Run {row['run_id']} is already active on this workspace (pid {row['pid']}).")
        if row and row["run_id"] != run_id and row["pid"] == os.getpid():
            st = c.execute("SELECT status FROM runs WHERE id=?", (row["run_id"],)).fetchone()
            if st and st["status"] == "running":
                raise WorkspaceBusy(f"Run {row['run_id']} is already active on this workspace.")
        c.execute("INSERT OR REPLACE INTO locks (workspace, run_id, pid, ts) VALUES (?,?,?,?)",
                  (key, run_id, os.getpid(), time.time()))
        c.commit()


def _release(self, run_id: str) -> None:
    self._exec("DELETE FROM locks WHERE run_id=?", (run_id,))


StateManager.acquire_lock = _acquire
StateManager.release_lock = _release
