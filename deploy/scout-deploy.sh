#!/usr/bin/env bash
# scout-deploy — pull the latest published scout image and verify its signature.
#
# Deliberately does NOT run scout. Scout is a batch job: scout-run.timer invokes it. This script
# only keeps the image current, so a pull and a run can fail independently and be diagnosed apart.
set -euo pipefail

DEPLOY_DIR="${SCOUT_DEPLOY_DIR:-/opt/scout}"
cd "$DEPLOY_DIR"

exec 9>"${DEPLOY_DIR}/.deploy.lock"
flock -n 9 || { echo "scout-deploy: another run holds the lock, skipping"; exit 0; }

echo "scout-deploy: pulling"
docker compose pull --quiet

# Supply-chain gate: only ever run an image this repo's release workflow signed. set -e means a bad
# or absent signature aborts here, before the run timer can use it. Fail closed.
IMAGE="ghcr.io/r055le/scout:main"
COSIGN_IDENTITY="https://github.com/R055LE/scout/.github/workflows/release.yml@refs/heads/main"
COSIGN_ISSUER="https://token.actions.githubusercontent.com"
echo "scout-deploy: verifying image signature (cosign)"
cosign verify \
  --certificate-identity "$COSIGN_IDENTITY" \
  --certificate-oidc-issuer "$COSIGN_ISSUER" \
  "$IMAGE" >/dev/null

echo "scout-deploy: pruning superseded images"
docker image prune -f >/dev/null
