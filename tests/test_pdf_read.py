"""阶段 B PDF 阅读：按页文本与页码、章节/关键词、分段缓存、扫描版 OCR 提示。"""
from __future__ import annotations

import json

from mock_server import make_test_pdf
from gateway.research.pdf import read_pdf, search_pdf_fulltext


def _pdf(tmp_path, name="paper.pdf", **kw):
    path = tmp_path / name
    path.write_bytes(make_test_pdf([
        "Page one headline\nAbstract\nWe study transformer models.\nMethods\nData",
        "Page two\nResults\nAccuracy improved.\nReferences\n[1] Vaswani et al.",
    ], **kw))
    return path


def test_page_text_page_numbers_and_keyword_matches(tmp_path):
    path = _pdf(tmp_path)
    result = read_pdf(path, keyword="transformer", list_sections=True)
    assert result["status"] == "success"
    assert result["page_count"] == 2
    assert "第 1/2 页" in result["pages"][0]
    assert "transformer" in result["pages"][0]
    assert result["matches"] and result["matches"][0]["page"] == 1
    headings = {s["heading"] for s in result["sections"]}
    assert "Abstract" in headings and "References" in headings
    assert any(s["page"] == 2 for s in result["sections"])


def test_single_page_and_reference_search(tmp_path):
    path = _pdf(tmp_path, "refs.pdf")
    only = read_pdf(path, page=2, max_chars=2000)
    assert "第 2/2 页" in only["pages"][0]
    assert "第 1/2 页" not in "".join(only["pages"])
    found = search_pdf_fulltext(path, "Vaswani")
    assert found["matches"] and found["matches"][0]["page"] == 2
    assert read_pdf(path, page=99)["status"] == "failed"


def test_cache_and_scanned_pdf_hint(tmp_path):
    path = _pdf(tmp_path, "cached.pdf")
    first = read_pdf(path)
    second = read_pdf(path)
    assert second["cached"] is True
    assert second["characters_extracted"] == first["characters_extracted"]

    scanned = tmp_path / "scanned.pdf"
    scanned.write_bytes(make_test_pdf(["", ""], empty_text=True))
    result = read_pdf(scanned)
    assert result["status"] == "scanned_no_text"
    assert "OCR" in result["reason"]
    assert not any(result["pages"]), "不得伪造扫描版正文"


def test_invalid_pdf_is_reported_not_parsed(tmp_path):
    path = tmp_path / "notpdf.pdf"
    path.write_bytes(b"<html>not a pdf</html>")
    result = read_pdf(path)
    assert result["status"] == "failed" and "%PDF-" in result["reason"]
    assert json.dumps(read_pdf(path))
