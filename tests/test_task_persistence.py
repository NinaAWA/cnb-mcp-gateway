"""v1.1 长任务真实测试：立即返回 ID、真实退出码、取消、重启后日志与未知态。"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "docker") not in sys.path:
    sys.path.insert(0, str(ROOT / "docker"))

os.environ.setdefault("MCP_TOKEN", "unit-test-token")
os.environ.setdefault("MCP_ALLOWED_PREFIXES", "")

from gateway.tasks.store import (  # noqa: E402
    TaskRecord, TaskStore, pid_is_alive, pid_start_time,
)


@pytest.fixture
def srv(tmp_path, monkeypatch, isolated_storage):
    logs = (tmp_path / "task-logs").resolve()
    logs.mkdir()
    db = tmp_path / "tasks.db"
    monkeypatch.setenv("MCP_TASK_LOG_DIR", str(logs))
    monkeypatch.setenv("AUDIT_DB_PATH", str(db))

    name = f"mcp_gateway_task_test_{abs(hash(str(tmp_path)))}"
    spec = importlib.util.spec_from_file_location(name, ROOT / "docker" / "server.py")
    server = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(server)

    store = TaskStore(db_path=str(db))
    monkeypatch.setattr(server, "_TASK_STORE", store)
    monkeypatch.setattr(server, "_TASK_DIR", logs)
    return server, store, logs


def _wait_done(server, task_id: str, timeout: float = 8.0) -> str:
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        status = server.get_task_status(task_id)
        if "status=done" in status:
            return status
        time.sleep(0.1)
    return status


def test_task_id_log_and_real_exit_code_persist(srv):
    server, store, _logs = srv
    out = server.run_command_background("echo persisted-line; exit 5", cwd="/tmp")
    assert "task_id=" in out
    task_id = out.split("task_id=", 1)[1].split("\n", 1)[0].strip()
    status = _wait_done(server, task_id)
    assert "status=done" in status
    assert "exit_code=5" in status
    assert "persisted-line" in server.get_task_output(task_id)
    row = store.get(task_id)
    assert row is not None and row.status == "done" and row.exit_code == 5
    assert row.log_path and Path(row.log_path).is_file()


def test_cancel_records_killed_state(srv):
    server, store, _logs = srv
    out = server.run_command_background("sleep 30", cwd="/tmp")
    task_id = out.split("task_id=", 1)[1].split("\n", 1)[0].strip()
    assert "running" in server.get_task_status(task_id)
    result = server.stop_task(task_id)
    assert "已终止" in result
    row = store.get(task_id)
    assert row is not None and row.status == "killed"
    # 终止后取到真实退出码（SIGTERM 通常为 -15），绝不臆测为成功
    assert row.exit_code is not None and row.exit_code != 0


def test_restart_marks_orphans_unknown_and_logs_stay_readable(srv):
    server, store, logs = srv
    task_id = "orphan-task-0001"
    log_path = logs / f"{task_id}.log"
    log_path.write_text("line-before-restart\n", encoding="utf-8")
    store.create(TaskRecord(
        task_id=task_id, command="python3 long_job.py", cwd="/tmp",
        status="running", log_path=str(log_path), started_at=time.time() - 60,
        pid=999999, proc_start="123456",
    ))

    # 模拟服务重启：全新的 TaskStore 实例读同一个 WAL 数据库
    restarted = TaskStore(db_path=store.db_path)
    changed = restarted.mark_orphans_unknown()
    assert changed == 1
    row = restarted.get(task_id)
    assert row.status == "unknown"
    assert row.exit_code is None
    assert "状态未知" in row.detail

    server._TASK_STORE = restarted
    status = server.get_task_status(task_id)
    assert "status=unknown" in status
    assert "exit_code=unknown" in status
    assert "status=done" not in status
    assert "exit_code=0" not in status
    assert "source=sqlite" in status
    assert "line-before-restart" in server.get_task_output(task_id)
    assert "状态未知" in server.stop_task(task_id)


def test_alive_process_is_never_marked_unknown(srv):
    _server, store, logs = srv
    start = pid_start_time(os.getpid())
    assert start
    assert pid_is_alive(os.getpid(), start)
    assert not pid_is_alive(os.getpid(), "0")
    assert not pid_is_alive(999999, start)
    store.create(TaskRecord(
        task_id="still-running", command="sleep 5", cwd="/tmp", status="running",
        log_path=str(logs / "still-running.log"), started_at=time.time(),
        pid=os.getpid(), proc_start=start,
    ))
    # pid 确实活着也只标 unknown：本进程没有它的 Popen，无法观测真实退出码，
    # 绝不因为「进程还在」就写成 done / success。
    store.mark_orphans_unknown()
    assert store.get("still-running").status == "unknown"
    assert store.get("still-running").exit_code is None


def test_task_list_recent_returns_persisted_rows(srv):
    _server, store, logs = srv
    for i in range(3):
        store.create(TaskRecord(
            task_id=f"t-{i}", command="echo x", status="done",
            log_path=str(logs / f"t-{i}.log"), started_at=time.time() + i,
            exit_code=0,
        ))
    rows = store.list_recent(10)
    assert len(rows) == 3
    assert {r.task_id for r in rows} == {"t-0", "t-1", "t-2"}
    assert rows[0].task_id == "t-2"


def test_unknown_task_can_be_cancelled_when_pid_confirmed(srv):
    """回归：重启后 unknown 曾被 stop_task 提前返回，确认存活的任务也无法取消。"""
    import subprocess

    server, store, logs = srv
    proc = subprocess.Popen(["sleep", "60"])
    try:
        start = pid_start_time(proc.pid)
        assert start
        task_id = "unknown-alive-0001"
        log_path = logs / f"{task_id}.log"
        log_path.write_text("line-before-restart\n", encoding="utf-8")
        store.create(TaskRecord(
            task_id=task_id, command="sleep 60", cwd="/tmp", status="unknown",
            log_path=str(log_path), started_at=time.time() - 10,
            pid=proc.pid, proc_start=start,
        ))
        result = server.stop_task(task_id)
        assert "已向" in result and "TERM" in result
        row = store.get(task_id)
        assert row is not None and row.status == "killed"
        assert row.exit_code is None  # 无 Popen 可观测，绝不臆测退出码
        assert "line-before-restart" in server.get_task_output(task_id)
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=10)


def test_reaper_finalizes_even_when_audit_disabled(srv, monkeypatch):
    """回归：reaper 曾只在审计开启时启动，状态会长期停在 running。"""
    server, store, _logs = srv
    monkeypatch.setattr(server, "AUDIT_ENABLED", False)
    out = server.run_command_background("echo no-audit; exit 2", cwd="/tmp")
    task_id = out.split("task_id=", 1)[1].split("\n", 1)[0].strip()
    row = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = store.get(task_id)
        if row and row.status == "done":
            break
        time.sleep(0.2)
    assert row is not None and row.status == "done" and row.exit_code == 2


def test_background_stdin_and_separate_stderr(srv):
    server, _store, logs = srv
    out = server.run_command_background(
        "cat; echo oops >&2; exit 4", cwd="/tmp", stdin="from-bg-stdin\n")
    task_id = out.split("task_id=", 1)[1].split("\n", 1)[0].strip()
    status = _wait_done(server, task_id)
    assert "status=done" in status and "exit_code=4" in status
    text = server.get_task_output(task_id, tail=0)
    assert "from-bg-stdin" in text
    assert "--- stderr ---" in text and "oops" in text
    assert "oops" not in (logs / f"{task_id}.log").read_text()
    assert "oops" in (logs / f"{task_id}.err.log").read_text()
    assert not list(logs.glob(f".{task_id}.stdin-*"))  # stdin 明文不落盘


def test_tail_task_incremental_offset(srv):
    import re

    server, _store, _logs = srv
    out = server.run_command_background(
        "echo first-tick; sleep 0.5; echo second-tick", cwd="/tmp")
    task_id = out.split("task_id=", 1)[1].split("\n", 1)[0].strip()
    first = server.tail_task(task_id, offset_bytes=0, follow_seconds=5)
    assert "first-tick" in first
    offset = int(re.search(r"next_offset_bytes=(\d+)", first).group(1))
    second = server.tail_task(task_id, offset_bytes=offset, follow_seconds=5)
    assert "first-tick" not in second
    assert "second-tick" in second
