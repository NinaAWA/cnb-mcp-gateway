"""合法开放获取全文发现（不绕过付费墙、不使用盗版镜像）。

发现顺序：arXiv 官方 PDF → Europe PMC / PMC → OpenAlex best_oa_location →
Unpaywall（缺邮箱时明确 SOURCE_UNAVAILABLE）→ 合法机构/出版社 OA 位置。
每个候选都带明确状态与原因；找不到时如实返回 NO_OA / SUBSCRIPTION_REQUIRED。
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from gateway.net.client import SafeHttpClient
from gateway.transfer.core import OA_ALLOWED_HOSTS, FORBIDDEN_HOST_PARTS, PolicyError

STATUS_FOUND = "FOUND"
STATUS_NO_OA = "NO_OA"
STATUS_SUBSCRIPTION = "SUBSCRIPTION_REQUIRED"
STATUS_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
STATUS_INVALID_HOST = "INVALID_HOST"
STATUS_ERROR = "ERROR"


def host_allowed(url: str, extra_hosts: frozenset[str] = frozenset()) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https"):
        return False
    if any(bad in host for bad in FORBIDDEN_HOST_PARTS):
        return False
    return host in OA_ALLOWED_HOSTS or host in extra_hosts


def _candidate(url: str, source: str, extra_hosts: frozenset[str],
               reasons: list[str]) -> dict[str, Any] | None:
    if not url:
        return None
    if not host_allowed(url, extra_hosts):
        reasons.append(f"{source} 返回的 PDF 主机不在合法 OA 白名单: {url}")
        return None
    return {"url": url, "source": source, "status": STATUS_FOUND,
            "host": urllib.parse.urlsplit(url).hostname or ""}


def discover_oa(paper: dict[str, Any], *, client: SafeHttpClient | None = None,
                mailto: str = "", extra_hosts: frozenset[str] = frozenset(),
                allow_private: bool = False) -> dict[str, Any]:
    """返回 {status, candidates, reasons}；绝不虚构 PDF 链接或成功状态。"""
    reasons: list[str] = []
    candidates: list[dict[str, Any]] = []
    paper_id = str(paper.get("arxiv_id") or paper.get("doi") or
                   paper.get("pmcid") or paper.get("title") or "unknown")
    doi = str(paper.get("doi") or "").strip()
    arxiv_id = str(paper.get("arxiv_id") or "").strip()
    pmcid = str(paper.get("pmcid") or "").strip()

    if arxiv_id:
        official = f"https://arxiv.org/pdf/{arxiv_id}"
        found = _candidate(official, "arXiv 官方", extra_hosts, reasons)
        if found:
            candidates.append(found)
    if pmcid:
        pmc = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/"
        found = _candidate(pmc, "PMC 开放全文", extra_hosts, reasons)
        if found:
            candidates.append(found)
    if paper.get("pdf_url"):
        found = _candidate(str(paper["pdf_url"]), "已登记 OA 位置",
                           extra_hosts, reasons)
        if found:
            candidates.append(found)

    if doi and client is not None:
        try:
            data = client.json_get(
                f"https://api.openalex.org/works/https://doi.org/{doi}",
                allow_private=allow_private)
            location = data.get("best_oa_location") or {}
            url = location.get("pdf_url") or ""
            found = _candidate(url, "OpenAlex OA", extra_hosts, reasons)
            if found:
                candidates.append(found)
            elif not location.get("is_oa"):
                reasons.append("OpenAlex: 该 DOI 未标记开放获取")
        except Exception as exc:  # noqa: BLE001 - 单源失败不影响其余来源
            reasons.append(f"OpenAlex OA 查询失败: {str(exc)[:180]}")

    if doi:
        if not mailto:
            reasons.append(
                "SOURCE_UNAVAILABLE: Unpaywall 需要 MCP_SEARCH_MAILTO（联系邮箱）"
                "才能查询合法 OA 位置；已跳过而非静默忽略")
        elif client is not None:
            try:
                data = client.json_get(
                    "https://api.unpaywall.org/v2/" + urllib.parse.quote(doi, safe="")
                    + "?email=" + urllib.parse.quote(mailto),
                    allow_private=allow_private)
                location = data.get("best_oa_location") or {}
                url = location.get("pdf_url") or ""
                found = _candidate(url, "Unpaywall OA", extra_hosts, reasons)
                if found:
                    candidates.append(found)
                else:
                    reasons.append(
                        f"Unpaywall: is_oa={data.get('is_oa')}, "
                        "未提供可用的合法 OA PDF 位置")
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"Unpaywall 查询失败: {str(exc)[:180]}")

    seen: set[str] = set()
    unique = []
    for item in candidates:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    if unique:
        status = STATUS_FOUND
    elif mailto or not doi:
        status = STATUS_NO_OA
        if doi and client is not None:
            status = STATUS_SUBSCRIPTION
    else:
        status = STATUS_SOURCE_UNAVAILABLE
    if any("不在合法 OA 白名单" in r for r in reasons):
        status = status if unique else STATUS_INVALID_HOST
    return {"paper_id": paper_id, "status": status, "candidates": unique,
            "reasons": reasons,
            "note": "仅发现合法开放获取位置；不绕过付费墙，不使用盗版镜像。"}
