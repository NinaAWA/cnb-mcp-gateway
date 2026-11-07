"""跨源论文元数据的归一化、去重合并与全局排序。

约定（V3 阶段 B）：
- ``from_year`` 统一表示「该年及之后」，``to_year`` 表示「该年及之前」；
  各来源不允许自行解释为「精确某一年」。
- ``citations=None`` 表示来源未提供该指标，``citations=0`` 表示确实为零引用；
  两者在输出中必须保持可区分。
- **先跨源合并去重 → 再全局排序 → 最后截断**，不允许先截断后排序。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

_DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)
_ARXIV_RE = re.compile(r"(?:arxiv[:\s/]?)?(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
_OLD_ARXIV_RE = re.compile(r"(?:arxiv[:\s/]?)?([a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?",
                           re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def normalize_doi(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^(https?://(dx\.)?doi\.org/)|^doi:\s*", "", text)
    match = _DOI_RE.search(text)
    return match.group(0).rstrip("./,)") if match else ""


def normalize_arxiv_id(value: Any) -> str:
    text = str(value or "").strip()
    match = _ARXIV_RE.search(text) or _OLD_ARXIV_RE.search(text)
    return match.group(1) if match else ""


def normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return _PUNCT_RE.sub(" ", _WS_RE.sub(" ", text)).strip()


def publication_year(value: Any) -> int | None:
    match = re.search(r"(1[5-9]\d{2}|20\d{2}|21\d{2})", str(value or ""))
    return int(match.group(1)) if match else None


def date_precision(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return "day"
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return "month"
    return "year" if re.fullmatch(r"\d{4}", text) else ""


def normalize_date(value: Any) -> str:
    """把 2020 / 2020-3 / 2020 Mar / 2020-03-02 统一成可排序的 ISO 前缀。"""
    text = str(value or "").strip()
    m = re.search(r"(1[5-9]\d{2}|20\d{2}|21\d{2})[-/ ]?(\d{1,2})?[-/ ]?(\d{1,2})?", text)
    if not m:
        return ""
    year = m.group(1)
    month = m.group(2)
    day = m.group(3)
    if month and 1 <= int(month) <= 12:
        if day and 1 <= int(day) <= 31:
            return f"{year}-{int(month):02d}-{int(day):02d}"
        return f"{year}-{int(month):02d}"
    return year


@dataclass
class Paper:
    """结构化论文契约；``citation_count=None`` 表示指标不可用。"""

    paper_id: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    publication_year: int | None = None
    publication_date: str = ""
    date_precision: str = ""
    venue: str = ""
    doi: str = ""
    arxiv_id: str = ""
    pmid: str = ""
    pmcid: str = ""
    citation_count: int | None = None
    oa_status: str = "metadata_only"
    landing_url: str = ""
    pdf_url: str = ""
    sources: list[str] = field(default_factory=list)
    retrieved_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _item_authors(item: dict[str, Any]) -> list[str]:
    raw = item.get("authors")
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def canonical_key(item: dict[str, Any] | Paper) -> str:
    data = item.to_dict() if isinstance(item, Paper) else item
    doi = normalize_doi(data.get("doi"))
    if doi:
        return "doi:" + doi
    arxiv_id = normalize_arxiv_id(data.get("arxiv_id") or data.get("url")
                                  or data.get("landing_url"))
    if arxiv_id:
        return "arxiv:" + arxiv_id
    title = normalize_title(data.get("title"))
    return "title:" + title if title else "url:" + str(data.get("url") or id(data))


def to_paper(item: dict[str, Any]) -> Paper:
    """把来源 dict 转成结构化记录，不虚构缺失字段。"""
    date = normalize_date(item.get("date"))
    pdf_url = str(item.get("pdf_url") or "")
    if not pdf_url:
        extra = str(item.get("extra") or "")
        pdf_url = extra[4:] if extra.startswith("PDF=") else ""
    extra = str(item.get("extra") or "")
    pmid = str(item.get("pmid") or "")
    if not pmid and extra.startswith("PMID="):
        pmid = extra[5:]
    pmcid = str(item.get("pmcid") or "")
    oa = str(item.get("oa_status") or "")
    if not oa:
        oa = "oa_pdf" if pdf_url else ("oa_html" if "OA=yes" in extra else "metadata_only")
    citations = item.get("citations")
    if citations is None:
        citations = item.get("citation_count")
    sources = item.get("sources")
    if sources is None:
        sources = [str(item.get("source") or "")] if item.get("source") else []
    if isinstance(sources, str):
        sources = [sources]
    return Paper(
        paper_id=canonical_key(item),
        title=str(item.get("title") or ""),
        authors=_item_authors(item),
        abstract=str(item.get("snippet") or item.get("abstract") or ""),
        publication_year=publication_year(date or item.get("date")),
        publication_date=date,
        date_precision=date_precision(date),
        venue=str(item.get("venue") or ""),
        doi=normalize_doi(item.get("doi")),
        arxiv_id=normalize_arxiv_id(item.get("arxiv_id") or item.get("url")),
        pmid=pmid,
        pmcid=pmcid,
        citation_count=(int(citations) if isinstance(citations, (int, float))
                        and not isinstance(citations, bool) else None),
        oa_status=oa,
        landing_url=str(item.get("landing_url") or item.get("url") or ""),
        pdf_url=pdf_url,
        sources=[str(s) for s in sources if s],
        retrieved_at=str(item.get("retrieved_at") or ""),
    )


def _prefer(old: Any, new: Any) -> Any:
    """合并时选择信息量更大的值，不把缺失误写成 0。"""
    if new is None or new == "" or new == []:
        return old
    if old is None or old == "" or old == []:
        return new
    if isinstance(old, str) and isinstance(new, str):
        return new if len(new) > len(old) else old
    return old


def merge_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """跨源去重合并：同一 DOI / arXiv ID / 标准标题只保留一条，来源累加。"""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for raw in items:
        item = dict(raw)
        key = canonical_key(item)
        if key not in merged:
            merged[key] = dict(item)
            order.append(key)
            continue
        target = merged[key]
        for field_name, value in item.items():
            if field_name in ("source", "sources", "citations", "citation_count"):
                continue
            target[field_name] = _prefer(target.get(field_name), value)
        src = str(item.get("source") or "")
        existing = target.setdefault("sources", [str(target.get("source") or "")])
        if not isinstance(existing, list):
            existing = [str(existing)]
            target["sources"] = existing
        if src and src not in existing:
            existing.append(src)
        known = [target.get("citations"), item.get("citations")]
        numeric = [int(v) for v in known if isinstance(v, (int, float))
                   and not isinstance(v, bool)]
        target["citations"] = max(numeric) if numeric else None
        if item.get("pdf_url") and not target.get("pdf_url"):
            target["pdf_url"] = item["pdf_url"]
    out = []
    for key in order:
        row = merged[key]
        sources = row.get("sources") or []
        if isinstance(sources, str):
            sources = [sources]
        row["sources"] = [s for s in sources if s]
        row["source"] = ", ".join(row["sources"]) or str(row.get("source") or "")
        row["paper_id"] = key
        out.append(row)
    return out


def rank_items(items: list[dict[str, Any]], sort: str = "relevance",
               query: str = "") -> list[dict[str, Any]]:
    """全局排序（在截断之前调用）；relevance 用轻量确定性打分。"""
    if sort == "date":
        return sorted(items, key=lambda x: (normalize_date(x.get("date")),
                                           str(x.get("title") or "")), reverse=True)
    terms = [t for t in normalize_title(query).split() if len(t) > 1]

    def score(item: dict[str, Any]) -> tuple[float, str]:
        title = normalize_title(item.get("title"))
        body = normalize_title(item.get("snippet"))
        hits = sum(1 for t in terms if t in title) * 3 + sum(
            0.3 for t in terms if t in body)
        citations = item.get("citations")
        cited = float(citations) if isinstance(citations, (int, float)) else 0.0
        # 引用数只影响排序，不改变输出中 None / 0 的语义
        diversity = min(3.0, 0.5 * len(item.get("sources") or []))
        return hits + min(20.0, cited ** 0.5) + diversity, title

    return sorted(items, key=score, reverse=True)


def structured_output(items: list[dict[str, Any]], *, query: str,
                      sources: list[str], took: float,
                      errors: list[str] | None = None,
                      scope_note: str = "") -> str:
    """JSON 输出：结构化元数据 + 逐源失败原因 + 覆盖范围说明。"""
    import json
    import time

    papers = [to_paper(item).to_dict() for item in items]
    payload = {
        "query": query,
        "sources": sources,
        "source_failures": errors or [],
        "took_seconds": round(took, 3),
        "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "result_count": len(papers),
        "scope_note": scope_note,
        "citations_semantics": "citation_count=null 表示来源未提供；0 表示真实零引用",
        "results": papers,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
