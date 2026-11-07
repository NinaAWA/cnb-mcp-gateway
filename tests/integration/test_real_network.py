"""独立 opt-in 真实网络集成测试。

不模拟、不打桩：直接访问免费数据源，并如实记录每个来源的可达性、耗时与失败原因。
「可达」只代表本次运行所处的出口，不代表全部部署环境、服务条款合规或无限额度。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]

ACADEMIC_SOURCES = {
    "openalex": "https://api.openalex.org/works?search=transformer&per-page=1",
    "crossref": "https://api.crossref.org/works?query.title=attention&rows=1",
    "arxiv": "http://export.arxiv.org/api/query?search_query=all:electron&max_results=1",
    "pubmed": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=cancer&retmax=1",
    "europepmc": "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=cancer&format=json&pageSize=1",
}
WEB_SOURCES = {
    "stackexchange": "https://api.stackexchange.com/2.3/search/advanced?site=stackoverflow&q=python&pagesize=1",
    "github": "https://api.github.com/search/repositories?q=mcp&per_page=1",
    "hn": "https://hn.algolia.com/api/v1/search?query=python&hitsPerPage=1",
}
OA_PDF = "https://arxiv.org/pdf/1706.03762"


def _probe(url: str, timeout: float = 12) -> dict:
    started = time.monotonic()
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "cnb-mcp-gateway-v3-acceptance/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(256)
            return {"reachable": True, "http_status": response.status,
                    "content_type": response.headers.get("content-type", ""),
                    "seconds": round(time.monotonic() - started, 2)}
    except Exception as exc:  # noqa: BLE001 - 记录真实失败原因，不隐藏
        return {"reachable": False,
                "error": f"{type(exc).__name__}: {str(exc)[:140]}",
                "seconds": round(time.monotonic() - started, 2)}


def test_real_free_source_connectivity(record):
    """逐源只读探测；结果如实写入验收记录，不把失败掩盖成通过。"""
    outcomes = {}
    for name, url in {**ACADEMIC_SOURCES, **WEB_SOURCES, "arxiv_oa_pdf": OA_PDF}.items():
        outcomes[name] = _probe(url)
    academic_ok = sum(outcomes[n]["reachable"] for n in ACADEMIC_SOURCES)
    web_ok = sum(outcomes[n]["reachable"] for n in WEB_SOURCES)
    record("real-network-connectivity",
           "PASS" if academic_ok >= 2 and web_ok >= 1 else "PARTIAL",
           {"academic_reachable": academic_ok, "web_reachable": web_ok,
            "sources": outcomes,
            "scope": "仅代表本次运行出口；不等于所有环境可达或无限额度"})
    assert academic_ok >= 2, f"学术来源可达数不足: {outcomes}"
    assert web_ok >= 1, f"网页来源全部不可达: {outcomes}"


def test_real_academic_multisource_search_and_failure_isolation(live_gateway, record):
    """多源真实检索 + JSON 结构化输出 + 逐源失败隔离。"""
    raw = live_gateway.gateway.call("search_papers", {
        "query": "attention is all you need",
        "sources": "openalex,crossref,arxiv,pubmed,europepmc",
        "max_results": 3,
        "format": "json",
    }, timeout=180)
    data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    record("real-academic-multisource", "PASS" if data["result_count"] else "PARTIAL",
           {"result_count": data["result_count"],
            "sources": data.get("sources"),
            "source_failures": data.get("source_failures"),
            "took_seconds": data.get("took_seconds"),
            "scope_note": data.get("scope_note")})
    assert data["result_count"] > 0, data
    assert isinstance(data.get("source_failures"), list), "必须逐源报告失败"
    assert len(data.get("sources") or []) >= 2, "至少两个真实来源返回结果"


def test_unpaywall_without_mailto_is_reported_not_silently_skipped(live_gateway, record):
    """缺 MCP_SEARCH_MAILTO 时 Unpaywall 必须显式 SOURCE_UNAVAILABLE。"""
    assert not os.environ.get("MCP_SEARCH_MAILTO"), "本用例刻意不配置联系邮箱"
    text = live_gateway.gateway.call("find_paper_fulltext", {
        "doi": "10.1038/nature12373",
    }, timeout=120)
    record("unpaywall-mailto-gate", "PASS" if "SOURCE_UNAVAILABLE" in text else "FAIL",
           text[:600])
    assert "SOURCE_UNAVAILABLE" in text, text
    assert "MCP_SEARCH_MAILTO" in text, text


def test_searxng_upstream_availability_is_not_fabricated(record):
    """未启用 search profile 时如实标注「未检测」，绝不虚构上游可达。"""
    url = os.environ.get("MCP_SEARCH_SEARXNG_URL", "").strip()
    if not url:
        record("searxng-upstream", "NOT_CONFIGURED",
               "未配置 MCP_SEARCH_SEARXNG_URL / 未启用 search profile；上游可达性未检测")
        pytest.skip("未启用 SearXNG search profile，上游可达性未检测")
    outcome = _probe(url.rstrip("/") + "/healthz")
    record("searxng-upstream", "PASS" if outcome["reachable"] else "FAIL", outcome)
    assert outcome["reachable"], outcome


def test_compose_deployment_health_after_rebuild(record):
    """重建后真实检查 3 个常驻服务；Docker 不可用时如实跳过。"""
    try:
        proc = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=True)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        record("compose-health", "SKIPPED", f"Docker 不可用: {exc}")
        pytest.skip(f"Docker 不可用: {exc}")
    rows = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            rows.extend(parsed if isinstance(parsed, list) else [parsed])
        except json.JSONDecodeError:
            continue
    summary = {r.get("Service"): r.get("Health") or r.get("Status")
               for r in rows if isinstance(r, dict)}
    expected = {"mcp-gateway", "mcp-admin-web", "audit-storage"}
    record("compose-health", "PASS" if expected <= set(summary) else "PARTIAL",
           {"services": summary, "expected": sorted(expected)})
    assert expected <= set(summary), f"常驻服务缺失: {summary}"
