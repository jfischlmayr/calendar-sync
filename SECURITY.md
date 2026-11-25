# Security policy

calsync handles calendar data and credentials (iCloud app-specific password, Google OAuth client and token).
It is pre-release software: use it at your own risk and keep independent backups of your calendars.

## Reporting a vulnerability

Please report security issues privately, **not** in a public issue: use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on the repository once published. Include affected version/commit,
impact and a minimal reproduction using **synthetic** data. Never send real calendar exports, tokens or passwords.
Expect an acknowledgement as time permits; this is a spare-time project without an SLA.

## Design stance (what the project tries to guarantee)

- Ships inert: no pair enabled, `CALSYNC_ALLOW_WRITES=0`, no published network port, health endpoint on loopback.
- Remote writes are gated (write switch, fresh verified backup, approved plan, no `PAUSE`), conditional
  (`If-None-Match`/`If-Match`) and verified after write.
- Credentials come only from environment variables or `*_FILE` secrets; they are not written into configuration,
  logs or error messages. Transport refuses cross-origin redirects and non-HTTPS endpoints (loopback excepted for
  tests), bounds response sizes and refuses XML with DOCTYPE/ENTITY.
- Backups are `age`-encrypted to a public recipient; restored trees are created with owner-only permissions.

## Known limits

- SHA-1 is used only to derive stable, non-secret identifiers (resource names, link ids), never for integrity or
  authentication. Changing it would orphan stored state.
- `requirements.lock` pins versions but not hashes. Check dependency vulnerabilities locally with `pip-audit -r requirements.lock` before releases.
- Treat the data volume (mirrors, state, token file, local backups) as sensitive; restrict who can read it.
