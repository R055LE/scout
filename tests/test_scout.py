#!/usr/bin/env python3
"""Offline tests for scout. No network, no clock, no host state.

Every test pins SCOUT_NOW and SCOUT_STATE_DIR, and feeds the runner a
FakeOpener. The network guard below is itself mutation-tested, because "our
tests are offline" is otherwise an untested claim.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import pathlib
import tempfile
import unittest
import urllib.error

from scout import cli as scout

HERE = pathlib.Path(__file__).resolve().parent
PREFIX = HERE.parent
FIXTURES = PREFIX / "share" / "scout" / "fixtures"
TEST_WATCHLIST = FIXTURES / "watchlist-test.json"
NOW = "2026-09-03T12:00:00+00:00"

# Frozen inputs. Editing a fixture must be deliberate, so its digest is
# asserted rather than assumed. Same discipline as the provider-eval suite.
FIXTURE_SHA256 = {
    "hn.xml": "3d4b9e62663c2e5601473b1f89ae5f059eb04578c82118d87929691d2b3faf20",
    "arxiv.xml": "858500efc389be6636d21d36cef3c4e8dc1587963d6e3bf72e6e7d2164028a65",
    "github.json": "5ebe25094c23d952bdeaa2014f88789e4576ea83bc03e5c84ddb3955ac606cc4",
    "billion-laughs.xml": "e054477783082207b3f997146af11333c27bff6fd632a60a217ae2a14934a4f4",
    "billion-laughs-utf16.xml": "9d4d0198beba7d1701ed123c703e87273324d8d7e875e268b41c1b156460c3c6",
    "billion-laughs-padded.xml": "794308762c30d8a82716f942c96a8e8aaaeee58086589063f1dd2c190f9b0eeb",
    "watchlist-test.json": "234a5d99e6711e37ff246b095929e9b41f24b8b68f6fc32c6b76e094aff8307b",
}




class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeOpener:
    """Maps a URL prefix to bytes. Anything unmapped raises, loudly."""

    def __init__(self, routes: dict[str, bytes]):
        self.routes = routes
        self.calls: list[str] = []

    def open(self, request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.calls.append(url)
        for prefix, payload in self.routes.items():
            if url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(payload)
        raise AssertionError(f"unmapped URL in test: {url}")


def default_routes(**overrides):
    routes = {
        "https://hnrss.org/": (FIXTURES / "hn.xml").read_bytes(),
        "https://rss.arxiv.org/": (FIXTURES / "arxiv.xml").read_bytes(),
        "https://api.github.com/": (FIXTURES / "github.json").read_bytes(),
    }
    routes.update(overrides)
    return routes


@contextlib.contextmanager
def sandbox(watchlist=TEST_WATCHLIST):
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "SCOUT_NOW": NOW,
            "SCOUT_STATE_DIR": tmp,
            "SCOUT_WATCHLIST": str(watchlist),
            "SCOUT_TRIGGER": "test",
        }
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            yield pathlib.Path(tmp)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def run(routes=None, argv=("run",)):
    opener = FakeOpener(routes or default_routes())
    code = scout.main(list(argv), opener=opener)
    return code, opener


def latest_run(state: pathlib.Path) -> dict:
    lines = (state / "runs.jsonl").read_text().splitlines()
    return json.loads(lines[-1])


def latest_items(state: pathlib.Path) -> list[dict]:
    newest = sorted((state / "digests").glob("*.json"))[-1]
    return json.loads(newest.read_text())["items"]


class FixtureIntegrity(unittest.TestCase):
    def test_fixtures_are_frozen(self):
        """Record digests on first run; assert them thereafter."""
        for name in FIXTURE_SHA256:
            digest = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
            expected = FIXTURE_SHA256[name]
            if expected is not None:
                self.assertEqual(digest, expected, f"{name} changed; update deliberately")


class Parsing(unittest.TestCase):
    def test_rss2_and_atom_both_parse(self):
        rss = scout.RssAdapter()
        hn = rss.parse(scout.Request("https://hnrss.org/x", "xml", "rss:hn"),
                       (FIXTURES / "hn.xml").read_bytes())
        arxiv = rss.parse(scout.Request("https://rss.arxiv.org/x", "xml", "rss:arxiv"),
                          (FIXTURES / "arxiv.xml").read_bytes())
        self.assertEqual(len(hn), 5)
        self.assertEqual(len(arxiv), 2)
        self.assertTrue(all(i.published.tzinfo is not None for i in hn + arxiv))
        self.assertEqual(arxiv[0].author, "A. Researcher")

    def test_github_parses_stars(self):
        items = scout.GithubAdapter().parse(
            scout.Request("https://api.github.com/x", "json", "github:q0"),
            (FIXTURES / "github.json").read_bytes(),
        )
        self.assertEqual(items[0].source_score, 412)

    def test_dates_normalize_to_utc(self):
        for raw in ["Wed, 02 Sep 2026 09:14:00 +0000", "2026-09-02T18:00:00Z",
                    "2026-09-02T20:00:00+02:00", "2026-09-02 18:00:00"]:
            parsed = scout.parse_date(raw)
            self.assertIsNotNone(parsed.tzinfo)
            self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_url_normalization_strips_tracking_and_www(self):
        a = scout.normalize_url("https://www.example.org/x/?utm_source=hn")
        b = scout.normalize_url("https://example.org/x")
        self.assertEqual(a, b)


class Safety(unittest.TestCase):
    def test_dtd_payload_is_refused(self):
        with self.assertRaises(scout.UnsafePayload):
            scout.parse_xml((FIXTURES / "billion-laughs.xml").read_bytes())

    def test_dtd_refusal_survives_utf16(self):
        """Regression: a byte-sniff for ASCII "<!DOCTYPE" misses UTF-16 entirely."""
        payload = (FIXTURES / "billion-laughs-utf16.xml").read_bytes()
        self.assertNotIn(b"<!DOCTYPE", payload[:4096], "fixture must defeat a byte sniff")
        with self.assertRaises(scout.UnsafePayload):
            scout.parse_xml(payload)

    def test_dtd_refusal_survives_leading_padding(self):
        """Regression: a 4 KiB sniff window misses a declaration pushed past it."""
        payload = (FIXTURES / "billion-laughs-padded.xml").read_bytes()
        self.assertNotIn(b"<!DOCTYPE", payload[:4096], "fixture must defeat a byte sniff")
        with self.assertRaises(scout.UnsafePayload):
            scout.parse_xml(payload)

    def test_clean_feeds_still_parse(self):
        """The DTD guard must not reject ordinary feeds."""
        self.assertIsNotNone(scout.parse_xml((FIXTURES / "hn.xml").read_bytes()))
        self.assertIsNotNone(scout.parse_xml((FIXTURES / "arxiv.xml").read_bytes()))

    def test_redirect_to_off_allowlist_host_is_blocked(self):
        """build_opener follows 3xx by default; the allowlist must re-check each hop."""
        fetcher = scout.Fetcher({})
        fetcher.current_allowed = scout.RssAdapter().hosts
        handler = scout.AllowlistRedirectHandler(fetcher)
        req = scout.urllib.request.Request("https://hnrss.org/frontpage")
        for target in (
            "https://169.254.169.254/latest/meta-data/",
            "https://evil.example.com/x",
            "http://hnrss.org/frontpage",
        ):
            with self.assertRaises(scout.UnsafePayload, msg=f"{target} should be blocked"):
                handler.redirect_request(req, None, 302, "Found", {}, target)

    def test_redirect_within_allowlist_is_permitted(self):
        fetcher = scout.Fetcher({})
        fetcher.current_allowed = scout.RssAdapter().hosts
        handler = scout.AllowlistRedirectHandler(fetcher)
        req = scout.urllib.request.Request("https://hnrss.org/frontpage")
        result = handler.redirect_request(
            req, None, 302, "Found", {}, "https://rss.arxiv.org/rss/cs.SE"
        )
        self.assertIsNotNone(result)

    def test_real_opener_installs_the_redirect_guard(self):
        """Assert the guard is actually wired in, not merely defined."""
        fetcher = scout.Fetcher({})
        self.assertTrue(
            any(isinstance(h, scout.AllowlistRedirectHandler) for h in fetcher._opener.handlers),
            "the default opener must carry the allowlist redirect handler",
        )

    def test_dtd_degrades_one_source_not_the_run(self):
        routes = default_routes(**{
            "https://hnrss.org/": (FIXTURES / "billion-laughs.xml").read_bytes()
        })
        with sandbox() as state:
            code, _ = run(routes)
            record = latest_run(state)
        self.assertEqual(code, 1, "a hostile feed should degrade, not kill, the run")
        self.assertEqual(record["status"], "partial")
        rss = [s for s in record["sources"] if s["id"] == "rss"][0]
        self.assertIn("DTD", rss["error_code"])
        # arXiv still got through.
        self.assertGreater(record["totals"]["fetched"], 0)

    def test_off_allowlist_host_is_refused_not_skipped(self):
        fetcher = scout.Fetcher({})
        with self.assertRaises(scout.Refusal):
            fetcher.check_host("https://evil.example.com/f.xml", scout.RssAdapter().hosts)

    def test_non_https_is_refused(self):
        fetcher = scout.Fetcher({})
        with self.assertRaises(scout.Refusal):
            fetcher.check_host("http://hnrss.org/f.xml", scout.RssAdapter().hosts)

    def test_network_guard_actually_guards(self):
        """Mutation test: with no opener injected, a run must not reach out."""
        original = scout.urllib.request.build_opener

        def exploding(*a, **k):
            raise AssertionError("test attempted a real network call")

        scout.urllib.request.build_opener = exploding
        try:
            with sandbox():
                with self.assertRaises(AssertionError):
                    scout.main(["run"])
        finally:
            scout.urllib.request.build_opener = original


class Filtering(unittest.TestCase):
    def test_reports_scores_and_explains(self):
        with sandbox() as state:
            code, _ = run()
            items = latest_items(state)
        self.assertEqual(code, 0)
        titles = [i["title"] for i in items]
        self.assertIn("A sandbox for agent harness isolation", titles)
        self.assertIn("Quantization tradeoffs for local LLM inference under a VRAM ceiling", titles)
        for item in items:
            self.assertGreaterEqual(item["relevance"], 3)
            self.assertTrue(item["matched"], "every reported item must carry its reason")

    def test_mute_suppresses_with_a_reason(self):
        item = scout.Item("rss", "x", "https://example.org/t", "Crypto token sale for agents",
                          "", (), "", scout.parse_date(NOW), 0, {})
        watchlist, _ = scout.load_watchlist(TEST_WATCHLIST)
        reason = scout.muted_by(item, watchlist)
        self.assertTrue(reason)
        self.assertIn("fixture mute", reason)

    def test_near_miss_is_reported_separately(self):
        """A weight-2 topic lands one under threshold and must surface as a near miss."""
        with sandbox() as state:
            run()
            record = latest_run(state)
            digest = sorted((state / "digests").glob("*.md"))[-1].read_text()
        self.assertGreaterEqual(record["totals"]["near_miss"], 1)
        self.assertIn("Near misses", digest)
        self.assertIn("systemd", digest)

    def test_old_items_are_dropped(self):
        with sandbox() as state:
            run()
            titles = [i["title"] for i in latest_items(state)]
        self.assertNotIn("An ancient post about agent harness sandbox design", titles)

    def test_duplicate_urls_collapse(self):
        with sandbox() as state:
            run()
            titles = [i["title"] for i in latest_items(state)]
        self.assertEqual(
            sum("sandbox for agent harness" in t for t in titles), 1,
            "the www/trailing-slash mirror should collapse into one entry",
        )

    def test_irrelevant_items_never_appear(self):
        with sandbox() as state:
            run()
            titles = [i["title"] for i in latest_items(state)]
        self.assertNotIn("A survey of ornamental typography in medieval manuscripts", titles)
        self.assertNotIn("example/knitting-patterns", titles)

    def test_raising_threshold_moves_an_item_to_near_miss(self):
        raised = json.loads(TEST_WATCHLIST.read_text())
        raised["defaults"]["min_relevance"] = 4
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(raised, fh)
            path = fh.name
        try:
            with sandbox(watchlist=pathlib.Path(path)) as state:
                run()
                record = latest_run(state)
            self.assertEqual(record["totals"]["reported"], 0)
            self.assertGreater(record["totals"]["near_miss"], 0)
        finally:
            os.unlink(path)


class SeenState(unittest.TestCase):
    def test_items_dropped_by_caps_can_be_reported_next_run(self):
        feed = b"""<rss><channel>
          <item><title>A sandbox for agent harness A</title>
            <link>https://example.org/a</link></item>
          <item><title>B sandbox for agent harness B</title>
            <link>https://example.org/b</link></item>
        </channel></rss>"""
        for cap in ("max_items_per_feed", "max_digest_items"):
            with self.subTest(cap=cap), tempfile.TemporaryDirectory() as tmp:
                watchlist = json.loads(TEST_WATCHLIST.read_text())
                watchlist["feeds"] = watchlist["feeds"][:1]
                watchlist["sources"] = {"rss": {"enabled": True}}
                watchlist["defaults"][cap] = 1
                path = pathlib.Path(tmp) / "watchlist.json"
                path.write_text(json.dumps(watchlist))
                with sandbox(watchlist=path) as state:
                    for _ in range(2):
                        run({"https://hnrss.org/": feed})
                        self.assertEqual(latest_run(state)["totals"]["reported"], 1)
                    seen = scout.load_seen(state / "seen.tsv")
                    self.assertIn("url:https://example.org/a", seen)
                    self.assertIn("url:https://example.org/b", seen)

    def test_second_run_reports_nothing_then_reset_restores(self):
        with sandbox() as state:
            run()
            first = latest_run(state)["totals"]["reported"]
            run()
            second = latest_run(state)["totals"]["reported"]
            self.assertGreater(first, 0)
            self.assertEqual(second, 0, "seen-state must suppress a repeat run")
            (state / "seen.tsv").unlink()
            run()
            third = latest_run(state)["totals"]["reported"]
            self.assertEqual(third, first, "losing seen.tsv costs one noisy digest, nothing more")

    def test_ttl_prunes_and_leaves_a_valid_file(self):
        with sandbox() as state:
            run()
            path = state / "seen.tsv"
            rows = path.read_text().splitlines()
            self.assertGreater(len(rows), 1)
            stale = rows[0] + "\n" + "\n".join(
                r.replace("2026-09-03", "2020-01-01") for r in rows[1:]
            ) + "\n"
            path.write_text(stale)
            run()
            after = scout.load_seen(path)
            self.assertTrue(all("2020-01-01" not in v["last_seen"] for v in after.values()))
            self.assertTrue(path.read_text().startswith("key\t"))


class OutputContract(unittest.TestCase):
    """The digest directory is an integration surface, so these are guarantees."""

    def test_digest_json_and_ledger_agree_on_the_run_block(self):
        with sandbox() as state:
            run()
            ledger = latest_run(state)
            newest = sorted((state / "digests").glob("*.json"))[-1]
            embedded = json.loads(newest.read_text())["run"]
        self.assertEqual(embedded["run_id"], ledger["run_id"])
        self.assertEqual(embedded["exit_code"], ledger["exit_code"])
        self.assertIn("exit_code", embedded, "a consumer reading only the digest needs it")

    def test_writes_are_atomic_leaving_no_partial_files(self):
        with sandbox() as state:
            run()
            leftovers = list((state / "digests").glob("*.partial"))
            self.assertEqual(leftovers, [], "temp files must be renamed, not left behind")
            for path in (state / "digests").glob("*.json"):
                json.loads(path.read_text())  # every visible file parses

    def test_run_ids_sort_chronologically(self):
        """Consumers pick the newest by name, so the name must order correctly."""
        ids = ["2026-09-04T03:47:23Z-aaaa", "2026-09-04T11:02:01Z-bbbb",
               "2026-09-03T23:47:23Z-cccc"]
        self.assertEqual(
            sorted(ids),
            ["2026-09-03T23:47:23Z-cccc", "2026-09-04T03:47:23Z-aaaa",
             "2026-09-04T11:02:01Z-bbbb"],
        )

    def test_every_item_carries_the_consumer_contract_fields(self):
        required = {"title", "url", "source", "published", "relevance", "matched",
                    "summary", "tags", "author", "source_score", "native_id", "extra"}
        with sandbox() as state:
            run()
            items = latest_items(state)
        self.assertTrue(items)
        for item in items:
            self.assertEqual(required - set(item), set(), f"missing fields on {item['title']!r}")


class Ledger(unittest.TestCase):
    def test_every_run_appends_a_record_with_a_derived_action_trace(self):
        with sandbox() as state:
            run()
            record = latest_run(state)
        self.assertEqual(record["operation"], "scout-digest-v1")
        self.assertEqual(record["trigger"], "test")
        kinds = {a["kind"] for a in record["actions"]}
        self.assertIn("http_get", kinds)
        self.assertIn("write_file", kinds, "a run that wrote a digest must record it")
        self.assertTrue(record["watchlist_sha256"])

    def test_spend_block_is_zero_and_ceilinged(self):
        with sandbox() as state:
            run()
            spend = latest_run(state)["spend"]
        self.assertEqual(spend["model_calls"], 0)
        self.assertEqual(spend["estimated_cost_usd"], 0.0)
        self.assertEqual(spend["max_cost_usd"], 0.0)

    def test_spend_ceiling_can_actually_fail(self):
        """Mutation test. A ledger that has never been able to fail is not instrumentation."""
        original = scout.render_markdown

        def sabotage(run_record, reported, near, now):
            run_record["spend"]["model_calls"] = 1
            return original(run_record, reported, near, now)

        # Patch the run dict after construction but before the ceiling check by
        # inflating the configured budget check input instead.
        watch = json.loads(TEST_WATCHLIST.read_text())
        watch["spend"]["max_model_calls"] = -1  # any call count now breaches
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(watch, fh)
            path = fh.name
        try:
            with sandbox(watchlist=pathlib.Path(path)):
                code, _ = run()
            self.assertEqual(code, 2, "breaching the spend ceiling must refuse")
        finally:
            os.unlink(path)
            scout.render_markdown = original


class Refusals(unittest.TestCase):
    def _watchlist(self, mutate):
        watch = json.loads(TEST_WATCHLIST.read_text())
        mutate(watch)
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(watch, fh)
        fh.close()
        return pathlib.Path(fh.name)

    def test_bad_schema_version_refuses(self):
        path = self._watchlist(lambda w: w.update(schema_version=99))
        try:
            with sandbox(watchlist=path) as state:
                code, _ = run()
            self.assertEqual(code, 2)
            self.assertFalse((state / "runs.jsonl").exists(), "a refusal writes no state")
        finally:
            os.unlink(path)

    def test_unknown_source_refuses(self):
        path = self._watchlist(lambda w: w["sources"].update(telepathy={"enabled": True}))
        try:
            with sandbox(watchlist=path):
                code, _ = run()
            self.assertEqual(code, 2)
        finally:
            os.unlink(path)

    def test_uncompilable_regex_refuses_at_load(self):
        path = self._watchlist(lambda w: w["topics"][0].update(regex=["([unclosed"]))
        try:
            with sandbox(watchlist=path):
                code, _ = run()
            self.assertEqual(code, 2)
        finally:
            os.unlink(path)

    def test_missing_watchlist_refuses(self):
        with sandbox(watchlist=pathlib.Path("/nonexistent/watchlist.json")):
            code, _ = run()
        self.assertEqual(code, 2)


class Determinism(unittest.TestCase):
    def test_same_inputs_produce_the_same_items(self):
        with sandbox() as state:
            run()
            first = latest_items(state)
            (state / "seen.tsv").unlink()
            run()
            second = latest_items(state)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))


class Containment(unittest.TestCase):
    def test_nothing_is_written_outside_the_state_dir(self):
        with sandbox() as state:
            before = {p for p in PREFIX.rglob("*") if p.is_file()}
            run()
            after = {p for p in PREFIX.rglob("*") if p.is_file()}
            self.assertEqual(before, after, "scout must not write into the repository")
            self.assertTrue((state / "runs.jsonl").exists())


class Degradation(unittest.TestCase):
    def test_unreachable_source_is_recorded_not_raised(self):
        routes = default_routes(**{
            "https://hnrss.org/": urllib.error.URLError("connection refused")
        })
        with sandbox() as state:
            code, _ = run(routes)
            record = latest_run(state)
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "partial")
        rss = [s for s in record["sources"] if s["id"] == "rss"][0]
        self.assertIn("URLError", rss["error_code"])

    def test_read_timeout_is_recorded_not_raised(self):
        """Regression: TimeoutError is an OSError but not a URLError.

        A URLError-only catch let a real read timeout escape and kill the run
        with a traceback and no digest at all. Found by running it, not by the
        suite, which is why it is now in the suite.
        """
        routes = default_routes(**{"https://hnrss.org/": TimeoutError("read timed out")})
        with sandbox() as state:
            code, _ = run(routes)
            record = latest_run(state)
            self.assertTrue(
                sorted((state / "digests").glob("*.md")),
                "a timing-out feed must not cost us the whole digest",
            )
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "partial")
        rss = [s for s in record["sources"] if s["id"] == "rss"][0]
        self.assertIn("TimeoutError", rss["error_code"])
        self.assertIn("rss:hn", rss["error_code"], "the failing feed must be named")

    def test_request_budget_clips_without_losing_the_digest(self):
        watch = json.loads(TEST_WATCHLIST.read_text())
        watch["budget"]["max_requests"] = 1
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(watch, fh)
        fh.close()
        try:
            with sandbox(watchlist=pathlib.Path(fh.name)) as state:
                code, _ = run()
                record = latest_run(state)
                # Assert inside the sandbox: the temp dir is gone after it.
                self.assertTrue(
                    sorted((state / "digests").glob("*.md")),
                    "a clipped budget must still write what it got",
                )
            self.assertEqual(code, 1)
            self.assertEqual(record["status"], "partial")
        finally:
            os.unlink(fh.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
