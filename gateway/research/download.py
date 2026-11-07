"""单篇合法 OA PDF 下载：全过程状态持久化，绝不假报成功。

状态语义（后台与审计必须区分）：
- ``created``：任务已创建、已落库，**不代表文件已下载成功**；
- ``downloading`` / ``verifying``：流式写入 / 完整性校验中；
- ``success``：最终文件已通过 %PDF- / %%EOF / Content-Type / 最终 URL /
  大小 / SHA-256 校验并原子改名；
- ``failed`` / ``cancelled`` / ``duplicate``：附具体原因。
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from gateway.net.client import SafeHttpClient
from gateway.transfer.core import PaperSourcePolicy, fetch_to_file
from .fulltext import discover_oa

PAPER_DIR = Path(os.environ.get("MCP_PAPER_DIR", "/workspace/research/papers"))
PAPER_MAX_MB = max(1, min(512, int(os.environ.get("MCP_PAPER_MAX_MB", "64"))))
DOWNLOAD_CONCURRENCY = max(1, min(8, int(os.environ.get("MCP_DOWNLOAD_CONCURRENCY", "3"))))


def _emit(values: dict[str, Any]) -> None:
    """结构化下载事件；审计层不可用时退化为直接写库，不中断下载。"""
    try:
        from gateway.audit.middleware import record_download_event
        if record_download_event(values):
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        from gateway.audit.storage import get_storage
        store = get_storage()
        keys = list(values)
        store._execute(
            f"INSERT INTO paper_downloads ({','.join(keys)}) "
            f"VALUES ({','.join('?' for _ in keys)})",
            [values[k] for k in keys])
    except Exception:  # noqa: BLE001
        pass


def safe_filename(paper_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(paper_id or "paper")).strip("._")
    if not cleaned:
        cleaned = hashlib.sha256(str(paper_id).encode()).hexdigest()[:16]
    return cleaned[:120] + ".pdf"


def paper_key(paper: dict[str, Any]) -> str:
    key = (str(paper.get("doi") or "") or str(paper.get("arxiv_id") or "")
           or str(paper.get("title") or "") or "paper")
    return key


def download_paper(paper: dict[str, Any], *, client: SafeHttpClient,
                   mailto: str = "", paper_dir: Path | None = None,
                   request_id: str = "", trace_id: str = "", project: str = "",
                   cancel: Callable[[], bool] | None = None,
                   extra_hosts: frozenset[str] = frozenset(),
                   allow_private: bool = False) -> dict[str, Any]:
    root = paper_dir or PAPER_DIR
    key = paper_key(paper)
    target = root / safe_filename(key)
    title = str(paper.get("title") or key)
    base = {"request_id": request_id, "trace_id": trace_id, "paper_id": key,
            "title": title, "project": project, "file_path": str(target),
            "status": "created", "bytes": 0, "total_bytes": 0,
            "sha256": "", "source": "", "url": "", "error_type": "",
            "error_message": ""}
    _emit(base)
    discovery = discover_oa(paper, client=client, mailto=mailto,
                            extra_hosts=extra_hosts,
                            allow_private=allow_private)
    if not discovery["candidates"]:
        reason = "; ".join(discovery["reasons"]) or discovery["status"]
        _emit({**base, "status": "failed", "error_type": discovery["status"],
               "error_message": reason[:500]})
        return {"status": "failed", "error_type": discovery["status"],
                "reason": reason, "discovery": discovery,
                "path": "", "sha256": "", "bytes": 0}
    policy = PaperSourcePolicy(extra_hosts=extra_hosts,
                               max_bytes=PAPER_MAX_MB * 1024 * 1024)
    failures: list[str] = []
    for candidate in discovery["candidates"]:
        _emit({**base, "status": "downloading", "source": candidate["source"],
               "url": candidate["url"]})
        result = fetch_to_file(
            client, candidate["url"], target, policy=policy, cancel=cancel,
            resume=True, allow_private=allow_private,
            # 只在阶段切换时落库，避免每个分块写一行；最终成功由下方统一记录，
            # 保证「最终校验成功」只有一条，不把 created/progress 误计为成功。
            progress=lambda info: _emit({
                **base, "status": info["status"],
                "bytes": info.get("bytes", 0), "source": candidate["source"],
                "url": candidate["url"],
            }) if info.get("status") == "verifying" else None)
        if result.status in ("success", "duplicate"):
            _emit({**base, "status": result.status, "bytes": result.bytes,
                   "total_bytes": result.bytes, "sha256": result.sha256,
                   "source": candidate["source"], "url": candidate["url"],
                   "detail": f"final_url={result.final_url}; "
                             f"content_type={result.content_type}"})
            _upsert_library(paper, result, request_id, project=project)
            return {"status": result.status, "path": result.path,
                    "sha256": result.sha256, "bytes": result.bytes,
                    "final_url": result.final_url,
                    "content_type": result.content_type,
                    "resumed": result.resumed, "discovery": discovery,
                    "reason": ""}
        failures.append(f"{candidate['source']}: {result.reason}")
        _emit({**base, "status": "failed", "source": candidate["source"],
               "url": candidate["url"], "bytes": result.bytes,
               "error_type": "VALIDATION_FAILED",
               "error_message": result.reason[:500]})
    return {"status": "failed", "error_type": "ALL_CANDIDATES_FAILED",
            "reason": "; ".join(failures), "discovery": discovery,
            "path": "", "sha256": "", "bytes": 0}


def _upsert_library(paper: dict[str, Any], result, request_id: str,
                    project: str = "") -> None:
    """成功下载后登记文献库；重复下载不得清空已有的项目/标签/阅读状态。

    ``project`` 是 download_paper 的显式参数（批量下载也走这里），不能只依赖
    ``paper[\"project\"]``，否则 batch_download_papers(project=...) 会丢失关联。
    """
    try:
        from gateway.audit.storage import get_storage
        import json as _json

        store = get_storage()
        paper_id = paper_key(paper)
        values = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "paper_id": paper_id,
            "title": str(paper.get("title") or paper_id),
            "authors": _json.dumps(paper.get("authors") or [], ensure_ascii=False),
            "publication_year": int(paper.get("publication_year") or 0),
            "venue": str(paper.get("venue") or ""),
            "doi": str(paper.get("doi") or ""),
            "arxiv_id": str(paper.get("arxiv_id") or ""),
            "pmid": str(paper.get("pmid") or ""),
            "pmcid": str(paper.get("pmcid") or ""),
            "citation_count": paper.get("citation_count"),
            "oa_status": "oa_pdf",
            "landing_url": str(paper.get("landing_url") or ""),
            "pdf_url": str(paper.get("pdf_url") or ""),
            "sources": _json.dumps(paper.get("sources") or [], ensure_ascii=False),
            "file_path": result.path,
            "sha256": result.sha256,
            "file_size": result.bytes,
            "project": str(project or paper.get("project") or ""),
            "tags": _json.dumps(paper.get("tags") or [], ensure_ascii=False),
            "reading_status": "unread",
            "detail": f"request_id={request_id}",
        }
        # 重新下载（含去重命中）时，空值不得覆盖用户已设置的项目 / 标签 / 阅读状态。
        preserve_when_empty = {
            "project": values["project"] == "",
            "tags": values["tags"] in ("[]", "", "null"),
            # 阅读状态由用户维护，重复下载不应重置为 unread（首次插入仍是 unread）。
            "reading_status": True,
        }
        updates = ",".join(
            f"{k}=excluded.{k}" for k in values
            if k not in ("ts", "paper_id")
            and not preserve_when_empty.get(k, False))
        cols = ",".join(values)
        store._execute(
            f"INSERT INTO paper_library ({cols}) VALUES "
            f"({','.join('?' for _ in values)}) "
            f"ON CONFLICT(paper_id) DO UPDATE SET {updates}",
            list(values.values()))
    except Exception as exc:  # noqa: BLE001 - 文献库写入失败不改变下载成败
        # 静默吞掉会让「下载成功但文献库为空」无法定位；显式告警，不含敏感值。
        import logging
        logging.getLogger(__name__).warning(
            "paper_library upsert failed for %s: %s",
            paper_key(paper), type(exc).__name__)
