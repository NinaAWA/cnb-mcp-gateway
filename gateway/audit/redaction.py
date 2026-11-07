"""敏感信息脱敏与大输出引用存储。

默认绝不写入明文令牌、密码、SSH 私钥或云访问密钥。参数、输出、错误信息
在落库与写入 artifact 之前都必须经过本模块。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

REDACTED = "***REDACTED***"

# 键名命中即整值替换（大小写不敏感的子串匹配）
SENSITIVE_KEY_PARTS = (
    "token", "password", "passwd", "secret", "api_key", "apikey", "access_key",
    "private_key", "credential", "authorization", "auth_header", "cookie",
    "session_key", "ssh_key", "bearer", "otp", "totp", "mfa", "passphrase",
    "dsn", "connection_string", "conn_str", "database_url",
)

# 值内容特征：即使键名无害也要替换
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in (
        r"gh[pousr]_[A-Za-z0-9]{20,}",
        r"tvly-[A-Za-z0-9_-]{10,}",
        r"sk-[A-Za-z0-9_-]{16,}",
        r"xox[baprs]-[A-Za-z0-9-]{10,}",
        r"AKIA[0-9A-Z]{12,}",
        r"AIza[0-9A-Za-z_-]{20,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}",
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s\"'&,;]{4,}",
        # 任意协议的连接串凭据（postgresql://user:pass@host 等），不只是 http(s)
        r"\b[a-z][a-z0-9+.\-]*://[^\s/@]+:[^\s/@]+@",
    )
)

# 环境变量转储里 k=v 形式同样要脱敏
_ENV_VALUE_RE = re.compile(
    r"(?im)^([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|PRIVATE|MAIL|CREDENTIAL|"
    r"DSN|CONNECTION|DATABASE_URL)[A-Z0-9_]*)=.*$"
)
# 命令文本中的赋值（export SECRET_TOKEN=xxx / FOO_API_KEY=yyy / --password=zzz）：
# 保留变量名便于排障，只替换值，且不受「行首」限制。
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|PRIVATE|MAIL|CREDENTIAL|"
    r"DSN|CONNECTION|DATABASE_URL)[A-Z0-9_]*)\s*=\s*(['\"]?)([^\s'\"&,;]{1,})\2"
)

_MAX_DEPTH = 12


def redact_text(value: Any, *, limit: int = 0) -> str:
    """对任意文本做特征脱敏，可选截断。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = _ASSIGNMENT_SECRET_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _ENV_VALUE_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    if limit and len(text) > limit:
        return text[:limit] + f"…[已截断，原始长度 {len(text)}]"
    return text


def redact_structure(value: Any, _depth: int = 0) -> Any:
    """递归脱敏 dict / list / 标量，保持结构便于前端 JSON 展示。"""
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            keyname = str(key)
            if any(part in keyname.lower() for part in SENSITIVE_KEY_PARTS):
                out[key] = REDACTED
            else:
                out[key] = redact_structure(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_structure(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(value)


def redact_json(value: Any) -> str:
    try:
        return json.dumps(redact_structure(value), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return redact_text(repr(value), limit=4096)


def fingerprint(secret: str) -> str:
    """把已验证令牌转成不可逆短指纹，作为 client_id / actor_id。"""
    return "tok_" + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class ArtifactStore:
    """大输出的受保护文件存储；数据库只保存引用、大小与校验值。"""

    def __init__(self, root: str | Path, threshold: int = 32768, max_bytes: int = 4 * 1024 * 1024):
        self.root = Path(root)
        self.threshold = threshold
        self.max_bytes = max_bytes

    def store(self, text: str) -> dict[str, Any]:
        """返回 {text, ref, size, sha256}；小文本直接内联，大文本落文件。"""
        raw = text.encode("utf-8", "replace")
        digest = sha256_bytes(raw)
        size = len(raw)
        if size <= self.threshold:
            return {"text": text, "ref": "", "size": size, "sha256": digest}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            # 大输出只保留受控上限，避免磁盘被撑爆；校验和必须对应实际落盘内容
            payload = raw[: self.max_bytes]
            digest = sha256_bytes(payload)
            path = self.root / f"{digest}.txt"
            if not path.exists():
                tmp = self.root / f".{digest}.tmp"
                tmp.write_bytes(payload)
                os.chmod(tmp, 0o600)
                tmp.replace(path)
            preview = payload[:2048].decode("utf-8", "replace")
            return {
                "text": preview + f"\n…[大输出已存为受保护引用，完整内容见 {path.name}]",
                "ref": f"artifact://{path.name}",
                "size": size,
                "stored_size": len(payload),
                "sha256": digest,
            }
        except OSError as exc:
            # 文件系统不可用时退化为截断内联，绝不把超大内容写进数据库
            return {
                "text": text[: self.threshold] + f"…[存储失败: {exc}]",
                "ref": "",
                "size": size,
                "sha256": digest,
            }

    def read(self, ref: str, expected_sha256: str = "") -> str | None:
        if not ref.startswith("artifact://"):
            return None
        name = Path(ref[len("artifact://"):]).name
        path = self.root / name
        if not path.is_file():
            return None
        try:
            raw = path.read_bytes()
            # 引用文件可被容器内进程改写，读取前校验摘要，避免展示被篡改内容
            if expected_sha256 and sha256_bytes(raw) != expected_sha256:
                return None
            return raw.decode("utf-8", errors="replace")
        except OSError:
            return None
