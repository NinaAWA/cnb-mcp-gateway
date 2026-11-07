"""采集容器自身的 CPU / 内存 / 磁盘 / 进程 / GPU / Docker 状态。

重要：psutil 读到的是**当前容器**的视图，不是宿主机。所有返回值都带
source 与 scope_note，避免把「网关容器指标」「后台容器指标」「宿主机指标」
混为一谈。GPU 只有在容器能调用 nvidia-smi 时才报告；Docker 容器状态只有
在显式挂载 /var/run/docker.sock 后才可用。
"""

from __future__ import annotations

import http.client
import os
import shutil
import socket
import subprocess
from typing import Any

import psutil

SOURCE_LABELS = {
    "mcp-gateway": "MCP 网关容器（实际命令执行后端）",
    "admin-container": "管理后台容器（仅展示自身指标）",
    "host": "宿主机",
}


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 2.0):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):  # noqa: D102
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _gpu() -> dict[str, Any]:
    binary = shutil.which("nvidia-smi")
    if not binary:
        return {"available": False, "reason": "容器内无 nvidia-smi，未透传 GPU"}
    try:
        out = subprocess.run(
            [binary, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, check=True,
        ).stdout.strip()
        devices = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                devices.append({
                    "index": parts[0], "name": parts[1],
                    "utilization_percent": float(parts[2] or 0),
                    "memory_used_mb": float(parts[3] or 0),
                    "memory_total_mb": float(parts[4] or 0),
                })
        return {"available": bool(devices), "devices": devices}
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        return {"available": False, "reason": str(exc)[:200]}


def _docker() -> dict[str, Any]:
    sock = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
    if not os.path.exists(sock):
        return {
            "available": False,
            "reason": "未挂载 Docker socket；如需容器状态请显式挂载并评估安全风险",
        }
    try:
        conn = _UnixHTTPConnection(sock)
        conn.request("GET", "/containers/json?all=1")
        resp = conn.getresponse()
        import json
        data = json.loads(resp.read().decode("utf-8", "replace"))
        conn.close()
        return {
            "available": True,
            "containers": [
                {"name": (c.get("Names") or ["?"])[0].lstrip("/"),
                 "image": c.get("Image", ""), "state": c.get("State", ""),
                 "status": c.get("Status", "")}
                for c in data[:50]
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": repr(exc)[:200]}


def collect(source: str = "mcp-gateway") -> dict[str, Any]:
    """返回本容器的系统指标；任何单项失败都不影响其它指标。"""
    label = SOURCE_LABELS.get(source, source)
    payload: dict[str, Any] = {
        "source": source,
        "source_label": label,
        "scope_note": (
            "以下 CPU/内存/磁盘/进程指标来自「" + label + "」，不是宿主机全机指标；"
            "命令实际执行位置以 source 字段为准。"
        ),
        "available": True,
    }
    try:
        payload["cpu_percent"] = psutil.cpu_percent(interval=None)
        payload["cpu_cores"] = psutil.cpu_count()
        payload["load_average"] = list(os.getloadavg()) if hasattr(os, "getloadavg") else []
    except Exception as exc:  # noqa: BLE001
        payload["cpu_percent"] = None
        payload["cpu_error"] = repr(exc)

    try:
        vm = psutil.virtual_memory()
        payload["memory"] = {
            "total_mb": round(vm.total / 1048576, 1),
            "used_mb": round(vm.used / 1048576, 1),
            "percent": vm.percent,
        }
    except Exception as exc:  # noqa: BLE001
        payload["memory"] = {"error": repr(exc)}

    try:
        disks = []
        for part in psutil.disk_partitions(all=False)[:8]:
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append({
                    "mount": part.mountpoint,
                    "total_gb": round(usage.total / 1073741824, 2),
                    "used_gb": round(usage.used / 1073741824, 2),
                    "percent": usage.percent,
                })
            except (PermissionError, OSError):
                continue
        payload["disks"] = disks
    except Exception as exc:  # noqa: BLE001
        payload["disks"] = []
        payload["disk_error"] = repr(exc)

    try:
        processes = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            info = proc.info
            processes.append({
                "pid": info.get("pid"),
                "name": info.get("name", ""),
                "cpu_percent": round(float(info.get("cpu_percent") or 0), 1),
                "memory_percent": round(float(info.get("memory_percent") or 0), 2),
            })
        processes.sort(key=lambda p: p["memory_percent"], reverse=True)
        payload["process_count"] = len(processes)
        payload["top_processes"] = processes[:10]
    except Exception as exc:  # noqa: BLE001
        payload["process_count"] = 0
        payload["top_processes"] = []
        payload["process_error"] = repr(exc)

    payload["gpu"] = _gpu()
    payload["docker"] = _docker()
    payload["process_uptime_seconds"] = int(
        __import__("time").time() - psutil.Process().create_time()
    )
    return payload
