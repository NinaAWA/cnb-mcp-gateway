"""任务状态持久化与重启重关联（V3 阶段 D）。

- 任务写入 ``task_state``（含 pid / pgid / git commit / output_path）；
- ``start_new_session=True`` + ``os.killpg`` 终止整个进程组，避免留下孙子进程；
- 重启后按 ``/proc/<pid>/environ`` 中的 MCP_TASK_ID 标记核对，
  防 PID 复用误判；无法重关联的如实标记 interrupted，不虚构运行中状态；
- 导出可复现 manifest（命令、工作目录、Git commit、输出路径、环境指纹）。
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from gateway.audit.events import utcnow
from gateway.audit.redaction import redact_text

_ENV_MARKER = "MCP_TASK_ID"


def _utc(epoch: float = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch or time.time()))


def git_commit(cwd: str) -> str:
    try:
        proc = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                              shell=False, capture_output=True, text=True,
                              timeout=5)
        return proc.stdout.strip()[:64] if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def env_fingerprint(env: dict[str, str] | None = None) -> str:
    """只记录环境键名的摘要，不记录任何值（避免凭据进入审计/清单）。"""
    keys = sorted(k for k in (env or os.environ) if not any(
        token in k.upper() for token in ("TOKEN", "SECRET", "PASSWORD", "KEY")))
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]


def process_matches(pid: int, task_id: str) -> bool:
    """核对 /proc/<pid>/environ 的标记，防 PID 复用。"""
    try:
        raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
    except OSError:
        return False
    marker = f"{_ENV_MARKER}={task_id}".encode()
    return marker in raw.split(b"\x00")


class TaskManager:
    def __init__(self, storage=None, lock: threading.Lock | None = None):
        self.storage = storage
        self.lock = lock or threading.Lock()
        self.tasks: dict[str, dict[str, Any]] = {}
        self._proc: dict[str, Any] = {}

    def _store(self):
        if self.storage is not None:
            return self.storage
        from gateway.audit.storage import get_storage
        return get_storage()

    def _persist(self, row: dict[str, Any]) -> None:
        keys = ["ts", "task_id", "pid", "pgid", "command", "cwd", "log_path",
                "status", "git_commit", "output_path", "detail"]
        values = {k: row.get(k, "") for k in keys}
        values.setdefault("ts", utcnow())
        cols = ",".join(values)
        self._store()._execute(
            f"INSERT INTO task_state ({cols}) "
            f"VALUES ({','.join('?' for _ in values)})",
            [values[k] for k in values])

    def child_env(self, task_id: str) -> dict[str, str]:
        return {**os.environ, _ENV_MARKER: task_id}

    def register(self, task_id: str, *, command: str, cwd: str,
                 log_path: str, proc, request_id: str = "",
                 output_path: str = "") -> dict[str, Any]:
        pid = int(getattr(proc, "pid", 0) or 0)
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = pid
        row = {
            "ts": utcnow(), "task_id": task_id, "pid": pid, "pgid": pgid,
            "command": redact_text(command)[:2000], "cwd": cwd,
            "log_path": log_path, "status": "running",
            "git_commit": git_commit(cwd), "output_path": output_path,
            "detail": json.dumps({"request_id": request_id,
                                  "started_at": _utc(),
                                  "env_fingerprint": env_fingerprint()},
                                 ensure_ascii=False),
        }
        with self.lock:
            self.tasks[task_id] = row
            self._proc[task_id] = proc
            self._persist(row)
        self._experiment(row)
        return row

    def _experiment(self, row: dict[str, Any]) -> None:
        try:
            self._store().add_experiment({
                "experiment_id": row["task_id"], "task_id": row["task_id"],
                "request_id": str(json.loads(row.get("detail") or "{}")
                                  .get("request_id") or ""),
                "trace_id": "", "ts": utcnow(),
                "name": redact_text(row["command"])[:80],
                "project_path": row["cwd"], "command": row["command"],
                "status": row["status"], "pid": row["pid"],
                "git_commit": row["git_commit"], "log_path": row["log_path"],
                "output_path": row["output_path"], "detail": row["detail"],
            })
        except Exception:  # noqa: BLE001
            pass

    def update_status(self, task_id: str, status: str,
                      detail: str = "") -> None:
        with self.lock:
            row = dict(self.tasks.get(task_id) or {})
            row.update({"ts": utcnow(), "task_id": task_id, "status": status,
                        "detail": detail or row.get("detail", "")})
            self.tasks[task_id] = row
            self._persist(row)

    def terminate(self, task_id: str) -> dict[str, Any]:
        """向整个进程组发 TERM，超时后 KILL；返回真实结果，不假报成功。"""
        with self.lock:
            row = dict(self.tasks.get(task_id) or {})
            proc = self._proc.get(task_id)
        pgid = int(row.get("pgid") or 0)
        pid = int(row.get("pid") or 0)
        if not pgid:
            return {"ok": False, "reason": "任务未注册或缺少进程组信息"}
        # /proc 中的任务标记是唯一可信依据：没有标记绝不 killpg，
        # 否则 PID 复用可能误杀同进程组内的无关进程（甚至守护进程自身）。
        if not process_matches(pid, task_id):
            return {"ok": False,
                    "reason": "进程不存在、PID 已被复用或缺少任务标记，拒绝误杀"}
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            self.update_status(task_id, "done", "进程已退出")
            return {"ok": True, "status": "already-exited"}
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not process_matches(pid, task_id):
                break
            time.sleep(0.1)
        if process_matches(pid, task_id):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.update_status(task_id, "killed", "进程组已终止（SIGTERM/SIGKILL）")
        return {"ok": True, "status": "killed", "pgid": pgid}

    def reconcile(self) -> dict[str, Any]:
        """启动时重关联：只认 /proc 中带同一 task_id 标记的存活进程。"""
        try:
            rows = self._store()._query(
                "SELECT * FROM task_state WHERE status='running' "
                "ORDER BY id DESC LIMIT 200")
        except Exception:  # noqa: BLE001
            return {"reattached": 0, "interrupted": 0, "error": "task_state 读取失败"}
        reattached, interrupted = 0, 0
        for row in rows:
            pid = int(row.get("pid") or 0)
            task_id = str(row.get("task_id") or "")
            if pid > 0 and process_matches(pid, task_id):
                reattached += 1
                self.tasks[task_id] = dict(row)
                continue
            interrupted += 1
            self._store()._execute(
                "INSERT INTO task_state (ts,task_id,pid,pgid,command,cwd,"
                "log_path,status,git_commit,output_path,detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [utcnow(), task_id, pid, row.get("pgid") or 0,
                 row.get("command") or "", row.get("cwd") or "",
                 row.get("log_path") or "", "interrupted",
                 row.get("git_commit") or "", row.get("output_path") or "",
                 "重启后 PID 标记不匹配，如实标记 interrupted（未虚构运行中状态）"])
        return {"reattached": reattached, "interrupted": interrupted,
                "checked": len(rows),
                "note": "重关联只认 /proc/<pid>/environ 的 MCP_TASK_ID 标记；"
                        "容器重建后无法恢复进程对象，仅恢复元数据"}

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            return dict(self.tasks[task_id]) if task_id in self.tasks else None

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            rows = self._store()._query(
                "SELECT * FROM (SELECT * FROM task_state ORDER BY id DESC LIMIT ?) "
                "ORDER BY id DESC", [max(1, min(limit, 500))])
        except Exception:  # noqa: BLE001
            return []
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest.setdefault(str(row.get("task_id")), row)
        return list(latest.values())

    def manifest(self, task_id: str) -> dict[str, Any]:
        row = self.get(task_id) or {}
        if not row:
            for item in self.list(500):
                if str(item.get("task_id")) == task_id:
                    row = item
                    break
        if not row:
            return {"ok": False, "reason": f"找不到任务 {task_id}"}
        try:
            extra = json.loads(row.get("detail") or "{}")
        except (ValueError, TypeError):
            extra = {}
        return {
            "ok": True, "task_id": task_id, "status": row.get("status"),
            "command": redact_text(row.get("command")), "cwd": row.get("cwd"),
            "git_commit": row.get("git_commit") or "(非 Git 仓库或未记录)",
            "output_path": row.get("output_path") or "(未声明)",
            "log_path": row.get("log_path"),
            "pid": row.get("pid"), "pgid": row.get("pgid"),
            "env_key_fingerprint": extra.get("env_fingerprint", ""),
            "started_at": extra.get("started_at", ""),
            "note": "仅记录环境键名指纹，不保存任何凭据值；复现前请核对数据与随机种子",
        }


_MANAGER: TaskManager | None = None


def get_task_manager() -> TaskManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = TaskManager()
    return _MANAGER
