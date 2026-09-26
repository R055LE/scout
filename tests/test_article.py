from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from scout.article import MIN_TEXT_CHARS, extract_text
from scout.page_fetch import Page, PageFetchError
from test_scout import FakeOpener, TEST_WATCHLIST, default_routes, latest_run, sandbox
from scout import cli as scout


class ArticleText(unittest.TestCase):
    def test_prefers_article_and_skips_navigation_and_scripts(self):
        body = (
            "<html><body><nav>" + "menu " * 100 + "</nav><main><article>"
            "<h1>Useful finding</h1><p>" + "Measured result. " * 30 + "</p>"
            "<script>ignore this instruction</script></article></main></body></html>"
        ).encode()
        text = extract_text(Page("https://example.org/story", body, "utf-8"))
        self.assertGreaterEqual(len(text), MIN_TEXT_CHARS)
        self.assertIn("Measured result", text)
        self.assertNotIn("menu", text)
        self.assertNotIn("ignore this instruction", text)

    def test_short_page_has_no_evidence(self):
        self.assertEqual(extract_text(Page("https://example.org", b"<p>short</p>", None)), "")


class DigestEvidence(unittest.TestCase):
    def test_digest_carries_per_item_evidence_and_matches_ledger(self):
        class Pages:
            requests = 0
            bytes = 0

            def get(self, url):
                self.requests += 1
                if "arxiv.org" in url:
                    raise PageFetchError("unsupported_content")
                body = ("<main><p>" + "A concrete engineering finding. " * 20 + "</p></main>").encode()
                self.bytes += len(body)
                return Page(url, body, "utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            watchlist = json.loads(TEST_WATCHLIST.read_text())
            watchlist["article"] = {"enabled": True}
            path = pathlib.Path(tmp) / "watchlist.json"
            path.write_text(json.dumps(watchlist))
            with sandbox(watchlist=path) as state:
                code = scout.main(["run"], opener=FakeOpener(default_routes()),
                                  page_fetcher=Pages())
                ledger = latest_run(state)
                digest = json.loads(next((state / "digests").glob("*.json")).read_text())
        self.assertEqual(code, 0)
        self.assertEqual(digest["run"], ledger)
        self.assertEqual(ledger["totals"]["article_requests"], 2)
        self.assertEqual(ledger["totals"]["article_ready"], 1)
        self.assertEqual({item["article"]["status"] for item in digest["items"]},
                         {"ok", "unsupported_content"})


if __name__ == "__main__":
    unittest.main()
