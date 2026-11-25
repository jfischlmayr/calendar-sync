# Enabling writes — gated sequence

Writes can change or lose calendar data if misconfigured. Do not skip steps. The engine re-checks
the technical gates on every cycle ([ENGINE.md](ENGINE.md)); the checklist below is for you.

## Preflight (all must pass)

- [ ] You know the calendar ids, ownership, time zone and read-only/shared status on both providers.
      Calendars you do not own must not be paired.
- [ ] `global.self_addresses` lists **all** your addresses (Google, iCloud, aliases). Missing ⇒ events are
      treated as guest events (safe, but nothing syncs).
- [ ] Raw ICS export of both calendars (`calsync export icloud|google <collection> <dir>`), stored encrypted
      and off-host; a restore was tested ([BACKUP-RESTORE.md](BACKUP-RESTORE.md)).
- [ ] Daily encrypted off-host backup runs and `state/backup_ok.json` is fresh with `offhost: true`.
- [ ] Google OAuth app is **In production**, or the token is younger than 7 days ([GOOGLE-OAUTH.md](GOOGLE-OAUTH.md)).
- [ ] Sandbox test with throw-away calendars: add/update/delete, DST change, yearly all-day, recurrence
      exception, meeting with an outside organiser, edit conflict, several deletions.
- [ ] `calsync audit --pair P --detail` reviewed: every `suspect_duplicate`, `uid_conflict`,
      `ambiguous_duplicate` handled with `calsync decide` or fixed at the source; every `exempt_unsupported` understood.
- [ ] Birthdays: decide which provider is the source of truth; the engine never syncs birthdays.
- [ ] Guest invitations appear as `exempt_guest` and nowhere in the plan.

## Sequence: one pair, one direction at a time

1. The pair stays `mirror`. `calsync plan --pair P --direction b --canary 5` stores a plan with only the
   safest creates (direction `b` = into Google, `a` = into iCloud). Note `plan_sha256`; read the `0600`
   detail file in `data/reports`. If it refuses, do **not** weaken the rules: wait for ordinary events or see
   `--since-days-ago` in ENGINE.md.
2. `calsync approve-plan --pair P --direction b --by NAME --note TEXT --expect-sha SHA`.
3. Set the pair to `seed` (writes only Google; `seed_a` writes only iCloud), `CALSYNC_ALLOW_WRITES=1`,
   `max_creates_per_run = 5`, redeploy, and watch the first cycle: `calsync status`, spot-check the events on
   the provider (time, zone, recurrence, alarms). Verify-after-write must not raise a `BLOCK`.
4. Raise the cap (≤ 25) and let the seed finish. Two further cycles must report `applied: {}`.
5. Repeat with `--direction a` for the other side, then switch the pair to `live`. A new event added between
   plan and run holds that direction back until you re-plan and approve (intended).
6. Keep `propagate_deletes = false` initially; review `pending_delete` findings by hand.
7. After initialisation, re-store unapproved plans (`calsync plan --direction both`).
8. Enable the next pair only after the first has been stable for several days. Never seed several pairs at once.

Providers rewrite some data (for example reminders, footers on conference links). Such items are held or raise
a `BLOCK_<pair>` marker; clear it only after you understand the cause ([ROLLBACK.md](ROLLBACK.md)).

## If anything looks wrong

`calsync pause`, set `CALSYNC_ALLOW_WRITES=0`, set the pair back to `mirror`, then follow [ROLLBACK.md](ROLLBACK.md).
