"""审计写入的磁盘持久化缓冲（disk spool）。

内存 deque 只能保证「当前进程内不丢事件」：进程重启、崩溃或缓冲被挤满时，
仍在内存中的审计事件会永久丢失。本模块把每次写库失败的载荷同步追加到 0600
JSONL 文件（append + fsync），启动或数据库恢复后由 ``AuditStorage`` 回放补写。

设计约束：
- 只在写库失败路径写盘（正常路径零写放大）；
- 每条载荷带 ``event_id``，回放端用 ``spool_applied`` 表做幂等去重；
- 有界轮转（总字节数 / 文件数），超限明确告警而不是无限增长；
- 磁盘本身不可用时退化为纯内存缓冲，并暴露 ``last_error`` 供健康检查告警。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

ACTIVE_NAME = "spool-active.jsonl"
_PREFIX = "spool-"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class DiskSpool:
    """0600 追加式 JSONL；线程安全；轮转上限可配置。"""

    def __init__(self, directory: str | Path, *,
                 max_bytes: int = 0, max_files: int = 0, enabled: bool | None = None):
        self.directory = Path(directory)
        self.max_bytes = max_bytes or _env_int("AUDIT_SPOOL_MAX_MB", 64, 1, 4096) * 1024 * 1024
        self.max_files = max_files or _env_int("AUDIT_SPOOL_MAX_FILES", 4, 1, 64)
        self.enabled = _env_flag("AUDIT_SPOOL_ENABLED", True) if enabled is None else enabled
        self._lock = threading.RLock()
        self._error = ""
        # 缓存「是否有待回放文件」，避免每次审计写入都做目录扫描。
        self._has_pending = False
        if not self.enabled:
            self._error = "磁盘缓冲已被 AUDIT_SPOOL_ENABLED=0 显式禁用"
        else:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                os.chmod(self.directory, 0o700)
                self._has_pending = bool(self.files())
            except OSError as exc:
                self._error = f"磁盘缓冲目录不可用: {exc!r}"
                self.enabled = False

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._error

    @property
    def active(self) -> Path:
        return self.directory / ACTIVE_NAME

    def append(self, payload: dict[str, Any]) -> str:
        """同步落盘一条载荷；返回 event_id。失败时返回空串并设置 last_error。"""
        event_id = str(payload.get("event_id") or uuid.uuid4().hex)
        record = {**payload, "event_id": event_id}
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            if not self.enabled:
                return ""
            try:
                self._rotate_if_needed(len(line.encode("utf-8")))
                fd = os.open(self.active, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line.encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self._has_pending = True
                return event_id
            except OSError as exc:
                self._error = f"磁盘缓冲写入失败，事件可能只存在于内存: {exc!r}"
                return ""

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.active.stat().st_size if self.active.exists() else 0
        except OSError:
            size = 0
        if size and size + incoming > self.max_bytes:
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            target = self.directory / f"{_PREFIX}{stamp}-{uuid.uuid4().hex[:8]}.jsonl"
            self.active.replace(target)
            rotated = sorted(
                (p for p in self.directory.glob(f"{_PREFIX}*.jsonl")
                 if p.name != ACTIVE_NAME),
                key=lambda p: p.stat().st_mtime,
            )
            for old in rotated[:-self.max_files]:
                old.unlink(missing_ok=True)

    def files(self) -> list[Path]:
        """按时间顺序返回所有待回放文件（active 放最后，保证 FIFO 语义）。"""
        if not self.directory.is_dir():
            return []
        rotated = sorted(
            (p for p in self.directory.glob(f"{_PREFIX}*.jsonl")
             if p.name != ACTIVE_NAME),
            key=lambda p: (p.stat().st_mtime, p.name),
        )
        if self.active.is_file():
            rotated.append(self.active)
        return rotated

    def has_pending(self) -> bool:
        return self._has_pending

    def read_all(self) -> Iterator[dict[str, Any]]:
        """流式逐行读取；单行损坏只跳过该行并记录，不阻断其余事件回放。"""
        if not self._has_pending:
            return
        for path in self.files():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                        except (ValueError, TypeError):
                            with self._lock:
                                self._error = f"磁盘缓冲存在损坏行，已跳过: {path.name}"
                            continue
                        if isinstance(item, dict) and item.get("sql"):
                            yield item
            except OSError as exc:
                with self._lock:
                    self._error = f"磁盘缓冲读取失败: {exc!r}"

    def pending(self) -> int:
        if not self._has_pending:
            return 0
        total = 0
        for path in self.files():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    total += sum(1 for line in fh if line.strip())
            except OSError:
                continue
        return total

    def clear(self) -> None:
        """仅在全部载荷已确认补写成功后调用。"""
        with self._lock:
            for path in self.files():
                path.unlink(missing_ok=True)
            self._has_pending = False
            if not self._error.startswith("磁盘缓冲存在损坏行"):
                self._error = ""
