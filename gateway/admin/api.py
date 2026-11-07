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
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from gateway.audit.redaction import redact_text
from gateway.audit.storage import get_storage
from gateway.monitoring.system import collect
from .auth import COOKIE_NAME, get_auth

BASE_DIR = Path(__file__).parent
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://mcp-gateway:8765").rstrip("/")
EXPORT_DIR = Path(os.environ.get("AUDIT_EXPORT_DIR", "/data/audit/exports"))
TASK_LOG_DIR = Path(os.environ.get("MCP_TASK_LOG_DIR", "/tmp/mcp_tasks")).resolve()
COOKIE_SECURE = os.environ.get("ADMIN_COOKIE_SECURE", "0").strip().lower() in ("1", "true", "yes")
# 默认不信任客户端自报的 X-Forwarded-For；只有部署在可信反向代理后才开启。
TRUST_PROXY = os.environ.get("ADMIN_TRUST_PROXY", "0").strip().lower() in ("1", "true", "yes", "on")

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    auth = get_auth()
    if auth.generated_password:
        show = os.environ.get("MCP_PRINT_SECRETS", "0").strip().lower() in ("1", "true", "yes", "on")
        if show:
            print("=" * 70, flush=True)
            print(" [自动生成] Admin Web 管理员密码（未配置 ADMIN_PASSWORD 时随机生成）:", flush=True)
            print(f"   账号: {auth.username}", flush=True)
            print(f"   密码: {auth.generated_password}", flush=True)
            print("=" * 70, flush=True)
        else:
            print("[admin] 已生成管理员口令并持久化，默认不写入容器日志；"
                  "请从 ADMIN_PASSWORD_FILE 读取（或设置 MCP_PRINT_SECRETS=1）", flush=True)
    elif os.environ.get("ADMIN_PASSWORD", "").strip() == "admin":
        print("[admin] 正在使用镜像默认管理员口令 'admin'，仅限隔离环境；"
              "请用 -e ADMIN_PASSWORD='<强口令>' 覆盖", flush=True)
    get_storage().add_event("admin_service", status="UP", source="admin",
                            detail="管理后台已启动")
    yield


app = FastAPI(title="MCP Admin Dashboard", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=_lifespan)


class CSRFGuard:
    """管理端写操作的跨站防护（SameSite=Lax 的补充）。

    使用纯 ASGI 而非 @app.middleware("http") 的 BaseHTTPMiddleware，
    避免缓冲响应体、破坏 /api/live/stream 的 SSE 事件流。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("method") not in ("GET", "HEAD", "OPTIONS"):
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            origin = headers.get("origin", "")
            host = headers.get("host", "")
            blocked = bool(origin) and urllib.parse.urlsplit(origin).netloc != host
            if not origin and headers.get("sec-fetch-site") in ("cross-site", "same-site"):
                blocked = True
            if blocked:
                response = JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(CSRFGuard)


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
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
    "MCP_OAUTH_REQUIRE_PKCE", "MCP_TRUST_PROXY", "ADMIN_TRUST_PROXY",
    "MCP_GIT_EXTRA_SUBCOMMANDS",
    "AUDIT_STRICT", "AUDIT_RETENTION_DAYS", "AUDIT_MAX_DB_MB",
    "ADMIN_SESSION_TTL", "ADMIN_LOGIN_MAX_FAILURES", "ADMIN_LOGIN_LOCK_MINUTES",
)
SENSITIVE_CONFIG = (
    "MCP_TOKEN", "ADMIN_PASSWORD", "ADMIN_SESSION_SECRET", "ADMIN_TOTP_SECRET",
    "CF_TUNNEL_TOKEN", "MCP_SEARCH_TAVILY_KEY", "MCP_SEARCH_MAILTO",
    "MCP_SEARCH_SEARXNG_URL", "AUDIT_DB_DSN",
)
EDITABLE_SETTINGS = ("default_range", "page_size", "theme")


# --------------------------------------------------------------------- auth
def _ip(request: Request) -> str:
    direct = request.client.host if request.client else "unknown"
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        return (forwarded or direct)[:64]
    return direct[:64]


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


def _gateway_token() -> str:
    """优先用环境变量；网关自动生成令牌时会同步到共享卷。"""
    token = os.environ.get("MCP_TOKEN", "").strip()
    if not token:
        try:
            token = Path("/data/audit/.mcp_token").read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return token


def _gateway_health() -> dict[str, Any]:
    """优先用 MCP_TOKEN 读取需鉴权的内部指标；公网 /health 只保留最小存活状态。"""
    token = _gateway_token()
    candidates = []
    if token:
        candidates.append((GATEWAY_URL + "/internal/metrics",
                           {"Authorization": f"Bearer {token}"}))
    candidates.append((GATEWAY_URL + "/health", {}))
    last_error = ""
    for url, headers in candidates:
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            last_error = repr(exc)[:200]
    return {"status": "down", "error": last_error, "source": "mcp-gateway"}


def _filters(request: Request) -> dict[str, Any]:
    allowed = ("since", "until", "tool_name", "status", "client_id", "actor_id",
               "request_id", "task_id", "git_repo", "experiment_id", "keyword",
               "file_path")
    return {key: request.query_params[key] for key in allowed
            if request.query_params.get(key)}


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
    monitoring["gateway_metrics_source"] = "mcp-gateway 容器（来自 /health）"
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
        "active_sessions": sum(1 for s in storage.sessions() if s["status"] == "active"),
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
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


@app.get("/api/calls/{request_id}")
def call_detail(request_id: str, session: dict = Depends(require_admin)):
    storage = get_storage()
    row = storage.get_call(request_id)
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    result = row.get("result", "")
    if row.get("result_ref"):
        full = storage.artifacts.read(row["result_ref"],
                                      expected_sha256=row.get("result_sha256", ""))
        if full is not None:
            result = redact_text(full)
    related_tasks = storage.tasks(task_id=row.get("task_id", "")) if row.get("task_id") else []
    git = storage.git_for_request(request_id)
    experiments = storage.experiments_for_request(request_id, row.get("task_id", ""))
    return {
        "call": {**row, "result": result},
        "timeline": storage.call_timeline(request_id, row.get("trace_id", "")),
        "tasks": related_tasks, "git": git, "experiments": experiments,
        "source_note": "参数与输出已脱敏；大输出以受保护引用保存。",
    }


@app.get("/api/live/recent")
def live_recent(request: Request, session: dict = Depends(require_admin)):
    after = int(request.query_params.get("after", "0"))
    storage = get_storage()
    # 首屏展示最新事件；after>0 时仍按 id 升序追赶，供 SSE 增量使用
    events = storage.latest_events(100) if after <= 0 else storage.recent_events(after, 100)
    return {"events": events, "last_id": events[-1]["id"] if events else after}


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
                    yield "data: " + json.dumps(
                        {k: str(v) if not isinstance(v, (int, float, bool, type(None))) else v
                         for k, v in event.items()}, ensure_ascii=False
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
    return {"rows": get_storage().git_ops(request.query_params.get("repo", ""), 200)}


@app.get("/api/experiments")
def experiments(session: dict = Depends(require_admin)):
    return {"rows": get_storage().experiments(200)}


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
        return {"log": "\n".join(lines), "path": str(path)}
    except OSError as exc:
        return {"log": f"(读取失败: {exc})"}


@app.get("/api/tasks")
def tasks(session: dict = Depends(require_admin)):
    return {"rows": get_storage().tasks(200)}


@app.get("/api/clients")
def clients(session: dict = Depends(require_admin)):
    """客户端列表：只显示令牌指纹，绝不显示明文 Token。"""
    return {"rows": get_storage().sessions(200)}


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


def _serialize(rows: list[dict[str, Any]], fmt: str) -> tuple[str, str]:
    if fmt == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2, default=str), "application/json"
    if fmt == "jsonl":
        return ("\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
                + ("\n" if rows else "")), "application/x-ndjson"
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_row(row))
        return buf.getvalue(), "text/csv; charset=utf-8"
    raise ValueError("fmt 只能是 csv / json / jsonl")


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
                for row in storage.iter_calls(filters, batch=1000):
                    writer.writerow(_csv_row(row))
                    count += 1
            elif fmt == "json":
                fh.write("[\n")
                for row in storage.iter_calls(filters, batch=1000):
                    fh.write((",\n" if count else "") +
                             json.dumps(row, ensure_ascii=False, indent=2, default=str))
                    count += 1
                fh.write("\n]\n")
            else:
                for row in storage.iter_calls(filters, batch=1000):
                    fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
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
    """小数据量直接下载，同样遵守筛选与脱敏策略。"""
    fmt = request.query_params.get("fmt", "csv")
    try:
        rows = list(get_storage().iter_calls(_filters(request), batch=1000))
        text, media = _serialize(rows, fmt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_storage().add_event("admin_export", status="DIRECT", source="admin",
                            actor_id=f"admin:{session['username']}",
                            detail=f"fmt={fmt} rows={len(rows)}")
    return StreamingResponse(
        iter([text]), media_type=media,
        headers={"Content-Disposition": f'attachment; filename="mcp-calls.{fmt}"'},
    )


