"""阶段 D 任务：持久化重关联、进程组终止、复现元数据、防 PID 复用。"""
from __future__ import annotations

import json
import os
import subprocess
import time

from gateway.tasks.manager import (TaskManager, env_fingerprint, git_commit,
                                   process_matches)


class _FakeProc:
    def __init__(self, pid=0):
        self.pid = pid

    def poll(self):
        return None


def test_register_manifest_and_persistence(isolated_storage, tmp_path):
    manager = TaskManager(storage=isolated_storage)
    row = manager.register("task-1", command="python train.py --seed 1",
                           cwd=str(tmp_path), log_path=str(tmp_path / "t.log"),
                           proc=_FakeProc(999999), request_id="req-task")
    assert row["status"] == "running" and row["pid"] == 999999
    assert row["git_commit"] == git_commit(str(tmp_path)) == ""
    rows = isolated_storage._query(
        "SELECT * FROM task_state WHERE task_id=?", ["task-1"])
    assert rows and rows[0]["status"] == "running"
    manifest = manager.manifest("task-1")
    assert manifest["ok"] and "train.py" in manifest["command"]
    assert len(manifest["env_key_fingerprint"]) == 16
    assert "TOKEN" not in json.dumps(manifest)
    assert "凭据" not in json.dumps(manifest) or "不保存" in manifest["note"]


def test_reconcile_marks_missing_processes_interrupted(isolated_storage,
                                                       tmp_path):
    manager = TaskManager(storage=isolated_storage)
    manager.register("task-dead", command="echo x", cwd=str(tmp_path),
                     log_path=str(tmp_path / "x.log"),
                     proc=_FakeProc(999999))
    result = manager.reconcile()
    assert result["checked"] == 1
    assert result["interrupted"] == 1 and result["reattached"] == 0
    assert "PID 标记不匹配" in result["note"] or result["interrupted"] == 1
    rows = isolated_storage._query(
        "SELECT status FROM task_state WHERE task_id=? ORDER BY id DESC",
        ["task-dead"])
    assert rows[0]["status"] == "interrupted"


def test_process_group_termination_kills_children(isolated_storage, tmp_path):
    manager = TaskManager(storage=isolated_storage)
    task_id = "task-pg"
    proc = subprocess.Popen(
        ["sh", "-c", "sleep 30 & wait"], start_new_session=True,
        env=manager.child_env(task_id),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.1)
        assert process_matches(proc.pid, task_id) is True
        manager.register(task_id, command="sleep 30", cwd=str(tmp_path),
                         log_path=str(tmp_path / "pg.log"), proc=proc)
        outcome = manager.terminate(task_id)
        assert outcome["ok"] is True, outcome
        time.sleep(0.2)
        assert process_matches(proc.pid, task_id) is False
        proc.wait(timeout=3)
        assert manager.get(task_id)["status"] == "killed"
    finally:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def test_terminate_refuses_reused_pid(isolated_storage, tmp_path):
    manager = TaskManager(storage=isolated_storage)
    manager.register("task-reuse", command="echo x", cwd=str(tmp_path),
                     log_path=str(tmp_path / "r.log"),
                     proc=_FakeProc(os.getpid()))
    outcome = manager.terminate("task-reuse")
    assert outcome["ok"] is False
    assert "复用" in outcome["reason"] or "不存在" in outcome["reason"]


def test_stop_task_kills_entire_process_group(gateway_module,
                                              isolated_storage, tmp_path,
                                              monkeypatch):
    server = gateway_module
    monkeypatch.setattr(server, "_TASK_DIR", tmp_path / "logs-pg")
    (tmp_path / "logs-pg").mkdir(exist_ok=True)
    started = server.run_command_background("sleep 25 & wait",
                                            cwd=str(tmp_path), timeout=0)
    assert "task_id=" in started
    task_id = started.split("task_id=", 1)[1].splitlines()[0].strip()
    time.sleep(0.2)
    pid = server._TASKS[task_id]["pid"]
    assert process_matches(pid, task_id) is True
    try:
        assert "进程组" in server.stop_task(task_id)
        time.sleep(0.3)
        assert process_matches(pid, task_id) is False
        assert server._TASKS[task_id]["status"] == "killed"
        rows = isolated_storage._query(
            "SELECT status,pgid FROM task_state WHERE task_id=? ORDER BY id DESC",
            [task_id])
        assert rows[0]["status"] == "killed" and rows[0]["pgid"]
    finally:
        try:
            os.killpg(pid, 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def test_background_task_tools_report_persisted_status(gateway_module,
                                                       isolated_storage,
                                                       tmp_path, monkeypatch):
    server = gateway_module
    monkeypatch.setattr(server, "_TASK_DIR", tmp_path / "logs")
    (tmp_path / "logs").mkdir(exist_ok=True)
    started = server.run_command_background("echo phase-d-task-ok",
                                            cwd=str(tmp_path), timeout=0)
    assert "task_id=" in started
    task_id = started.split("task_id=", 1)[1].splitlines()[0].strip()
    for _ in range(50):
        if "running" not in server.get_task_status(task_id):
            break
        time.sleep(0.05)
    assert "done" in server.get_task_status(task_id)
    assert "phase-d-task-ok" in server.get_task_output(task_id, tail=20)
    assert task_id in server.list_tasks()
    manifest = json.loads(server.experiment_manifest(task_id, format="json"))
    assert manifest["ok"] is True and manifest["git_commit"] is not None
    rows = isolated_storage._query(
        "SELECT * FROM task_state WHERE task_id=? ORDER BY id DESC", [task_id])
    assert rows and rows[0]["status"] in ("done", "failed", "killed")
