"""阶段 C 权限：full_admin 默认、辅助保护可配置、独立审批可选、兼容旧行为。"""
from __future__ import annotations

import os

from gateway.access.policy import AccessPolicy, get_policy


def test_full_admin_is_default_and_guard_is_not_a_security_boundary(monkeypatch):
    monkeypatch.delenv("MCP_ACCESS_MODE", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_PREFIXES", raising=False)
    policy = AccessPolicy()
    assert policy.mode == "full_admin" and policy.full_admin is True
    assert policy.allowed_prefixes == []
    assert "不是安全边界" in policy.note()
    ok, reason = policy.check("python train.py --epochs 10")
    assert ok and reason == ""


def test_restricted_mode_enables_prefix_whitelist(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_PREFIXES", "ls|pwd")
    allowed, _ = AccessPolicy(mode="full_admin").check("git status")
    assert allowed, "full_admin 不受路径白名单限制"
    policy = AccessPolicy(mode="restricted")
    ok, reason = policy.check("git status")
    assert not ok and "白名单" in reason
    assert policy.check("ls -la")[0] is True


def test_danger_guard_can_be_disabled_explicitly(monkeypatch):
    monkeypatch.setenv("MCP_DANGER_GUARD", "0")
    policy = AccessPolicy(danger_guard=False)
    assert policy.classify("shutdown -h now") == "normal"
    assert AccessPolicy(danger_guard=True).classify("shutdown -h now") == "catastrophic"


def test_independent_confirmation_is_opt_in_and_one_time(isolated_storage,
                                                         monkeypatch):
    monkeypatch.setenv("MCP_INDEPENDENT_CONFIRM", "1")
    policy = AccessPolicy(independent_confirm=False)
    ok, reason = policy.check("rm -rf ./build", confirm=True, approval_id="x",
                              extra_destructive=(r"rm\s+-rf?\b",))
    assert ok and reason == "", "关闭时不应要求独立审批"

    strict = AccessPolicy(independent_confirm=True)
    ok, reason = strict.check("rm -rf ./build", confirm=True, approval_id="",
                              extra_destructive=(r"rm\s+-rf?\b",))
    assert not ok and "独立审批" in reason
    approval = strict.request_approval("rm -rf ./build", request_id="req-a")
    ok, reason = strict.check("rm -rf ./build", confirm=True,
                              approval_id=approval["approval_id"],
                              extra_destructive=(r"rm\s+-rf?\b",))
    assert ok, reason
    ok, reason = strict.check("rm -rf ./build", confirm=True,
                              approval_id=approval["approval_id"],
                              extra_destructive=(r"rm\s+-rf?\b",))
    assert not ok, "同一审批不得重复使用"
    rows = isolated_storage._query("SELECT action FROM approval_events")
    assert {"request", "consume"} <= {r["action"] for r in rows}


def test_existing_command_tools_keep_their_behavior(gateway_module, monkeypatch):
    server = gateway_module
    monkeypatch.delenv("MCP_INDEPENDENT_CONFIRM", raising=False)
    assert "exit=0" in server.run_command("echo phase-c-ok", timeout=10)
    assert "confirm=true" in server.run_command("rm -rf ./some-dir", timeout=10)
    assert "已被安全网硬禁止" in server.run_command("rm -rf /", timeout=10)
    assert "HOST_AGENT_UNAVAILABLE" in server.run_command(
        "echo x", backend="host", timeout=10)
    assert "未知执行后端" in server.run_command(
        "echo x", backend="not-a-backend", timeout=10)
    assert get_policy().mode in ("full_admin", "restricted")
    assert "container" in server.host_status() and "host-agent" in server.host_status()
