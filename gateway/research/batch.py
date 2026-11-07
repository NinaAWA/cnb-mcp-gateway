"""批量下载：去重、有界并发、可取消、逐篇状态与部分成功统计。"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from gateway.net.client import SafeHttpClient
from .download import DOWNLOAD_CONCURRENCY, download_paper, paper_key


def batch_download(papers: list[dict[str, Any]], *, client: SafeHttpClient,
                   mailto: str = "", paper_dir: Path | None = None,
                   request_id: str = "", trace_id: str = "", project: str = "",
                   max_workers: int = 0,
                   cancel: Callable[[], bool] | None = None,
                   extra_hosts: frozenset[str] = frozenset(),
                   allow_private: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    workers = max(1, min(max_workers or DOWNLOAD_CONCURRENCY, 8))
    unique: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for item in papers:
        key = paper_key(item)
        if key in unique:
            duplicates.append(key)
            continue
        unique[key] = item
    results: list[dict[str, Any]] = []
    if unique:
        with ThreadPoolExecutor(max_workers=min(workers, len(unique))) as pool:
            futures = {
                pool.submit(download_paper, item, client=client, mailto=mailto,
                            paper_dir=paper_dir,
                            request_id=f"{request_id}:{idx}", trace_id=trace_id,
                            project=project, cancel=cancel,
                            extra_hosts=extra_hosts,
                            allow_private=allow_private): key
                for idx, (key, item) in enumerate(unique.items())
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001 - 单篇失败不影响其余
                    outcome = {"status": "failed", "error_type": "UNEXPECTED",
                               "reason": repr(exc)}
                results.append({"paper_id": key, **outcome})
    succeeded = sum(1 for r in results if r["status"] in ("success", "duplicate"))
    failed = len(results) - succeeded
    return {
        "status": "success" if results and failed == 0 else (
            "partial" if succeeded else "failed"),
        "total": len(papers), "unique": len(unique),
        "duplicates_skipped": duplicates,
        "succeeded": succeeded, "failed": failed,
        "workers": workers,
        "took_seconds": round(time.monotonic() - started, 3),
        "results": [{k: v for k, v in r.items() if k != "discovery"}
                    for r in results],
        "note": "部分成功如实列出，未成功条目附具体原因；created 不等于下载成功",
    }
