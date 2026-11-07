"""PDF 按页阅读：pypdf 提取页文本/页码/页数、章节与关键词/参考文献检索。

OCR 明确为可选能力：默认不安装、不调用；扫描版（无可提取文本）如实提示
需要 OCR 且不伪造正文。大 PDF 只返回文本片段与路径，绝不 Base64 或写入审计库。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from gateway.transfer.core import verify_pdf_file

_CACHE_DIR = Path(os.environ.get("MCP_PAPER_TEXT_CACHE_DIR", "/tmp/mcp_paper_text"))
_SECTION_RE = re.compile(
    r"^\s*(?:(\d+(?:\.\d+)*)\s+)?(abstract|introduction|background|related work|"
    r"methods?|methodology|experiments?|results?|discussion|conclusion|"
    r"references|bibliography|appendix|摘要|引言|方法|实验|结果|结论|参考文献)\s*$",
    re.IGNORECASE)


def _reader(path: str | Path):
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依赖缺失时显式报错
        raise RuntimeError("缺少 pypdf 依赖：pip install 'pypdf>=5,<8'") from exc
    return PdfReader(str(path))


def _cache_key(path: Path) -> str:
    import hashlib
    st = path.stat()
    return hashlib.sha256(f"{path}|{st.st_size}|{int(st.st_mtime)}"
                          .encode()).hexdigest()[:32]


def _cache_path(key: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{key}.json"


def _load_pages(path: Path, use_cache: bool = True) -> list[str]:
    key = _cache_key(path)
    cached = _cache_path(key) if use_cache else None
    if cached and cached.is_file():
        try:
            data = json.loads(cached.read_text(encoding="utf-8"))
            if isinstance(data, list) and all(isinstance(x, str) for x in data):
                return data
        except (OSError, ValueError):
            pass
    pages = []
    for page in _reader(path).pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - 单页解析失败保留空页而非中断
            pages.append("")
    if cached:
        try:
            cached.write_text(json.dumps(pages, ensure_ascii=False),
                              encoding="utf-8")
            os.chmod(cached, 0o600)
        except OSError:
            pass
    return pages


def _sections(pages: list[str]) -> list[dict[str, Any]]:
    found = []
    for number, text in enumerate(pages, 1):
        for line in text.splitlines()[:80]:
            match = _SECTION_RE.match(line)
            if match:
                found.append({"page": number, "heading": line.strip()[:120],
                              "number": match.group(1) or ""})
    return found[:100]


def read_pdf(path: str | Path, *, page: int = 0, keyword: str = "",
             max_chars: int = 8000, use_cache: bool = True,
             list_sections: bool = False) -> dict[str, Any]:
    """返回结构化阅读结果；page=0 表示全部页，keyword 支持跨页检索。"""
    file = Path(path)
    ok, reason = verify_pdf_file(file)
    if not ok:
        return {"status": "failed", "reason": reason, "page_count": 0,
                "pages": [], "matches": [], "sections": []}
    try:
        pages = _load_pages(file, use_cache)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "reason": f"PDF 解析失败: {exc!r}",
                "page_count": 0, "pages": [], "matches": [], "sections": []}
    page_count = len(pages)
    text_total = sum(len(p.strip()) for p in pages)
    scanned = page_count > 0 and text_total < max(20, page_count * 8)
    selected = list(range(1, page_count + 1)) if page <= 0 else [page]
    selected = [n for n in selected if 1 <= n <= page_count]
    if page > page_count:
        return {"status": "failed", "reason": f"页码 {page} 超出范围（共 {page_count} 页）",
                "page_count": page_count, "pages": [], "matches": [], "sections": []}
    body: list[str] = []
    if not scanned:
        used = 0
        for number in selected:
            text = pages[number - 1]
            if max_chars <= 0 or used >= max_chars:
                break
            room = max(0, max_chars - used)
            piece = text[:room]
            body.append(f"----- 第 {number}/{page_count} 页 -----\n" + piece)
            used += len(piece)
    matches: list[dict[str, Any]] = []
    if keyword:
        needle = keyword.strip().lower()
        for number, text in enumerate(pages, 1):
            if needle in text.lower():
                idx = text.lower().find(needle)
                matches.append({"page": number,
                                "excerpt": text[max(0, idx - 80):idx + 160].strip()})
    result = {
        "status": "success" if not scanned else "scanned_no_text",
        "path": str(file), "page_count": page_count,
        "pages": body, "matches": matches[:100],
        "sections": _sections(pages) if list_sections else [],
        "characters_extracted": text_total,
        "cached": use_cache and _cache_path(_cache_key(file)).is_file(),
        "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if scanned:
        result["reason"] = ("未提取到可用文本，疑似扫描版 PDF；需可选 OCR 能力"
                           "（默认未安装、未启用），不伪造正文")
    return result


def search_pdf_fulltext(path: str | Path, keyword: str,
                        max_results: int = 20) -> dict[str, Any]:
    result = read_pdf(path, keyword=keyword, max_chars=0)
    result["matches"] = result.get("matches", [])[:max(1, max_results)]
    result["keyword"] = keyword
    return result
