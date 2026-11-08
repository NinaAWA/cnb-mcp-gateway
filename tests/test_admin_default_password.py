"""Admin Web 口令回归测试。

v1.1 起不再内置硬编码弱口令：容器首次启动由 entrypoint.sh 随机生成并写入
600 权限持久文件，容器重建复用；显式 ADMIN_PASSWORD 始终优先。裸跑时
AdminAuth 自身也会生成随机口令，绝不回落到 admin。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gateway.admin.auth import AdminAuth

ROOT = Path(__file__).resolve().parents[1]


def _try_login(password: str) -> bool:
    auth = AdminAuth()
    ok, _message, _cookie = auth.login("admin", password, "", "127.0.0.1", "pytest")
    return ok


@pytest.mark.usefixtures("isolated_storage")
def test_explicit_password_is_used(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "S3cret-Pass!")
    monkeypatch.delenv("ADMIN_TOTP_SECRET", raising=False)
    assert _try_login("admin") is False
    assert _try_login("S3cret-Pass!") is True


@pytest.mark.usefixtures("isolated_storage")
def test_unset_password_never_falls_back_to_admin(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_TOTP_SECRET", raising=False)
    assert _try_login("admin") is False
    auth = AdminAuth()
    assert auth.generated_password and auth.generated_password != "admin"


def test_no_hardcoded_default_password_in_deployment_assets():
    assets = {
        "docker/entrypoint.sh": ROOT / "docker" / "entrypoint.sh",
        "docker/Dockerfile": ROOT / "docker" / "Dockerfile",
        "docker-compose.yml": ROOT / "docker-compose.yml",
        ".env.example": ROOT / ".env.example",
    }
    for name, path in assets.items():
        text = path.read_text(encoding="utf-8")
        assert 'ADMIN_PASSWORD="admin"' not in text, f"{name} 仍内置弱口令"
        assert "ADMIN_PASSWORD=admin" not in text, f"{name} 仍内置弱口令"

    entrypoint = assets["docker/entrypoint.sh"].read_text(encoding="utf-8")
    assert "generate_secret" in entrypoint
    assert 'cat "$PASS_FILE"' in entrypoint
    assert '> "$PASS_FILE"' in entrypoint
    assert "umask 077" in entrypoint
    env_lines = [line.strip() for line in assets[".env.example"].read_text().splitlines()]
    assert "ADMIN_PASSWORD=" in env_lines, "示例配置应留空由首次启动生成"
