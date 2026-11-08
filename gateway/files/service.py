"""通用二进制文件传输服务（仅标准库）。

设计要点：
- 所有路径 resolve() 后必须位于 MCP_FILE_ROOT 内，并复用 git_guard.check_write_path
  拒绝 .git 与系统凭据路径；额外拒绝 .env / 私钥 / token 文件。
- 分片先写 .part，完成时流式计算 SHA-256，校验通过才 os.replace 原子落盘；
  任何失败都删除 .part 并保留原文件，绝不留下半截文件覆盖目标。
- 下载链接使用 HMAC-SHA256 + 短时效，支持 HTTP Range 流式读取；
  URL 中不含任何凭据，签名材料与分片内容一律不写审计。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from gateway.git_guard import check_write_path

MAX_CHUNK = min(8 * 1024 * 1024,
                max(4096, int(os.environ.get("MCP_FILE_CHUNK_BYTES", str(1024 * 1024)))))
TICKET_TTL = 3600
DEFAULT_LINK_TTL = 600
_TICKET_DIR_NAME = ".mcp-uploads"
_LOCK = threading.RLock()
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# 凭据 / 私钥类文件名：即使位于受管根目录内也一律拒绝读写，避免凭据被外带。
_SENSITIVE_NAMES = frozenset({
    ".env", ".git-credentials", ".netrc", ".npmrc", ".pypirc", ".htpasswd",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "authorized_keys",
    "known_hosts", ".admin_password", ".mcp_token", "credentials.json",
    "service-account.json", "shadow", "sudoers",
})
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".jks", ".keystore")
_SENSITIVE_PARTS = frozenset({".git", ".ssh", ".aws", ".gnupg", ".kube"})


class FileTransferError(Exception):
    """路径越权、参数非法或校验失败。"""


def file_root() -> Path:
    raw = os.environ.get("MCP_FILE_ROOT", "/data/files").strip() or "/data/files"
    root = Path(os.path.expandvars(raw)).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ticket_dir() -> Path:
    path = file_root() / _TICKET_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _check_name_parts(parts: tuple[str, ...]) -> None:
    for part in parts:
        lowered = part.lower()
        if lowered in _SENSITIVE_PARTS:
            raise FileTransferError(f"禁止访问凭据目录: {part}")
        if lowered in _SENSITIVE_NAMES or lowered.startswith(".env"):
            raise FileTransferError(f"禁止访问凭据/密钥文件: {part}")
        if lowered.endswith(_SENSITIVE_SUFFIXES):
            raise FileTransferError(f"禁止访问私钥文件: {part}")
        if part.endswith(".part"):
            raise FileTransferError("禁止直接访问未完成的分片文件")


def _resolve_target(path: str) -> Path:
    """解析为绝对路径，并确保仍在 MCP_FILE_ROOT 内（绝对路径不授予根外权限）。"""
    text = (path or "").strip()
    if not text:
        raise FileTransferError("路径不能为空")
    if "\x00" in text:
        raise FileTransferError("路径含非法字符")
    root = file_root()
    candidate = Path(os.path.expandvars(text)).expanduser()
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if target != root and root not in target.parents:
        raise FileTransferError(f"路径越出受管根目录 {root}: {target}")
    return target


def _check_target(target: Path, *, allow_root: bool = False) -> None:
    root = file_root()
    if target == root:
        if not allow_root:
            raise FileTransferError("路径必须指向具体文件，而不是根目录")
        return
    relative = target.relative_to(root)
    _check_name_parts(relative.parts)
    decision = check_write_path(str(target))
    if not decision.allowed:
        raise FileTransferError(decision.reason)
    if _TICKET_DIR_NAME in relative.parts:
        raise FileTransferError("禁止访问上传暂存目录")


def resolve_path(path: str, *, for_write: bool = False) -> Path:
    """把用户路径解析为受管根目录内的文件绝对路径，并做敏感路径硬禁。

    旧版文本/图片工具与新版二进制上传下载工具共用此入口，路径规则完全一致：
    越出 MCP_FILE_ROOT、.git、凭据/私钥、上传暂存目录一律拒绝。
    """
    target = _resolve_target(path)
    _check_target(target)
    if for_write and target.is_dir():
        raise FileTransferError("目标已存在且是目录")
    return target


def resolve_dir(path: str = "") -> Path:
    """解析目录（允许受管根目录本身），供 list_directory/search_files/grep_content 复用。"""
    text = (path or "").strip()
    target = file_root() if not text or text in (".", "./") else _resolve_target(text)
    _check_target(target, allow_root=True)
    if not target.is_dir():
        raise FileTransferError(f"目录不存在或不是目录: {text or target}")
    return target


def is_reserved_name(name: str) -> bool:
    """列出/递归搜索时应跳过的内部暂存目录名（票据与未完成分片）。"""
    return name == _TICKET_DIR_NAME


def relative_key(path: str) -> str:
    return resolve_path(path).relative_to(file_root()).as_posix()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# file_checksum / file_info / download_file 常对同一文件连续取摘要；按
# (路径, 大小, mtime_ns) 缓存，避免每次调用都全量重算大文件，且内容一变即失效。
_CHECKSUM_CACHE: dict[tuple[str, int, int], str] = {}
_CHECKSUM_CACHE_MAX = 256


def _checksum_key(target: Path) -> tuple[str, int, int]:
    stat = target.stat()
    return (str(target), stat.st_size, stat.st_mtime_ns)


def cached_sha256(target: Path) -> str:
    key = _checksum_key(target)
    with _LOCK:
        hit = _CHECKSUM_CACHE.get(key)
    if hit:
        return hit
    digest = sha256_file(target)
    store_checksum(target, digest)
    return digest


def store_checksum(target: Path, digest: str) -> None:
    """上传落盘后直接复用已算出的摘要，不再重复读盘。"""
    with _LOCK:
        if len(_CHECKSUM_CACHE) >= _CHECKSUM_CACHE_MAX:
            _CHECKSUM_CACHE.clear()
        _CHECKSUM_CACHE[_checksum_key(target)] = digest


def _signing_secret() -> bytes:
    raw = (os.environ.get("MCP_FILE_SIGNING_SECRET", "").strip()
           or os.environ.get("MCP_TOKEN", "").strip())
    # MCP_TOKEN 由网关 entrypoint 保证存在；两个进程都读同一值，链接跨进程可用。
    return raw.encode("utf-8") if raw else b"mcp-gateway-file-signing-fallback"


def _token_for(key: str, expiry: int) -> str:
    mac = hmac.new(_signing_secret(), f"{key}:{expiry}".encode("utf-8"),
                   hashlib.sha256).hexdigest()[:32]
    return f"{expiry}.{mac}"


def verify_download_token(key: str, token: str) -> bool:
    """校验短时效签名；失败一律视为未授权，不泄露原因细节。"""
    try:
        expiry_text, mac = (token or "").split(".", 1)
        if not expiry_text.isdigit():
            return False
        expiry = int(expiry_text)
        if expiry < int(time.time()) or expiry > int(time.time()) + 7200:
            return False
        return hmac.compare_digest(mac, _token_for(key, expiry).split(".", 1)[1])
    except (ValueError, TypeError):
        return False


def signed_download_url(path: str, *, base_url: str = "",
                        ttl_seconds: int = DEFAULT_LINK_TTL) -> str:
    import urllib.parse
    key = relative_key(path)
    ttl = min(3600, max(30, int(ttl_seconds)))
    expiry = int(time.time()) + ttl
    base = (base_url or os.environ.get("MCP_PUBLIC_BASE_URL", "")).strip().rstrip("/")
    if not base:
        base = f"http://127.0.0.1:{os.environ.get('MCP_PORT', '8765')}"
    query = urllib.parse.urlencode({"path": key, "token": _token_for(key, expiry)})
    return f"{base}/files/download?{query}"


def _info_dict(target: Path, *, base_url: str = "",
               ttl_seconds: int = DEFAULT_LINK_TTL) -> dict[str, Any]:
    stat = target.stat()
    key = target.relative_to(file_root()).as_posix()
    # 未配置固定公网入口时如实标注 local：链接可用，但不假装是公网 HTTPS 地址。
    # base_url 可能只是本次请求的 Host 回落值，只有显式配置才算固定公网入口。
    public = bool(os.environ.get("MCP_PUBLIC_BASE_URL", "").strip())
    return {
        "path": key,
        "size": stat.st_size,
        "sha256": cached_sha256(target),
        "mime": mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        "mtime": stat.st_mtime,
        "link_scope": "public" if public else "local",
        "download_url": signed_download_url(key, base_url=base_url,
                                            ttl_seconds=ttl_seconds),
    }


def file_info(path: str, *, base_url: str = "",
              ttl_seconds: int = DEFAULT_LINK_TTL) -> dict[str, Any]:
    target = resolve_path(path)
    if not target.is_file():
        raise FileTransferError(f"文件不存在或不是普通文件: {path}")
    return _info_dict(target, base_url=base_url, ttl_seconds=ttl_seconds)


def download_chunk(path: str, offset: int = 0, length: int = 0,
                   *, base_url: str = "") -> dict[str, Any]:
    """返回一个 base64 分片与可直接打开的签名下载链接。"""
    target = resolve_path(path)
    if not target.is_file():
        raise FileTransferError(f"文件不存在: {path}")
    size = target.stat().st_size
    start = max(0, int(offset))
    if start > size:
        start = size
    want = MAX_CHUNK if length is None or int(length) <= 0 else min(int(length), MAX_CHUNK)
    with target.open("rb") as fh:
        fh.seek(start)
        raw = fh.read(want)
    info = _info_dict(target, base_url=base_url)
    return {
        **info,
        "offset": start,
        "length": len(raw),
        "eof": start + len(raw) >= size,
        "content_base64": base64.b64encode(raw).decode("ascii"),
    }


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """解析单段 Range；返回 (start, end_inclusive)，None 表示无 Range，非法抛错。"""
    if not header:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match:
        raise FileTransferError("不支持的 Range 语法")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise FileTransferError("空 Range")
    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    else:
        suffix = int(end_text)
        start = max(0, size - suffix)
        end = size - 1
    if start >= size or end < start:
        raise FileTransferError("Range 超出文件大小")
    return start, min(end, size - 1)


def iter_file_range(path: Path, start: int, end: int,
                    chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as fh:
        fh.seek(start)
        while remaining > 0:
            block = fh.read(min(chunk_size, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


def _ticket_path(ticket: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", ticket or ""):
        raise FileTransferError("非法上传票据")
    return _ticket_dir() / f"{ticket}.json"


def _load_ticket(ticket: str) -> tuple[dict[str, Any], Path]:
    meta = _ticket_path(ticket)
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FileTransferError(f"上传票据不存在或已过期: {ticket}") from exc
    if float(data.get("created_at", 0)) + TICKET_TTL < time.time():
        _cleanup_ticket(ticket)
        raise FileTransferError("上传票据已过期，请重新发起上传")
    return data, meta


def _save_ticket(ticket: str, data: dict[str, Any]) -> None:
    tmp = _ticket_dir() / f".{ticket}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(_ticket_path(ticket))


def _cleanup_ticket(ticket: str) -> None:
    try:
        _ticket_path(ticket).unlink(missing_ok=True)
        (_ticket_dir() / f"{ticket}.part").unlink(missing_ok=True)
    except OSError:
        pass


def upload_begin(path: str, total_size: int = 0, sha256_expected: str = "",
                 overwrite: bool = False) -> dict[str, Any]:
    """开始一次分片上传；返回 ticket，内容先写 .part，目标文件此时不受影响。"""
    target = resolve_path(path, for_write=True)
    if target.exists() and not overwrite:
        raise FileTransferError("目标文件已存在；如需覆盖请显式传 overwrite=true")
    expected = (sha256_expected or "").strip().lower()
    if expected and not _HEX64_RE.match(expected):
        raise FileTransferError("sha256 必须是 64 位十六进制摘要")
    size = int(total_size or 0)
    if size < 0:
        raise FileTransferError("total_size 不能为负")
    ticket = secrets.token_hex(12)
    data = {
        "path": target.relative_to(file_root()).as_posix(),
        "total_size": size,
        "sha256_expected": expected,
        "overwrite": bool(overwrite),
        "offset": 0,
        "created_at": time.time(),
    }
    with _LOCK:
        _save_ticket(ticket, data)
        part = _ticket_dir() / f"{ticket}.part"
        part.write_bytes(b"")
        os.chmod(part, 0o600)
    return {"ticket": ticket, "path": data["path"], "offset": 0,
            "chunk_limit": MAX_CHUNK, "sha256_expected": expected}


def upload_chunk(ticket: str, offset: int, chunk_base64: str) -> dict[str, Any]:
    """按 offset 顺序追加一个 base64 分片；票据元数据落盘，网关重启后可续传。"""
    try:
        raw = base64.b64decode(chunk_base64 or "", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FileTransferError("分片不是合法的 base64") from exc
    if not raw:
        raise FileTransferError("分片内容为空")
    if len(raw) > MAX_CHUNK:
        raise FileTransferError(f"单个分片超过上限 {MAX_CHUNK} 字节")
    with _LOCK:
        data, meta = _load_ticket(ticket)
        expected_offset = int(data["offset"])
        if int(offset) != expected_offset:
            raise FileTransferError(f"分片 offset 不连续：期望 {expected_offset}，收到 {offset}")
        if data["total_size"] and expected_offset + len(raw) > int(data["total_size"]):
            raise FileTransferError("累计分片超过声明的 total_size")
        part = _ticket_dir() / f"{ticket}.part"
        with part.open("r+b") as fh:
            fh.seek(expected_offset)
            fh.write(raw)
            fh.flush()
        new_offset = expected_offset + len(raw)
        data["offset"] = new_offset
        _save_ticket(ticket, data)
    return {"ticket": ticket, "offset": new_offset, "received": len(raw),
            "path": data["path"]}


def upload_finish(ticket: str, *, base_url: str = "") -> dict[str, Any]:
    """校验大小与 SHA-256 后原子落盘；失败删除 .part，原文件保持不变。"""
    with _LOCK:
        data, _meta = _load_ticket(ticket)
        part = _ticket_dir() / f"{ticket}.part"
        if not part.is_file():
            _cleanup_ticket(ticket)
            raise FileTransferError("上传分片文件丢失，请重新上传")
        target = resolve_path(data["path"], for_write=True)
        try:
            size = part.stat().st_size
            if data["total_size"] and size != int(data["total_size"]):
                raise FileTransferError(f"大小不符：声明 {data['total_size']}，实际 {size}")
            digest = sha256_file(part)
            if data["sha256_expected"] and digest != data["sha256_expected"]:
                raise FileTransferError(f"SHA-256 不符：期望 {data['sha256_expected']}，实际 {digest}")
            if target.exists() and not data["overwrite"]:
                raise FileTransferError("目标文件在上传期间被创建；需显式 overwrite=true 重试")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(part, target)
            os.chmod(target, 0o600)
            # 摘要已在校验阶段算出，落盘后直接复用，避免 _info_dict 再读一遍大文件。
            store_checksum(target, digest)
        except Exception:
            _cleanup_ticket(ticket)
            raise
        _cleanup_ticket(ticket)
        return _info_dict(target, base_url=base_url)


def upload_abort(ticket: str) -> dict[str, Any]:
    """主动放弃上传：删除 .part 与票据，原文件不受影响。"""
    with _LOCK:
        _cleanup_ticket(ticket)
    return {"ticket": ticket, "aborted": True}


def upload_state(ticket: str) -> dict[str, Any]:
    """查询票据当前已接收字节数（不含任何分片内容），供空文件/续传使用。"""
    with _LOCK:
        data, _meta = _load_ticket(ticket)
    return {"ticket": ticket, "offset": int(data["offset"]),
            "received": 0, "path": data["path"]}


def record_transfer(tool_name: str, path: str, *, direction: str,
                    status: str, size: int = 0, sha256: str = "",
                    error_message: str = "", duration_ms: int = 0) -> None:
    """写完整调用记录；只记路径/大小/摘要/终态，绝不记内容与签名。"""
    try:
        from gateway.audit.storage import get_storage
        storage = get_storage()
        storage.add_file_transfer({
            "tool_name": tool_name,
            "path": path or "",
            "direction": direction,
            "size": int(size or 0),
            "sha256": sha256 or "",
            "status": status,
            "error_message": (error_message or "")[:1000],
            "duration_ms": int(duration_ms or 0),
            "client_id": "",
        })
        storage.add_event(
            "file_transfer", status=status, tool_name=tool_name, source="file",
            detail={"path": path or "", "direction": direction,
                    "size": int(size or 0), "sha256": sha256 or ""},
        )
    except Exception:  # noqa: BLE001 - 审计故障不得阻断真实文件传输
        pass


def cleanup_stale_tickets() -> int:
    """启动时清理过期票据与 .part，避免持久卷堆积。"""
    removed = 0
    try:
        for meta in _ticket_dir().glob("*.json"):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                expired = float(data.get("created_at", 0)) + TICKET_TTL < time.time()
            except (OSError, ValueError):
                expired = True
            if expired:
                _cleanup_ticket(meta.stem)
                removed += 1
    except OSError:
        pass
    return removed
