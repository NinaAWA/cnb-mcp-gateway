"""网关侧 Unix Socket 客户端：超时、HMAC、明确错误与结果脱敏。"""
from __future__ import annotations

import json
import os
import socket
import time
import uuid
from typing import Any

from .protocol import DEFAULT_SOCKET, encode_frame, error, sign_payload

try:
    from gateway.audit.redaction import redact_structure
except Exception:  # noqa: BLE001 - 宿主 agent 独立部署时可能没有审计包
    def redact_structure(value):
        return value


class HostAgentError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code


class HostAgentClient:
    def __init__(self, socket_path: str = "", secret: str = "",
                 timeout: float = 10.0):
        self.socket_path = socket_path or os.environ.get(
            "MCP_HOST_AGENT_SOCKET", DEFAULT_SOCKET)
        self.secret = secret or os.environ.get("MCP_HOST_AGENT_TOKEN", "")
        self.timeout = timeout

    def available(self) -> tuple[bool, str]:
        if not self.secret:
            return False, "MCP_HOST_AGENT_TOKEN 未配置"
        if not os.path.exists(self.socket_path):
            return False, f"Host Agent socket 不存在: {self.socket_path}"
        return True, ""

    def call(self, op: str, args: dict[str, Any] | None = None,
             request_id: str = "") -> dict[str, Any]:
        ok, reason = self.available()
        if not ok:
            raise HostAgentError("EXEC_FAILED", reason)
        payload = {
            "op": op, "args": args or {}, "ts": time.time(),
            "nonce": uuid.uuid4().hex, "request_id": request_id,
        }
        frame = dict(payload)
        frame["sig"] = sign_payload(self.secret, payload)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
            sock.sendall(encode_frame(frame))
            buf = bytearray()
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break
                if len(buf) > 1024 * 1024:
                    raise HostAgentError("BAD_FRAME", "响应超过 1MB")
            if not buf:
                raise HostAgentError("EXEC_FAILED", "Host Agent 未返回数据")
            response = json.loads(bytes(buf).split(b"\n", 1)[0].decode("utf-8"))
        except (socket.timeout, TimeoutError) as exc:
            raise HostAgentError("TIMEOUT", str(exc)) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise HostAgentError("EXEC_FAILED", str(exc)[:200]) from exc
        finally:
            sock.close()
        if not isinstance(response, dict):
            raise HostAgentError("BAD_FRAME", "响应不是 JSON 对象")
        if not response.get("ok"):
            raise HostAgentError(response.get("code", "EXEC_FAILED"),
                                 response.get("error", ""))
        return redact_structure(response.get("data", {}))

    def ping(self) -> dict[str, Any]:
        return self.call("ping")
