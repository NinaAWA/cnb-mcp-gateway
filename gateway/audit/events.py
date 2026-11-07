"""统一事件模型与请求追踪上下文。

设计要点：
- request_id 每次调用唯一；trace_id 用于把 MCP / REST / 后台任务串成一条链。
- client_id 来自**已验证**的令牌指纹；declared_client 只是客户端自报名称，
  仅用于展示，绝不用于鉴权或身份判定。
- 后台任务拥有独立的生命周期事件，不与「启动工具调用」的结果混为一谈。
"""

from __future__ import annotations

import contextvars
import datetime as dt
import secrets
from dataclasses import dataclass, field
from typing import Any

TOOL_STATUSES = (
    "RECEIVED", "RUNNING", "SUCCESS", "FAILED", "DENIED",
    "TIMEOUT", "CANCELLED", "INTERRUPTED",
)

TERMINAL_STATUSES = frozenset(
    {"SUCCESS", "FAILED", "DENIED", "TIMEOUT", "CANCELLED", "INTERRUPTED"}
)

# V3 新增事件类别 / 状态。保持既有 TOOL_STATUSES 语义不变。
EVENT_KINDS = (
    "tool_call", "task_lifecycle", "admin_auth", "admin_export", "service_start",
    "search_event", "paper_download", "paper_read", "file_transfer",
    "host_agent", "approval", "audit_strict_violation", "audit_storage_warning",
    "audit_archive", "audit_archive_warning", "audit_archive_restore",
    "oauth_persistence_warning", "oauth_revoke", "audit_unavailable",
)
SOURCES = ("mcp", "rest", "admin", "task", "system", "host-agent", "external",
           "audit-storage", "oauth", "container")
DOWNLOAD_STATUSES = ("created", "downloading", "verifying", "success",
                     "failed", "cancelled", "duplicate")
TRANSFER_STATUSES = ("created", "uploading", "downloading", "verifying",
                     "success", "failed", "cancelled")


class StrictAuditDenied(RuntimeError):
    """统一严格审计门拒绝执行；调用方必须把 DENIED 终态如实返回给客户端。"""


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str = "") -> str:
    value = secrets.token_hex(12)
    return f"{prefix}{value}" if prefix else value


@dataclass
class RequestTrace:
    """一次调用贯穿全链路的追踪信息。"""

    request_id: str = field(default_factory=lambda: new_id("req_"))
    trace_id: str = field(default_factory=lambda: new_id("trc_"))
    client_id: str = "unknown"          # 已验证身份：令牌指纹，非明文令牌
    actor_id: str = "unknown"           # 管理员 / 令牌身份标识
    declared_client: str = ""           # 客户端自称，未验证
    session_id: str = ""
    source: str = "mcp"                 # mcp | rest | task | host-agent
    transport: str = "streamable-http"
    remote_ip: str = ""
    started_at: str = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "client_id": self.client_id,
            "actor_id": self.actor_id,
            "declared_client": self.declared_client,
            "session_id": self.session_id,
            "source": self.source,
            "transport": self.transport,
            "remote_ip": self.remote_ip,
            "started_at": self.started_at,
        }


_TRACE: contextvars.ContextVar[RequestTrace | None] = contextvars.ContextVar(
    "mcp_audit_trace", default=None
)


def current_trace() -> RequestTrace | None:
    return _TRACE.get()


def new_trace(**kwargs: Any) -> RequestTrace:
    """创建并绑定新的追踪上下文；返回设置 token 以便必要时复位。"""
    parent = _TRACE.get()
    trace = RequestTrace()
    if parent is not None and not kwargs.get("trace_id"):
        trace.trace_id = parent.trace_id
    for key, value in kwargs.items():
        if hasattr(trace, key) and value not in (None, ""):
            setattr(trace, key, value)
    _TRACE.set(trace)
    return trace


def bind_trace(trace: RequestTrace) -> None:
    _TRACE.set(trace)


def clear_trace() -> None:
    _TRACE.set(None)


@dataclass
class ToolCallRecord:
    """tool_calls 表的一行；所有字段均为脱敏后的结构化数据。"""

    request_id: str
    trace_id: str
    timestamp_start: str
    tool_name: str
    source: str = "mcp"
    client_id: str = "unknown"
    actor_id: str = "unknown"
    declared_client: str = ""
    session_id: str = ""
    remote_ip: str = ""
    arguments: str = "{}"
    result: str = ""
    status: str = "RECEIVED"
    error_type: str = ""
    error_message: str = ""
    exit_code: int | None = None
    task_id: str = ""
    approval_id: str = ""
    git_repo: str = ""
    experiment_id: str = ""
    file_path: str = ""
    duration_ms: int = 0
    timestamp_end: str = ""
    result_ref: str = ""
    result_size: int = 0
    result_sha256: str = ""
    strict_denied: int = 0

    def to_row(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "timestamp_start": self.timestamp_start,
            "timestamp_end": self.timestamp_end,
            "duration_ms": self.duration_ms,
            "client_id": self.client_id,
            "actor_id": self.actor_id,
            "declared_client": self.declared_client,
            "session_id": self.session_id,
            "remote_ip": self.remote_ip,
            "source": self.source,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "result": self.result,
            "status": self.status,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "exit_code": self.exit_code,
            "task_id": self.task_id,
            "approval_id": self.approval_id,
            "git_repo": self.git_repo,
            "experiment_id": self.experiment_id,
            "file_path": self.file_path,
            "result_ref": self.result_ref,
            "result_size": self.result_size,
            "result_sha256": self.result_sha256,
            "strict_denied": self.strict_denied,
        }
