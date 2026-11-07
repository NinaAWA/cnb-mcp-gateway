"""独立 Host Agent 进程：0600 Unix Socket、无 TCP、无公网端口。

真实宿主权限只能在部署时由用户显式挂载目录并注入共享密钥授予；容器内验收
只能覆盖协议与模拟，不能声称真实宿主权限已验证。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .protocol import (ALLOWED_OPS, DEFAULT_SOCKET, decode_frame, encode_frame,
                       error, verify_payload)

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

try:
    from gateway.audit.redaction import redact_text
except Exception:  # noqa: BLE001
    def redact_text(value, **_kw):
        return str(value)


def _roots(raw: str) -> list[Path]:
    return [Path(p).resolve() for p in raw.split(os.pathsep) if p.strip()]


class HostAgent:
    def __init__(self, socket_path: str = "", secret: str = "",
                 allowed_roots: str = "", default_timeout: int = 60):
        self.socket_path = socket_path or os.environ.get(
            "MCP_HOST_AGENT_SOCKET", DEFAULT_SOCKET)
        self.secret = secret or os.environ.get("MCP_HOST_AGENT_TOKEN", "")
        self.roots = _roots(allowed_roots or os.environ.get(
            "MCP_HOST_AGENT_ROOTS", "/workspace"))
        self.default_timeout = default_timeout
        self._server: socket.socket | None = None
        self._running = False
        self._children: dict[int, subprocess.Popen] = {}

    def _in_roots(self, path: str) -> bool:
        try:
            candidate = Path(path).resolve()
        except OSError:
            return False
        if not self.roots:
            return False
        return any(candidate == root or root in candidate.parents
                   for root in self.roots)

    def bind(self) -> None:
        if not self.secret:
            raise RuntimeError("MCP_HOST_AGENT_TOKEN 未配置，拒绝以无认证方式监听")
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(16)
        self._server = server
        self._running = True

    def serve_forever(self) -> None:
        if self._server is None:
            self.bind()
        assert self._server is not None
        while self._running:
            try:
                conn, _ = self._server.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def stop(self) -> None:
        self._running = False
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        Path(self.socket_path).unlink(missing_ok=True)

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            buf = bytearray()
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if b"\n" in buf:
                        break
                    if len(buf) > 1024 * 1024:
                        conn.sendall(encode_frame(error("BAD_FRAME")))
                        return
                if not buf:
                    return
                frame = decode_frame(bytes(buf).split(b"\n", 1)[0])
                sig = frame.pop("sig", "")
                if not verify_payload(self.secret, frame, sig):
                    conn.sendall(encode_frame(error("AUTH_FAILED")))
                    return
                response = self.execute(str(frame.get("op", "")),
                                        dict(frame.get("args") or {}),
                                        str(frame.get("request_id") or ""))
                conn.sendall(encode_frame(response))
            except (ValueError, TypeError) as exc:
                conn.sendall(encode_frame(error("BAD_FRAME", str(exc))))
            except OSError as exc:
                conn.sendall(encode_frame(error("EXEC_FAILED", str(exc)[:200])))

    def execute(self, op: str, args: dict[str, Any],
                request_id: str = "") -> dict[str, Any]:
        if op not in ALLOWED_OPS:
            return error("BAD_OP", f"{op}")
        try:
            if op == "ping":
                return {"ok": True, "data": {"role": "host-agent",
                                            "no_tcp": True,
                                            "roots": [str(r) for r in self.roots],
                                            "timestamp": time.time()}}
            if op == "system_info":
                return {"ok": True, "data": self._system_info()}
            if op == "list_dir":
                path = str(args.get("path") or "")
                if not self._in_roots(path):
                    return error("BAD_ROOT", path)
                return {"ok": True, "data": {"entries": sorted(os.listdir(path))[:200]}}
            if op == "run":
                return self._run(args)
            if op == "git":
                return self._git(args)
            if op == "spawn":
                return self._spawn(args)
            if op == "kill":
                return self._kill(args)
            if op == "processes":
                return {"ok": True, "data": {"processes": self._processes()}}
            if op == "docker_ps":
                return self._command(["docker", "ps", "--format",
                                      "{{.Names}}\t{{.Status}}"], None, 15)
            return error("BAD_OP", op)
        except Exception as exc:  # noqa: BLE001
            return error("EXEC_FAILED", repr(exc)[:300])

    def _system_info(self) -> dict[str, Any]:
        if psutil is None:
            return {"cpu_percent": None, "memory_percent": None,
                    "disk_percent": None,
                    "gpu": {"status": "unavailable", "reason": "psutil 未安装"},
                    "source": "host-agent", "scope_note": "数据来自宿主机"}
        vm = psutil.virtual_memory()
        data = {
            "cpu_percent": psutil.cpu_percent(interval=0.05),
            "memory_percent": vm.percent,
            "disk_percent": psutil.disk_usage("/").percent,
            "gpu": {"status": "unavailable",
                    "reason": "Host Agent 未配置 GPU 采集器（如实报告，不虚构数据）"},
            "source": "host-agent", "scope_note": "数据来自宿主机（经授权）",
        }
        return data

    def _processes(self) -> list[dict[str, Any]]:
        if psutil is None:
            return []
        out = []
        for proc in list(psutil.process_iter(["pid", "name", "cpu_percent",
                                              "memory_percent"]))[:50]:
            out.append({"pid": proc.info.get("pid"),
                        "name": redact_text(proc.info.get("name") or "")[:80],
                        "cpu_percent": proc.info.get("cpu_percent"),
                        "memory_percent": proc.info.get("memory_percent")})
        return out

    def _command(self, argv: list[str], cwd: str | None,
                 timeout: int) -> dict[str, Any]:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, shell=False)
        return {"ok": proc.returncode == 0,
                "data": {"exit": proc.returncode,
                         "stdout": redact_text(proc.stdout or "")[:20000],
                         "stderr": redact_text(proc.stderr or "")[:4000]},
                "error": "" if proc.returncode == 0 else redact_text(proc.stderr)}

    def _run(self, args: dict[str, Any]) -> dict[str, Any]:
        cwd = str(args.get("cwd") or (self.roots[0] if self.roots else ""))
        if not self._in_roots(cwd):
            return error("BAD_ROOT", cwd)
        command = str(args.get("command") or "")
        if not command:
            return error("EXEC_FAILED", "command 为空")
        timeout = max(1, min(int(args.get("timeout") or self.default_timeout), 600))
        proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True,
                              text=True, timeout=timeout,
                              start_new_session=True)
        return {"ok": True,
                "data": {"exit": proc.returncode,
                         "stdout": redact_text(proc.stdout or "")[:20000],
                         "stderr": redact_text(proc.stderr or "")[:4000],
                         "source": "host-agent"}}

    def _git(self, args: dict[str, Any]) -> dict[str, Any]:
        repo = str(args.get("repo") or "")
        if not self._in_roots(repo):
            return error("BAD_ROOT", repo)
        argv = ["git", "-C", repo] + [str(x) for x in (args.get("argv") or [])]
        return self._command(argv, None, 60)

    def _spawn(self, args: dict[str, Any]) -> dict[str, Any]:
        cwd = str(args.get("cwd") or (self.roots[0] if self.roots else ""))
        if not self._in_roots(cwd):
            return error("BAD_ROOT", cwd)
        proc = subprocess.Popen(str(args.get("command") or ""), shell=True,
                                cwd=cwd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
        pgid = os.getpgid(proc.pid)
        self._children[proc.pid] = proc
        return {"ok": True, "data": {"pid": proc.pid, "pgid": pgid,
                                     "source": "host-agent"}}

    def _kill(self, args: dict[str, Any]) -> dict[str, Any]:
        pid = int(args.get("pid") or 0)
        if pid <= 0 or pid not in self._children:
            return error("EXEC_FAILED", "仅允许终止本 Agent 启动的进程")
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        self._children.pop(pid, None)
        return {"ok": True, "data": {"pid": pid, "status": "terminated"}}


def main() -> None:
    agent = HostAgent()
    agent.bind()
    print(f"[host-agent] 已监听 Unix Socket {agent.socket_path}（0600，无 TCP 端口）；"
          f"授权目录: {[str(r) for r in agent.roots]}", flush=True)
    try:
        agent.serve_forever()
    finally:
        agent.stop()


if __name__ == "__main__":
    main()
