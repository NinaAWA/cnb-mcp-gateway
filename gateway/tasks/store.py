"""后台长任务的 SQLite WAL 持久化（仅标准库）。

与审计库共用同一个 DB 文件，但使用独立的 `mcp_tasks` 表，避免改动既有
`background_tasks` 生命周期事件表的语义。日志仍按 task_id 落在
MCP_TASK_LOG_DIR（持久卷），因此服务重启后旧日志随时可读。

核心不变量：重启时无法确认仍存活的 running 任务一律标为 `unknown`，
exit_code 保持 NULL；绝不臆测为 done / success。确认存活用 pid +
/proc/<pid>/stat 的 starttime 双重比对，防止 pid 复用误判。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_tasks (
  task_id TEXT PRIMARY KEY,
  command TEXT NOT NULL,
  cwd TEXT,
  status TEXT NOT NULL,
  exit_code INTEGER,
  log_path TEXT,
  started_at REAL NOT NULL,
  finished_at REAL,
  pid INTEGER,
  proc_start TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_mcp_tasks_status ON mcp_tasks(status, started_at);
"""

TERMINAL = frozenset({"done", "killed", "timeout", "unknown"})


@dataclass
class TaskRecord:
    task_id: str
    command: str
    status: str
    log_path: str
    started_at: float
    cwd: str = ""
    exit_code: int | None = None
    finished_at: float | None = None
    pid: int | None = None
    proc_start: str = ""
    detail: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "command": self.command, "cwd": self.cwd,
            "status": self.status, "exit_code": self.exit_code,
            "log_path": self.log_path, "started_at": self.started_at,
            "finished_at": self.finished_at, "pid": self.pid,
            "proc_start": self.proc_start, "detail": self.detail,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> TaskRecord:
        return cls(
            task_id=str(row["task_id"]), command=str(row["command"] or ""),
            cwd=str(row["cwd"] or ""), status=str(row["status"]),
            exit_code=row["exit_code"], log_path=str(row["log_path"] or ""),
            started_at=float(row["started_at"] or 0),
            finished_at=row["finished_at"], pid=row["pid"],
            proc_start=str(row["proc_start"] or ""),
            detail=str(row["detail"] or ""),
        )


def pid_start_time(pid: int | None) -> str:
    """读取 /proc/<pid>/stat 第 22 字段 starttime；不可读时返回空串。"""
    if not pid or pid <= 0:
        return ""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        fields = raw.rsplit(")", 1)[1].split()
        return fields[19] if len(fields) > 19 else ""
    except (OSError, IndexError, ValueError):
        return ""


def pid_is_alive(pid: int | None, proc_start: str) -> bool:
    """只有 pid 存在且 /proc 启动时间完全一致才认定是同一个进程。"""
    if not proc_start:
        return False
    return pid_start_time(pid) == proc_start


class TaskStore:
    """线程安全的任务表；写失败不向调用方抛异常（任务本身优先）。"""

    def __init__(self, db_path: str = ""):
        self.db_path = str(db_path or os.environ.get(
            "AUDIT_DB_PATH", "/data/audit/audit.db"))
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init(self) -> None:
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                conn = self._connect()
                try:
                    conn.executescript(_SCHEMA)
                    conn.commit()
                finally:
                    conn.close()
        except (OSError, sqlite3.Error):
            pass

    def create(self, rec: TaskRecord) -> bool:
        row = rec.to_row()
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        return self._write(
            f"INSERT OR REPLACE INTO mcp_tasks ({cols}) VALUES ({marks})",
            list(row.values()))

    def _write(self, sql: str, params: list[Any]) -> bool:
        try:
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute(sql, tuple(params))
                    conn.commit()
                finally:
                    conn.close()
            return True
        except (OSError, sqlite3.Error):
            return False

    def update_status(self, task_id: str, status: str,
                      exit_code: int | None = None, detail: str = "") -> bool:
        finished = time.time() if status in TERMINAL else None
        return self._write(
            "UPDATE mcp_tasks SET status=?, exit_code=COALESCE(?, exit_code), "
            "finished_at=COALESCE(?, finished_at), detail=? WHERE task_id=?",
            [status, exit_code, finished, detail[:1000], task_id])

    def get(self, task_id: str) -> TaskRecord | None:
        rows = self._query("SELECT * FROM mcp_tasks WHERE task_id=?", [task_id])
        return TaskRecord.from_row(rows[0]) if rows else None

    def list_recent(self, limit: int = 100) -> list[TaskRecord]:
        rows = self._query("SELECT * FROM mcp_tasks ORDER BY started_at DESC LIMIT ?",
                           [min(max(1, limit), 500)])
        return [TaskRecord.from_row(row) for row in rows]

    def _query(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        try:
            with self._lock:
                conn = self._connect()
                try:
                    return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
                finally:
                    conn.close()
        except (OSError, sqlite3.Error):
            return []

    def mark_orphans_unknown(self) -> int:
        """启动时调用：running 且进程无法确认存活 -> unknown（exit_code 保持 NULL）。"""
        rows = self._query("SELECT * FROM mcp_tasks WHERE status='running'", [])
        changed = 0
        for row in rows:
            if pid_is_alive(row["pid"], str(row["proc_start"] or "")):
                # 进程仍在（例如同机多 worker 共享 DB），但本进程无法接管其 Popen：
                # 仍标 unknown，因为无法观测到它的真实退出码。
                pass
            if self._write(
                    "UPDATE mcp_tasks SET status='unknown', exit_code=NULL, "
                    "finished_at=?, detail=? WHERE task_id=? AND status='running'",
                    [time.time(), "服务重启：无法确认进程存活与真实退出码，状态未知",
                     row["task_id"]]):
                changed += 1
        return changed


_STORE: TaskStore | None = None
_STORE_LOCK = threading.Lock()


def get_task_store() -> TaskStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = TaskStore()
    return _STORE
