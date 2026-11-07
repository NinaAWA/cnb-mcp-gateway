"""统一调用采集层：在 FastMCP 工具分发层拦截，不改动任何工具内部。

覆盖三类入口：
- MCP tools/call：AuditMiddleware.on_call_tool（FastMCP 服务端中间件）
- REST /run_command：trace_call 上下文管理器
- 后台任务生命周期：task_lifecycle（与启动调用相互独立的事件）

客户端自称名称只写入 declared_client，绝不作为已验证身份；client_id 来自
Bearer/OAuth 令牌的不可逆指纹。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from typing import Any, Iterator

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult

from .enrich import derive_status, enrich, extract_exit_code, result_text
from .events import (
    RequestTrace, StrictAuditDenied, ToolCallRecord, bind_trace, current_trace,
    new_trace, utcnow,
)
from .redaction import fingerprint, redact_json, redact_params, redact_text
from .storage import HIGH_RISK_TOOLS, STRICT, get_storage

_MAX_PREVIEW = 200_000
_AUDIT_DENIED_MESSAGE = "审计存储不可用，严格审计模式已拒绝执行该高风险操作"


def strict_gate(tool_name: str, request_id: str, source: str,
                storage: Any | None = None) -> None:
    """MCP / REST / 后台任务共用的唯一严格审计门。

    只有「DB 健康 + 内存缓冲与磁盘 spool 均为空 + RECEIVED 行可读回」才放行。
    拒绝时先如实写入 DENIED 终态与告警事件，再抛出专用异常。
    """
    if not STRICT or tool_name not in HIGH_RISK_TOOLS:
        return
    store = storage or get_storage()
    if store.confirm_persisted(request_id):
        return
    store.update_call(request_id, status="DENIED", timestamp_end=utcnow(),
                      strict_denied=1, error_type="AuditUnavailable",
                      error_message=_AUDIT_DENIED_MESSAGE)
    store.add_event("tool_call", status="DENIED", request_id=request_id,
                    tool_name=tool_name, source=source,
                    detail=_AUDIT_DENIED_MESSAGE)
    store.note_strict_violation(request_id, tool_name, "入口")
    raise StrictAuditDenied(_AUDIT_DENIED_MESSAGE)


def _token_expiry() -> str:
    """从已验证的 Bearer 令牌查询持久化的过期时间（ISO），失败返回空。"""
    headers = _headers()
    raw = headers.get("authorization", "")
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    if not token:
        return ""
    try:
        from gateway.oauth.store import get_oauth_store
        return get_oauth_store().token_expiry_iso(token)
    except Exception:  # noqa: BLE001 - 审计字段不可用不得影响调用
        return ""


def _insert_redacted(table: str, values: dict[str, Any]) -> bool:
    """结构化事件的统一写入口：字段与值都经过既有脱敏规则。"""
    values.setdefault("ts", utcnow())
    keys = list(values)
    marks = ",".join("?" for _ in keys)
    return get_storage()._execute(
        f"INSERT INTO {table} ({','.join(keys)}) VALUES ({marks})",
        redact_params([values[k] for k in keys]),
    )


def record_search_event(values: dict[str, Any]) -> bool:
    return _insert_redacted("search_events", values)


def record_download_event(values: dict[str, Any]) -> bool:
    return _insert_redacted("paper_downloads", values)


def record_transfer_event(values: dict[str, Any]) -> bool:
    return _insert_redacted("file_transfers", values)


def record_task_state(values: dict[str, Any]) -> bool:
    return _insert_redacted("task_state", values)


def record_host_event(status: str, *, request_id: str = "", tool_name: str = "",
                      detail: Any = "") -> None:
    get_storage().add_event("host_agent", status=status, request_id=request_id,
                            tool_name=tool_name, source="host-agent",
                            detail=redact_text(detail, limit=2000))


def bind_http_context(scope: dict[str, Any]) -> RequestTrace | None:
    """ASGI 入口：从已鉴权请求中提取追踪信息（不记录 SSE 连接本身）。"""
    if scope.get("type") != "http":
        return None
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }
    raw = headers.get("authorization", "")
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    client_id = fingerprint(token) if token else "anonymous"
    declared = (headers.get("x-mcp-client") or headers.get("x-client-name")
                or headers.get("user-agent") or "")[:120]
    client_host = ""
    if scope.get("client"):
        client_host = str(scope["client"][0])
    remote_ip = (headers.get("x-forwarded-for", "").split(",")[0].strip()
                 or client_host)[:64]
    trace = new_trace(
        trace_id=headers.get("x-trace-id", ""),
        client_id=client_id,
        actor_id=client_id,
        declared_client=declared,
        session_id=headers.get("mcp-session-id", ""),
        remote_ip=remote_ip,
        source="mcp",
    )
    return trace


def _headers() -> dict[str, str]:
    """补充读取 HTTP header（仅在请求上下文内可用，失败返回空）。"""
    try:
        from fastmcp.server.dependencies import get_http_headers
        return get_http_headers(include_all=True) or {}
    except Exception:  # noqa: BLE001
        return {}


def _identity(parent: RequestTrace | None) -> dict[str, Any]:
    headers = _headers()
    if parent is None:
        parent = RequestTrace()
        raw = headers.get("authorization", "")
        token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
        parent.client_id = fingerprint(token) if token else "anonymous"
        parent.actor_id = parent.client_id
    return {
        "trace_id": parent.trace_id,
        "client_id": parent.client_id,
        "actor_id": parent.actor_id,
        "declared_client": (parent.declared_client or headers.get("x-mcp-client")
                            or headers.get("x-client-name") or headers.get("user-agent") or "")[:120],
        "session_id": parent.session_id or headers.get("mcp-session-id", ""),
        "remote_ip": parent.remote_ip,
        "source": "mcp",
    }


def _denied_result(message: str) -> ToolResult:
    return ToolResult(content=[f"ERROR: {message}"], is_error=True)


class AuditMiddleware(Middleware):
    """所有 MCP 工具调用的统一入口/出口拦截器。"""

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        storage = get_storage()
        name = str(context.message.name)
        arguments = dict(context.message.arguments or {})
        identity = _identity(current_trace())
        trace = new_trace(**identity)
        started = time.monotonic()

        record = ToolCallRecord(
            request_id=trace.request_id,
            trace_id=trace.trace_id,
            timestamp_start=trace.started_at,
            tool_name=name,
            source="mcp",
            client_id=trace.client_id,
            actor_id=trace.actor_id,
            declared_client=trace.declared_client,
            session_id=trace.session_id,
            remote_ip=trace.remote_ip,
            arguments=redact_json(arguments),
            status="RECEIVED",
        )
        storage.insert_call(record)
        storage.add_event("tool_call", status="RECEIVED", request_id=trace.request_id,
                          trace_id=trace.trace_id, tool_name=name,
                          actor_id=trace.actor_id, client_id=trace.client_id,
                          source="mcp", detail={"arguments": redact_json(arguments)[:2000]})
        storage.upsert_session(trace.session_id or trace.request_id,
                               trace.client_id, trace.declared_client,
                               token_expires=_token_expiry())

        # 严格审计门：MCP / REST / 后台共用 strict_gate，判定口径完全一致
        try:
            strict_gate(name, trace.request_id, "mcp", storage)
        except StrictAuditDenied as exc:
            return _denied_result(str(exc))

        storage.update_call(trace.request_id, status="RUNNING")
        result: Any = None
        status = "SUCCESS"
        error_type = ""
        error_message = ""
        exception: BaseException | None = None
        try:
            result = await call_next(context)
        except asyncio.CancelledError as exc:  # 客户端断开 / 服务关闭
            status, error_type, error_message = "CANCELLED", "CancelledError", "调用被取消"
            exception = exc
        except ToolError as exc:
            status, error_type = "FAILED", type(exc).__name__
            error_message = redact_text(str(exc), limit=2000)
            exception = exc
        except Exception as exc:  # noqa: BLE001 - 记录后必须原样抛出
            status, error_type = "FAILED", type(exc).__name__
            error_message = redact_text(repr(exc), limit=2000)
            if "timeout" in error_type.lower() or "超时" in error_message:
                status = "TIMEOUT"
            exception = exc
        finally:
            text = "" if result is None else result_text(result)
            is_error = bool(getattr(result, "is_error", False)) if result is not None else True
            if exception is None:
                exit_code = extract_exit_code(text)
                status = derive_status(name, text, exit_code, is_error)
            else:
                exit_code = None
            safe = redact_text(text, limit=_MAX_PREVIEW)
            stored = storage.artifacts.store(safe)
            derived = enrich(
                {"request_id": trace.request_id, "trace_id": trace.trace_id,
                 "tool_name": name, "client_id": trace.client_id,
                 "status": status, "error_message": error_message},
                arguments, text,
            )
            patch = {
                "timestamp_end": utcnow(),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "status": status,
                "result": stored["text"][:32768],
                "result_ref": stored["ref"],
                "result_size": stored["size"],
                "result_sha256": stored["sha256"],
                "error_type": error_type,
                "error_message": error_message[:4000],
                "exit_code": exit_code,
            }
            for key, value in derived.items():
                if key != "sub_events" and value not in (None, ""):
                    patch[key] = value
            if not storage.update_call(trace.request_id, **patch) and STRICT:
                storage.note_strict_violation(trace.request_id, name, "终态")
            for kind, values in derived.get("sub_events", []):
                if kind == "git":
                    values["status"] = status
                    storage.add_git(values)
                elif kind == "experiment":
                    storage.add_experiment(values)
            storage.add_event(
                "tool_call", status=status, request_id=trace.request_id,
                trace_id=trace.trace_id, tool_name=name,
                actor_id=trace.actor_id, client_id=trace.client_id, source="mcp",
                detail={"duration_ms": patch["duration_ms"], "exit_code": exit_code,
                        "error_type": error_type},
            )
        if exception is not None:
            raise exception
        return result


class CallRecorder:
    """REST / 同步入口的记录器。"""

    def __init__(self, request_id: str, trace_id: str, tool_name: str,
                 arguments: dict[str, Any], trace: RequestTrace):
        self.storage = get_storage()
        self.request_id = request_id
        self.trace_id = trace_id
        self.tool_name = tool_name
        self.arguments = arguments or {}
        self.trace = trace
        self.started = time.monotonic()
        self.status = "SUCCESS"
        self.error_type = ""
        self.error_message = ""
        self.result = ""
        self.extra: dict[str, Any] = {}

    def set(self, **fields: Any) -> None:
        self.extra.update(fields)

    def ok(self, result: str, status: str | None = None) -> None:
        self.result = result or ""
        if status:
            self.status = status

    def fail(self, exc: BaseException) -> None:
        self.status = "FAILED"
        self.error_type = type(exc).__name__
        self.error_message = redact_text(repr(exc), limit=2000)

    def finish(self) -> None:
        text = self.result
        if not self.status or self.status == "SUCCESS":
            exit_code = extract_exit_code(text)
            self.status = derive_status(self.tool_name, text, exit_code, False)
            if exit_code is not None:
                self.extra.setdefault("exit_code", exit_code)
        stored = self.storage.artifacts.store(redact_text(text, limit=_MAX_PREVIEW))
        derived = enrich(
            {"request_id": self.request_id, "trace_id": self.trace_id,
             "tool_name": self.tool_name, "client_id": self.trace.client_id,
             "status": self.status, "error_message": self.error_message},
            self.arguments, text,
        )
        patch = {
            "timestamp_end": utcnow(),
            "duration_ms": int((time.monotonic() - self.started) * 1000),
            "status": self.status,
            "result": stored["text"][:32768],
            "result_ref": stored["ref"],
            "result_size": stored["size"],
            "result_sha256": stored["sha256"],
            "error_type": self.error_type,
            "error_message": self.error_message[:4000],
            **self.extra,
        }
        for key, value in derived.items():
            if key != "sub_events" and value not in (None, ""):
                patch.setdefault(key, value)
        self.storage.update_call(self.request_id, **patch)
        for kind, values in derived.get("sub_events", []):
            if kind == "git":
                values["status"] = self.status
                self.storage.add_git(values)
        self.storage.add_event("tool_call", status=self.status,
                               request_id=self.request_id, trace_id=self.trace_id,
                               tool_name=self.tool_name, actor_id=self.trace.actor_id,
                               client_id=self.trace.client_id, source=self.trace.source,
                               detail={"duration_ms": patch["duration_ms"]})


@contextmanager
def trace_call(tool_name: str, arguments: dict[str, Any] | None = None,
               source: str = "rest") -> Iterator[CallRecorder]:
    """REST 等绕过 tools/call 的入口使用；与 MCP 共用同一张表。"""
    parent = current_trace()
    identity = _identity(parent)
    identity["source"] = source
    trace = new_trace(**identity)
    bind_trace(trace)
    storage = get_storage()
    record = ToolCallRecord(
        request_id=trace.request_id, trace_id=trace.trace_id,
        timestamp_start=trace.started_at, tool_name=tool_name, source=source,
        client_id=trace.client_id, actor_id=trace.actor_id,
        declared_client=trace.declared_client, session_id=trace.session_id,
        remote_ip=trace.remote_ip, arguments=redact_json(arguments or {}),
        status="RECEIVED",
    )
    storage.insert_call(record)
    storage.add_event("tool_call", status="RECEIVED", request_id=trace.request_id,
                      trace_id=trace.trace_id, tool_name=tool_name,
                      actor_id=trace.actor_id, client_id=trace.client_id,
                      source=source, detail={"arguments": redact_json(arguments or {})[:2000]})
    recorder = CallRecorder(trace.request_id, trace.trace_id, tool_name,
                            arguments or {}, trace)
    # REST 入口原先直接以 RUNNING 落库、完全绕过严格审计门；此处与 MCP 共用同一判定。
    strict_gate(tool_name, trace.request_id, source, storage)
    storage.update_call(trace.request_id, status="RUNNING")
    try:
        yield recorder
    except Exception as exc:  # noqa: BLE001
        recorder.fail(exc)
        recorder.finish()
        raise
    else:
        recorder.finish()


def task_lifecycle(task_id: str, status: str, *, request_id: str = "",
                   command: str = "", cwd: str = "", pid: int | None = None,
                   log_path: str = "", detail: str = "") -> None:
    """后台任务独立生命周期：启动调用成功 != 训练任务运行结果。"""
    storage = get_storage()
    parent = current_trace()
    trace_id = parent.trace_id if parent else ""
    values = {
        "task_id": task_id, "request_id": request_id, "trace_id": trace_id,
        "status": status, "command": redact_text(command, limit=2000),
        "cwd": cwd, "pid": pid, "log_path": log_path,
        "detail": redact_text(detail, limit=1000),
    }
    storage.add_task(values)
    storage.add_event("task_lifecycle", status=status.upper(), request_id=request_id,
                      trace_id=trace_id, tool_name="run_command_background",
                      source="task", detail={"task_id": task_id, "detail": values["detail"]})
    if task_id:
        storage.add_experiment({
            "experiment_id": task_id, "task_id": task_id, "request_id": request_id,
            "trace_id": trace_id, "name": (command.strip().split() or ["task"])[0].rsplit("/", 1)[-1],
            "project_path": cwd, "command": redact_text(command, limit=2000),
            "status": status, "pid": pid, "log_path": log_path,
            "detail": "训练任务生命周期事件，与启动工具调用结果分开记录",
        })
        storage.update_call(request_id, task_id=task_id, experiment_id=task_id) if request_id else None


def record_event(kind: str, *, status: str = "", request_id: str = "",
                 tool_name: str = "", detail: Any = "", source: str = "system") -> None:
    """认证、配置变更等事件的统一入口。"""
    parent = current_trace()
    get_storage().add_event(
        kind, status=status, request_id=request_id,
        trace_id=parent.trace_id if parent else "", tool_name=tool_name,
        actor_id=parent.actor_id if parent else "system",
        client_id=parent.client_id if parent else "",
        source=source, detail=redact_text(detail, limit=2000),
    )
