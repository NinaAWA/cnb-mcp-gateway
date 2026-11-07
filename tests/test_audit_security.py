"""安全边界：明文凭据、身份来源、会话、下载签名、日志路径与严格模式。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.audit.events import clear_trace
from gateway.audit.redaction import REDACTED


def test_no_plaintext_credentials_are_persisted(isolated_storage, monkeypatch):
    from gateway.audit import middleware as mw

    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    monkeypatch.setattr(mw, "get_storage", lambda: isolated_storage)
    try:
        with mw.trace_call("run_command",
                           {"command": "echo ok", "api_key": secret,
                            "password": "hunter2", "env": "MY_TOKEN=" + secret},
                           source="rest") as rec:
            rec.ok("deployed with Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
        rows, _ = isolated_storage.query_calls({})
        blob = repr(rows) + repr(isolated_storage.recent_events(0, 100))
        assert secret not in blob
        assert "hunter2" not in blob
        assert "abcdefghijklmnopqrstuvwxyz" not in blob
        assert REDACTED in blob
    finally:
        clear_trace()


def test_client_identity_comes_from_verified_token_only():
    from gateway.audit import middleware as mw
    from gateway.audit.redaction import fingerprint

    scope = {
        "type": "http",
        "headers": [
            (b"authorization", b"Bearer verified-token-abcdef"),
            (b"x-mcp-client", b"codex-cli/1.0"),
            (b"x-forwarded-for", b"203.0.113.7"),
        ],
        "client": ("10.0.0.5", 12345),
    }
    try:
        trace = mw.bind_http_context(scope)
        assert trace.client_id == fingerprint("verified-token-abcdef")
        assert trace.actor_id == trace.client_id
        assert "verified-token-abcdef" not in trace.client_id
        assert trace.declared_client == "codex-cli/1.0"
        # 默认不信任客户端自报的 XFF，来源 IP 取直连地址
        assert trace.remote_ip == "10.0.0.5"
        original_trust = mw.TRUST_PROXY
        try:
            mw.TRUST_PROXY = True
            assert mw.bind_http_context(scope).remote_ip == "203.0.113.7"
        finally:
            mw.TRUST_PROXY = original_trust
        assert mw.bind_http_context({"type": "websocket", "headers": []}) is None
    finally:
        clear_trace()


def test_admin_cookie_tampering_and_login_lockout(admin_client, login):
    client, storage, _ = admin_client
    login(client)
    original = client.cookies.get("mcp_admin_session")
    client.cookies.clear()
    client.cookies.set("mcp_admin_session", original + "tampered")
    assert client.get("/api/calls").status_code == 401
    client.cookies.clear()
    client.cookies.set("mcp_admin_session", original)
    assert client.get("/api/calls").status_code == 200

    for _ in range(5):
        assert client.post("/api/login", json={"username": "admin",
                                              "password": "bad", "otp": ""}).status_code == 401
    locked = client.post("/api/login", json={"username": "admin",
                                             "password": "TestPassword-123", "otp": ""})
    assert locked.status_code == 401 and "过多" in locked.json()["message"]
    attempts = storage._query("SELECT outcome FROM login_attempts")
    assert sum(1 for a in attempts if a["outcome"] == "locked") >= 1


def test_mfa_is_required_when_enabled(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from gateway.admin import api as api_mod
    from gateway.admin import auth as auth_mod
    from gateway.audit import storage as storage_mod

    secret = "JBSWY3DPEHPK3PXP"
    monkeypatch.setenv("ADMIN_TOTP_SECRET", secret)
    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "mfa.db"))
    storage_mod._STORAGE = None
    auth_mod._AUTH = None
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    counter = int(time.time() // 30)
    mac = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = f"{(struct.unpack('>I', mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"
    try:
        with TestClient(api_mod.app) as client:
            body = {"username": "admin", "password": "TestPassword-123", "otp": ""}
            assert client.post("/api/login", json=body).status_code == 401
            body["otp"] = code
            assert client.post("/api/login", json=body).status_code == 200
    finally:
        storage_mod._STORAGE = None
        auth_mod._AUTH = None


def test_download_requires_session_or_valid_signature(admin_client, login, make_call):
    from fastapi.testclient import TestClient
    from gateway.admin.api import app

    client, storage, _ = admin_client
    make_call(storage, request_id="req_download")
    login(client)
    job = client.post("/api/exports", json={"fmt": "jsonl", "filters": {}}).json()["job_id"]
    for _ in range(100):
        row = storage.get_export(job)
        if row and row["status"] == "done":
            break
        time.sleep(0.02)
    link = client.get(f"/api/exports/{job}/link").json()["url"]
    with TestClient(app) as anon:
        assert anon.get(f"/api/exports/{job}/download").status_code == 401
        assert anon.get(link + "x").status_code == 401
        assert anon.get(link.replace("token=", "token=1.")).status_code == 401
        assert anon.get(link).status_code == 200
    assert client.get(link).status_code == 200


def test_experiment_logs_are_confined_to_task_dir(admin_client, login, tmp_path):
    client, storage, logs = admin_client
    login(client)
    (logs / "task_ok.log").write_text("training output line", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("TOP_SECRET_CONTENT", encoding="utf-8")

    storage.add_task({"task_id": "task_ok", "status": "running",
                      "log_path": str(logs / "task_ok.log")})
    storage.add_task({"task_id": "task_bad", "status": "running", "log_path": str(outside)})
    storage.add_task({"task_id": "task_up", "status": "running",
                      "log_path": str(logs / ".." / "secret.txt")})

    assert "training output line" in client.get("/api/experiments/task_ok/logs").json()["log"]
    for task_id in ("task_bad", "task_up"):
        text = client.get(f"/api/experiments/{task_id}/logs").json()["log"]
        assert "TOP_SECRET_CONTENT" not in text
        assert "不在允许" in text
    assert client.get("/api/experiments/../etc/logs").status_code in (400, 404)


def test_strict_mode_rejects_high_risk_when_storage_unavailable(monkeypatch):
    import asyncio
    from gateway.audit import middleware as mw

    fake = MagicMock()
    fake.available.return_value = False
    monkeypatch.setattr(mw, "get_storage", lambda: fake)
    monkeypatch.setattr(mw, "STRICT", True)
    context = SimpleNamespace(message=SimpleNamespace(name="run_command", arguments={}))

    async def must_not_run(_context):
        raise AssertionError("高风险工具在审计不可用时不应执行")

    try:
        result = asyncio.run(mw.AuditMiddleware().on_call_tool(context, must_not_run))
        assert result.is_error is True
        assert "审计存储不可用" in mw.result_text(result)
    finally:
        clear_trace()


def test_read_path_redacts_legacy_plaintext(admin_client, login):
    """绕过采集层直接写入的明文，不能从列表 / 详情 / 导出 / SSE 泄露。"""
    from gateway.audit.events import ToolCallRecord, utcnow

    client, storage, _ = admin_client
    storage.insert_call(ToolCallRecord(
        request_id="req_legacy", trace_id="trc_legacy",
        timestamp_start=utcnow(), timestamp_end=utcnow(),
        tool_name="run_command", status="SUCCESS", client_id="tok_test",
        declared_client="legacy", source="mcp",
        arguments='{"command":"echo MY_TOKEN=supersecretvalue"}',
        result="postgresql://mcp:supersecretvalue@db:5432/audit",
        duration_ms=1,
    ))
    login(client)
    for path in ("/api/calls", "/api/calls/req_legacy", "/api/live/recent?after=0",
                 "/api/export/stream?fmt=json", "/api/export/stream?fmt=csv"):
        assert "supersecretvalue" not in client.get(path).text, path
    rows, _ = storage.query_calls({})
    assert "supersecretvalue" not in repr(rows)
