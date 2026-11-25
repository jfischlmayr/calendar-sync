"""Test helpers: ICS builders and an in-memory CalendarStore with failure injection."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from calsync.store import Item, PreconditionFailed, StoreError  # noqa: E402

ME = "me@example.com"


def vevent(uid, summary="Meeting", start="20260301T100000Z", end=None, tzid=None, allday=False, rrule=None,
           exdates=(), organizer=None, attendees=(), rid=None, status=None, extra=(), alarm=True) -> str:
    L = ["BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20260101T000000Z"]
    if allday:
        L.append(f"DTSTART;VALUE=DATE:{start}")
        if end:
            L.append(f"DTEND;VALUE=DATE:{end}")
    elif tzid:
        L.append(f"DTSTART;TZID={tzid}:{start}")
        if end:
            L.append(f"DTEND;TZID={tzid}:{end}")
    else:
        L.append(f"DTSTART:{start}")
        if end:
            L.append(f"DTEND:{end}")
    if rid:
        L.append(f"RECURRENCE-ID{';TZID=' + tzid if tzid else ''}:{rid}")
    if rrule:
        L.append(f"RRULE:{rrule}")
    for x in exdates:
        L.append(f"EXDATE{';TZID=' + tzid if tzid else ''}:{x}")
    L.append(f"SUMMARY:{summary}")
    if status:
        L.append(f"STATUS:{status}")
    if organizer:
        L.append(f"ORGANIZER;CN=Org:mailto:{organizer}")
    for a, ps in attendees:
        L.append(f"ATTENDEE;PARTSTAT={ps}:mailto:{a}")
    L += list(extra)
    if alarm:
        L += ["BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT15M", "DESCRIPTION:x", "END:VALARM"]
    L.append("END:VEVENT")
    return "\r\n".join(L)


def ics(*events, prodid="-//Apple Inc.//Test//EN", calprops=(), tz=False) -> bytes:
    head = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{prodid}", *calprops]
    if tz:
        head += ["BEGIN:VTIMEZONE", "TZID:Europe/Vienna", "BEGIN:STANDARD", "DTSTART:19701025T030000",
                 "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100", "END:STANDARD", "BEGIN:DAYLIGHT",
                 "DTSTART:19700329T020000", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200", "END:DAYLIGHT", "END:VTIMEZONE"]
    return ("\r\n".join(head) + "\r\n" + "\r\n".join(events) + "\r\nEND:VCALENDAR\r\n").encode()


def simple(uid, summary="Meeting", start="20260301T100000Z", **kw) -> bytes:
    return ics(vevent(uid, summary, start, end=kw.pop("end", None) or _plus1h(start, kw.get("allday")), **kw))


def _plus1h(start, allday):
    if allday:
        return None
    hh = int(start[9:11]) + 1
    return start[:9] + f"{hh:02d}" + start[11:]


class MemoryStore:
    """In-memory CalDAV-like collection. ``mutate`` rewrites data on store (simulates provider
    normalisation); ``fail`` maps method name -> exception raised once (or every time if
    ``sticky``)."""

    def __init__(self, name="mem", seed: dict[str, bytes] | None = None, mutate=None, read_only=False):
        self.name = name
        self.items: dict[str, tuple[str, bytes]] = {}
        self.n = 0
        self.mutate = mutate
        self.read_only = read_only
        self.fail: dict[str, Exception] = {}
        self.sticky = False
        self.fail_after_writes: int | None = None
        self.lose_response: dict[str, Exception] = {}      # write is APPLIED, then the call raises (lost response)
        self.log: list[tuple[str, str]] = []
        for href, data in (seed or {}).items():
            self.put_raw(href, data)

    # --- test-side helpers (simulate a user editing in the provider UI; not logged as engine writes)
    def put_raw(self, href: str, data: bytes) -> None:
        self.n += 1
        self.items[href] = (f'"{self.n}"', data)

    def remove_raw(self, href: str) -> None:
        self.items.pop(href, None)

    def hrefs(self):
        return sorted(self.items)

    def datas(self):
        return [d for _, d in self.items.values()]

    def writes(self):
        return [x for x in self.log if x[0] in ("create", "update", "delete")]

    # --- CalendarStore
    def _maybe_fail(self, m: str):
        e = self.fail.get(m)
        if e is not None:
            if not self.sticky:
                self.fail.pop(m)
            raise e

    def list_items(self):
        self.log.append(("list", ""))
        self._maybe_fail("list_items")
        return [Item(h, e, d) for h, (e, d) in sorted(self.items.items())]

    def _write_guard(self):
        if self.read_only:
            raise RuntimeError("read-only store written")
        if self.fail_after_writes is not None and len(self.writes()) >= self.fail_after_writes:
            raise StoreError("injected outage after N writes")

    def _lost(self, m):
        e = self.lose_response.pop(m, None)
        if e is not None:
            raise e

    def create(self, data):
        self._write_guard()
        self._maybe_fail("create")
        from calsync import icsmodel as im
        uid = im.parse_resource(data).uid
        href = "/cal/" + hashlib.sha1(uid.encode()).hexdigest()[:16] + ".ics"
        if href in self.items:
            raise PreconditionFailed("exists")
        if any(im.parse_resource(d).uid == uid for _, d in self.items.values()):
            raise StoreError("no-uid-conflict", transient=False, status=409)      # like Radicale/Google
        if self.mutate:
            data = self.mutate(data)
        self.put_raw(href, data)
        self.log.append(("create", href))
        self._lost("create")
        return Item(href, self.items[href][0], self.items[href][1])

    def update(self, href, etag, data):
        self._write_guard()
        self._maybe_fail("update")
        if href not in self.items:
            raise StoreError("not found", transient=False, status=404)
        if self.items[href][0] != etag:
            raise PreconditionFailed("etag")
        from calsync import icsmodel as im
        if im.parse_resource(data).uid != im.parse_resource(self.items[href][1]).uid:
            raise StoreError("UID may not change", transient=False, status=409)
        if self.mutate:
            data = self.mutate(data)
        self.put_raw(href, data)
        self.log.append(("update", href))
        self._lost("update")
        return Item(href, self.items[href][0], self.items[href][1])

    def delete(self, href, etag):
        self._write_guard()
        self._maybe_fail("delete")
        if href not in self.items:
            raise StoreError("not found", transient=False, status=404)
        if self.items[href][0] != etag:
            raise PreconditionFailed("etag")
        del self.items[href]
        self.log.append(("delete", href))
