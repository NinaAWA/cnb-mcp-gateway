"""访问策略（V3 阶段 C）。

重要边界声明：默认 ``full_admin`` —— 不启用路径白名单、不启用 shell 白名单，
管理员可以在执行环境内执行任意命令。内置的灾难级正则 / 破坏性确认门是
**辅助保护，不是安全边界**：命令行拼接、别名、解释器、脚本文件都能绕过正则。
真正的边界是「只把 MCP_TOKEN 交给可信管理员」与容器/宿主隔离。

可选 ``MCP_INDEPENDENT_CONFIRM=1`` 时，重大破坏性操作除了 ``confirm=true``
还需要一次独立审批（另一请求 ID 写入 approval_events 后方可消费）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from typing import Any

from gateway.audit.events import utcnow

DEFAULT_DANGER_PATTERNS = (
    r"^rm\s+-rf\s+/\s*($|\*|;)", r"^rm\s+-rf\s+/\.\s*($|;)",
    r"\bmkfs\.", r"\b(shutdown|halt|reboot|poweroff)\b",
    r"dd\b.*\bof=/dev/", r">\s*/dev/sd", r"chmod\s+-R\s+.*\s+/\s*$",
)
CATASTROPHIC_EXTRA = (
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",
)


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class AccessPolicy:
    def __init__(self, *, mode: str = "", danger_guard: bool | None = None,
                 independent_confirm: bool | None = None,
                 approval_ttl: int = 300):
        self.mode = (mode or os.environ.get("MCP_ACCESS_MODE", "full_admin")).strip().lower()
        if self.mode not in ("full_admin", "restricted"):
            self.mode = "full_admin"
        self.danger_guard = (_flag("MCP_DANGER_GUARD", True)
                             if danger_guard is None else danger_guard)
        self.independent_confirm = (
            _flag("MCP_INDEPENDENT_CONFIRM", False)
            if independent_confirm is None else independent_confirm)
        self.approval_ttl = max(10, min(approval_ttl, 3600))
        self.deny_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in (*DEFAULT_DANGER_PATTERNS, *CATASTROPHIC_EXTRA)
            + tuple(x for x in os.environ.get("MCP_DENY_PREFIXES", "")
                    .replace(",", "|").split("|") if x)
        ]
        self.require_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in tuple(x for x in os.environ.get("MCP_REQUIRE_CONFIRM", "")
                           .replace(",", "|").split("|") if x)
        ]
        self.allowed_prefixes = [
            p for p in os.environ.get("MCP_ALLOWED_PREFIXES", "").split("|") if p
        ] if self.mode == "restricted" else []

    @property
    def full_admin(self) -> bool:
        return self.mode == "full_admin"

    def note(self) -> str:
        return ("辅助保护（正则/确认）不是安全边界；完整管理员权限由可信令牌与"
                "容器隔离保证。")

    def classify(self, command: str, extra_deny=(),
                 extra_destructive=()) -> str:
        """返回 'catastrophic' | 'destructive' | 'normal'。"""
        normalized = re.sub(r"\s+", " ", re.sub(r"^sudo\s+", "", str(command).strip(),
                                                flags=re.IGNORECASE))
        if self.danger_guard:
            for pattern in self.deny_patterns:
                if pattern.search(normalized):
                    return "catastrophic"
            for raw in extra_deny:
                if re.search(raw, normalized):
                    return "catastrophic"
        for pattern in self.require_patterns:
            if pattern.search(normalized):
                return "destructive"
        for raw in extra_destructive:
            if re.search(raw, normalized):
                return "destructive"
        return "normal"

    def check(self, command: str, *, confirm: bool = False,
              approval_id: str = "", request_id: str = "",
              extra_deny=(), extra_destructive=()) -> tuple[bool, str]:
        """返回 (是否放行, 拒绝原因)。独立审批在此消费。"""
        kind = self.classify(command, extra_deny, extra_destructive)
        if kind == "catastrophic":
            return False, "毁灭级危险指令已被辅助安全网拒绝（不是安全边界）"
        if self.allowed_prefixes and not any(
                command.strip().startswith(p) for p in self.allowed_prefixes):
            return False, f"restricted 模式：命令不在白名单 {self.allowed_prefixes} 内"
        if kind == "destructive" and not confirm:
            return False, "破坏性指令需要 confirm=true"
        if kind == "destructive" and self.independent_confirm:
            if not self.consume_approval(command, approval_id):
                return False, ("启用 MCP_INDEPENDENT_CONFIRM：需先用 "
                               "request_approval 取得独立审批，并把审批 ID 传入 "
                               "approval_id")
        return True, ""

    # ---------------------------------------------------------- 独立审批
    def _storage(self):
        from gateway.audit.storage import get_storage
        return get_storage()

    @staticmethod
    def command_hash(command: str) -> str:
        return hashlib.sha256(str(command).encode("utf-8")).hexdigest()[:32]

    def request_approval(self, command: str, *, actor_id: str = "",
                         request_id: str = "") -> dict[str, Any]:
        approval_id = secrets.token_hex(12)
        payload = {"approval_id": approval_id, "command_hash": self.command_hash(command),
                   "expires_at": time.time() + self.approval_ttl,
                   "created_at": utcnow(), "consumed": False}
        self._storage().add_approval({
            "approval_id": approval_id, "request_id": request_id, "ts": utcnow(),
            "action": "request", "actor_id": actor_id,
            "detail": json.dumps(payload, ensure_ascii=False),
        })
        return {"ok": True, "approval_id": approval_id,
                "expires_in": self.approval_ttl,
                "note": "独立审批仅对同一命令摘要有效，且只能消费一次"}

    def _consume(self, approval_id: str) -> bool:
        if not approval_id:
            return False
        store = self._storage()
        rows = store._query(
            "SELECT id, detail FROM approval_events WHERE action='request' "
            "AND approval_id=? ORDER BY id DESC LIMIT 1", [approval_id])
        if not rows:
            return False
        try:
            payload = json.loads(rows[0]["detail"] or "{}")
        except (ValueError, TypeError):
            return False
        if payload.get("consumed") or float(payload.get("expires_at") or 0) < time.time():
            return False
        payload["consumed"] = True
        store._execute("UPDATE approval_events SET detail=? WHERE id=?",
                       [json.dumps(payload, ensure_ascii=False), rows[0]["id"]])
        store.add_approval({
            "approval_id": approval_id, "request_id": "", "ts": utcnow(),
            "action": "consume", "actor_id": "",
            "detail": json.dumps({"command_hash": payload.get("command_hash")},
                                 ensure_ascii=False),
        })
        return True

    def consume_approval(self, command: str, approval_id: str) -> bool:
        """校验审批 ID 对应同一命令且未消费；调用方负责把 ID 透传进来。"""
        if not approval_id:
            return False
        store = self._storage()
        rows = store._query(
            "SELECT detail FROM approval_events WHERE action='request' "
            "AND approval_id=? ORDER BY id DESC LIMIT 1", [approval_id])
        if not rows:
            return False
        try:
            payload = json.loads(rows[0]["detail"] or "{}")
        except (ValueError, TypeError):
            return False
        return (payload.get("command_hash") == self.command_hash(command)
                and self._consume(approval_id))


_POLICY: AccessPolicy | None = None


def get_policy() -> AccessPolicy:
    global _POLICY
    if _POLICY is None:
        _POLICY = AccessPolicy()
    return _POLICY
