"""
CNB Command MCP Gateway（OAuth 版，通用 MCP）
============================================
把当前 CNB 服务器暴露成 MCP server，唯一工具 `run_command`。
面向所有 MCP 客户端（ChatGPT、Claude、Cursor、VS Code、Codex、各类 agent）。

两种接入方式：
  A. Bearer 直连（最简单、最通用）：任意能配置请求头/环境变量的 agent，
     直接对 https://<host>/mcp 发送 Authorization: Bearer <MCP_TOKEN> 即可，无需 OAuth。
  B. OAuth 2.1（PKCE + DCR）：适合只支持 OAuth 的客户端（如 ChatGPT 连接器）。

安全设计：
  - /mcp 及一切业务路径：强制 Bearer token（OAuth 签发的或 .env 里的 MCP_TOKEN）。
  - OAuth 端点（/authorize /token /register /.well-known/*）免 Bearer，
    但 /authorize 的同意页要求输入 MCP_TOKEN 作为口令，签发授权码后换取 token。
  - 支持 PKCE S256、动态客户端注册（DCR）、authorization_code。
  - redirect_uri 接受任意合法绝对 URI（https/http/自定义 scheme），兼容各 agent。
  - 命令前缀白名单 MCP_ALLOWED_PREFIXES；全程审计日志 audit.log。
  - 只监听 127.0.0.1，由 cloudflared 隧道负责 TLS 与公网暴露。

OAuth 客户端注册对应关系：
  服务URL            = https://<tunnel>/mcp
  授权网址           = https://<tunnel>/authorize
  Token URL          = https://<tunnel>/token
  注册地址           = https://<tunnel>/register
  授权服务器基础地址 = https://<tunnel>
  Token端点身份验证  = none；PKCE(S256) 勾选；客户端密钥留空；scope 留空
"""

import os
import subprocess
import logging
import datetime
import secrets
import hashlib
import base64
import html
import time
import urllib.parse
from pathlib import Path

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.responses import (
    JSONResponse,
    HTMLResponse,
    RedirectResponse,
    PlainTextResponse,
    Response,
)
from starlette.routing import Route

CONFIG_DIR = Path(__file__).parent


def load_env(p: Path):
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(CONFIG_DIR / ".env")

TOKEN = os.environ.get("MCP_TOKEN", "")
PORT = int(os.environ.get("MCP_PORT", "8765"))
BIND = os.environ.get("MCP_BIND", "127.0.0.1")
WORKDIR = os.environ.get("MCP_WORKDIR", "/workspace")
ALLOWED_PREFIXES = [p for p in os.environ.get("MCP_ALLOWED_PREFIXES", "").split("|") if p]

logging.basicConfig(
    filename=str(CONFIG_DIR / "audit.log"),
    level=logging.INFO,
    format="%(message)s",
)
AUDIT = logging.getLogger("audit")


def audit(msg: str):
    AUDIT.info(f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} {msg}")


if not TOKEN:
    raise SystemExit("FATAL: MCP_TOKEN 未在 .env 中设置，拒绝启动。")


# ---------------------------------------------------------------------------
# OAuth 最小实现（authorization_code + PKCE S256 + DCR）
# ---------------------------------------------------------------------------

AUTH_CODES: dict = {}   # code -> {expires, redirect_uri, challenge, method, fallback}
ISSUED_TOKENS: set = set()
CLIENTS: dict = {}      # client_id -> {"redirect_uris": [...]}（DCR 注册信息）
CODE_TTL = 600
TOKEN_TTL = 60 * 60 * 24 * 30

OAUTH_PATHS = {
    "/authorize",
    "/token",
    "/register",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    # ChatGPT 插件 / GPT Actions 的公开元数据（无需鉴权）
    "/.well-known/ai-plugin.json",
    "/openapi.yaml",
    "/logo.png",
}


def _issuer(request) -> str:
    host = request.headers.get("host", f"127.0.0.1:{PORT}")
    return f"https://{host}"


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def metadata_endpoint(request):
    iss = _issuer(request)
    return JSONResponse(
        {
            "issuer": iss,
            "authorization_endpoint": f"{iss}/authorize",
            "token_endpoint": f"{iss}/token",
            "registration_endpoint": f"{iss}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["mcp"],
        }
    )


async def protected_resource_endpoint(request):
    iss = _issuer(request)
    return JSONResponse(
        {
            "resource": f"{iss}/mcp",
            "authorization_servers": [iss],
            "scopes_supported": ["mcp"],
            "bearer_methods_supported": ["header"],
        }
    )


async def register_endpoint(request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    client_id = "cnb-" + secrets.token_urlsafe(16)
    uris = [str(u) for u in (body.get("redirect_uris") or [])]
    CLIENTS[client_id] = {"redirect_uris": uris}
    audit(f"DCR client_id={client_id} uris={uris}")
    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": uris,
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )


_AUTHORIZE_FORM = """<!doctype html><html><head><meta charset="utf-8">
<title>CNB MCP 授权</title></head>
<body style="font-family:sans-serif;max-width:420px;margin:60px auto;">
<h3>授权 Agent 访问 CNB 命令网关</h3>
<p>回调地址：<code>{redirect}</code></p>
<p>请输入访问口令（即 .env 中的 MCP_TOKEN）：</p>
<form method="post">
{hidden}
<input name="password" type="password" placeholder="MCP_TOKEN" required
       style="width:100%;padding:8px;margin:8px 0;"/>
<button type="submit" style="padding:8px 20px;">授权</button>
</form></body></html>"""


async def authorize_endpoint(request):
    if request.method == "GET":
        qp = request.query_params
        if qp.get("response_type") != "code":
            return PlainTextResponse("response_type must be 'code'", 400)
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(k, quote=True)}" '
            f'value="{html.escape(v, quote=True)}"/>'
            for k, v in qp.multi_items()
        )
        reg = CLIENTS.get(str(qp.get("client_id", "")), {}).get("redirect_uris", [])
        shown = qp.get("redirect_uri", "") or (reg[0] if reg else "")
        return HTMLResponse(
            _AUTHORIZE_FORM.format(
                redirect=html.escape(shown, quote=True),
                hidden=hidden,
            )
        )
    form = await request.form()
    if form.get("password") != TOKEN:
        audit("AUTHORIZE DENIED (bad password)")
        return HTMLResponse("<h3>口令错误</h3>", status_code=401)
    client_id = str(form.get("client_id", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    registered = CLIENTS.get(client_id, {}).get("redirect_uris", [])
    fallback = False
    if not redirect_uri and registered:
        redirect_uri = registered[0]
        fallback = True
    # 接受任意合法绝对 URI（https/http/自定义 scheme），兼容所有 agent 的回调
    if not redirect_uri or "://" not in redirect_uri:
        audit(f"AUTHORIZE DENIED redirect={redirect_uri!r} registered={registered}")
        return PlainTextResponse(f"redirect_uri 不被允许: {redirect_uri!r}", 400)
    code = secrets.token_urlsafe(32)
    AUTH_CODES[code] = {
        "expires": time.time() + CODE_TTL,
        "redirect_uri": redirect_uri,
        "challenge": str(form.get("code_challenge", "")),
        "method": str(form.get("code_challenge_method", "")),
        "fallback": fallback,
    }
    audit(f"AUTHORIZE OK code_issued redirect={redirect_uri}")
    state = str(form.get("state", ""))
    sep = "&" if "?" in redirect_uri else "?"
    url = f"{redirect_uri}{sep}code={urllib.parse.quote(code)}"
    if state:
        url += f"&state={urllib.parse.quote(state)}"
    return RedirectResponse(url, status_code=302)


async def token_endpoint(request):
    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    code = str(form.get("code", ""))
    rec = AUTH_CODES.pop(code, None)
    if not rec or rec["expires"] < time.time():
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    provided = str(form.get("redirect_uri", ""))
    if provided != rec["redirect_uri"] and not (provided == "" and rec.get("fallback")):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if rec["challenge"]:
        calc = _pkce_s256(str(form.get("code_verifier", "")))
        if calc != rec["challenge"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
    tok = secrets.token_urlsafe(32)
    ISSUED_TOKENS.add(tok)
    audit("TOKEN ISSUED")
    return JSONResponse(
        {"access_token": tok, "token_type": "Bearer", "expires_in": TOKEN_TTL}
    )


# ---------------------------------------------------------------------------
# run_command 工具
# ---------------------------------------------------------------------------

mcp = FastMCP("cnb-command-gateway")


def _exec(command: str, cwd: str = WORKDIR, timeout: int = 60) -> str:
    """实际执行 shell 命令（含白名单与审计），MCP 工具与 REST 共用。"""
    if ALLOWED_PREFIXES:
        if not any(command.strip().startswith(p) for p in ALLOWED_PREFIXES):
            audit(f"DENIED(prefix) cmd={command!r}")
            return f"ERROR: 命令不在白名单 {ALLOWED_PREFIXES} 内，已拒绝。"
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (
            f"exit={proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
        audit(f"OK cmd={command!r} cwd={cwd} exit={proc.returncode}")
        return out
    except subprocess.TimeoutExpired:
        audit(f"TIMEOUT cmd={command!r}")
        return f"ERROR: 命令在 {timeout}s 后超时。"
    except Exception as e:  # noqa: BLE001
        audit(f"ERR cmd={command!r} exc={e!r}")
        return f"ERROR: {e}"


@mcp.tool()
def run_command(command: str, cwd: str = WORKDIR, timeout: int = 60) -> str:
    """在 CNB 服务器上执行一条 shell 命令。

    Args:
        command: 要执行的 shell 命令（经 OAuth/token 鉴权后执行，当前为 root）。
        cwd: 工作目录，默认 /workspace。
        timeout: 超时秒数，默认 60。

    Returns:
        命令的退出码、stdout、stderr 拼接文本。
    """
    return _exec(command, cwd, timeout)


# ---------------------------------------------------------------------------
# ChatGPT 插件 / GPT Actions：REST 端点 + 官方 ai-plugin.json / openapi.yaml
# ---------------------------------------------------------------------------

_LOGO_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

_OPENAPI_TMPL = """openapi: 3.0.1
info:
  title: CNB Command Gateway
  version: 1.0.0
  description: 在 CNB 云端服务器执行 shell 命令（需 Bearer 授权）。
servers:
  - url: {BASE}
paths:
  /run_command:
    post:
      operationId: run_command
      summary: 在服务器上执行一条 shell 命令
      description: 执行给定的 shell 命令并返回 stdout/stderr/退出码。仅在用户明确要求时调用。
      security:
        - bearerAuth: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required:
                - command
              properties:
                command:
                  type: string
                  description: 要执行的 shell 命令
                cwd:
                  type: string
                  description: 工作目录，默认 /workspace
                timeout:
                  type: integer
                  description: 超时秒数，默认 60
      responses:
        '200':
          description: 命令结果
          content:
            application/json:
              schema:
                type: object
                properties:
                  output:
                    type: string
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
"""


def _base(request) -> str:
    host = request.headers.get("host", f"127.0.0.1:{PORT}")
    return f"https://{host}"


async def run_command_rest(request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"output": "ERROR: 请求体必须是 JSON"}, status_code=400)
    command = str(body.get("command", ""))
    cwd = str(body.get("cwd", WORKDIR))
    try:
        timeout = int(body.get("timeout", 60))
    except Exception:  # noqa: BLE001
        timeout = 60
    return JSONResponse({"output": _exec(command, cwd, timeout)})


async def plugin_manifest(request):
    base = _base(request)
    manifest = {
        "schema_version": "v1",
        "name_for_human": "CNB Command Gateway",
        "name_for_model": "cnb_command_gateway",
        "description_for_human": "在 CNB 云端服务器上执行 shell 命令（需授权）",
        "description_for_model": "Execute shell commands on the CNB cloud server. Only call this when the user explicitly asks to run a command. Requires authorization.",
        "auth": {
            "type": "oauth",
            "client_url": f"{base}/authorize",
            "scope": "mcp",
            "authorization_url": f"{base}/authorize",
            "token_url": f"{base}/token",
            "verification_url": f"{base}/.well-known/oauth-protected-resource",
        },
        "api": {"type": "openapi", "url": f"{base}/openapi.yaml"},
        "logo_url": f"{base}/logo.png",
        "contact_email": "admin@localhost",
    }
    return JSONResponse(manifest)


async def openapi_spec(request):
    base = _base(request)
    return PlainTextResponse(_OPENAPI_TMPL.format(BASE=base), media_type="text/yaml")


async def logo_png(request):
    return Response(_LOGO_PNG, media_type="image/png")


class BearerAuthMiddleware:
    """纯 ASGI 中间件：业务路径强制 Bearer token；OAuth 端点免验。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path not in OAUTH_PATHS:
                headers = {
                    k.decode().lower(): v.decode() for k, v in scope.get("headers", [])
                }
                raw = headers.get("authorization", "")
                ok = raw.startswith("Bearer ") and (
                    raw[len("Bearer "):].strip() == TOKEN
                    or raw[len("Bearer "):].strip() in ISSUED_TOKENS
                )
                if not ok:
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 401,
                            "headers": [(b"content-type", b"application/json")],
                        }
                    )
                    await send(
                        {"type": "http.response.body", "body": b'{"error":"unauthorized"}'}
                    )
                    return
        await self.app(scope, receive, send)


app = mcp.http_app(
    middleware=[Middleware(BearerAuthMiddleware)],
    allowed_hosts=["*"],
)

app.router.routes.append(Route("/authorize", authorize_endpoint, methods=["GET", "POST"]))
app.router.routes.append(Route("/token", token_endpoint, methods=["POST"]))
app.router.routes.append(Route("/register", register_endpoint, methods=["POST"]))
app.router.routes.append(
    Route("/.well-known/oauth-authorization-server", metadata_endpoint, methods=["GET"])
)
app.router.routes.append(
    Route("/.well-known/oauth-authorization-server/mcp", metadata_endpoint, methods=["GET"])
)
app.router.routes.append(
    Route("/.well-known/oauth-protected-resource", protected_resource_endpoint, methods=["GET"])
)
app.router.routes.append(
    Route("/.well-known/oauth-protected-resource/mcp", protected_resource_endpoint, methods=["GET"])
)
# ChatGPT 插件 / GPT Actions：REST + 官方清单/OpenAPI
app.router.routes.append(Route("/run_command", run_command_rest, methods=["POST"]))
app.router.routes.append(
    Route("/.well-known/ai-plugin.json", plugin_manifest, methods=["GET"])
)
app.router.routes.append(Route("/openapi.yaml", openapi_spec, methods=["GET"]))
app.router.routes.append(Route("/logo.png", logo_png, methods=["GET"]))

if __name__ == "__main__":
    import uvicorn

    audit(f"START bind={BIND} port={PORT} allowlist={ALLOWED_PREFIXES or 'ALL'}")
    uvicorn.run(app, host=BIND, port=PORT, log_level="info")
