"""统一的执行后端抽象（唯一新增抽象，两个真实调用方：容器 / 宿主代理）。

现有工具只追加可选 ``backend`` 参数，默认 ``container``，行为保持不变。
宿主后端需要部署时显式挂载 socket 与共享密钥；未配置时返回明确错误，
绝不静默降级成容器执行并声称是宿主机。
"""
from __future__ import annotations

import os
from typing import Any, Callable, Protocol

from gateway.host.client import HostAgentClient, HostAgentError


class ExecutionBackend(Protocol):
    name: str

    def run(self, command: str, cwd: str, timeout: int, confirm: bool) -> str: ...
    def spawn(self, command: str, cwd: str, log_path: str) -> dict[str, Any]: ...
    def system_info(self) -> dict[str, Any]: ...
    def reconcile(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


class ContainerBackend:
    """包装既有 ``_exec`` / ``Popen``，不重写命令执行语义。"""

    name = "container"

    def __init__(self, runner: Callable[..., str] | None = None,
                 spawner: Callable[..., dict[str, Any]] | None = None):
        self.runner = runner
        self.spawner = spawner

    def run(self, command: str, cwd: str, timeout: int, confirm: bool) -> str:
        if self.runner is None:
            raise RuntimeError("容器后端未注入 runner")
        return self.runner(command, cwd=cwd, timeout=timeout, confirm=confirm)

    def spawn(self, command: str, cwd: str, log_path: str) -> dict[str, Any]:
        if self.spawner is None:
            raise RuntimeError("容器后端未注入 spawner")
        return self.spawner(command, cwd=cwd, log_path=log_path)

    def system_info(self) -> dict[str, Any]:
        from gateway.monitoring.system import collect
        return collect("container")

    def reconcile(self) -> dict[str, Any]:
        return {"backend": self.name, "reattached": 0,
                "note": "容器后端任务在进程内管理"}

    def close(self) -> None:
        return None


class HostBackend:
    """经 HMAC 认证的 Unix Socket 代理；无 TCP、无公网端口。"""

    name = "host-agent"

    def __init__(self, client: HostAgentClient | None = None):
        self.client = client or HostAgentClient()
        self.last_error = ""

    def _call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            self.last_error = ""
            return self.client.call(op, args)
        except HostAgentError as exc:
            self.last_error = str(exc)
            raise RuntimeError(
                f"HOST_AGENT_UNAVAILABLE: {exc}；请确认已启用 host profile、"
                "挂载授权目录并配置 MCP_HOST_AGENT_TOKEN") from exc

    def run(self, command: str, cwd: str, timeout: int, confirm: bool) -> str:
        data = self._call("run", {"command": command, "cwd": cwd,
                                  "timeout": timeout, "confirm": bool(confirm)})
        return (f"source=host-agent\n"
                f"exit={data.get('exit', 0)}\n"
                f"--- stdout ---\n{data.get('stdout', '')}\n"
                f"--- stderr ---\n{data.get('stderr', '')}")

    def spawn(self, command: str, cwd: str, log_path: str) -> dict[str, Any]:
        data = self._call("spawn", {"command": command, "cwd": cwd})
        return {"pid": data.get("pid"), "pgid": data.get("pgid"),
                "backend": self.name, "log_path": log_path}

    def system_info(self) -> dict[str, Any]:
        return self._call("system_info", {})

    def reconcile(self) -> dict[str, Any]:
        try:
            data = self._call("processes", {})
            return {"backend": self.name, "reattached": 0,
                    "processes": len(data.get("processes") or [])}
        except RuntimeError:
            return {"backend": self.name, "reattached": 0,
                    "error": self.last_error}

    def close(self) -> None:
        return None


_BACKENDS: dict[str, ExecutionBackend] = {}


def get_backend(name: str = "", *, runner=None, spawner=None) -> ExecutionBackend:
    """默认容器；显式 backend="host" 才走宿主机（无隐式降级）。"""
    key = (name or os.environ.get("MCP_EXEC_BACKEND", "container")).strip().lower()
    if key in ("container", "", "docker"):
        if "container" not in _BACKENDS or runner is not None:
            _BACKENDS["container"] = ContainerBackend(runner, spawner)
        return _BACKENDS["container"]
    if key in ("host", "host-agent", "host_agent"):
        backend = _BACKENDS.get("host-agent")
        if backend is None:
            backend = HostBackend()
            _BACKENDS["host-agent"] = backend
        return backend
    raise ValueError(f"未知执行后端 {key!r}（可选 container / host）")


def backend_status() -> dict[str, Any]:
    client = HostAgentClient()
    ok, reason = client.available()
    info: dict[str, Any] = {
        "default": os.environ.get("MCP_EXEC_BACKEND", "container"),
        "container": {"available": True, "source": "容器"},
        "host-agent": {"available": ok, "reason": reason,
                       "socket": client.socket_path, "transport": "unix-socket",
                       "no_tcp": True,
                       "note": "真实宿主权限需部署时由用户挂载授权目录并注入共享密钥"},
    }
    if ok:
        try:
            info["host-agent"]["ping"] = client.ping()
        except HostAgentError as exc:
            info["host-agent"]["available"] = False
            info["host-agent"]["reason"] = str(exc)
    return info
