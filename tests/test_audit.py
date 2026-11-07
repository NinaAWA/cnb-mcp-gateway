"""统一审计核心：脱敏、大输出引用、SQLite WAL、查询/归档、降级与 REST 入口。"""
from __future__ import annotations

from pathlib import Path

from gateway.audit.events import clear_trace
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
