"""v1.1 文件传输真实测试：二进制分片往返、摘要、覆盖、失败恢复与签名链接。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import os
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "docker") not in sys.path:
    sys.path.insert(0, str(ROOT / "docker"))

os.environ.setdefault("MCP_TOKEN", "unit-test-token")
os.environ.setdefault("MCP_ALLOWED_PREFIXES", "")

from gateway.files import service  # noqa: E402
from gateway.files.service import FileTransferError  # noqa: E402

CHUNK = 65536


@pytest.fixture
def fenv(tmp_path, monkeypatch, isolated_storage):
    root = (tmp_path / "files").resolve()
    root.mkdir()
    monkeypatch.setenv("MCP_FILE_ROOT", str(root))
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://gw.example.test")
    monkeypatch.setenv("MCP_FILE_SIGNING_SECRET", "test-file-signing-secret")
    monkeypatch.setattr(service, "MAX_CHUNK", CHUNK)
    return root, isolated_storage


def _upload(path: str, raw: bytes, *, chunk_size: int = CHUNK,
            sha256: str = "", overwrite: bool = False, corrupt_sha: str = ""):
    digest = corrupt_sha or (sha256 or hashlib.sha256(raw).hexdigest())
    begun = service.upload_begin(path, total_size=len(raw),
                                 sha256_expected=digest, overwrite=overwrite)
    ticket = begun["ticket"]
    offset = 0
    while offset < len(raw):
        part = raw[offset:offset + chunk_size]
        state = service.upload_chunk(ticket, offset, base64.b64encode(part).decode())
        offset = state["offset"]
    return ticket, service.upload_finish(ticket, base_url="https://gw.example.test")


def test_binary_chunked_roundtrip_and_sha256(fenv):
    root, _ = fenv
    raw = os.urandom(CHUNK * 2 + 137)
    digest = hashlib.sha256(raw).hexdigest()
    _ticket, info = _upload("nested/bin/blob.bin", raw)
    assert info["size"] == len(raw)
    assert info["sha256"] == digest
    assert (root / "nested" / "bin" / "blob.bin").read_bytes() == raw

    # 通过 download_chunk 逐片还原，验证任意二进制（含 NUL 与高位字节）无损
    restored = bytearray()
    offset = 0
    while True:
        piece = service.download_chunk("nested/bin/blob.bin", offset, CHUNK)
        restored += base64.b64decode(piece["content_base64"])
        offset += piece["length"]
        if piece["eof"]:
            break
    assert bytes(restored) == raw
    assert hashlib.sha256(restored).hexdigest() == digest
    assert service.file_info("nested/bin/blob.bin")["mime"]


def test_overwrite_detection_and_atomic_replacement(fenv):
    root, _ = fenv
    _upload("data.bin", b"original")
    with pytest.raises(FileTransferError, match="overwrite"):
        service.upload_begin("data.bin", 4, "", False)
    _upload("data.bin", b"replaced!", overwrite=True)
    assert (root / "data.bin").read_bytes() == b"replaced!"
    assert service.file_info("data.bin")["size"] == 9


def test_failed_checksum_keeps_original_and_removes_part(fenv):
    root, _ = fenv
    _upload("keep.bin", b"ORIGINAL-CONTENT")
    begun = service.upload_begin("keep.bin", 3, "0" * 64, overwrite=True)
    ticket = begun["ticket"]
    service.upload_chunk(ticket, 0, base64.b64encode(b"BAD").decode())
    with pytest.raises(FileTransferError, match="SHA-256"):
        service.upload_finish(ticket)
    assert (root / "keep.bin").read_bytes() == b"ORIGINAL-CONTENT"
    assert not (root / ".mcp-uploads" / f"{ticket}.part").exists()
    assert not (root / ".mcp-uploads" / f"{ticket}.json").exists()
    # 失败后票据已作废
    with pytest.raises(FileTransferError):
        service.upload_state(ticket)


def test_non_contiguous_offset_and_size_mismatch_are_rejected(fenv):
    begun = service.upload_begin("seq.bin", 6, "", False)
    ticket = begun["ticket"]
    service.upload_chunk(ticket, 0, base64.b64encode(b"abc").decode())
    with pytest.raises(FileTransferError, match="offset"):
        service.upload_chunk(ticket, 10, base64.b64encode(b"def").decode())
    with pytest.raises(FileTransferError, match="total_size"):
        service.upload_chunk(ticket, 3, base64.b64encode(b"toolong").decode())
    service.upload_abort(ticket)
    assert not (service.file_root() / "seq.bin").exists()


def test_path_containment_and_credential_paths_are_denied(fenv):
    root, _ = fenv
    bad = ["../escape.bin", "/etc/passwd", "/workspace/secret.txt", ".env",
           "deploy/.env.production", "keys/id_rsa", "keys/server.pem",
           "repo/.git/config", ".ssh/authorized_keys", "a/../b/c.part"]
    for path in bad:
        with pytest.raises(FileTransferError, match=".*"):
            service.resolve_path(path, for_write=True)
    assert service.resolve_path("ok/plain.bin", for_write=True) == root / "ok" / "plain.bin"


def test_signed_url_expiry_and_tampering(fenv):
    _upload("signed.bin", b"signed-content")
    url = service.signed_download_url("signed.bin", base_url="https://gw.example.test",
                                      ttl_seconds=60)
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    assert parts.path == "/files/download"
    key, token = query["path"][0], query["token"][0]
    assert service.verify_download_token(key, token)
    assert not service.verify_download_token(key + "x", token)
    assert not service.verify_download_token(key, token[:-1] + ("0" if token[-1] != "0" else "1"))
    expired = service._token_for(key, int(time.time()) - 5)
    assert not service.verify_download_token(key, expired)
    assert not service.verify_download_token(key, "not-a-token")


def test_range_parsing_and_streaming(fenv):
    root, _ = fenv
    raw = bytes(range(256)) * 4
    (root / "rng.bin").write_bytes(raw)
    target = service.resolve_path("rng.bin")
    assert service.parse_range("", len(raw)) is None
    assert service.parse_range("bytes=10-19", len(raw)) == (10, 19)
    assert service.parse_range("bytes=100-", len(raw)) == (100, len(raw) - 1)
    assert service.parse_range("bytes=-5", len(raw)) == (len(raw) - 5, len(raw) - 1)
    assert service.parse_range("bytes=0-99999999", len(raw)) == (0, len(raw) - 1)  # 末端自动收敛
    with pytest.raises(FileTransferError):
        service.parse_range("bytes=1000000-1000010", len(raw))
    with pytest.raises(FileTransferError):
        service.parse_range("items=0-1", len(raw))
    assert b"".join(service.iter_file_range(target, 10, 19)) == raw[10:20]


def test_transfer_records_have_no_content(fenv):
    _root, storage = fenv
    service.record_transfer("upload_file", "records.bin", direction="upload",
                            status="SUCCESS", size=42, sha256="a" * 64)
    rows = storage.file_transfers(50, "records.bin")
    assert rows and rows[0]["path"] == "records.bin"
    assert rows[0]["size"] == 42
    assert "content_base64" not in rows[0]
    assert rows[0]["status"] == "SUCCESS"


class _HttpRequest:
    def __init__(self, path: str = "", token: str = "", headers: dict | None = None):
        self.query_params = {"path": path, "token": token}
        self.headers = headers or {}


async def _collect(resp):
    out = b""
    iterator = resp.body_iterator
    if hasattr(iterator, "__aiter__"):
        async for block in iterator:
            out += block if isinstance(block, bytes) else block.encode()
    else:
        for block in iterator:
            out += block if isinstance(block, bytes) else block.encode()
    return out


def test_mcp_tools_and_http_download_endpoint(fenv):
    root, _ = fenv
    spec = importlib.util.spec_from_file_location(
        "mcp_gateway_server_file_test", ROOT / "docker" / "server.py")
    server = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(server)

    raw = os.urandom(CHUNK + 999)
    digest = hashlib.sha256(raw).hexdigest()
    first_b64 = base64.b64encode(raw[:CHUNK]).decode()
    second_b64 = base64.b64encode(raw[CHUNK:]).decode()
    first = server.upload_file("api/blob.bin", content_base64=first_b64,
                               offset=0, total_size=len(raw), sha256=digest,
                               finish=False)
    assert "ticket=" in first and "finish=false" in first
    ticket = first.split("ticket=", 1)[1].split("\n", 1)[0].strip()
    next_offset = int(first.split("offset=", 1)[1].split("\n", 1)[0].strip())
    assert next_offset == CHUNK

    done = server.upload_file("api/blob.bin", content_base64=second_b64,
                              offset=next_offset, ticket=ticket, finish=True)
    assert f"sha256={digest}" in done
    assert "download_url=https://gw.example.test/files/download?" in done

    listed = server.download_file("api/blob.bin", offset=0, length=CHUNK)
    assert "content_base64=" in listed
    assert base64.b64decode(listed.split("content_base64=", 1)[1].strip()) == raw[:CHUNK]

    url = done.split("download_url=", 1)[1].split("\n", 1)[0].strip()
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    key, token = query["path"][0], query["token"][0]

    async def scenario():
        ok = await server.file_download_endpoint(_HttpRequest(key, token))
        body = await _collect(ok)
        partial = await server.file_download_endpoint(
            _HttpRequest(key, token, {"range": "bytes=10-19"}))
        partial_body = await _collect(partial)
        denied = await server.file_download_endpoint(_HttpRequest(key, "bad.token"))
        return ok, body, partial, partial_body, denied

    ok, body, partial, partial_body, denied = asyncio.run(scenario())
    assert ok.status_code == 200 and body == raw
    assert partial.status_code == 206 and partial.headers["content-range"] == f"bytes 10-19/{len(raw)}"
    assert partial_body == raw[10:20]
    assert denied.status_code == 401
    assert (root / "api" / "blob.bin").read_bytes() == raw


def test_redaction_strips_base64_chunk_and_signature():
    from gateway.audit.redaction import redact_text, redact_structure
    text = "path=x.bin\ncontent_base64=" + "A" * 500 + "\ntoken=abcdef1234567890"
    cleaned = redact_text(text)
    assert "A" * 100 not in cleaned
    assert "***REDACTED***" in cleaned
    assert "content_base64=" in cleaned
    struct = redact_structure({"env": {"SECRET": "v"}, "stdin": "p",
                               "content_base64": "AAAA", "signature": "s",
                               "download_url": "https://x/files/download?token=abcdef123456"})
    assert struct["env"] == "***REDACTED***"
    assert struct["stdin"] == "***REDACTED***"
    assert struct["content_base64"] == "***REDACTED***"
    assert struct["signature"] == "***REDACTED***"
    assert "abcdef123456" not in struct["download_url"]


_SERVER = None


def _load_server():
    global _SERVER
    if _SERVER is None:
        spec = importlib.util.spec_from_file_location(
            "mcp_gateway_server_legacy_tools", ROOT / "docker" / "server.py")
        server = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(server)
        _SERVER = server
    return _SERVER


def test_legacy_file_tools_are_confined_to_file_root(fenv):
    """旧 6 个文件工具必须与新工具共用 MCP_FILE_ROOT 防护，不能读任意绝对路径。"""
    root, _ = fenv
    server = _load_server()
    (root / "safe.txt").write_text("hello-root\n", encoding="utf-8")
    assert "hello-root" in server.read_file("safe.txt")
    assert "safe.txt" in server.list_directory()
    assert "OK:" in server.write_file("made.txt", "written")
    assert (root / "made.txt").read_text() == "written"

    denied = {
        "read_file escape": lambda: server.read_file("../../../etc/passwd"),
        "read_file absolute": lambda: server.read_file("/etc/passwd"),
        "read_file credential": lambda: server.read_file("keys/id_rsa"),
        "write_file escape": lambda: server.write_file("../escape.txt", "x"),
        "write_file env": lambda: server.write_file(".env", "SECRET=x"),
        "list_directory escape": lambda: server.list_directory("/etc"),
        "search_files escape": lambda: server.search_files("*.conf", "/etc"),
        "grep_content escape": lambda: server.grep_content("root", "/etc"),
    }
    for name, call in denied.items():
        out = call()
        assert out.startswith("ERROR:"), (name, out)

    (root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    assert "download_file" in server.read_file("blob.bin")
    info = server.file_info("safe.txt")
    assert "download_url=https://gw.example.test/files/download?" in info
    assert "link_scope=public" in info


def test_download_link_is_real_but_marked_local_without_public_base(fenv, monkeypatch):
    """未配置公网入口时如实标注 local，且返回的链接仍可真实下载。"""
    import re as _re

    root, _ = fenv
    monkeypatch.delenv("MCP_PUBLIC_BASE_URL", raising=False)
    server = _load_server()
    (root / "a.bin").write_bytes(b"abc")
    out = server.download_file("a.bin")
    assert "link_scope=local" in out
    assert _re.search(r"download_url=http://127\.0\.0\.1:\d+/files/download\?", out)
    url = _re.search(r"download_url=(\S+)", out).group(1)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert service.verify_download_token(query["path"][0], query["token"][0])


def test_checksum_cache_avoids_rehashing_unchanged_file(fenv, monkeypatch):
    root, _ = fenv
    (root / "cache.bin").write_bytes(b"stable-content")
    calls: list[int] = []
    original = service.sha256_file

    def spy(path):
        calls.append(1)
        return original(path)

    monkeypatch.setattr(service, "sha256_file", spy)
    first = service.file_info("cache.bin")["sha256"]
    second = service.file_info("cache.bin")["sha256"]
    assert first == second == hashlib.sha256(b"stable-content").hexdigest()
    assert len(calls) == 1
