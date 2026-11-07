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
        assert trace.remote_ip == "203.0.113.7"
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
    fake.confirm_persisted.return_value = False
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


def test_derived_git_and_experiment_fields_are_redacted():
    from gateway.audit.enrich import enrich

    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    git = enrich(
        {"tool_name": "run_command", "request_id": "r1", "status": "SUCCESS"},
        {"command": f"git push https://user:pass@git.example.com/repo main -m 'token={secret}'"},
        "On branch main",
    )
    experiment = enrich(
        {"tool_name": "run_command_background", "request_id": "r2", "status": "SUCCESS"},
        {"command": f"python train.py --api_key {secret}"},
        "OK: 后台任务已启动",
    )
    for derived in (git, experiment):
        assert secret not in repr(derived)
        assert "user:pass@" not in repr(derived)
        assert REDACTED in repr(derived)


def test_legacy_file_audit_log_is_redacted(gateway_client, monkeypatch):
    server, _, _ = gateway_client
    captured: list[str] = []
    monkeypatch.setattr(server.AUDIT, "info", captured.append)
    server.audit("cmd token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
    assert captured and REDACTED in captured[0]
    assert "ghp_" not in captured[0]


def test_legacy_plaintext_never_leaves_via_subtables_archive_or_export(
        admin_client, login, tmp_path, monkeypatch):
    """模拟修复前绕过脱敏写入的存量行：后台读取、导出、归档都不能再泄露。"""
    from gateway.audit import storage as storage_mod
    from gateway.audit.events import utcnow

    client, storage, _ = admin_client
    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    old = "2001-01-01T00:00:00.000+00:00"
    monkeypatch.setattr(storage_mod, "redact_params", lambda params: list(params))

    storage._insert("tool_calls", {
        "request_id": "legacy_old", "trace_id": "tr", "timestamp_start": old,
        "tool_name": "run_command", "arguments": secret, "result": secret,
        "status": "SUCCESS"})
    storage._insert("tool_calls", {
        "request_id": "legacy_new", "trace_id": "tr", "timestamp_start": utcnow(),
        "tool_name": "run_command", "arguments": secret, "result": secret,
        "status": "SUCCESS"})
    subtables = (
        ("audit_events", {"ts": old, "kind": "legacy", "detail": secret}),
        ("background_tasks", {"ts": old, "task_id": "t_old", "command": secret}),
        ("git_operations", {"ts": old, "operation": "push", "commit_message": secret,
                            "push_remote": "https://user:pass@git.example.com/repo"}),
        ("experiment_runs", {"ts": old, "experiment_id": "e_old", "command": secret}),
        ("approval_events", {"ts": old, "approval_id": "a_old", "detail": secret}),
        ("login_attempts", {"ts": old, "username": "u", "detail": secret}),
        ("export_jobs", {"ts": old, "job_id": "j_old", "filters": secret}),
        ("admin_sessions", {"session_id": "s_old", "expires_at": old,
                            "user_agent": secret}),
        ("client_sessions", {"session_id": "c_old", "last_seen": old,
                             "declared_client": secret}),
    )
    for table, values in subtables:
        assert storage._insert(table, values), table

    login(client)
    assert secret not in client.get("/api/calls").text
    assert secret not in client.get("/api/calls/legacy_new").text
    for fmt in ("csv", "json", "jsonl"):
        response = client.get(f"/api/export/stream?fmt={fmt}")
        assert response.status_code == 200
        assert secret not in response.text, fmt
        assert REDACTED in response.text, fmt

    result = storage.archive_and_prune(1, 0)
    assert result["errors"] == []
    assert result["archived_by_table"]["tool_calls"] == 1
    for table, _ in subtables:
        assert result["archived_by_table"].get(table) == 1, table
    archive_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "archive").glob("*.jsonl"))
    assert archive_text
    assert secret not in archive_text
    assert "user:pass@" not in archive_text
    assert REDACTED in archive_text


def test_confirm_strings_are_not_mistaken_for_true(gateway_client, tmp_path):
    server, client, _ = gateway_client
    assert [server._strict_confirm(v) for v in
            (True, False, "true", "TRUE", "false", "0", "no", 1, 0, None, {})] == [
        True, False, True, True, False, False, False, True, False, False, False]

    target = tmp_path / "confirm-gate"
    target.mkdir()
    response = client.post(
        "/run_command",
        json={"command": f"rm -rf {target}", "confirm": "false"},
        headers={"Authorization": f"Bearer {server.TOKEN}"},
    )
    assert response.status_code == 200
    assert "需明确确认" in response.json()["output"]
    assert target.exists(), "字符串 'false' 不能被当成已确认而真的执行删除"
