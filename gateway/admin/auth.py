"""Admin Web 独立认证：密码、会话、失败限制、可选 TOTP、登录审计。

与 MCP_API 的 Bearer/OAuth 完全隔离：不读取 MCP_TOKEN，也不因公网地址可达
而放行。未登录用户无法查询任何调用记录。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from gateway.audit.events import utcnow
from gateway.audit.storage import get_storage

PBKDF2_ROUNDS = 200_000
COOKIE_NAME = "mcp_admin_session"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _totp_valid(secret: str, code: str, window: int = 1) -> bool:
    if not secret or not code or not code.isdigit():
        return False
    try:
        padded = secret.upper().replace(" ", "") + "=" * ((8 - len(secret) % 8) % 8)
        key = base64.b32decode(padded, casefold=True)
    except (ValueError, TypeError):
        return False
    counter = int(time.time() // 30)
    for shift in range(-window, window + 1):
        mac = hmac.new(key, (counter + shift).to_bytes(8, "big"), hashlib.sha1).digest()
        offset = mac[-1] & 0x0F
        value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
        if hmac.compare_digest(f"{value % 1_000_000:06d}", code):
            return True
    return False


class AdminAuth:
    def __init__(self) -> None:
        self.storage = get_storage()
        self.username = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
        self.generated_password = ""
        password = os.environ.get("ADMIN_PASSWORD", "").strip()
        if not password:
            password = secrets.token_urlsafe(24)
            self.generated_password = password
        self.salt = secrets.token_bytes(16)
        self.password_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), self.salt, PBKDF2_ROUNDS
        )
        # 未配置固定密钥时随机生成：容器重启后旧会话自动失效（更安全）
        self.session_secret = (
            os.environ.get("ADMIN_SESSION_SECRET", "").encode() or secrets.token_bytes(32)
        )
        self.ttl_seconds = max(300, int(os.environ.get("ADMIN_SESSION_TTL", "28800")))
        self.totp_secret = os.environ.get("ADMIN_TOTP_SECRET", "").strip()
        self.max_failures = max(1, int(os.environ.get("ADMIN_LOGIN_MAX_FAILURES", "5")))
        self.lock_minutes = max(1, int(os.environ.get("ADMIN_LOGIN_LOCK_MINUTES", "15")))
        self._lock = threading.Lock()
        self._cleanup_sessions()

    @property
    def mfa_enabled(self) -> bool:
        return bool(self.totp_secret)

    def _sign(self, session_id: str) -> str:
        mac = hmac.new(self.session_secret, session_id.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{session_id}.{mac}"

    def _unsign(self, value: str) -> str | None:
        if not value or "." not in value:
            return None
        session_id, mac = value.rsplit(".", 1)
        expected = hmac.new(
            self.session_secret, session_id.encode(), hashlib.sha256
        ).hexdigest()[:32]
        return session_id if hmac.compare_digest(mac, expected) else None

    def _log(self, outcome: str, username: str, remote_ip: str, detail: str = "") -> None:
        self.storage._insert("login_attempts", {
            "ts": utcnow(), "username": username[:120], "remote_ip": remote_ip[:64],
            "outcome": outcome, "detail": detail[:300],
        })
        self.storage.add_event(
            "admin_auth", status=outcome.upper(), source="admin",
            actor_id=f"admin:{username}", detail={"remote_ip": remote_ip, "detail": detail},
        )

    def _locked(self, username: str, remote_ip: str) -> bool:
        since = _iso(_now() - timedelta(minutes=self.lock_minutes))
        rows = self.storage._query(
            "SELECT outcome, ts FROM login_attempts WHERE username=? AND remote_ip=? "
            "AND ts>=? ORDER BY id DESC LIMIT ?",
            [username, remote_ip, since, self.max_failures + 2],
        )
        failures = 0
        for row in rows:
            if row["outcome"] == "success":
                break
            if row["outcome"] in ("failure", "locked"):
                failures += 1
        return failures >= self.max_failures

    def login(self, username: str, password: str, otp: str,
              remote_ip: str, user_agent: str) -> tuple[bool, str, str]:
        """返回 (是否成功, 提示, 会话 cookie 值)。"""
        with self._lock:
            if self._locked(username, remote_ip):
                self._log("locked", username, remote_ip, "失败次数过多，账户临时锁定")
                return False, "登录失败次数过多，请稍后再试", ""
            ok = hmac.compare_digest(
                hashlib.pbkdf2_hmac("sha256", password.encode(), self.salt, PBKDF2_ROUNDS),
                self.password_hash,
            ) and hmac.compare_digest(username, self.username)
            if not ok:
                self._log("failure", username, remote_ip, "用户名或密码错误")
                return False, "用户名或密码错误", ""
            if self.mfa_enabled and not _totp_valid(self.totp_secret, otp):
                self._log("failure", username, remote_ip, "动态验证码错误")
                return False, "动态验证码错误", ""

            session_id = secrets.token_urlsafe(24)
            created = _now()
            expires = created + timedelta(seconds=self.ttl_seconds)
            self.storage._insert("admin_sessions", {
                "session_id": session_id, "username": username,
                "created_at": _iso(created), "expires_at": _iso(expires),
                "last_seen": _iso(created), "remote_ip": remote_ip[:64],
                "user_agent": user_agent[:250], "revoked": 0,
            })
            self._log("success", username, remote_ip, "登录成功")
            return True, "登录成功", self._sign(session_id)

    def session(self, cookie_value: str) -> dict[str, Any] | None:
        session_id = self._unsign(cookie_value or "")
        if not session_id:
            return None
        rows = self.storage._query(
            "SELECT * FROM admin_sessions WHERE session_id=?", [session_id]
        )
        if not rows:
            return None
        row = rows[0]
        expires = _parse(str(row["expires_at"]))
        if row["revoked"] or not expires or expires <= _now():
            return None
        self.storage._execute(
            "UPDATE admin_sessions SET last_seen=? WHERE session_id=?",
            [utcnow(), session_id],
        )
        return row

    def logout(self, cookie_value: str) -> None:
        session_id = self._unsign(cookie_value or "")
        if session_id:
            self.storage._execute(
                "UPDATE admin_sessions SET revoked=1 WHERE session_id=?", [session_id]
            )
            self.storage.add_event("admin_auth", status="LOGOUT", source="admin",
                                   detail=f"session={session_id[:8]}… 已安全退出")

    def revoke(self, session_id: str) -> None:
        self.storage._execute(
            "UPDATE admin_sessions SET revoked=1 WHERE session_id=?", [session_id]
        )
        self.storage.add_event("admin_auth", status="REVOKE", source="admin",
                               detail=f"session={session_id[:8]}… 被管理员撤销")

    def sessions(self) -> list[dict[str, Any]]:
        rows = self.storage._query(
            "SELECT session_id, username, created_at, expires_at, last_seen, "
            "remote_ip, user_agent, revoked FROM admin_sessions ORDER BY created_at DESC LIMIT 100"
        )
        for row in rows:
            row["session_short"] = str(row["session_id"])[:10]
            row["active"] = not row["revoked"] and bool(
                _parse(str(row["expires_at"])) and _parse(str(row["expires_at"])) > _now()
            )
        return rows

    def _cleanup_sessions(self) -> None:
        self.storage._execute(
            "DELETE FROM admin_sessions WHERE expires_at < ?", [_iso(_now())]
        )


_AUTH: AdminAuth | None = None
_AUTH_LOCK = threading.Lock()


def get_auth() -> AdminAuth:
    global _AUTH
    if _AUTH is None:
        with _AUTH_LOCK:
            if _AUTH is None:
                _AUTH = AdminAuth()
    return _AUTH
