"""JSON-lines 帧协议、操作白名单、HMAC 认证与错误码（标准库实现）。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

MAX_LINE = 1024 * 1024
DEFAULT_SOCKET = os.environ.get(
    "MCP_HOST_AGENT_SOCKET", "/var/run/mcp-host-agent.sock")

ALLOWED_OPS = frozenset({
    "ping", "system_info", "run", "spawn", "kill", "git",
    "list_dir", "docker_ps", "processes",
})

ERROR_CODES = {
    "AUTH_FAILED": "HMAC 签名校验失败或请求已过期",
    "BAD_OP": "操作不在白名单内",
    "BAD_ROOT": "路径不在授权宿主目录内",
    "TIMEOUT": "宿主操作超时",
    "CANCELLED": "宿主操作被取消",
    "EXEC_FAILED": "宿主操作执行失败",
    "BAD_FRAME": "帧格式非法",
}

# 请求时间窗：防止重放旧帧；与 HMAC 密钥同为部署时共享。
MAX_SKEW_SECONDS = 60


def sign_payload(secret: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hmac.new(str(secret).encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_payload(secret: str, payload: dict[str, Any], signature: str) -> bool:
    expected = sign_payload(secret, payload)
    if not hmac.compare_digest(expected, str(signature or "")):
        return False
    try:
        return abs(time.time() - float(payload.get("ts", 0))) <= MAX_SKEW_SECONDS
    except (TypeError, ValueError):
        return False


def encode_frame(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(raw) > MAX_LINE:
        raise ValueError("帧超过 1MB 上限")
    return raw + b"\n"


def decode_frame(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_LINE:
        raise ValueError("帧超过 1MB 上限")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("帧必须是 JSON 对象")
    return value


def error(code: str, message: str = "") -> dict[str, Any]:
    return {"ok": False, "code": code,
            "error": message or ERROR_CODES.get(code, code)}
