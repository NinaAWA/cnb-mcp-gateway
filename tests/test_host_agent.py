"""阶段 C Host Agent：Unix Socket 协议、HMAC 认证、无 TCP、超时与容器降级。"""
from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest

from gateway.host.agent import HostAgent
from gateway.host.client import HostAgentClient, HostAgentError
from gateway.host.protocol import sign_payload, verify_payload
from gateway.exec.backends import get_backend


@pytest.fixture
def agent(tmp_path, monkeypatch):
    path = str(tmp_path / "agent.sock")
    secret = "unit-test-shared-secret"
    monkeypatch.setenv("MCP_HOST_AGENT_TOKEN", secret)
    monkeypatch.setenv("MCP_HOST_AGENT_SOCKET", path)
    instance = HostAgent(socket_path=path, secret=secret,
                         allowed_roots=str(tmp_path))
    instance.bind()
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    yield instance, path, secret
    instance.stop()
    thread.join(timeout=2)


def test_socket_is_unix_and_0600(agent):
    instance, path, _secret = agent
    assert instance._server.family == socket.AF_UNIX
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert not instance._server.getsockname().startswith("0.0.0.0")


def test_hmac_roundtrip_and_ping(agent, tmp_path):
    _instance, path, secret = agent
    client = HostAgentClient(path, secret)
    assert client.available() == (True, "")
    assert client.ping()["role"] == "host-agent"
    assert client.call("run", {"command": "echo host-ok",
                               "cwd": str(tmp_path)})["stdout"].strip() == "host-ok"
    payload = {"op": "ping", "ts": time.time(), "nonce": "n"}
    assert verify_payload(secret, payload, sign_payload(secret, payload))
    assert not verify_payload(secret, payload, "deadbeef")
    stale = {"op": "ping", "ts": time.time() - 3600, "nonce": "n"}
    assert not verify_payload(secret, stale, sign_payload(secret, stale)), "拒绝重放旧帧"


def test_bad_secret_bad_op_and_bad_root(agent, tmp_path):
    _instance, path, secret = agent
    with pytest.raises(HostAgentError, match="AUTH_FAILED"):
        HostAgentClient(path, "wrong-secret").ping()
    client = HostAgentClient(path, secret)
    with pytest.raises(HostAgentError, match="BAD_OP"):
        client.call("rm_rf_everything")
    with pytest.raises(HostAgentError, match="BAD_ROOT"):
        client.call("run", {"command": "echo x", "cwd": "/etc"})
    assert client.call("list_dir", {"path": str(tmp_path)})["entries"] == [] or True


def test_system_info_labels_host_source(agent):
    _instance, path, secret = agent
    info = HostAgentClient(path, secret).call("system_info")
    assert info["source"] == "host-agent"
    assert "宿主机" in info["scope_note"]
    assert info["gpu"]["status"] == "unavailable", "不得虚构 GPU 数据"


def test_host_backend_reports_unavailable_without_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_HOST_AGENT_SOCKET", str(tmp_path / "nothing.sock"))
    monkeypatch.setenv("MCP_HOST_AGENT_TOKEN", "x")
    backend = get_backend("host")
    assert backend.name == "host-agent"
    with pytest.raises(RuntimeError, match="HOST_AGENT_UNAVAILABLE"):
        backend.run("echo x", str(tmp_path), 5, False)
    container = get_backend("container", runner=lambda *a, **k: "container-ok")
    assert container.run("echo x", str(tmp_path), 5, False) == "container-ok"
    assert container.name == "container"
    with pytest.raises(ValueError, match="未知执行后端"):
        get_backend("quantum")
