# Deploying scout

Scout is a **batch job**, not a service. Nothing runs continuously. Two timers
with separate jobs:

| Timer | Does | Why separate |
|---|---|---|
| `scout-deploy.timer` | pull the image, cosign-verify it | a pull failure and a run failure are different problems |
| `scout-run.timer` | `docker compose run --rm scout run` | the fixed operation, run to completion |

Splitting them means a broken image and a broken digest never look alike in the
journal.

## Install

```sh
scp -r deploy <host>:/tmp/scout-src
ssh <host> 'sudo bash /tmp/scout-src/install-systemd.sh'
```

The installer creates `/opt/scout/data` owned by **uid 10002**, which is the
container's runtime user. Letting Docker create it instead would make it
root-owned, and then nothing without sudo can clean it up. Mode is 0755 so
Roger's container, running as a different uid, can read the digests.

Put `compose.yaml` and `config/watchlist.json` in `/opt/scout/` on the host.

## Supply chain

`scout-deploy` verifies the keyless cosign signature against the release
workflow's OIDC identity before the image is ever used. `set -e` means a bad or
missing signature aborts. Fail closed. cosign must be on `PATH` for the systemd
unit, so install it to `/usr/local/bin`.

Image pruning belongs in coordinated host maintenance. On a shared Docker
daemon, cleanup must hold every image consumer's project lock so it cannot
overlap a pull or Scout run. Updating this repository does not update
`/usr/local/bin/scout-deploy`; explicitly reinstall the approved host-side
script before relying on a control change.

## Checking on it

```sh
systemctl list-timers 'scout-*'
journalctl -u scout-run.service -n 50
docker compose run --rm scout ledger     # runs, requests, items, spend
docker compose run --rm scout latest     # the digest itself
```

Exit codes from `scout-run.service` are the tool's own: `0` ran, `1` partial (a
feed failed or a budget clipped), `2` refusal (bad watchlist, off-allowlist
host, spend ceiling breached). A non-zero status is how a broken producer
becomes visible without anyone reading a digest.

## Rollback

```sh
sudo systemctl disable --now scout-run.timer scout-deploy.timer
sudo rm /etc/systemd/system/scout-{deploy,run}.{service,timer}
sudo rm /usr/local/bin/scout-{deploy,run}
sudo systemctl daemon-reload
sudo rm -rf /opt/scout        # data: digests, seen state, the run ledger
```

Losing the data directory costs one noisy digest, because seen-state resets and
recent items get reported again. Nothing else depends on it surviving.

Roger reports a missing producer while Scout is gone, which is the designed
failure mode rather than a silent quiet day.
