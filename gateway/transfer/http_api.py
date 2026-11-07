"""认证分块上传 / HTTP Range 下载 / 短期 HMAC 签名链接。

论文下载与通用文件传输共用 ``gateway/transfer/core.py`` 的流式原语与校验策略：
这里只负责 HTTP 边界、路径隔离与签名，不重复实现完整性校验逻辑。
所有传输事件写入 ``file_transfers``，状态与下载任务一样区分「创建」与「校验成功」。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

from starlette.responses import JSONResponse, StreamingResponse

CHUNK_SIZE = 65536
DEFAULT_MAX_MB = 256


def secret() -> str:
    return os.environ.get("MCP_TRANSFER_SECRET", "") or os.environ.get(
        "MCP_TOKEN", "") or "insecure-empty-secret"


def max_bytes() -> int:
    return max(1, min(int(os.environ.get("TRANSFER_MAX_MB", str(DEFAULT_MAX_MB))),
                      4096)) * 1024 * 1024


def _canonical(path: str) -> str:
    """签名绑定客户端提交的原始路径串；路径隔离由 safe_path 单独保证。"""
    return str(Path(str(path)).as_posix())


def sign_path(path: str, ttl: int = 600) -> tuple[int, str]:
    expiry = int(time.time()) + max(30, min(ttl, 3600))
    payload = f"{_canonical(path)}:{expiry}".encode()
    return expiry, hmac.new(secret().encode(), payload, hashlib.sha256).hexdigest()[:32]


def verify_path(path: str, expiry: str, signature: str) -> bool:
    try:
        exp = int(expiry)
    except (TypeError, ValueError):
        return False
    if exp < time.time() or exp > time.time() + 3600:
        return False
    expected = hmac.new(secret().encode(),
                        f"{_canonical(path)}:{exp}".encode(),
                        hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, str(signature or ""))


def signed_url(base: str, path: str, ttl: int = 600) -> str:
    expiry, signature = sign_path(path, ttl)
    return (f"{base.rstrip('/')}/files/download?"
            + urllib.parse.urlencode({"path": path, "exp": expiry, "sig": signature}))


def safe_path(root: str | Path, raw: str) -> Path | None:
    if not raw:
        return None
    base = Path(root).resolve()
    try:
        given = Path(raw)
        candidate = (given if given.is_absolute() else base / given).resolve()
    except OSError:
        return None
    if candidate != base and base not in candidate.parents:
        return None
    return candidate


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """只支持单段 Range；非法范围返回 None（由调用方返回 416）。"""
    if not header or not header.lower().startswith("bytes="):
        return 0, size - 1
    try:
        spec = header[6:].split(",", 1)[0].strip()
        start_text, end_text = spec.split("-", 1)
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        else:
            suffix = int(end_text)
            start = max(0, size - suffix)
            end = size - 1
    except (ValueError, TypeError):
        return None
    if start < 0 or end < start or start >= size:
        return None
    return start, min(end, size - 1)


def _emit(values: dict[str, Any]) -> None:
    try:
        from gateway.audit.middleware import record_transfer_event
        if record_transfer_event(values):
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        from gateway.audit.storage import get_storage
        store = get_storage()
        keys = list(values)
        store._execute(
            f"INSERT INTO file_transfers ({','.join(keys)}) "
            f"VALUES ({','.join('?' for _ in keys)})",
            [values[k] for k in keys])
    except Exception:  # noqa: BLE001
        pass


def _request_id(request) -> str:
    try:
        from gateway.audit.events import current_trace
        trace = current_trace()
        return trace.request_id if trace else ""
    except Exception:  # noqa: BLE001
        return ""


async def upload_endpoint(request, root: str | Path):
    """分块上传：X-File-Path / X-Upload-Offset / X-File-Sha256。"""
    base = Path(root).resolve()
    raw = request.headers.get("x-file-path", "")
    target = safe_path(base, raw)
    if target is None:
        return JSONResponse({"ok": False, "error": "路径不在授权传输目录内"},
                            status_code=400)
    expected = request.headers.get("x-file-sha256", "").strip().lower()
    try:
        offset = max(0, int(request.headers.get("x-upload-offset", "0") or 0))
    except ValueError:
        return JSONResponse({"ok": False, "error": "X-Upload-Offset 非法"},
                            status_code=400)
    part = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    if offset:
        if not part.is_file() or part.stat().st_size != offset:
            return JSONResponse(
                {"ok": False, "error": "断点偏移与临时分片大小不一致；"
                                       "请从 0 重新上传或对齐 X-Upload-Offset"},
                status_code=409)
        with part.open("rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
                digest.update(chunk)
    else:
        part.unlink(missing_ok=True)
    req = _request_id(request)
    _emit({"request_id": req, "direction": "upload", "path": str(target),
           "status": "created", "bytes": offset, "total_bytes": 0})
    written = offset
    try:
        with part.open("ab" if offset else "wb") as fh:
            async for chunk in request.stream():
                if not chunk:
                    continue
                if written + len(chunk) > max_bytes():
                    part.unlink(missing_ok=True)
                    _emit({"request_id": req, "direction": "upload",
                           "path": str(target), "status": "failed",
                           "bytes": written, "detail": "超过 TRANSFER_MAX_MB 上限"})
                    return JSONResponse(
                        {"ok": False, "error": "超过 TRANSFER_MAX_MB 上限，临时分片已删除"},
                        status_code=413)
                fh.write(chunk)
                digest.update(chunk)
                written += len(chunk)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        _emit({"request_id": req, "direction": "upload", "path": str(target),
               "status": "failed", "bytes": written,
               "detail": repr(exc)[:300]})
        return JSONResponse({"ok": False, "error": f"写入失败: {exc!r}"},
                            status_code=500)
    actual = digest.hexdigest()
    if expected and not hmac.compare_digest(actual, expected):
        part.unlink(missing_ok=True)
        _emit({"request_id": req, "direction": "upload", "path": str(target),
               "status": "failed", "bytes": written,
               "detail": "SHA-256 与期望值不一致"})
        return JSONResponse({"ok": False, "error": "SHA-256 校验失败，临时文件已删除",
                             "sha256": actual}, status_code=422)
    os.replace(part, target)
    _emit({"request_id": req, "direction": "upload", "path": str(target),
           "status": "success", "bytes": written, "total_bytes": written,
           "sha256": actual, "detail": "原子改名 + fsync"})
    return JSONResponse({"ok": True, "status": "success", "path": str(target),
                         "bytes": written, "sha256": actual,
                         "note": "created 仅表示任务创建；success 表示最终校验通过"})


def iter_file(path: Path, start: int, end: int) -> Iterator[bytes]:
    """共享的按 Range 流式读取原语（论文下载与通用传输共用）。"""
    remaining = end - start + 1
    with path.open("rb") as fh:
        fh.seek(start)
        while remaining > 0:
            chunk = fh.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


async def download_endpoint(request, root: str | Path):
    """认证下载：Bearer（由网关中间件保证）或短期 HMAC 签名；支持 Range。"""
    base = Path(root).resolve()
    raw = request.query_params.get("path", "")
    target = safe_path(base, raw)
    if target is None:
        return JSONResponse({"ok": False, "error": "路径不在授权传输目录内"},
                            status_code=400)
    expiry = request.query_params.get("exp", "")
    signature = request.query_params.get("sig", "")
    if not verify_path(raw, expiry, signature) and not request.headers.get(
            "authorization", "").lower().startswith("bearer "):
        return JSONResponse({"ok": False, "error": "缺少有效签名或 Bearer 凭据"},
                            status_code=401)
    if not target.is_file():
        return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
    size = target.stat().st_size
    span = parse_range(request.headers.get("range", ""), size)
    if span is None:
        return JSONResponse({"ok": False, "error": "Range 不合法"},
                            status_code=416)
    start, end = span
    partial = request.headers.get("range", "").startswith("bytes=")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Disposition": f'attachment; filename="{target.name}"',
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    req = _request_id(request)
    _emit({"request_id": req, "direction": "download", "path": str(target),
           "status": "success", "bytes": end - start + 1,
           "total_bytes": size,
           "detail": f"range={start}-{end}" if partial else "full"})
    return StreamingResponse(iter_file(target, start, end),
                             status_code=206 if partial else 200,
                             media_type="application/octet-stream",
                             headers=headers)


def transfer_status(limit: int = 50) -> dict[str, Any]:
    try:
        from gateway.audit.storage import get_storage
        rows = get_storage()._query(
            "SELECT * FROM file_transfers ORDER BY id DESC LIMIT ?",
            [max(1, min(limit, 500))])
    except Exception:  # noqa: BLE001
        return {"rows": [], "total": 0, "counts": {}}
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"rows": rows, "total": len(rows), "counts": counts,
            "note": "created 仅表示任务创建；success 表示最终校验/传输成功"}
