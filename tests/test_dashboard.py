"""Admin Web：登录拦截、仪表盘、实时、历史记录、详情、导出与配置。"""
from __future__ import annotations

import json
import time


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


def test_files_view_and_admin_file_transfer(admin_client, login, tmp_path, monkeypatch):
    import base64
    import hashlib

    client, storage, _ = admin_client
    root = tmp_path / "files"
    root.mkdir()
    monkeypatch.setenv("MCP_FILE_ROOT", str(root))
    from gateway.files import service
    service.record_transfer("upload_file", "blob.bin", direction="upload",
                            status="SUCCESS", size=5, sha256="a" * 64)
    login(client)

    data = client.get("/api/files").json()
    assert data["summary"]["transfers"] == 1
    assert data["rows"][0]["path"] == "blob.bin"
    assert "分片内容" in data["note"]

    payload = b"hello-binary\x00\xff"
    uploaded = client.post("/files/upload", json={
        "path": "hello.bin", "content_base64": base64.b64encode(payload).decode(),
        "total_size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
    })
    assert uploaded.status_code == 200
    assert (root / "hello.bin").read_bytes() == payload

    downloaded = client.get("/files/download?path=hello.bin")
    assert downloaded.status_code == 200 and downloaded.content == payload
    assert client.get("/files/download?path=../escape").status_code == 404
    assert client.get("/api/files?keyword=hello.bin").json()["summary"]["transfers"] >= 1

    dashboard = client.get("/api/dashboard").json()
    assert "file_root_disk" in dashboard and "disks" in dashboard
    assert dashboard["file_root_disk"]["total_bytes"] > 0
    assert dashboard["files"]["uploads"] >= 1


def test_tasks_view_reports_persisted_unknown(admin_client, login, tmp_path, monkeypatch):
    import time as _time
    from gateway.admin import api as api_mod
    from gateway.tasks.store import TaskRecord, TaskStore

    client, _storage, _ = admin_client
    store = TaskStore(db_path=str(tmp_path / "tasks.db"))
    store.create(TaskRecord(
        task_id="unknown-task", command="python3 train.py", status="unknown",
        log_path=str(tmp_path / "unknown-task.log"), started_at=_time.time(),
        detail="服务重启：无法确认进程存活与真实退出码，状态未知",
    ))
    monkeypatch.setattr(api_mod, "get_task_store", lambda: store)
    login(client)
    payload = client.get("/api/tasks").json()
    assert payload["orphaned"][0]["status"] == "unknown"
    assert payload["orphaned"][0]["exit_code"] is None
    assert "unknown" in payload["note"]
    assert "tasks" in client.get("/api/dashboard").json()
