"""v1.1 交付物静态校验：傻瓜式一键部署、持久化与凭据不入库。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_one_command_deploy_exists_in_both_readmes():
    for name in ("README.md", "README.zh-CN.md"):
        text = _text(name)
        assert "install-native.sh" in text
        assert "cp .env.example .env && docker compose up -d --build" in text
        assert "scripts/selfcheck_v11.py" in text
        assert "git clone" in text


def test_env_example_has_all_required_keys_and_no_secrets():
    lines = [line.strip() for line in _text(".env.example").splitlines()]
    required = ("MCP_TOKEN", "ADMIN_PASSWORD", "MCP_PUBLIC_BASE_URL",
                "MCP_ALLOWED_PREFIXES", "MCP_FILE_ROOT", "MCP_TASK_LOG_DIR",
                "MCP_DOCKER_SOCKET", "MCP_FILE_CHUNK_BYTES", "MCP_IMAGE")
    for key in required:
        assert any(line.startswith(key + "=") for line in lines), key
    text = _text(".env.example")
    assert "ghp_" not in text
    assert "REPLACE_WITH_A_LONG_RANDOM_TOKEN" not in text
    assert "MCP_TOKEN=" in lines, "一键部署应允许留空自动生成令牌"
    assert "ADMIN_PASSWORD=" in lines, "一键部署应允许留空自动生成口令"
    assert "MCP_TUNNEL_DISABLED=1" in lines, "默认不应暴露随机公网隧道"


def test_selfcheck_waits_for_real_readiness_before_scoring():
    text = _text("scripts/selfcheck_v11.py")
    assert "def wait_ready" in text
    assert "wait_ready()" in text
    assert "未运行任何用例" in text, "未就绪时必须如实报错而不是逐项误判"


def test_compose_persists_files_tasks_and_audit():
    text = _text("docker-compose.yml")
    for marker in ("audit-data:/data/audit", "files-data:/data/files",
                   "MCP_TASK_LOG_DIR=/data/audit/task-logs",
                   "MCP_FILE_ROOT=/data/files", "127.0.0.1",
                   "MCP_PUBLIC_BASE_URL"):
        assert marker in text, marker
    assert "ADMIN_PASSWORD=${ADMIN_PASSWORD:-admin}" not in text
    volumes = text.rsplit("volumes:", 1)[-1]
    assert "audit-data:" in volumes and "files-data:" in volumes


def test_host_docker_override_is_explicitly_opt_in():
    text = _text("docker-compose.host-docker.yml")
    assert "/var/run/docker.sock:/var/run/docker.sock" in text
    assert "MCP_DOCKER_SOCKET=1" in text
    assert "root" in text, "必须写明授权范围等价于宿主机 root"


def test_native_installer_covers_upgrade_rollback_and_git_lfs():
    text = _text("scripts/install-native.sh")
    for marker in ("--upgrade", "--rollback", "git-lfs", "git lfs install",
                   "chmod 600", "MCP_TASK_LOG_DIR", "MCP_FILE_ROOT",
                   "MCP_PUBLIC_BASE_URL", "systemctl"):
        assert marker in text, marker


def test_dockerfile_installs_git_lfs_without_research_deps():
    text = _text("docker/Dockerfile")
    assert "git-lfs" in text
    assert "/usr/bin/git-lfs install --system" in text
    assert "ENV ADMIN_PASSWORD=admin" not in text
    req = _text("docker/requirements.txt").lower()
    for banned in ("torch", "tensorflow", "numpy", "scipy", "transformers"):
        assert banned not in req, f"镜像不应硬编码科研依赖: {banned}"


def test_selfcheck_fixtures_are_ignored():
    ignored = _text(".gitignore")
    assert ".mcp-v11-selfcheck/" in ignored
    assert ".env" in ignored


def test_no_plaintext_pat_in_tracked_sources():
    for path in ("README.md", "README.zh-CN.md", ".env.example",
                 "docker-compose.yml", "docker/entrypoint.sh",
                 "scripts/install-native.sh"):
        assert "ghp_" not in _text(path), f"{path} 不应含明文 PAT"
