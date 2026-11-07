"""本地文献库：项目/标签/阅读状态、去重与 BibTeX/RIS/CSV/JSON 导出。"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from gateway.audit.storage import get_storage


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    try:
        parsed = json.loads(value or "[]")
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def list_papers(filters: dict[str, Any] | None = None,
                limit: int = 200) -> list[dict[str, Any]]:
    f = filters or {}
    conditions = []
    params: list[Any] = []
    if f.get("keyword"):
        conditions.append("(title LIKE ? OR authors LIKE ? OR doi LIKE ? "
                          "OR arxiv_id LIKE ?)")
        like = f"%{f['keyword']}%"
        params.extend([like] * 4)
    if f.get("project"):
        conditions.append("project = ?")
        params.append(f["project"])
    if f.get("tag"):
        conditions.append("tags LIKE ?")
        params.append(f"%{f['tag']}%")
    if f.get("reading_status"):
        conditions.append("reading_status = ?")
        params.append(f["reading_status"])
    if f.get("from_year"):
        conditions.append("publication_year >= ?")
        params.append(int(f["from_year"]))
    if f.get("to_year"):
        conditions.append("publication_year <= ?")
        params.append(int(f["to_year"]))
    if f.get("doi"):
        conditions.append("doi = ?")
        params.append(f["doi"])
    if f.get("identifier"):
        conditions.append("(doi = ? OR arxiv_id = ? OR pmid = ? OR pmcid = ?)")
        params.extend([f["identifier"]] * 4)
    if f.get("oa_status"):
        conditions.append("oa_status = ?")
        params.append(f["oa_status"])
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = get_storage()._query(
        f"SELECT * FROM paper_library{where} ORDER BY id DESC LIMIT ?",
        params + [max(1, min(limit, 2000))])
    for row in rows:
        row["authors"] = _json_list(row.get("authors"))
        row["sources"] = _json_list(row.get("sources"))
        row["tags"] = _json_list(row.get("tags"))
        row["sha256_verified"] = bool(row.get("sha256") and row.get("file_path"))
    return rows


def _bibtex_key(row: dict[str, Any], index: int) -> str:
    author = (row.get("authors") or ["anon"])[0] if isinstance(
        row.get("authors"), list) else "anon"
    surname = re.sub(r"[^A-Za-z]", "", str(author).split()[-1] if author else "anon")
    year = row.get("publication_year") or "nd"
    return f"{surname or 'anon'}{year}{index}"


def export_bibliography(fmt: str = "bibtex",
                        filters: dict[str, Any] | None = None) -> str:
    rows = list_papers(filters)
    fmt = (fmt or "bibtex").strip().lower()
    if fmt == "json":
        return json.dumps({"count": len(rows), "items": rows},
                          ensure_ascii=False, indent=2, default=str)
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf, fieldnames=["paper_id", "title", "authors", "publication_year",
                             "venue", "doi", "arxiv_id", "oa_status", "file_path",
                             "sha256", "project", "reading_status"],
            extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            copy = dict(row)
            copy["authors"] = "; ".join(row.get("authors") or [])
            writer.writerow({k: copy.get(k, "") for k in writer.fieldnames})
        return buf.getvalue()
    if fmt == "ris":
        chunks = []
        for row in rows:
            lines = ["TY  - JOUR"]
            if row.get("title"):
                lines.append(f"TI  - {row['title']}")
            for author in row.get("authors") or []:
                lines.append(f"AU  - {author}")
            if row.get("publication_year"):
                lines.append(f"PY  - {row['publication_year']}")
            if row.get("venue"):
                lines.append(f"JO  - {row['venue']}")
            if row.get("doi"):
                lines.append(f"DO  - {row['doi']}")
            if row.get("file_path"):
                lines.append(f"L1  - {row['file_path']}")
            lines.append("ER  - ")
            chunks.append("\n".join(lines))
        return "\n".join(chunks)
    if fmt != "bibtex":
        raise ValueError("format 只能是 bibtex/ris/csv/json")
    chunks = []
    for index, row in enumerate(rows, 1):
        fields = [
            f"  title = {{{row.get('title') or ''}}}",
            "  author = {" + " and ".join(row.get("authors") or []) + "}",
            f"  year = {{{row.get('publication_year') or ''}}}",
        ]
        if row.get("venue"):
            fields.append(f"  journal = {{{row['venue']}}}")
        if row.get("doi"):
            fields.append(f"  doi = {{{row['doi']}}}")
        if row.get("arxiv_id"):
            fields.append(f"  eprint = {{{row['arxiv_id']}}}")
        if row.get("file_path"):
            fields.append(f"  file = {{{row['file_path']}}}")
        chunks.append("@article{" + _bibtex_key(row, index) + ",\n"
                      + ",\n".join(fields) + "\n}")
    return "\n\n".join(chunks)


def download_status(request_id: str = "", limit: int = 50) -> dict[str, Any]:
    """区分「任务创建」与「文件最终校验成功」，不把 created 计为成功。"""
    store = get_storage()
    if request_id:
        rows = store._query(
            "SELECT * FROM paper_downloads WHERE request_id=? ORDER BY id ASC",
            [request_id])
    else:
        rows = store._query(
            "SELECT * FROM paper_downloads ORDER BY id DESC LIMIT ?",
            [max(1, min(limit, 500))])
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "rows": rows, "total": len(rows), "counts": counts,
        "verified_success": counts.get("success", 0) + counts.get("duplicate", 0),
        "note": "created 只表示任务已创建；success/duplicate 表示文件最终校验通过",
    }
