# Quickstart

Everything below is safe by default: the service ships **inert** and `CALSYNC_ALLOW_WRITES=0`. Do the
first steps with a throw-away or test calendar pair if you can.

## 1. Install and run the tests (no accounts needed)

```sh
git clone <your-copy-of-this-repository> calsync && cd calsync
python3 -m venv .venv-test
.venv-test/bin/pip install -r requirements.lock radicale pytest
scripts/test.sh                                # synthetic data + local Radicale only
python3 tests/fixtures/gen_synthetic.py --check
```

## 2. Credentials (kept outside the repository)

- **iCloud**: an *app-specific password* (appleid.apple.com), never your Apple ID password. `ICLOUD_USER`, `ICLOUD_APP_PASSWORD`.
- **Google**: an OAuth client (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`) and a token file created once on a
  workstation with `scripts/google-oauth-local.sh`; see [GOOGLE-OAUTH.md](GOOGLE-OAUTH.md).

Pass them as environment variables or `*_FILE` variables pointing at files. Keep them in an untracked
`.env` (git-ignored) or your orchestrator's secret store. Never commit them.

## 3. Configuration

```sh
mkdir -p ~/calsync-local && cp config/calsync.example.toml ~/calsync-local/calsync.toml
```

Edit the copy outside the repository: set `self_addresses` (**all** your own addresses on both providers)
and your pairs. Pairs stay `mode = "disabled"` until you change them.

After step 4 has created the container, copy the edited file into the `/config` volume
(`docker compose cp ~/calsync-local/calsync.toml calsync:/config/calsync.toml`) and restart the service.
The image itself only contains the inert example.

## 4. Run inert with Docker Compose

```sh
cp .env.example .env            # fill in values; .env is git-ignored
docker compose config --quiet   # validate
docker compose up -d --build
docker compose logs calsync     # expect: "running INERT"
docker compose exec calsync python -m calsync status
```

No port is published. The health endpoint binds to `127.0.0.1` inside the container
(`CALSYNC_HEALTH_BIND`, `CALSYNC_HEALTH_PORT`); the container `HEALTHCHECK` does not need a network port.
`/status` is served on loopback only unless `CALSYNC_HEALTH_STATUS=1`.

The timezone is `TZ` (default `UTC`). Calendars created by calsync use `--timezone`, `CALSYNC_TIMEZONE`,
`TZ` or `UTC`, in that order.

## 5. Find your calendars (read-only)

```sh
docker compose exec calsync python -m calsync list-calendars
```

Copy the iCloud collection id and Google calendar id into your pair in `calsync.toml`.

## 6. Mirror and audit (still read-only)

Set the pair to `mode = "mirror"`, redeploy, then:

```sh
docker compose exec calsync python -m calsync audit --pair my_pair --detail
```

Review duplicates, guest invitations, birthdays and unsupported items. Record decisions with
`calsync decide`.

## 7. Back up, then enable writes

Set up encrypted off-host backups ([BACKUP-RESTORE.md](BACKUP-RESTORE.md)), prove a restore, and only then
follow [ENABLING-WRITES.md](ENABLING-WRITES.md): `plan` → review → `approve-plan` → `seed` → `live`, one pair
and one direction at a time. Stop at any time with `calsync pause` and `CALSYNC_ALLOW_WRITES=0`.
