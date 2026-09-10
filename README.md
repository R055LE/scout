# scout

A deterministic discovery digest. It walks a watchlist of public feeds, scores
each item against explicit weighted topics, and writes out the survivors along
with the reason each one matched.

There is no model in it. A model would rank better, and it would also make every
disappointing digest ambiguous: was the watchlist wrong, or the ranker? Scoring
deterministically keeps that legible. Every reported item carries the topic and
term that surfaced it, and the footer lists the near misses, so an over-narrow
watchlist looks different from a quiet week.

```
scout run       # fetch, score, write a digest
scout latest    # print the most recent one
scout ledger    # runs, requests, items, spend
scout check     # validate the watchlist offline
```

## What it is for

Discovery: new projects, patterns, papers. Not a feed reader, and not a
replacement for one. It produces; something else delivers. The first consumer is
[roger](https://github.com/R055LE/roger), whose scheduled brains read the digest
instead of fetching feeds themselves.

Keeping the two apart is deliberate. A digest builder folded into a Discord bot
means every future consumer has to go through Discord to reach the data.

## Shape

No credential. No daemon. No listening socket. Outbound HTTPS only, to a host
allowlist compiled into the tool, and it writes only to its own data directory.
Removing it is deleting a container, a timer and a directory.

Two adapters cover every source: `rss` handles RSS 2.0 and Atom, which is enough
for Hacker News (via hnrss, which does score thresholds server-side), arXiv and
lobste.rs; `github` is bespoke because search has no feed. Adding most sources
is a line of config.

## Tuning

`config/watchlist.json`. Read the near-miss list at the foot of a digest first,
because that is the signal telling you the watchlist is wrong rather than the
week being quiet. Every topic and mute carries a `why` so a stale entry reads as
stale rather than as policy.

Full contract, output schema and exit codes: [`docs/scout.md`](docs/scout.md).
