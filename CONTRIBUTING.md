# Contributing

Small, safe, well-tested changes are welcome. The project is intentionally narrow: keeping eligible events
consistent between one iCloud calendar and one Google calendar, self-hosted. No UI and no hosted service.

## Ground rules

- **Never use real calendars, accounts or credentials** in tests, issues or pull requests. Tests run offline
  against synthetic data, local stub servers and a local Radicale instance on loopback.
- Never commit real calendar exports, tokens, `.env` files, private configs or backups. Real captures belong in
  `.private-fixtures/` (ignored). Do not copy titles, names, addresses or locations from real data into fixtures.
- Safety first: do not weaken write gates, add default-on writes, or loosen origin/XML/size checks.
- Keep stable identifiers stable (link ids, resource names) unless you provide a migration.

## Development

```sh
python3 -m venv .venv-test
.venv-test/bin/pip install -r requirements.lock radicale pytest
scripts/test.sh                                  # full suite incl. Radicale + vdirsyncer
python3 tests/fixtures/gen_synthetic.py --check  # synthetic fixture corpus check
docker compose config --quiet                    # compose validation
```

Run tests from outside the working tree with an isolated `HOME` and `CALSYNC_ALLOW_WRITES=0` (the default).
Edit `tests/fixtures/gen_synthetic.py` and regenerate instead of hand-editing `.ics` fixtures.

## Pull requests

Describe the behavior change and the safety impact, add or update tests, and keep the diff focused.
By contributing you agree that your contribution is licensed under the MIT license of this project.
