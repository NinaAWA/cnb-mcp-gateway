"""Admin Web 默认口令回归测试。

镜像默认值由 Dockerfile 的 ENV 与 docker/entrypoint.sh 提供；这里验证最终
生效口令能被 AdminAuth 正确使用，且仍可用更强的 ADMIN_PASSWORD 覆盖。
"""
from __future__ import annotations

import pytest

from gateway.admin.auth import AdminAuth


def _try_login(password: str) -> bool:
    auth = AdminAuth()
    ok, _message, _cookie = auth.login("admin", password, "", "127.0.0.1", "pytest")
    return ok


@pytest.mark.usefixtures("isolated_storage")
def test_default_admin_password_can_login(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    monkeypatch.delenv("ADMIN_TOTP_SECRET", raising=False)
    assert _try_login("admin") is True
    assert _try_login("not-the-password") is False


@pytest.mark.usefixtures("isolated_storage")
def test_explicit_password_overrides_default(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "S3cret-Pass!")
    monkeypatch.delenv("ADMIN_TOTP_SECRET", raising=False)
    assert _try_login("admin") is False
    assert _try_login("S3cret-Pass!") is True


@pytest.mark.usefixtures("isolated_storage")
def test_stale_password_file_is_ignored(monkeypatch, tmp_path):
    stale = tmp_path / ".admin_password"
    stale.write_text("OLD-RANDOM-PASSWORD\n", encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_FILE", str(stale))
    monkeypatch.delenv("ADMIN_TOTP_SECRET", raising=False)
    assert _try_login("OLD-RANDOM-PASSWORD") is False
    assert _try_login("admin") is True
