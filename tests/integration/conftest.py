"""阶段 F 真实网络 / 真实 MCP 客户端集成测试的公共夹具（opt-in）。

运行：.venv/bin/python -m pytest -q -m integration tests/integration/
默认离线套件由根目录 pytest.ini 的 ``-m "not integration"`` 排除。

这里启动的是**真实的 uvicorn + ASGI 网关**（同一份 docker/server.py），
再用 mcp.ClientSession 经 streamable HTTP 连接，不是进程内直接调函数。
所有运行时数据落在临时目录，不触碰仓库的 workspace/ 或生产卷。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = Path(tempfile.mkdtemp(prefix="mcp-v3-integration-"))
TOKEN = "v3-integration-token"
TRANSFER_SECRET = "v3-integration-transfer-secret"
RESULT_PATH = Path(os.environ.get("MCP_V3_E2E_RESULT", "/tmp/mcp-v3-e2e-results.json"))

@pytest.fixture(scope="session", autouse=True)
def integration_env():
    """仅在本目录用例真正运行时覆盖环境，默认离线套件完全不受影响。

    父级 tests/conftest.py 已固定一批环境；这里改为集成测试专属隔离目录。
    不虚构联系邮箱：Unpaywall 缺 MCP_SEARCH_MAILTO 必须如实报 SOURCE_UNAVAILABLE。
    """
    os.environ.update({
        "MCP_TOKEN": TOKEN,
        "AUDIT_DB_PATH": str(RUNTIME_DIR / "audit.db"),
        "AUDIT_EXPORT_DIR": str(RUNTIME_DIR / "exports"),
        "GATEWAY_METRICS_TOKEN_FILE": str(RUNTIME_DIR / ".metrics_token"),
        "MCP_PAPER_DIR": str(RUNTIME_DIR / "papers"),
        "MCP_PAPER_TEXT_CACHE_DIR": str(RUNTIME_DIR / "paper-text"),
        "MCP_TRANSFER_DIR": str(RUNTIME_DIR / "transfers"),
        "MCP_TASK_LOG_DIR": str(RUNTIME_DIR / "task-logs"),
        "MCP_TRANSFER_SECRET": TRANSFER_SECRET,
        # 验收环境显式开启网页正文抓取；仓库默认仍是关闭（0）。
        "MCP_SEARCH_FETCH_ENABLED": "1",
        "AUDIT_STRICT": "0",
        "AUDIT_ALLOW_UNAUDITED": "1",
    })
    os.environ.pop("MCP_SEARCH_MAILTO", None)
    return RUNTIME_DIR


class LiveGateway:
    """真实 MCP streamable HTTP 客户端 + 网关 HTTP 端点的薄封装。"""

    def __init__(self, module, base: str, token: str):
        self.module = module
        self.base = base
        self.token = token

    def headers(self, token: str = "") -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

    async def _session_call(self, coro_factory, token: str, timeout: float):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with httpx.AsyncClient(headers=self.headers(token),
                                     timeout=timeout) as client:
            async with streamable_http_client(self.base + "/mcp",
                                              http_client=client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await coro_factory(session)

    def _run(self, coro_factory, token: str = "", timeout: float = 90):
        return asyncio.run(asyncio.wait_for(
            self._session_call(coro_factory, token, timeout), timeout=timeout))

    def list_tools(self, token: str = "", timeout: float = 30) -> list[str]:
        async def _list(session):
            result = await session.list_tools()
            return [t.name for t in result.tools]

        return self._run(_list, token=token, timeout=timeout)

    def call(self, name: str, arguments: dict | None = None, *,
             token: str = "", timeout: float = 120) -> str:
        async def _call(session):
            result = await session.call_tool(name, arguments or {})
            parts = []
            for item in result.content:
                text = getattr(item, "text", None)
                if text:
                    parts.append(text)
            return "\n".join(parts)

        return self._run(_call, token=token, timeout=timeout)


@pytest.fixture(scope="session")
def live_gateway(integration_env):
    """启动真实 uvicorn 服务；会话结束关闭，不留后台进程。"""
    import uvicorn

    spec = importlib.util.spec_from_file_location(
        "mcp_gateway_server_under_test", ROOT / "docker" / "server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    server = uvicorn.Server(uvicorn.Config(
        module.app, host="127.0.0.1", port=port,
        log_level="error", lifespan="on", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except Exception:  # noqa: BLE001 - 等待端口就绪
            pass
        time.sleep(0.1)
    else:
        server.should_exit = True
        pytest.fail("uvicorn 网关未能在 10 秒内就绪")

    yield SimpleNamespace(
        gateway=LiveGateway(module, base, TOKEN),
        module=module,
        base=base,
        token=TOKEN,
        transfer_secret=TRANSFER_SECRET,
        runtime_dir=RUNTIME_DIR,
    )
    server.should_exit = True
    thread.join(timeout=15)


@pytest.fixture
def record(request):
    """把工作流结果先缓存在用例上，由 makereport 按真实结论定稿。

    不能在断言之前就写 PASS：否则失败用例也会被记录成通过，验收报告会失真。
    """
    buffer: list[dict] = []
    request.node._v3_records = buffer

    def _record(workflow: str, declared: str, detail) -> None:
        # declared 仅是测试自称的结论；最终 status 由 makereport 用 pytest 结果覆盖。
        payload = {
            "workflow": workflow,
            "declared": declared,
            "detail": detail if isinstance(detail, str) else json.dumps(
                detail, ensure_ascii=False, default=str),
        }
        buffer.append(payload)
        print(f"[V3-E2E][{workflow}][{declared}] {payload['detail']}")

    return _record


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """按 pytest 真实结论定稿结果：失败用例绝不记为 PASS。"""
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return
    buffered = getattr(item, "_v3_records", [])
    results = getattr(item.config, "_v3_e2e_results", None)
    if results is None:
        results = []
        item.config._v3_e2e_results = results
    if report.passed:
        status = "PASS"
    elif report.skipped:
        status = "SKIPPED"
    else:
        status = "FAILED"
    for payload in buffered:
        results.append({**payload, "status": status})
    if not buffered and status in ("FAILED", "SKIPPED"):
        results.append({
            "workflow": item.name, "status": status,
            "detail": str(report.longrepr or "")[:1200]})


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: opt-in real-network / real-MCP-client tests")


def pytest_sessionfinish(session, exitstatus):
    results = getattr(session.config, "_v3_e2e_results", [])
    if results:
        RESULT_PATH.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[V3-E2E] 结果已写入 {RESULT_PATH}（{len(results)} 条）")
