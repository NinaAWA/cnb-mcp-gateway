"""audit-storage 维护服务：保留策略、归档、备份、配额与故障告警。

SQLite 是文件型数据库，「audit-storage」服务不提供网络端口，而是作为独立
容器对共享数据卷执行维护；需要多实例 / 高并发时改用 postgres profile。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .audit.storage import get_storage

DATA_DIR = Path(os.environ.get("AUDIT_DB_PATH", "/data/audit/audit.db")).parent
INTERVAL = max(30, int(os.environ.get("AUDIT_MAINTENANCE_INTERVAL", "300")))
RETENTION_DAYS = max(1, int(os.environ.get("AUDIT_RETENTION_DAYS", "90")))
MAX_DB_MB = max(0, int(os.environ.get("AUDIT_MAX_DB_MB", "512")))
BACKUP_KEEP = max(0, int(os.environ.get("AUDIT_BACKUP_KEEP", "7")))
HEARTBEAT = DATA_DIR / ".maintenance.ok"


def backup_once(storage) -> None:
    if os.environ.get("AUDIT_BACKUP_ENABLED", "1") != "1":
        return
    try:
        path = storage.backup("")
        if BACKUP_KEEP:
            files = sorted((DATA_DIR / "backup").glob("audit-*.db"))
            for old in files[:-BACKUP_KEEP]:
                old.unlink(missing_ok=True)
        storage.add_event("audit_backup", status="OK", source="audit-storage",
                          detail=path)
    except Exception as exc:  # noqa: BLE001
        warn(storage, f"备份失败: {exc!r}")


def warn(storage, message: str) -> None:
    storage.add_event("audit_storage_warning", status="DEGRADED",
                      source="audit-storage", detail=message[:1000])
    print(f"[audit-storage] WARN {message}", flush=True)


def tick(storage) -> None:
    if not storage.health()["healthy"]:
        warn(storage, "审计数据库不可用，网关已进入有界缓冲/降级模式")
    result = storage.archive_and_prune(RETENTION_DAYS, MAX_DB_MB)
    for message in result.get("errors", []):
        warn(storage, f"归档未完成，原记录已保留: {message}")
    if result["size_mb"] > MAX_DB_MB > 0:
        warn(storage, f"数据库超过配额 {MAX_DB_MB}MB，当前 {result['size_mb']}MB")
    if result["archived"] or result["pruned"] or result.get("errors"):
        storage.add_event(
            "audit_archive", status="DEGRADED" if result.get("errors") else "OK",
            source="audit-storage", detail=result,
        )
    backup_once(storage)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(str(int(time.time())), encoding="utf-8")


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    storage = get_storage()
    print(f"[audit-storage] backend={storage.health()['backend']} "
          f"retention={RETENTION_DAYS}d quota={MAX_DB_MB}MB interval={INTERVAL}s",
          flush=True)
    while True:
        try:
            tick(storage)
        except Exception as exc:  # noqa: BLE001
            print(f"[audit-storage] ERROR {exc!r}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
