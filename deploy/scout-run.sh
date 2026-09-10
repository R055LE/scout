#!/usr/bin/env bash
# scout-run — execute one digest run to completion.
#
# The fixed operation, in full: one command, one working directory, no arguments, no credential.
# Exit codes come straight from the tool: 0 ran, 1 partial (a feed failed or a budget clipped),
# 2 refusal (bad watchlist, off-allowlist host, spend ceiling breached). systemd records them, and
# a non-zero status is how a broken producer becomes visible without anyone reading a digest.
set -euo pipefail

DEPLOY_DIR="${SCOUT_DEPLOY_DIR:-/opt/scout}"
cd "$DEPLOY_DIR"

# Serialize against the deploy timer: never run a half-pulled image.
exec 9>"${DEPLOY_DIR}/.deploy.lock"
flock -w 60 9 || { echo "scout-run: could not take the lock, skipping"; exit 0; }

exec docker compose run --rm scout run
