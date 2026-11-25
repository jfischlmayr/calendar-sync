"""Minimal, dependency-free iCalendar reader used by guards, reconcile and backup checks.

Only extracts the properties needed for identity/duplicate decisions. It never
modifies or re-serialises calendar data (raw bytes are always preserved).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split(line: str) -> tuple[str, dict[str, str], str]:
    """Split 'NAME;P=V:value' honouring quoted parameter values."""
    in_q, idx = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            idx = i
            break
    if idx < 0:
        return line.upper(), {}, ""
    head, value = line[:idx], line[idx + 1 :]
    parts, cur, in_q = [], "", False
    for ch in head:
        if ch == '"':
            in_q = not in_q
        if ch == ";" and not in_q:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


@dataclass
class Event:
    uid: str = ""
    summary: str = ""
    dtstart: str = ""          # raw value, incl. TZID/VALUE params folded in as prefix
    dtend: str = ""
    rrule: str = ""
    exdates: list[str] = field(default_factory=list)
    recurrence_id: str = ""
    status: str = ""
    organizer: str = ""
    attendees: list[tuple[str, str]] = field(default_factory=list)  # (address, PARTSTAT)
    source: str = ""           # file the event came from
    sha256: str = ""           # hash of the whole source resource (raw bytes)

    @property
    def is_recurring(self) -> bool:
        return bool(self.rrule)

    @property
    def has_guests(self) -> bool:
        return bool(self.attendees) or bool(self.organizer)

    @property
    def all_day(self) -> bool:
        return len(self.dtstart.split(":")[-1]) == 8

    def key_time_title(self) -> tuple[str, str]:
        return (self.dtstart, " ".join(self.summary.casefold().split()))


def parse(text: str, source: str = "", sha256: str = "") -> list[Event]:
    events: list[Event] = []
    cur: Event | None = None
    depth = 0  # nested components (VALARM) inside VEVENT
    for line in unfold(text):
        if not line:
            continue
        name, params, value = _split(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            cur = Event(source=source, sha256=sha256)
            depth = 0
            continue
        if cur is None:
            continue
        if name == "BEGIN":
            depth += 1
            continue
        if name == "END":
            if value.upper() == "VEVENT" and depth == 0:
                events.append(cur)
                cur = None
            else:
                depth -= 1
            continue
        if depth:
            continue
        tz = params.get("TZID", "")
        tagged = (f"TZID={tz}:" if tz else ("DATE:" if params.get("VALUE") == "DATE" else "")) + value
        if name == "UID":
            cur.uid = value.strip()
        elif name == "SUMMARY":
            cur.summary = value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()
        elif name == "DTSTART":
            cur.dtstart = tagged
        elif name == "DTEND":
            cur.dtend = tagged
        elif name == "RRULE":
            cur.rrule = value
        elif name == "EXDATE":
            cur.exdates += [v for v in value.split(",") if v]
        elif name == "RECURRENCE-ID":
            cur.recurrence_id = tagged
        elif name == "STATUS":
            cur.status = value.upper()
        elif name == "ORGANIZER":
            cur.organizer = value.lower().removeprefix("mailto:")
        elif name == "ATTENDEE":
            cur.attendees.append((value.lower().removeprefix("mailto:"), params.get("PARTSTAT", "").upper()))
    return events


def read_dir(path: str | Path) -> list[Event]:
    """Read every *.ics under a vdirsyncer 'filesystem' collection directory."""
    out: list[Event] = []
    for p in sorted(Path(path).glob("*.ics")):
        raw = p.read_bytes()
        out += parse(raw.decode("utf-8", "replace"), source=p.name, sha256=hashlib.sha256(raw).hexdigest())
    return out


def uid_set(path: str | Path) -> set[str]:
    return {e.uid for e in read_dir(path) if e.uid}
