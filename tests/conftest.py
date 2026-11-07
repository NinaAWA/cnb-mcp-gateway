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
})
os.environ.pop("ADMIN_TOTP_SECRET", None)
os.environ.pop("AUDIT_DB_DSN", None)


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    from gateway.audit import storage as storage_mod

    db = tmp_path / "audit.db"
    monkeypatch.setenv("AUDIT_DB_PATH", str(db))
    storage_mod._STORAGE = None
    store = storage_mod.AuditStorage(db_path=str(db))
    yield store
    storage_mod._STORAGE = None


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    """返回 (TestClient, AuditStorage, 日志目录)；用例需自行登录。"""
    from gateway.admin import api as api_mod
    from gateway.admin import auth as auth_mod
    from gateway.audit import storage as storage_mod
    from fastapi.testclient import TestClient

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
    with TestClient(api_mod.app) as client:
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
