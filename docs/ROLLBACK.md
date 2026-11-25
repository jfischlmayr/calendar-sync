# Rollback and recovery

## Stop writing immediately
1. `calsync pause` (creates `state/PAUSE`; every write gate fails closed on the next cycle).
2. Set `CALSYNC_ALLOW_WRITES=0`, redeploy, and set the pair to `mode = "mirror"` in `calsync.toml`
   (keep a dated copy of the previous config).

## Undo what the engine wrote
The engine only ever **creates** (first-time), **updates** linked items and — only if
`propagate_deletes = true` — **deletes** linked items. Every write is recorded as a link in
`/data/state/engine_<pair>.json` with `via` (`created_a`, `created_b`, `uid`, `duplicate`, `decision`).

* Created items: list them from the state file (`via = created_*`, hrefs on the target side) and delete those
  hrefs by hand (or in the provider's UI); nothing else was added.
* Updated items: restore from the **pre-change encrypted raw export** you took before enabling writes. The engine's
  own backups also run before every write window (`backup_ok.json` freshness gate).
* Google keeps deleted events in its trash for a limited time; iCloud has no trash ⇒ keep `propagate_deletes = false`.

## Reset the engine state without touching calendars
Stop the service, move `/data/state/engine_<pair>.json` aside. The next audit re-adopts every UID twin with
zero writes; first-time ops need a new approved plan again.

## `BLOCK_<pair>` marker (verify-after-write failure)
Means a provider stored something different from what was sent. Inspect `calsync status` / the report, compare
the two items, fix the cause (usually a feature the provider rewrites), then remove `/data/state/BLOCK_<pair>`.

## Local mirror rollback and automatic recovery

The local vdirsyncer mirror is read-only with respect to the providers. It identifies local files by modification time and inode. A plain copy during rollback can change these identities, causing a stale status to appear as a local edit and trigger a rejected write to the read-only provider.

The runner preserves file identities during rollback, scopes status restoration to the affected pair, and automatically rebuilds an affected side in isolated staging when exact restoration is not possible. Staging is kept on the same volume, outside the backup set (`/data/backups/mirror-scratch`). A failed or suspiciously incomplete read leaves the previous mirror intact. Zero-resource and mass-deletion guards still apply, and the recovery never writes to the providers.

The marker `/data/state/mirror_rebuild_<pair>.json` tracks pending recovery. Manual removal of vdirsyncer status files should no longer be necessary. Review the health status if automatic recovery fails.

## Restore drill
`scripts/restore-drill.sh <archive.age> <identity>`; restore into production only with the service paused.
