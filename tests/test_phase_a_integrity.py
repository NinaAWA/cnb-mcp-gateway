"""阶段 A 回归：磁盘缓冲、归档摘要/恢复、统一严格门、OAuth 持久化、
后台 CSRF / 会话上限、任务日志写入时脱敏、审计导入失败显式告警。

全部离线，不依赖外部网络或 Docker。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time

import pytest

from gateway.audit.events import StrictAuditDenied, clear_trace

REDIRECT = "https://client.example.com/callback"
SECRET = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


# --------------------------------------------------------------- 磁盘 spool
def test_spool_replays_events_after_process_restart(isolated_storage, tmp_path):
    from gateway.audit import storage as storage_mod

    store = isolated_storage
    original = store.db_path
    store.db_path = "/proc/self/no-such-dir/audit.db"
    assert store.add_event("tool_call", detail="durable-event-v3") is False
    assert store.health()["spool_pending"] >= 1
    assert store.health()["spool_error"] == ""
    store.db_path = original

    # 模拟进程重启：只保留同一个 DB 文件与 spool 目录
    restarted = storage_mod.AuditStorage(db_path=original)
    assert restarted.health()["spool_pending"] == 0
    assert restarted.health()["spool_replayed"] >= 1
    details = [row["detail"] for row in restarted.recent_events(0, 100)]
    assert "durable-event-v3" in details
    # 重启后即使再读一次也不应重复补写（event_id 幂等标记）
    assert details.count("durable-event-v3") == 1
    assert restarted.confirm_persisted("req_missing") is False


def test_spool_files_are_private_and_bounded(tmp_path):
    from gateway.audit.spool import DiskSpool

    spool = DiskSpool(tmp_path / "spool", max_bytes=180, max_files=2, enabled=True)
    for index in range(12):
        assert spool.append({"sql": "INSERT INTO audit_events(ts) VALUES (?)",
                             "params": [f"e{index}"], "many": False})
    pending = list(spool.read_all())
    assert pending, "轮转后仍应保留部分待回放事件，不能全部丢弃"
    active = tmp_path / "spool" / "spool-active.jsonl"
    assert oct(active.stat().st_mode & 0o777) == "0o600"
    assert spool.pending() == len(pending)
    spool.clear()
    assert spool.pending() == 0 and list(spool.read_all()) == []


# ------------------------------------------------------- 归档摘要 / 恢复查询
def test_archive_manifest_verify_restore_and_corruption(isolated_storage,
                                                        make_call, tmp_path):
    store = isolated_storage
    make_call(store, request_id="req_archived", timestamp_start="2001-01-01T00:00:00.000+00:00",
              timestamp_end="2001-01-01T00:00:00.000+00:00")
    result = store.archive_and_prune(1, 0)
    assert result["errors"] == [] and result["archived"] == 1
    assert store.get_call("req_archived") is None

    files = list((tmp_path / "archive").glob("tool_calls-*.jsonl"))
    assert len(files) == 1
    path = files[0]
    manifest_path = path.with_suffix(".manifest.json")
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["row_count"] == 1 and manifest["table"] == "tool_calls"
    assert len(manifest["sha256"]) == 64

    checked = store.verify_archive(str(path))
    assert checked["ok"] is True and checked["rows"] == 1
    assert checked["sha256"] == manifest["sha256"]

    listed = store.list_archives({"table": "tool_calls"})
    assert listed and listed[0]["has_manifest"] is True
    assert listed[0]["status"] == "verified"
    assert store.query_archive("req_archived")
    assert store.query_archive("req_absent") == []

    restored = store.restore_archive(str(path))
    assert restored["ok"] is True and restored["restored"] == 1
    assert store.get_call("req_archived") is not None
    again = store.restore_archive(str(path))
    assert again["already_present"] == 1 and again["restored"] == 0
    assert store._query("SELECT COUNT(*) AS n FROM tool_calls")[0]["n"] == 1

    # 篡改归档内容后：校验必须失败，恢复必须拒绝，且原记录不被误删
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"request_id":"forged"}\n')
    broken = store.verify_archive(str(path))
    assert broken["ok"] is False and broken["reason"]
    assert "行数" in broken["reason"] or "摘要" in broken["reason"]
    assert store.restore_archive(str(path))["ok"] is False


def test_archive_failure_keeps_rows_and_is_reported(isolated_storage,
                                                     bulk_calls, tmp_path):
    from gateway import maintenance

    store = isolated_storage
    bulk_calls(store, 30, timestamp="2001-01-01T00:00:00.000+00:00")

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    original = store._write_archive
    store._write_archive = broken
    warnings: list[str] = []
    try:
        result = store.archive_and_prune(1, 0)
        assert result["archived"] == 0 and result["errors"]
        assert store._query("SELECT COUNT(*) AS n FROM tool_calls")[0]["n"] == 30
        monkeypatch_target = maintenance.warn
        maintenance.warn = lambda _s, msg: warnings.append(msg)
        try:
            maintenance.tick(store)
        finally:
            maintenance.warn = monkeypatch_target
        assert any("归档未完成" in message for message in warnings), warnings
    finally:
        store._write_archive = original


# ------------------------------------------------------------- 统一严格审计门
def test_rest_entry_uses_shared_strict_gate(isolated_storage, monkeypatch):
    from gateway.audit import middleware as mw

    monkeypatch.setattr(mw, "get_storage", lambda: isolated_storage)
    monkeypatch.setattr(mw, "STRICT", True)
    monkeypatch.setattr(isolated_storage, "confirm_persisted", lambda _rid: False)
    executed = {"value": False}
    try:
        with pytest.raises(StrictAuditDenied):
            with mw.trace_call("run_command", {"command": "echo ok"}, source="rest"):
                executed["value"] = True
        assert executed["value"] is False, "严格门拒绝后绝不能执行命令"
        rows, total = isolated_storage.query_calls({"source": "rest"})
        assert total == 1 and rows[0]["status"] == "DENIED"
        assert rows[0]["strict_denied"] == 1
        assert rows[0]["error_type"] == "AuditUnavailable"
    finally:
        clear_trace()

    # 非高风险工具不受门限制，避免严格模式把只读工具一并锁死
    try:
        with mw.trace_call("get_system_info", {}, source="rest") as rec:
            rec.ok("ok")
        assert isolated_storage.get_call(
            isolated_storage.query_calls({"tool_name": "get_system_info"})[0][0]["request_id"]
        )["status"] == "SUCCESS"
    finally:
        clear_trace()


def test_mcp_entry_uses_same_strict_gate(isolated_storage, monkeypatch):
    from gateway.audit import middleware as mw

    monkeypatch.setattr(mw, "get_storage", lambda: isolated_storage)
    monkeypatch.setattr(mw, "STRICT", True)

    def blocked(*_args, **_kwargs):
        raise StrictAuditDenied("blocked by shared gate")

    monkeypatch.setattr(mw, "strict_gate", blocked)
    called = {"value": False}

    async def call_next(_context):
        called["value"] = True
        return None

    class _Message:
        name = "run_command"
        arguments = {}

    class _Context:
        message = _Message()

    try:
        result = asyncio.run(mw.AuditMiddleware().on_call_tool(_Context(), call_next))
        assert called["value"] is False
        assert getattr(result, "is_error", False) is True
        assert "blocked by shared gate" in " ".join(
            str(getattr(block, "text", block)) for block in result.content)
    finally:
        clear_trace()


# ------------------------------------------------------------- OAuth 持久化
def test_oauth_tokens_are_hashed_persisted_hydrated_and_revoked(gateway_client):
    from gateway.audit.storage import get_storage
    from gateway.oauth.store import OAuthStore

    server, client, _ = gateway_client
    storage = get_storage()
    token = "persisted-token-" + secrets.token_hex(8)
    OAuthStore().save_token(token, time.time() + 600, "client-x")

    rows = storage._query("SELECT token_hash FROM oauth_tokens")
    assert rows
    assert token not in json.dumps(rows), "持久化层不得出现明文令牌"
    assert hashlib.sha256(token.encode()).hexdigest() in [r["token_hash"] for r in rows]

    server.ISSUED_TOKENS.clear()
    assert server._token_is_authorized(token) is True, "重启后应能按摘要水合"
    assert isinstance(server.ISSUED_TOKENS.get(token), float)

    response = client.post("/revoke", json={"token": token},
                           headers={"Authorization": f"Bearer {server.TOKEN}"})
    assert response.status_code == 200 and response.json()["ok"] is True
    assert token not in server.ISSUED_TOKENS
    server.ISSUED_TOKENS.clear()
    assert server._token_is_authorized(token) is False
    assert client.post("/revoke", json={},
                       headers={"Authorization": f"Bearer {server.TOKEN}"}
                       ).status_code == 400
    assert client.post("/revoke", json={"token": "x"}).status_code == 401


def test_oauth_clients_and_codes_survive_inmemory_reset(gateway_client):
    server, client, _ = gateway_client
    registered = client.post("/register", json={"redirect_uris": [REDIRECT]})
    assert registered.status_code == 201
    client_id = registered.json()["client_id"]
    verifier = "v" * 43
    query = {
        "response_type": "code", "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": server._pkce_s256(verifier),
        "code_challenge_method": "S256",
    }
    issued = client.post("/authorize", data={**query, "password": server.TOKEN},
                         follow_redirects=False)
    assert issued.status_code == 302
    import urllib.parse
    code = urllib.parse.parse_qs(
        urllib.parse.urlsplit(issued.headers["location"]).query)["code"][0]

    # 模拟重启：清空三个进程内字典，注册信息与授权码都应从持久层恢复
    server.CLIENTS.clear()
    server.AUTH_CODES.clear()
    server.ISSUED_TOKENS.clear()
    assert REDIRECT in server._oauth_client_uris(client_id)
    token = client.post("/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": client_id, "redirect_uri": REDIRECT,
        "code_verifier": verifier,
    })
    assert token.status_code == 200, token.text
    assert client.post("/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": client_id, "redirect_uri": REDIRECT,
        "code_verifier": verifier,
    }).status_code == 400, "授权码必须一次性消费，重启后也不能重放"


# ------------------------------------------------------- CSRF / 会话上限
def test_csrf_blocks_state_changes_and_exempts_login_healthz(admin_client, login):
    from fastapi.testclient import TestClient
    from gateway.admin.api import app

    client, _, _ = admin_client
    with TestClient(app) as raw:
        assert raw.get("/healthz").status_code == 200
        assert raw.get("/login").status_code == 200
        assert raw.cookies.get("mcp_admin_csrf")
        denied = raw.post("/api/config", json={"key": "page_size", "value": "10"})
        assert denied.status_code == 403
        assert "CSRF" in denied.json()["detail"]
        token = raw.cookies.get("mcp_admin_csrf")
        # 带正确 token 但没有登录会话：应落入 401 而不是被 CSRF 拦截
        assert raw.post("/api/config", json={"key": "page_size", "value": "10"},
                        headers={"X-CSRF-Token": token}).status_code == 401
        # 错误 Origin 即使带 token 也拒绝
        assert raw.post("/api/config", json={"key": "page_size", "value": "10"},
                        headers={"X-CSRF-Token": token,
                                 "Origin": "https://evil.example.com"}).status_code == 403

    assert login(client).status_code == 200, "登录端点豁免（尚无已认证会话）"
    assert client.post("/api/config", json={"key": "page_size", "value": "12"}).json()["ok"]
    assert client.post("/api/config", json={"key": "page_size", "value": "13"},
                       headers={"Origin": "https://evil.example.com"}).status_code == 403


def test_admin_max_sessions_revokes_oldest(admin_client):
    from gateway.admin.auth import get_auth

    _, storage, _ = admin_client
    auth = get_auth()
    auth.max_sessions = 2
    for _ in range(4):
        ok, message, _cookie = auth.login("admin", "TestPassword-123", "",
                                          "127.0.0.1", "pytest")
        assert ok, message
    active = [row for row in auth.sessions() if row["active"]]
    assert len(active) <= 2
    assert storage._query(
        "SELECT COUNT(*) AS n FROM admin_sessions WHERE revoked=0")[0]["n"] <= 2


# ------------------------------------------------------------ 脱敏与启动告警
def test_task_log_is_redacted_before_hitting_disk(gateway_module, tmp_path):
    server = gateway_module
    log_path = tmp_path / "task-output.log"
    read_fd, write_fd = os.pipe()
    worker = server._spawn_redacting_logger(log_path, read_fd)
    os.write(write_fd, f"token={SECRET}\nplain line\n".encode())
    os.close(write_fd)
    worker.join(timeout=5)
    assert not worker.is_alive()
    text = log_path.read_text(encoding="utf-8")
    assert "ghp_" not in text and "***REDACTED***" in text
    assert "plain line" in text
    assert oct(log_path.stat().st_mode & 0o777) == "0o600"


def test_structured_event_helpers_use_redaction_boundary(isolated_storage,
                                                         monkeypatch):
    from gateway.audit import middleware as mw

    monkeypatch.setattr(mw, "get_storage", lambda: isolated_storage)
    assert mw.record_search_event({"tool_name": "search_web",
                                   "keyword": f"query {SECRET}",
                                   "provider": "mock", "status": "SUCCESS"}) is True
    assert mw.record_download_event({"paper_id": "p1", "status": "created"}) is True
    assert mw.record_transfer_event({"direction": "download", "status": "success"}) is True
    assert mw.record_task_state({"task_id": "t1", "status": "running"}) is True
    rows = isolated_storage._query("SELECT * FROM search_events")
    assert rows and rows[0]["status"] == "SUCCESS"
    assert "ghp_" not in json.dumps(rows)


def test_audit_import_failure_can_refuse_startup(gateway_module, monkeypatch):
    server = gateway_module
    monkeypatch.setattr(server, "AUDIT_ENABLED", False)
    monkeypatch.setattr(server, "AUDIT_IMPORT_ERROR", "simulated import failure")
    monkeypatch.setenv("AUDIT_REQUIRED", "1")
    monkeypatch.delenv("AUDIT_ALLOW_UNAUDITED", raising=False)
    with pytest.raises(SystemExit) as exc:
        server._audit_startup_gate()
    assert "拒绝以无审计模式启动" in str(exc.value)
    monkeypatch.setenv("AUDIT_ALLOW_UNAUDITED", "1")
    server._audit_startup_gate()  # 显式承担风险时放行
    assert server.AUDIT_ALLOW_UNAUDITED is True
