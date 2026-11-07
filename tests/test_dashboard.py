"""Admin Web：登录拦截、仪表盘、实时、历史记录、详情、导出与配置。"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def test_unauthenticated_access_is_blocked(admin_client):
    client, _, _ = admin_client
    assert client.get("/", follow_redirects=False).status_code == 302
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    for path in ("/api/dashboard", "/api/calls", "/api/calls/anything",
                 "/api/live/recent", "/api/git", "/api/experiments", "/api/tasks",
                 "/api/clients", "/api/sessions", "/api/config",
                 "/api/exports", "/api/export/stream"):
        assert client.get(path).status_code == 401, path
    assert client.get("/login").status_code == 200
    assert client.get("/healthz").status_code == 200  # 健康检查不含调用数据


def test_login_logout_and_page_rendering(admin_client, login):
    client, _, _ = admin_client
    assert login(client, password="wrong").status_code == 401
    assert login(client).status_code == 200
    assert client.get("/api/me").json()["username"] == "admin"
    page = client.get("/")
    assert page.status_code == 200
    assert "window.APP" in page.text and "/static/app.js" in page.text
    assert client.get("/static/app.css").status_code == 200
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/calls").status_code == 401


def test_dashboard_and_monitoring_sources(admin_client, login, make_call):
    client, storage, _ = admin_client
    make_call(storage)
    login(client)
    data = client.get("/api/dashboard").json()
    assert data["stats"]["total"] == 1
    assert len(data["time_series"]["calls"]) == 24
    assert data["gateway"]["online"] is False  # 网关不可达时后台仍可用
    assert data["audit"]["healthy"] is True
    assert "scope_note" in data["monitoring"]
    monitoring = client.get("/api/monitoring").json()
    assert "非宿主机全机指标" in monitoring["scope_note"]
    assert "scope_note" in monitoring["admin_container"]
    assert client.get("/api/live/recent").status_code == 200


def test_calls_filters_sort_pagination_and_detail(admin_client, login, make_call):
    client, storage, _ = admin_client
    for i in range(6):
        make_call(storage, request_id=f"req_{i}", duration_ms=i * 10,
                  status="FAILED" if i == 2 else "SUCCESS",
                  tool_name="search_web" if i % 2 else "run_command")
    storage.add_event("tool_call", status="SUCCESS", request_id="req_0",
                      tool_name="run_command", detail="事件")
    storage.add_git({"request_id": "req_0", "repo_path": "/workspace",
                     "operation": "commit", "status": "SUCCESS"})
    login(client)

    first = client.get("/api/calls?limit=3").json()
    assert first["total"] == 6 and len(first["rows"]) == 3
    second = client.get("/api/calls?limit=3&offset=3").json()
    assert len(second["rows"]) == 3
    assert {r["request_id"] for r in first["rows"]}.isdisjoint(
        {r["request_id"] for r in second["rows"]})

    ascending = client.get("/api/calls?sort=duration_ms&order=asc").json()
    durations = [r["duration_ms"] for r in ascending["rows"]]
    assert durations == sorted(durations)
    assert client.get("/api/calls?status=FAILED").json()["total"] == 1
    assert client.get("/api/calls?tool_name=search_web").json()["total"] == 3
    assert client.get("/api/calls?keyword=hello").json()["total"] == 0

    detail = client.get("/api/calls/req_0").json()
    assert detail["call"]["tool_name"] == "run_command"
    assert len(detail["timeline"]) >= 1
    assert len(detail["git"]) == 1
    assert client.get("/api/calls/not-exist").status_code == 404


def test_exports_are_valid_documents(admin_client, login, make_call):
    client, storage, _ = admin_client
    make_call(storage, request_id="req_export")
    login(client)

    direct = client.get("/api/export/stream?fmt=csv")
    assert direct.status_code == 200 and "req_export" in direct.text
    assert direct.text.splitlines()[0].startswith("timestamp_start,")

    for fmt in ("json", "jsonl", "csv"):
        job = client.post("/api/exports", json={"fmt": fmt, "filters": {}}).json()["job_id"]
        for _ in range(100):
            row = storage.get_export(job)
            if row and row["status"] != "running":
                break
            time.sleep(0.02)
        assert row["status"] == "done" and row["row_count"] == 1
        link = client.get(f"/api/exports/{job}/link").json()["url"]
        downloaded = client.get(link)
        assert downloaded.status_code == 200
        if fmt == "json":
            assert isinstance(json.loads(downloaded.content), list)
        elif fmt == "jsonl":
            assert [json.loads(line) for line in downloaded.text.strip().splitlines()]
        else:
            assert downloaded.text.count("\n") >= 2


def test_config_and_session_management(admin_client, login):
    client, storage, _ = admin_client
    login(client)
    config = client.get("/api/config").json()
    assert "MCP_TOKEN" in config["sensitive"]
    assert config["sensitive"]["MCP_TOKEN"] in ("已配置", "未配置")
    assert "明文" in config["note"]
    assert client.post("/api/config", json={"key": "page_size", "value": "100"}).json()["ok"]
    assert storage.get_settings()["page_size"] == "100"
    assert client.get("/api/sessions").json()["rows"]
    assert client.delete("/api/sessions/fake").status_code == 200
    assert len(client.get("/api/clients").json()["rows"]) == 0


def test_full_export_and_stream_never_truncate(admin_client, login, bulk_calls):
    client, storage, _ = admin_client
    bulk_calls(storage, 1200)
    login(client)

    csv_response = client.get("/api/export/stream?fmt=csv")
    assert csv_response.status_code == 200
    assert len(csv_response.text.strip().splitlines()) == 1201  # 表头 + 1200 行
    assert len(client.get("/api/export/stream?fmt=jsonl").text.strip().splitlines()) == 1200
    assert len(json.loads(client.get("/api/export/stream?fmt=json").text)) == 1200

    job = client.post("/api/exports", json={"fmt": "jsonl", "filters": {}}).json()["job_id"]
    for _ in range(300):
        row = storage.get_export(job)
        if row and row["status"] != "running":
            break
        time.sleep(0.02)
    assert row["status"] == "done" and row["row_count"] == 1200


def test_clients_active_count_respects_session_ttl(admin_client, login, monkeypatch):
    from gateway.admin import api as api_mod
    from gateway.audit import storage as storage_mod

    client, storage, _ = admin_client
    monkeypatch.setattr(storage_mod, "SESSION_TTL", 3600)
    monkeypatch.setattr(api_mod, "SESSION_TTL", 3600)
    stale = (datetime.now(timezone.utc)
             - timedelta(hours=2)).isoformat(timespec="milliseconds")
    storage._insert("client_sessions", {
        "session_id": "idle", "last_seen": stale, "status": "active"})
    storage.upsert_session("fresh", "tok_fresh")
    login(client)

    data = client.get("/api/clients").json()
    assert data["total"] == 2 and data["active"] == 1
    assert data["ttl_seconds"] == 3600
    assert {r["session_id"]: r["status"] for r in data["rows"]} == {
        "idle": "expired", "fresh": "active"}
    assert client.get("/api/dashboard").json()["active_sessions"] == 1


def test_public_health_is_minimal_and_details_require_auth(gateway_client):
    server, client, internal_token = gateway_client
    assert "/health" in server.OAUTH_PATHS
    assert "/health/details" not in server.OAUTH_PATHS

    health = client.get("/health")
    assert health.status_code == 200
    assert set(health.json()) == {"status", "service", "uptime_seconds", "audit"}
    assert set(health.json()["audit"]) == {"healthy"}
    assert "process" not in health.text.lower()

    assert client.get("/health/details").status_code == 401
    for token in (server.TOKEN, internal_token):
        details = client.get("/health/details",
                             headers={"Authorization": f"Bearer {token}"})
        assert details.status_code == 200
        assert "tools_registered" in details.json() and "metrics" in details.json()
    assert client.get("/mcp",
                      headers={"Authorization": f"Bearer {internal_token}"}).status_code == 401


def test_gateway_health_uses_internal_token_and_falls_back(admin_client, login,
                                                           tmp_path, monkeypatch):
    from gateway.admin import api as api_mod

    token = "internal-readonly-token"
    token_file = tmp_path / ".metrics_token"
    token_file.write_text(token, encoding="utf-8")
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                code, body = 200, {"status": "ok", "service": "mcp-gateway",
                                   "uptime_seconds": 1, "audit": {"healthy": True}}
            elif self.path == "/health/details":
                seen.append(self.headers.get("Authorization", ""))
                if self.headers.get("Authorization") == f"Bearer {token}":
                    code, body = 200, {"status": "ok", "service": "mcp-gateway",
                                       "tools_registered": 21, "audit": {"healthy": True},
                                       "metrics": {"cpu_percent": 1.0}}
                else:
                    code, body = 401, {"error": "unauthorized"}
            else:
                code, body = 404, {}
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(api_mod, "GATEWAY_URL",
                        f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(api_mod, "METRICS_TOKEN_FILE", token_file)
    monkeypatch.delenv("GATEWAY_METRICS_TOKEN", raising=False)
    try:
        health = api_mod._gateway_health()
        assert health["detail_level"] == "full" and health["tools_registered"] == 21
        assert seen == [f"Bearer {token}"]

        client, _, _ = admin_client
        login(client)
        dashboard = client.get("/api/dashboard").json()
        assert dashboard["gateway"]["online"] is True
        assert dashboard["gateway"]["tools_registered"] == 21
        assert dashboard["monitoring"]["gateway_metrics"]["cpu_percent"] == 1.0
        assert client.get("/api/monitoring").json()["gateway_container"]["cpu_percent"] == 1.0

        token_file.write_text("wrong-token", encoding="utf-8")
        degraded = api_mod._gateway_health()
        assert degraded["detail_level"] == "minimal"
        assert "tools_registered" not in degraded and "metrics" not in degraded
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
