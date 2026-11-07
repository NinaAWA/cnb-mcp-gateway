"""OAuth 客户端 / 授权码 / 令牌的持久化写入穿透与启动水合。

兼容性约束（不可破坏现有测试与客户端）：
- ``docker/server.py`` 的 ``CLIENTS`` / ``AUTH_CODES`` / ``ISSUED_TOKENS`` 继续作为
  进程内缓存，键与值类型保持不变（令牌字典仍是 ``明文 token -> epoch float``）。
- 持久化层只保存不可逆哈希：令牌与授权码均以 SHA-256 摘要作为主键，
  磁盘/数据库里没有可重放的明文凭据。
- 重启后不需要「把明文令牌灌回字典」：``token_valid`` / ``load_code`` /
  ``client_uris`` 直接从持久层按哈希查活，等价于隐式水合。

所有写入都复用 ``get_storage()._execute``（内含统一脱敏边界），不直连数据库。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from gateway.audit.storage import get_storage


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")


def _epoch(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.timestamp() if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0


class OAuthStore:
    """无状态包装：每次操作都取当前 ``get_storage()``，避免夹具替换存储后写错库。"""

    def __init__(self) -> None:
        self.last_error = ""

    @property
    def storage(self):
        return get_storage()

    def _warn(self, operation: str, exc: BaseException) -> None:
        self.last_error = f"OAuth 持久化 {operation} 失败: {exc!r}"
        try:
            self.storage.add_event(
                "oauth_persistence_warning", status="DEGRADED", source="oauth",
                detail=self.last_error,
            )
        except Exception:  # noqa: BLE001 - 告警失败不能影响 OAuth 主流程
            pass
        print(f"[oauth] WARN {self.last_error}", flush=True)

    # ---------------------------------------------------------------- clients
    def save_client(self, client_id: str, redirect_uris: list[str]) -> None:
        try:
            self.storage._execute(
                "INSERT INTO oauth_clients (client_id,redirect_uris,created_at) "
                "VALUES (?,?,?) ON CONFLICT(client_id) DO UPDATE SET "
                "redirect_uris=excluded.redirect_uris",
                [client_id, json.dumps(redirect_uris, ensure_ascii=False), _iso(time.time())],
            )
        except Exception as exc:  # noqa: BLE001
            self._warn("save_client", exc)

    def client_uris(self, client_id: str) -> list[str]:
        try:
            rows = self.storage._query(
                "SELECT redirect_uris FROM oauth_clients WHERE client_id=?", [client_id])
            if not rows:
                return []
            return [str(u) for u in json.loads(rows[0]["redirect_uris"] or "[]")]
        except Exception as exc:  # noqa: BLE001
            self._warn("client_uris", exc)
            return []

    # ------------------------------------------------------------------ codes
    def save_code(self, code: str, record: dict[str, Any]) -> None:
        try:
            self.storage._execute(
                "INSERT INTO oauth_codes (code_hash,client_id,redirect_uri,challenge,"
                "challenge_method,expires_at,consumed,created_at) VALUES (?,?,?,?,?,?,0,?) "
                "ON CONFLICT(code_hash) DO UPDATE SET consumed=0, "
                "expires_at=excluded.expires_at, redirect_uri=excluded.redirect_uri",
                [_digest(code), str(record.get("client_id", "")),
                 str(record.get("redirect_uri", "")), str(record.get("challenge", "")),
                 str(record.get("method", "")), _iso(float(record.get("expires", 0))),
                 _iso(time.time())],
            )
        except Exception as exc:  # noqa: BLE001
            self._warn("save_code", exc)

    def load_code(self, code: str) -> dict[str, Any] | None:
        try:
            rows = self.storage._query(
                "SELECT * FROM oauth_codes WHERE code_hash=?", [_digest(code)])
        except Exception as exc:  # noqa: BLE001
            self._warn("load_code", exc)
            return None
        if not rows:
            return None
        row = rows[0]
        expires = _epoch(str(row.get("expires_at") or ""))
        if row.get("consumed") or expires <= time.time():
            return None
        return {
            "expires": expires,
            "redirect_uri": row.get("redirect_uri") or "",
            "client_id": row.get("client_id") or "",
            "challenge": row.get("challenge") or "",
            "method": row.get("challenge_method") or "",
        }

    def consume_code(self, code: str) -> None:
        try:
            self.storage._execute(
                "UPDATE oauth_codes SET consumed=1 WHERE code_hash=?", [_digest(code)])
        except Exception as exc:  # noqa: BLE001
            self._warn("consume_code", exc)

    # ----------------------------------------------------------------- tokens
    def save_token(self, token: str, expires_epoch: float, client_id: str = "") -> None:
        try:
            self.storage._execute(
                "INSERT INTO oauth_tokens (token_hash,client_id,expires_at,revoked,created_at) "
                "VALUES (?,?,?,0,?) ON CONFLICT(token_hash) DO UPDATE SET "
                "expires_at=excluded.expires_at, revoked=0",
                [_digest(token), client_id, _iso(expires_epoch), _iso(time.time())],
            )
        except Exception as exc:  # noqa: BLE001
            self._warn("save_token", exc)

    def token_valid(self, token: str) -> tuple[bool, float]:
        """返回 (是否有效, epoch 过期时间)；只按哈希查询，不还原明文令牌。"""
        try:
            rows = self.storage._query(
                "SELECT expires_at, revoked FROM oauth_tokens WHERE token_hash=?",
                [_digest(token)],
            )
        except Exception as exc:  # noqa: BLE001
            self._warn("token_valid", exc)
            return False, 0.0
        if not rows or rows[0].get("revoked"):
            return False, 0.0
        expires = _epoch(str(rows[0].get("expires_at") or ""))
        return (expires > time.time()), expires

    def revoke_token(self, token: str) -> bool:
        try:
            rows = self.storage._query(
                "SELECT token_hash FROM oauth_tokens WHERE token_hash=?", [_digest(token)])
            if not rows:
                return False
            return self.storage._execute(
                "UPDATE oauth_tokens SET revoked=1 WHERE token_hash=?", [_digest(token)])
        except Exception as exc:  # noqa: BLE001
            self._warn("revoke_token", exc)
            return False

    def cleanup_expired(self) -> None:
        now = _iso(time.time())
        try:
            self.storage._execute(
                "DELETE FROM oauth_codes WHERE expires_at < ? OR consumed = 1", [now])
            self.storage._execute(
                "DELETE FROM oauth_tokens WHERE expires_at < ?", [now])
        except Exception as exc:  # noqa: BLE001
            self._warn("cleanup_expired", exc)

    def hydrate(self) -> dict[str, int]:
        """启动时报告已持久化状态并清理过期项；有效令牌按哈希惰性查活。"""
        self.cleanup_expired()
        try:
            counts = {
                "clients": int(self.storage._query(
                    "SELECT COUNT(*) AS n FROM oauth_clients")[0]["n"]),
                "codes": int(self.storage._query(
                    "SELECT COUNT(*) AS n FROM oauth_codes WHERE consumed=0")[0]["n"]),
                "tokens": int(self.storage._query(
                    "SELECT COUNT(*) AS n FROM oauth_tokens WHERE revoked=0")[0]["n"]),
            }
        except Exception as exc:  # noqa: BLE001
            self._warn("hydrate", exc)
            return {"clients": 0, "codes": 0, "tokens": 0}
        return counts

    def token_expiry_iso(self, token: str) -> str:
        """供审计会话表记录令牌有效期；不返回令牌本身。"""
        ok, expires = self.token_valid(token)
        return _iso(expires) if ok else ""


_STORE: OAuthStore | None = None


def get_oauth_store() -> OAuthStore:
    global _STORE
    if _STORE is None:
        _STORE = OAuthStore()
    return _STORE


def oauth_persistence_enabled() -> bool:
    """显式关闭时（例如内存-only 测试）跳过写入穿透。默认开启。"""
    return os.environ.get("OAUTH_PERSIST_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off")
