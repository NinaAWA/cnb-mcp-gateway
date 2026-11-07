"""阶段 B 学术检索：年份语义、跨源合并去重、全局排序、JSON 与故障隔离。"""
from __future__ import annotations

import json
import urllib.parse
import xml.etree.ElementTree as ET

import pytest


def _install_fake_http(monkeypatch, server, payloads):
    """零网络：按 URL 分发模拟响应，同时记录所有请求 URL 便于断言语义。"""
    seen: list[str] = []

    def fake_json(url, **_kw):
        seen.append(url)
        for key, value in payloads.items():
            if key and key in url:
                return value
        return {}

    def fake_xml(url, **_kw):
        seen.append(url)
        return ET.fromstring(payloads.get("__atom__", "<feed/>"))

    monkeypatch.setattr(server, "_json_get", fake_json)
    monkeypatch.setattr(server, "_xml_get", fake_xml)
    return seen


def test_from_year_and_to_year_are_consistent_across_sources(gateway_module,
                                                             monkeypatch):
    server = gateway_module
    seen = _install_fake_http(monkeypatch, server, {
        "api.openalex.org": {"results": []},
        "api.crossref.org": {"message": {"items": []}},
        "europepmc/webservices": {"resultList": {"result": []}},
        "esearch.fcgi": {"esearchresult": {"idlist": []}},
        "api.openreview.net": {"notes": []},
        "dblp.org": {"result": {"hits": {"hit": []}}},
        "api.semanticscholar.org": {"data": []},
    })
    server.search_papers(
        "graph neural network",
        sources="openalex,crossref,europepmc,pubmed,openreview,dblp,semanticscholar",
        max_results=3, from_year=2020, to_year=2022)
    decoded = urllib.parse.unquote_plus("\n".join(seen))
    assert "from_publication_date:2020-01-01" in decoded, "OpenAlex 下限"
    assert "to_publication_date:2022-12-31" in decoded, "OpenAlex 上限"
    assert "from-pub-date:2020-01-01" in decoded, "CrossRef 下限"
    assert "until-pub-date:2022-12-31" in decoded, "CrossRef 上限"
    assert "FIRST_PDATE:[2020-01-01 TO 2022-12-31]" in decoded, "EuropePMC 区间"
    assert "2020:2022[pdat]" in decoded, "PubMed 年份区间"
    assert "year=2020-2022" in decoded, "SemanticScholar 应使用年份区间"


def test_client_side_year_filter_for_sources_without_range_query(gateway_module,
                                                                 monkeypatch):
    server = gateway_module
    hits = [{"info": {"title": f"Paper {y}", "year": str(y),
                      "authors": {"author": []}, "url": f"https://dblp.org/{y}"}}
            for y in (2019, 2021, 2023)]
    _install_fake_http(monkeypatch, server, {
        "dblp.org": {"result": {"hits": {"hit": hits}}},
        "api.openreview.net": {"notes": [
            {"content": {"title": "Paper 2019"}, "cdate": 1546300800000},
            {"content": {"title": "Paper 2021"}, "cdate": 1609459200000},
            {"content": {"title": "Paper 2023"}, "cdate": 1672531200000},
        ]},
        "api.semanticscholar.org": {"data": [
            {"title": "Paper 2019", "year": 2019, "externalIds": {}},
            {"title": "Paper 2021", "year": 2021, "externalIds": {}},
            {"title": "Paper 2023", "year": 2023, "externalIds": {}},
        ]},
    })
    out = server.search_papers(
        "Paper", sources="dblp,openreview,semanticscholar", max_results=5,
        from_year=2020, to_year=2022, format="json")
    payload = json.loads(out)
    years = {row["publication_year"] for row in payload["results"]}
    assert years <= {2021}, f"客户端过滤应剔除 2019/2023，实际 {years}"
    assert payload["result_count"] >= 1


def test_merge_dedupe_before_global_sort_and_truncate(gateway_module,
                                                      monkeypatch):
    server = gateway_module
    shared = {"title": "Attention Is All You Need", "doi": "10.48550/arXiv.1706.03762",
              "date": "2017-06-12", "url": "https://arxiv.org/abs/1706.03762",
              "snippet": "transformer"}

    def src_a(_query, _limit, _year, year_to=0):
        return [{**shared, "source": "OpenAlex", "citations": 100}]

    def src_b(_query, _limit, _year, year_to=0):
        return [{**shared, "source": "arXiv", "citations": None},
                {"title": "Zero citation paper", "source": "arXiv",
                 "date": "2024-01-01", "url": "https://arxiv.org/abs/2401.00001",
                 "snippet": "", "citations": 0}]

    monkeypatch.setattr(server, "_PAPER_SOURCES", {
        "openalex": ("OpenAlex", src_a), "arxiv": ("arXiv", src_b)})
    payload = json.loads(server.search_papers(
        "attention", sources="openalex,arxiv", max_results=2, format="json"))
    assert payload["result_count"] == 2
    merged = next(r for r in payload["results"]
                  if r["title"] == "Attention Is All You Need")
    assert set(merged["sources"]) == {"OpenAlex", "arXiv"}
    assert merged["citation_count"] == 100, "合并保留已知最大值，不把缺失写成 0"
    zero = next(r for r in payload["results"] if r["title"] == "Zero citation paper")
    assert zero["citation_count"] == 0, "真实零引用必须与缺失(null)区分"


def test_missing_citations_stay_null_in_json(gateway_module, monkeypatch):
    server = gateway_module

    def only(_query, _limit, _year, year_to=0):
        return [{"title": "No citation metric", "source": "arXiv",
                 "date": "2022", "url": "https://arxiv.org/abs/2201.00001",
                 "snippet": ""}]

    monkeypatch.setattr(server, "_PAPER_SOURCES", {"arxiv": ("arXiv", only)})
    payload = json.loads(server.search_papers(
        "metric", sources="arxiv", max_results=1, format="json"))
    assert payload["results"][0]["citation_count"] is None
    assert "null" in payload["citations_semantics"]


def test_source_failure_is_isolated_and_reported(gateway_module, monkeypatch):
    server = gateway_module

    def broken(_query, _limit, _year, year_to=0):
        raise server.SearchError("HTTP 429（限流）")

    def healthy(_query, _limit, _year, year_to=0):
        return [{"title": "Still here", "source": "OpenAlex", "date": "2023",
                 "url": "https://example.org/p", "snippet": ""}]

    monkeypatch.setattr(server, "_PAPER_SOURCES", {
        "arxiv": ("arXiv", broken), "openalex": ("OpenAlex", healthy)})
    text = server.search_papers("test", sources="arxiv,openalex", max_results=2)
    assert "Still here" in text
    assert "429" in text and "部分来源失败" in text
    payload = json.loads(server.search_papers(
        "test", sources="arxiv,openalex", max_results=2, format="json"))
    assert payload["source_failures"] and "429" in payload["source_failures"][0]


def test_validation_and_get_paper_json(gateway_module, monkeypatch):
    server = gateway_module
    assert "to_year 不能小于 from_year" in server.search_papers(
        "x", from_year=2022, to_year=2020)
    assert "未知来源" in server.search_papers("x", sources="not-a-source")
    assert "format" in server.search_papers("x", format="xml")

    monkeypatch.setattr(server, "_paper_by_doi", lambda _doi: "\n".join([
        "标识符: 10.1000/abc", "标题: A Structured Paper",
        "作者: Ada, Bob", "发表: 2021", "引用数: 7",
        "摘要: structured metadata", "链接: https://doi.org/10.1000/abc"]))
    payload = json.loads(server.get_paper(doi="10.1000/abc", format="json"))
    row = payload["results"][0]
    assert row["title"] == "A Structured Paper"
    assert row["citation_count"] == 7
    assert row["source"] == "CrossRef"
    assert payload["result_count"] == 1
    assert server.get_paper().startswith("ERROR:")


def test_search_arxiv_to_year_and_json(gateway_module, monkeypatch):
    server = gateway_module
    atom = """<feed xmlns='http://www.w3.org/2005/Atom'>
      <entry><title>Old</title><published>2019-01-01T00:00:00Z</published>
        <id>http://arxiv.org/abs/1901.00001</id><summary>old</summary></entry>
      <entry><title>New</title><published>2021-01-01T00:00:00Z</published>
        <id>http://arxiv.org/abs/2101.00001</id><summary>new</summary></entry>
    </feed>"""
    _install_fake_http(monkeypatch, server, {"__atom__": atom})
    payload = json.loads(server.search_arxiv(
        "test", from_year=2020, to_year=2022, max_results=5, format="json"))
    assert [row["title"] for row in payload["results"]] == ["New"]
    assert payload["scope_note"]
