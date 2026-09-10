# Pinned by digest (not just the moving 3.14-slim tag) so builds are reproducible; Dependabot bumps
# the digest + comment on a new base release. Same pin as R055LE/roger, which shares this host.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

# Non-root runtime user with a fixed uid/gid so the host can chown the bind-mounted /data to match.
# That fixed uid is what stops a bind mount leaving root-owned files behind, which is a documented
# way to strand a directory nobody can clean up without sudo.
#
# APT_CACHE_BUST (release.yml sets it per run) forces apt-get update to actually re-run rather than
# reuse a cached layer, so a Debian security patch between digest bumps still reaches the image.
ARG APT_CACHE_BUST=0
RUN echo "apt cache bust: ${APT_CACHE_BUST}" \
 && groupadd --system --gid 10002 scout \
 && useradd --system --uid 10002 --gid 10002 --home-dir /app --no-create-home scout \
 && apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY scout ./scout
RUN pip install --no-cache-dir . \
 && pip uninstall --yes pip setuptools

RUN mkdir -p /data /config && chown scout:scout /data

USER scout
ENV PYTHONUNBUFFERED=1

ARG SCOUT_VERSION=dev
ENV SCOUT_VERSION=${SCOUT_VERSION}

# No HEALTHCHECK and no long-running process: this is a fixed operation, run to completion by a
# timer. `docker compose run --rm scout run` is the whole contract.
ENTRYPOINT ["python", "-m", "scout"]
CMD ["run"]
