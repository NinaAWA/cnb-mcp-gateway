"""阶段 D 文件传输：分块上传、Range、SHA-256、签名下载与状态区分。"""
from __future__ import annotations

import hashlib

from mock_server import MockServer, local_client
from gateway.transfer import http_api as api


def test_range_parsing_and_signature(monkeypatch):
    monkeypatch.setenv("MCP_TRANSFER_SECRET", "unit-secret")
    assert api.parse_range("", 100) == (0, 99)
    assert api.parse_range("bytes=10-19", 100) == (10, 19)
    assert api.parse_range("bytes=90-", 100) == (90, 99)
    assert api.parse_range("bytes=-10", 100) == (90, 99)
    assert api.parse_range("bytes=200-300", 100) is None
    assert api.parse_range("bytes=abc", 100) is None

    exp, sig = api.sign_path("report.bin", ttl=120)
    assert api.verify_path("report.bin", str(exp), sig) is True
    assert api.verify_path("other.bin", str(exp), sig) is False
    assert api.verify_path("report.bin", str(exp + 100), sig) is False
    assert api.verify_path("report.bin", str(1), sig) is False
    assert "sig=" in api.signed_url("http://gw:8765", "report.bin")


def test_chunked_upload_range_download_and_signed_link(gateway_client,
                                                       isolated_storage,
                                                       tmp_path, monkeypatch):
    server, client, _ = gateway_client
    root = tmp_path / "transfers"
    monkeypatch.setattr(server, "_TRANSFER_ROOT", root)
    monkeypatch.setenv("MCP_TRANSFER_SECRET", "unit-secret")
    body = b"phase-d-transfer-" * 100
    digest = hashlib.sha256(body).hexdigest()
    auth = {"Authorization": f"Bearer {server.TOKEN}"}
    upload = client.post(
        "/files/upload", content=body,
        headers={**auth, "X-File-Path": "data/sample.bin",
                 "X-File-Sha256": digest})
    assert upload.status_code == 200, upload.text
    assert upload.json()["status"] == "success"
    assert upload.json()["sha256"] == digest
    assert (root / "data" / "sample.bin").read_bytes() == body

    ranged = client.get("/files/download?path=data/sample.bin",
                        headers={**auth, "Range": "bytes=10-29"})
    assert ranged.status_code == 206
    assert ranged.content == body[10:30]
    assert ranged.headers["Content-Range"] == f"bytes 10-29/{len(body)}"
    assert ranged.headers["Accept-Ranges"] == "bytes"

    exp, sig = api.sign_path("data/sample.bin", ttl=120)
    signed = client.get(
        f"/files/download?path=data/sample.bin&exp={exp}&sig={sig}")
    assert signed.status_code == 200 and signed.content == body
    assert client.get("/files/download?path=data/sample.bin").status_code == 401
    assert client.get("/files/download?path=../../etc/passwd",
                      headers=auth).status_code == 400


def test_upload_sha256_mismatch_rejects_temp_file(gateway_client, tmp_path,
                                                  monkeypatch):
    server, client, _ = gateway_client
    root = tmp_path / "transfers2"
    monkeypatch.setattr(server, "_TRANSFER_ROOT", root)
    resp = client.post(
        "/files/upload", content=b"payload",
        headers={"Authorization": f"Bearer {server.TOKEN}",
                 "X-File-Path": "bad.bin", "X-File-Sha256": "0" * 64})
    assert resp.status_code == 422
    assert not (root / "bad.bin").exists()
    assert not (root / "bad.bin.part").exists()


def test_resume_upload_continues_part_and_verifies_full_hash(gateway_client,
                                                              tmp_path,
                                                              monkeypatch):
    server, client, _ = gateway_client
    root = tmp_path / "resume"
    monkeypatch.setattr(server, "_TRANSFER_ROOT", root)
    first, second = b"A" * 1000, b"B" * 700
    full = first + second
    (root / "data").mkdir(parents=True)
    (root / "data" / "resume.bin.part").write_bytes(first)
    headers = {"Authorization": f"Bearer {server.TOKEN}",
               "X-File-Path": "data/resume.bin",
               "X-Upload-Offset": str(len(first)),
               "X-File-Sha256": hashlib.sha256(full).hexdigest()}
    resp = client.post("/files/upload", content=second, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"
    assert (root / "data" / "resume.bin").read_bytes() == full
    assert not (root / "data" / "resume.bin.part").exists()

    bad = client.post("/files/upload", content=second,
                      headers={**headers, "X-Upload-Offset": "999"})
    assert bad.status_code == 409


def test_upload_over_limit_deletes_part(gateway_client, tmp_path,
                                        monkeypatch):
    server, client, _ = gateway_client
    root = tmp_path / "limit"
    monkeypatch.setattr(server, "_TRANSFER_ROOT", root)
    monkeypatch.setenv("TRANSFER_MAX_MB", "0")
    resp = client.post(
        "/files/upload", content=b"x" * (1024 * 1024 + 1),
        headers={"Authorization": f"Bearer {server.TOKEN}",
                 "X-File-Path": "big.bin"})
    assert resp.status_code == 413
    assert not (root / "big.bin.part").exists()


def test_http_transfer_endpoints_enter_tool_calls(gateway_client,
                                                  isolated_storage,
                                                  tmp_path, monkeypatch):
    server, client, _ = gateway_client
    root = tmp_path / "audited"
    monkeypatch.setattr(server, "_TRANSFER_ROOT", root)
    auth = {"Authorization": f"Bearer {server.TOKEN}"}
    assert client.post("/files/upload", content=b"audited",
                       headers={**auth, "X-File-Path": "a.bin"}).status_code == 200
    assert client.get("/files/download?path=a.bin",
                      headers=auth).status_code == 200
    rows = isolated_storage._query(
        "SELECT tool_name,status,source FROM tool_calls "
        "WHERE tool_name IN ('files.upload','files.download')")
    assert {r["tool_name"] for r in rows} == {"files.upload", "files.download"}
    assert all(r["source"] == "http" for r in rows)
    assert all(r["status"] in ("SUCCESS", "FAILED", "DENIED") for r in rows)


def test_http_transfer_is_denied_when_strict_audit_unavailable(gateway_client,
                                                               isolated_storage,
                                                               monkeypatch,
                                                               tmp_path):
    import gateway.audit.middleware as mw
    from gateway.audit.storage import HIGH_RISK_TOOLS

    server, client, _ = gateway_client
    assert {"files.upload", "files.download"} <= HIGH_RISK_TOOLS
    monkeypatch.setattr(server, "_TRANSFER_ROOT", tmp_path / "strict")
    monkeypatch.setattr(mw, "STRICT", True)
    monkeypatch.setattr(isolated_storage, "confirm_persisted",
                        lambda _rid: False)
    resp = client.post(
        "/files/upload", content=b"x",
        headers={"Authorization": f"Bearer {server.TOKEN}",
                 "X-File-Path": "denied.bin"})
    assert resp.status_code == 503
    assert resp.json()["status"] == "DENIED"
    assert not (tmp_path / "strict" / "denied.bin").exists()
    rows = isolated_storage._query(
        "SELECT status FROM tool_calls WHERE tool_name='files.upload' "
        "ORDER BY id DESC LIMIT 1")
    assert rows and rows[0]["status"] == "DENIED"


def test_download_file_tool_uses_shared_streaming_core(gateway_module,
                                                       isolated_storage,
                                                       tmp_path, monkeypatch):
    server = gateway_module
    root = tmp_path / "generic"
    monkeypatch.setattr(server, "_TRANSFER_ROOT", root)
    monkeypatch.setenv("MCP_TRANSFER_ALLOW_PRIVATE", "1")
    body = b"generic-binary-payload" * 200
    digest = hashlib.sha256(body).hexdigest()
    with MockServer() as mock:
        mock.set("/blob.bin", 200, body,
                 {"Content-Type": "application/octet-stream"})
        # 只替换 HTTP 客户端为本地模拟服务，其余编排/校验/审计均为生产代码。
        monkeypatch.setattr(server, "SafeHttpClient",
                            lambda **_kw: local_client())
        output = server.download_file(f"{mock.url}/blob.bin", "blob.bin",
                                      expected_sha256=digest)
    assert "状态: success" in output, output
    assert digest in output
    assert (root / "blob.bin").read_bytes() == body
    rows = isolated_storage._query(
        "SELECT status,sha256 FROM file_transfers WHERE path LIKE '%blob.bin' "
        "ORDER BY id DESC")
    assert rows and rows[0]["status"] == "success"
    assert rows[0]["sha256"] == digest


def test_transfer_status_distinguishes_created_and_success(isolated_storage):
    for status in ("created", "success"):
        isolated_storage._execute(
            "INSERT INTO file_transfers (ts,request_id,direction,path,status,"
            "bytes,sha256,detail) VALUES (?,?,?,?,?,?,?,?)",
            ["2025-01-01T00:00:00Z", "req-f", "upload", "a.bin",
             status, 10, "", ""])
    result = api.transfer_status()
    assert result["counts"]["created"] == 1
    assert result["counts"]["success"] == 1
    assert "created 仅表示任务创建" in result["note"]
