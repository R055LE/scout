"""Bounded public-page fetching for Scout's source-evidence stage.

Resolve each hop once and connect to that checked address. Resolving a hostname
for validation and then asking an HTTP client to resolve it again would leave a
DNS rebinding gap.
"""

from __future__ import annotations

import dataclasses
import http.client
import ipaddress
import socket
import ssl
import time
import urllib.parse


class PageFetchError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclasses.dataclass(frozen=True)
class Page:
    url: str
    body: bytes
    charset: str | None


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, *, tls: bool, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self.address = address
        self.tls = tls

    def connect(self) -> None:
        sock = socket.create_connection((self.address, self.port), self.timeout)
        try:
            peer = ipaddress.ip_address(sock.getpeername()[0])
            if not peer.is_global or peer != ipaddress.ip_address(self.address):
                raise PageFetchError("unsafe_address")
            self.sock = (
                ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
                if self.tls else sock
            )
        except BaseException:
            sock.close()
            raise


class PageFetcher:
    def __init__(
        self,
        *,
        max_requests: int = 24,
        max_page_bytes: int = 1_048_576,
        max_total_bytes: int = 12_582_912,
        timeout_seconds: float = 10,
        wall_seconds: float = 120,
        max_redirects: int = 3,
        resolver=None,
        connection_factory=None,
        clock=None,
    ) -> None:
        self.max_requests = max_requests
        self.max_page_bytes = max_page_bytes
        self.max_total_bytes = max_total_bytes
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max_redirects
        self.requests = 0
        self.bytes = 0
        self._resolver = resolver or socket.getaddrinfo
        self._connection_factory = connection_factory or _PinnedConnection
        self._clock = clock or time.monotonic
        self._deadline = self._clock() + wall_seconds

    def _destination(self, url: str) -> tuple[urllib.parse.SplitResult, str, int, str]:
        try:
            parts = urllib.parse.urlsplit(url)
            host = parts.hostname
            port = parts.port
        except ValueError as exc:
            raise PageFetchError("invalid_url") from exc
        if (
            parts.scheme not in {"http", "https"}
            or not host
            or parts.username is not None
            or parts.password is not None
            or port not in (None, 80, 443)
        ):
            raise PageFetchError("invalid_url")
        port = port or (443 if parts.scheme == "https" else 80)
        if (parts.scheme == "https" and port != 443) or (parts.scheme == "http" and port != 80):
            raise PageFetchError("invalid_url")
        try:
            addresses = self._resolver(host, port, type=socket.SOCK_STREAM)
            ips = [ipaddress.ip_address(row[4][0]) for row in addresses]
        except (OSError, ValueError) as exc:
            raise PageFetchError("resolve_failed") from exc
        if not ips or not all(ip.is_global for ip in ips):
            raise PageFetchError("unsafe_address")
        return parts, host, port, str(ips[0])

    def get(self, url: str) -> Page:
        current = url
        visited: set[str] = set()
        for _ in range(self.max_redirects + 1):
            if current in visited:
                raise PageFetchError("redirect_loop")
            visited.add(current)
            remaining = self._deadline - self._clock()
            if remaining <= 0 or self.requests >= self.max_requests:
                raise PageFetchError("budget_exceeded")
            parts, host, port, address = self._destination(current)
            remaining = self._deadline - self._clock()
            if remaining <= 0:
                raise PageFetchError("budget_exceeded")
            connection = self._connection_factory(
                host, port, address, tls=parts.scheme == "https",
                timeout=min(self.timeout_seconds, remaining),
            )
            self.requests += 1
            try:
                path = parts.path or "/"
                if parts.query:
                    path += "?" + parts.query
                connection.request("GET", path, headers={
                    "User-Agent": "scout/1 (+https://github.com/R055LE/scout)",
                    "Accept": "text/html, application/xhtml+xml",
                    "Accept-Encoding": "identity",
                })
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise PageFetchError("redirect_missing_location")
                    current = urllib.parse.urljoin(current, location)
                    continue
                if response.status != 200:
                    raise PageFetchError("http_error")
                content_type = response.headers.get_content_type()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise PageFetchError("unsupported_content")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise PageFetchError("unsupported_encoding")
                remaining_bytes = self.max_total_bytes - self.bytes
                if remaining_bytes <= 0:
                    raise PageFetchError("budget_exceeded")
                body = response.read(min(self.max_page_bytes, remaining_bytes) + 1)
                if len(body) > self.max_page_bytes or len(body) > remaining_bytes:
                    raise PageFetchError("budget_exceeded")
                self.bytes += len(body)
                return Page(current, body, response.headers.get_content_charset())
            except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                raise PageFetchError("request_failed") from exc
            except UnicodeError as exc:
                raise PageFetchError("invalid_url") from exc
            finally:
                connection.close()
        raise PageFetchError("too_many_redirects")
