"""测试公共夹具：每个用例使用独立 SQLite 文件，绝不写宿主 /data。"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 在测试进程内强制固定配置：模块 import 时就会读取，且绝不继承宿主机 shell / .env。
os.environ.update({
    "AUDIT_DB_PATH": "/tmp/mcp-audit-tests/audit.db",
    "AUDIT_EXPORT_DIR": "/tmp/mcp-audit-tests/exports",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "TestPassword-123",
    "ADMIN_SESSION_SECRET": "test-session-secret",
    "ADMIN_SESSION_TTL": "3600",
    "GATEWAY_URL": "http://127.0.0.1:9",
    # 网关模块在 import 时读取这些值；固定为测试值，绝不继承宿主 .env / shell。
    "MCP_TOKEN": "test-mcp-token",
    "MCP_SESSION_TTL": "3600",
    "AUDIT_STRICT": "0",
    "GATEWAY_METRICS_TOKEN_FILE": "/tmp/mcp-audit-tests/.metrics_token",
})
for _name in ("ADMIN_TOTP_SECRET", "AUDIT_DB_DSN", "GATEWAY_METRICS_TOKEN",
              "OAUTH_ALLOW_ANY_REDIRECT", "OAUTH_ALLOW_NO_PKCE",
              "MCP_OAUTH_TOKEN_TTL", "AUDIT_ARCHIVE_BATCH"):
    os.environ.pop(_name, None)
# 网关模块级测试共用一个固定 DB；清理遗留磁盘 spool，避免跨轮次回放旧事件。
for _stale in Path("/tmp/mcp-audit-tests/spool").glob("*.jsonl"):
    _stale.unlink(missing_ok=True)
os.environ.setdefault("AUDIT_REQUIRED", "0")
os.environ.setdefault("AUDIT_ALLOW_UNAUDITED", "1")
os.environ.setdefault("ADMIN_MAX_SESSIONS", "8")


class CsrfTestClientMixin:
    """自动携带双提交 CSRF token：保持既有 POST/DELETE 用例无需逐个改签名。"""

    def request(self, method, url, **kwargs):
        if str(method).upper() in ("POST", "PUT", "PATCH", "DELETE"):
            token = self.cookies.get("mcp_admin_csrf")
            if token:
                headers = dict(kwargs.pop("headers", None) or {})
                headers.setdefault("X-CSRF-Token", token)
                kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


def csrf_test_client(app):
    from fastapi.testclient import TestClient

    class _Client(CsrfTestClientMixin, TestClient):
        pass

    return _Client(app)


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    from gateway.audit import storage as storage_mod

    db = tmp_path / "audit.db"
    monkeypatch.setenv("AUDIT_DB_PATH", str(db))
    storage_mod._STORAGE = None
    store = storage_mod.AuditStorage(db_path=str(db))
    # 让 get_storage() 单例也指向隔离库：新模块的 library/download 事件入口
    # 通过 get_storage() 取存储，必须与夹具一致。
    storage_mod._STORAGE = store
    yield store
    storage_mod._STORAGE = None


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    """返回 (TestClient, AuditStorage, 日志目录)；用例需自行登录。"""
    from gateway.admin import api as api_mod
    from gateway.admin import auth as auth_mod
    from gateway.audit import storage as storage_mod

    db = tmp_path / "audit.db"
    monkeypatch.setenv("AUDIT_DB_PATH", str(db))
    monkeypatch.setenv("ADMIN_PASSWORD", "TestPassword-123")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:9")
    storage_mod._STORAGE = None
    auth_mod._AUTH = None
    logs = (tmp_path / "task-logs").resolve()
    logs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(api_mod, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(api_mod, "TASK_LOG_DIR", logs)
    with csrf_test_client(api_mod.app) as client:
        yield client, storage_mod.get_storage(), logs
    storage_mod._STORAGE = None
    auth_mod._AUTH = None


@pytest.fixture
def make_call():
    from gateway.audit.events import ToolCallRecord, utcnow

    def _make(storage, **kwargs):
        defaults = {
            "request_id": "req_" + uuid.uuid4().hex[:12],
            "trace_id": "trc_test",
            "timestamp_start": utcnow(),
            "timestamp_end": utcnow(),
            "tool_name": "run_command",
            "status": "SUCCESS",
            "client_id": "tok_testclient",
            "declared_client": "codex-cli/1.0",
            "arguments": '{"command":"git status"}',
            "result": "ok",
            "source": "mcp",
            "duration_ms": 10,
        }
        defaults.update(kwargs)
        record = ToolCallRecord(**defaults)
        storage.insert_call(record)
        return record

    return _make


@pytest.fixture
def login():
    def _login(client, password="TestPassword-123", username="admin", otp=""):
        return client.post("/api/login", json={
            "username": username, "password": password, "otp": otp})

    return _login


@pytest.fixture
def bulk_calls():
    """直接批量灌入历史数据；生产写入仍统一走 _execute 脱敏边界。

    允许注入「修复前的明文行」，用于验证归档 / 导出对存量数据也脱敏。
    """
    import sqlite3

    from gateway.audit.events import utcnow

    def _seed(storage, count, *, timestamp="", secret="", status="SUCCESS"):
        ts = timestamp or utcnow()
        arguments = f'{{"command":"echo {secret}"}}' if secret else '{"command":"echo ok"}'
        conn = sqlite3.connect(storage.db_path)
        try:
            conn.executemany(
                "INSERT INTO tool_calls (request_id,trace_id,timestamp_start,"
                "timestamp_end,duration_ms,tool_name,arguments,result,status,"
                "client_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(f"bulk_{i}", "trc_bulk", ts, ts, i % 1000, "run_command",
                  arguments, "ok", status, "tok_test", "mcp")
                 for i in range(count)],
            )
            conn.commit()
        finally:
            conn.close()
        return count

    return _seed


@pytest.fixture(scope="module")
def gateway_module():
    """加载真实的 docker/server.py（含 FastMCP / ASGI 中间件 / OAuth 状态）。"""
    import importlib.util

    path = ROOT / "docker" / "server.py"
    spec = importlib.util.spec_from_file_location("mcp_gateway_server_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


@pytest.fixture
def gateway_client(gateway_module, tmp_path, monkeypatch):
    """每个用例重置 OAuth 状态，并注入独立的内部指标令牌。"""
    from starlette.testclient import TestClient

    gateway_module.CLIENTS.clear()
    gateway_module.AUTH_CODES.clear()
    gateway_module.ISSUED_TOKENS.clear()
    metrics_file = tmp_path / ".metrics_token"
    metrics_file.write_text("test-internal-metrics-token", encoding="utf-8")
    monkeypatch.setattr(gateway_module, "METRICS_TOKEN_FILE", metrics_file)
    monkeypatch.setattr(gateway_module, "OAUTH_ALLOW_ANY_REDIRECT", False)
    monkeypatch.setattr(gateway_module, "OAUTH_ALLOW_NO_PKCE", False)
    with TestClient(gateway_module.app) as client:
        yield gateway_module, client, "test-internal-metrics-token"


@pytest.fixture(autouse=True)
def _reset_v3_singletons():
    """V3 模块在 import 期或首次调用时缓存 env / 存储单例，逐用例复位避免串状态。

    只复位「已导入的」模块：不强制 import 生产模块，也不触碰测试自行构造的
    AuditStorage / TaskManager 实例。server.py 的 _TASKS 由 gateway_module
    夹具按 module 复用，这里仅清理其中的任务条目。
    """
    import sys

    caches = (
        ("gateway.access.policy", "_POLICY", None),
        ("gateway.oauth.store", "_STORE", None),
        ("gateway.tasks.manager", "_MANAGER", None),
        ("gateway.exec.backends", "_BACKENDS", {}),
        ("gateway.gitops.tools", "_REPO_LOCKS", {}),
    )
    for mod_name, attr, fresh in caches:
        module = sys.modules.get(mod_name)
        if module is not None and hasattr(module, attr):
            setattr(module, attr, dict(fresh) if isinstance(fresh, dict) else fresh)
    server = sys.modules.get("mcp_gateway_server_under_test")
    if server is not None and isinstance(getattr(server, "_TASKS", None), dict):
        server._TASKS.clear()
    yield
