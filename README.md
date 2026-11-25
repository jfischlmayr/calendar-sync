# CalendarSync — iCloud ↔ Google calendar sync (self-hosted)

> **Status: pre-release candidate.** Not affiliated with or endorsed by Apple or Google. Use at your own
> risk and **take backups first**: this software can create, update and (if you enable it) delete events
> in real calendars. It ships **inert**: nothing is read or written until you configure and enable it.

## Why

Many people use the native Apple Calendar on iPhone and Mac with iCloud calendars. Many third-party tools
(schedulers, assistants, booking and automation tools) integrate only with **Google Calendar via Google
OAuth**. Adding a Google account to Apple Calendar does not help those tools: it shows Google events in
Apple Calendar, but your iCloud events stay invisible to anything that talks to Google.

calsync keeps **eligible events consistent** between one iCloud calendar and one Google calendar, so tools
that use Google see your iCloud events and vice versa. It runs on your own machine or server; there is **no
hosted intermediary**. Calendar synchronization talks directly to Apple and Google. Optional backup and
alerting destinations are configured and controlled by you.

## How it works

A purpose-built plan/apply engine ([docs/ENGINE.md](docs/ENGINE.md)) compares both calendars and applies only
approved, conditional, verified writes. vdirsyncer is used only as a **read-only** filesystem mirror (the
source for backups); it never writes to a provider.

Safety-first by design:

- ships inert; per-pair modes `disabled` → `mirror` (read-only audit) → `seed` / `seed_a` (one side) → `live`
- writes need `CALSYNC_ALLOW_WRITES=1`, no `PAUSE` file, a fresh verified (off-host) backup, and an approved plan per direction
- canary-sized batches, conditional writes (`If-Match` / `If-None-Match`), verify-after-write, kill switch
- guest invitations, birthdays and unsupported items are **exempt** (never touched)
- deletions are reported, not propagated, unless enabled; zero-event and mass-deletion guards; conflicts are never auto-resolved
- Google OAuth refresh is handled by the engine (atomic 0600 token file; `invalid_grant` raises an alert)
- age-encrypted state + ICS backups with off-host upload hooks and a restore drill
- hardened container: non-root, read-only root filesystem, no published port

## Example pairs

| iCloud calendar | Google calendar | Direction |
|---|---|---|
| `Personal` | `Personal` | two-way |
| `Work` (writable) | `Work` | two-way |

Read-only or shared calendars that you do not own must not be configured as a pair.

## Documentation

- [docs/QUICKSTART.md](docs/QUICKSTART.md): install, test, run inert, first audit
- [docs/ENABLING-WRITES.md](docs/ENABLING-WRITES.md): the gated sequence to turn writes on
- [docs/ENGINE.md](docs/ENGINE.md): design, guarantees, limits
- [docs/GOOGLE-OAUTH.md](docs/GOOGLE-OAUTH.md): Google and iCloud credentials
- [docs/BACKUP-RESTORE.md](docs/BACKUP-RESTORE.md), [docs/ROLLBACK.md](docs/ROLLBACK.md): backups, restore, undo
- [docs/COOLIFY.md](docs/COOLIFY.md): Docker Compose / Coolify deployment
- [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md)

## Local QA

```sh
python3 -m venv .venv-test && .venv-test/bin/pip install -r requirements.lock radicale pytest
scripts/test.sh                                              # full suite incl. real vdirsyncer + Radicale
python3 tests/fixtures/gen_synthetic.py --check              # synthetic fixture checker
docker compose config --quiet
```

Tests use only synthetic data and local Radicale servers; never point them at real calendars.

## License

Original code: MIT, see [LICENSE](LICENSE). Third-party components (including a GPL-licensed dependency pulled
in by vdirsyncer) and obligations when distributing container images: see
[docs/THIRD-PARTY-LICENSES.md](docs/THIRD-PARTY-LICENSES.md).
