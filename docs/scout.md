# scout: a deterministic discovery digest

Surfaces external material worth reading (new projects, patterns, papers) from
a small explicit watchlist of public feeds. Tracking issue: [agent-platform#39].

Runs as a batch job on the deploy host: `scout-run.timer` invokes
`docker compose run --rm scout run` to completion. Nothing runs continuously.

## Fixed operation

| | |
|---|---|
| Executable | `docker compose run --rm scout run` from `/opt/scout` |
| Argument | `run`, no other arguments |
| Working directory | `/opt/scout` |
| Inputs | `/config/watchlist.json`, mounted read-only |
| Network | outbound HTTPS GET only, to `HOST_ALLOWLIST` in `scout/cli.py` |
| Credentials | **none** |
| Outputs | `/data/{digests/,seen.tsv,runs.jsonl}` |
| Resources | 25 requests, 8 MiB, 20 s per request, all from `budget` |
| Timeout | per request; no global wall clock yet |
| Stop | `systemctl stop scout-run.service`, or let the run finish |

It holds no credential, mutates no repository or remote service, and writes
only under its state directory. Those properties are what make it eligible to
become an unattended fixed operation later.

## Why there is no model in it

A model would rank better. It would also make every bad digest ambiguous: was
the watchlist wrong, or the ranker? Deterministic scoring keeps that legible.
Every reported item prints the topic and term that surfaced it, and the footer
lists near misses, so an over-narrow watchlist reads differently from a quiet
week.

That is not theoretical. The first live run scored 48 of 49 items at zero and
printed an empty near-miss list, which told us nothing; the band was widened
from "exactly one below threshold" to "matched something but scored under
threshold" as a direct result. The second run immediately showed `LoRA`
matching federated-learning and reranking papers, and that keyword was dropped.

## Reuse evaluation (decision 0035)

Outcome: **borrow, then build**. Evidence observed 2026-09-03, recorded in [agent-platform#39].

Miniflux (AGPL-3.0, 2.3.3, **PostgreSQL-only**, no SQLite) and Newsboat (MIT,
2.44) both cover fetching and dedup well. Both miss the same three requirements,
and those three exist because of the fleet's own contract: reproducible
filtering, per-item match provenance, and the run ledger. Miniflux's filtering
is block/keep regex evaluated as a boolean, which cannot say why an item scored
4 or what almost made it.

The borrow shrank the build materially. Every source except GitHub search
publishes a native feed with server-side filtering, so v0 ships **two adapters,
not five**:

| Source | Feed | Server-side filtering |
|---|---|---|
| Hacker News | `hnrss.org/newest?q=...&points=40` | `points=`, `comments=`, `q=`, `author` |
| arXiv | `rss.arxiv.org/rss/cs.SE` | per-category |
| lobste.rs | `lobste.rs/t/ai,devops.rss` | per-tag, combinable |

Adding most future sources is a watchlist line, not code.

`feedparser` was rejected: this repo has no third-party Python dependency and
`xml.etree` covers RSS 2.0 and Atom. **Revisit trigger:** repeated parse
failures recorded in `runs.jsonl`.

## Two network-facing controls

Both came out of a security review of the phase 1 commit, and the first version
of each was wrong in a way worth recording.

**Feed XML refuses a DTD, in a parser pre-pass.** `xml.etree` does not expand
external entities, so XXE is not the exposure; entity expansion is. Measured,
not assumed: on CPython 3.14 `ET.fromstring` on the billion-laughs fixture
expands a sub-kilobyte payload into a 3000-character title, and the response
byte cap bounds input rather than expansion.

The first version sniffed the first 4 KiB for a literal ASCII `<!DOCTYPE`. Two
bypasses, both now fixtures: a UTF-16 document, where those bytes are not ASCII
but the declaration is still honoured, and more than 4 KiB of leading comments
pushing the declaration past the window. An expat pre-pass with
`StartDoctypeDeclHandler` and `EntityDeclHandler` has neither weakness, because
expat decodes the document itself and reports the declaration wherever it sits.
It rejects at declaration time, before any reference expands.

**Redirects re-check the allowlist on every hop.** `build_opener` installs a
redirect handler by default, so validating only the planned URL left the
allowlist trivially bypassable: an allowlisted feed answering 302 could send
the fetch to a link-local or internal address and the original check never ran
again. `AllowlistRedirectHandler` validates each hop.

Both are `UnsafePayload` rather than `Refusal`: a remote server behaving badly
degrades that one feed and is recorded, the same as a 502. A bad URL the
operator typed is still a `Refusal`, at plan time.

## Results

`digests/<run_id>.md` is what a human reads. `runs.jsonl` appends one record per
run, including on failure, carrying per-source counts, a derived `actions` trace,
and a `spend` block.

`digests/<run_id>.json` is the **consumer contract**, described below.

**The spend ceiling is enforced now, while it is zero.** Any code path that ever
makes a model call breaches `max_model_calls: 0` and refuses. A ledger that has
never been able to fail is not instrumentation, so `test-scout` mutation-tests
it.

## The consumer contract

Scout produces; it does not deliver. Delivery belongs to a consumer, and there
is expected to be more than one: Roger posts to Discord today, and a different
front end later should not require unpicking the digest from a Discord bot.
That separation is the reason this is a standalone tool rather than a Roger
module, given Roger already ships Digest, personal-digest and Spark brains over
its own feed plumbing.

`digests/<run_id>.json`:

```json
{
  "run":   { "run_id": "...", "schema_version": 1, "status": "ok|partial",
             "exit_code": 0, "started_at": "...", "totals": {...},
             "sources": [...], "spend": {...}, "actions": [...] },
  "items": [ { "title": "...", "url": "...", "source": "rss|github",
               "published": "2026-09-03T04:00:00+00:00",
               "relevance": 3,
               "matched": [ {"topic": "...", "term": "...", "field": "text"} ],
               "summary": "...", "tags": [], "author": "",
               "source_score": 0, "native_id": "...", "extra": {"feed": "..."} } ]
}
```

Guarantees a consumer may rely on, each asserted in `test-scout`:

- **Files appear atomically.** Written to `<name>.partial` and renamed, so a
  poller sees a file either absent or complete, never truncated.
- **`run_id` sorts chronologically** as a plain string, so "newest" is the last
  entry of a sorted glob. No index file to keep consistent.
- **Every item carries every contract field**, including `matched`, so a
  consumer can show why an item surfaced without re-deriving it.
- **The embedded `run` block matches the `runs.jsonl` record** for the same
  `run_id`, `exit_code` included. Reading the digest and reading the ledger give
  the same answer.
- **`items` contains only what passed the threshold.** Near misses appear in the
  markdown for tuning, not in the JSON, because they are a signal to the
  operator rather than to a consumer.

Not provided, deliberately: no push, no callback, no server, no `latest`
symlink. A consumer polls the directory. Adding a delivery mechanism here would
recreate inside Scout the coupling that keeping it separate was meant to avoid.

Exit codes differ from `fleet-audit.py`, where 1 means findings. Here, reporting
items is success:

| Code | Meaning |
|---|---|
| 0 | ran, with or without items |
| 1 | partial: a feed failed, a budget clipped |
| 2 | refusal: bad watchlist, off-allowlist host, spend ceiling breached |

A oneshot unit exiting 1 whenever it works would train us to ignore red.

## Reading it

```sh
docker compose run --rm scout run       # fetch and write a digest
docker compose run --rm scout latest    # print the most recent digest
docker compose run --rm scout ledger    # runs, requests, items, spend
docker compose run --rm scout check     # validate the watchlist offline
```

From a checkout, without the container: `pip install -e '.[dev]'` then
`SCOUT_CONFIG_DIR=config SCOUT_STATE_DIR=/tmp/scout scout run`.

## Tuning

Edit `config/watchlist.json`. Read the near-miss list first: it is
the recall signal, and it is what tells you the watchlist is wrong rather than
the week being quiet. Every topic and mute carries a `why` so a stale entry
reads as stale rather than as policy.

`max_items_per_feed` (default 6) caps each feed before the global cap. Without
it the highest-volume source wins on volume alone: the first capped run cut
arXiv's share from 23 items to 6.

An individual RSS feed may set a higher `min_relevance` or a lower `max_items`
than those defaults. Scout's September 2026 review of 33 production runs found
120 of 141 reported items came from the two arXiv feeds, while the HN agent
search feed failed in 18 runs and contributed only two items. The watchlist now
requires a cross-topic score from broad `arxiv-ai`, limits both arXiv feeds,
and disables the failing HN search. Capped-out items remain eligible in a later
run; they are marked seen only when emitted.

## Local tests

```sh
pytest
```

41 offline tests, no network and no host state: pinned clock, temp state
directory, injected opener. Fixture digests are asserted, so editing a fixture
is deliberate. Mutation-tested: the network guard, the spend ceiling, the DTD
refusal, seen-state suppression, threshold changes, budget clipping.

## Why a timer is legitimate here

`runbook/docs/model-routing.md` ("Unattended work") allows unattended execution
only for a named fixed operation with a documented command, working directory,
inputs, resources, outputs, timeout and stop procedure, and adds: *"Add a timer
only after the same fixed operation succeeds under supervision and its output
has a real consumer."*

Both conditions are met. The operation is the table above, and it was run under
supervision repeatedly during development before any timer existed. The consumer
is Roger, which reports a missing or stale producer rather than posting nothing,
so a Scout that stops running is visible without anyone reading a digest.

It holds no credential, mutates no repository or remote service, and writes only
to its own data directory. Those properties are what keep it eligible.

An earlier draft of this document described phases tied to a systemd user unit on
the development machine. That was wrong: the only consumer runs on the deploy
host, and a production dependency cannot live on the development box. The tool
did not change; its packaging did.


## Deferred: model ranking

Not a flag on `scout`. No Anthropic or OpenAI key is reachable from a script
(`credential_custody: managed-client`); only DeepSeek is brokered, as a
root-owned fixed argv. Ranking becomes a *second* fixed operation with its own
broker policy entry, taking `digests/<run_id>.json` as fixed input. That flips
the operation from no-credential to credential, a separate authority change that
does not inherit the timer's approval.

[agent-platform#39]: https://github.com/R055LE/agent-platform/issues/39
