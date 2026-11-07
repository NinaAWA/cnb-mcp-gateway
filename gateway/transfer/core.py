"""共享流式分块原语（论文下载与通用文件传输共用底层）。

职责：增量 SHA-256、临时文件 + fsync + 原子改名、Range 续传、取消、
大小上限、最终 URL / Content-Type / PDF 头尾完整性校验、同哈希去重。
校验策略分成 PaperSourcePolicy（合法 OA 主机白名单）与
GenericSourcePolicy（仅 SSRF 防护），避免把论文下载放宽成任意 URL。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from gateway.net.client import SafeHttpClient

# 合法开放获取来源；刻意不包含任何盗版镜像 / 付费墙绕过站点。
OA_ALLOWED_HOSTS = frozenset({
    "arxiv.org", "export.arxiv.org", "www.arxiv.org",
    "www.ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
    "europepmc.org", "www.ebi.ac.uk", "api.openalex.org",
    "api.unpaywall.org", "www.nature.com", "nature.com",
    "journals.plos.org", "www.frontiersin.org", "www.mdpi.com",
    "link.springer.com", "onlinelibrary.wiley.com", "academic.oup.com",
    "www.sciencedirect.com", "openreview.net", "proceedings.mlr.press",
    "papers.nips.cc", "openaccess.thecvf.com", "www.aaai.org",
    "dl.acm.org", "ieeexplore.ieee.org", "www.tandfonline.com",
})
FORBIDDEN_HOST_PARTS = ("sci-hub", "libgen", "pirate", "booksc", "z-lib")


class PolicyError(Exception):
    """来源不符合当前校验策略。"""


@dataclass
class PaperSourcePolicy:
    """论文下载：必须 https、主机在合法 OA 白名单内、禁止盗版镜像。"""

    extra_hosts: frozenset[str] = frozenset()
    max_bytes: int = 64 * 1024 * 1024

    def validate(self, url: str) -> str:
        parts = urllib.parse.urlsplit(str(url or ""))
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" and host not in self.extra_hosts:
            raise PolicyError("论文下载仅允许 https 来源")
        if any(bad in host for bad in FORBIDDEN_HOST_PARTS):
            raise PolicyError("拒绝盗版镜像 / 付费墙绕过来源")
        if host not in OA_ALLOWED_HOSTS and host not in self.extra_hosts:
            raise PolicyError(f"PDF 主机不在合法开放获取白名单: {host}")
        return host


@dataclass
class GenericSourcePolicy:
    """通用传输：只做 SSRF 基础校验，由 SafeHttpClient 完成。"""

    max_bytes: int = 256 * 1024 * 1024

    def validate(self, url: str) -> str:
        parts = urllib.parse.urlsplit(str(url or ""))
        if parts.scheme not in ("http", "https"):
            raise PolicyError("仅允许 http/https")
        return (parts.hostname or "").lower()


@dataclass
class TransferResult:
    status: str = "created"                 # created|downloading|verifying|success|failed|duplicate|cancelled
    path: str = ""
    bytes: int = 0
    sha256: str = ""
    final_url: str = ""
    content_type: str = ""
    reason: str = ""
    resumed: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "path": self.path, "bytes": self.bytes,
            "sha256": self.sha256, "final_url": self.final_url,
            "content_type": self.content_type, "reason": self.reason,
            "resumed": self.resumed, "detail": self.detail,
        }


def verify_pdf_file(path: str | Path, *, content_type: str = "",
                    max_bytes: int = 0) -> tuple[bool, str]:
    """PDF 完整性：magic header、%%EOF、Content-Type、大小。"""
    file = Path(path)
    if not file.is_file():
        return False, "文件不存在"
    size = file.stat().st_size
    if max_bytes and size > max_bytes:
        return False, f"超过体积上限 {max_bytes} 字节"
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype.startswith("text/html"):
        return False, "响应是 HTML（常见于登录页/付费墙），不是 PDF"
    with file.open("rb") as fh:
        head = fh.read(1024)
        if not head.startswith(b"%PDF-"):
            return False, "缺少 %PDF- 文件头（可能为 HTML 假 PDF）"
        if size < 100:
            return False, "文件过小，不是有效 PDF"
        fh.seek(max(0, size - 2048))
        tail = fh.read()
    if b"%%EOF" not in tail:
        return False, "缺少 %%EOF 尾部标记，PDF 可能未下载完整"
    return True, ""


def fetch_to_file(client: SafeHttpClient, url: str, dest: str | Path,
                  *, policy: PaperSourcePolicy | GenericSourcePolicy,
                  cancel: Callable[[], bool] | None = None,
                  resume: bool = True, allow_private: bool = False,
                  expected_sha256: str = "", verify_pdf: bool = True,
                  progress: Callable[[dict[str, Any]], None] | None = None,
                  ) -> TransferResult:
    """流式下载并严格校验；只有最终校验成功才返回 status=success。"""
    result = TransferResult(status="created", path=str(dest))
    try:
        policy.validate(url)
    except PolicyError as exc:
        result.status, result.reason = "failed", str(exc)
        return result
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    offset = 0
    if resume and part.is_file() and not target.is_file():
        offset = part.stat().st_size
        if offset:
            with part.open("rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    digest.update(chunk)
            result.resumed = True
    mode = "ab" if offset else "wb"
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    result.status = "downloading"
    written = offset
    try:
        with part.open(mode) as fh:
            for chunk in client.stream(
                    url, headers=headers or None,
                    max_bytes=policy.max_bytes,
                    allow_any_host=True, allow_private=allow_private):
                if cancel and cancel():
                    result.status, result.reason = "cancelled", "调用方请求取消"
                    return result
                if written + len(chunk) > policy.max_bytes:
                    raise OSError(f"超过体积上限 {policy.max_bytes} 字节")
                fh.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                if progress:
                    progress({"bytes": written, "total": 0,
                              "status": "downloading", "path": str(target)})
            fh.flush()
            os.fsync(fh.fileno())
    except Exception as exc:  # noqa: BLE001 - 保留 .part 供下次断点续传
        result.status = "failed"
        result.reason = f"下载中断: {exc!r}"
        result.bytes = written
        result.final_url = getattr(client, "last_url", "")
        return result
    result.bytes = written
    result.sha256 = digest.hexdigest()
    result.final_url = getattr(client, "last_url", "") or url
    result.content_type = getattr(client, "last_content_type", "")
    result.status = "verifying"
    if progress:
        progress({"bytes": written, "status": "verifying", "path": str(target)})
    try:
        policy.validate(result.final_url)
    except PolicyError as exc:
        part.unlink(missing_ok=True)
        result.status, result.reason = "failed", f"最终跳转地址不合法: {exc}"
        return result
    if verify_pdf:
        ok, reason = verify_pdf_file(part, content_type=result.content_type,
                                     max_bytes=policy.max_bytes)
    else:
        ok, reason = True, ""
        if result.content_type.split(";", 1)[0].strip().lower().startswith("text/html"):
            ok, reason = False, "响应是 HTML，不是目标文件"
        if not result.bytes:
            ok, reason = False, "空文件"
    if not ok:
        # 内容不合法不留续传残片，避免下次把 HTML 拼进 PDF
        part.unlink(missing_ok=True)
        result.status, result.reason = "failed", reason
        return result
    if expected_sha256 and result.sha256 != expected_sha256:
        part.unlink(missing_ok=True)
        result.status, result.reason = "failed", "SHA-256 与期望值不一致"
        return result
    if target.is_file():
        existing = hashlib.sha256(target.read_bytes()).hexdigest()
        if existing == result.sha256:
            part.unlink(missing_ok=True)
            result.status, result.path = "duplicate", str(target)
            return result
    os.replace(part, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    result.status, result.path = "success", str(target)
    result.detail = {"verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "atomic_rename": True, "fsync": True}
    if progress:
        progress({"bytes": written, "status": "success", "path": str(target),
                  "sha256": result.sha256})
    return result


def make_cancel_event() -> tuple[threading.Event, Callable[[], bool]]:
    event = threading.Event()
    return event, event.is_set
