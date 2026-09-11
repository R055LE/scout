#!/usr/bin/env bash
# install-systemd.sh — install the scout-deploy script + poll timer on the host. Idempotent.
#
# Run from a checkout of deploy/ (not piped over stdin — it reads its sibling files):
#   scp -r deploy <host>:/tmp/scout-src
#   ssh <host> 'sudo bash /tmp/scout-src/install-systemd.sh'
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

install -m 0755 "$SRC/scout-deploy.sh" /usr/local/bin/scout-deploy
install -m 0755 "$SRC/scout-run.sh" /usr/local/bin/scout-run

# The deploy runs as the (non-root) invoking user so Docker isn't driven as root and the age
# key stays in that user's home. Fill the unit's placeholder with whoever ran sudo.
DEPLOY_USER="${SUDO_USER:?run via sudo so the deploy user is known}"
for unit in scout-deploy scout-run; do
  sed "s/__DEPLOY_USER__/${DEPLOY_USER}/" "$SRC/${unit}.service" \
    > "/etc/systemd/system/${unit}.service"
  chmod 0644 "/etc/systemd/system/${unit}.service"
  install -m 0644 "$SRC/${unit}.timer" "/etc/systemd/system/${unit}.timer"
done

# The data directory is bind-mounted into a container running as uid 10002. Create it owned by that
# uid up front: letting Docker create it would make it root-owned, and then nothing without sudo can
# clean it up. Mode 755 so Roger's container (a different uid) can read the digests.
#
# mkdir+chown, not `install -d -o -g`: uutils coreutils' install rejects a bare numeric
# owner with no matching passwd entry, and 10002 is a container-only uid with none on the
# host. chown accepts numeric IDs unconditionally; install does not on that implementation.
mkdir -p /opt/scout/data /opt/scout/data/digests
chown 10002:10002 /opt/scout/data /opt/scout/data/digests
chmod 0755 /opt/scout/data /opt/scout/data/digests

systemctl daemon-reload
systemctl enable --now scout-deploy.timer scout-run.timer

echo "Installed. Timers:"
systemctl list-timers scout-deploy.timer scout-run.timer --no-pager || true
echo
echo "Pull an image now with:   sudo systemctl start scout-deploy.service"
echo "Produce a digest now with: sudo systemctl start scout-run.service"
echo "Follow logs with:          journalctl -u scout-run.service -f"
