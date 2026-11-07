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


def test_host_agent_role_requires_token_and_uses_unix_socket():
    script = ENTRYPOINT.read_text(encoding="utf-8")
    assert "host-agent)" in script
    assert "MCP_HOST_AGENT_TOKEN" in script
    assert "gateway.host.agent" in script
    # 缺共享密钥时必须拒绝以无认证方式监听
    process = subprocess.run(
        ["bash", "-c", script.replace("cd /app", "cd /workspace")],
        text=True, capture_output=True, timeout=30,
        env={**os.environ, "MCP_SERVICE_ROLE": "host-agent",
             "MCP_HOST_AGENT_TOKEN": ""},
    )
    assert process.returncode != 0
    assert "拒绝" in process.stderr or "MCP_HOST_AGENT_TOKEN" in process.stderr
    assert "python -m gateway.host.agent" in script


def test_new_phase_c_variables_are_forwarded():
    script = ENTRYPOINT.read_text(encoding="utf-8")
    for name in ("MCP_ACCESS_MODE", "MCP_DANGER_GUARD",
                 "MCP_INDEPENDENT_CONFIRM", "MCP_EXEC_BACKEND",
                 "MCP_HOST_AGENT_SOCKET", "MCP_HOST_AGENT_TOKEN",
                 "MCP_HOST_AGENT_ROOTS"):
        assert re.search(rf"^\s*{name}$", script, re.M), f"{name} 未透传"
    template = (ROOT / "docker" / ".env.template").read_text(encoding="utf-8")
    assert "MCP_ACCESS_MODE=full_admin" in template
    assert "MCP_INDEPENDENT_CONFIRM=0" in template


def test_compose_host_profile_has_no_published_port():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    start = compose.index("  host-agent:")
    end = compose.index("  # 可选 HTTPS 入口", start)
    block = compose[start:end]
    assert 'profiles: ["host"]' in block
    assert "MCP_SERVICE_ROLE=host-agent" in block
    assert "ports:" not in block, "Host Agent 不得发布任何 TCP 端口"
    assert "host-socket:" in block
    assert "host-socket:" in compose[compose.index("volumes:"):]


def test_phase_d_transfer_variables_are_forwarded():
    script = ENTRYPOINT.read_text(encoding="utf-8")
    for name in ("MCP_TRANSFER_DIR", "MCP_TRANSFER_SECRET",
                 "MCP_TRANSFER_ALLOW_PRIVATE", "TRANSFER_MAX_MB",
                 "MCP_TASK_LOG_DIR"):
        assert re.search(rf"^\s*{name}$", script, re.M), f"{name} 未透传"
    template = (ROOT / "docker" / ".env.template").read_text(encoding="utf-8")
    assert "MCP_TRANSFER_DIR=/workspace/transfers" in template
    assert "TRANSFER_MAX_MB=256" in template
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "MCP_TRANSFER_SECRET=${MCP_TRANSFER_SECRET:-}" in compose
    assert "TRANSFER_MAX_MB=${TRANSFER_MAX_MB:-256}" in compose
    assert "**/*.part" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_docs_and_config_stay_in_sync_with_v3_code():
    """文档/配置同步：关键事实必须与已实现代码一致，不能写成旧状态。"""
    template = (ROOT / "docker" / ".env.template").read_text(encoding="utf-8")
    for name in ("MCP_ACCESS_MODE", "MCP_HOST_AGENT_TOKEN", "MCP_TRANSFER_SECRET",
                 "MCP_PAPER_DIR", "AUDIT_SPOOL_ENABLED", "SEARXNG_SECRET"):
        assert re.search(rf"^{name}=", template, re.M), f"{name} 未在模板中"
    # load_env() 不剥离行内注释：赋值行不得出现 "key=value # comment"
    inline = [line for line in template.splitlines()
              if "=" in line and not line.lstrip().startswith("#")
              and re.search(r"\S\s+#", line)]
    assert inline == [], f"模板存在行内注释（会被 load_env 误读）: {inline}"

    requirements = (ROOT / "docker" / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^pypdf", requirements, re.M), "唯一新增依赖 pypdf 缺失"
    for ocr in ("pytesseract", "easyocr", "ocrmypdf", "tesserocr"):
        assert ocr not in requirements, f"OCR 依赖默认不应安装: {ocr}"

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for profile in ("search", "host", "tunnel", "postgres"):
        assert f'"{profile}"' in compose, f"缺少 {profile} profile"
    searxng = (ROOT / "searxng" / "settings.yml").read_text(encoding="utf-8")
    assert "- json" in searxng and "limiter: true" in searxng

    for name in ("docs/v3-inventory.md", "docs/v3-upgrade.zh-CN.md",
                 "docs/v3-acceptance-report.md", "docs/admin-dashboard.zh-CN.md",
                 "docs/search-sources-research.md"):
        assert (ROOT / name).is_file(), name
    inventory = (ROOT / "docs" / "v3-inventory.md").read_text(encoding="utf-8")
    assert "3cf1ab57" in inventory and "阶段 E 实施结果" in inventory
    report = (ROOT / "docs" / "v3-acceptance-report.md").read_text(encoding="utf-8")
    assert "未执行" in report and "143 passed" in report, "验收报告必须如实标注未验收项与当前测试数"
    # 文档里引用的测试路径必须真实存在，且 opt-in 集成测试确实存在并已注册 marker。
    for doc in ("docs/v3-upgrade.zh-CN.md", "docs/admin-dashboard.zh-CN.md",
                "docs/v3-acceptance-report.md", "README.md", "README.zh-CN.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        for rel in re.findall(r"tests/[\w/]+\.py", text):
            assert (ROOT / rel).is_file(), f"{doc} 引用了不存在的测试文件: {rel}"
    ini = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "integration" in ini and 'not integration' in ini
    assert (ROOT / "tests" / "integration" / "test_real_network.py").is_file()
    assert (ROOT / "tests" / "integration" / "test_workflows.py").is_file()

    # 所有实际注册的工具都必须在三份文档中出现，避免文档与代码漂移。
    server_source = (ROOT / "docker" / "server.py").read_text(encoding="utf-8")
    tools = re.findall(r"@mcp\.tool\(\)\s*\n\s*def\s+(\w+)", server_source)
    assert len(tools) == 49, f"实际工具数为 {len(tools)}，文档需同步"
    for name in ("README.md", "README.zh-CN.md", "docs/v3-upgrade.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        missing = [t for t in tools if f"`{t}`" not in text]
        assert not missing, f"{name} 缺少工具: {missing}"
        assert "49" in text and "V3" in text, f"{name} 未同步 V3 工具规模"
    zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    assert "不等于全网通用搜索" in zh and "OCR" in zh
    assert "search_web_advanced" in zh and "git_inspect" in zh


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
