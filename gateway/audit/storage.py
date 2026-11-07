"""审计持久化：SQLite WAL（默认）或 PostgreSQL（可选）。

- 写库失败不影响工具执行：进入有界内存缓冲并标记降级；
  AUDIT_STRICT=1 时，高风险工具的调用在无法可靠写入时按配置拒绝。
- 所有查询走索引；大输出只保存引用、大小与 SHA-256。
- 提供保留策略、按时间/大小归档、备份恢复与启动时的未终态收敛。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .events import TERMINAL_STATUSES, ToolCallRecord, utcnow
from .redaction import ArtifactStore

DEFAULT_DB_PATH = os.environ.get("AUDIT_DB_PATH", "/data/audit/audit.db")
MAX_FIELD_CHARS = int(os.environ.get("AUDIT_MAX_FIELD_CHARS", "32768"))
MAX_BUFFER = int(os.environ.get("AUDIT_BUFFER_MAX", "1000"))
STRICT = os.environ.get("AUDIT_STRICT", "0").strip().lower() in ("1", "true", "yes", "on")

# 需要严格审计的高风险工具（strict 模式下写库失败即拒绝）
HIGH_RISK_TOOLS = frozenset({
    "run_command", "run_command_background", "write_file", "kill_process",
    "stop_task", "read_web_page",
})

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL UNIQUE,
  trace_id TEXT NOT NULL,
  timestamp_start TEXT NOT NULL,
  timestamp_end TEXT,
  duration_ms INTEGER DEFAULT 0,
  client_id TEXT, actor_id TEXT, declared_client TEXT, session_id TEXT,
  remote_ip TEXT, source TEXT,
  tool_name TEXT NOT NULL,
  arguments TEXT, result TEXT,
  status TEXT NOT NULL,
  error_type TEXT, error_message TEXT, exit_code INTEGER,
  task_id TEXT, approval_id TEXT,
  git_repo TEXT, experiment_id TEXT, file_path TEXT,
  result_ref TEXT, result_size INTEGER DEFAULT 0, result_sha256 TEXT,
  strict_denied INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, kind TEXT NOT NULL, status TEXT,
  request_id TEXT, trace_id TEXT, tool_name TEXT,
  actor_id TEXT, client_id TEXT, source TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS client_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT UNIQUE, client_id TEXT, declared_client TEXT,
  first_seen TEXT, last_seen TEXT, call_count INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active', token_expires TEXT
);
CREATE TABLE IF NOT EXISTS background_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT, request_id TEXT, trace_id TEXT, ts TEXT NOT NULL,
  status TEXT, command TEXT, cwd TEXT, pid INTEGER, log_path TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS git_operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT, trace_id TEXT, ts TEXT NOT NULL,
  repo_path TEXT, repo_name TEXT, branch TEXT, operation TEXT,
  files_changed INTEGER, commit_sha TEXT, commit_message TEXT,
  push_remote TEXT, push_branch TEXT, client_id TEXT,
  status TEXT, error_message TEXT
);
CREATE TABLE IF NOT EXISTS experiment_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT, task_id TEXT, request_id TEXT, trace_id TEXT,
  ts TEXT NOT NULL, name TEXT, project_path TEXT, command TEXT,
  status TEXT, pid INTEGER, git_commit TEXT, log_path TEXT,
  output_path TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS approval_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  approval_id TEXT, request_id TEXT, ts TEXT NOT NULL,
  action TEXT, actor_id TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS admin_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT UNIQUE, username TEXT, created_at TEXT, expires_at TEXT,
  last_seen TEXT, remote_ip TEXT, user_agent TEXT, revoked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS login_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, username TEXT, remote_ip TEXT, outcome TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS export_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT UNIQUE, ts TEXT NOT NULL, fmt TEXT, filters TEXT,
  status TEXT, row_count INTEGER DEFAULT 0, file_path TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS admin_settings (
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_calls_ts ON tool_calls(timestamp_start);
CREATE INDEX IF NOT EXISTS ix_calls_tool ON tool_calls(tool_name, timestamp_start);
CREATE INDEX IF NOT EXISTS ix_calls_status ON tool_calls(status, timestamp_start);
CREATE INDEX IF NOT EXISTS ix_calls_client ON tool_calls(client_id, timestamp_start);
CREATE INDEX IF NOT EXISTS ix_calls_actor ON tool_calls(actor_id, timestamp_start);
CREATE INDEX IF NOT EXISTS ix_calls_task ON tool_calls(task_id);
CREATE INDEX IF NOT EXISTS ix_calls_trace ON tool_calls(trace_id);
CREATE INDEX IF NOT EXISTS ix_calls_repo ON tool_calls(git_repo);
CREATE INDEX IF NOT EXISTS ix_calls_exp ON tool_calls(experiment_id);
CREATE INDEX IF NOT EXISTS ix_calls_file ON tool_calls(file_path);
CREATE INDEX IF NOT EXISTS ix_events_ts ON audit_events(ts);
CREATE INDEX IF NOT EXISTS ix_events_req ON audit_events(request_id);
CREATE INDEX IF NOT EXISTS ix_git_repo ON git_operations(repo_path, ts);
CREATE INDEX IF NOT EXISTS ix_exp_task ON experiment_runs(task_id, ts);
CREATE INDEX IF NOT EXISTS ix_tasks_task ON background_tasks(task_id, ts);
"""

_PG_SCHEMA = _SQLITE_SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")


def _to_pg(sql: str) -> str:
    return sql.replace("?", "%s")


class AuditStorage:
    """线程安全的审计存储；所有写操作都不得向调用方抛异常。"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, dsn: str = ""):
        self.dsn = dsn.strip()
        self.pg = bool(self.dsn)
        self.db_path = str(db_path)
        self.artifacts = ArtifactStore(
            str(Path(self.db_path).parent / "artifacts"), threshold=MAX_FIELD_CHARS
        )
        self._lock = threading.RLock()
        self._buffer: deque[dict[str, Any]] = deque(maxlen=MAX_BUFFER)
        self._healthy = True
        self._last_error = ""
        self._init_db()

    # ---------------------------------------------------------------- backend
    def _connect(self) -> Any:
        if self.pg:
            import psycopg  # 可选依赖，仅在 AUDIT_DB_DSN 配置时加载
            return psycopg.connect(self.dsn, autocommit=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        if not self.pg:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock:
                conn = self._connect()
                try:
                    schema = _PG_SCHEMA if self.pg else _SQLITE_SCHEMA
                    if self.pg:
                        with conn.cursor() as cur:
                            cur.execute(schema)
                    else:
                        conn.executescript(schema)
                        conn.commit()
                finally:
                    conn.close()
            self._healthy = True
        except Exception as exc:  # noqa: BLE001 - 存储故障不得拖垮网关
            self._healthy = False
            self._last_error = repr(exc)

    def health(self) -> dict[str, Any]:
        return {
            "healthy": self._healthy,
            "backend": "postgresql" if self.pg else "sqlite-wal",
            "path": "" if self.pg else self.db_path,
            "buffered": len(self._buffer),
            "last_error": self._last_error,
            "strict": STRICT,
        }

    def available(self) -> bool:
        return self._healthy or len(self._buffer) < MAX_BUFFER

    def _execute(self, sql: str, params: Iterable[Any] = (), *, many: bool = False) -> bool:
        """执行写操作；失败时进入有界缓冲并返回 False。"""
        payload = {"sql": sql, "params": list(params), "many": many}
        try:
            with self._lock:
                conn = self._connect()
                try:
                    if self.pg:
                        with conn.cursor() as cur:
                            cur.execute(_to_pg(sql), tuple(params))
                    else:
                        conn.execute(sql, tuple(params))
                        conn.commit()
                    # 缓冲恢复：尽力补写之前失败的事件
                    while self._buffer:
                        item = self._buffer[0]
                        if self.pg:
                            with conn.cursor() as cur:
                                cur.execute(_to_pg(item["sql"]), tuple(item["params"]))
                        else:
                            conn.execute(item["sql"], tuple(item["params"]))
                        self._buffer.popleft()
                    if not self.pg:
                        conn.commit()
                finally:
                    conn.close()
            self._healthy = True
            self._last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self._healthy = False
            self._last_error = repr(exc)
            self._buffer.append(payload)
            return False

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        try:
            with self._lock:
                conn = self._connect()
                try:
                    if self.pg:
                        with conn.cursor() as cur:
                            cur.execute(_to_pg(sql), tuple(params))
                            if cur.description is None:
                                return []
                            cols = [d[0] for d in cur.description]
                            return [dict(zip(cols, row)) for row in cur.fetchall()]
                    cur = conn.execute(sql, tuple(params))
                    return [dict(row) for row in cur.fetchall()]
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001
            self._last_error = repr(exc)
            return []

    # ------------------------------------------------------------- tool calls
    def insert_call(self, record: ToolCallRecord) -> bool:
        row = record.to_row()
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        updates = ",".join(f"{k}=excluded.{k}" for k in row if k != "request_id")
        sql = (f"INSERT INTO tool_calls ({cols}) VALUES ({marks}) "
               f"ON CONFLICT(request_id) DO UPDATE SET {updates}")
        return self._execute(sql, list(row.values()))

    def update_call(self, request_id: str, **patch: Any) -> bool:
        if not patch:
            return True
        sets = ",".join(f"{k}=?" for k in patch)
        return self._execute(
            f"UPDATE tool_calls SET {sets} WHERE request_id=?",
            [*patch.values(), request_id],
        )

    def add_event(self, kind: str, *, status: str = "", request_id: str = "",
                  trace_id: str = "", tool_name: str = "", actor_id: str = "",
                  client_id: str = "", source: str = "", detail: Any = "") -> bool:
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        return self._execute(
            "INSERT INTO audit_events (ts,kind,status,request_id,trace_id,tool_name,"
            "actor_id,client_id,source,detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [utcnow(), kind, status, request_id, trace_id, tool_name,
             actor_id, client_id, source, text[:MAX_FIELD_CHARS]],
        )

    # ------------------------------------------------------------- sub tables
    def _insert(self, table: str, values: dict[str, Any]) -> bool:
        cols = ",".join(values)
        marks = ",".join("?" for _ in values)
        return self._execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values())
        )

    def upsert_session(self, session_id: str, client_id: str, declared: str = "") -> None:
        if not session_id:
            return
        self._execute(
            "INSERT INTO client_sessions (session_id,client_id,declared_client,first_seen,last_seen,call_count,status) "
            "VALUES (?,?,?,?,?,1,'active') ON CONFLICT(session_id) DO UPDATE SET "
            "last_seen=excluded.last_seen, call_count=client_sessions.call_count+1, "
            "declared_client=excluded.declared_client",
            [session_id, client_id, declared, utcnow(), utcnow()],
        )

    def add_task(self, values: dict[str, Any]) -> None:
        values.setdefault("ts", utcnow())
        self._insert("background_tasks", values)

    def add_git(self, values: dict[str, Any]) -> None:
        values.setdefault("ts", utcnow())
        self._insert("git_operations", values)

    def add_experiment(self, values: dict[str, Any]) -> None:
        values.setdefault("ts", utcnow())
        self._insert("experiment_runs", values)

    def add_approval(self, values: dict[str, Any]) -> None:
        values.setdefault("ts", utcnow())
        self._insert("approval_events", values)

    # ---------------------------------------------------------------- queries
    @staticmethod
    def _normalize_ts(value: Any) -> str:
        """把前端 ISO / datetime-local 边界统一成与 utcnow() 相同的 UTC 格式。"""
        text = str(value)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        except (ValueError, TypeError):
            return text

    @classmethod
    def _filters(cls, f: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if f.get("since"):
            clauses.append("timestamp_start >= ?")
            args.append(cls._normalize_ts(f["since"]))
        if f.get("until"):
            clauses.append("timestamp_start <= ?")
            args.append(cls._normalize_ts(f["until"]))
        for key in ("tool_name", "status", "client_id", "actor_id", "request_id",
                    "task_id", "git_repo", "experiment_id"):
            if f.get(key):
                clauses.append(f"{key} = ?")
                args.append(f[key])
        if f.get("keyword"):
            clauses.append("(arguments LIKE ? OR result LIKE ? OR error_message LIKE ? OR tool_name LIKE ?)")
            term = f"%{f['keyword']}%"
            args.extend([term] * 4)
        if f.get("file_path"):
            clauses.append("(file_path LIKE ? OR arguments LIKE ?)")
            args.extend([f"%{f['file_path']}%"] * 2)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    SORTABLE_COLUMNS = frozenset({
        "timestamp_start", "timestamp_end", "duration_ms", "tool_name",
        "status", "client_id", "actor_id", "source",
    })

    def query_calls(self, filters: dict[str, Any], *, limit: int = 50,
                    offset: int = 0, order: str = "desc",
                    sort: str = "timestamp_start") -> tuple[list[dict[str, Any]], int]:
        where, args = self._filters(filters)
        direction = "ASC" if order == "asc" else "DESC"
        column = sort if sort in self.SORTABLE_COLUMNS else "timestamp_start"
        total_rows = self._query(f"SELECT COUNT(*) AS n FROM tool_calls{where}", args)
        total = int(total_rows[0]["n"]) if total_rows else 0
        rows = self._query(
            f"SELECT * FROM tool_calls{where} ORDER BY {column} {direction}, id {direction} "
            "LIMIT ? OFFSET ?",
            [*args, min(limit, 500), max(0, offset)],
        )
        return rows, total

    def get_call(self, request_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM tool_calls WHERE request_id=?", [request_id])
        return rows[0] if rows else None

    def iter_calls(self, filters: dict[str, Any], batch: int = 1000) -> Any:
        """分页迭代导出，避免一次性把全部历史读进内存。"""
        offset = 0
        while True:
            rows, _ = self.query_calls(filters, limit=batch, offset=offset)
            if not rows:
                break
            yield from rows
            offset += len(rows)
            if len(rows) < batch:
                break

    def recent_errors(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._query(
            "SELECT request_id, tool_name, timestamp_start, status, error_type, "
            "error_message FROM tool_calls WHERE status IN "
            "('FAILED','DENIED','TIMEOUT','INTERRUPTED','CANCELLED') "
            "ORDER BY timestamp_start DESC LIMIT ?", [min(limit, 100)]
        )

    def get_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in
                self._query("SELECT key, value FROM admin_settings")}

    def set_setting(self, key: str, value: str) -> bool:
        return self._execute(
            "INSERT INTO admin_settings (key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at", [key, value, utcnow()]
        )

    def set_export(self, job_id: str, **patch: Any) -> None:
        sets = ",".join(f"{k}=?" for k in patch)
        self._execute(f"UPDATE export_jobs SET {sets} WHERE job_id=?",
                      [*patch.values(), job_id])

    def get_export(self, job_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM export_jobs WHERE job_id=?", [job_id])
        return rows[0] if rows else None

    def list_exports(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM export_jobs ORDER BY ts DESC LIMIT ?", [limit])

    def call_timeline(self, request_id: str, trace_id: str = "") -> list[dict[str, Any]]:
        if trace_id:
            return self._query(
                "SELECT * FROM audit_events WHERE request_id=? OR trace_id=? ORDER BY id",
                [request_id, trace_id],
            )
        return self._query("SELECT * FROM audit_events WHERE request_id=? ORDER BY id", [request_id])

    def recent_events(self, after_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM audit_events WHERE id > ? ORDER BY id LIMIT ?",
            [after_id, min(limit, 500)],
        )

    def max_event_id(self) -> int:
        rows = self._query("SELECT COALESCE(MAX(id),0) AS n FROM audit_events")
        return int(rows[0]["n"]) if rows else 0

    def stats(self, since: str, until: str = "") -> dict[str, Any]:
        end = until or utcnow()
        rows = self._query(
            "SELECT status, COUNT(*) AS n, COALESCE(AVG(duration_ms),0) AS avg_ms "
            "FROM tool_calls WHERE timestamp_start >= ? AND timestamp_start <= ? GROUP BY status",
            [since, end],
        )
        by_status = {r["status"]: int(r["n"]) for r in rows}
        total = sum(by_status.values())
        avg = sum(float(r["avg_ms"]) * int(r["n"]) for r in rows) / total if total else 0.0
        top = self._query(
            "SELECT tool_name, COUNT(*) AS n FROM tool_calls WHERE timestamp_start >= ? "
            "AND timestamp_start <= ? GROUP BY tool_name ORDER BY n DESC LIMIT 8",
            [since, end],
        )
        clients = self._query(
            "SELECT COALESCE(NULLIF(declared_client,''), client_id) AS label, COUNT(*) AS n "
            "FROM tool_calls WHERE timestamp_start >= ? AND timestamp_start <= ? "
            "GROUP BY label ORDER BY n DESC LIMIT 8",
            [since, end],
        )
        return {
            "total": total,
            "by_status": by_status,
            "avg_duration_ms": round(avg, 1),
            "top_tools": [{"label": r["tool_name"], "count": int(r["n"])} for r in top],
            "client_distribution": [
                {"label": r["label"] or "unknown", "count": int(r["n"])} for r in clients
            ],
        }

    def time_series(self, since: str, until: str = "", buckets: int = 24) -> dict[str, Any]:
        """按时间桶返回调用 / 失败 / 超时 / 平均耗时趋势，SQL 聚合避免全表扫描。"""
        end = until or utcnow()
        rows = self._query(
            "SELECT timestamp_start, status, duration_ms FROM tool_calls "
            "WHERE timestamp_start >= ? AND timestamp_start <= ? ORDER BY timestamp_start",
            [since, end],
        )
        try:
            from datetime import datetime
            start_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            span = max(1.0, (end_dt - start_dt).total_seconds())
        except (ValueError, TypeError):
            span, start_dt = 1.0, None
        width = span / max(1, buckets)
        labels = [f"T{i}" for i in range(buckets)]
        calls = [0] * buckets
        failed = [0] * buckets
        timed_out = [0] * buckets
        durations: list[list[int]] = [[] for _ in range(buckets)]
        for row in rows:
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(str(row["timestamp_start"]).replace("Z", "+00:00"))
                idx = min(buckets - 1, max(0, int((ts - start_dt).total_seconds() / width)))
            except (ValueError, TypeError):
                idx = 0
            calls[idx] += 1
            status = row["status"]
            if status in ("FAILED", "DENIED", "INTERRUPTED"):
                failed[idx] += 1
            if status == "TIMEOUT":
                timed_out[idx] += 1
            durations[idx].append(int(row["duration_ms"] or 0))
        avg_ms = [round(sum(d) / len(d), 1) if d else 0 for d in durations]
        return {"labels": labels, "calls": calls, "failed": failed,
                "timeouts": timed_out, "avg_duration_ms": avg_ms}

    def duration_distribution(self, since: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT duration_ms FROM tool_calls WHERE timestamp_start >= ?", [since]
        )
        bins = [(0, 50), (50, 200), (200, 1000), (1000, 5000), (5000, 10**12)]
        labels = ["<50ms", "50-200ms", "0.2-1s", "1-5s", ">5s"]
        counts = [0] * len(bins)
        for row in rows:
            value = int(row["duration_ms"] or 0)
            for i, (lo, hi) in enumerate(bins):
                if lo <= value < hi:
                    counts[i] += 1
                    break
        return [{"label": labels[i], "count": counts[i]} for i in range(len(bins))]

    def active_tasks(self) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM background_tasks WHERE status='running' "
            "AND id IN (SELECT MAX(id) FROM background_tasks GROUP BY task_id)"
        )
        return int(rows[0]["n"]) if rows else 0

    def tool_count(self) -> int:
        rows = self._query("SELECT COUNT(DISTINCT tool_name) AS n FROM tool_calls")
        return int(rows[0]["n"]) if rows else 0

    def sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM client_sessions ORDER BY last_seen DESC LIMIT ?", [limit]
        )

    def git_ops(self, repo: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if repo:
            return self._query(
                "SELECT * FROM git_operations WHERE repo_path LIKE ? OR repo_name LIKE ? "
                "ORDER BY ts DESC LIMIT ?", [f"%{repo}%", f"%{repo}%", limit]
            )
        return self._query("SELECT * FROM git_operations ORDER BY ts DESC LIMIT ?", [limit])

    def experiments(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM experiment_runs ORDER BY ts DESC LIMIT ?", [limit]
        )

    def git_for_request(self, request_id: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM git_operations WHERE request_id=? ORDER BY ts", [request_id]
        )

    def experiments_for_request(self, request_id: str,
                                task_id: str = "") -> list[dict[str, Any]]:
        if task_id:
            return self._query(
                "SELECT * FROM experiment_runs WHERE request_id=? OR task_id=? ORDER BY ts",
                [request_id, task_id],
            )
        return self._query(
            "SELECT * FROM experiment_runs WHERE request_id=? ORDER BY ts", [request_id]
        )

    def tasks(self, limit: int = 100, task_id: str = "") -> list[dict[str, Any]]:
        if task_id:
            return self._query(
                "SELECT * FROM background_tasks WHERE task_id=? ORDER BY ts", [task_id]
            )
        return self._query("SELECT * FROM background_tasks ORDER BY ts DESC LIMIT ?", [limit])

    # ------------------------------------------------------------- maintenance
    def mark_interrupted(self) -> int:
        """服务异常中断后，把残留的非终态调用收敛为 INTERRUPTED 终态。"""
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        sql = (f"UPDATE tool_calls SET status='INTERRUPTED', timestamp_end=?, "
               f"error_type='ServiceInterrupted', error_message='服务中断，未记录到终态' "
               f"WHERE status NOT IN ({placeholders})")
        self._execute(sql, [utcnow(), *TERMINAL_STATUSES])
        rows = self._query("SELECT COUNT(*) AS n FROM tool_calls WHERE status='INTERRUPTED'")
        return int(rows[0]["n"]) if rows else 0

    def archive_and_prune(self, retention_days: int, max_db_mb: int,
                          archive_dir: str = "") -> dict[str, Any]:
        """按时间归档为 JSONL 后删除旧行；数据库超配额时继续淘汰最旧记录。"""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))).isoformat()
        directory = Path(archive_dir or str(Path(self.db_path).parent / "archive"))
        rows = self._query(
            "SELECT * FROM tool_calls WHERE timestamp_start < ? ORDER BY timestamp_start LIMIT 5000",
            [cutoff],
        )
        archived = 0
        if rows:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"tool_calls-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.jsonl"
                with path.open("w", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                os.chmod(path, 0o600)
                archived = len(rows)
            except OSError:
                archived = 0
        if archived:
            self._execute("DELETE FROM tool_calls WHERE timestamp_start < ?", [cutoff])
        size_mb = 0.0
        try:
            size_mb = Path(self.db_path).stat().st_size / (1024 * 1024)
        except OSError:
            pass
        pruned = 0
        if size_mb > max_db_mb > 0:
            old = self._query(
                "SELECT request_id FROM tool_calls ORDER BY timestamp_start LIMIT 5000"
            )
            for row in old:
                self._execute("DELETE FROM tool_calls WHERE request_id=?", [row["request_id"]])
            pruned = len(old)
            self._execute("VACUUM") if not self.pg else None
        return {"archived": archived, "pruned": pruned, "size_mb": round(size_mb, 2)}

    def backup(self, target: str) -> str:
        from datetime import datetime, timezone
        path = target or str(
            Path(self.db_path).parent / "backup" /
            f"audit-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db"
        )
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if self.pg:
            raise RuntimeError("PostgreSQL 请使用 pg_dump 备份")
        with self._lock:
            src = self._connect()
            try:
                src.execute("VACUUM INTO ?", [path])
            finally:
                src.close()
        return path

    def restore(self, source: str) -> bool:
        """恢复备份：替换当前 DB 文件（调用方需先停止写入方进程）。"""
        if self.pg or not Path(source).is_file():
            return False
        try:
            with self._lock:
                Path(source).replace(self.db_path)
            self._init_db()
            return True
        except OSError:
            return False


_STORAGE: AuditStorage | None = None
_STORAGE_LOCK = threading.Lock()


def get_storage() -> AuditStorage:
    global _STORAGE
    if _STORAGE is None:
        with _STORAGE_LOCK:
            if _STORAGE is None:
                _STORAGE = AuditStorage(
                    db_path=os.environ.get("AUDIT_DB_PATH", DEFAULT_DB_PATH),
                    dsn=os.environ.get("AUDIT_DB_DSN", ""),
                )
    return _STORAGE
