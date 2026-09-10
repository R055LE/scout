<!-- Delete everything down to the line marked END SETUP once you've done it. -->

# New repo setup

You made this from `HalcyonOps/repo-template`. Four steps and the fleet audit
goes quiet. Skipping them is how repos drift, which is the whole reason this
template exists.

1. **Install the commit guard.** It's not automatic, git never copies hooks.

   ```bash
   cp scripts/pre-commit-worktree-guard .git/hooks/pre-commit
   ```

2. **Check the license.** MIT is here because most code is meant to be reused.
   Swap it if that's not this repo (see
   [ADR-0006](https://github.com/R055LE/runbook/blob/main/decisions/0006-license-by-reuse-intent.md)):
   CC-BY-4.0 for citable reference content, delete it entirely for original
   creative IP where all-rights-reserved is the point. If you delete it and the
   repo is public, add the repo to `NO_LICENSE_BY_DESIGN` in `fleet-audit.py`
   with a reason.

3. **Add the component to the catalog.** Description and topics are generated,
   so don't set them by hand. In `R055LE/runbook`, add an entry to
   `catalog/fleet.yaml` and run:

   ```bash
   ./scripts/catalog.py apply --only <owner>/<repo>
   ```

4. **Apply the repo settings.** Everything the audit checks that isn't a file:

   ```bash
   ./scripts/bootstrap-repo.sh <owner>/<repo>      # in R055LE/runbook
   ./scripts/fleet-audit.py --only <owner>/<repo>  # should print nothing
   ```

<!-- END SETUP -->

# <repo name>

One or two sentences on what this is and who it's for. The GitHub description
is generated from the catalog, so this is the place to actually explain it.

## Usage

```bash
```

## Development

```bash
```

## License

MIT, see [LICENSE](LICENSE).
