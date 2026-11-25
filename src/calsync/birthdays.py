"""Birthday ledger (read-only).

Birthdays are deliberately OUTSIDE the normal pair sync: Google's contact-linked birthday
events are special records (ORGANIZER at calendar.google.com, owned by Contacts), and copying
yearly all-day events between providers produces duplicates on every birthday. The engine
therefore never writes a ``birthday`` item. This module only *reports*:

  matched_across_calendars   same month-day + same name tokens on both sides (true duplicates
                             that already exist; nothing to do)
  only_a / only_b            present on one provider only
  missing_in_view            Google birthdays that have no counterpart in iCloud's normal
                             calendar NOR in the dedicated birthday-view calendar (if given)
  view_only                  items in the birthday view without a Google counterpart (orphans)

Output holds opaque ids and counts; names appear only in the detail structure that callers
write to 0600 files.
"""
from __future__ import annotations

from . import icsmodel as im


def key(res: im.Resource) -> tuple[str, frozenset[str]]:
    m = res.master
    md = m.start[5:9] if m and m.start.startswith("D") else ""          # 'D' + YYYYMMDD -> MMDD
    toks = frozenset(t for t in im.title_tokens(m.summary if m else "") if t not in im.BIRTHDAY_WORDS)
    return md, toks


def _same(k1, k2) -> bool:
    if k1[0] != k2[0] or not k1[0]:
        return False
    a, b = k1[1], k2[1]
    return bool(a and b) and (a == b or a <= b or b <= a)


def ledger(a: dict[str, im.Resource], b: dict[str, im.Resource], view: dict[str, im.Resource] | None = None) -> dict:
    ba = {h: r for h, r in a.items() if r.kind == "birthday"}
    bb = {h: r for h, r in b.items() if r.kind == "birthday"}
    bv = {h: r for h, r in (view or {}).items() if r.kind == "birthday"}
    ka = {h: key(r) for h, r in ba.items()}
    kb = {h: key(r) for h, r in bb.items()}
    kv = {h: key(r) for h, r in bv.items()}
    matched, only_a, only_b, missing, contact_linked = [], [], [], [], 0
    used_a: set[str] = set()
    for hb, rb in sorted(bb.items()):
        if "google_system_organizer" in rb.reasons:
            contact_linked += 1
        ma = next((h for h in sorted(ba) if h not in used_a and _same(ka[h], kb[hb])), None)
        in_view = next((h for h in sorted(bv) if _same(kv[h], kb[hb])), None)
        if ma:
            used_a.add(ma)
            matched.append({"a": ba[ma].ident, "b": rb.ident})
        else:
            only_b.append(rb.ident)
            if view is not None and not in_view:
                missing.append(rb.ident)
            elif view is None:
                missing.append(rb.ident)
    only_a = [r.ident for h, r in sorted(ba.items()) if h not in used_a]
    view_only = [r.ident for h, r in sorted(bv.items())
                 if not any(_same(kv[h], kb[x]) for x in bb) and not any(_same(kv[h], ka[x]) for x in ba)]
    return {
        "birthday_series": {"a": len(ba), "b": len(bb), "view": len(bv) if view is not None else None},
        "contact_linked_b": contact_linked,
        "matched_across_calendars": matched,
        "only_a": only_a,
        "only_b": only_b,
        "missing_in_view": missing,
        "view_only": view_only,
        "note": "birthdays are exempt from the pair sync; this ledger never causes writes",
    }
