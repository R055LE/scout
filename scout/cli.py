"""Produce one deterministic discovery digest from public feeds.

Why this exists. Discovery material worth reading (new projects, patterns,
papers) arrives irregularly and gets missed. Watching our own repositories is
low yield because we already see that traffic. This walks a small, explicit
watchlist of public feeds and prints what matched, with the reason it matched.

Why there is no model in it. A model would rank better, and it would also make
every bad digest ambiguous: was the watchlist wrong, or the model? Deterministic
scoring keeps the failure legible. Every reported item carries the topic and the
term that surfaced it, and the footer prints the near misses, so an over-narrow
watchlist reads differently from a quiet week. That distinction is the whole
substitute for ranking.

Why Python here when claude-mem-sample is bash. The periodic-job pattern in this
repo is the shape of the job, not the interpreter: rationale in the header,
env-overridable config with hardcoded defaults, append-only self-describing
state under ~/.local/state, stderr for journald. This one parses XML, three date
formats and writes a JSON ledger, which bash cannot do honestly. scripts/
agent-provider-eval and scripts/platform-doctor already establish stdlib
Python 3 here. No third-party dependency: agent-platform has none today, and
xml.etree covers RSS 2.0 and Atom.

Authority. Reads the watchlist, writes only under the state directory, makes
outbound HTTPS GETs to a named host allowlist, and holds no credential. It
cannot mutate a repository or a remote service. That is what lets it become an
unattended fixed operation later (runbook/docs/model-routing.md, "Unattended
work"); a timer is earned in phase 3, not assumed here.

Exit codes deliberately differ from fleet-audit.py, where 1 means findings.
Here, reporting items is success:
    0  ran (with or without items)
    1  partial (a source failed, a budget clipped, state is stale)
    2  refusal (bad watchlist, off-allowlist URL, spend ceiling breached)
A oneshot unit that exits 1 whenever it works would train us to ignore red.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import email.utils
import hashlib
import json
import os
import pathlib
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat

OPERATION = "scout-digest-v1"
SCHEMA_VERSION = 1
# Container-first defaults: /config is the mounted watchlist, /data the volume
# Roger reads. Both are overridable so the tool still runs from a checkout.
PREFIX = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_WATCHLIST = pathlib.Path(os.environ.get("SCOUT_CONFIG_DIR", "/config")) / "watchlist.json"
REPO_WATCHLIST = PREFIX / "config" / "watchlist.json"
DEFAULT_STATE_DIR = pathlib.Path("/data")
USER_AGENT = "scout/1 (+https://github.com/R055LE/agent-platform)"

# Every host any adapter may contact. An adapter also declares its own subset;
# a planned URL must satisfy both. Widening this is a deliberate edit, which is
# the point of it being a literal rather than derived from the watchlist.
HOST_ALLOWLIST = frozenset(
    {
        "hnrss.org",
        "rss.arxiv.org",
        "export.arxiv.org",
        "lobste.rs",
        "api.github.com",
        "lwn.net",
    }
)

ATOM = "{http://www.w3.org/2005/Atom}"
TRACKING_PARAMS = re.compile(r"^(utm_|ref$|source$|fbclid$|gclid$)")


class ScoutError(Exception):
    """Base class. Anything raised here is a refusal unless caught."""


class Refusal(ScoutError):
    """Configuration or safety failure. Exit 2, no state written."""


class UnsafePayload(ScoutError):
    """A fetched payload was refused before parsing.

    Degrades one source rather than failing the run: nothing was expanded and
    no memory was consumed, so the protection already did its job. Recorded in
    the ledger with its error code, which is louder than a silent skip and
    cheaper than losing the whole digest to one bad feed.
    """


@dataclasses.dataclass(frozen=True)
class Request:
    url: str
    kind: str  # "xml" | "json"
    label: str


@dataclasses.dataclass(frozen=True)
class Item:
    source: str
    native_id: str
    url: str
    title: str
    summary: str
    tags: tuple[str, ...]
    author: str
    published: dt.datetime  # tz-aware UTC
    source_score: int
    extra: dict


@dataclasses.dataclass
class Scored:
    item: Item
    relevance: int
    matched: list[dict]


def now_utc() -> dt.datetime:
    """Wall clock, or a pinned clock for deterministic tests."""
    override = os.environ.get("SCOUT_NOW")
    if override:
        return parse_date(override)
    return dt.datetime.now(dt.timezone.utc)


def parse_date(raw: str) -> dt.datetime:
    """Accept RFC-3339, RFC-822 and naive stamps. Always return tz-aware UTC."""
    raw = raw.strip()
    if not raw:
        raise ValueError("empty date")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(raw)
    if parsed is None:
        raise ValueError(f"unparseable date: {raw!r}")
    if parsed.tzinfo is None:
        # A feed that omits an offset is claiming UTC by convention. Recorded
        # rather than guessed at, because guessing local time silently shifts
        # every age comparison by the host's offset.
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def normalize_url(url: str) -> str:
    """Collapse the cosmetic differences that would otherwise defeat dedup."""
    parts = urllib.parse.urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not TRACKING_PARAMS.match(k)
    ]
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), host, path, urllib.parse.urlencode(query), "")
    )


def write_atomic(path: pathlib.Path, text: str) -> None:
    """Write via a temp file and rename, so a reader never sees a partial file.

    The digest directory is an integration surface, not just an output folder.
    A consumer polling it (Roger today, another front end later) would otherwise
    be able to open a file mid-write and get truncated JSON. rename(2) within a
    directory is atomic, so a file is either absent or complete.
    """
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def text_of(node, *names: str) -> str:
    for name in names:
        found = node.find(name)
        if found is not None and found.text:
            return found.text.strip()
    return ""


def parse_xml(payload: bytes):
    """Parse feed XML, refusing anything that declares a DTD or an entity.

    xml.etree does not expand external entities, so XXE is not the exposure
    here. Entity expansion (billion laughs) is: a few kilobytes of nested
    entity definitions can expand to gigabytes in memory, and the response byte
    cap bounds the input rather than the expansion.

    The exposure is measured, not assumed. On CPython 3.14, ET.fromstring on
    the billion-laughs fixture parses and expands: the title comes back 3000
    characters long from a payload well under a kilobyte.

    The refusal happens in a parser pre-pass, not by sniffing bytes. An earlier
    version scanned the first 4 KiB for a literal ASCII "<!DOCTYPE" and had two
    bypasses, both now fixtures: a UTF-16 document, where those bytes are not
    ASCII but the declaration is still honoured, and more than 4 KiB of leading
    comments before the declaration. Expat decodes the document itself and
    reports the declaration wherever it sits, so there is no window to slip
    past.

    Two passes because ET.XMLParser stopped exposing its expat parser (no
    `.parser` attribute on 3.14), so the handlers cannot be installed on the
    parser that builds the tree. The pre-pass builds no tree and rejects at
    *declaration* time, before any reference is expanded, so nothing expands
    even on a hostile payload.

    defusedxml would also fix this, at the cost of the first third-party
    dependency in this repository, which the reuse evaluation rejected on the
    same grounds it rejected feedparser. Revisit if the pre-pass cost shows up
    in run timings.
    """

    def reject_doctype(name, sysid, pubid, has_internal_subset):
        raise UnsafePayload(f"feed declares a DTD ({name!r}); refusing")

    def reject_entity(*args):
        raise UnsafePayload("feed declares an entity; refusing")

    scanner = expat.ParserCreate()
    scanner.StartDoctypeDeclHandler = reject_doctype
    scanner.EntityDeclHandler = reject_entity
    try:
        scanner.Parse(payload, True)
    except expat.ExpatError as exc:
        raise ET.ParseError(str(exc)) from exc
    return ET.fromstring(payload)


# --------------------------------------------------------------------------
# Adapters. An adapter never opens a socket: it plans requests and parses
# bytes. That one rule is what makes the offline tests, the host allowlist and
# the request budget possible at all.
# --------------------------------------------------------------------------


class RssAdapter:
    id = "rss"
    hosts = frozenset(
        {"hnrss.org", "rss.arxiv.org", "export.arxiv.org", "lobste.rs", "lwn.net"}
    )

    def plan(self, cfg: dict, watchlist: dict, now: dt.datetime) -> list[Request]:
        return [
            Request(url=feed["url"], kind="xml", label=f"rss:{feed['id']}")
            for feed in watchlist.get("feeds", [])
            if feed.get("enabled", True)
        ]

    def parse(self, req: Request, payload: bytes) -> list[Item]:
        root = parse_xml(payload)
        feed_id = req.label.split(":", 1)[1]
        entries = root.findall(f".//{ATOM}entry")
        if entries:
            return [self._atom(feed_id, e) for e in entries]
        return [self._rss2(feed_id, i) for i in root.findall(".//item")]

    def _rss2(self, feed_id: str, node) -> Item:
        link = text_of(node, "link")
        guid = text_of(node, "guid") or link
        raw_date = text_of(node, "pubDate", "date")
        return Item(
            source="rss",
            native_id=f"{feed_id}:{guid}",
            url=link,
            title=text_of(node, "title"),
            summary=text_of(node, "description"),
            tags=tuple(c.text.strip() for c in node.findall("category") if c.text),
            author=text_of(node, "author", "creator"),
            published=parse_date(raw_date) if raw_date else now_utc(),
            source_score=0,  # server-side thresholds do this; see docs/scout.md
            extra={"feed": feed_id},
        )

    def _atom(self, feed_id: str, node) -> Item:
        link_node = node.find(f"{ATOM}link")
        link = link_node.get("href", "") if link_node is not None else ""
        ident = text_of(node, f"{ATOM}id") or link
        raw_date = text_of(node, f"{ATOM}updated", f"{ATOM}published")
        author_node = node.find(f"{ATOM}author")
        author = text_of(author_node, f"{ATOM}name") if author_node is not None else ""
        return Item(
            source="rss",
            native_id=f"{feed_id}:{ident}",
            url=link,
            title=text_of(node, f"{ATOM}title"),
            summary=text_of(node, f"{ATOM}summary", f"{ATOM}content"),
            tags=tuple(
                c.get("term", "") for c in node.findall(f"{ATOM}category") if c.get("term")
            ),
            author=author,
            published=parse_date(raw_date) if raw_date else now_utc(),
            source_score=0,
            extra={"feed": feed_id},
        )


class GithubAdapter:
    """GitHub search has no feed, so this stays bespoke.

    Unauthenticated on purpose. `gh` would carry the operator's full
    write-capable token, and decision 0022 is explicit that direct gh access is
    not a security boundary. A read-only digest should not hold write authority
    to read public search results. The cost is a 10 req/min unauthenticated
    rate limit, which the request budget already keeps us well under.
    """

    id = "github"
    hosts = frozenset({"api.github.com"})

    def plan(self, cfg: dict, watchlist: dict, now: dt.datetime) -> list[Request]:
        if not cfg.get("enabled", False):
            return []
        since = (now - dt.timedelta(hours=cfg.get("max_age_hours", 168))).date().isoformat()
        out = []
        for i, query in enumerate(cfg.get("queries", [])):
            q = urllib.parse.quote(query.replace("{since}", since))
            out.append(
                Request(
                    url=f"https://api.github.com/search/repositories?q={q}&sort=stars&per_page=20",
                    kind="json",
                    label=f"github:q{i}",
                )
            )
        return out

    def parse(self, req: Request, payload: bytes) -> list[Item]:
        data = json.loads(payload)
        out = []
        for repo in data.get("items", []):
            out.append(
                Item(
                    source="github",
                    native_id=f"gh:{repo['full_name']}",
                    url=repo.get("html_url", ""),
                    title=repo.get("full_name", ""),
                    summary=repo.get("description") or "",
                    tags=tuple(repo.get("topics", [])),
                    author=repo.get("owner", {}).get("login", ""),
                    published=parse_date(repo["created_at"]),
                    source_score=int(repo.get("stargazers_count", 0)),
                    extra={"stars": repo.get("stargazers_count", 0)},
                )
            )
        return out


ADAPTERS = {a.id: a for a in (RssAdapter(), GithubAdapter())}


# --------------------------------------------------------------------------
# Fetching. The runner owns every socket.
# --------------------------------------------------------------------------


class AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-check the allowlist on every redirect hop.

    build_opener installs a redirect handler by default, so validating only the
    planned URL leaves the allowlist trivially bypassable: an allowlisted feed
    that answers 302 can send the fetch anywhere, including link-local and
    internal addresses, and the original check never runs again. Each hop is
    validated here instead.

    An off-allowlist redirect is UnsafePayload rather than Refusal because it is
    a remote server behaving unexpectedly, not a watchlist mistake. It degrades
    that one feed and is recorded, the same as a 502 or a DTD. A bad URL the
    operator typed is still a Refusal, at plan time.
    """

    def __init__(self, fetcher: "Fetcher") -> None:
        self.fetcher = fetcher

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            self.fetcher.check_host(newurl, self.fetcher.current_allowed)
        except Refusal as exc:
            raise UnsafePayload(f"redirect blocked: {exc}") from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    def __init__(self, budget: dict, opener=None) -> None:
        self.max_requests = budget.get("max_requests", 25)
        self.max_bytes = budget.get("max_bytes", 8 * 1024 * 1024)
        self.timeout = budget.get("timeout_seconds", 20)
        self.requests = 0
        self.bytes = 0
        self.actions: list[dict] = []
        self.current_allowed: frozenset = frozenset()
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            AllowlistRedirectHandler(self),
        )

    def check_host(self, url: str, allowed: frozenset) -> str:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https":
            raise Refusal(f"non-https url: {url}")
        host = parts.netloc.lower()
        # An off-allowlist URL is refused, never skipped. A silently skipped
        # fetch is a check that cannot fail.
        if host not in allowed or host not in HOST_ALLOWLIST:
            raise Refusal(f"host not on allowlist: {host}")
        return host

    def get(self, req: Request, allowed: frozenset) -> bytes:
        host = self.check_host(req.url, allowed)
        # The redirect handler reads this to validate each subsequent hop.
        self.current_allowed = allowed
        if self.requests >= self.max_requests:
            raise BudgetExceeded(f"request budget {self.max_requests} reached")
        self.requests += 1
        request = urllib.request.Request(req.url, headers={"User-Agent": USER_AGENT})
        with self._opener.open(request, timeout=self.timeout) as response:
            payload = response.read(self.max_bytes + 1)
        if len(payload) > self.max_bytes:
            raise BudgetExceeded(f"response exceeded {self.max_bytes} bytes")
        self.bytes += len(payload)
        self.actions.append(
            {"kind": "http_get", "target": host, "count": 1, "bytes": len(payload), "status": "ok"}
        )
        return payload


class BudgetExceeded(ScoutError):
    pass


# --------------------------------------------------------------------------
# Watchlist, scoring, mute
# --------------------------------------------------------------------------


def load_watchlist(path: pathlib.Path) -> tuple[dict, str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise Refusal(f"cannot read watchlist {path}: {exc}") from exc
    try:
        watchlist = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal(f"watchlist is not valid JSON: {exc}") from exc
    if watchlist.get("schema_version") != SCHEMA_VERSION:
        raise Refusal(
            f"watchlist schema_version {watchlist.get('schema_version')!r} "
            f"!= {SCHEMA_VERSION}"
        )
    for source_id in watchlist.get("sources", {}):
        if source_id not in ADAPTERS:
            raise Refusal(f"unknown source id: {source_id}")
    for feed in watchlist.get("feeds", []):
        for name in ("min_relevance", "max_items"):
            value = feed.get(name)
            if value is not None and (type(value) is not int or value < 1):
                raise Refusal(f"feed {feed.get('id')!r} {name} must be a positive integer")
    for topic in watchlist.get("topics", []):
        for pattern in topic.get("regex", []):
            try:
                re.compile(pattern)
            except re.error as exc:
                # Refuse at load. A bad pattern that silently never matches is
                # indistinguishable from a topic nobody is publishing about.
                raise Refusal(f"topic {topic.get('id')!r} regex {pattern!r}: {exc}") from exc
    return watchlist, hashlib.sha256(raw).hexdigest()


def matches_term(haystack: str, term: str) -> bool:
    """Word-boundary match, so "AI" does not hit "chain"."""
    return re.search(rf"\b{re.escape(term)}\b", haystack, re.IGNORECASE) is not None


def score(item: Item, watchlist: dict) -> tuple[int, list[dict]]:
    """Sum weights over *distinct* matched topics.

    Per topic rather than per hit, so one keyword repeated ten times in an
    abstract cannot outrank an item that genuinely spans three topics.
    """
    haystack = " ".join([item.title, item.summary, " ".join(item.tags), item.author])
    total = 0
    matched: list[dict] = []
    for topic in watchlist.get("topics", []):
        if any(matches_term(haystack, t) for t in topic.get("none", [])):
            continue
        alls = topic.get("all", [])
        if alls and not all(matches_term(haystack, t) for t in alls):
            continue
        hit = next((t for t in topic.get("any", []) if matches_term(haystack, t)), "")
        if not hit:
            hit = next(
                (
                    p
                    for p in topic.get("regex", [])
                    if re.search(p, haystack, re.IGNORECASE)
                ),
                "",
            )
        if not hit and not alls:
            continue
        total += int(topic.get("weight", 1))
        matched.append({"topic": topic["id"], "term": hit or "all", "field": "text"})
    for org in watchlist.get("orgs", []):
        if item.author and item.author.lower() == org["name"].lower():
            total += int(org.get("weight", 1))
            matched.append({"topic": f"org:{org['name']}", "term": org["name"], "field": "author"})
    return total, matched


def muted_by(item: Item, watchlist: dict) -> str:
    """Return "" when not muted, else the reason.

    Same convention as fleet-audit.py's exemptions and claude-script-drift's
    ungitted_by_design(): a suppression names itself, so a silent change of
    circumstance reads as a waiver rather than as compliance.
    """
    mute = watchlist.get("mute", {})
    host = urllib.parse.urlsplit(item.url).netloc.lower().removeprefix("www.")
    for entry in mute.get("domains", []):
        if host == entry["value"] or host.endswith("." + entry["value"]):
            return f"domain {entry['value']}: {entry['why']}"
    for entry in mute.get("title_regex", []):
        if re.search(entry["value"], item.title):
            return f"title {entry['value']}: {entry['why']}"
    for entry in mute.get("url_regex", []):
        if re.search(entry["value"], item.url):
            return f"url {entry['value']}: {entry['why']}"
    return ""


# --------------------------------------------------------------------------
# Seen state
# --------------------------------------------------------------------------

SEEN_HEADER = "key\tfirst_seen\tlast_seen\tsource\ttitle_prefix\n"


def load_seen(path: pathlib.Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    seen = {}
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        seen[parts[0]] = {
            "first_seen": parts[1],
            "last_seen": parts[2],
            "source": parts[3],
            "title_prefix": parts[4],
        }
    return seen


def save_seen(path: pathlib.Path, seen: dict[str, dict], now: dt.datetime, ttl_days: int) -> None:
    cutoff = now - dt.timedelta(days=ttl_days)
    rows = []
    for key, row in seen.items():
        try:
            if parse_date(row["last_seen"]) < cutoff:
                continue
        except ValueError:
            continue
        rows.append(f"{key}\t{row['first_seen']}\t{row['last_seen']}\t{row['source']}\t{row['title_prefix']}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(SEEN_HEADER + "\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    os.replace(tmp, path)


def seen_key(item: Item) -> str:
    return f"{item.source}:{item.native_id}" if item.native_id else f"url:{normalize_url(item.url)}"


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------


def render_markdown(run: dict, reported: list[Scored], near: list[Scored], now: dt.datetime) -> str:
    lines = [f"# Scout digest {run['run_id']}", ""]
    if not reported:
        lines += ["No new items matched the watchlist.", ""]
    by_topic: dict[str, list[Scored]] = {}
    for s in reported:
        key = s.matched[0]["topic"] if s.matched else "unscored"
        by_topic.setdefault(key, []).append(s)
    for topic in sorted(by_topic):
        lines.append(f"## {topic}")
        lines.append("")
        for s in sorted(by_topic[topic], key=lambda x: -x.relevance):
            age = int((now - s.item.published).total_seconds() // 3600)
            score_note = f", {s.item.source_score} pts" if s.item.source_score else ""
            lines.append(f"- **{s.item.title}** ({s.item.source}, {age}h{score_note})")
            lines.append(f"  {s.item.url}")
            why = ", ".join(f"{m['topic']}:{m['term']}" for m in s.matched)
            lines.append(f"  why: relevance {s.relevance} via {why}")
        lines.append("")
    t = run["totals"]
    lines += [
        "---",
        "",
        f"Fetched {t['fetched']}, matched {t['matched']}, reported {t['reported']}. "
        f"Suppressed: {t['suppressed_seen']} seen, {t['suppressed_muted']} muted.",
        "",
    ]
    # Near misses exist so an over-narrow watchlist looks different from a
    # quiet week. Without this the digest cannot tell you it is wrong.
    if near:
        lines.append(f"Near misses ({len(near)}), matched something but scored under threshold:")
        for s in sorted(near, key=lambda x: -x.relevance)[:5]:
            why = ", ".join(f"{m['topic']}:{m['term']}" for m in s.matched)
            lines.append(f"- [{s.relevance}] {s.item.title} ({why})")
            lines.append(f"  {s.item.url}")
        lines.append("")
    for source in run["sources"]:
        if source["status"] != "ok":
            lines.append(f"! {source['id']}: {source['status']} {source['error_code']}")
    spend = run["spend"]
    lines.append(
        f"Spend: {spend['model_calls']} model calls, "
        f"${spend['estimated_cost_usd']:.4f} (ceiling ${spend['max_cost_usd']:.4f})."
    )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


def do_run(args, opener=None) -> int:
    now = now_utc()
    state_dir = pathlib.Path(os.environ.get("SCOUT_STATE_DIR", DEFAULT_STATE_DIR))
    watch_path = pathlib.Path(
        args.watchlist or os.environ.get("SCOUT_WATCHLIST") or DEFAULT_WATCHLIST
    )
    watchlist, watch_sha = load_watchlist(watch_path)

    defaults = watchlist.get("defaults", {})
    budget = watchlist.get("budget", {})
    spend_cfg = watchlist.get("spend", {})
    min_relevance = int(defaults.get("min_relevance", 3))
    feed_policies = {feed["id"]: feed for feed in watchlist.get("feeds", [])}
    max_age = int(defaults.get("max_age_hours", 168))

    fetcher = Fetcher(budget, opener=opener)
    run_id = now.strftime("%Y-%m-%dT%H:%M:%SZ") + "-" + os.urandom(4).hex()
    status = "ok"
    source_records = []
    all_items: list[Item] = []

    for source_id, cfg in watchlist.get("sources", {}).items():
        adapter = ADAPTERS[source_id]
        record = {
            "id": source_id,
            "status": "ok",
            "requests": 0,
            "fetched": 0,
            "error_code": "",
        }
        errors: list[str] = []
        for req in adapter.plan(cfg, watchlist, now):
            # Isolation is per *request*, not per adapter. Six feeds sit behind
            # the rss adapter, so "rss failed" would not say which one, and an
            # unactionable error is the kind of red that gets ignored.
            try:
                payload = fetcher.get(req, adapter.hosts)
                record["requests"] += 1
                items = adapter.parse(req, payload)
                record["fetched"] += len(items)
                all_items.extend(items)
            except Refusal:
                raise
            except BudgetExceeded as exc:
                # Budget clipping degrades the run; it does not lose what we
                # have, and no later request can succeed either.
                errors.append(f"{req.label}: {exc}")
                record["status"] = "partial"
                break
            except (
                UnsafePayload,
                # OSError, not URLError. A read timeout surfaces as a bare
                # TimeoutError from the socket layer, which is an OSError but
                # not a URLError, so a URLError-only catch let it escape and
                # kill the run with a traceback and no digest. URLError and
                # HTTPError are OSError subclasses, so this covers them too.
                OSError,
                ET.ParseError,
                json.JSONDecodeError,
                ValueError,
                KeyError,
            ) as exc:
                errors.append(f"{req.label}: {type(exc).__name__}: {exc}")
                record["status"] = "error"
        if errors:
            record["error_code"] = "; ".join(errors)
            status = "partial"
        source_records.append(record)

    seen = load_seen(state_dir / "seen.tsv")
    stamp = now.isoformat()
    reported: list[Scored] = []
    near: list[Scored] = []
    n_muted = n_seen = n_matched = 0
    collapsed: set[str] = set()

    for item in all_items:
        if (now - item.published).total_seconds() > max_age * 3600:
            continue
        if muted_by(item, watchlist):
            n_muted += 1
            continue
        relevance, matched = score(item, watchlist)
        feed = feed_policies.get(item.extra.get("feed"), {})
        if relevance >= max(min_relevance, feed.get("min_relevance", min_relevance)):
            n_matched += 1
        elif relevance > 0:
            # Anything that matched *something* but not enough. Deliberately
            # wider than "one point below": the first live run scored 48 of 49
            # items at zero, so a one-point band reported nothing and the
            # recall feedback could not tell an over-narrow watchlist from a
            # quiet week. Zero-scoring items stay invisible, correctly.
            near.append(Scored(item, relevance, matched))
            continue
        else:
            continue
        key = seen_key(item)
        url_key = f"url:{normalize_url(item.url)}"
        if key in seen or url_key in seen or url_key in collapsed:
            # Seen means *reported*, not fetched. An item filtered out today at
            # a low score must stay eligible when it climbs tomorrow.
            n_seen += 1
            continue
        collapsed.add(url_key)
        reported.append(Scored(item, relevance, matched))

    reported.sort(key=lambda s: (-s.relevance, s.item.title))

    # Cap per feed before the global cap. Without this the highest-volume
    # source wins on volume alone regardless of relevance: the first live run
    # returned 23 arXiv papers and nothing else, because one firehose feed
    # outnumbered every other source combined.
    per_feed = int(defaults.get("max_items_per_feed", 6))
    kept: list[Scored] = []
    counts: dict[str, int] = {}
    for s in reported:
        group = s.item.extra.get("feed", s.item.source)
        feed_cap = min(per_feed, feed_policies.get(group, {}).get("max_items", per_feed))
        if counts.get(group, 0) >= feed_cap:
            continue
        counts[group] = counts.get(group, 0) + 1
        kept.append(s)
    reported = kept[: int(defaults.get("max_digest_items", 25))]

    for scored in reported:
        item = scored.item
        row = {
            "first_seen": stamp,
            "last_seen": stamp,
            "source": item.source,
            "title_prefix": item.title[:60].replace("\t", " "),
        }
        # Record only emitted items. A capped-out item must remain eligible
        # tomorrow. The native id and normalized URL suppress later mirrors.
        seen[seen_key(item)] = row
        seen[f"url:{normalize_url(item.url)}"] = dict(row)

    run = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "operation": OPERATION,
        "trigger": os.environ.get("SCOUT_TRIGGER", "manual"),
        "watchlist_sha256": watch_sha,
        "started_at": stamp,
        "status": status,
        "sources": source_records,
        "totals": {
            "requests": fetcher.requests,
            "bytes": fetcher.bytes,
            "fetched": len(all_items),
            "matched": n_matched,
            "reported": len(reported),
            "suppressed_seen": n_seen,
            "suppressed_muted": n_muted,
            "near_miss": len(near),
        },
        "spend": {
            "currency": "USD",
            "provider": "",
            "model": "",
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0,
            "cost_source": "none",
            "max_cost_usd": float(spend_cfg.get("max_cost_usd", 0.0)),
        },
        "actions": list(fetcher.actions),
    }

    # The ceiling is enforced in v0 while it is zero, so any future code path
    # that makes a model call fails here immediately. A ledger that has never
    # been able to fail is not instrumentation.
    if run["spend"]["model_calls"] > int(spend_cfg.get("max_model_calls", 0)):
        raise Refusal("spend ceiling: model_calls exceeded")
    if run["spend"]["estimated_cost_usd"] > run["spend"]["max_cost_usd"]:
        raise Refusal("spend ceiling: estimated_cost_usd exceeded")

    digests = state_dir / "digests"
    digests.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(run, reported, near, now)
    write_atomic(digests / f"{run_id}.md", markdown)
    run["actions"].append(
        {"kind": "write_file", "target": str(digests / f"{run_id}.md"), "count": 1}
    )
    # exit_code is set here rather than after the write, so the run block inside
    # the digest matches the one appended to runs.jsonl. A consumer reading only
    # the digest should not see a different record from one reading the ledger.
    run["exit_code"] = 1 if status == "partial" else 0
    payload = {
        "run": run,
        "items": [
            {**dataclasses.asdict(s.item), "published": s.item.published.isoformat(),
             "tags": list(s.item.tags), "relevance": s.relevance, "matched": s.matched}
            for s in reported
        ],
    }
    write_atomic(
        digests / f"{run_id}.json",
        json.dumps(payload, indent=2, sort_keys=True, default=str),
    )
    run["actions"].append(
        {"kind": "write_file", "target": str(digests / f"{run_id}.json"), "count": 1}
    )

    save_seen(state_dir / "seen.tsv", seen, now, int(os.environ.get("SCOUT_SEEN_TTL_DAYS", 180)))
    run["actions"].append(
        {"kind": "append_file", "target": str(state_dir / "seen.tsv"), "count": len(reported)}
    )
    with (state_dir / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(run, sort_keys=True, separators=(",", ":")) + "\n")

    if args.stdout:
        sys.stdout.write(markdown)
    print(
        f"scout {run_id}: {len(reported)} reported, {n_seen} seen, {n_muted} muted, "
        f"{fetcher.requests} requests, status={status}",
        file=sys.stderr,
    )
    return run["exit_code"]


def do_latest(args) -> int:
    state_dir = pathlib.Path(os.environ.get("SCOUT_STATE_DIR", DEFAULT_STATE_DIR))
    digests = sorted((state_dir / "digests").glob("*.md"))
    if not digests:
        print("no digests yet", file=sys.stderr)
        return 1
    sys.stdout.write(digests[-1].read_text(encoding="utf-8"))
    return 0


def do_ledger(args) -> int:
    state_dir = pathlib.Path(os.environ.get("SCOUT_STATE_DIR", DEFAULT_STATE_DIR))
    path = state_dir / "runs.jsonl"
    if not path.exists():
        print("no runs yet", file=sys.stderr)
        return 1
    runs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if args.json:
        print(json.dumps(runs, indent=2))
        return 0
    print(f"{'run':38} {'status':8} {'req':>4} {'rep':>4} {'cost':>8}")
    for run in runs:
        print(
            f"{run['run_id']:38} {run['status']:8} {run['totals']['requests']:>4} "
            f"{run['totals']['reported']:>4} {run['spend']['estimated_cost_usd']:>8.4f}"
        )
    total = sum(r["spend"]["estimated_cost_usd"] for r in runs)
    print(f"\n{len(runs)} runs, total spend ${total:.4f}")
    return 0


def do_check(args) -> int:
    path = pathlib.Path(
        args.watchlist or os.environ.get("SCOUT_WATCHLIST") or DEFAULT_WATCHLIST
    )
    if not path.exists() and REPO_WATCHLIST.exists():
        path = REPO_WATCHLIST
    watchlist, sha = load_watchlist(path)
    feeds = watchlist.get("feeds", [])
    for feed in feeds:
        Fetcher({}).check_host(feed["url"], ADAPTERS["rss"].hosts)
    print(f"watchlist ok: {path}")
    print(f"  sha256 {sha}")
    print(f"  {len(watchlist.get('topics', []))} topics, {len(feeds)} feeds")
    return 0


def main(argv=None, opener=None) -> int:
    parser = argparse.ArgumentParser(prog="scout", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="fetch, filter and write one digest")
    run_p.add_argument("--watchlist", help="override the watchlist path")
    run_p.add_argument("--stdout", action="store_true", help="also print the digest")
    sub.add_parser("latest", help="print the most recent digest")
    ledger_p = sub.add_parser("ledger", help="summarize runs and spend")
    ledger_p.add_argument("--json", action="store_true")
    check_p = sub.add_parser("check", help="validate the watchlist offline")
    check_p.add_argument("--watchlist")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "run":
            return do_run(args, opener=opener)
        if args.cmd == "latest":
            return do_latest(args)
        if args.cmd == "ledger":
            return do_ledger(args)
        return do_check(args)
    except Refusal as exc:
        print(f"scout: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
