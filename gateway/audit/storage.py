"""审计持久化：SQLite WAL（默认）或 PostgreSQL（可选）。

- 写库失败不影响工具执行：进入有界内存缓冲并标记降级；
  AUDIT_STRICT=1 时，高风险工具的调用在无法可靠写入时按配置拒绝。
- 所有查询走索引；大输出只保存引用、大小与 SHA-256。
- 提供保留策略、按时间/大小归档、备份恢复与启动时的未终态收敛。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .events import TERMINAL_STATUSES, ToolCallRecord, utcnow
from .redaction import ArtifactStore, redact_params, redact_structure
from .spool import DiskSpool

DEFAULT_DB_PATH = os.environ.get("AUDIT_DB_PATH", "/data/audit/audit.db")
MAX_FIELD_CHARS = int(os.environ.get("AUDIT_MAX_FIELD_CHARS", "32768"))
MAX_BUFFER = int(os.environ.get("AUDIT_BUFFER_MAX", "1000"))
STRICT = os.environ.get("AUDIT_STRICT", "0").strip().lower() in ("1", "true", "yes", "on")
# 单次查询上限：必须 >= iter_calls 的批量，否则导出会在第一批提前终止。
QUERY_MAX_LIMIT = 1000
ARCHIVE_BATCH = max(100, min(5000, int(os.environ.get("AUDIT_ARCHIVE_BATCH", "1000"))))
# 客户端会话的令牌/空闲有效期（秒）
SESSION_TTL = max(60, int(os.environ.get("MCP_SESSION_TTL", "3600")))
# SQL 聚合不可用时的有界回退行数，避免把整表拉进内存
AGGREGATION_FALLBACK_ROWS = 20_000
# 纳入保留策略的历史表：(表名, 时间列, 主键)。
# 不纳入的表：admin_settings（运行配置）、oauth_* / paper_library（当前有效状态，
# 按时间删除会误删仍有效的授权与文献）、spool_applied（幂等去重标记）。
ARCHIVE_TABLES = (
    ("tool_calls", "timestamp_start", "id"),
    ("audit_events", "ts", "id"),
    ("background_tasks", "ts", "id"),
    ("git_operations", "ts", "id"),
    ("experiment_runs", "ts", "id"),
    ("approval_events", "ts", "id"),
    ("login_attempts", "ts", "id"),
    ("export_jobs", "ts", "id"),
    ("admin_sessions", "expires_at", "id"),
    ("client_sessions", "last_seen", "id"),
    ("search_events", "ts", "id"),
    ("paper_downloads", "ts", "id"),
    ("paper_text_cache", "ts", "id"),
    ("file_transfers", "ts", "id"),
    ("task_state", "ts", "id"),
    ("archive_manifest", "ts", "id"),
)

# 需要严格审计的高风险工具（strict 模式下写库失败即拒绝）。
# 新增的论文下载 / 结构化 Git / 传输 / 宿主工具全部纳入同一道门。
HIGH_RISK_TOOLS = frozenset({
    "run_command", "run_command_background", "write_file", "kill_process",
    "stop_task", "read_web_page", "fetch_web_page", "search_website",
    "download_paper", "batch_download_papers", "read_paper_pdf",
    "git_clone", "git_branch", "git_stage", "git_commit", "git_sync",
    "git_recover", "git_worktree", "upload_file", "download_file",
    "host_exec", "host_spawn", "host_system_info",
    # 非 MCP 的 HTTP 传输端点也走 trace_call，严格模式下同样不得绕过审计。
    "files.upload", "files.download",
})

# 归档校验：流式 SHA-256 清单 + 删前二次读回比对
ARCHIVE_MANIFEST_SUFFIX = ".manifest.json"

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
-- V3：以下 DDL 保持方言中性（PG 由 _PG_SCHEMA 的字符串替换生成）。
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id TEXT PRIMARY KEY, redirect_uris TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS oauth_codes (
  code_hash TEXT PRIMARY KEY, client_id TEXT, redirect_uri TEXT,
  challenge TEXT, challenge_method TEXT, expires_at TEXT,
  consumed INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
  token_hash TEXT PRIMARY KEY, client_id TEXT, expires_at TEXT,
  revoked INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS spool_applied (
  event_id TEXT PRIMARY KEY, applied_at TEXT
);
CREATE TABLE IF NOT EXISTS archive_manifest (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, path TEXT, table_name TEXT,
  batch INTEGER DEFAULT 0, row_count INTEGER DEFAULT 0, sha256 TEXT,
  status TEXT, verified_at TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS search_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, request_id TEXT, trace_id TEXT,
  tool_name TEXT, client_id TEXT, source TEXT, query_hash TEXT, keyword TEXT,
  provider TEXT, status TEXT, duration_ms INTEGER DEFAULT 0,
  result_count INTEGER DEFAULT 0, cached INTEGER DEFAULT 0,
  rate_limited INTEGER DEFAULT 0, error TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS paper_library (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, paper_id TEXT UNIQUE,
  title TEXT, authors TEXT, publication_year INTEGER, venue TEXT,
  doi TEXT, arxiv_id TEXT, pmid TEXT, pmcid TEXT, citation_count INTEGER,
  oa_status TEXT, landing_url TEXT, pdf_url TEXT, sources TEXT,
  file_path TEXT, sha256 TEXT, file_size INTEGER DEFAULT 0,
  project TEXT, tags TEXT, reading_status TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS paper_downloads (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, request_id TEXT, trace_id TEXT,
  paper_id TEXT, title TEXT, source TEXT, url TEXT, file_path TEXT,
  status TEXT, bytes INTEGER DEFAULT 0, total_bytes INTEGER DEFAULT 0,
  sha256 TEXT, project TEXT, error_type TEXT, error_message TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS paper_text_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, paper_id TEXT,
  page_count INTEGER DEFAULT 0, cache_path TEXT, sha256 TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS file_transfers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, request_id TEXT, trace_id TEXT,
  direction TEXT, path TEXT, status TEXT, bytes INTEGER DEFAULT 0,
  total_bytes INTEGER DEFAULT 0, sha256 TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS task_state (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, task_id TEXT, pid INTEGER,
  pgid INTEGER, command TEXT, cwd TEXT, log_path TEXT, status TEXT,
  git_commit TEXT, output_path TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_search_events_ts ON search_events(ts);
CREATE INDEX IF NOT EXISTS ix_search_events_req ON search_events(request_id);
CREATE INDEX IF NOT EXISTS ix_paper_downloads_req ON paper_downloads(request_id);
CREATE INDEX IF NOT EXISTS ix_paper_downloads_status ON paper_downloads(status, ts);
CREATE INDEX IF NOT EXISTS ix_paper_library_doi ON paper_library(doi);
CREATE INDEX IF NOT EXISTS ix_transfers_req ON file_transfers(request_id);
CREATE INDEX IF NOT EXISTS ix_task_state_task ON task_state(task_id, ts);
CREATE INDEX IF NOT EXISTS ix_archive_manifest_ts ON archive_manifest(ts);
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
        self.spool = DiskSpool(Path(self.db_path).parent / "spool")
        self._spool_error = self.spool.last_error
        self._spool_replayed = 0
        self._healthy = True
        self._last_error = ""
        self._strict_violations = 0
        self._init_db()
        self._startup_replay()

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
            "strict_violations": self._strict_violations,
            # 磁盘 spool：>0 表示仍有事件未补写，严格审计门必须拒绝高风险调用
            "spool_pending": self.spool.pending(),
            "spool_error": self._spool_error or self.spool.last_error,
            "spool_replayed": self._spool_replayed,
            "archive_dir": str(Path(self.db_path).parent / "archive"),
        }

    def available(self) -> bool:
        """是否仍能接收写入（含缓冲）；不代表已持久化。"""
        return self._healthy or len(self._buffer) < MAX_BUFFER

    def _durable(self) -> bool:
        return (bool(self._healthy) and not self._buffer
                and self.spool.pending() == 0
                and not self._spool_error and not self.spool.last_error)

    def persistence_state(self) -> dict[str, Any]:
        """区分「数据库健康」「可缓冲写入」「已确认持久化」三态。

        对外仍保持四个既有字段（后台与既有测试依赖精确结构）；spool 状态放在
        ``health()``，但严格门判定必须把磁盘缓冲是否为空计入。
        """
        return {
            "healthy": bool(self._healthy),
            "buffered": len(self._buffer),
            "bufferable": self.available(),
            "persisted": self._durable(),
        }

    def confirm_persisted(self, request_id: str) -> bool:
        """严格审计：DB 健康、内存缓冲与磁盘 spool 均为空，且调用确实可读回。"""
        if not self._durable():
            return False
        rows = self._query(
            "SELECT request_id FROM tool_calls WHERE request_id=?", [request_id]
        )
        return bool(rows)

    def note_strict_violation(self, request_id: str, tool_name: str,
                              phase: str) -> None:
        self._strict_violations += 1
        self.add_event("audit_strict_violation", status="FAILED",
                       request_id=request_id, tool_name=tool_name, source="audit",
                       detail=f"{phase}写入失败；该调用可能缺少终态记录")

    def _apply_payload(self, conn: Any, payload: dict[str, Any]) -> None:
        """在同一连接/事务内执行一条已脱敏的写载荷。"""
        sql = payload["sql"]
        params = tuple(payload.get("params") or ())
        if self.pg:
            with conn.cursor() as cur:
                cur.execute(_to_pg(sql), params)
        else:
            conn.execute(sql, params)

    def _applied(self, conn: Any, event_id: str) -> bool:
        if not event_id:
            return False
        sql = ("SELECT event_id FROM spool_applied WHERE event_id=?"
               if not self.pg else
               "SELECT event_id FROM spool_applied WHERE event_id=%s")
        if self.pg:
            with conn.cursor() as cur:
                cur.execute(sql, (event_id,))
                return cur.fetchone() is not None
        return conn.execute(sql, (event_id,)).fetchone() is not None

    def _mark_applied(self, conn: Any, event_id: str) -> None:
        if not event_id:
            return
        sql = ("INSERT OR IGNORE INTO spool_applied (event_id,applied_at) VALUES (?,?)"
               if not self.pg else
               "INSERT INTO spool_applied (event_id,applied_at) VALUES (%s,%s) "
               "ON CONFLICT DO NOTHING")
        if self.pg:
            with conn.cursor() as cur:
                cur.execute(sql, (event_id, utcnow()))
        else:
            conn.execute(sql, (event_id, utcnow()))

    def _flush_pending(self, conn: Any) -> None:
        """按 event_id 幂等地补写内存缓冲 + 磁盘 spool，成功后才清空 spool。"""
        while self._buffer:
            item = self._buffer[0]
            self._apply_payload(conn, item)
            self._mark_applied(conn, str(item.get("event_id") or ""))
            self._buffer.popleft()
        spooled = list(self.spool.read_all())
        for item in spooled:
            event_id = str(item.get("event_id") or "")
            if event_id and self._applied(conn, event_id):
                continue
            self._apply_payload(conn, item)
            self._mark_applied(conn, event_id)
        if not self.pg:
            conn.commit()
        self.spool.clear()
        self._spool_error = self.spool.last_error
        self._spool_replayed += len(spooled)

    def _startup_replay(self) -> None:
        """服务重启后回放上次进程未及补写的审计事件。"""
        if not self._healthy or self.spool.pending() == 0:
            return
        try:
            with self._lock:
                conn = self._connect()
                try:
                    self._flush_pending(conn)
                finally:
                    conn.close()
            self._healthy = True
            self._last_error = ""
        except Exception as exc:  # noqa: BLE001
            self._healthy = False
            self._last_error = f"spool 启动回放失败: {exc!r}"

    def _execute(self, sql: str, params: Iterable[Any] = (), *, many: bool = False) -> bool:
        """唯一 SQL 写边界：统一脱敏；失败时同时进入有界内存缓冲与磁盘 spool。"""
        safe_params = redact_params(list(params))
        payload = {"event_id": uuid.uuid4().hex, "sql": sql,
                   "params": safe_params, "many": many}
        try:
            with self._lock:
                conn = self._connect()
                try:
                    self._apply_payload(conn, payload)
                    if not self.pg:
                        conn.commit()
                    self._flush_pending(conn)
                finally:
                    conn.close()
            self._healthy = True
            self._last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self._healthy = False
            self._last_error = repr(exc)
            self._buffer.append(payload)
            if not self.spool.append(payload):
                self._spool_error = (self.spool.last_error
                                     or "磁盘 spool 写入失败，事件仅存于内存缓冲")
            return False

    def _query_checked(self, sql: str,
                       params: Iterable[Any] = ()) -> list[dict[str, Any]] | None:
        """与 _query 相同，但用 None 区分「查询失败」与「查到 0 行」。"""
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
            return None

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        rows = self._query_checked(sql, params)
        return rows if rows is not None else []

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

    def upsert_session(self, session_id: str, client_id: str, declared: str = "",
                       token_expires: str = "") -> None:
        if not session_id:
            return
        self._execute(
            "INSERT INTO client_sessions (session_id,client_id,declared_client,first_seen,"
            "last_seen,call_count,status,token_expires) VALUES (?,?,?,?,?,1,'active',?) "
            "ON CONFLICT(session_id) DO UPDATE SET last_seen=excluded.last_seen, "
            "call_count=client_sessions.call_count+1, "
            "declared_client=excluded.declared_client, status='active', "
            "token_expires=excluded.token_expires",
            [session_id, client_id, declared, utcnow(), utcnow(), token_expires],
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
                    "task_id", "git_repo", "experiment_id", "source"):
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
            [*args, min(limit, QUERY_MAX_LIMIT), max(0, offset)],
        )
        return rows, total

    def get_call(self, request_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM tool_calls WHERE request_id=?", [request_id])
        return rows[0] if rows else None

    def iter_calls(self, filters: dict[str, Any],
                   batch: int = 1000) -> Iterator[dict[str, Any]]:
        """按主键游标分批导出：固定时间上界 + keyset 分页，避免 OFFSET 漂移与截断。"""
        effective = max(1, min(int(batch), QUERY_MAX_LIMIT))
        frozen = dict(filters)
        if not frozen.get("until"):
            frozen["until"] = utcnow()
        where, args = self._filters(frozen)
        clause = f"{where} AND id > ?" if where else " WHERE id > ?"
        last_id = 0
        while True:
            rows = self._query(
                f"SELECT * FROM tool_calls{clause} ORDER BY id ASC LIMIT ?",
                [*args, last_id, effective],
            )
            if not rows:
                break
            yield from rows
            last_id = int(rows[-1]["id"])
            if len(rows) < effective:
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

    @staticmethod
    def _parse_ts(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _duration_bin(value: Any) -> int:
        try:
            ms = int(value or 0)
        except (TypeError, ValueError):
            return -1
        for idx, (lo, hi) in enumerate(((0, 50), (50, 200), (200, 1000),
                                        (1000, 5000), (5000, 10**12))):
            if lo <= ms < hi:
                return idx
        return -1

    def time_series(self, since: str, until: str = "", buckets: int = 24) -> dict[str, Any]:
        """按方言 SQL 聚合时间桶；方言不支持时回退到有界 Python 聚合。"""
        n = max(1, min(int(buckets), 168))
        end = until or utcnow()
        labels = [f"T{i}" for i in range(n)]
        calls = [0] * n
        failed = [0] * n
        timed_out = [0] * n
        sums = [0.0] * n

        start_dt = self._parse_ts(since)
        end_dt = self._parse_ts(end)
        span = (max(1.0, (end_dt - start_dt).total_seconds())
                if start_dt and end_dt else 1.0)
        width = span / n

        if self.pg:
            bucket = ("CAST(FLOOR((EXTRACT(EPOCH FROM timestamp_start::timestamptz) "
                      "- EXTRACT(EPOCH FROM ?::timestamptz)) / ?) AS INTEGER)")
        else:
            bucket = ("CAST((julianday(timestamp_start) - julianday(?)) "
                      "* 86400.0 / ? AS INTEGER)")
        sql = (
            f"SELECT {bucket} AS bucket, COUNT(*) AS n, "
            "SUM(CASE WHEN status IN ('FAILED','DENIED','INTERRUPTED') THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN status='TIMEOUT' THEN 1 ELSE 0 END) AS timed_out, "
            "COALESCE(AVG(COALESCE(duration_ms,0)),0) AS avg_ms "
            "FROM tool_calls WHERE timestamp_start >= ? AND timestamp_start <= ? GROUP BY 1"
        )
        rows = self._query_checked(sql, [since, width, since, end])
        if rows is None:
            fallback = self._query_checked(
                "SELECT timestamp_start, status, duration_ms FROM tool_calls "
                "WHERE timestamp_start >= ? AND timestamp_start <= ? "
                "ORDER BY timestamp_start LIMIT ?",
                [since, end, AGGREGATION_FALLBACK_ROWS],
            ) or []
            for row in fallback:
                ts = self._parse_ts(row.get("timestamp_start"))
                idx = (min(n - 1, max(0, int((ts - start_dt).total_seconds() / width)))
                       if ts and start_dt else 0)
                calls[idx] += 1
                if row["status"] in ("FAILED", "DENIED", "INTERRUPTED"):
                    failed[idx] += 1
                if row["status"] == "TIMEOUT":
                    timed_out[idx] += 1
                sums[idx] += int(row["duration_ms"] or 0)
        else:
            for row in rows:
                try:
                    idx = min(n - 1, max(0, int(row["bucket"] or 0)))
                except (TypeError, ValueError):
                    idx = 0
                count = int(row["n"] or 0)
                calls[idx] += count
                failed[idx] += int(row["failed"] or 0)
                timed_out[idx] += int(row["timed_out"] or 0)
                sums[idx] += float(row["avg_ms"] or 0) * count
        avg_ms = [round(sums[i] / calls[i], 1) if calls[i] else 0 for i in range(n)]
        return {"labels": labels, "calls": calls, "failed": failed,
                "timeouts": timed_out, "avg_duration_ms": avg_ms}

    def duration_distribution(self, since: str) -> list[dict[str, Any]]:
        labels = ["<50ms", "50-200ms", "0.2-1s", "1-5s", ">5s"]
        counts = [0] * len(labels)
        sql = (
            "SELECT CASE WHEN duration_ms IS NULL THEN 0 "
            "WHEN duration_ms < 0 THEN -1 "
            "WHEN duration_ms < 50 THEN 0 WHEN duration_ms < 200 THEN 1 "
            "WHEN duration_ms < 1000 THEN 2 WHEN duration_ms < 5000 THEN 3 "
            "ELSE 4 END AS bin, COUNT(*) AS n "
            "FROM tool_calls WHERE timestamp_start >= ? GROUP BY 1"
        )
        rows = self._query_checked(sql, [since])
        if rows is None:
            fallback = self._query_checked(
                "SELECT duration_ms FROM tool_calls WHERE timestamp_start >= ? LIMIT ?",
                [since, AGGREGATION_FALLBACK_ROWS],
            ) or []
            for row in fallback:
                idx = self._duration_bin(row.get("duration_ms"))
                if idx >= 0:
                    counts[idx] += 1
        else:
            for row in rows:
                try:
                    idx = int(row["bin"])
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(counts):
                    counts[idx] += int(row["n"] or 0)
        return [{"label": labels[i], "count": counts[i]} for i in range(len(labels))]

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
        """活跃状态按 last_seen / token_expires 实时判定，历史会话不再永远 active。"""
        rows = self._query(
            "SELECT * FROM client_sessions ORDER BY last_seen DESC LIMIT ?", [limit]
        )
        now = utcnow()
        idle_cutoff = (datetime.now(timezone.utc)
                       - timedelta(seconds=SESSION_TTL)).isoformat(timespec="milliseconds")
        for row in rows:
            last_seen = str(row.get("last_seen") or "")
            token_expires = str(row.get("token_expires") or "")
            active = bool(last_seen and last_seen >= idle_cutoff
                          and (not token_expires or token_expires >= now))
            row["status"] = "active" if active else "expired"
            row["active"] = active
            row["idle_ttl_seconds"] = SESSION_TTL
        return rows

    def active_session_count(self) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM client_sessions "
            "WHERE last_seen >= ? AND (token_expires IS NULL OR token_expires = '' "
            "OR token_expires >= ?)",
            [(datetime.now(timezone.utc)
              - timedelta(seconds=SESSION_TTL)).isoformat(timespec="milliseconds"), utcnow()],
        )
        return int(rows[0]["n"]) if rows else 0

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

    def _db_size_mb(self) -> float:
        try:
            return Path(self.db_path).stat().st_size / (1024 * 1024)
        except OSError:
            return 0.0

    @staticmethod
    def _archive_table(label: str) -> str:
        """归档文件名前缀 -> 真实表名；配额归档复用 tool_calls 的标签。"""
        return label.split("-quota", 1)[0]

    def _write_archive(self, rows: list[dict[str, Any]], directory: Path,
                       label: str, seq: int) -> Path:
        """0600 创建、fsync、流式 SHA-256 + 清单；核验失败不留下误导性副本。"""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        path = directory / f"{label}-{stamp}-{seq}.jsonl"
        manifest_path = path.with_suffix(ARCHIVE_MANIFEST_SUFFIX)
        digest = hashlib.sha256()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for row in rows:
                    # 兼容修复前落库的历史行：归档文件也经过同一脱敏入口
                    safe = redact_structure(row)
                    line = json.dumps(safe, ensure_ascii=False, default=str) + "\n"
                    fh.write(line)
                    digest.update(line.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            check = hashlib.sha256()
            count = 0
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    check.update(chunk)
                    count += chunk.count(b"\n")
            if count != len(rows) or check.hexdigest() != digest.hexdigest():
                raise OSError("归档文件行数/摘要校验失败")
            manifest = {
                "version": 1, "path": path.name, "table": self._archive_table(label),
                "label": label, "batch": seq, "row_count": len(rows),
                "sha256": digest.hexdigest(), "created_at": utcnow(),
            }
            tmp = manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(manifest_path)
            self._insert("archive_manifest", {
                "ts": utcnow(), "path": str(path), "table_name": manifest["table"],
                "batch": seq, "row_count": len(rows), "sha256": manifest["sha256"],
                "status": "written", "verified_at": "", "detail": "",
            })
        except Exception:
            path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            raise
        return path

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.with_suffix(ARCHIVE_MANIFEST_SUFFIX).read_text(
                encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    def verify_archive(self, source: str) -> dict[str, Any]:
        """流式重算行数与 SHA-256，与清单比对；结果同步写回 archive_manifest。"""
        path = Path(source)
        result: dict[str, Any] = {"path": str(path), "ok": False, "rows": 0,
                                  "sha256": "", "reason": ""}
        if not path.is_file():
            result["reason"] = "归档文件不存在"
            return result
        manifest = self._read_manifest(path)
        digest = hashlib.sha256()
        count = 0
        try:
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    digest.update(chunk)
                    count += chunk.count(b"\n")
        except OSError as exc:
            result["reason"] = f"归档文件不可读: {exc!r}"
            return result
        result.update({"rows": count, "sha256": digest.hexdigest()})
        if not manifest:
            result["reason"] = "缺少 SHA-256 清单，无法确认完整性"
        elif count != int(manifest.get("row_count", -1)):
            result["reason"] = (f"行数不符：清单 {manifest.get('row_count')}，"
                                f"实际 {count}")
        elif digest.hexdigest() != str(manifest.get("sha256", "")):
            result["reason"] = "SHA-256 摘要不符，归档可能已损坏或被篡改"
        else:
            result["ok"] = True
        self._execute("UPDATE archive_manifest SET status=?, verified_at=?, detail=? "
                      "WHERE path=?",
                      ["verified" if result["ok"] else "corrupt", utcnow(),
                       result["reason"], str(path)])
        if not result["ok"]:
            self.add_event("audit_archive_warning", status="FAILED",
                           source="audit-storage",
                           detail=f"归档校验失败，原记录不得删除: {path.name} "
                                  f"{result['reason']}")
        return result

    def list_archives(self, filters: dict[str, Any] | None = None,
                      limit: int = 100) -> list[dict[str, Any]]:
        f = filters or {}
        root = Path(f.get("directory") or str(Path(self.db_path).parent / "archive"))
        if not root.is_dir():
            return []
        rows = self._query(
            "SELECT path, table_name, batch, row_count, sha256, status, ts, "
            "verified_at, detail FROM archive_manifest ORDER BY id DESC LIMIT ?",
            [min(max(1, limit), 1000)],
        )
        by_path = {str(r.get("path")): r for r in rows}
        out: list[dict[str, Any]] = []
        for path in sorted(root.glob("*.jsonl"), key=lambda p: p.name, reverse=True):
            manifest = self._read_manifest(path)
            stored = by_path.get(str(path), {})
            record = {
                "path": str(path), "name": path.name,
                "table": manifest.get("table") or stored.get("table_name") or
                         self._archive_table(path.name.split("-")[0]),
                "row_count": int(manifest.get("row_count") or stored.get("row_count") or 0),
                "sha256": manifest.get("sha256") or stored.get("sha256") or "",
                "status": stored.get("status") or ("written" if manifest else "unverified"),
                "created_at": manifest.get("created_at") or stored.get("ts") or "",
                "verified_at": stored.get("verified_at") or "",
                "size_bytes": path.stat().st_size,
                "has_manifest": bool(manifest),
            }
            if f.get("table") and record["table"] != f["table"]:
                continue
            if f.get("request_id"):
                hits = self.query_archive(str(f["request_id"]), [path])
                if not hits:
                    continue
                record["match_count"] = len(hits)
            out.append(record)
            if len(out) >= min(max(1, limit), 1000):
                break
        return out

    def query_archive(self, request_id: str,
                      paths: list[Path] | None = None) -> list[dict[str, Any]]:
        """按 request_id 在归档 JSONL 中查找（只读，用于恢复前定位）。"""
        if not request_id:
            return []
        root = Path(self.db_path).parent / "archive"
        candidates = paths or sorted(root.glob("*.jsonl"))
        hits: list[dict[str, Any]] = []
        for path in candidates:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        if request_id not in line or not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if row.get("request_id") == request_id:
                            hits.append({"path": str(path), "row": redact_structure(row)})
            except OSError:
                continue
            if len(hits) >= 200:
                break
        return hits

    def _table_columns(self, table: str) -> list[str]:
        if self.pg:
            rows = self._query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name=?", [table])
            return [str(r["column_name"]) for r in rows]
        return [str(r["name"]) for r in self._query(f"PRAGMA table_info({table})")]

    def restore_archive(self, source: str) -> dict[str, Any]:
        """幂等恢复归档行：先校验摘要，再逐行 INSERT OR IGNORE / ON CONFLICT DO NOTHING。"""
        path = Path(source)
        verified = self.verify_archive(str(path))
        result: dict[str, Any] = {
            "ok": False, "path": str(path),
            "table": self._read_manifest(path).get("table", ""),
            "rows": 0, "restored": 0, "already_present": 0, "reason": "",
        }
        if not verified["ok"]:
            result["reason"] = verified["reason"]
            return result
        table = result["table"] or self._archive_table(path.name.split("-")[0])
        if table not in {t[0] for t in ARCHIVE_TABLES}:
            result["reason"] = f"未知归档表 {table}，拒绝恢复"
            return result
        columns = self._table_columns(table)
        if not columns:
            result["reason"] = f"无法读取表 {table} 的列定义"
            return result
        pk = next((t[2] for t in ARCHIVE_TABLES if t[0] == table), "id")
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    result["rows"] += 1
                    keys = [k for k in row if k in columns]
                    if not keys:
                        continue
                    if pk in row:
                        found = self._query(
                            f"SELECT 1 AS hit FROM {table} WHERE {pk}=? LIMIT 1",
                            [row[pk]])
                        if found:
                            result["already_present"] += 1
                            continue
                    marks = ",".join("?" for _ in keys)
                    prefix = "" if self.pg else "OR IGNORE "
                    sql = (f"INSERT {prefix}INTO {table} ({','.join(keys)}) "
                           f"VALUES ({marks})")
                    if self.pg:
                        sql += " ON CONFLICT DO NOTHING"
                    if self._execute(sql, [row[k] for k in keys]):
                        result["restored"] += 1
        except OSError as exc:
            result["reason"] = f"读取归档失败: {exc!r}"
            return result
        result["ok"] = result["rows"] > 0
        self.add_event("audit_archive_restore",
                       status="OK" if result["ok"] else "FAILED",
                       source="admin", detail=result)
        return result

    def _delete_ids(self, table: str, pk: str, ids: list[Any]) -> bool:
        """只删除已成功归档的主键；分片避开 SQLite 的变量数量上限。"""
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            if not self._execute(f"DELETE FROM {table} WHERE {pk} IN ({marks})", chunk):
                return False
        return True

    def _vacuum(self) -> None:
        if self.pg:
            return
        try:
            with self._lock:
                conn = self._connect()
                try:
                    conn.isolation_level = None
                    conn.execute("VACUUM")
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001
            self._last_error = repr(exc)

    def archive_and_prune(self, retention_days: int, max_db_mb: int,
                          archive_dir: str = "") -> dict[str, Any]:
        """先完整归档、再按主键精确删除；任一步失败都保留数据，绝不按时间整批删。"""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max(1, retention_days))).isoformat()
        directory = Path(archive_dir or str(Path(self.db_path).parent / "archive"))
        result: dict[str, Any] = {
            "archived": 0, "archived_by_table": {}, "pruned": 0,
            "size_mb": 0.0, "errors": [],
        }
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result["errors"].append(f"归档目录不可用，未删除任何记录: {exc!r}")
            return result

        for table, time_col, pk in ARCHIVE_TABLES:
            seq = 0
            while True:
                rows = self._query(
                    f"SELECT * FROM {table} WHERE {time_col} < ? "
                    f"ORDER BY {time_col}, {pk} LIMIT ?",
                    [cutoff, ARCHIVE_BATCH],
                )
                if not rows:
                    break
                seq += 1
                try:
                    archived_path = self._write_archive(rows, directory, table, seq)
                    checked = self.verify_archive(str(archived_path))
                    if not checked["ok"]:
                        raise OSError(f"归档删前二次校验失败: {checked['reason']}")
                except OSError as exc:
                    result["errors"].append(f"{table} 归档失败，已保留原记录: {exc!r}")
                    break
                if not self._delete_ids(table, pk, [r[pk] for r in rows]):
                    result["errors"].append(f"{table} 精确删除失败，归档副本仍在")
                    break
                result["archived"] += len(rows)
                result["archived_by_table"][table] = (
                    result["archived_by_table"].get(table, 0) + len(rows))
                if len(rows) < ARCHIVE_BATCH:
                    break

        size_mb = self._db_size_mb()
        if size_mb > max_db_mb > 0:
            seq = 0
            while size_mb > max_db_mb:
                rows = self._query(
                    "SELECT * FROM tool_calls ORDER BY timestamp_start, id LIMIT ?",
                    [ARCHIVE_BATCH],
                )
                if not rows:
                    break
                seq += 1
                try:
                    archived_path = self._write_archive(
                        rows, directory, "tool_calls-quota", seq)
                    checked = self.verify_archive(str(archived_path))
                    if not checked["ok"]:
                        raise OSError(f"归档删前二次校验失败: {checked['reason']}")
                except OSError as exc:
                    result["errors"].append(f"配额归档失败，已保留原记录: {exc!r}")
                    break
                if not self._delete_ids("tool_calls", "id", [r["id"] for r in rows]):
                    result["errors"].append("配额精确删除失败，归档副本仍在")
                    break
                result["pruned"] += len(rows)
                self._vacuum()
                size_mb = self._db_size_mb()
                if len(rows) < ARCHIVE_BATCH:
                    break
        result["size_mb"] = round(size_mb, 2)
        return result

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
