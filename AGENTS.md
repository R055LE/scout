# <repo name>

<!-- One or two sentences on what this is, then delete this comment. Keep this
file short. It's for the things someone would otherwise have to break to
discover, not a second README. -->

## Fleet rules that govern this repo

Decisions live in
[`R055LE/runbook/decisions`](https://github.com/R055LE/runbook/tree/main/decisions),
indexed in that directory's README. Read the one covering what you're about to
change. Most are asserted by `fleet-audit.py`, so breaking one shows up as drift
on the Monday run. The index marks the ones that aren't, which are the ones
where reversing the decision looks like tidying up.

The two that catch people out:

- **Description and topics are generated** from `catalog/fleet.yaml` in
  `runbook` ([0002](https://github.com/R055LE/runbook/blob/main/decisions/0002-centralise-catalog-authoring.md)).
  Setting them by hand gets overwritten.
- **Routine dependency updates are opt-in**
  ([0009](https://github.com/R055LE/runbook/blob/main/decisions/0009-dependabot-as-the-only-dependency-bot.md)).
  This repo ships a `.github/dependabot.yml` because new repos start covered.
  Delete it if the PR traffic isn't worth it, that's a supported choice. Don't
  add a second dependency bot.

## Working here

One issue, one branch, one worktree under `.claude/worktrees/<slug>` (already
gitignored), one PR, then prune. `scripts/pre-commit-worktree-guard` refuses
commits from the canonical clone, but only once it's installed, and git never
copies hooks on clone. See step 1 of the setup in the README.

## Invariants

<!-- Delete this section if there aren't any yet. Add things a change would
silently break: assumptions the tests don't cover, gates that don't run on PRs,
conventions nothing enforces. If a rule defends itself, it doesn't need to be
here. If breaking it leaves everything green, it does. -->
