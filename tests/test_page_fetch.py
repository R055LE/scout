from __future__ import annotations

import email.message
import io
import unittest
from unittest.mock import patch

from scout.page_fetch import PageFetchError, PageFetcher, _PinnedConnection


def resolve(addresses):
    def fake(host, port, *, type):
        return [(None, None, None, None, (address, port)) for address in addresses[host]]
    return fake


class Response:
    def __init__(self, *, status=200, body=b"<html>article</html>", headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = email.message.Message()
        for key, value in (headers or {"Content-Type": "text/html; charset=utf-8"}).items():
            self.headers[key] = value

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, size):
        return self.body.read(size)


class Connection:
    def __init__(self, response):
        self.response = response
        self.closed = False

    def request(self, method, path, headers):
        self.method, self.path, self.headers = method, path, headers

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class PageFetch(unittest.TestCase):
    def test_fetch_uses_checked_address_and_bounded_html(self):
        made = []

        def factory(host, port, address, **kw):
            made.append((host, port, address, kw))
            return Connection(Response())

        fetcher = PageFetcher(
            resolver=resolve({"example.org": ["93.184.215.14"]}),
            connection_factory=factory,
        )
        page = fetcher.get("https://example.org/story?q=1")
        self.assertEqual(page.body, b"<html>article</html>")
        self.assertEqual(page.charset, "utf-8")
        self.assertEqual(made[0][:3], ("example.org", 443, "93.184.215.14"))
        self.assertEqual(fetcher.requests, 1)

    def test_private_or_mixed_dns_answers_are_rejected_before_connect(self):
        for addresses in (["127.0.0.1"], ["93.184.215.14", "10.0.0.1"]):
            with self.subTest(addresses=addresses):
                fetcher = PageFetcher(
                    resolver=resolve({"example.org": addresses}),
                    connection_factory=lambda *a, **kw: self.fail("must not connect"),
                )
                with self.assertRaisesRegex(PageFetchError, "unsafe_address"):
                    fetcher.get("https://example.org/story")

    def test_redirect_rechecks_destination(self):
        calls = []

        def factory(host, port, address, **kw):
            calls.append(host)
            return Connection(Response(status=302, headers={"Location": "http://internal.test/x"}))

        fetcher = PageFetcher(
            resolver=resolve({"example.org": ["93.184.215.14"], "internal.test": ["192.168.1.1"]}),
            connection_factory=factory,
        )
        with self.assertRaisesRegex(PageFetchError, "unsafe_address"):
            fetcher.get("https://example.org/story")
        self.assertEqual(calls, ["example.org"])

    def test_oversized_and_unsupported_responses_fail_closed(self):
        for response, code in (
            (Response(body=b"12345"), "budget_exceeded"),
            (Response(headers={"Content-Type": "application/pdf"}), "unsupported_content"),
            (Response(headers={"Content-Type": "text/html", "Content-Encoding": "gzip"}),
             "unsupported_encoding"),
        ):
            with self.subTest(code=code):
                fetcher = PageFetcher(
                    resolver=resolve({"example.org": ["93.184.215.14"]}),
                    connection_factory=lambda *a, **kw: Connection(response),
                    max_page_bytes=4,
                )
                with self.assertRaisesRegex(PageFetchError, code):
                    fetcher.get("https://example.org/story")

    def test_budget_and_url_restrictions(self):
        fetcher = PageFetcher(
            resolver=resolve({"example.org": ["93.184.215.14"]}),
            connection_factory=lambda *a, **kw: Connection(Response()),
            max_requests=1,
        )
        fetcher.get("https://example.org/one")
        with self.assertRaisesRegex(PageFetchError, "budget_exceeded"):
            fetcher.get("https://example.org/two")
        for url in ("file:///etc/passwd", "http://user@example.org/x", "https://example.org:444/x"):
            with self.subTest(url=url), self.assertRaisesRegex(PageFetchError, "invalid_url"):
                PageFetcher()._destination(url)

    def test_timeout_is_a_per_item_failure(self):
        class TimedOut(Connection):
            def request(self, method, path, headers):
                raise TimeoutError("read timed out")

        fetcher = PageFetcher(
            resolver=resolve({"example.org": ["93.184.215.14"]}),
            connection_factory=lambda *a, **kw: TimedOut(Response()),
        )
        with self.assertRaisesRegex(PageFetchError, "request_failed"):
            fetcher.get("https://example.org/story")

    def test_pinned_connection_checks_actual_peer(self):
        class Socket:
            closed = False

            def getpeername(self):
                return ("10.0.0.1", 443)

            def close(self):
                self.closed = True

        sock = Socket()
        connection = _PinnedConnection("example.org", 443, "93.184.215.14", tls=True, timeout=1)
        with patch("scout.page_fetch.socket.create_connection", return_value=sock) as connect:
            with self.assertRaisesRegex(PageFetchError, "unsafe_address"):
                connection.connect()
        connect.assert_called_once_with(("93.184.215.14", 443), 1)
        self.assertTrue(sock.closed)


if __name__ == "__main__":
    unittest.main()
