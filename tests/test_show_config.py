"""scripts/show-config.sh 的静态约束与只读功能回归。

脚本是部署后唯一的连接信息汇总入口：只读、不落盘、不对配置文件求值，
并且必须诚实标注「本机 / 固定公网 / 随机隧道」，绝不伪造公网地址。
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "show-config.sh"

_SENSITIVE_ENV = (
    "MCP_ENV_FILE", "MCP_CONTAINER", "MCP_ADMIN_CONTAINER",
    "MCP_PUBLIC_BASE_URL", "ADMIN_PUBLIC_BASE_URL", "MCP_TUNNEL_DISABLED",
    "ADMIN_USERNAME", "MCP_HOST_PORT", "ADMIN_HOST_PORT",
    "MCP_PORT", "ADMIN_PORT", "MCP_BIND", "ADMIN_BIND",
    "MCP_BIND_HOST", "ADMIN_BIND_HOST", "MCP_TOKEN", "ADMIN_PASSWORD",
)


def _run(
    script: Path,
    *args: str,
    env_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in _SENSITIVE_ENV:
        env.pop(key, None)
    if env_file is not None:
        env["MCP_ENV_FILE"] = str(env_file)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=script.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _isolated_script(tmp_path: Path) -> Path:
    """把脚本复制到临时目录，避免仓库根的 .env / 真实容器影响功能测试。"""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    target = scripts / "show-config.sh"
    shutil.copy2(SCRIPT, target)
    return target


def _native_env(tmp_path: Path, **values: str) -> Path:
    config = {
        "MCP_TOKEN": "token-abc123",
        "ADMIN_PASSWORD": "pass-xyz789",
        "ADMIN_USERNAME": "admin",
        "MCP_PORT": "8765",
        "ADMIN_PORT": "8788",
        "MCP_BIND": "127.0.0.1",
        "ADMIN_BIND": "127.0.0.1",
    }
    config.update(values)
    path = tmp_path / "gateway.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in config.items()), encoding="utf-8")
    return path


def _json_config(output: str) -> dict:
    for line in output.splitlines():
        if line.startswith('{"mcpServers"'):
            return json.loads(line)
    raise AssertionError("输出里没有可直接复制的一行 mcpServers JSON 配置")


def test_script_is_executable_and_shell_syntax_is_valid():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_script_never_evaluates_config_files():
    text = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("eval ", "source ", "\teval", "\tsource"):
        assert forbidden not in text, "配置文件只能作为文本逐键读取"
    assert 'sed -n "s/^${key}=//p"' in text


def test_script_exposes_required_contract():
    text = SCRIPT.read_text(encoding="utf-8")
    for marker in (
        "--mode", "--help", "MCP_ENV_FILE", "MCP_CONTAINER", "MCP_ADMIN_CONTAINER",
        "MCP_PUBLIC_BASE_URL", "ADMIN_PUBLIC_BASE_URL", "mcpServers", "Bearer Token",
        "入口类型", "管理网站", "管理员账号", "管理员密码",
        "仅本机或内网可达，未配置公网入口", "警告",
    ):
        assert marker in text, marker
    assert "setting MCP_TUNNEL_DISABLED 0" in text, "默认必须开启公网入口，否则远程拿不到 /mcp"
    assert "seq 1 30" in text, "隧道域名异步出现，必须轮询等待后再打印"


def test_native_mode_prints_local_only_credentials_and_client_json(tmp_path):
    script = _isolated_script(tmp_path)
    result = _run(script, "--mode", "native", env_file=_native_env(tmp_path))
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "http://127.0.0.1:8765/mcp" in out
    assert "http://127.0.0.1:8788/" in out
    assert "token-abc123" in out and "pass-xyz789" in out
    assert "[1] 入口类型      仅本机或内网可达，未配置公网入口" in out
    assert "固定公网入口" not in out
    assert "trycloudflare.com" not in out
    entry = _json_config(out)["mcpServers"]["cnb-gateway"]
    assert entry["url"] == "http://127.0.0.1:8765/mcp"
    assert entry["headers"]["Authorization"] == "Bearer token-abc123"


def test_fixed_public_base_is_printed_and_used_in_client_json(tmp_path):
    script = _isolated_script(tmp_path)
    result = _run(
        script,
        "--mode",
        "native",
        env_file=_native_env(tmp_path),
        extra_env={
            "MCP_PUBLIC_BASE_URL": "https://mcp.example.com/",
            "ADMIN_PUBLIC_BASE_URL": "https://admin.example.com",
        },
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "固定公网入口" in out
    assert "https://mcp.example.com/mcp" in out
    assert "https://admin.example.com/" in out
    assert "仅本机或内网可达" not in out
    assert _json_config(out)["mcpServers"]["cnb-gateway"]["url"] == "https://mcp.example.com/mcp"


def test_credentials_with_json_metacharacters_stay_valid_json(tmp_path):
    script = _isolated_script(tmp_path)
    env_file = _native_env(tmp_path, MCP_TOKEN='tok"with\\slash')
    result = _run(script, "--mode", "native", env_file=env_file)
    assert result.returncode == 0, result.stderr
    auth = _json_config(result.stdout)["mcpServers"]["cnb-gateway"]["headers"]["Authorization"]
    assert auth == 'Bearer tok"with\\slash'


def test_missing_token_fails_with_clear_message(tmp_path):
    script = _isolated_script(tmp_path)
    env_file = tmp_path / "env"
    env_file.write_text("ADMIN_PASSWORD=pass-xyz789\n", encoding="utf-8")
    result = _run(script, "--mode", "native", env_file=env_file)
    assert result.returncode != 0
    assert "未能读取 MCP_TOKEN" in result.stderr


def test_missing_docker_container_is_reported_instead_of_fabricated_credentials(tmp_path):
    script = _isolated_script(tmp_path)
    result = _run(
        script,
        "--mode",
        "docker",
        extra_env={"MCP_CONTAINER": "definitely-not-a-running-container"},
    )
    assert result.returncode != 0
    assert "未运行" in result.stderr


def test_help_and_invalid_mode():
    help_result = _run(SCRIPT, "--help")
    assert help_result.returncode == 0
    assert "--mode" in help_result.stdout
    bad = _run(SCRIPT, "--mode", "bogus")
    assert bad.returncode != 0
    assert "native|docker|auto" in bad.stderr
