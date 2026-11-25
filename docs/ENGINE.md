# Sync engine — design, guarantees, limits

calsync's engine is the **only writer**; vdirsyncer is not used for writing. vdirsyncer remains a read-only mirror to the local
filesystem (source of the encrypted backups); it can no longer write to iCloud or Google
(`vdsconf.live_conf` was removed).

## Why vdirsyncer alone is not safe for this

| Problem with real calendars | vdirsyncer | engine |
|---|---|---|
| Many one-sided events on first sync | copies all blindly | planned, staged, approved per direction |
| Different-UID duplicates of the same event | creates a second copy | strict twin ⇒ linked, loose twin ⇒ *suspect*, nothing copied |
| Guest invitations (organiser ≠ me) | copies ATTENDEE/ORGANIZER ⇒ RSVP reset / notification mails | **exempt**, never read-through, never written |
| Google contact birthdays, yearly all-day series | copied ⇒ duplicate every year | **exempt**, separate read-only ledger |
| Manifest `skip` / review `decision` | ignored by the tool | the approved plan **is** the execution |
| Edit on both sides | `conflict_resolution = null` aborts the whole pair | per-item `conflict` finding, nothing overwritten, rest keeps syncing |
| Provider rewrites data (alarms, X-WR props) | etag churn | per-side baseline fingerprints: no ping-pong |

## Pipeline

```
list A, list B  (CalDAV, all-or-nothing)            ── StoreError ⇒ nothing happens, backoff
   │
analyze: parse → classify → core_fp / full_fp / strict_key
   │            kinds: normal | guest | birthday | unsupported | ignored
build_plan (pure): links ► UID twins ► decisions ► strict twins ► suspects ► creates
   │
audit (mirror)       report only, never writes (stores are read_only=True and refuse writes)
apply (seed/live)    gates ► outage/mass-delete guards ► approved ops only (until initialised)
                     ► caps ► conditional write ► read back ► verify ► persist link ► next op
```

### Identity and fingerprints (`icsmodel.py`)

* A **resource** (one CalDAV object) holds one series: a master (optional RRULE) plus all
  `RECURRENCE-ID` overrides. The engine always moves **whole resources**, so EXDATEs, moved and
  cancelled instances travel together. *Events vs occurrences:* the audit reports resources,
  components, recurring series, overrides and an occurrence estimate for the next 12 months;
  occurrences are never synced individually.
* `core_fp` — cross-provider comparable: UTC-normalised instants (`TZID=Europe/Berlin`,
  `GMT+0200` + VTIMEZONE, `Z`, all-day dates), DTEND/DURATION unified, RRULE canonical
  (`INTERVAL=1` dropped), EXDATE set, RECURRENCE-ID, normalised text (NFKC, whitespace),
  STATUS/TRANSP defaults, **privacy** (`CLASS:PRIVATE/CONFIDENTIAL`), and for recurring series the
  **time-zone name** of DTSTART (a provider that flattens `TZID=Europe/Berlin` to UTC would drift by an
  hour at DST). Used for "same event" and for **verify-after-write**. Alarms are compared separately
  (`alarm_fp`): an alarm-only edit propagates, but providers may rewrite alarms without that failing verify.
* `full_fp` — change detector for **one side only**: all properties except volatile ones
  (DTSTAMP, SEQUENCE, LAST-MODIFIED, X-APPLE-CREATOR-*, X-WR-*, …) plus alarms. Compared only with
  that side's own previous value, so a provider that rewrites what we wrote never looks like an edit.
* `strict_key` — different-UID duplicate identity (same instants, title, recurrence shape).

### Exemptions (never created, updated, deleted — in any mode)

| kind | rule | why |
|---|---|---|
| `guest` | any ORGANIZER/ATTENDEE that is not in `self_addresses` (VALARM e-mail attendees are ignored) | RSVP state and iTIP notifications must not be touched. Unknown `self_addresses` ⇒ every attendee counts as foreign (fail-closed). |
| `birthday` | master is a yearly **all-day** series, or carries a birthday marker; includes Google's `@calendar.google.com`-organised contact birthdays | contact-linked special records, would duplicate every year; handled by the ledger |
| `unsupported` | no/duplicate UID, several UIDs in one resource, orphan override, `RANGE=THISANDFUTURE`, RDATE, EXRULE, VTODO/VJOURNAL, nested components (RFC 9073 PARTICIPANT …), **floating** times, unresolvable TZID, bare CR / parse errors, UID occurring twice in one calendar | cannot be copied faithfully ⇒ flagged, not guessed |
| `ignored` | operator decision `ignore` | manual exemption |

Exempt items are also *look-alike barriers*: a normal event that resembles an exempt one (same start,
similar title) is **not** copied next to it (`suspect_duplicate`).

### Links and the three-way state

Per linked pair the state (`/data/state/engine_<pair>.json`, atomic, rewritten after **every**
successful write) stores both hrefs/UIDs, etags and `full_fp` baselines. Per cycle:

* nothing changed ⇒ nothing; only etag churn ⇒ silent rebase
* changed on one side ⇒ `update` of the other side (`If-Match`), also when only alarms/non-core properties
  changed; the target's own UID is kept for adopted different-UID twins
* changed on both sides ⇒ identical result ⇒ rebase, else **`conflict` finding**, neither overwritten
  (resolve with `calsync decide --action prefer`)
* a link updated ≥ 4 times within an hour is frozen as `flapping_link` (provider rewriting data)
* deleted on one side ⇒ `pending_delete` finding (executed only with `propagate_deletes = true`, and
  then only up to `max_delete_abs`/`max_delete_pct`); deleted-vs-edited ⇒ conflict, never a delete
* an item that turns into a guest/birthday/unsupported item ⇒ link **frozen**

### First-time reconciliation (empty state)

1. UID twin + equal `core_fp` ⇒ **adopt** (link, zero writes). UID twin but different content (also
   privacy or recurring-TZID differences) ⇒ `uid_conflict` (untouched until `prefer`).
2. Different UID, same `strict_key` (instants + title + recurrence shape), exactly one on each side ⇒
   **adopt** (zero writes). If description/location/etc. differ the link is created *without baseline*
   (`duplicate_content_differs`), so a later unrelated edit can never overwrite the other side; several
   candidates ⇒ `ambiguous_duplicate`. The survivor of a half-deleted link also counts as a look-alike,
   so a deleted-and-recreated event (new UID) is a `suspect_duplicate`, not a second copy.
3. Same start + similar title (token Jaccard ≥ 0.5 or subset), anywhere on the other side, any kind ⇒
   `suspect_duplicate`: **not copied** until an operator decision (`link` or `copy`).
4. Everything else that is `normal` and one-sided ⇒ `create` on the other side.
5. `calsync plan --direction a|b|both` stores the ops that write to that side (a = iCloud, b = Google);
   `calsync approve-plan --direction D --expect-sha <sha>` signs them. Creates are approved as "copy
   item X to side D" (editing X afterwards is fine), updates/deletes are bound to the exact content.
   Approval is **per direction** (`plans/<pair>.<dir>.json`): a seed approval for Google does not cover
   iCloud. A direction becomes *initialised* only when **every** planned op for it is done; a new, unapproved
   op (e.g. an event added after approval, or approval of an empty plan) holds it back until you re-plan and
   approve. After initialisation ops flow automatically within the caps; more than `max_burst_ops`
   create/update ops in one cycle require a fresh approval.

### Safety layers (all must pass; none is the only protection)

1. `CALSYNC_ALLOW_WRITES=1`, no `state/PAUSE`, no `BLOCK_<pair>` marker, fresh **off-host** backup
   (future-dated timestamps are rejected), non-empty `self_addresses`.
2. Plan = execution. Only ops contained in an approved plan run before initialisation.
3. Conditional writes: create `If-None-Match: *` (deterministic name from the UID), update/delete
   `If-Match`. A lost race is `precondition_failed_replan`, never an overwrite.
4. Outage guard (side returned zero resources), vanished-count guard, delete cap.
5. Caps per run (`max_creates_per_run`, `max_updates_per_run`) ⇒ canary-sized batches; `ack-deletes`
   is consumed by the next cycle (one-shot). A provider that permanently rejects one item (4xx) only
   isolates that op (two attempts, then left for the operator); auth, rate-limit and transient errors
   abort the run.
6. **Verify-after-write**: the stored resource is parsed and its `core_fp` must equal the source's.
   Mismatch ⇒ `BLOCK_<pair>` marker written, pair stops writing until an operator reviews.
7. Crash windows: state is saved after each write; a PUT whose state was lost is re-adopted by UID
   next cycle (no duplicate).
8. `seed` mode physically cannot write side A (`may_write`), tested with a store that raises on write.

### Held items (known provider rewrites) and decision order

* **`held_provider_rewrite`** (create *and* update, steady state too): a source that contains a video-conference link in its
  summary/location/description, or a conference `X-` property, is **not copied/updated automatically** - providers append their own
  footer or conference object, so the copy could never equal the source and verify-after-write would `BLOCK` the whole pair. The item
  stays untouched on both sides and shows up as a finding; it does not hold back initialisation. Handle it by hand (copy it in the UI).
* Operator decisions: the **newest** matching decision wins (an older stale `prefer` recorded for a previous version of the item can no
  longer shadow a fresh one). `ignore` is permanent for that item id (also after edits) and is the supported way to exempt a genuine
  special item without deleting anything; remove the entry from `/data/decisions/<pair>.json` to undo.
* After a pair is initialised, **re-store unapproved plans** (`calsync plan --direction both`): an approval that lingers would re-authorise
  the very same create ops if the state file were ever lost.

### Birthdays and contacts

Separate from the pair sync by construction. `calsync audit` includes a **ledger**: birthday series per
side, series matched across calendars (same month-day + name tokens), one-sided series, and — if
`birthday_view_collection` (the collection id of an iCloud calendar that shows Google birthdays) is
configured — Google birthdays *missing* from it. The ledger never writes. Automating the
birthday view is **not implemented** (flagged): the recommended source of truth is one Contacts provider.

### Unsupported / flagged (not synced, reported)

RDATE, EXRULE, `RANGE=THISANDFUTURE`, orphan overrides (invitation to a single instance), VTODO,
resources with several UIDs, unresolvable time zones, UID reused inside one calendar, attachments are
passed through unchanged but not verified, conference data is provider-specific. Guest events and
birthdays are exempt rather than unsupported.

## Canary (filtered) plans

`plan` normally stores *every* op for a direction, and the cap (`max_creates_per_run`) then runs whichever
sort first by opaque id. For the very first writes use a filtered plan instead:

* `--canary N` (`canary.py`) picks the N **soonest** creates whose source is a single, timed (resolved UTC
  instant), future (2–270 days), ≤ 12 h, `CONFIRMED`/unset-status event with **no** organiser/attendees,
  recurrence, overrides, privacy class, birthday/anniversary-style title (`Geburtstag`, `birthday`, …), attachment/URL/conference property, non-display alarm or special
  marker (every property must be on a whitelist). Fewer than N candidates ⇒ refuses, never relaxes the rules.
* `--since-days-ago N` (opt-in, only with `--canary`/`--only`, N ≤ 90) additionally admits events that started between N and 2 days ago
  (most recent first). It widens **only** the date window; every other rule above still applies. Use it when the pair holds history
  but no ordinary future events (first import of an old calendar); review the `0600` canary detail file before approving.
* `--only ID` (repeatable) takes an explicit list; each id must be a planned *create* for that direction **and pass the same
  eligibility rules** (no override flag). Both forms are limited to 10 items and exactly one direction.
* The plan stores a content fingerprint per selected item: if a selected source is edited after `plan`, its approval is
  withdrawn (the create is skipped with a warning) until you re-plan and approve again.
* Only the selected ops are in the signed set, so only they can run; the remaining ops are `unapproved_before_initialization`
  and the direction stays un-initialised. The titles of a canary go to a `0600` file `<pair>-canary-*.json`
  in `/data/reports`, not to stdout.

## Batch profile (bulk migration after a successful canary)

`plan --direction a|b --canary N|--only ID… --profile batch` (N ≤ 25, one direction) uses a wider, still whitelist-based
eligibility (`canary.ineligible_reasons(profile="batch")`). Compared with the canary profile it admits:
any date (history and far future; no `--since-days-ago`), single/multi-day **all-day** events (non-yearly, explicit
DTEND, resolved dates), spans up to 100 days, `STATUS:TENTATIVE`, a **finite plain recurrence** (single master, `COUNT`/`UNTIL`,
FREQ daily/weekly/monthly, named time zone, no EXDATE/RDATE/overrides), `URL` (empty or http(s)), Apple structured-location /
travel metadata, and `EMAIL` alarms whose attendees are all in `self_addresses`. Still rejected (both profiles): organiser/attendees,
overrides, infinite or exception-carrying series, privacy class, birthday/anniversary titles, attachments, any other X- property,
conference data **or conference links in summary/location/description** (providers append their own footer, the copy would never
equal the source), `CANCELLED`, floating/unresolved times, and every `guest`/`birthday`/`unsupported` kind. Batches are ordered
most-recent-first; fewer candidates than requested ⇒ refusal. `--only` and `--canary` both write the titles to the `0600` detail file.

Pair mode **`seed_a`** writes only side A (iCloud); `seed` writes only side B (Google). Using the mode that matches the batch's direction
means the other direction's first-time ops need no approval (`live` would demand an approved plan for both). Gates are identical.

## Operator CLI

```
calsync audit [--pair P] [--detail]          read-only; counts + opaque ids; --detail writes a 0600 file with titles
calsync plan --pair P --direction a|b|both   store plan for approval, prints plan_sha256 per direction
calsync plan --pair P --direction a|b --canary N [--since-days-ago D]  plan with ONLY the N safest creates (see below)
calsync plan --pair P --direction a|b --only ID ...   plan with ONLY these creates (ids from `audit`)
calsync plan ... --profile batch             wider whitelist, up to 25 items (see "Batch profile")
calsync approve-plan --pair P --direction a|b --by NAME --note TEXT --expect-sha SHA
calsync decide --pair P --action ignore|copy|prefer|link --id ID [--side a|b] [--b-id ID]
calsync ack-deletes --pair P --max N         one-hour allowance for a legitimate bulk delete
calsync auth-status                          Google token status (no secrets)
calsync pause | resume                       global kill switch
```
Reports live in `/data/reports` (0600), plans in `/data/plans`, decisions in `/data/decisions`; none
belong into Git.
