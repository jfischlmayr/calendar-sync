# Test fixtures

`synthetic/` is a **fully synthetic** iCalendar corpus (80 resources) that mimics the
structure of real iCloud and Google CalDAV resources: Apple VTIMEZONE/X-APPLE-*/VALARM
style vs. Google UTC times, X-WR-* props and EMAIL alarms. All names, addresses and
domains are invented (`example.com`, `gmail.example`, `hotel.example`, ...).

- `synthetic/icloud/*.ics`, `synthetic/google/*.ics`: one CalDAV resource per file, CRLF,
  75-octet folded.
- `synthetic/EXPECTED.json`: per file `side`, `uid`/`uids`, expected classifier `kind`
  (`normal | guest | birthday | unsupported:<reason>`), `case`, and `group`; `groups` lists
  files that are the same real-world event across providers (`same_uid`,
  `strict_duplicate`, `loose_duplicate`, `birthday_match`). `self_addresses` holds the
  owner addresses (`me@example.com` iCloud, `me@gmail.example` Google).

## Generator

`gen_synthetic.py` (stdlib only, deterministic: UIDs via uuid5/sha1 of fixed keys,
timestamps hashed from keys, no clock or RNG) produced the committed files.

    python3 tests/fixtures/gen_synthetic.py           # rewrite synthetic/
    python3 tests/fixtures/gen_synthetic.py --check   # validate + verify reproducibility

`--check` verifies balanced BEGIN/END, CRLF only, physical lines <= 75 octets, UID and
EXPECTED.json consistency, only synthetic mail/URL domains, and that the files on disk are
byte-identical to what the generator emits. Tests read the committed files; edit the
generator and regenerate rather than hand-editing `.ics` files.

## Real data

**Never commit real calendar exports.** Real captures belong in `.private-fixtures/`
(gitignored). Do not copy titles, names, addresses, locations or descriptions from there
into synthetic fixtures.
