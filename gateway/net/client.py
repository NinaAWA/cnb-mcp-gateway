"""检索、OA 发现与 PDF 下载共用的安全 HTTP 客户端（仅标准库）。

关键加固（V3 阶段 B）：
1. **一次 DNS 解析、校验全部返回 IP、固定 IP 连接**：避免 getaddrinfo 与实际
   连接分两次解析导致的 DNS rebinding。TLS 的 SNI / 证书校验仍按原始主机名。
2. **重定向逐跳重校验**，并在响应结束后再校验最终 URL。
3. **Content-Type 非文本检测**：JSON/XML/页面抓取不会把 PDF / 图片 / 二进制
   当成文本解析；PDF 下载必须显式声明 expect_text=False。
4. **每来源限速 + 有界缓存 + 429/5xx 指数退避（含 Retry-After）**。
5. **流式分块读取与 Range**，内存 O(分块)，供大 PDF 下载与断点续传使用。

私网访问仅对运维显式配置的自建实例（如 SearXNG）开放，且限定到该配置主机。
"""
from __future__ import annotations

import email.utils
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Iterator

DEFAULT_UA = "cnb-mcp-gateway/3.0 (+https://github.com/NinaAWA/cnb-mcp-gateway)"

# 只拦真正可用于内网 / 本机 / 云元数据服务的地址段。
# 不拦 198.18.0.0/15：部分机房用这段地址做透明出网代理，合法域名会被
# 解析到该段；若一并拦截，页面抓取在本环境将完全不可用。
BLOCKED_NETS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "::1/128", "::/128",
    "fc00::/7", "fe80::/10",
))

_NON_TEXT_TYPES = ("application/pdf", "application/octet-stream", "image/",
                   "audio/", "video/", "application/zip", "application/gzip",
                   "application/x-", "font/")


class SafeHttpError(Exception):
    """可读的外部请求错误；工具层统一转成 "ERROR: ..." 文本。"""


def is_public_ip(ip: Any) -> bool:
    return not any(ip in net for net in BLOCKED_NETS)


def resolve_public_ips(host: str) -> list[str]:
    """一次解析并返回全部地址；任一地址属于私网即拒绝（防 DNS rebinding）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SafeHttpError(f"域名解析失败: {host} ({exc})")
    addresses: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not is_public_ip(ip):
            raise SafeHttpError(f"拒绝访问非公网地址: {host} -> {ip}")
        if info[4][0] not in addresses:
            addresses.append(info[4][0])
    if not addresses:
        raise SafeHttpError(f"域名未解析到可用地址: {host}")
    return addresses


def normalize_host(value: str) -> str:
    return str(value or "").strip().lower().rstrip(".")


def configured_host(url: str) -> str:
    return normalize_host(urllib.parse.urlsplit(url).hostname)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连接到已校验的固定 IP，但 TLS SNI / 证书仍按原始主机名校验。"""

    def __init__(self, host: str, pinned_ip: str, *args: Any, **kwargs: Any):
        super().__init__(host, *args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, pinned_ip: str, *args: Any, **kwargs: Any):
        super().__init__(host, *args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()


class _PinnedHandler(urllib.request.HTTPSHandler):
    def __init__(self, host: str, pinned_ip: str, context: ssl.SSLContext):
        super().__init__(context=context)
        self._original_host = host
        self._pinned_ip = pinned_ip

    def _factory(self, host: str, **kwargs: Any):
        if normalize_host(host) == self._original_host:
            return _PinnedHTTPSConnection(host, self._pinned_ip, **kwargs)
        # 跳转到其它主机时不再沿用旧 IP；该跳转会先被 RedirectHandler 校验。
        return http.client.HTTPSConnection(host, **kwargs)

    def https_open(self, req):
        return self.do_open(self._factory, req, context=self._context)


class SafeHttpClient:
    def __init__(self, *, allowed_hosts: frozenset[str] | set[str] = frozenset(),
                 min_intervals: dict[str, float] | None = None,
                 default_interval: float = 0.15, timeout: float = 10.0,
                 max_bytes: int = 2_000_000, retries: int = 1,
                 cache_ttl: float = 300.0, user_agent: str = DEFAULT_UA,
                 pin_dns: bool = True, private_hosts: frozenset[str] | set[str] = frozenset()):
        self.allowed_hosts = frozenset(normalize_host(h) for h in allowed_hosts)
        self.private_hosts = frozenset(normalize_host(h) for h in private_hosts)
        self.min_intervals = dict(min_intervals or {})
        self.default_interval = default_interval
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.retries = retries
        self.cache_ttl = cache_ttl
        self.user_agent = user_agent
        self.pin_dns = pin_dns
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._last_call: dict[str, float] = {}
        self._cache: dict[str, tuple[float, bytes]] = {}
        self.last_content_type = ""
        self.last_url = ""
        self.last_status = 0
        self.last_cache_hit = False

    # ------------------------------------------------------------- validation
    def validate_url(self, url: str, *, allow_any_host: bool = False,
                     allow_private: bool = False) -> str:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise SafeHttpError("仅允许 http/https 协议")
        if parts.username or parts.password:
            raise SafeHttpError("URL 不允许携带用户名或密码")
        host = normalize_host(parts.hostname)
        if not host:
            raise SafeHttpError("URL 缺少主机名")
        private_allowed = allow_private and host in self.private_hosts
        if not allow_any_host and host not in self.allowed_hosts:
            raise SafeHttpError(f"来源不在允许列表: {host}")
        # 自建实例（如 searxng:8080）允许自定义端口，但仅限显式配置的私网主机。
        if (parts.port and parts.port not in (80, 443)
                and not private_allowed and host not in self.private_hosts):
            raise SafeHttpError("不允许访问非标准端口")
        if allow_any_host and not private_allowed:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise SafeHttpError("不允许直接使用 IP 地址作为目标主机")
            resolve_public_ips(host)
        return host

    def _redirect_handler(self, allow_any_host: bool, allow_private: bool):
        client = self

        class _Handler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                client.validate_url(newurl, allow_any_host=allow_any_host,
                                    allow_private=allow_private)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        return _Handler()

    def _handlers(self, host: str, parts: urllib.parse.SplitResult,
                  allow_private: bool) -> list[Any]:
        context = ssl.create_default_context()
        if parts.scheme == "https" and self.pin_dns and not allow_private:
            ips = resolve_public_ips(host)
            return [_PinnedHandler(host, ips[0], context)]
        return [urllib.request.HTTPSHandler(context=context)]

    # ------------------------------------------------------------- throttling
    def throttle(self, host: str) -> None:
        interval = self.min_intervals.get(host, self.default_interval)
        with self._lock:
            lock = self._host_locks.setdefault(host, threading.Lock())
        with lock:
            wait = self._last_call.get(host, 0.0) + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call[host] = time.monotonic()

    def _cache_get(self, key: str) -> bytes | None:
        if self.cache_ttl <= 0:
            return None
        with self._lock:
            item = self._cache.get(key)
            if item and time.time() - item[0] < self.cache_ttl:
                return item[1]
            self._cache.pop(key, None)
        return None

    def _cache_put(self, key: str, raw: bytes) -> None:
        if self.cache_ttl <= 0:
            return
        with self._lock:
            if len(self._cache) >= 256:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
            self._cache[key] = (time.time(), raw)

    @staticmethod
    def _retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
        raw = exc.headers.get("Retry-After") if exc.headers else ""
        if raw:
            try:
                return max(0.0, min(30.0, float(raw)))
            except ValueError:
                try:
                    when = email.utils.parsedate_to_datetime(raw)
                    return max(0.0, min(30.0, when.timestamp() - time.time()))
                except (TypeError, ValueError, OverflowError):
                    pass
        return float(min(2 ** attempt, 4))

    @staticmethod
    def check_content_type(value: str, *, expect_text: bool = True) -> None:
        ctype = (value or "").split(";", 1)[0].strip().lower()
        if expect_text and ctype and any(
                ctype.startswith(prefix.rstrip("*")) or ctype == prefix
                for prefix in _NON_TEXT_TYPES):
            raise SafeHttpError(f"响应 Content-Type 非文本({ctype})，拒绝按文本解析")

    # --------------------------------------------------------------- requests
    def request(self, url: str, *, method: str = "GET", data: Any = None,
                headers: dict[str, str] | None = None, timeout: float = 0,
                max_bytes: int = 0, allow_any_host: bool = False,
                allow_private: bool = False, retries: int = -1,
                use_cache: bool = True, expect_text: bool = True) -> bytes:
        host = self.validate_url(url, allow_any_host=allow_any_host,
                                 allow_private=allow_private)
        parts = urllib.parse.urlsplit(url)
        timeout = timeout or self.timeout
        max_bytes = max_bytes or self.max_bytes
        attempts = self.retries if retries < 0 else retries
        hdrs = {"User-Agent": self.user_agent, "Accept-Encoding": "identity"}
        if headers:
            hdrs.update(headers)
        body: bytes | None = None
        if data is not None:
            body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        cacheable = use_cache and method == "GET" and body is None
        cache_key = f"{method}:{url}"
        self.last_cache_hit = False
        if cacheable:
            cached = self._cache_get(cache_key)
            if cached is not None:
                self.last_cache_hit = True
                self.last_url, self.last_status = url, 200
                self.last_content_type = ""
                return cached
        opener = urllib.request.build_opener(
            *self._handlers(host, parts, allow_private),
            self._redirect_handler(allow_any_host, allow_private),
        )
        last_err = "未知错误"
        for attempt in range(attempts + 1):
            self.throttle(host)
            try:
                req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
                with opener.open(req, timeout=timeout) as resp:
                    final_url = resp.geturl()
                    self.validate_url(final_url, allow_any_host=allow_any_host,
                                      allow_private=allow_private)
                    ctype = resp.headers.get("Content-Type", "") if resp.headers else ""
                    self.check_content_type(ctype, expect_text=expect_text)
                    status = getattr(resp, "status", 200)
                    if status == 202:
                        raise SafeHttpError("上游返回 202（排队或反自动化校验）")
                    raw = resp.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise SafeHttpError(f"响应超过体积上限 {max_bytes} 字节")
                self.last_url, self.last_status = final_url, status
                self.last_content_type = ctype
                if cacheable:
                    self._cache_put(cache_key, raw)
                return raw
            except SafeHttpError:
                raise
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code}"
                retryable = exc.code in (429, 500, 502, 503, 504)
                delay = self._retry_delay(exc, attempt)
                try:
                    exc.close()
                except OSError:
                    pass
                if not retryable or attempt >= attempts:
                    raise SafeHttpError(
                        f"{last_err}（{urllib.parse.urlsplit(url).netloc}）")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = str(exc)[:180]
                delay = float(min(2 ** attempt, 4))
                if attempt >= attempts:
                    raise SafeHttpError(f"请求失败: {last_err}")
            if attempt < attempts:
                time.sleep(delay)
        raise SafeHttpError(f"请求失败: {last_err}")

    def json_get(self, url: str, **kw: Any) -> Any:
        hdrs = {"Accept": "application/json"}
        if kw.get("headers"):
            hdrs.update(kw.pop("headers"))
        kw["headers"] = hdrs
        raw = self.request(url, **kw)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SafeHttpError(f"JSON 解析失败: {exc}")

    def xml_get(self, url: str, **kw: Any) -> ET.Element:
        hdrs = {"Accept": "application/atom+xml, application/xml, text/xml"}
        if kw.get("headers"):
            hdrs.update(kw.pop("headers"))
        kw["headers"] = hdrs
        raw = self.request(url, **kw)
        try:
            return ET.fromstring(raw)
        except ET.ParseError as exc:
            raise SafeHttpError(f"XML 解析失败: {exc}")

    def stream(self, url: str, *, headers: dict[str, str] | None = None,
               chunk_size: int = 65536, max_bytes: int = 0,
               allow_any_host: bool = False, allow_private: bool = False,
               timeout: float = 0) -> Iterator[bytes]:
        """流式分块读取（PDF / 大文件）；调用方负责逐块哈希与落盘校验。"""
        host = self.validate_url(url, allow_any_host=allow_any_host,
                                 allow_private=allow_private)
        parts = urllib.parse.urlsplit(url)
        hdrs = {"User-Agent": self.user_agent, "Accept-Encoding": "identity"}
        if headers:
            hdrs.update(headers)
        opener = urllib.request.build_opener(
            *self._handlers(host, parts, allow_private),
            self._redirect_handler(allow_any_host, allow_private),
        )
        self.throttle(host)
        req = urllib.request.Request(url, headers=hdrs, method="GET")
        with opener.open(req, timeout=timeout or self.timeout) as resp:
            final_url = resp.geturl()
            self.validate_url(final_url, allow_any_host=allow_any_host,
                              allow_private=allow_private)
            self.last_url = final_url
            self.last_status = getattr(resp, "status", 200)
            self.last_content_type = (resp.headers.get("Content-Type", "")
                                      if resp.headers else "")
            total = 0
            limit = max_bytes or 512 * 1024 * 1024
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise SafeHttpError(f"流式响应超过体积上限 {limit} 字节")
                yield chunk
