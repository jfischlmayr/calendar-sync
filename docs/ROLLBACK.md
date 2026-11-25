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

## Empty mirror side
The local read-only vdirsyncer mirror refuses a storage that becomes completely empty ("was completely emptied");
the pair then reports an error. Move the stale mirror directory and its status (`/data/status/mirror/m_<pair>_a*`)
aside instead of forcing deletion.

## Restore drill
`scripts/restore-drill.sh <archive.age> <identity>`; restore into production only with the service paused.
