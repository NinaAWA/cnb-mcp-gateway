"""网页检索增强：站点/时间/语言筛选、跨源去重、正文与元数据提取、分段。

不把垂直社区检索伪称为全网搜索；站点限定搜索在未配置通用 provider 时由调用方
明确返回 SOURCE_UNAVAILABLE，不静默降级成 HN / StackExchange。
"""
from __future__ import annotations

import html
import re
import time
from html.parser import HTMLParser
from typing import Any

from gateway.research.metadata import merge_items, normalize_date, rank_items

_META_RE = re.compile(
    r"<meta[^>]+(?:name|property)=[\"']([^\"']+)[\"'][^>]+content=[\"']([^\"']*)[\"']",
    re.IGNORECASE)
_META_REV_RE = re.compile(
    r"<meta[^>]+content=[\"']([^\"']*)[\"'][^>]+(?:name|property)=[\"']([^\"']+)[\"']",
    re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class _MainTextExtractor(HTMLParser):
    """优先取 article/main，跳过脚本样式，保留段落边界。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._main_depth = 0
        self.main_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template", "nav", "footer"):
            self._skip += 1
        if tag in ("article", "main"):
            self._main_depth += 1
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "section"):
            (self.main_parts if self._main_depth else self.parts).append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template", "nav", "footer"):
            self._skip = max(0, self._skip - 1)
        if tag in ("article", "main"):
            self._main_depth = max(0, self._main_depth - 1)

    def handle_data(self, data):
        if not self._skip:
            (self.main_parts if self._main_depth else self.parts).append(data)


def _clean(value: Any, limit: int = 0) -> str:
    text = _WS_RE.sub(" ", html.unescape(str(value or ""))).strip()
    return text[:limit] if limit else text


def extract_content(raw_html: str, *, max_chars: int = 8000,
                    chunk_size: int = 0) -> dict[str, Any]:
    """提取标题、作者、发布时间、正文与可选分段；不虚构缺失字段。"""
    parser = _MainTextExtractor()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:  # noqa: BLE001
        pass
    body = " ".join(parser.main_parts) if parser.main_parts else " ".join(parser.parts)
    body = _WS_RE.sub(" ", body).strip()
    if not body:
        body = _clean(_TAG_RE.sub(" ", raw_html))
    title = ""
    match = _TITLE_RE.search(raw_html)
    if match:
        title = _clean(_TAG_RE.sub(" ", match.group(1)), 300)
    meta: dict[str, str] = {}
    for name, value in _META_RE.findall(raw_html):
        meta[name.lower()] = html.unescape(value)
    for value, name in _META_REV_RE.findall(raw_html):
        meta.setdefault(name.lower(), html.unescape(value))
    author = (meta.get("author") or meta.get("article:author")
              or meta.get("og:article:author") or "")
    published = (meta.get("article:published_time")
                 or meta.get("date") or meta.get("dc.date") or "")
    body = body[:max_chars]
    chunks: list[str] = []
    if chunk_size and chunk_size > 0:
        chunks = [body[i:i + chunk_size] for i in range(0, len(body), chunk_size)]
    return {
        "title": title,
        "author": _clean(author, 200),
        "published": _clean(published, 60),
        "description": _clean(meta.get("description") or meta.get("og:description"), 500),
        "text": body,
        "chunks": chunks,
        "chunk_size": chunk_size,
        "truncated": len(_WS_RE.sub(" ", " ".join(parser.parts)).strip()) > max_chars,
        "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def filter_results(items: list[dict[str, Any]], *, query: str = "",
                   from_year: int = 0, to_year: int = 0,
                   language: str = "", site: str = "") -> tuple[list[dict[str, Any]], list[str]]:
    """客户端筛选；返回 (结果, 被过滤原因计数说明)。"""
    notes: list[str] = []
    out: list[dict[str, Any]] = []
    dropped = {"date": 0, "site": 0, "language": 0}
    for item in items:
        date = normalize_date(item.get("date"))
        if (from_year or to_year) and date:
            year = int(date[:4]) if date[:4].isdigit() else 0
            if (from_year and year < from_year) or (to_year and year > to_year):
                dropped["date"] += 1
                continue
        if site and site.lower() not in str(item.get("url") or "").lower():
            if item.get("source") in ("Hacker News", "StackExchange"):
                # 垂直社区的结果 URL 不一定属于查询站点，仅在明确要求站点时剔除
                dropped["site"] += 1
                continue
        if language:
            text = f"{item.get('title') or ''} {item.get('snippet') or ''}"
            if language.lower() == "zh" and not re.search(r"[\u4e00-\u9fff]", text):
                dropped["language"] += 1
                continue
            if language.lower() == "en" and re.search(r"[\u4e00-\u9fff]", text):
                dropped["language"] += 1
                continue
        out.append(item)
    for key, count in dropped.items():
        if count:
            notes.append(f"按 {key} 条件过滤 {count} 条")
    return out, notes


def merge_and_rank(items: list[dict[str, Any]], sort: str = "relevance",
                   query: str = "", limit: int = 30) -> list[dict[str, Any]]:
    """先去重合并，再全局排序，最后截断（顺序不可颠倒）。"""
    return rank_items(merge_items(items), sort, query)[:limit]
