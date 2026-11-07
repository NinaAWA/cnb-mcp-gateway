"""阶段 B 文献库：导出格式、筛选、下载状态区分、批量部分成功。"""
from __future__ import annotations

import json

from gateway.research import batch as batch_mod
from gateway.research.library import (download_status, export_bibliography,
                                      list_papers)


def _seed(store):
    rows = [
        ("10.1000/one", "First Paper", '["Ada Lovelace"]', 2021,
         "Journal A", "alpha", "hash-one", "/papers/one.pdf", "proj-x", '["ml"]'),
        ("10.1000/two", "Second Paper", '["Bob"]', 2024,
         "Journal B", "", "hash-two", "/papers/two.pdf", "proj-y", '["nlp"]'),
    ]
    for doi, title, authors, year, venue, tags0, sha, path, project, tags in rows:
        store._execute(
            "INSERT INTO paper_library (ts,paper_id,title,authors,publication_year,"
            "venue,doi,arxiv_id,pmid,pmcid,citation_count,oa_status,landing_url,"
            "pdf_url,sources,file_path,sha256,file_size,project,tags,"
            "reading_status,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ["2025-01-01T00:00:00Z", doi, title, authors, year, venue, doi,
             "", "", "", None, "oa_pdf", f"https://doi.org/{doi}", "",
             '["OpenAlex"]', path, sha, 1234, project, tags, "unread", ""])
    return rows


def test_library_filters_and_all_export_formats(isolated_storage):
    _seed(isolated_storage)
    assert len(list_papers()) == 2
    assert len(list_papers({"keyword": "First"})) == 1
    assert len(list_papers({"project": "proj-y"})) == 1
    assert len(list_papers({"from_year": 2023})) == 1
    assert list_papers()[0]["authors"] == ["Ada Lovelace"] or any(
        p["authors"] == ["Ada Lovelace"] for p in list_papers())

    bib = export_bibliography("bibtex")
    assert bib.count("@article{") == 2 and "title = {First Paper}" in bib
    ris = export_bibliography("ris")
    assert ris.count("TY  - JOUR") == 2 and "AU  - Ada Lovelace" in ris
    csv_text = export_bibliography("csv")
    assert "paper_id,title,authors" in csv_text and "First Paper" in csv_text
    payload = json.loads(export_bibliography("json"))
    assert payload["count"] == 2
    try:
        export_bibliography("unknown")
    except ValueError:
        pass
    else:
        raise AssertionError("未知格式应报错")


def test_download_status_distinguishes_created_from_verified(isolated_storage):
    store = isolated_storage
    for status in ("created", "downloading", "verifying"):
        store._execute(
            "INSERT INTO paper_downloads (ts,request_id,paper_id,title,status,"
            "bytes,sha256,detail) VALUES (?,?,?,?,?,?,?,?)",
            ["2025-01-01T00:00:00Z", "req-a", "p1", "Paper", status, 0, "", ""])
    store._execute(
        "INSERT INTO paper_downloads (ts,request_id,paper_id,title,status,"
        "bytes,sha256,detail) VALUES (?,?,?,?,?,?,?,?)",
        ["2025-01-01T00:00:01Z", "req-a", "p1", "Paper", "success", 1234,
         "abc", "verified"])
    result = download_status("req-a")
    assert result["total"] == 4
    assert result["counts"]["created"] == 1
    assert result["verified_success"] == 1, "created 不得计入下载成功"
    assert "created 只表示任务已创建" in result["note"]


def test_upsert_library_records_project_and_preserves_user_metadata(isolated_storage):
    """回归：批量下载传入的 project 必须落库；重复下载不得清空用户元数据。"""
    from types import SimpleNamespace

    from gateway.research.download import _upsert_library

    store = isolated_storage
    paper = {"doi": "10.1000/v3", "title": "V3 Paper"}
    result = SimpleNamespace(path="/papers/v3.pdf", sha256="a" * 64, bytes=1000)

    _upsert_library(paper, result, "req-1", project="proj-v3")
    rows = list_papers({"project": "proj-v3"})
    assert len(rows) == 1 and rows[0]["paper_id"] == "10.1000/v3"

    store._execute(
        "UPDATE paper_library SET tags=?, reading_status=? WHERE paper_id=?",
        ['["important"]', "reading", "10.1000/v3"])
    # 再次下载（例如去重命中）且未指定 project：项目/标签/阅读状态都应保留。
    _upsert_library(paper, result, "req-2")
    row = list_papers()[0]
    assert row["project"] == "proj-v3"
    assert row["tags"] == ["important"]
    assert row["reading_status"] == "reading"
    assert export_bibliography("bibtex", {"project": "proj-v3"}).count(
        "@article{") == 1


def test_batch_is_bounded_deduplicated_and_reports_partial(monkeypatch):
    calls = {"n": 0}

    def fake_download(paper, **kw):
        calls["n"] += 1
        key = paper.get("doi") or paper.get("arxiv_id") or paper.get("title")
        if "bad" in str(key):
            return {"status": "failed", "reason": "NO_OA", "path": "",
                    "sha256": "", "bytes": 0}
        return {"status": "success", "path": f"/papers/{key}.pdf",
                "sha256": "hash", "bytes": 100, "reason": ""}

    monkeypatch.setattr(batch_mod, "download_paper", fake_download)
    result = batch_mod.batch_download(
        [{"doi": "10.1/good"}, {"doi": "10.1/good"},
         {"doi": "10.1/bad"}, {"arxiv_id": "2301.00001"}],
        client=None, request_id="req-batch", max_workers=2)
    assert result["status"] == "partial"
    assert result["total"] == 4 and result["unique"] == 3
    assert result["succeeded"] == 2 and result["failed"] == 1
    assert result["duplicates_skipped"] == ["10.1/good"]
    assert result["workers"] == 2
    assert calls["n"] == 3
    assert any(r["status"] == "failed" and r["reason"] == "NO_OA"
               for r in result["results"])
