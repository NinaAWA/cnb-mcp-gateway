"""阶段 B 检索：安全 HTTP 客户端、网页增强工具与结构化搜索事件（离线）。"""
from __future__ import annotations

import json

import pytest

from gateway.net.client import SafeHttpError
from mock_server import MockServer, local_client


# ------------------------------------------------------------- SafeHttpClient
def test_client_rejects_non_text_content_type():
    with MockServer() as srv:
        srv.set("/fake", 200, "<html>not json</html>",
                {"Content-Type": "application/pdf"})
        client = local_client()
        with pytest.raises(SafeHttpError, match="非文本"):
            client.json_get(f"{srv.url}/fake", allow_any_host=True,
                            allow_private=True)
        # 显式声明二进制时放行：PDF 下载走这条路径
        raw = client.request(f"{srv.url}/fake", allow_any_host=True,
                             allow_private=True, expect_text=False)
        assert raw.startswith(b"<html>")


def test_client_honors_retry_after_and_cache():
    calls = {"n": 0}

    def throttled(_handler):
        calls["n"] += 1
        if calls["n"] == 1:
            return (429, b"slow down", {"Content-Type": "text/plain",
                                        "Retry-After": "0"})
        return (200, b'{"ok":true}', {"Content-Type": "application/json"})

    with MockServer() as srv:
        srv.set_handler("/api", throttled)
        client = local_client(cache_ttl=30.0)
        first = client.json_get(f"{srv.url}/api", allow_any_host=True,
                                allow_private=True)
        assert first == {"ok": True} and calls["n"] == 2
        second = client.json_get(f"{srv.url}/api", allow_any_host=True,
                                 allow_private=True)
        assert second == {"ok": True} and calls["n"] == 2, "第二次应命中缓存"
        assert client.last_cache_hit is True


def test_client_revalidates_redirect_and_rejects_metadata_ip():
    with MockServer() as srv:
        srv.set("/go", 302, b"", {"Location": "http://169.254.169.254/latest/"})
        client = local_client()
        with pytest.raises(SafeHttpError):
            client.request(f"{srv.url}/go", allow_any_host=True,
                           allow_private=True)
        # 到自建服务的重定向允许（主机在显式私网白名单内）
        srv.set("/ok", 200, b"ok", {"Content-Type": "text/plain"})
        srv.set("/go", 302, b"", {"Location": f"{srv.url}/ok"})
        assert client.request(f"{srv.url}/go", allow_any_host=True,
                              allow_private=True) == b"ok"


def test_client_rejects_ip_literal_and_private_dns():
    from gateway.net.client import resolve_public_ips

    client = local_client()
    with pytest.raises(SafeHttpError):
        client.validate_url("http://192.168.1.1/", allow_any_host=True)
    with pytest.raises(SafeHttpError):
        resolve_public_ips("127.0.0.1")


def test_dns_pinning_keeps_original_tls_hostname():
    import ssl
    from gateway.net.client import _PinnedHandler, _PinnedHTTPSConnection

    handler = _PinnedHandler("api.example.org", "93.184.216.34",
                             ssl.create_default_context())
    conn = handler._factory("api.example.org", timeout=3,
                            context=ssl.create_default_context())
    assert isinstance(conn, _PinnedHTTPSConnection)
    assert conn.host == "api.example.org", "TLS SNI/证书校验仍按原始主机名"
    assert conn._pinned_ip == "93.184.216.34"
    # 跳转到其它主机时不得沿用旧 IP
    other = handler._factory("other.example.org", timeout=3,
                             context=ssl.create_default_context())
    assert not isinstance(other, _PinnedHTTPSConnection)


# --------------------------------------------------------------- 网页增强工具
def _fake_provider(items):
    def provider(_query, _limit, _site):
        return list(items)
    return provider


def test_advanced_search_dedupes_filters_and_global_sorts(gateway_module,
                                                          monkeypatch):
    server = gateway_module
    shared = {"source": "Hacker News", "title": "Shared result",
              "snippet": "", "url": "https://news.ycombinator.com/item?id=1",
              "date": "2024-01-02", "extra": "", "citations": 5}
    monkeypatch.setattr(server, "_WEB_PROVIDERS", {
        "hn": ("Hacker News", _fake_provider([shared])),
        "so": ("StackExchange", _fake_provider([
            {**shared, "source": "StackExchange"},
            {"source": "StackExchange", "title": "Older item", "snippet": "",
             "url": "https://stackoverflow.com/q/1", "date": "2019-05-01"},
            {"source": "StackExchange", "title": "中文结果 2025", "snippet": "",
             "url": "https://example.com/zh", "date": "2025-03-01"},
        ])),
    })
    # 先单独验证跨源去重（不带筛选）
    payload = json.loads(server.search_web_advanced(
        "Shared result", providers="hn,so", max_results=5, format="json"))
    merged_titles = [row["title"] for row in payload["results"]]
    assert merged_titles.count("Shared result") == 1, "跨源同标题应合并去重"
    duplicate = next(r for r in payload["results"] if r["title"] == "Shared result")
    assert set(duplicate["sources"]) == {"Hacker News", "StackExchange"}

    # 再验证年份 / 语言筛选 + 日期排序
    filtered = json.loads(server.search_web_advanced(
        "Shared result", providers="hn,so", max_results=5, from_year=2021,
        language="zh", sort="date", format="json"))
    titles = [row["title"] for row in filtered["results"]]
    assert "Older item" not in titles, "from_year 应过滤更早年份"
    assert "Shared result" not in titles, "语言筛选应剔除纯英文结果"
    assert "中文结果 2025" in titles
    assert payload["scope_note"]
    assert payload["citations_semantics"]


def test_advanced_search_merge_before_sort_before_truncate(gateway_module,
                                                           monkeypatch):
    server = gateway_module

    def many(_query, _limit, _site):
        return [{"source": "Hacker News", "title": f"Result {i}",
                 "snippet": "", "url": f"https://news.ycombinator.com/{i}",
                 "date": f"2024-01-{i + 1:02d}"} for i in range(8)]

    monkeypatch.setattr(server, "_WEB_PROVIDERS", {
        "hn": ("Hacker News", many),
        "so": ("StackExchange", _fake_provider([
            {"source": "StackExchange", "title": "Result 0", "snippet": "",
             "url": "https://news.ycombinator.com/0", "date": "2024-01-01"},
        ])),
    })
    payload = json.loads(server.search_web_advanced(
        "Result", providers="hn,so", max_results=10, format="json"))
    assert payload["result_count"] == 8, "先合并去重再排序再截断，不应提前丢结果"
    assert sum(row["title"] == "Result 0" for row in payload["results"]) == 1


def test_search_website_reports_unavailable_without_general_provider(
        gateway_module, monkeypatch):
    server = gateway_module
    monkeypatch.setattr(server, "SEARCH_SEARXNG_URL", "")
    monkeypatch.setattr(server, "SEARCH_TAVILY_KEY", "")
    out = server.search_website("example.com", "release notes")
    assert "SOURCE_UNAVAILABLE" in out
    assert "不能替代" in out


def test_search_website_uses_only_general_provider(gateway_module, monkeypatch):
    server = gateway_module
    seen = {}

    def fake_collect(names, _query, _limit, _site):
        seen["names"] = list(names)
        return ([{"source": "SearXNG", "title": "Hit", "snippet": "",
                  "url": "https://example.com/a", "date": "2025-01-01"}],
                ["SearXNG"], [], {}, 0.01)

    monkeypatch.setattr(server, "SEARCH_SEARXNG_URL", "http://searxng:8080")
    monkeypatch.setattr(server, "SEARCH_TAVILY_KEY", "")
    monkeypatch.setattr(server, "_collect_web_results", fake_collect)
    out = server.search_website("example.com", "release notes")
    assert seen["names"] == ["searxng"], "不得把 HN/StackExchange 混入站点搜索"
    assert "site:example.com" not in out or "Hit" in out


def test_fetch_and_extract_content_are_chunked_and_labeled(gateway_module,
                                                            monkeypatch):
    server = gateway_module
    html = (
        "<html><head><title>研究 &amp; 页面</title>"
        "<meta name='author' content='Ada Lovelace'>"
        "<meta property='article:published_time' content='2025-02-03'>"
        "</head><body><nav>skip me</nav><article><p>第一段正文。</p>"
        "<p>第二段正文含关键词 transformer。</p></article>"
        "<script>alert('secret')</script></body></html>")
    monkeypatch.setattr(server, "SEARCH_FETCH_ENABLED", True)
    monkeypatch.setattr(server, "_http_request", lambda *a, **kw: html.encode())
    fetched = server.fetch_web_page("https://example.org/a", max_chars=1000)
    assert "抓取时间(UTC)" in fetched and "不可信" in fetched
    assert "第一段正文" in fetched and "skip me" not in fetched

    extracted = json.loads(server.extract_web_content(
        "https://example.org/a", max_chars=1000, chunk_size=12, format="json"))
    assert extracted["title"] == "研究 & 页面"
    assert extracted["author"] == "Ada Lovelace"
    assert extracted["published"] == "2025-02-03"
    assert len(extracted["chunks"]) >= 2
    assert "alert" not in extracted["text"]


# ------------------------------------------------------------ 结构化搜索审计
def test_search_writes_structured_events(isolated_storage, gateway_module,
                                         monkeypatch):
    from gateway.audit import middleware as mw

    server = gateway_module
    monkeypatch.setattr(mw, "get_storage", lambda: isolated_storage)
    monkeypatch.setattr(server, "_WEB_PROVIDERS", {
        "hn": ("Hacker News", _fake_provider([
            {"source": "Hacker News", "title": "Audit me", "snippet": "",
             "url": "https://news.ycombinator.com/1", "date": "2025-01-01"}]))})
    assert "Audit me" in server.search_web(
        "Audit me", providers="hn", max_results=2)
    rows = isolated_storage._query("SELECT * FROM search_events")
    providers = {row["provider"] for row in rows}
    assert "" in providers, "应有一条聚合事件"
    assert "Hacker News" in providers, "应有逐源耗时/条数事件"
    source_row = next(r for r in rows if r["provider"] == "Hacker News")
    assert source_row["result_count"] == 1
    assert source_row["status"] == "SUCCESS"
    assert source_row["keyword"] == "Audit me" or source_row["query_hash"]
