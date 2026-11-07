"""阶段 B 论文下载：合法 OA、严格校验、HTML 假 PDF、断点续传、状态持久化。"""
from __future__ import annotations

import hashlib
import json

from mock_server import MockServer, local_client, make_test_pdf
from gateway.research.download import download_paper, safe_filename
from gateway.research.fulltext import discover_oa
from gateway.research.library import download_status, list_papers
from gateway.transfer.core import PaperSourcePolicy, verify_pdf_file

LOCAL = frozenset({"127.0.0.1"})


def _paper(url: str) -> dict:
    return {"doi": "10.5555/local-test", "title": "Local OA Paper",
            "pdf_url": url, "authors": ["Ada Lovelace"],
            "publication_year": 2024, "venue": "Offline Journal",
            "sources": ["mock"]}


def test_valid_oa_pdf_download_is_verified_and_recorded(isolated_storage,
                                                         tmp_path):
    body = make_test_pdf(["Abstract. Local OA paper.", "References. Cited works."])
    with MockServer() as srv:
        srv.set("/paper.pdf", 200, body, {"Content-Type": "application/pdf"})
        client = local_client()
        result = download_paper(
            _paper(f"{srv.url}/paper.pdf"), client=client,
            paper_dir=tmp_path / "papers", request_id="req-success",
            project="unit", extra_hosts=LOCAL, allow_private=True)
    assert result["status"] == "success", result
    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    assert verify_pdf_file(result["path"])[0] is True
    rows = isolated_storage._query(
        "SELECT status, error_type FROM paper_downloads WHERE request_id=? "
        "ORDER BY id ASC", ["req-success"])
    statuses = [r["status"] for r in rows]
    assert "created" in statuses and "success" in statuses
    assert statuses.index("created") < statuses.index("success")
    assert download_status("req-success")["verified_success"] == 1
    library = list_papers({"keyword": "Local OA"})
    assert len(library) == 1 and library[0]["sha256_verified"] is True


def test_html_fake_pdf_and_truncated_pdf_are_rejected(isolated_storage,
                                                       tmp_path):
    with MockServer() as srv:
        srv.set("/fake.pdf", 200, "<html><h1>Login required</h1></html>",
                {"Content-Type": "text/html; charset=utf-8"})
        srv.set("/truncated.pdf", 200,
                make_test_pdf(["Only page"], missing_eof=True),
                {"Content-Type": "application/pdf"})
        client = local_client()
        for path, marker in (("/fake.pdf", "HTML"),
                             ("/truncated.pdf", "%%EOF")):
            result = download_paper(
                _paper(f"{srv.url}{path}"), client=client,
                paper_dir=tmp_path / "papers", request_id=f"req-{path[1:]}",
                extra_hosts=LOCAL, allow_private=True)
            assert result["status"] == "failed", result
            assert marker in result["reason"] or "缺少" in result["reason"]
            assert not result["path"] or not __import__("os").path.exists(result["path"])
    assert download_status()["verified_success"] == 0


def test_forbidden_host_and_missing_mailto_are_explicit(gateway_module):
    from gateway.research.fulltext import STATUS_SOURCE_UNAVAILABLE

    blocked = discover_oa({"doi": "10.1/x",
                           "pdf_url": "https://sci-hub.example/pdf"},
                          client=None, mailto="")
    assert "盗版" in " ".join(blocked["reasons"]) or blocked["status"]
    assert not blocked["candidates"]

    no_mail = discover_oa({"doi": "10.1/x"}, client=None, mailto="")
    assert no_mail["status"] == STATUS_SOURCE_UNAVAILABLE
    assert any("MCP_SEARCH_MAILTO" in r for r in no_mail["reasons"])
    assert not no_mail["candidates"], "不得虚构 PDF 链接"

    assert gateway_module.find_paper_fulltext(
        identifier="sci-hub.example/x").startswith("状态:")


def test_resume_from_partial_file(tmp_path):
    body = make_test_pdf(["Resume part one.", "Resume part two."])
    cut = len(body) // 2
    ranged = {"used": False}

    def handler(req):
        raw = req.headers.get("Range", "")
        if raw.startswith("bytes="):
            start = int(raw[6:].split("-", 1)[0])
            ranged["used"] = True
            return (206, body[start:],
                    {"Content-Type": "application/pdf",
                     "Content-Range": f"bytes {start}-{len(body)-1}/{len(body)}"})
        return (200, body, {"Content-Type": "application/pdf"})

    with MockServer() as srv:
        srv.set_handler("/resume.pdf", handler)
        dest = tmp_path / "papers" / safe_filename("10.5555/local-test")
        dest.parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "papers" / (dest.name + ".part")).write_bytes(body[:cut])
        result = download_paper(
            _paper(f"{srv.url}/resume.pdf"), client=local_client(),
            paper_dir=tmp_path / "papers", request_id="req-resume",
            extra_hosts=LOCAL, allow_private=True)
    assert ranged["used"] is True
    assert result["resumed"] is True
    assert result["status"] == "success", result
    assert result["sha256"] == hashlib.sha256(body).hexdigest()


def test_duplicate_hash_is_not_reported_as_new_success(tmp_path):
    body = make_test_pdf(["Duplicate paper."])
    with MockServer() as srv:
        srv.set("/dup.pdf", 200, body, {"Content-Type": "application/pdf"})
        client = local_client()
        first = download_paper(
            _paper(f"{srv.url}/dup.pdf"), client=client,
            paper_dir=tmp_path / "papers", request_id="r1",
            extra_hosts=LOCAL, allow_private=True)
        second = download_paper(
            _paper(f"{srv.url}/dup.pdf"), client=client,
            paper_dir=tmp_path / "papers", request_id="r2",
            extra_hosts=LOCAL, allow_private=True)
    assert first["status"] == "success"
    assert second["status"] == "duplicate", "同哈希不应重复落盘却不说明"
    assert second["sha256"] == first["sha256"]


def test_paper_source_policy_rejects_non_oa_hosts():
    policy = PaperSourcePolicy()
    for url in ("https://sci-hub.se/pdf/x", "https://example.com/x.pdf",
                "http://arxiv.org/pdf/2301.00001"):
        try:
            policy.validate(url)
        except Exception:
            continue
        raise AssertionError(f"应拒绝 {url}")
    assert policy.validate("https://arxiv.org/pdf/2301.00001") == "arxiv.org"


def test_download_tool_reports_created_vs_success(gateway_module, monkeypatch,
                                                  isolated_storage):
    server = gateway_module
    monkeypatch.setattr(server, "_PAPER_DIR", __import__("pathlib").Path(
        "/tmp/mcp-paper-tool-test"))
    called = {}

    def fake_download(paper, **kw):
        called["request_id"] = kw.get("request_id")
        return {"status": "failed", "path": "", "bytes": 0, "sha256": "",
                "final_url": "", "reason": "NO_OA: 未发现合法开放获取位置",
                "discovery": {"reasons": ["NO_OA"]}}

    monkeypatch.setattr(server, "_download_one", fake_download)
    out = server.download_paper(identifier="10.1/no-oa")
    assert "状态: failed" in out and "NO_OA" in out
    assert "created 仅表示任务创建" in out
    assert called["request_id"] == "" or isinstance(called["request_id"], str)
