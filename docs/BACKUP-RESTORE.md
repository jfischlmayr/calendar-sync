# Backups, off-host storage, restore

**Persistent container volumes are not backups.** The service needs two mounted named volumes: /data for status, tokens, mirror ICS, manifests, local backup staging; /config for calsync.toml. A restore of /data is operational only if the matching OAuth credentials remain valid.

## Encryption and off-host requirement

Generate an age key pair on a trusted workstation *outside* the service host. Keep the private identity in a password manager/off-host safe storage; give the service **only the public age1 recipient** via `CALSYNC_BACKUP_AGE_RECIPIENTS`. Do not store the private key on the service host or commit it to Git.

Configure a durable off-host location (e.g. an S3-compatible object store, a NAS remote or another independent storage service) via an rclone remote; use secret-marked environment settings for any keys. Avoid relying only on a local mount or same filesystem; the system deliberately treats off-host proof as a write prerequisite.

For the supplied Compose configuration, use the rclone remote name `offsite` (for example, set
`CALSYNC_BACKUP_REMOTE=offsite:calendar-backups/path`, `RCLONE_CONFIG_OFFSITE_TYPE=s3` and the corresponding
provider, endpoint and credentials in your untracked `.env` or secret store). The alternative
`CALSYNC_BACKUP_LOCAL_DIR` requires explicitly passing the environment variable and mounting an independent
backup destination into a custom deployment; it is not configured in the supplied Compose file.

Example backup commands inside a configured container (no calendar writes):
```sh
python -m calsync.backup create
python -m calsync.backup upload --require-offhost
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

## Test coverage

The synthetic restore tests exercise backup and restore without accessing real calendars. Passing them does
not prove that your own off-host backup exists or can be restored.

## Monitoring

- Local encrypted retention, remote retention and independent restore checks are necessary.
- Alerts go to configured webhook/email; absence of alert transport must not silently certify remote backups.
- If off-host backup becomes stale, new live writes must fail closed.
