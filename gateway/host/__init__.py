"""Host Agent：Unix Socket + HMAC，无 TCP / 无公网管理端口。"""
from .client import HostAgentClient, HostAgentError
from .protocol import ALLOWED_OPS, sign_payload, verify_payload

__all__ = ["HostAgentClient", "HostAgentError", "ALLOWED_OPS",
           "sign_payload", "verify_payload"]
