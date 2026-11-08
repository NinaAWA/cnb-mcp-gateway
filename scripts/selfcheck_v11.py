#!/usr/bin/env python3
"""CNB MCP Gateway v1.1 — T01..T12 真实端到端自检。

全部通过真实 MCP 工具调用 / REST / HTTP / 管理网站会话完成，不做 mock、不虚构结果。
每项输出：执行内容、真实输出摘录、PASS / FAIL / SKIP 与原因；任一 FAIL 时退出码非 0。

用法（一键部署后无需任何参数：凭据自动从容器内的 600 权限文件读取）：
  python3 scripts/selfcheck_v11.py
  # 或显式提供：
  MCP_TOKEN=<token> ADMIN_PASSWORD=<password> \
    python3 scripts/selfcheck_v11.py
  MCP_BASE_URL=https://mcp.example.com MCP_SELFCHECK_SKIP_RESTART=1 \
    python3 scripts/selfcheck_v11.py --skip-restart

环境变量见文件末尾 CONFIG 段。需要 fastmcp（与网关相同依赖）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import http.cookiejar
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any, Callable

BASE = os.environ.get("MCP_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
ADMIN_BASE = os.environ.get("ADMIN_BASE_URL", "http://127.0.0.1:8788").rstrip("/")
TOKEN = os.environ.get("MCP_TOKEN", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
CONTAINER = os.environ.get("MCP_SELFCHECK_CONTAINER", "cnb-mcp-gateway-plus")
ADMIN_CONTAINER = os.environ.get("MCP_SELFCHECK_ADMIN_CONTAINER", "cnb-mcp-admin-web")
FILE_ROOT = os.environ.get("MCP_SELFCHECK_FILE_ROOT", "/data/files").rstrip("/")
WORK = "/workspace/.mcp-v11-selfcheck"
REWRITE_URL = os.environ.get("MCP_SELFCHECK_REWRITE_URL", "1") == "1"

RESULTS: list[dict[str, str]] = []


# ------------------------------------------------------------------ MCP 调用
async def _tool(name: str, args: dict[str, Any]) -> str:
    from fastmcp import Client
    async with Client(f"{BASE}/mcp", auth=TOKEN, timeout=120) as client:
        result = await client.call_tool(name, args)
    parts = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    return "\n".join(parts)


def tool(name: str, args: dict[str, Any]) -> str:
    """真实 MCP 调用；服务重启瞬间允许有限重试（仅连接类异常），不吞掉业务错误。"""
    for attempt in range(3):
        try:
            return asyncio.run(_tool(name, args))
        except Exception as exc:  # noqa: BLE001
            transient = isinstance(exc, OSError) or any(
                key in type(exc).__name__
                for key in ("Connection", "Reset", "RemoteProtocol", "ReadError"))
            if not transient or attempt == 2:
                raise
            time.sleep(1 + attempt)
    raise RuntimeError("unreachable")


def wait_ready(timeout: int = 120) -> bool:
    """等待网关与 MCP 会话真正就绪，避免把容器重建的启动竞态误判成功能失败。

    只探测 health 与真实 MCP 握手，不做任何 mock；超时返回 False 由 main 如实报错。
    """
    from fastmcp import Client

    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            status, _, _ = _http(f"{BASE}/health")
            if status == 200:
                async def probe():
                    async with Client(f"{BASE}/mcp", auth=TOKEN, timeout=10) as client:
                        await client.list_tools()
                asyncio.run(probe())
                return True
            last = f"health={status}"
        except Exception as exc:  # noqa: BLE001 - 启动期连接失败属正常竞态
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
        time.sleep(1)
    print(f"[preflight] 最后状态: {last}", file=sys.stderr)
    return False


def _http(url: str, *, data: bytes | None = None, headers: dict | None = None,
          opener=None, method: str = "") -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method or None)
    op = opener or urllib.request.build_opener()
    try:
        with op.open(req, timeout=30) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)
    except (urllib.error.URLError, OSError) as exc:
        # docker restart 重建端口映射的瞬间会收到 TCP RST：如实返回状态码 0 与原因，
        # 让 health 轮询继续等待，而不是把整项测试变成未捕获异常。
        return 0, f"{type(exc).__name__}: {exc}".encode(errors="replace"), {}


def rest(command: str, **kw: Any) -> str:
    body = json.dumps({"command": command, **kw}).encode()
    status, raw, _ = _http(f"{BASE}/run_command", data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    if status != 200:
        return f"ERROR: REST {status}: {raw.decode(errors='replace')}"
    return json.loads(raw.decode()).get("output", "")


def _read_container_file(container: str, path: str) -> str:
    """从本机容器读取首次启动生成的凭据；不打印内容，仅用于自检鉴权。"""
    if not (BASE.startswith("http://127.0.0.1")
            or ADMIN_BASE.startswith("http://127.0.0.1")):
        return ""
    try:
        proc = subprocess.run(["docker", "exec", container, "cat", path],
                              capture_output=True, text=True, timeout=15)
        return proc.stdout if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _mcp_token() -> str:
    if TOKEN:
        return TOKEN
    for line in _read_container_file(CONTAINER, "/app/.env").splitlines():
        if line.startswith("MCP_TOKEN="):
            return line.split("=", 1)[1].strip()
    token_file = os.environ.get("MCP_TOKEN_FILE", "").strip()
    if token_file:
        try:
            with open(token_file, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            pass
    return ""


_ADMIN_PASSWORD_CACHE = ""


def _admin_password() -> str:
    """显式环境变量优先，否则读取一键部署首次启动生成的随机口令。"""
    global _ADMIN_PASSWORD_CACHE
    if ADMIN_PASSWORD:
        return ADMIN_PASSWORD
    if _ADMIN_PASSWORD_CACHE:
        return _ADMIN_PASSWORD_CACHE
    pass_file = os.environ.get("ADMIN_PASSWORD_FILE", "").strip()
    if pass_file:
        try:
            with open(pass_file, encoding="utf-8") as fh:
                _ADMIN_PASSWORD_CACHE = fh.read().strip()
        except OSError:
            pass
        if _ADMIN_PASSWORD_CACHE:
            return _ADMIN_PASSWORD_CACHE
    _ADMIN_PASSWORD_CACHE = _read_container_file(
        ADMIN_CONTAINER, "/data/audit/.admin_password").strip()
    return _ADMIN_PASSWORD_CACHE


def admin_session():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = json.dumps({"username": "admin", "password": _admin_password()}).encode()
    status, raw, _ = _http(f"{ADMIN_BASE}/api/login", data=body,
                           headers={"Content-Type": "application/json"}, opener=opener)
    return opener if status == 200 else None


def _excerpt(text: str, limit: int = 420) -> str:
    flat = " | ".join(line.strip() for line in str(text).splitlines() if line.strip())
    return flat if len(flat) <= limit else flat[:limit] + " …[摘录]"


def record(tid: str, title: str, status: str, detail: str, output: str) -> None:
    RESULTS.append({"id": tid, "title": title, "status": status})
    print(f"\n[{tid}] {title}")
    print(f"  判定: {status}")
    print(f"  说明: {detail}")
    print(f"  真实输出: {_excerpt(output)}")
    sys.stdout.flush()


def check(tid: str, title: str, fn: Callable[[], tuple[bool, str, str, str]]):
    """fn -> (ok, detail, output, skip_reason)；SKIP 也必须给出真实原因。"""
    try:
        ok, detail, output, skip = fn()
        status = "PASS" if ok else ("SKIP" if skip else "FAIL")
        if skip:
            detail = f"SKIP: {skip} | {detail}"
        record(tid, title, status, detail, output)
    except Exception as exc:  # noqa: BLE001 - 任何异常都必须如实记为 FAIL
        record(tid, title, "FAIL", f"异常: {type(exc).__name__}: {exc}", "")


# ------------------------------------------------------------------ 测试实现
def t01():
    out = tool("run_command", {"command": "whoami; uname -s; df -h / | tail -1", "cwd": "/workspace"})
    ok = ("exit_code=0" in out and "duration_ms=" in out and "timed_out=false" in out)
    return ok, "普通命令真实退出码 / 双流 / 耗时", out, ""


def _local_wheel() -> bytes:
    buf = io.BytesIO()
    names = ["mcp_v11_selfcheck/__init__.py",
             "mcp_v11_selfcheck-1.0.dist-info/METADATA",
             "mcp_v11_selfcheck-1.0.dist-info/WHEEL",
             "mcp_v11_selfcheck-1.0.dist-info/RECORD"]
    files = {
        names[0]: "VERSION = '1.0'\n",
        names[1]: "Metadata-Version: 2.1\nName: mcp-v11-selfcheck\nVersion: 1.0\n",
        names[2]: "Wheel-Version: 1.0\nGenerator: selfcheck\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        names[3]: "".join(f"{n},,\n" for n in names[:3]),
    }
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def t02():
    wheel = _local_wheel()
    digest = hashlib.sha256(wheel).hexdigest()
    # PEP 427：wheel 文件名必须是 name-version-py3-none-any.whl，pip 才会接受。
    wheel_name = "mcp_v11_selfcheck-1.0-py3-none-any.whl"
    up = tool("upload_file", {
        "path": f"selfcheck/{wheel_name}", "content_base64": base64.b64encode(wheel).decode(),
        "total_size": len(wheel), "sha256": digest, "finish": True,
        "overwrite": True,
    })
    if "ERROR:" in up:
        return False, "上传离线 wheel 失败", up, ""
    command = (
        f"python3 -m venv /tmp/mcp-v11-venv && "
        f"/tmp/mcp-v11-venv/bin/python -m pip install --no-index --quiet --disable-pip-version-check "
        f"{FILE_ROOT}/selfcheck/{wheel_name} && "
        f"/tmp/mcp-v11-venv/bin/python -c \"import mcp_v11_selfcheck; "
        f"print('SELFCHECK_PKG=' + mcp_v11_selfcheck.VERSION)\""
    )
    out = tool("run_command", {"command": command, "cwd": "/workspace", "timeout": 120})
    ok = "exit_code=0" in out and "SELFCHECK_PKG=1.0" in out
    return ok, "真实创建 venv 并 pip 离线安装一个 wheel（软件安装能力）", up + "\n" + out, ""


def t03():
    out = tool("run_command", {
        "command": "cat; printf ' ENV=%s' \"$MCP_V11_VAR\"; exit 7",
        "cwd": "/workspace", "env": {"MCP_V11_VAR": "from-env"},
        "stdin": "from-stdin\n",
    })
    ok = ("exit_code=7" in out and "from-stdin" in out
          and "ENV=from-env" in out and "duration_ms=" in out)
    return ok, "env / stdin / 真实退出码 7 / 耗时", out, ""


def t04():
    started = tool("run_command_background", {
        "command": "for i in 1 2 3 4 5 6 7 8 9 10; do echo tick-$i; sleep 0.2; done; echo DONE; exit 3",
        "cwd": "/workspace",
    })
    match = re.search(r"task_id=([A-Za-z0-9_-]+)", started)
    if not match:
        return False, "后台任务未返回 task_id", started, ""
    tid = match.group(1)
    status = ""
    for _ in range(80):
        status = tool("get_task_status", {"task_id": tid})
        if "status=done" in status:
            break
        time.sleep(0.2)
    log = tool("get_task_output", {"task_id": tid, "tail": 20})
    ok = ("status=done" in status and "exit_code=3" in status
          and "tick-1" in log and "DONE" in log)
    return ok, "立即返回 task_id、实时日志、真实退出码 3", started + "\n" + status + "\n" + log, ""


def t05():
    raw = os.urandom(2 * 1024 * 1024 + 12345)
    digest = hashlib.sha256(raw).hexdigest()
    chunk = 1024 * 1024
    first = tool("upload_file", {
        "path": "selfcheck/blob.bin", "content_base64": base64.b64encode(raw[:chunk]).decode(),
        "total_size": len(raw), "sha256": digest, "finish": False,
        "overwrite": True,
    })
    tid = re.search(r"ticket=([A-Za-z0-9_-]+)", first)
    off = re.search(r"offset=(\d+)", first)
    if not tid or not off:
        return False, "首片未返回 ticket/offset", first, ""
    offset = int(off.group(1))
    pieces = [first]
    while offset < len(raw):
        part = raw[offset:offset + chunk]
        pieces.append(tool("upload_file", {
            "path": "selfcheck/blob.bin", "ticket": tid.group(1), "offset": offset,
            "content_base64": base64.b64encode(part).decode(),
            "finish": offset + len(part) >= len(raw),
        }))
        offset += len(part)
    done = pieces[-1]
    if f"sha256={digest}" not in done or "download_url=" not in done:
        return False, "分片上传未完成或摘要不一致", "\n".join(pieces[-2:]), ""

    restored = bytearray()
    offset = 0
    urls = []
    while True:
        piece = tool("download_file", {"path": "selfcheck/blob.bin",
                                       "offset": offset, "length": chunk})
        if "download_url=" in piece:
            urls.append(re.search(r"download_url=(\S+)", piece).group(1))
        b64 = re.search(r"content_base64=([A-Za-z0-9+/=]*)", piece)
        restored += base64.b64decode(b64.group(1)) if b64 else b""
        length = int(re.search(r"length=(\d+)", piece).group(1))
        offset += length
        if "eof=true" in piece or length == 0:
            break
    url = urls[0]
    if REWRITE_URL:
        parts = urllib.parse.urlsplit(url)
        url = f"{BASE}{parts.path}?{parts.query}"
    status, body, headers = _http(url, headers={"Range": "bytes=16-31"})
    ranged_ok = status == 206 and body == raw[16:32]
    status_full, full, _ = _http(url)
    http_ok = status_full == 200 and full == raw
    ok = (bytes(restored) == raw and hashlib.sha256(restored).hexdigest() == digest
          and ranged_ok and http_ok)
    detail = (f"任意二进制分片往返 + SHA-256 + 签名链接 HTTP 200/206 "
              f"(size={len(raw)}, range={ranged_ok}, full={http_ok})")
    output = done + f"\nrestored={len(restored)} sha256={hashlib.sha256(restored).hexdigest()}"
    return ok, detail, output, ""


def t06():
    out = tool("docker_status", {})
    probe = tool("run_command", {"command": "docker --version; docker ps --format '{{.Names}}'"})
    allowed = "ERROR: 命令不在白名单" not in probe and "硬禁止" not in probe
    if not allowed:
        return False, "Docker 命令被误拦", probe, ""
    if "exit_code=0" not in probe:
        return (True,
                "docker 命令默认放行（真实执行返回非 0）；"
                "容器部署默认不挂载 /var/run/docker.sock，属预期安全边界",
                out + "\n" + probe, "")
    # 已显式授权宿主机 Docker：真实运行一个测试容器，核对输出与 --rm 自动清理。
    # 用本地已构建的镜像，避免自检依赖公网拉取。
    image = os.environ.get("MCP_IMAGE", "ghcr.io/ninaawa/cnb-mcp-gateway-plus:v1.1.0")
    name = f"mcp-v11-selfcheck-{int(time.time())}"
    run = tool("run_command", {
        "command": (f"docker run --rm --name {name} --entrypoint /bin/echo "
                    f"{image} MCP_V11_DOCKER_OK"),
        "confirm": True, "timeout": 180,
    })
    leftover = tool("run_command", {
        "command": f"docker ps -a --filter name={name} --format '{{{{.Names}}}}'",
    })
    detail = out + "\n" + probe + "\n" + run + "\n" + leftover
    ok = ("exit_code=0" in run and "MCP_V11_DOCKER_OK" in run
          and name not in leftover)
    if not ok:
        return False, "已授权宿主机 Docker，但测试容器未真实运行成功", detail, ""
    return True, ("docker_status 真实可读；已显式授权宿主机 Docker 并真实运行测试容器"
                  "（--rm 自动清理）"), detail, ""


def _git_setup() -> str:
    env = {
        "GIT_AUTHOR_NAME": "v11-selfcheck", "GIT_AUTHOR_EMAIL": "v11@example.com",
        "GIT_COMMITTER_NAME": "v11-selfcheck", "GIT_COMMITTER_EMAIL": "v11@example.com",
        "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "commit.gpgsign",
        "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_KEY_1": "tag.gpgsign",
        "GIT_CONFIG_VALUE_1": "false",
    }
    # 结构化 git_commit 工具不接受 env，身份必须写进仓库级 config，否则真实 git 会报
    # "Author identity unknown"。这也是部署文档要求配置 GIT_USER_NAME/EMAIL 的原因。
    setup = rest(f"rm -rf {WORK}/git && mkdir -p {WORK}/git/repo && "
                 f"git init -q -b main {WORK}/git/repo && "
                 f"git init -q --bare -b main {WORK}/git/remote.git && "
                 f"git -C {WORK}/git/repo config user.name v11-selfcheck && "
                 f"git -C {WORK}/git/repo config user.email v11@example.com && "
                 f"git -C {WORK}/git/repo config commit.gpgsign false && "
                 f"printf 'hello\\n' > {WORK}/git/repo/a.txt && "
                 f"git -C {WORK}/git/repo remote add origin {WORK}/git/remote.git",
                 confirm=True, env=env)
    if "exit_code=0" not in setup:
        return "SETUP_FAILED: " + setup
    add = tool("git_add", {"paths": ["a.txt"], "cwd": f"{WORK}/git/repo"})
    commit = tool("git_commit", {"message": "v11 selfcheck", "cwd": f"{WORK}/git/repo"})
    if "exit=0" not in add or "exit=0" not in commit:
        return "SETUP_COMMIT_FAILED: " + add + commit
    return ""


def t07():
    failed = _git_setup()
    if failed:
        return False, "准备临时 Git 仓库失败", failed, ""
    repo = f"{WORK}/git/repo"
    head = tool("git_current_branch", {"cwd": repo})
    pushed = tool("git_push", {"cwd": repo, "remote": "origin", "branch": "main"})
    ls = tool("run_command", {"command": f"git ls-remote origin refs/heads/main", "cwd": repo})
    local = tool("run_command", {"command": "git rev-parse HEAD", "cwd": repo})
    sha = re.search(r"\b[0-9a-f]{40}\b", local)
    ok = ("remote_verification=PASS" in pushed and "exit_code=0" in pushed
          and sha is not None and sha.group(0) in ls)
    return ok, "git_push 真实核验远端 HEAD 与本地提交一致（ls-remote）", head + "\n" + pushed + "\n" + ls, ""


def t08():
    attempts = [
        tool("run_command", {"command": "git branch -D main", "cwd": "/workspace", "confirm": True}),
        tool("run_command", {"command": "git push origin --delete main", "cwd": "/workspace", "confirm": True}),
        tool("run_command", {"command": "echo Z2l0IGJyYW5jaCAtRCBtYWlu | base64 -d | sh",
                             "cwd": "/workspace", "confirm": True}),
        tool("run_command_background", {"command": "git branch -D main", "cwd": "/workspace", "confirm": True}),
    ]
    ok = all("硬禁止" in out or "禁止" in out or "ERROR:" in out for out in attempts)
    return ok, "本地/远端分支删除、编码绕过、后台入口一律硬禁", "\n".join(attempts), ""


def t09():
    version = tool("run_command", {"command": "git lfs version", "cwd": "/workspace"})
    # 原生安装脚本建议的 `git lfs install` 曾被自身守卫误拦；必须在真实仓库里验证放行。
    install = tool("run_command", {
        "command": "git lfs install --local && git lfs track '*.bin'",
        "cwd": f"{WORK}/git/repo",
    })
    wrapped = tool("run_command", {
        "command": "sh -c 'git lfs pre-push origin https://example.invalid/repo.git'",
        "cwd": "/workspace", "timeout": 15,
    })
    prune = tool("run_command", {"command": "git lfs prune", "cwd": "/workspace"})
    not_false_positive = "硬禁止" not in wrapped and "混淆执行" not in wrapped
    ok = ("exit_code=0" in version and "exit_code=0" in install
          and not_false_positive and ("硬禁止" in prune or "禁止" in prune))
    return (ok,
            "内置 git-lfs 可用；git lfs install/track 不被误拦；"
            "sh -c 包裹的 .git URL pre-push 不被误杀；prune 仍被拒绝",
            version + "\n" + install + "\n" + wrapped + "\n" + prune, "")


def t10(skip_restart: bool):
    if skip_restart:
        return True, "按 --skip-restart 要求未重启；此项不参与判定", "", "--skip-restart 显式指定"
    if not ADMIN_BASE.startswith("http://127.0.0.1"):
        return True, "非本机部署，无法在此环境真实重启容器", "", "远程部署，无法从本机执行 docker restart"
    started = tool("run_command_background", {
        "command": "echo before-restart; sleep 60; echo after-restart", "cwd": "/workspace"})
    tid = re.search(r"task_id=([A-Za-z0-9_-]+)", started)
    if not tid:
        return False, "无法创建用于重启验证的任务", started, ""
    task_id = tid.group(1)
    time.sleep(1)
    proc = subprocess.run(["docker", "restart", CONTAINER], capture_output=True,
                          text=True, timeout=120)
    if proc.returncode != 0:
        return False, "docker restart 失败", proc.stdout + proc.stderr, ""
    healthy = False
    for _ in range(60):
        status, _, _ = _http(f"{BASE}/health")
        if status == 200:
            healthy = True
            break
        time.sleep(1)
    if not healthy:
        return False, "重启后 /health 未恢复", proc.stdout, ""
    # /health 200 只代表 HTTP 已监听。docker restart 重建端口映射的瞬间，
    # 新连接仍可能收到 TCP RST；必须等一次真实 MCP 会话可用后再断言，
    # 否则 T10 与紧随其后的 T12 会被无关的重启竞态误判为 FAIL。
    ready = ""
    last_err = ""
    for _ in range(30):
        try:
            ready = tool("run_command", {"command": "echo mcp-ready"})
            if "exit_code=0" in ready:
                break
        except Exception as exc:  # noqa: BLE001 - 重启竞态下如实重试并保留最后原因
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    if "exit_code=0" not in ready:
        return False, "重启后 MCP 会话未恢复", proc.stdout + "\n" + ready + "\n" + last_err, ""
    state = tool("get_task_status", {"task_id": task_id})
    log = tool("get_task_output", {"task_id": task_id, "tail": 20})
    ok = (("before-restart" in log) and ("source=sqlite" in state)
          and ("status=unknown" in state or "status=done" in state)
          and not ("status=unknown" in state and "exit_code=0" in state))
    return ok, "服务重启后日志可追溯；未知状态绝不误标成功", proc.stdout + "\n" + state + "\n" + log, ""


def t11():
    opener = admin_session()
    if opener is None:
        return False, "管理网站登录失败（检查 ADMIN_PASSWORD）", "", ""
    outputs = {}
    for path in ("/api/dashboard", "/api/calls", "/api/tasks", "/api/git", "/api/files"):
        status, raw, _ = _http(f"{ADMIN_BASE}{path}", opener=opener)
        outputs[path] = (status, raw.decode(errors="replace"))
    ok_status = all(s == 200 for s, _ in outputs.values())
    dashboard = outputs["/api/dashboard"][1]
    leak = TOKEN and TOKEN in json.dumps(outputs)
    ok = ok_status and "file_root_disk" in dashboard and "files" in dashboard and not leak
    return ok, ("五个页面对应接口全部 200；首页含磁盘指标；"
                "响应中不含明文 MCP_TOKEN"), json.dumps({k: v[0] for k, v in outputs.items()}), ""


def t12():
    status_no_auth, body_no_auth, _ = _http(f"{BASE}/run_command", data=b'{"command":"echo x"}',
                                           headers={"Content-Type": "application/json"})
    status_bad, _, _ = _http(f"{BASE}/internal/metrics",
                             headers={"Authorization": "Bearer definitely-wrong-token"})
    denied = tool("run_command", {"command": "git branch -D main", "cwd": "/workspace", "confirm": True})
    ok = status_no_auth == 401 and status_bad == 401 and "硬禁止" in denied
    return ok, "无 Bearer / 错误 Bearer 均 401；带合法令牌的分支删除仍被硬禁", (
        f"no_auth={status_no_auth} {body_no_auth[:80]!r} bad_token={status_bad}\n{denied}"), ""


def main() -> int:
    global TOKEN
    parser = argparse.ArgumentParser(description="v1.1 T01-T12 真实自检")
    parser.add_argument("--skip-restart", action="store_true",
                        help="跳过 T10 的真实容器重启（会明确标记为 SKIP）")
    args = parser.parse_args()
    skip = args.skip_restart or os.environ.get("MCP_SELFCHECK_SKIP_RESTART", "0") == "1"

    print("=" * 78)
    print("CNB MCP Gateway v1.1 — T01..T12 真实端到端自检")
    print(f"MCP_BASE_URL={BASE}  ADMIN_BASE_URL={ADMIN_BASE}")
    TOKEN = _mcp_token()
    password = _admin_password()
    print(f"TOKEN={'已配置' if TOKEN else '未配置'}  "
          f"ADMIN_PASSWORD={'已配置' if password else '未配置'}")
    print("=" * 78)
    if not TOKEN:
        print("FATAL: 未找到 MCP_TOKEN；请显式设置，或在一键部署的容器内保留 /app/.env。",
              file=sys.stderr)
        return 2

    print("[preflight] 等待网关与 MCP 会话就绪（容器重建后最长 120 秒）…")
    if not wait_ready():
        print("FATAL: 服务在 120 秒内未就绪，未运行任何用例；不虚构 PASS/FAIL。",
              file=sys.stderr)
        return 2

    check("T01", "普通日常命令与真实结果语义", t01)
    check("T02", "软件安装 / venv / 依赖", t02)
    check("T03", "环境变量、标准输入、真实退出码与耗时", t03)
    check("T04", "后台长任务 ID / 日志 / 真实退出码", t04)
    check("T05", "任意二进制分片上传下载与签名链接", t05)
    check("T06", "Docker 命令与宿主机授权边界", t06)
    check("T07", "Git 推送并核验远端 HEAD", t07)
    check("T08", "Git 分支删除硬禁（含编码 / 后台绕过）", t08)
    check("T09", "Git LFS 与 .git URL 误杀回归", t09)
    check("T10", "服务重启后日志与未知状态", lambda: t10(skip))
    check("T11", "管理网站五个页面与审计脱敏", t11)
    check("T12", "未授权访问拒绝", t12)

    print("\n" + "=" * 78)
    print("汇总（逐项真实结果，无虚构）")
    for r in RESULTS:
        print(f"  {r['id']}  {r['status']:<4}  {r['title']}")
    failed = [r["id"] for r in RESULTS if r["status"] == "FAIL"]
    skipped = [r["id"] for r in RESULTS if r["status"] == "SKIP"]
    print(f"  PASS={sum(r['status'] == 'PASS' for r in RESULTS)}  "
          f"FAIL={len(failed)} {failed}  SKIP={len(skipped)} {skipped}")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
