# Docker Compose / Coolify deployment

`compose.yaml` works with plain `docker compose` and as a Coolify "Docker Compose" resource. It ships **inert**.

## Recommended setup

Use a **private** source repository (or a private image in a trusted registry), deploy from `compose.yaml`, and keep
all environment variables in the platform's secret store. Never put credential values in `compose.yaml` or Git. The
build uses a pinned base image and the dependency lock `requirements.lock`.

Persistent volumes:
- `calsync-data` mounted at `/data` (state, mirrors, tokens, manifests, local backup staging)
- `calsync-config` mounted at `/config` (`calsync.toml`, seeded inert from the image's example on first start)

If you use a prebuilt image, keep all mounts and safety flags of `compose.yaml`.

Networking: **no port is published and no domain or proxy is required.** The health endpoint listens on
`127.0.0.1` inside the container (`CALSYNC_HEALTH_BIND`, `CALSYNC_HEALTH_PORT`) and the container `HEALTHCHECK` runs
`python -m calsync healthcheck`. Do not expose it publicly. Restrict who can read the mirrors, backups and secrets.

Environment: `TZ` (default `UTC`), `CALSYNC_ALLOW_WRITES` (default `0`), credentials as described in
[GOOGLE-OAUTH.md](GOOGLE-OAUTH.md) (`GOOGLE_CLIENT_SECRET`, not a suffixed variant), alerts and backup settings from `.env.example`.

## First start

All pairs disabled, `CALSYNC_ALLOW_WRITES=0`. Verify that `calsync status` shows `state: inert` and the logs do not
reveal secrets. Do not enable writes without raw backups and approved plans: see [ENABLING-WRITES.md](ENABLING-WRITES.md).

## Verification

After the inert deployment, check build logs (secrets redacted), the health check, named volumes, restart behaviour and
the resource limits (512 MB RAM, 0.5 CPU). Then enable one pair in `mirror` mode at a time. Off-host backups are
mandatory before writes.
