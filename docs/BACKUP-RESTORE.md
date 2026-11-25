# Backups, off-host storage, restore

**Persistent container volumes are not backups.** The service needs two mounted named volumes: /data for status, tokens, mirror ICS, manifests, local backup staging; /config for calsync.toml. A restore of /data is operational only if the matching OAuth credentials remain valid.

## Encryption and off-host requirement

Generate an age key pair on a trusted workstation *outside* the service host. Keep the private identity in a password manager/off-host safe storage; give the service **only the public age1 recipient** via `CALSYNC_BACKUP_AGE_RECIPIENTS`. Do not store the private key on the service host or commit it to Git.

Configure a durable off-host location (e.g. an S3-compatible object store, a NAS remote or another independent storage service) via an rclone remote; use secret-marked environment settings for any keys. Avoid relying only on a local mount or same filesystem; the system deliberately treats off-host proof as a write prerequisite.

Example read-only backup commands inside a configured container:
```sh
python -m calsync.backup create
python -m calsync.backup upload
```
Do not enable writes just because an archive exists. Confirm successful remote upload and restore drill from **downloaded off-host copy**. See `python -m calsync.backup --help` for exact options.

## Independent restoration

On a separate trusted workstation holding the private age identity:
```sh
scripts/restore-drill.sh /path/to/calsync-YYYYMMDDTHHMMSSZ.tar.gz.age /secure/path/age-identity.txt
```
The drill decrypts to a temporary isolated directory, validates manifest hashes/event counts, checks file modes and removes its working directory afterwards. The script logs no private keys.

Restore into production **only** after pausing the service, validating source event counts and ensuring no other instance runs. Never overwrite live /data from an unverified snapshot.

## Pulling backups to a workstation

You may additionally pull the encrypted archives (`python -m calsync.backup create`) to a workstation with your own
tooling (for example `docker cp` or an `rclone` remote), as long as only the public age recipient is on the server. The
repository does not ship host-specific pull timers.

## Existing local QA

The included synthetic restore test reported `DRILL OK` for a small fixture with 4 UIDs and 4 events; its simulated remote was on the same filesystem (not disaster-proof). This tests the backup code but does **not** prove a real off-host backup of your own calendars exists.

## Monitoring

- Local encrypted retention, remote retention and independent restore checks are necessary.
- Alerts go to configured webhook/email; absence of alert transport must not silently certify remote backups.
- If off-host backup becomes stale, new live writes must fail closed.
