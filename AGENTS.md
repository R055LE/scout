# AGENTS.md: scout

A deterministic discovery digest. Walks a watchlist of public feeds, scores each
item against explicit weighted topics, and writes the survivors with the reason
each one matched. No model, no credential, no daemon.

Its first consumer is `R055LE/roger`, whose scheduled brains read the digest
instead of fetching feeds (Roger ADR-0012). It is deliberately not part of
Roger: folding a digest builder into a Discord bot means any other consumer has
to go through Discord to reach the data.

## Read first

- **`docs/scout.md`** is the contract: the fixed operation, the output schema
  consumers rely on, the exit codes, and how tuning works.
- **`config/watchlist.json`** is the filter policy. It is the thing you will
  actually change, and every topic and mute carries a `why`.

## No model, on purpose

A model would rank better. It would also make every bad digest ambiguous: was
the watchlist wrong, or the ranker? Deterministic scoring keeps that legible.
Every reported item prints the topic and term that surfaced it, and the footer
lists near misses, so an over-narrow watchlist reads differently from a quiet
week.

If you are tempted to add a model here, don't. Ranking belongs in a consumer, or
in a second fixed operation with its own credential boundary, because it flips
this tool from no-credential to credential and that is a separate authority
change.

## No dependencies, twice tested

`pyproject.toml` has an empty `dependencies` list and that is load-bearing.
`feedparser` was rejected in the reuse evaluation on update-ownership grounds,
and an hour later a security review recommended `defusedxml` for the same
problem and was rejected for the same reason. Taking the second one would have
made the first rejection arbitrary.

Both have recorded revisit triggers. `feedparser`: repeated real-world parse
failures in `runs.jsonl`. `defusedxml`: if the expat pre-pass ever costs
meaningful time in run timings.

## The output is an interface

`data/digests/<run_id>.json` is consumed by another service. Consequences:

- **Writes are atomic.** Temp file plus rename, so a poller sees a file either
  absent or complete. This was a real defect, found when the directory stopped
  being an output folder and became an integration surface.
- **`run_id` sorts chronologically** as a plain string, so "newest" is the last
  entry of a sorted glob. Do not add an index file to keep consistent.
- **The embedded `run` block matches the `runs.jsonl` record** for the same
  run, `exit_code` included. Reading the digest and reading the ledger must give
  the same answer.
- Adding a field is fine. Removing or renaming one breaks a consumer, so it
  needs a `schema_version` bump and a note in `docs/scout.md`.

## Exit codes are not fleet-audit's

`0` ran, `1` partial, `2` refusal. Reporting items is success here, unlike
`runbook`'s audit where `1` means findings. A timer-driven oneshot that exits
non-zero whenever it works trains an operator to ignore red.

## A check that cannot fail is not a check

The spend ceiling ships at zero and is enforced, so any future code path that
makes a model call refuses. The network guard is mutation-tested by an adapter
that deliberately attempts a real fetch. Keep both that way: a ledger that has
never been able to fail is not instrumentation.

## Deploying

Batch job, not a service. `scout-run.timer` invokes
`docker compose run --rm scout run` to completion; nothing runs continuously.
`scout-deploy.timer` only pulls and cosign-verifies the image, so a pull failure
and a run failure are diagnosable apart. See `deploy/README.md`.

The container runs as a fixed uid 10002 and the host data directory is created
owned by it. That is what stops the bind mount leaving root-owned files nobody
can clean up without sudo.

## Claude Code specifics

`CLAUDE.md` is a symlink to this file, per `R055LE/runbook` `decisions/0012`.
Fleet-wide conventions, including the worktree and PR recipe this repo's
default-branch protection requires, live in the mirror root's `AGENTS.md`.
