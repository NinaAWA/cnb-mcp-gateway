"""离线模拟 HTTP 服务：可控状态码、响应头、重定向、二进制与断点响应。"""
from __future__ import annotations

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self) -> None:
        server: MockServer = self.server  # type: ignore[assignment]
        path = urllib.parse.urlsplit(self.path).path
        server.requests.append({"method": self.command, "path": self.path,
                                "headers": dict(self.headers)})
        entry = server.responses.get(path, server.responses.get("*"))
        if entry is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if callable(entry):
            entry = entry(self)
        status, body, headers = entry
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    do_GET = _respond
    do_POST = _respond
    do_HEAD = _respond

    def log_message(self, *_args):
        pass


class MockServer:
    """用法：with MockServer() as srv: srv.set("/x", 200, b"...")"""

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.requests: list[dict[str, Any]] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.responses = self.responses  # type: ignore[attr-defined]
        self._httpd.requests = self.requests  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    @property
    def host(self) -> str:
        return "127.0.0.1"

    def set(self, path: str, status: int = 200, body: bytes | str = b"",
            headers: dict[str, str] | None = None) -> None:
        self.responses[path] = (status, body, headers or {})

    def set_handler(self, path: str, handler) -> None:
        self.responses[path] = handler

    def __enter__(self) -> "MockServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3)


def make_test_pdf(pages, *, missing_eof: bool = False,
                  empty_text: bool = False) -> bytes:
    """不依赖第三方库生成最小但合法的 PDF（标准 Helvetica 字体，可提取文本）。"""
    def esc(text: str) -> str:
        return (str(text).replace("\\", r"\\").replace("(", r"\(")
                .replace(")", r"\)"))

    page_count = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(page_count))
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, text in enumerate(pages):
        page_id, content_id = 4 + 2 * i, 5 + 2 * i
        stream = ("" if empty_text else
                  f"BT /F1 12 Tf 72 720 Td ({esc(text)}) Tj ET\n").encode()
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {content_id} 0 R >>").encode()
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream
            + b"endstream")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for obj_id in sorted(objects):
        offsets[obj_id] = len(out)
        out += f"{obj_id} 0 obj\n".encode() + objects[obj_id] + b"\nendobj\n"
    xref = len(out)
    total = max(offsets) + 1
    out += f"xref\n0 {total}\n".encode()
    out += b"0000000000 65535 f \n"
    for obj_id in range(1, total):
        out += f"{offsets[obj_id]:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n"
            f"{xref}\n").encode()
    if not missing_eof:
        out += b"%%EOF\n"
    return bytes(out)


def local_client(**kw):
    """构造允许访问本地模拟服务的 SafeHttpClient（关闭 DNS 固定）。"""
    from gateway.net.client import SafeHttpClient

    defaults = {
        "allowed_hosts": frozenset({"127.0.0.1"}),
        "private_hosts": frozenset({"127.0.0.1"}),
        "pin_dns": False, "default_interval": 0.0, "retries": 1,
        "cache_ttl": 0.0, "timeout": 5.0, "max_bytes": 2_000_000,
    }
    defaults.update(kw)
    return SafeHttpClient(**defaults)
