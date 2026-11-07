"""启动配置回归：空 MCP_TOKEN、随机口令表达式与内部指标令牌的幂等生成。"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"


def _run_bash(body: str, env: dict[str, str] | None = None) -> str:
    process = subprocess.run(
        ["bash", "-c", body], text=True, capture_output=True,
        env={**os.environ, **(env or {})}, timeout=30,
    )
    assert process.returncode == 0, process.stderr
    return process.stdout


def test_entrypoint_bash_syntax_is_valid():
    process = subprocess.run(["bash", "-n", str(ENTRYPOINT)], capture_output=True, text=True)
    assert process.returncode == 0, process.stderr


def test_empty_mcp_token_generates_valid_random_secret():
    script = ENTRYPOINT.read_text(encoding="utf-8")
    assert "string.ascii_letters+string.digits" in script
    assert not re.search(r"ascii_letters\s*\+\s*digits\b", script)

    block = re.search(
        r'if \[ -z "\$\{MCP_TOKEN:-\}" \]; then\n(.*?)\nfi', script, re.S)
    assert block, "未找到空 MCP_TOKEN 的生成分支"
    stdout = _run_bash(
        "set -e\nunset MCP_TOKEN\n" + block.group(1)
        + '\nprintf "__TOKEN__%s" "$MCP_TOKEN"',
        env={"MCP_TOKEN": ""},
    )
    token = stdout.rsplit("__TOKEN__", 1)[-1].strip()
    assert len(token) == 48 and re.fullmatch(r"[A-Za-z0-9]{48}", token), repr(token)


def test_internal_metrics_token_is_generated_once_with_0600(tmp_path):
    script = ENTRYPOINT.read_text(encoding="utf-8")
    start = script.index('if [ "$ROLE" = "gateway" ]; then')
    end = script.index("\nfi\n", start) + len("\nfi\n")
    block = script[start:end]
    path = tmp_path / ".metrics_token"
    harness = (
        "set -e\nROLE=gateway\nunset GATEWAY_METRICS_TOKEN\n"
        f'GATEWAY_METRICS_TOKEN_FILE="{path}"\n' + block
    )
    _run_bash(harness)
    first = path.read_text(encoding="utf-8").strip()
    assert len(first) >= 32
    assert (path.stat().st_mode & 0o777) == 0o600
    _run_bash(harness)
    assert path.read_text(encoding="utf-8").strip() == first, "重启不应更换内部指标令牌"

    # 显式配置时以配置为准，且不把令牌写进日志
    stdout = _run_bash(
        "set -e\nROLE=gateway\nGATEWAY_METRICS_TOKEN=explicit-metrics-token\n"
        f'GATEWAY_METRICS_TOKEN_FILE="{path}"\n' + block,
    )
    assert path.read_text(encoding="utf-8").strip() == "explicit-metrics-token"
    assert "explicit-metrics-token" not in stdout
