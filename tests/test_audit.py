"""统一审计核心：脱敏、大输出引用、SQLite WAL、查询/归档、降级与 REST 入口。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway.audit.events import clear_trace, utcnow
from gateway.audit.redaction import (
    REDACTED, ArtifactStore, fingerprint, redact_json, redact_text,
)

FAR_PAST = "2001-01-01T00:00:00.000+00:00"
FAR_FUTURE = "2099-01-01T00:00:00.000+00:00"


def test_redaction_masks_all_secret_shapes():
    payload = {
        "api_key": "tvly-abcdefghijklmnop",
        "nested": {"password": "hunter2", "command": "echo token=plaintext"},
        "url": "https://user:pass@example.com",
    }
    out = redact_json(payload)
    assert REDACTED in out
    for secret in ("tvly-abcdefghijklmnop", "hunter2", "plaintext",
                   "user:pass@", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"):
        assert secret not in out
    assert REDACTED in redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    assert REDACTED in redact_text("MY_SECRET=abcdef")
    assert REDACTED in redact_text("-----BEGIN RSA PRIVATE KEY-----abcd-----END RSA PRIVATE KEY-----")
    assert REDACTED in redact_text("AKIAIOSFODNN7EXAMPLE")
    assert fingerprint("token") == fingerprint("token") != "token"


def test_derive_status_confirm_gate_is_denied():
    from gateway.audit.enrich import derive_status

    rest_gate = "⚠️ 这是破坏性指令，执行前需明确确认。请带上 confirm=true 重新调用本工具"
    mcp_gate = "⚠️ 破坏性指令，后台执行前需确认。请带 confirm=true 重新调用"
    assert derive_status("run_command", rest_gate, None) == "DENIED"
    assert derive_status("run_command_background", mcp_gate, None) == "DENIED"
    assert derive_status("run_command", "exit=0\nok", 0) == "SUCCESS"
    assert derive_status("run_command", "exit=1\nfailed", 1) == "FAILED"
    assert derive_status("run_command", "timed out", None) == "TIMEOUT"


def test_artifact_store_roundtrip(tmp_path):
    store = ArtifactStore(tmp_path, threshold=64)
    small = store.store("short")
    assert small["ref"] == "" and small["text"] == "short"
    big = store.store("x" * 5000)
    assert big["ref"].startswith("artifact://") and big["size"] == 5000
    assert len(big["sha256"]) == 64
    assert store.read(big["ref"]) == "x" * 5000
    assert store.read("artifact:///etc/passwd") is None
    assert store.read("") is None


def test_storage_wal_crud_and_queries(isolated_storage, make_call):
    store = isolated_storage
    assert store.health()["healthy"] is True
    assert store.health()["backend"] == "sqlite-wal"
    assert store._query("PRAGMA journal_mode")[0]["journal_mode"].lower() == "wal"

    for i, ms in enumerate((300, 20, 100)):
        make_call(store, request_id=f"req_{i}", duration_ms=ms,
                  status="FAILED" if i == 0 else "SUCCESS",
                  result="hello world", arguments='{"token":"***REDACTED***"}')

    rows, total = store.query_calls({})
    assert total == 3 and len(rows) == 3
    rows, _ = store.query_calls({}, sort="duration_ms", order="asc")
    assert [r["duration_ms"] for r in rows] == [20, 100, 300]
    rows, _ = store.query_calls({"status": "SUCCESS"}, limit=1)
    assert len(rows) == 1
    rows, _ = store.query_calls({"keyword": "hello"})
    assert len(rows) == 3
    assert store.get_call("req_0")["tool_name"] == "run_command"

    stats = store.stats(FAR_PAST, FAR_FUTURE)
    assert stats["total"] == 3 and stats["by_status"]["FAILED"] == 1
    series = store.time_series(FAR_PAST, FAR_FUTURE)
    assert len(series["calls"]) == 24 and sum(series["calls"]) == 3
    assert len(store.duration_distribution(FAR_PAST)) == 5
    assert list(store.iter_calls({}, batch=2))


def test_interrupted_archive_backup_restore(isolated_storage, make_call, tmp_path):
    store = isolated_storage
    make_call(store, request_id="req_running", status="RUNNING")
    assert store.get_call("req_running")["status"] == "RUNNING"
    assert store.mark_interrupted() >= 1
    assert store.get_call("req_running")["status"] == "INTERRUPTED"

    make_call(store, request_id="req_old", timestamp_start=FAR_PAST,
              timestamp_end=FAR_PAST)
    result = store.archive_and_prune(1, 0)
    assert result["archived"] >= 1
    assert store.get_call("req_old") is None
    archived = list((tmp_path / "archive").glob("tool_calls-*.jsonl"))
    assert archived and "req_old" in archived[0].read_text(encoding="utf-8")

    backup = store.backup(str(tmp_path / "backup" / "audit.db"))
    assert Path(backup).is_file()
    assert store.restore(backup) is True
    assert store.health()["healthy"] is True


def test_write_failure_buffers_then_recovers(isolated_storage):
    store = isolated_storage
    original = store.db_path
    store.db_path = "/proc/self/no-such-dir/audit.db"
    assert store.add_event("tool_call", detail="buffered") is False
    assert store.health()["healthy"] is False
    assert store.health()["buffered"] >= 1
    assert store.available() is True
    store.db_path = original
    assert store.add_event("tool_call", detail="recovered") is True
    assert store.health()["healthy"] is True
    assert store.health()["buffered"] == 0
    details = [e["detail"] for e in store.recent_events(0, 100)]
    assert "buffered" in details and "recovered" in details


def test_trace_call_covers_rest_entry_and_derives_git(isolated_storage, monkeypatch):
    from gateway.audit import middleware as mw

    monkeypatch.setattr(mw, "get_storage", lambda: isolated_storage)
    try:
        with mw.trace_call("run_command",
                           {"command": "git status", "token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"},
                           source="rest") as rec:
            rec.ok("On branch main")
        rows, total = isolated_storage.query_calls({"source": "rest"})
        assert total == 1
        row = rows[0]
        assert row["status"] == "SUCCESS"
        assert "ghp_" not in row["arguments"] and REDACTED in row["arguments"]
        assert len(isolated_storage.git_ops()) == 1
        assert isolated_storage.git_ops()[0]["operation"] == "status"
    finally:
        clear_trace()


def test_large_output_is_stored_as_protected_reference(isolated_storage, monkeypatch):
    from gateway.audit import middleware as mw

    monkeypatch.setattr(mw, "get_storage", lambda: isolated_storage)
    huge = "A" * 60000 + " ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    try:
        with mw.trace_call("run_command", {"command": "echo big"}, source="rest") as rec:
            rec.ok(huge)
        row = isolated_storage.query_calls({})[0][0]
        assert row["result_ref"].startswith("artifact://")
        assert row["result_size"] > 32768
        assert len(row["result"] or "") < 5000
        full = isolated_storage.artifacts.read(row["result_ref"])
        assert "ghp_" not in full and REDACTED in full
        assert len(row["result"]) < len(full)
    finally:
        clear_trace()


def test_archive_over_5000_rows_is_complete(isolated_storage, bulk_calls,
                                            make_call, tmp_path, monkeypatch):
    from gateway.audit import storage as storage_mod

    monkeypatch.setattr(storage_mod, "ARCHIVE_BATCH", 1000)
    bulk_calls(isolated_storage, 6000, timestamp=FAR_PAST)
    make_call(isolated_storage, request_id="req_recent")

    result = isolated_storage.archive_and_prune(1, 0)
    assert result["errors"] == []
    assert result["archived"] == 6000, result
    assert result["archived_by_table"]["tool_calls"] == 6000

    files = sorted((tmp_path / "archive").glob("tool_calls-*.jsonl"))
    assert len(files) >= 6, "超过单批上限时应分批循环归档"
    archived = [json.loads(line) for path in files
                for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(archived) == 6000
    assert {row["request_id"] for row in archived} == {f"bulk_{i}" for i in range(6000)}
    remaining = isolated_storage._query("SELECT request_id FROM tool_calls")
    assert [row["request_id"] for row in remaining] == ["req_recent"]


def test_archive_covers_all_history_tables_and_keeps_settings(isolated_storage, tmp_path):
    old = FAR_PAST
    fixtures = (
        ("audit_events", {"ts": old, "kind": "legacy", "status": "OK"}),
        ("background_tasks", {"ts": old, "task_id": "t_old", "status": "done"}),
        ("git_operations", {"ts": old, "operation": "push", "repo_name": "repo"}),
        ("experiment_runs", {"ts": old, "experiment_id": "e_old", "status": "done"}),
        ("approval_events", {"ts": old, "approval_id": "a_old", "action": "deny"}),
        ("login_attempts", {"ts": old, "username": "u", "outcome": "ok"}),
        ("export_jobs", {"ts": old, "job_id": "j_old", "status": "done"}),
        ("admin_sessions", {"session_id": "s_old", "expires_at": old}),
        ("client_sessions", {"session_id": "c_old", "last_seen": old, "status": "active"}),
    )
    for table, values in fixtures:
        assert isolated_storage._insert(table, values)
    isolated_storage.set_setting("theme", "auto")

    result = isolated_storage.archive_and_prune(1, 0)
    assert result["errors"] == []
    for table, _ in fixtures:
        assert result["archived_by_table"].get(table) == 1, table
        assert isolated_storage._query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] == 0, table
    assert isolated_storage.get_settings()["theme"] == "auto", "配置表不应被保留策略清理"
    archived = "\n".join(path.read_text(encoding="utf-8")
                         for path in (tmp_path / "archive").glob("*.jsonl"))
    assert "t_old" in archived and "j_old" in archived


def test_archive_failure_keeps_all_rows(isolated_storage, bulk_calls, tmp_path, monkeypatch):
    bulk_calls(isolated_storage, 1200, timestamp=FAR_PAST)

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(isolated_storage, "_write_archive", broken)
    result = isolated_storage.archive_and_prune(1, 0)
    assert result["archived"] == 0 and result["errors"]
    assert isolated_storage._query("SELECT COUNT(*) AS n FROM tool_calls")[0]["n"] == 1200


def test_export_iterates_full_history_without_truncation(isolated_storage, bulk_calls):
    bulk_calls(isolated_storage, 1200)
    rows = list(isolated_storage.iter_calls({}, batch=500))
    assert len(rows) == 1200
    assert len({row["request_id"] for row in rows}) == 1200
    page, total = isolated_storage.query_calls({}, limit=1000)
    assert total == 1200 and len(page) == 1000, "单次查询上限不能再截在 500"


def test_time_series_and_duration_distribution_use_sql_aggregation(
        isolated_storage, bulk_calls, monkeypatch):
    bulk_calls(isolated_storage, 250)
    original = isolated_storage._query_checked
    seen: list[tuple[str, int | None]] = []

    def spy(sql, params=()):
        rows = original(sql, params)
        seen.append((sql, len(rows) if rows is not None else None))
        return rows

    monkeypatch.setattr(isolated_storage, "_query_checked", spy)
    series = isolated_storage.time_series(FAR_PAST, FAR_FUTURE)
    distribution = isolated_storage.duration_distribution(FAR_PAST)

    assert sum(series["calls"]) == 250
    assert seen[0][1] is not None and seen[0][1] <= 24, "趋势查询只应返回桶，而不是整表行"
    assert sum(item["count"] for item in distribution) == 250
    assert [item["count"] for item in distribution] == [50, 150, 50, 0, 0]

    # 方言 SQL 失败时回退到有界 Python 聚合，结果保持一致
    calls = {"n": 0}

    def flaky(sql, params=()):
        calls["n"] += 1
        return None if calls["n"] == 1 else original(sql, params)

    monkeypatch.setattr(isolated_storage, "_query_checked", flaky)
    assert sum(isolated_storage.time_series(FAR_PAST, FAR_FUTURE)["calls"]) == 250
    calls["n"] = 0
    assert sum(item["count"] for item in
               isolated_storage.duration_distribution(FAR_PAST)) == 250


def test_sessions_expire_by_ttl_not_forever_active(isolated_storage, monkeypatch):
    from gateway.audit import storage as storage_mod

    monkeypatch.setattr(storage_mod, "SESSION_TTL", 3600)
    stale = (datetime.now(timezone.utc)
             - timedelta(hours=2)).isoformat(timespec="milliseconds")
    isolated_storage._insert("client_sessions", {
        "session_id": "idle", "last_seen": stale, "status": "active"})
    isolated_storage._insert("client_sessions", {
        "session_id": "expired_token", "last_seen": utcnow(),
        "token_expires": stale, "status": "active"})
    isolated_storage.upsert_session("fresh", "tok_fresh")

    rows = {row["session_id"]: row for row in isolated_storage.sessions()}
    assert rows["idle"]["status"] == "expired" and rows["idle"]["active"] is False
    assert rows["expired_token"]["status"] == "expired"
    assert rows["fresh"]["status"] == "active" and rows["fresh"]["active"] is True
    assert isolated_storage.active_session_count() == 1


def test_strict_mode_confirms_real_persistence_three_states(isolated_storage, make_call):
    make_call(isolated_storage, request_id="req_persisted")
    state = isolated_storage.persistence_state()
    assert state == {"healthy": True, "buffered": 0, "bufferable": True, "persisted": True}
    assert isolated_storage.confirm_persisted("req_persisted") is True
    assert isolated_storage.confirm_persisted("req_missing") is False

    isolated_storage._healthy = False
    assert isolated_storage.available() is True, "故障期间仍可缓冲写入"
    assert isolated_storage.persistence_state()["persisted"] is False
    assert isolated_storage.confirm_persisted("req_persisted") is False
