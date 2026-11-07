"""阶段 E：搜索监控 / 论文库 / 下载 / 传输 / 宿主来源标识后台 API 与页面。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.admin import api as api_mod


def _now(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)).isoformat()


def _seed_search(storage) -> None:
    rows = [
        ("req-s1", "search_papers", "arxiv", "SUCCESS", 120, 8, 0, 0, ""),
        ("req-s1", "search_papers", "crossref", "FAILED", 900, 0, 0, 1, "429 rate limited"),
        ("req-s2", "search_web", "hn", "SUCCESS", 80, 12, 1, 1, ""),
        ("req-s2", "search_web", "stackexchange", "SOURCE_UNAVAILABLE", 30, 0, 0, 0,
         "上游不可达"),
    ]
    for i, (rid, tool, provider, status, ms, count, cached, limited, err) in enumerate(rows):
        storage._execute(
            "INSERT INTO search_events (ts,request_id,tool_name,provider,status,"
            "duration_ms,result_count,cached,rate_limited,keyword,query_hash,error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [_now(60 - i), rid, tool, provider, status, ms, count,
             cached, limited, "query ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
             hashlib.sha256(b"q").hexdigest()[:16], err])


def _seed_papers(storage, file_path: str = "") -> None:
    storage._execute(
        "INSERT INTO paper_library (ts,paper_id,title,authors,publication_year,venue,"
        "doi,arxiv_id,oa_status,landing_url,file_path,sha256,file_size,sources,"
        "project,tags,reading_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [_now(30), "p-1", "Reproducible Research",
         '["Ada Lovelace"]', 2024, "Offline Journal", "10.5555/v3", "2401.00001",
         "oa_pdf", "https://example.org/paper", file_path,
         "a" * 64 if file_path else "", 1024 if file_path else 0,
         '["arxiv"]', "unit", '["ml"]', "unread"])
    for status in ("created", "downloading", "success", "failed"):
        storage._execute(
            "INSERT INTO paper_downloads (ts,request_id,paper_id,title,source,status,"
            "bytes,total_bytes,error_message) VALUES (?,?,?,?,?,?,?,?,?)",
            [_now(20), "req-dl", "p-1", "Reproducible Research",
             "arxiv", status, 512, 1024,
             "HTML 假 PDF" if status == "failed" else ""])
    storage._execute(
        "INSERT INTO file_transfers (ts,request_id,direction,path,status,bytes,"
        "total_bytes,sha256,detail) VALUES (?,?,?,?,?,?,?,?,?)",
        [_now(10), "req-tr", "upload", "/workspace/transfers/a.bin",
         "success", 1024, 1024, "b" * 64, "原子改名"])


def _seed_calls(storage) -> None:
    for i, source in enumerate(("mcp", "rest", "admin", "http", "host-agent")):
        storage._execute(
            "INSERT INTO tool_calls (request_id,trace_id,timestamp_start,tool_name,"
            "status,source,client_id,arguments,result) VALUES (?,?,?,?,?,?,?,?,?)",
            [f"req-v3-{i}", "trc_v3", _now(5 - i), "run_command",
             "SUCCESS", source, "tok_test", "{}", "ok"])


def test_new_views_are_wired_into_navigation_and_frontend():
    html = (Path(api_mod.__file__).parent / "templates" / "index.html").read_text(
        encoding="utf-8")
    js = (Path(api_mod.__file__).parent / "static" / "app.js").read_text(
        encoding="utf-8")
    css = (Path(api_mod.__file__).parent / "static" / "app.css").read_text(
        encoding="utf-8")
    for view, label in (("search", "搜索监控"), ("papers", "论文库与下载")):
        assert f'data-view="{view}"' in html and label in html
        assert f"{view}: {{ title:" in js
    assert "source-chip" in css and "paper-card" in css and "progress" in css
    assert "scopeBar(" in js and "renderSearch" in js and "renderPapers" in js
    assert "/api/search/events" in js and "/api/papers/downloads" in js
    assert "/api/transfers?limit" in js and "data-transfer-link" in js
    assert "/api/v3/overview" in js and "/api/host/status" in js
    assert "f-source" in js and "结果摘要" in js
    assert "prefers-reduced-motion" in css and "1280px" in css


def test_search_events_matrix_and_honest_upstream_status(admin_client, login):
    client, storage, _ = admin_client
    _seed_search(storage)
    login(client)
    data = client.get("/api/search/events?range=30d").json()
    assert data["total"] == 4
    providers = {r["provider"]: r for r in data["by_provider"]}
    assert providers["arxiv"]["requests"] == 1
    assert providers["arxiv"]["successes"] == 1
    assert providers["crossref"]["failures"] == 1
    assert providers["crossref"]["rate_limited"] == 1
    assert providers["hn"]["cached"] == 1
    assert "不等于全网" in data["coverage_note"]
    assert data["upstream"]["status"] == "未检测"
    assert "外部" in data["source_note"]
    # 脱敏：关键词中的类令牌串不得原样出现在响应里
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in str(data)
    filtered = client.get("/api/search/events?provider=hn&range=30d").json()
    assert filtered["total"] == 1 and filtered["rows"][0]["provider"] == "hn"


def test_papers_library_downloads_and_status_distinction(admin_client, login):
    client, storage, _ = admin_client
    _seed_papers(storage)
    login(client)
    lib = client.get("/api/papers").json()
    assert lib["total"] == 1 and lib["stats"]["total"] == 1
    assert lib["stats"]["verified"] == 0, "无已校验文件时应如实为 0"
    assert "不等于" in lib["download_note"] and "OCR" in lib["ocr_note"]
    assert client.get("/api/papers?identifier=2401.00001").json()["total"] == 1
    assert client.get("/api/papers?oa_status=subscription_required").json()["total"] == 0
    assert client.get("/api/papers?from_year=2025").json()["total"] == 0

    dl = client.get("/api/papers/downloads").json()
    assert dl["counts"]["created"] == 1 and dl["counts"]["success"] == 1
    assert "created 仅表示任务创建" in dl["note"]
    failed = [r for r in dl["rows"] if r["status"] == "failed"][0]
    assert "HTML 假 PDF" in failed["error_message"]


def test_bibliography_exports_and_invalid_format(admin_client, login):
    client, storage, _ = admin_client
    _seed_papers(storage)
    login(client)
    bib = client.get("/api/papers/export?fmt=bibtex")
    assert bib.status_code == 200 and "@article" in bib.text
    assert "Reproducible Research" in bib.text
    assert "TY  - JOUR" in client.get("/api/papers/export?fmt=ris").text
    assert "paper_id,title" in client.get("/api/papers/export?fmt=csv").text
    assert client.get("/api/papers/export?fmt=json").json()["count"] == 1
    assert client.get("/api/papers/export?fmt=exe").status_code == 400


def test_signed_paper_download_range_and_permission(admin_client, login,
                                                    tmp_path, monkeypatch):
    client, storage, _ = admin_client
    paper_dir = tmp_path / "papers"
    paper_dir.mkdir()
    payload = b"%PDF-1.4\nphase-e-range-test\n%%EOF\n"
    target = paper_dir / "p-1.pdf"
    target.write_bytes(payload)
    monkeypatch.setattr(api_mod, "PAPER_DIR", paper_dir.resolve())
    _seed_papers(storage, str(target))
    login(client)

    link = client.get("/api/papers/link?paper_id=p-1").json()
    assert link["ok"] is True and "sig=" in link["url"]
    ranged = client.get(link["url"], headers={"Range": "bytes=0-8"})
    assert ranged.status_code == 206
    assert ranged.content == payload[:9]
    assert ranged.headers["Content-Range"] == f"bytes 0-8/{len(payload)}"
    assert client.get(link["url"]).content == payload
    assert client.get("/api/papers/link?paper_id=missing").json()["ok"] is False

    from fastapi.testclient import TestClient
    with TestClient(api_mod.app) as anonymous:
        assert anonymous.get(link["url"]).status_code == 200, "纯签名链接应可用"
        assert anonymous.get(
            "/api/papers/file?paper_id=p-1&exp=1&sig=deadbeef").status_code == 401
        assert anonymous.get("/api/papers").status_code == 401


def test_transfers_host_status_and_v3_overview(admin_client, login):
    client, storage, _ = admin_client
    _seed_papers(storage)
    _seed_calls(storage)
    login(client)
    transfers = client.get("/api/transfers").json()
    assert transfers["counts"]["success"] == 1
    assert "created 仅表示任务创建" in transfers["note"]
    host = client.get("/api/host/status").json()
    assert host["default"] == "container"
    assert host["host-agent"]["no_tcp"] is True
    assert host["host-agent"]["available"] is False, "未授权时不得声称可用"
    assert "container" in host["source_labels"] and "host-agent" in host["source_labels"]
    overview = client.get("/api/v3/overview?range=30d").json()
    assert overview["papers"] == 1
    assert overview["downloads"]["success"] == 1
    assert overview["transfers"]["success"] == 1
    assert "host-agent" in overview["tool_sources"], "来源标识应覆盖宿主"
    assert "外部" in overview["source_note"]


def test_transfer_signed_download_and_range(admin_client, login, tmp_path,
                                            monkeypatch):
    client, _, _ = admin_client
    transfer_dir = tmp_path / "transfers"
    transfer_dir.mkdir()
    payload = b"phase-e-transfer-range-payload"
    (transfer_dir / "blob.bin").write_bytes(payload)
    monkeypatch.setattr(api_mod, "TRANSFER_DIR", transfer_dir.resolve())
    monkeypatch.setenv("MCP_TRANSFER_SECRET", "phase-e-secret")
    login(client)

    denied = client.get("/api/transfers/link?path=blob.bin",
                        headers={}).json()
    assert denied["ok"] is True, denied
    url = denied["url"]
    ranged = client.get(url, headers={"Range": "bytes=5-14"})
    assert ranged.status_code == 206 and ranged.content == payload[5:15]
    assert ranged.headers["Content-Range"] == f"bytes 5-14/{len(payload)}"
    assert client.get(url).content == payload
    assert client.get("/api/transfers/link?path=../outside").json()["ok"] is False

    from fastapi.testclient import TestClient
    with TestClient(api_mod.app) as anonymous:
        assert anonymous.get(url).status_code == 200, "纯签名链接应可用"
        assert anonymous.get("/api/transfers/file?path=blob.bin&exp=1&sig=x").status_code == 401
        assert anonymous.get("/api/transfers").status_code == 401

    monkeypatch.delenv("MCP_TRANSFER_SECRET")
    assert client.get("/api/transfers/link?path=blob.bin").json()["ok"] is False


def test_call_source_filter_and_result_summary(admin_client, login):
    client, storage, _ = admin_client
    _seed_calls(storage)
    login(client)
    assert client.get("/api/calls?source=http&range=30d").json()["total"] == 1
    assert client.get("/api/calls?source=host-agent&range=30d").json()["total"] == 1
    all_rows = client.get("/api/calls?range=30d").json()
    assert all_rows["total"] == 5
    assert "result" in all_rows["rows"][0]


@pytest.mark.parametrize("path", [
    "/api/search/events", "/api/papers", "/api/papers/downloads",
    "/api/papers/export", "/api/papers/link", "/api/papers/file",
    "/api/transfers", "/api/transfers/link", "/api/transfers/file",
    "/api/host/status", "/api/v3/overview",
])
def test_new_endpoints_require_admin_session(admin_client, path):
    client, _, _ = admin_client
    assert client.get(path).status_code == 401, path
