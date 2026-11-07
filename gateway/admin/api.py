"""Admin Web FastAPI 应用：独立认证 + 仪表盘 / 实时 / 历史 / 详情 / 导出。

与 MCP 网关分进程运行：后台不可用不会中断已启动的科研任务；所有事件都从
共享的 SQLite(WAL) 读取，SSE 只推送已落库的真实工具调用事件。
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
    RedirectResponse, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from gateway.audit.redaction import redact_structure, redact_text
from gateway.audit.storage import SESSION_TTL, get_storage
from gateway.monitoring.system import collect
from gateway.transfer.http_api import iter_file, parse_range
from .auth import (
    COOKIE_NAME, CSRF_COOKIE_NAME, CSRF_EXEMPT_PATHS, CSRF_SAFE_METHODS,
    csrf_cookie, csrf_valid, get_auth, new_csrf_token,
)


class AdminCsrfMiddleware:
    """纯 ASGI 双提交 Cookie 校验，避免 BaseHTTPMiddleware 干扰 SSE / 流式导出。"""

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _headers(scope) -> dict[str, str]:
        return {key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])}

    @staticmethod
    def _cookie(headers: dict[str, str], name: str) -> str:
        from http.cookies import SimpleCookie
        jar = SimpleCookie()
        try:
            jar.load(headers.get("cookie", ""))
        except Exception:  # noqa: BLE001
            return ""
        item = jar.get(name)
        return item.value if item else ""

    @staticmethod
    def _with_cookie(message: dict, cookie: str) -> dict:
        headers = list(message.get("headers") or [])
        headers.append((b"set-cookie", cookie.encode("latin-1")))
        return {**message, "headers": headers}

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = self._headers(scope)
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        existing = self._cookie(headers, CSRF_COOKIE_NAME)
        token = existing or new_csrf_token()
        cookie = csrf_cookie(COOKIE_SECURE).format(token=token) if not existing else ""
        needs_check = (method not in CSRF_SAFE_METHODS and path.startswith("/api/")
                       and path not in CSRF_EXEMPT_PATHS)
        if needs_check:
            # 首次请求尚无 Cookie 时，用刚生成的 token 组成响应头供客户端重试。
            probe = dict(headers)
            if not existing:
                probe["cookie"] = (headers.get("cookie", "") + "; " if headers.get("cookie") else "") \
                    + f"{CSRF_COOKIE_NAME}={token}"
            ok, reason = csrf_valid(probe)
            if not ok:
                body = json.dumps({"ok": False, "detail": reason},
                                  ensure_ascii=False).encode("utf-8")
                start = {
                    "type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())],
                }
                if cookie:
                    start = self._with_cookie(start, cookie)
                await send(start)
                await send({"type": "http.response.body", "body": body})
                return
        if not cookie:
            await self.app(scope, receive, send)
            return

        async def send_with_cookie(message):
            if message.get("type") == "http.response.start":
                message = self._with_cookie(message, cookie)
            await send(message)

        await self.app(scope, receive, send_with_cookie)

BASE_DIR = Path(__file__).parent
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://mcp-gateway:8765").rstrip("/")
EXPORT_DIR = Path(os.environ.get("AUDIT_EXPORT_DIR", "/data/audit/exports"))
TASK_LOG_DIR = Path(os.environ.get("MCP_TASK_LOG_DIR", "/tmp/mcp_tasks")).resolve()
COOKIE_SECURE = os.environ.get("ADMIN_COOKIE_SECURE", "0").strip().lower() in ("1", "true", "yes")
METRICS_TOKEN_FILE = Path(
    os.environ.get("GATEWAY_METRICS_TOKEN_FILE", "/data/audit/.metrics_token"))

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    auth = get_auth()
    if auth.generated_password:
        print("=" * 70, flush=True)
        print(" [自动生成] Admin Web 管理员密码（未配置 ADMIN_PASSWORD 时随机生成）:", flush=True)
        print(f"   账号: {auth.username}", flush=True)
        print(f"   密码: {auth.generated_password}", flush=True)
        print("=" * 70, flush=True)
    get_storage().add_event("admin_service", status="UP", source="admin",
                            detail="管理后台已启动")
    yield


app = FastAPI(title="MCP Admin Dashboard", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.add_middleware(AdminCsrfMiddleware)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _render(name: str, request: Request, **context: Any) -> HTMLResponse:
    """直接渲染模板，避免不同 Starlette 版本 TemplateResponse 签名不兼容。"""
    return HTMLResponse(templates.env.get_template(name).render(
        {"request": request, **context}
    ))

CONFIG_WHITELIST = (
    "MCP_PORT", "MCP_BIND", "MCP_WORKDIR", "MCP_ALLOWED_PREFIXES",
    "MCP_SEARCH_TIMEOUT", "MCP_SEARCH_MAX_RESULTS", "MCP_SEARCH_RETRIES",
    "MCP_SEARCH_CACHE_TTL", "MCP_SEARCH_ARXIV_MIN_INTERVAL",
    "MCP_SEARCH_WEB_PROVIDERS", "MCP_SEARCH_FETCH_ENABLED",
    "AUDIT_STRICT", "AUDIT_RETENTION_DAYS", "AUDIT_MAX_DB_MB",
    "AUDIT_ARCHIVE_BATCH", "AUDIT_BUFFER_MAX", "AUDIT_MAX_FIELD_CHARS",
    "MCP_SESSION_TTL",
    "AUDIT_REQUIRED", "AUDIT_ALLOW_UNAUDITED", "AUDIT_SPOOL_ENABLED",
    "AUDIT_SPOOL_MAX_MB", "AUDIT_SPOOL_MAX_FILES", "OAUTH_PERSIST_ENABLED",
    "MCP_SEARCH_PIN_DNS", "MCP_SEARCH_SEARXNG_URL",
    "MCP_PAPER_DIR", "MCP_PAPER_MAX_MB", "MCP_DOWNLOAD_CONCURRENCY",
    "MCP_PAPER_ALLOW_PRIVATE", "MCP_PAPER_TEXT_CACHE_DIR",
    "MCP_PAPER_EXTRA_HOSTS",
    "MCP_ACCESS_MODE", "MCP_DANGER_GUARD", "MCP_INDEPENDENT_CONFIRM",
    "MCP_EXEC_BACKEND", "MCP_HOST_AGENT_SOCKET", "MCP_HOST_AGENT_ROOTS",
    "MCP_TRANSFER_DIR", "MCP_TRANSFER_ALLOW_PRIVATE", "TRANSFER_MAX_MB",
    "MCP_TASK_LOG_DIR",
    "MCP_OAUTH_TOKEN_TTL", "OAUTH_ALLOW_ANY_REDIRECT", "OAUTH_ALLOW_NO_PKCE",
    "ADMIN_SESSION_TTL", "ADMIN_LOGIN_MAX_FAILURES", "ADMIN_LOGIN_LOCK_MINUTES",
    "ADMIN_MAX_SESSIONS",
)
SENSITIVE_CONFIG = (
    "MCP_TOKEN", "ADMIN_PASSWORD", "ADMIN_SESSION_SECRET", "ADMIN_TOTP_SECRET",
    "CF_TUNNEL_TOKEN", "MCP_SEARCH_TAVILY_KEY", "MCP_SEARCH_MAILTO",
    "MCP_SEARCH_SEARXNG_URL", "AUDIT_DB_DSN", "GATEWAY_METRICS_TOKEN",
    "MCP_TRANSFER_SECRET", "MCP_HOST_AGENT_TOKEN",
)
EDITABLE_SETTINGS = ("default_range", "page_size", "theme")


# --------------------------------------------------------------------- auth
def _ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return (forwarded or (request.client.host if request.client else "unknown"))[:64]


def _session(request: Request) -> dict[str, Any] | None:
    return get_auth().session(request.cookies.get(COOKIE_NAME, ""))


def require_admin(request: Request) -> dict[str, Any]:
    session = _session(request)
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return session


def _page_or_login(request: Request):
    return _session(request)


class LoginBody(BaseModel):
    username: str
    password: str
    otp: str = ""


class ConfigBody(BaseModel):
    key: str
    value: str = ""
    password: str = ""


class ExportBody(BaseModel):
    fmt: str = "jsonl"
    filters: dict[str, Any] = {}


@app.get("/healthz")
def healthz():
    """容器 healthcheck：只暴露后台自身与审计存储状态，不含调用数据。"""
    return {"status": "ok", "service": "mcp-admin-web",
            "audit": get_storage().health()}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _session(request):
        return RedirectResponse("/", status_code=302)
    auth = get_auth()
    return _render("login.html", request, mfa_enabled=auth.mfa_enabled,
                   username=auth.username, error="")


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    auth = get_auth()
    ok, message, cookie = auth.login(
        body.username.strip(), body.password, body.otp.strip(),
        _ip(request), request.headers.get("user-agent", "")[:250],
    )
    if not ok:
        return JSONResponse({"ok": False, "message": message}, status_code=401)
    response = JSONResponse({"ok": True, "message": message})
    response.set_cookie(
        COOKIE_NAME, cookie, httponly=True, samesite="lax",
        secure=COOKIE_SECURE, max_age=auth.ttl_seconds, path="/",
    )
    return response


@app.post("/api/logout")
def logout(request: Request):
    get_auth().logout(request.cookies.get(COOKIE_NAME, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/api/me")
def me(session: dict = Depends(require_admin)):
    return {"username": session["username"], "mfa_enabled": get_auth().mfa_enabled,
            "expires_at": session["expires_at"]}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _page_or_login(request):
        return RedirectResponse("/login", status_code=302)
    return _render("index.html", request, username=_session(request)["username"],
                   mfa_enabled=get_auth().mfa_enabled)


# ---------------------------------------------------------------- utilities
def _range(request: Request) -> tuple[str, str, str]:
    now = datetime.now(timezone.utc)
    which = request.query_params.get("range", "24h")
    if which == "custom":
        since = request.query_params.get("since", "")
        until = request.query_params.get("until", "")
        try:
            start = datetime.fromisoformat(since.replace("Z", "+00:00"))
            end = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            return (start.astimezone(timezone.utc).isoformat(),
                    end.astimezone(timezone.utc).isoformat(), "自定义")
        except ValueError:
            which = "24h"
    hours = {"1h": 1, "24h": 24, "7d": 24 * 7, "30d": 24 * 30}.get(which, 24)
    label = {"1h": "最近 1 小时", "24h": "最近 24 小时", "7d": "最近 7 天",
             "30d": "最近 30 天"}.get(which, "最近 24 小时")
    return (now - timedelta(hours=hours)).isoformat(), now.isoformat(), label


def _metrics_token() -> str:
    """内部只读指标令牌：优先环境变量，其次共享卷文件；绝不使用 MCP_TOKEN。"""
    configured = os.environ.get("GATEWAY_METRICS_TOKEN", "").strip()
    if configured:
        return configured
    try:
        return METRICS_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _gateway_health() -> dict[str, Any]:
    """先取需鉴权的完整详情；令牌缺失或鉴权失败时降级到最小化的 /health。"""
    fallback_reason = "缺少内部指标令牌"
    token = _metrics_token()
    if token:
        request = urllib.request.Request(
            GATEWAY_URL + "/health/details",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            data["detail_level"] = "full"
            return data
        except Exception as exc:  # noqa: BLE001
            fallback_reason = f"详情接口不可用: {exc!r}"[:200]
    try:
        with urllib.request.urlopen(GATEWAY_URL + "/health", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        data["detail_level"] = "minimal"
        data["detail_fallback"] = fallback_reason
        return data
    except Exception as exc:  # noqa: BLE001
        return {"status": "down", "error": repr(exc)[:200], "source": "mcp-gateway",
                "detail_level": "minimal", "detail_fallback": fallback_reason}


def _filters(request: Request) -> dict[str, Any]:
    allowed = ("since", "until", "tool_name", "status", "client_id", "actor_id",
               "request_id", "task_id", "git_repo", "experiment_id", "keyword",
               "file_path", "source")
    return {key: request.query_params[key] for key in allowed
            if request.query_params.get(key)}


def _safe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """读取侧兜底：修复前落库的存量明文行也不会经后台接口外泄。"""
    return [redact_structure(row) for row in rows]


# ------------------------------------------------------------------ dashboard
@app.get("/api/dashboard")
def dashboard(request: Request, session: dict = Depends(require_admin)):
    storage = get_storage()
    since, until, label = _range(request)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    stats = storage.stats(since, until)
    gateway = _gateway_health()
    monitoring = collect("admin-container")
    monitoring["gateway_metrics"] = gateway.get("metrics")
    monitoring["gateway_metrics_source"] = (
        "mcp-gateway 容器（来自 /health/details，需鉴权）" if gateway.get("detail_level") == "full"
        else "详情接口不可用，已降级到最小化 /health")
    return {
        "range": {"label": label, "since": since, "until": until},
        "gateway": {
            "online": gateway.get("status") == "ok",
            "started_at": gateway.get("started_at", ""),
            "uptime_seconds": gateway.get("uptime_seconds", 0),
            "tools_registered": gateway.get("tools_registered"),
            "detail": gateway,
        },
        "stats": stats,
        "today": storage.stats(today.isoformat(), until)["total"],
        "last_24h": storage.stats(
            (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(), until)["total"],
        "active_tasks": storage.active_tasks(),
        "active_sessions": storage.active_session_count(),
        "time_series": storage.time_series(since, until),
        "duration_distribution": storage.duration_distribution(since),
        "monitoring": monitoring,
        "recent_errors": storage.recent_errors(12),
        "audit": storage.health(),
    }


@app.get("/api/monitoring")
def monitoring(session: dict = Depends(require_admin)):
    gateway = _gateway_health()
    return {
        "admin_container": collect("admin-container"),
        "gateway_container": gateway.get("metrics"),
        "gateway_online": gateway.get("status") == "ok",
        "scope_note": "两份指标分别来自管理后台容器与 MCP 网关容器，均非宿主机全机指标；"
                      "命令实际在 MCP 网关容器内执行。",
    }


# ---------------------------------------------------------------------- calls
@app.get("/api/calls")
def calls(request: Request, session: dict = Depends(require_admin)):
    storage = get_storage()
    limit = min(200, max(1, int(request.query_params.get("limit", "50"))))
    offset = max(0, int(request.query_params.get("offset", "0")))
    order = request.query_params.get("order", "desc")
    sort = request.query_params.get("sort", "timestamp_start")
    rows, total = storage.query_calls(_filters(request), limit=limit,
                                      offset=offset, order=order, sort=sort)
    return {"rows": _safe_rows(rows), "total": total, "limit": limit, "offset": offset}


@app.get("/api/calls/{request_id}")
def call_detail(request_id: str, session: dict = Depends(require_admin)):
    storage = get_storage()
    row = storage.get_call(request_id)
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    result = row.get("result", "")
    if row.get("result_ref"):
        full = storage.artifacts.read(row["result_ref"])
        if full is not None:
            result = full
    related_tasks = storage.tasks(task_id=row.get("task_id", "")) if row.get("task_id") else []
    git = storage.git_for_request(request_id)
    experiments = storage.experiments_for_request(request_id, row.get("task_id", ""))
    return {
        "call": {**redact_structure(row), "result": redact_text(result)},
        "timeline": storage.call_timeline(request_id, row.get("trace_id", "")),
        "tasks": related_tasks, "git": git, "experiments": experiments,
        "source_note": "参数与输出已脱敏；大输出以受保护引用保存。",
    }


@app.get("/api/live/recent")
def live_recent(request: Request, session: dict = Depends(require_admin)):
    after = int(request.query_params.get("after", "0"))
    events = get_storage().recent_events(after, 100)
    return {"events": _safe_rows(events),
            "last_id": events[-1]["id"] if events else after}


@app.get("/api/live/stream")
async def live_stream(request: Request, session: dict = Depends(require_admin)):
    """SSE：只推送已落库的真实调用事件，不把 SSE 连接算作工具执行。"""
    storage = get_storage()
    last = storage.max_event_id()

    async def generator():
        nonlocal last
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                break
            events = storage.recent_events(last, 100)
            for event in events:
                last = event["id"]
                if event["kind"] in ("tool_call", "task_lifecycle", "admin_auth"):
                    safe = redact_structure(event)
                    yield "data: " + json.dumps(
                        {k: str(v) if not isinstance(v, (int, float, bool, type(None))) else v
                         for k, v in safe.items()}, ensure_ascii=False
                    ) + "\n\n"
            if not events:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        generator(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@app.get("/api/git")
def git_ops(request: Request, session: dict = Depends(require_admin)):
    return {"rows": _safe_rows(get_storage().git_ops(
        request.query_params.get("repo", ""), 200))}


@app.get("/api/experiments")
def experiments(session: dict = Depends(require_admin)):
    return {"rows": _safe_rows(get_storage().experiments(200))}


@app.get("/api/experiments/{task_id}/logs")
def experiment_logs(task_id: str, request: Request,
                    session: dict = Depends(require_admin)):
    """读取实验日志：路径由后台任务记录，严格限制在任务日志目录内。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
        raise HTTPException(status_code=400, detail="非法任务 ID")
    rows = get_storage().tasks(task_id=task_id)
    raw_path = next((r["log_path"] for r in reversed(rows) if r.get("log_path")), "")
    path = Path(raw_path).resolve() if raw_path else None
    if not path or TASK_LOG_DIR not in path.parents or not path.is_file():
        return {"log": "(日志不可用、容器重启后已丢失，或路径不在允许的日志目录内)"}
    tail = min(2000, max(10, int(request.query_params.get("tail", "300"))))
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
        return {"log": redact_text("\n".join(lines)), "path": str(path)}
    except OSError as exc:
        return {"log": f"(读取失败: {exc})"}


@app.get("/api/tasks")
def tasks(session: dict = Depends(require_admin)):
    return {"rows": _safe_rows(get_storage().tasks(200))}


@app.get("/api/clients")
def clients(session: dict = Depends(require_admin)):
    """客户端列表：只显示令牌指纹；活跃数按空闲/令牌有效期实时统计。"""
    storage = get_storage()
    rows = storage.sessions(200)
    return {"rows": rows, "active": sum(1 for r in rows if r["active"]),
            "total": len(rows), "ttl_seconds": SESSION_TTL}


@app.get("/api/sessions")
def admin_sessions(session: dict = Depends(require_admin)):
    return {"rows": get_auth().sessions()}


@app.delete("/api/sessions/{session_id}")
def revoke_session(session_id: str, session: dict = Depends(require_admin)):
    get_auth().revoke(session_id)
    return {"ok": True}


# --------------------------------------------------------------------- config
@app.get("/api/config")
def config(session: dict = Depends(require_admin)):
    runtime = {key: os.environ.get(key, "") for key in CONFIG_WHITELIST}
    sensitive = {key: "已配置" if os.environ.get(key) else "未配置"
                 for key in SENSITIVE_CONFIG}
    return {"runtime": runtime, "sensitive": sensitive,
            "settings": {k: get_storage().get_settings().get(k, v) for k, v in
                         {"default_range": "24h", "page_size": "50", "theme": "auto"}.items()},
            "editable": list(EDITABLE_SETTINGS),
            "note": "敏感值不返回明文；修改运行环境配置需通过环境变量并重启容器。"}


@app.post("/api/config")
def update_config(body: ConfigBody, request: Request,
                  session: dict = Depends(require_admin)):
    auth = get_auth()
    if body.key in SENSITIVE_CONFIG:
        # 敏感配置变更：必须重新认证，且只记录审计事件（实际生效需重启）
        probe = hashlib.pbkdf2_hmac(
            "sha256", body.password.encode(), auth.salt, 200_000
        )
        if not hmac.compare_digest(probe, auth.password_hash):
            get_storage().add_event("admin_config", status="DENIED", source="admin",
                                   actor_id=f"admin:{session['username']}",
                                   detail=f"重新认证失败 key={body.key}")
            raise HTTPException(status_code=403, detail="重新认证失败")
        get_storage().add_event(
            "admin_config", status="CHANGE_REQUESTED", source="admin",
            actor_id=f"admin:{session['username']}",
            detail=f"敏感配置 {body.key} 变更请求已记录；需通过环境变量修改并重启服务后生效",
        )
        return {"ok": True, "requires_restart": True,
                "message": "变更已审计；请通过环境变量修改并重启服务后生效"}
    if body.key not in EDITABLE_SETTINGS:
        raise HTTPException(status_code=400, detail="该配置项不可在后台修改")
    get_storage().set_setting(body.key, body.value[:200])
    get_storage().add_event("admin_config", status="UPDATED", source="admin",
                            actor_id=f"admin:{session['username']}",
                            detail=f"{body.key}={body.value[:100]}")
    return {"ok": True, "requires_restart": False, "message": "已保存并立即生效"}


# --------------------------------------------------------------------- export
EXPORT_COLUMNS = (
    "timestamp_start", "timestamp_end", "duration_ms", "request_id",
    "trace_id", "tool_name", "status", "client_id", "actor_id",
    "declared_client", "source", "exit_code", "task_id", "git_repo",
    "experiment_id", "error_type", "error_message", "arguments", "result",
)


def _csv_row(row: dict[str, Any]) -> dict[str, str]:
    return {k: (str(v).replace("\r", " ").replace("\n", " ") if v is not None else "")
            for k, v in row.items()}


def _safe_export_row(row: dict[str, Any]) -> dict[str, Any]:
    """兼容修复前落库的历史行：所有导出都再走一次统一脱敏入口。"""
    return redact_structure(row)


def _run_export(job_id: str, filters: dict[str, Any], fmt: str) -> None:
    """流式写文件：各格式都保持单一合法文档，避免分批拼接出多个数组/表头。"""
    storage = get_storage()
    try:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = EXPORT_DIR / f"{job_id}.{fmt}"
        count = 0
        with path.open("w", encoding="utf-8", newline="") as fh:
            if fmt == "csv":
                writer = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                for raw in storage.iter_calls(filters, batch=1000):
                    writer.writerow(_csv_row(_safe_export_row(raw)))
                    count += 1
            elif fmt == "json":
                fh.write("[\n")
                for raw in storage.iter_calls(filters, batch=1000):
                    fh.write((",\n" if count else "") +
                             json.dumps(_safe_export_row(raw), ensure_ascii=False,
                                        indent=2, default=str))
                    count += 1
                fh.write("\n]\n")
            else:
                for raw in storage.iter_calls(filters, batch=1000):
                    fh.write(json.dumps(_safe_export_row(raw), ensure_ascii=False,
                                        default=str) + "\n")
                    count += 1
        os.chmod(path, 0o600)
        storage.set_export(job_id, status="done", row_count=count, file_path=str(path))
        storage.add_event("admin_export", status="DONE", source="admin",
                          detail=f"job={job_id} rows={count} fmt={fmt}")
    except Exception as exc:  # noqa: BLE001
        storage.set_export(job_id, status="failed", detail=repr(exc)[:300])


@app.post("/api/exports")
def create_export(body: ExportBody, request: Request,
                  session: dict = Depends(require_admin)):
    if body.fmt not in ("csv", "json", "jsonl"):
        raise HTTPException(status_code=400, detail="不支持的导出格式")
    storage = get_storage()
    job_id = "exp_" + hashlib.sha256(
        f"{time.time()}{session['username']}".encode()).hexdigest()[:16]
    storage._insert("export_jobs", {
        "job_id": job_id, "ts": datetime.now(timezone.utc).isoformat(),
        "fmt": body.fmt, "filters": json.dumps(body.filters, ensure_ascii=False),
        "status": "running", "row_count": 0, "file_path": "", "detail": "",
    })
    threading.Thread(target=_run_export, args=(job_id, body.filters, body.fmt),
                     daemon=True).start()
    storage.add_event("admin_export", status="CREATED", source="admin",
                      actor_id=f"admin:{session['username']}",
                      detail=f"job={job_id} fmt={body.fmt}")
    return {"ok": True, "job_id": job_id}


@app.get("/api/exports")
def list_exports(session: dict = Depends(require_admin)):
    return {"rows": get_storage().list_exports()}


def _download_token(job_id: str) -> str:
    expiry = int(time.time()) + 600
    mac = hmac.new(get_auth().session_secret,
                   f"{job_id}:{expiry}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{expiry}.{mac}"


@app.get("/api/exports/{job_id}/link")
def export_link(job_id: str, session: dict = Depends(require_admin)):
    return {"url": f"/api/exports/{job_id}/download?token={_download_token(job_id)}"}


@app.get("/api/exports/{job_id}/download")
def download_export(job_id: str, request: Request):
    """认证下载链接：会话 cookie 或 10 分钟有效的 HMAC 签名 token。"""
    storage = get_storage()
    if not _session(request):
        token = request.query_params.get("token", "")
        if "." not in token:
            raise HTTPException(status_code=401, detail="下载链接未认证")
        expiry, mac = token.split(".", 1)
        expected = hmac.new(get_auth().session_secret,
                            f"{job_id}:{expiry}".encode(),
                            hashlib.sha256).hexdigest()[:24]
        if not expiry.isdigit() or int(expiry) < time.time() or not hmac.compare_digest(mac, expected):
            raise HTTPException(status_code=401, detail="下载链接无效或已过期")
    job = storage.get_export(job_id)
    if not job or job["status"] != "done":
        raise HTTPException(status_code=409, detail="导出任务尚未完成")
    path = Path(str(job["file_path"]))
    if not path.is_file() or EXPORT_DIR.resolve() not in path.resolve().parents:
        raise HTTPException(status_code=404, detail="导出文件不存在")
    storage.add_event("admin_export", status="DOWNLOADED", source="admin",
                      detail=f"job={job_id}")
    return FileResponse(path, filename=path.name)


@app.get("/api/export/stream")
def export_stream(request: Request, session: dict = Depends(require_admin)):
    """流式直接下载：内存 O(批)，不再一次性把全部历史读入列表。"""
    fmt = request.query_params.get("fmt", "csv")
    if fmt not in ("csv", "json", "jsonl"):
        raise HTTPException(status_code=400, detail="fmt 只能是 csv / json / jsonl")
    filters = _filters(request)
    counter = {"rows": 0}

    def generate():
        storage = get_storage()
        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS,
                                    extrasaction="ignore")
            writer.writeheader()
            yield buf.getvalue()
            for raw in storage.iter_calls(filters, batch=500):
                buf.seek(0)
                buf.truncate(0)
                writer.writerow(_csv_row(_safe_export_row(raw)))
                counter["rows"] += 1
                yield buf.getvalue()
        elif fmt == "json":
            yield "[\n"
            first = True
            for raw in storage.iter_calls(filters, batch=500):
                yield ("" if first else ",\n") + json.dumps(
                    _safe_export_row(raw), ensure_ascii=False, indent=2, default=str)
                first = False
                counter["rows"] += 1
            yield "\n]\n"
        else:
            for raw in storage.iter_calls(filters, batch=500):
                yield json.dumps(_safe_export_row(raw), ensure_ascii=False,
                                 default=str) + "\n"
                counter["rows"] += 1
        storage.add_event("admin_export", status="DIRECT", source="admin",
                          actor_id=f"admin:{session['username']}",
                          detail=f"fmt={fmt} rows={counter['rows']}")

    media = {"csv": "text/csv; charset=utf-8", "json": "application/json",
             "jsonl": "application/x-ndjson"}[fmt]
    return StreamingResponse(
        generate(), media_type=media,
        headers={"Content-Disposition": f'attachment; filename="mcp-calls.{fmt}"'},
    )


# ------------------------------------------------------------- 归档查询与恢复
class ArchiveBody(BaseModel):
    path: str = ""


def _archive_path(raw: str) -> Path | None:
    """路径必须解析到审计归档目录内，杜绝任意文件读取 / 恢复。"""
    if not raw:
        return None
    root = Path(get_storage().health().get("archive_dir") or "").resolve()
    try:
        candidate = Path(raw).resolve()
    except OSError:
        return None
    if not root or root not in candidate.parents or candidate.suffix != ".jsonl":
        return None
    return candidate


def _archive_event(kind: str, status: str, session: dict, detail: Any) -> None:
    get_storage().add_event(
        kind, status=status, source="admin",
        actor_id=f"admin:{session.get('username', '')}",
        detail=redact_text(detail, limit=1000),
    )


@app.get("/api/archives")
def archives(request: Request, session: dict = Depends(require_admin)):
    storage = get_storage()
    rows = storage.list_archives({
        "table": request.query_params.get("table", ""),
        "request_id": request.query_params.get("request_id", ""),
    }, min(500, max(1, int(request.query_params.get("limit", "100")))))
    return {"rows": _safe_rows(rows), "total": len(rows),
            "audit": storage.health(),
            "note": "归档文件位于审计数据卷；status=verified 表示行数与 SHA-256 均已复核。"}


@app.get("/api/archives/query")
def archives_query(request: Request, session: dict = Depends(require_admin)):
    request_id = request.query_params.get("request_id", "")
    hits = get_storage().query_archive(request_id)
    return {"request_id": request_id, "rows": hits, "total": len(hits)}


@app.post("/api/archives/verify")
def archives_verify(body: ArchiveBody, session: dict = Depends(require_admin)):
    path = _archive_path(body.path)
    if not path:
        raise HTTPException(status_code=400, detail="归档路径不在允许的归档目录内")
    result = get_storage().verify_archive(str(path))
    _archive_event("audit_archive_verify", "OK" if result["ok"] else "FAILED",
                   session, result)
    return {"ok": result["ok"], "result": result}


@app.post("/api/archives/restore")
def archives_restore(body: ArchiveBody, session: dict = Depends(require_admin)):
    path = _archive_path(body.path)
    if not path:
        raise HTTPException(status_code=400, detail="归档路径不在允许的归档目录内")
    storage = get_storage()
    try:
        from gateway.audit.middleware import trace_call
    except Exception:  # noqa: BLE001 - 后台进程未安装 fastmcp 时仍要记录事件
        trace_call = None
    arguments = {"path": str(path)}
    if trace_call is not None:
        with trace_call("restore_archive", arguments, source="admin") as rec:
            result = storage.restore_archive(str(path))
            rec.ok(json.dumps(result, ensure_ascii=False))
            if not result["ok"]:
                rec.status = "FAILED"
    else:
        result = storage.restore_archive(str(path))
        storage.add_event("audit_archive_restore",
                          status="OK" if result["ok"] else "FAILED",
                          source="admin", detail=result)
    _archive_event("audit_archive_restore", "OK" if result["ok"] else "FAILED",
                   session, result)
    return {"ok": result["ok"], "result": result}


# ------------------------------------------------------------------ V3 阶段 E
# 搜索监控 / 论文库与下载 / 文件传输 / 执行后端来源标识。
# 所有接口都走 require_admin，返回前经 _safe_rows() 脱敏；
# 上游连通性不自动探测，只如实报告「未检测」，避免虚构可达性。
PAPER_DIR = Path(os.environ.get("MCP_PAPER_DIR", "/workspace/research/papers")).resolve()
TRANSFER_DIR = Path(os.environ.get("MCP_TRANSFER_DIR", "/workspace/transfers")).resolve()


def _limit(request: Request, default: int = 100) -> int:
    try:
        return min(500, max(1, int(request.query_params.get("limit", default))))
    except ValueError:
        return default


def _paper_filters(request: Request) -> dict[str, str]:
    keys = ("keyword", "project", "tag", "reading_status", "from_year",
            "to_year", "doi", "identifier", "oa_status")
    return {k: request.query_params.get(k, "").strip() for k in keys
            if request.query_params.get(k, "").strip()}


def _paper_link_token(paper_id: str, expiry: int) -> str:
    secret = os.environ.get("ADMIN_SESSION_SECRET", "")
    payload = f"paper:{paper_id}:{expiry}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()[:32]


def _paper_file(paper_id: str) -> Path | None:
    rows = get_storage()._query(
        "SELECT file_path FROM paper_library WHERE paper_id=?", [paper_id])
    raw = (rows[0].get("file_path") if rows else "") or ""
    if not raw:
        return None
    try:
        path = Path(raw).resolve()
    except OSError:
        return None
    if path != PAPER_DIR and PAPER_DIR not in path.parents:
        return None
    return path if path.is_file() else None


@app.get("/api/search/events")
def search_events(request: Request, session: dict = Depends(require_admin)):
    """结构化检索事件：逐源耗时/成败/条数/限流/缓存命中，均为真实落库数据。"""
    since, until, label = _range(request)
    clauses = ["ts >= ?", "ts <= ?"]
    params: list[Any] = [since, until]
    for key in ("provider", "status", "tool_name", "client_id"):
        value = request.query_params.get(key, "").strip()
        if value:
            clauses.append(f"{key} = ?")
            params.append(value)
    keyword = request.query_params.get("keyword", "").strip()
    if keyword:
        clauses.append("(keyword LIKE ? OR query_hash LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    where = " WHERE " + " AND ".join(clauses)
    limit = _limit(request)
    storage = get_storage()
    rows = _safe_rows(storage._query(
        f"SELECT * FROM search_events{where} ORDER BY id DESC LIMIT ?",
        [*params, limit]))
    matrix = storage._query(
        "SELECT provider, COUNT(*) AS requests, "
        "SUM(CASE WHEN status IN ('SUCCESS','OK','success') THEN 1 ELSE 0 END) AS successes, "
        "SUM(CASE WHEN status IN ('FAILED','ERROR','SOURCE_UNAVAILABLE','failed','error') "
        "THEN 1 ELSE 0 END) AS failures, "
        "AVG(duration_ms) AS avg_ms, SUM(result_count) AS results, "
        "SUM(rate_limited) AS rate_limited, SUM(cached) AS cached "
        f"FROM search_events{where} GROUP BY provider ORDER BY requests DESC", params)
    return {
        "rows": rows, "total": len(rows), "by_provider": matrix,
        "range": {"label": label, "since": since, "until": until},
        "coverage_note": "垂直学术源与社区源不等于全网通用搜索；可选 SearXNG/通用源需单独启用，"
                         "软件免费不等于上游可达。",
        "upstream": {"status": "未检测",
                     "note": "后台不自动发起外部请求；上游可达性由独立集成测试记录，"
                             "此处不虚构探测结果。"},
        "source_note": "数据来源：审计库 search_events（外部来源的调用结果，已脱敏关键词）",
    }


@app.get("/api/papers")
def papers(request: Request, session: dict = Depends(require_admin)):
    from gateway.research.library import list_papers

    filters = _paper_filters(request)
    rows = _safe_rows(list_papers(filters, _limit(request, 200)))
    stats = get_storage()._query(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN file_path != '' AND sha256 != '' THEN 1 ELSE 0 END) AS verified, "
        "SUM(file_size) AS bytes FROM paper_library")
    stat = stats[0] if stats else {}
    total, verified = int(stat.get("total") or 0), int(stat.get("verified") or 0)
    return {
        "rows": rows, "total": len(rows),
        "stats": {"total": total, "verified": verified,
                  "bytes": int(stat.get("bytes") or 0),
                  "verified_rate": round(verified / total * 100, 1) if total else 0.0},
        "source_note": "数据来源：本地文献库（审计库 paper_library）+ 容器内已校验 PDF 文件",
        "ocr_note": "OCR 为可选能力，默认未安装、未启用；扫描版 PDF 需另行处理。",
        "download_note": "「任务创建成功」不等于「文件最终校验成功」，请查看下载任务面板。",
    }


@app.get("/api/papers/downloads")
def paper_downloads(request: Request, session: dict = Depends(require_admin)):
    limit = _limit(request)
    rows = _safe_rows(get_storage()._query(
        "SELECT * FROM paper_downloads ORDER BY id DESC LIMIT ?", [limit]))
    grouped = get_storage()._query(
        "SELECT status, COUNT(*) AS n FROM paper_downloads GROUP BY status")
    return {"rows": rows, "total": len(rows),
            "counts": {str(r["status"]): int(r["n"]) for r in grouped},
            "note": "created 仅表示任务创建；success 才表示文件通过 %PDF-/EOF/SHA-256 最终校验。",
            "source_note": "数据来源：审计库 paper_downloads（容器内下载任务）"}


@app.get("/api/papers/export")
def papers_export(request: Request, session: dict = Depends(require_admin)):
    from gateway.research.library import export_bibliography

    fmt = request.query_params.get("fmt", "bibtex").strip().lower()
    try:
        content = export_bibliography(fmt, _paper_filters(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    media = {"csv": "text/csv", "json": "application/json"}.get(fmt, "text/plain")
    filename = f"bibliography.{fmt if fmt != 'bibtex' else 'bib'}"
    get_storage().add_event("paper_export", status="OK", source="admin",
                            detail=f"format={fmt} rows_exported")
    return PlainTextResponse(content, media_type=media + "; charset=utf-8",
                             headers={"Content-Disposition":
                                      f'attachment; filename="{filename}"'})


@app.get("/api/papers/link")
def paper_link(request: Request, session: dict = Depends(require_admin)):
    paper_id = request.query_params.get("paper_id", "").strip()
    if not paper_id or not _paper_file(paper_id):
        return {"ok": False, "error": "论文不存在或未通过校验，不生成下载链接"}
    expiry = int(time.time()) + 600
    token = _paper_link_token(paper_id, expiry)
    return {"ok": True, "paper_id": paper_id, "expires_in": 600,
            "url": f"/api/papers/file?paper_id={urllib.parse.quote(paper_id)}"
                   f"&exp={expiry}&sig={token}",
            "note": "短期 HMAC 签名绑定论文 ID 与过期时间；文件不存在时明确失败。"}


@app.get("/api/papers/file")
def paper_file(request: Request):
    """已校验 PDF 的 Range 下载：有效后台会话或短期 HMAC 签名，二选一。

    刻意不挂 require_admin 依赖，否则纯签名链接会在进入函数前被 401 拦截。
    二者皆无时明确拒绝，绝不因「已登录过」而放行无效签名。
    """
    paper_id = request.query_params.get("paper_id", "").strip()
    expiry = request.query_params.get("exp", "")
    signature = request.query_params.get("sig", "")
    try:
        exp = int(expiry)
    except ValueError:
        exp = 0
    signed = bool(signature) and exp > time.time() and exp <= time.time() + 3600 \
        and hmac.compare_digest(signature, _paper_link_token(paper_id, exp))
    session = _session(request)
    if not signed and not session:
        raise HTTPException(status_code=401, detail="签名无效/已过期，且未登录")
    path = _paper_file(paper_id)
    if path is None:
        raise HTTPException(status_code=404, detail="文件不存在、未校验或路径越界")
    size = path.stat().st_size
    span = parse_range(request.headers.get("range", ""), size)
    if span is None:
        raise HTTPException(status_code=416, detail="Range 不合法")
    start, end = span
    partial = request.headers.get("range", "").startswith("bytes=")
    headers = {"Accept-Ranges": "bytes",
               "Content-Length": str(end - start + 1),
               "Content-Disposition": f'attachment; filename="{path.name}"'}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    get_storage().add_event(
        "paper_file_download", status="OK", source="admin",
        actor_id=f"admin:{(session or {}).get('username', 'signed-link')}",
        detail=redact_text(f"paper_id={paper_id} bytes={end - start + 1} "
                           f"range={start}-{end} signed={signed}", limit=300))
    return StreamingResponse(iter_file(path, start, end),
                             status_code=206 if partial else 200,
                             media_type="application/pdf", headers=headers)


@app.get("/api/transfers")
def transfers(request: Request, session: dict = Depends(require_admin)):
    rows = _safe_rows(get_storage()._query(
        "SELECT * FROM file_transfers ORDER BY id DESC LIMIT ?", [_limit(request)]))
    grouped = get_storage()._query(
        "SELECT status, COUNT(*) AS n FROM file_transfers GROUP BY status")
    return {"rows": rows, "total": len(rows),
            "counts": {str(r["status"]): int(r["n"]) for r in grouped},
            "note": "created 仅表示任务创建；success 表示最终校验/传输成功。",
            "source_note": "数据来源：审计库 file_transfers（容器内传输）"}


@app.get("/api/transfers/link")
def transfer_link(request: Request, session: dict = Depends(require_admin)):
    """为已落盘的通用文件生成短期 HMAC 签名下载链接（后台侧）。"""
    from gateway.transfer.http_api import safe_path, sign_path

    if not os.environ.get("MCP_TRANSFER_SECRET", "").strip():
        return {"ok": False, "error": "MCP_TRANSFER_SECRET 未配置，拒绝用弱默认密钥签发链接"}
    raw = request.query_params.get("path", "").strip()
    path = safe_path(TRANSFER_DIR, raw)
    if path is None or not path.is_file():
        return {"ok": False, "error": "文件不存在或路径不在授权传输目录内"}
    expiry, signature = sign_path(raw, 600)
    return {"ok": True, "path": str(path), "expires_in": 600,
            "url": f"/api/transfers/file?path={urllib.parse.quote(raw)}"
                   f"&exp={expiry}&sig={signature}",
            "note": "签名绑定原始路径与过期时间；后台只读挂载，不改写网关下载结果。"}


@app.get("/api/transfers/file")
def transfer_file(request: Request):
    """通用文件 Range 下载：有效后台会话或短期签名，二选一。"""
    from gateway.transfer.http_api import safe_path, verify_path

    raw = request.query_params.get("path", "").strip()
    expiry = request.query_params.get("exp", "")
    signature = request.query_params.get("sig", "")
    signed = bool(signature) and verify_path(raw, expiry, signature)
    if not signed and not _session(request):
        raise HTTPException(status_code=401, detail="签名无效/已过期，且未登录")
    path = safe_path(TRANSFER_DIR, raw)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在或路径越界")
    size = path.stat().st_size
    span = parse_range(request.headers.get("range", ""), size)
    if span is None:
        raise HTTPException(status_code=416, detail="Range 不合法")
    start, end = span
    partial = request.headers.get("range", "").startswith("bytes=")
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
               "Content-Disposition": f'attachment; filename="{path.name}"'}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    get_storage().add_event(
        "transfer_file_download", status="OK", source="admin",
        detail=redact_text(f"range={start}-{end} signed={signed}", limit=200))
    return StreamingResponse(iter_file(path, start, end),
                             status_code=206 if partial else 200,
                             media_type="application/octet-stream", headers=headers)


@app.get("/api/host/status")
def host_status(session: dict = Depends(require_admin)):
    from gateway.exec.backends import backend_status
    from gateway.monitoring.system import SOURCE_LABELS

    info = backend_status()
    info["source_labels"] = SOURCE_LABELS
    info["scope_note"] = "容器指标来自容器自身；宿主指标仅在用户显式挂载授权目录并启用 Host Agent 后可用。"
    return info


@app.get("/api/v3/overview")
def v3_overview(request: Request, session: dict = Depends(require_admin)):
    """仪表盘单条聚合查询：避免每页 N+1 请求。"""
    since, until, label = _range(request)
    storage = get_storage()
    def _count(table: str, column: str = "ts") -> int:
        rows = storage._query(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {column} >= ? AND {column} <= ?",
            [since, until])
        return int(rows[0]["n"]) if rows else 0

    def _grouped(table: str, key: str = "status") -> dict[str, int]:
        rows = storage._query(
            f"SELECT {key}, COUNT(*) AS n FROM {table} "
            f"WHERE ts >= ? AND ts <= ? GROUP BY {key}", [since, until])
        return {str(r[key]): int(r["n"]) for r in rows}

    failed_downloads = _safe_rows(storage._query(
        "SELECT ts,title,status,error_message,request_id FROM paper_downloads "
        "WHERE status IN ('failed','error','no_oa','source_unavailable') "
        "ORDER BY id DESC LIMIT 5"))
    source_counts = storage._query(
        "SELECT source, COUNT(*) AS n FROM tool_calls "
        "WHERE timestamp_start >= ? AND timestamp_start <= ? GROUP BY source",
        [since, until])
    return {
        "range": {"label": label, "since": since, "until": until},
        "search_events": _count("search_events"),
        "papers": int(storage._query(
            "SELECT COUNT(*) AS n FROM paper_library")[0]["n"]),
        "downloads": _grouped("paper_downloads"),
        "transfers": _grouped("file_transfers"),
        "tool_sources": {str(r["source"]): int(r["n"]) for r in source_counts},
        "recent_failed_downloads": failed_downloads,
        "audit": storage.health(),
        "source_note": "全部数据来自共享审计库；标记为外部来源的是上游 API 的调用结果，非本机指标。",
    }
