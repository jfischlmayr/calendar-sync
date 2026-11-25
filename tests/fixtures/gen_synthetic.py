#!/usr/bin/env python3
"""Generate the fully synthetic iCalendar corpus in tests/fixtures/synthetic/.

Everything here is invented: names, addresses, example.com / *.example mail
domains, UIDs derived from fixed strings.  No random numbers, no clock, no
network, stdlib only -> running the script twice yields byte-identical files.

The generated files are what tests use (they are committed); this script only
documents how they were produced and lets you regenerate / verify them.

    python3 tests/fixtures/gen_synthetic.py          # (re)write the corpus
    python3 tests/fixtures/gen_synthetic.py --check  # validate on-disk corpus

Layout:
    synthetic/icloud/*.ics    iCloud-style CalDAV resources (one per file)
    synthetic/google/*.ics    Google-style CalDAV resources (one per file)
    synthetic/EXPECTED.json   side, uid(s), expected classifier `kind`, case and
                              dedupe `group` per file, plus group definitions

Format rules applied to every file: CRLF line endings, RFC 5545 folding at
75 octets (never inside a UTF-8 sequence), TEXT escaping of \\ ; , and newline.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "synthetic"

ME_IC = "me@example.com"
ME_G = "me@gmail.example"
NS = uuid.UUID("5c1f6d2e-0000-4000-8000-00000000ca1c")
KIND_RE = re.compile(r"^(normal|guest|birthday|unsupported:[a-z_]+)$")
ALLOWED_HOST_SUFFIXES = (".example", "example.com", "example.org", "example.net")
ALLOWED_MAIL_DOMAINS = {"google.com"}  # only as `<id>@google.com` UID suffix

# --------------------------------------------------------------------------
# low-level helpers
# --------------------------------------------------------------------------

_WD = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def D(y, m, d, h=0, mi=0, wd=None):
    """Naive datetime; optional weekday assertion guards the RRULE/BYDAY logic."""
    dt = datetime(y, m, d, h, mi)
    if wd is not None:
        assert dt.weekday() == _WD[wd], (dt, wd)
    return dt


def fl(dt):  # floating / local form
    return dt.strftime("%Y%m%dT%H%M%S")


def fz(dt):  # UTC form
    return fl(dt) + "Z"


def fd(d):
    return d.strftime("%Y%m%d")


def _last_sunday(y, m):
    d = date(y, m, 31)  # March and October both have 31 days
    return d - timedelta(days=(d.weekday() + 1) % 7)


def vienna_offset(local):
    """Hours east of UTC for a naive Europe/Vienna wall-clock time (EU rules)."""
    start = datetime.combine(_last_sunday(local.year, 3), datetime.min.time()).replace(hour=2)
    end = datetime.combine(_last_sunday(local.year, 10), datetime.min.time()).replace(hour=3)
    return 2 if start <= local < end else 1


def v2u(local):
    return local - timedelta(hours=vienna_offset(local))


def esc(s):
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def text(name, value, params=""):
    return f"{name}{params}:{esc(value)}"


def fold(line):
    """RFC 5545 3.1: physical lines <= 75 octets, continuation starts with a space."""
    if len(line.encode("utf-8")) <= 75:
        return [line]
    out, cur = [], b""
    for ch in line:
        cb = ch.encode("utf-8")
        if len(cur) + len(cb) > 75:
            out.append(cur.decode("utf-8"))
            cur = b" "
        cur += cb
    out.append(cur.decode("utf-8"))
    return out


def render(lines):
    phys = []
    for ln in lines:
        phys.extend(fold(ln))
    return ("\r\n".join(phys) + "\r\n").encode("utf-8")


def _h(key):
    return int(hashlib.sha256(key.encode()).hexdigest()[:12], 16)


def stamps(key):
    """Deterministic (created, last-modified, dtstamp) in UTC form."""
    h = _h(key)
    created = datetime(2026, 5, 1, 8, 0, 0) + timedelta(days=h % 90, minutes=(h >> 8) % 600)
    modified = created + timedelta(days=1 + (h >> 4) % 20, minutes=(h >> 12) % 500)
    return fz(created), fz(modified), fz(modified)


def ic_uid(key):
    return str(uuid.uuid5(NS, key)).upper()


def g_uid(key):
    raw = base64.b32encode(hashlib.sha1(key.encode()).digest()).decode().lower()
    return raw[:26] + "@google.com"


# when-builders: return the DTSTART/DTEND (and callers append RRULE etc.)
def w_local(start, end, tz="Europe/Vienna"):
    return [f"DTSTART;TZID={tz}:{fl(start)}", f"DTEND;TZID={tz}:{fl(end)}"]


def w_utc(start_utc, end_utc):
    return [f"DTSTART:{fz(start_utc)}", f"DTEND:{fz(end_utc)}"]


def w_vienna_as_utc(start, end):
    return w_utc(v2u(start), v2u(end))


def w_date(start, end_excl):
    return [f"DTSTART;VALUE=DATE:{fd(start)}", f"DTEND;VALUE=DATE:{fd(end_excl)}"]


def w_float(start, end):
    return [f"DTSTART:{fl(start)}", f"DTEND:{fl(end)}"]


# --------------------------------------------------------------------------
# iCloud flavour
# --------------------------------------------------------------------------

IC_PRODID = "-//Apple Inc.//iOS 17.5.1//EN"

IC_VTZ = {
    "Europe/Vienna": [
        "BEGIN:VTIMEZONE", "TZID:Europe/Vienna",
        "BEGIN:DAYLIGHT", "TZOFFSETFROM:+0100", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        "DTSTART:19810329T020000", "TZNAME:GMT+2", "TZOFFSETTO:+0200", "END:DAYLIGHT",
        "BEGIN:STANDARD", "TZOFFSETFROM:+0200", "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        "DTSTART:19961027T030000", "TZNAME:GMT+1", "TZOFFSETTO:+0100", "END:STANDARD",
        "END:VTIMEZONE",
    ],
    "GMT+0200": [
        "BEGIN:VTIMEZONE", "TZID:GMT+0200",
        "BEGIN:STANDARD", "DTSTART:18901231T000000", "TZNAME:GMT+2",
        "TZOFFSETFROM:+0200", "TZOFFSETTO:+0200", "END:STANDARD",
        "END:VTIMEZONE",
    ],
    "GMT+0100": [
        "BEGIN:VTIMEZONE", "TZID:GMT+0100",
        "BEGIN:STANDARD", "DTSTART:18901231T000000", "TZNAME:GMT+1",
        "TZOFFSETFROM:+0100", "TZOFFSETTO:+0100", "END:STANDARD",
        "END:VTIMEZONE",
    ],
}


def ic_dur(minutes):
    if minutes % 1440 == 0:
        return f"-P{minutes // 1440}D"
    if minutes % 60 == 0:
        return f"-PT{minutes // 60}H"
    return f"-PT{minutes}M"


def ic_alarm(key, minutes, n=0, default=False):
    au = ic_uid(f"{key}/alarm{n}")
    L = ["BEGIN:VALARM", f"X-WR-ALARMUID:{au}", f"UID:{au}", f"TRIGGER:{ic_dur(minutes)}"]
    if default:
        L.append("X-APPLE-DEFAULT-ALARM:TRUE")
    L += ["DESCRIPTION:Erinnerung", "ACTION:DISPLAY", "END:VALARM"]
    return L


def ic_org(name, mail):
    return f"ORGANIZER;CN={name};EMAIL={mail}:mailto:{mail}"


def ic_att(name, mail, partstat, role="REQ-PARTICIPANT"):
    return (f"ATTENDEE;CN={name};CUTYPE=INDIVIDUAL;EMAIL={mail};PARTSTAT={partstat};"
            f"ROLE={role};RSVP=TRUE:mailto:{mail}")


def ic_vevent(key, uid, summary, when, *, location=None, structured=None, desc=None,
              alarms=(), seq=0, transp="OPAQUE", extra=(), status=None,
              recurrence_id=None, travel=False, stamp_key=None):
    c, m, s = stamps(stamp_key or key)
    L = ["BEGIN:VEVENT", f"CREATED:{c}", f"DTSTAMP:{s}"]
    L += list(when)
    L.append(f"LAST-MODIFIED:{m}")
    if location:
        L.append(text("LOCATION", location))
    if structured:
        title, addr, geo = structured
        L.append(f'X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-ADDRESS="{addr}";'
                 f'X-APPLE-RADIUS=70;X-TITLE="{title}":geo:{geo}')
    if desc:
        L.append(text("DESCRIPTION", desc))
    L.append(f"SEQUENCE:{seq}")
    L.append(text("SUMMARY", summary))
    if status:
        L.append(f"STATUS:{status}")
    L.append(f"TRANSP:{transp}")
    if uid is not None:
        L.append(f"UID:{uid}")
    if recurrence_id:
        L.append(recurrence_id)
    if travel:
        L.append("X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC")
    L += list(extra)
    for a in alarms:
        L += a
    L.append("END:VEVENT")
    return L


def ic_cal(events, tzs=("Europe/Vienna",)):
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{IC_PRODID}", "CALSCALE:GREGORIAN"]
    for tz in tzs:
        L += IC_VTZ[tz]
    for e in events:
        L += e
    L.append("END:VCALENDAR")
    return L


# --------------------------------------------------------------------------
# Google flavour
# --------------------------------------------------------------------------

G_PRODID = "-//Google Inc//Google Calendar 70.9054//EN"

G_VTZ = [
    "BEGIN:VTIMEZONE", "TZID:Europe/Vienna", "X-LIC-LOCATION:Europe/Vienna",
    "BEGIN:DAYLIGHT", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200", "TZNAME:CEST",
    "DTSTART:19700329T020000", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "END:DAYLIGHT",
    "BEGIN:STANDARD", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100", "TZNAME:CET",
    "DTSTART:19701025T030000", "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "END:STANDARD",
    "END:VTIMEZONE",
]


def g_dur(minutes):
    return f"-P{minutes // 1440}DT{(minutes % 1440) // 60}H{minutes % 60}M0S"


def g_alarm_display(minutes):
    return ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:This is an event reminder",
            f"TRIGGER:{g_dur(minutes)}", "END:VALARM"]


def g_alarm_email(minutes, who=ME_G):
    # The ATTENDEE inside the VALARM is the alarm recipient, NOT an event guest.
    return ["BEGIN:VALARM", "ACTION:EMAIL", "ATTENDEE:mailto:" + who,
            "DESCRIPTION:This is an event reminder", "SUMMARY:Alarm notification",
            f"TRIGGER:{g_dur(minutes)}", "END:VALARM"]


def g_org(name, mail):
    return f"ORGANIZER;CN={name}:mailto:{mail}"


def g_att(name, mail, partstat, role="REQ-PARTICIPANT"):
    return (f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE={role};PARTSTAT={partstat};RSVP=TRUE;"
            f"CN={name};X-NUM-GUESTS=0:mailto:{mail}")


def g_vevent(key, uid, summary, when, *, location=None, desc=None, alarms=None, seq=0,
             transp="OPAQUE", cls="PRIVATE", status="CONFIRMED", extra=(),
             recurrence_id=None, stamp_key=None):
    c, m, s = stamps(stamp_key or key)
    L = ["BEGIN:VEVENT"] + list(when) + [f"DTSTAMP:{s}"]
    if uid is not None:
        L.append(f"UID:{uid}")
    if recurrence_id:
        L.append(recurrence_id)
    if cls:
        L.append(f"CLASS:{cls}")
    L.append(f"CREATED:{c}")
    if desc:
        L.append(text("DESCRIPTION", desc))
    L.append(f"LAST-MODIFIED:{m}")
    if location:
        L.append(text("LOCATION", location))
    L.append(f"SEQUENCE:{seq}")
    if status:
        L.append(f"STATUS:{status}")
    L.append(text("SUMMARY", summary))
    L.append(f"TRANSP:{transp}")
    L += list(extra)
    for a in (alarms if alarms is not None else [g_alarm_display(30)]):
        L += a
    L.append("END:VEVENT")
    return L


def g_cal(events, vtz=False, calname="Privat"):
    L = ["BEGIN:VCALENDAR", f"PRODID:{G_PRODID}", "VERSION:2.0", "CALSCALE:GREGORIAN",
         "METHOD:PUBLISH", f"X-WR-CALNAME:{calname}", "X-WR-TIMEZONE:Europe/Vienna"]
    if vtz:
        L += G_VTZ
    for e in events:
        L += e
    L.append("END:VCALENDAR")
    return L


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

FILES: dict[str, bytes] = {}
META: dict[str, dict] = {}
GROUPS: dict[str, dict] = {}


def grp(gid, relation, note):
    GROUPS[gid] = {"relation": relation, "note": note, "files": []}


def put(case, side, name, lines, kind, uid, *, uids=None, group=None, note=None):
    assert KIND_RE.match(kind), kind
    rel = f"{side}/{name}.ics"
    assert rel not in FILES, rel
    FILES[rel] = render(lines)
    if uids is None:
        uids = [uid] if uid else []
    META[rel] = {"side": side, "uid": uid, "uids": uids, "kind": kind, "case": case,
                 "group": group}
    if note:
        META[rel]["note"] = note
    if group:
        GROUPS[group]["files"].append(rel)


# --------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------

def case_icloud_only_timed():
    c = "icloud_only_timed"
    k = "ic-only-01"
    u = ic_uid(k)
    put(c, "icloud", "only-timed-01-kickoff", ic_cal([ic_vevent(
        k, u, "Projekt-Kickoff Brückenbau", w_local(D(2026, 11, 10, 9), D(2026, 11, 10, 10, 30)),
        location="Seminarraum 3\nBeispielgasse 12\n1010 Wien",
        structured=("Seminarraum 3", "Beispielgasse 12\\n1010 Wien", "48.200000,16.370000"),
        desc="Bitte Unterlagen mitbringen.", alarms=[ic_alarm(k, 15, 0, default=True)],
        travel=True)]), "normal", u)

    k = "ic-only-02"
    u = ic_uid(k)
    put(c, "icloud", "only-timed-02-escapes", ic_cal([ic_vevent(
        k, u, "Müller & Söhne; Angebot, Rückfrage", w_local(D(2026, 11, 11, 14), D(2026, 11, 11, 15)),
        location="Café Größenwahn, Musterstraße 5",
        desc="Zeile eins\nZeile zwei, mit Komma; und Semikolon\nPfad: C:\\Daten\\Angebot",
        alarms=[ic_alarm(k, 60, 0)], seq=1)]), "normal", u)

    k = "ic-only-03"
    u = ic_uid(k)
    put(c, "icloud", "only-timed-03-two-alarms", ic_cal([ic_vevent(
        k, u, "Elternabend Volksschule", w_local(D(2026, 11, 12, 17, 30), D(2026, 11, 12, 18, 30)),
        alarms=[ic_alarm(k, 1440, 0), ic_alarm(k, 30, 1, default=True)], seq=2)]), "normal", u)

    k = "ic-only-04"
    u = ic_uid(k)
    put(c, "icloud", "only-timed-04-transparent", ic_cal([ic_vevent(
        k, u, "Mittagspause (frei)", w_local(D(2026, 11, 13, 12), D(2026, 11, 13, 13)),
        transp="TRANSPARENT", seq=3)]), "normal", u)


def case_google_only_timed():
    c = "google_only_timed"
    k = "g-only-01"
    u = g_uid(k)
    put(c, "google", "only-timed-01-dentist", g_cal([g_vevent(
        k, u, "Zahnarzt Dr. Beispiel", w_vienna_as_utc(D(2026, 11, 10, 9, 30), D(2026, 11, 10, 10, 15)),
        alarms=[g_alarm_display(10), g_alarm_email(1440)])]), "normal", u)

    k = "g-only-02"
    u = g_uid(k)
    put(c, "google", "only-timed-02-umlauts", g_cal([g_vevent(
        k, u, "Grüße aus Köln; Rückflug, Gate 7", w_vienna_as_utc(D(2026, 11, 14, 8), D(2026, 11, 14, 9, 30)),
        location="Flughafen Köln/Bonn, Terminal 1",
        desc="Check-in öffnet um 06:30\nHandgepäck: 8 kg; Koffer: 23 kg, bitte wiegen",
        cls=None)]), "normal", u)

    k = "g-only-03"
    u = g_uid(k)
    put(c, "google", "only-timed-03-email-alarm-attendee", g_cal([g_vevent(
        k, u, "Steuerberater Termin", w_vienna_as_utc(D(2026, 11, 16, 15), D(2026, 11, 16, 16)),
        alarms=[g_alarm_email(60), g_alarm_email(10, ME_G)], seq=1)]), "normal", u,
        note="ATTENDEE only inside VALARM (EMAIL action): must NOT be classified as guest")

    k = "g-only-04"
    u = g_uid(k)
    put(c, "google", "only-timed-04-transparent", g_cal([g_vevent(
        k, u, "Fokuszeit", w_vienna_as_utc(D(2026, 11, 17, 8), D(2026, 11, 17, 10)),
        transp="TRANSPARENT", alarms=[])]), "normal", u)


def case_tz_gmt_offset():
    c = "tz_gmt_offset"
    grp("g-gmt-offset", "same_uid", "Same UID; iCloud TZID=GMT+0200 vs Google UTC for the same instant")
    k = "ic-gmt-01"
    u = ic_uid(k)
    s, e = D(2027, 6, 15, 14), D(2027, 6, 15, 15)
    put(c, "icloud", "gmt-offset-01-summer", ic_cal([ic_vevent(
        k, u, "Telefonat Lieferant", w_local(s, e, "GMT+0200"), alarms=[ic_alarm(k, 15)])],
        tzs=("GMT+0200",)), "normal", u, group="g-gmt-offset")
    put(c, "google", "gmt-offset-01-summer", g_cal([g_vevent(
        k + "/g", u, "Telefonat Lieferant", w_utc(s - timedelta(hours=2), e - timedelta(hours=2)),
        alarms=[g_alarm_display(10)])]), "normal", u, group="g-gmt-offset")

    k = "ic-gmt-02"
    u = ic_uid(k)
    put(c, "icloud", "gmt-offset-02-winter", ic_cal([ic_vevent(
        k, u, "Online-Seminar Datenschutz", w_local(D(2026, 12, 3, 16), D(2026, 12, 3, 17), "GMT+0100"))],
        tzs=("GMT+0100",)), "normal", u)


def case_same_uid_both():
    c = "same_uid_both"
    # A: Google-origin UID mirrored into iCloud with local TZID
    grp("g-same-uid-01", "same_uid", "Google-origin UID present on iCloud (TZID) and Google (UTC)")
    k = "same-a"
    u = g_uid(k)
    s, e = D(2026, 11, 17, 10), D(2026, 11, 17, 11)
    put(c, "icloud", "same-uid-01", ic_cal([ic_vevent(
        k + "/ic", u, "Quartalsgespräch Vertrieb", w_local(s, e), location="Besprechungsraum Süd",
        alarms=[ic_alarm(k, 15)])]), "normal", u, group="g-same-uid-01")
    put(c, "google", "same-uid-01", g_cal([g_vevent(
        k, u, "Quartalsgespräch Vertrieb", w_vienna_as_utc(s, e), location="Besprechungsraum Süd",
        alarms=[g_alarm_display(10), g_alarm_email(30)])]), "normal", u, group="g-same-uid-01")

    # B: iCloud-origin UUID mirrored into Google, summer time
    grp("g-same-uid-02", "same_uid", "iCloud-origin UID present on Google; instant in CEST")
    k = "same-b"
    u = ic_uid(k)
    s, e = D(2027, 4, 6, 15), D(2027, 4, 6, 16, 30)
    put(c, "icloud", "same-uid-02", ic_cal([ic_vevent(
        k, u, "Jahresplanung Marketing", w_local(s, e), desc="Agenda folgt per Mail.",
        alarms=[ic_alarm(k, 60)], seq=1)]), "normal", u, group="g-same-uid-02")
    put(c, "google", "same-uid-02", g_cal([g_vevent(
        k + "/g", u, "Jahresplanung Marketing", w_vienna_as_utc(s, e),
        desc="Agenda folgt per Mail.", seq=1, alarms=[g_alarm_display(30)])]), "normal", u,
        group="g-same-uid-02")


def case_strict_duplicate():
    c = "strict_duplicate"
    for n, (title, s, e) in enumerate([
        ("Chorprobe Stadtchor", D(2026, 11, 18, 18), D(2026, 11, 18, 19, 30)),
        ("Sprint Planning", D(2027, 1, 14, 8, 30), D(2027, 1, 14, 9)),
    ], 1):
        gid = f"g-strict-dup-0{n}"
        grp(gid, "strict_duplicate", "Different UIDs, same instant, same title, same duration")
        k = f"strict-{n}"
        iu, gu = ic_uid(k), g_uid(k)
        put(c, "icloud", f"strict-dup-0{n}", ic_cal([ic_vevent(
            k, iu, title, w_local(s, e), alarms=[ic_alarm(k, 15)])]), "normal", iu, group=gid)
        put(c, "google", f"strict-dup-0{n}", g_cal([g_vevent(
            k + "/g", gu, title, w_vienna_as_utc(s, e))]), "normal", gu, group=gid)


def case_loose_duplicate():
    c = "loose_duplicate"
    items = [
        ("Zahnarzt Kontrolle", "Kontrolle Zahnarzt", D(2026, 11, 19, 11), D(2026, 11, 19, 11, 45),
         D(2026, 11, 19, 11, 45), "word order swapped, same start and duration"),
        ("Mittagessen mit Anna Beispiel", "Anna Beispiel - Mittagessen", D(2026, 11, 20, 12, 30),
         D(2026, 11, 20, 13, 30), D(2026, 11, 20, 14), "title reworded, same start, different end"),
    ]
    for n, (t_ic, t_g, s, e_ic, e_g, why) in enumerate(items, 1):
        gid = f"g-loose-dup-0{n}"
        grp(gid, "loose_duplicate", f"Different UIDs, same start; {why}")
        k = f"loose-{n}"
        iu, gu = ic_uid(k), g_uid(k)
        put(c, "icloud", f"loose-dup-0{n}", ic_cal([ic_vevent(
            k, iu, t_ic, w_local(s, e_ic), alarms=[ic_alarm(k, 15)])]), "normal", iu, group=gid)
        put(c, "google", f"loose-dup-0{n}", g_cal([g_vevent(
            k + "/g", gu, t_g, w_vienna_as_utc(s, e_g))]), "normal", gu, group=gid)


def case_allday_single():
    c = "allday_single"
    k = "ic-ad-01"
    u = ic_uid(k)
    put(c, "icloud", "allday-01-bridge-day", ic_cal([ic_vevent(
        k, u, "Brückentag", w_date(date(2026, 12, 8), date(2026, 12, 9)), transp="TRANSPARENT")]),
        "normal", u)

    grp("g-allday-dup", "strict_duplicate", "Different UIDs, same all-day date and title")
    k = "ic-ad-02"
    iu = ic_uid(k)
    gu = g_uid(k)
    put(c, "icloud", "allday-02-move", ic_cal([ic_vevent(
        k, iu, "Umzug Wohnung", w_date(date(2027, 1, 9), date(2027, 1, 10)),
        desc="Umzugswagen ab 8 Uhr")]), "normal", iu, group="g-allday-dup")
    put(c, "google", "allday-02-move", g_cal([g_vevent(
        k + "/g", gu, "Umzug Wohnung", w_date(date(2027, 1, 9), date(2027, 1, 10)),
        desc="Umzugswagen ab 8 Uhr", transp="TRANSPARENT", alarms=[])]), "normal", gu,
        group="g-allday-dup")

    k = "g-ad-01"
    u = g_uid(k)
    put(c, "google", "allday-01-vacation", g_cal([g_vevent(
        k, u, "Urlaub Bruder", w_date(date(2026, 12, 28), date(2026, 12, 29)),
        transp="TRANSPARENT", alarms=[])]), "normal", u)


def case_allday_multi():
    c = "allday_multi"
    grp("g-allday-multi-dup", "strict_duplicate", "Different UIDs, same multi-day all-day span and title")
    k = "multi-1"
    iu, gu = ic_uid(k), g_uid(k)
    span = (date(2027, 2, 13), date(2027, 2, 20))  # DTEND exclusive
    put(c, "icloud", "allday-multi-01-skiing", ic_cal([ic_vevent(
        k, iu, "Skiurlaub Zillertal", w_date(*span), location="Zillertal, Tirol",
        transp="TRANSPARENT")]), "normal", iu, group="g-allday-multi-dup")
    put(c, "google", "allday-multi-01-skiing", g_cal([g_vevent(
        k + "/g", gu, "Skiurlaub Zillertal", w_date(*span), location="Zillertal, Tirol",
        transp="TRANSPARENT", alarms=[])]), "normal", gu, group="g-allday-multi-dup")
    k = "multi-2"
    u = g_uid(k)
    put(c, "google", "allday-multi-02-business-trip", g_cal([g_vevent(
        k, u, "Dienstreise Linz", w_date(date(2026, 11, 24), date(2026, 11, 27)), alarms=[])]),
        "normal", u)


def case_rrule_weekly_count():
    c = "rrule_weekly_count"
    k = "wk-ic"
    u = ic_uid(k)
    put(c, "icloud", "rrule-weekly-count-01", ic_cal([ic_vevent(
        k, u, "Yoga Montag", w_local(D(2026, 11, 9, 18, wd="MO"), D(2026, 11, 9, 19)) +
        ["RRULE:FREQ=WEEKLY;COUNT=8"], location="Studio Lotus", alarms=[ic_alarm(k, 30)])]),
        "normal", u)
    k = "wk-g"
    u = g_uid(k)
    put(c, "google", "rrule-biweekly-count-01", g_cal([g_vevent(
        k, u, "Jour fixe Team", w_local(D(2026, 11, 10, 10, wd="TU"), D(2026, 11, 10, 10, 30)) +
        ["RRULE:FREQ=WEEKLY;WKST=MO;COUNT=6;INTERVAL=2;BYDAY=TU"])], vtz=True), "normal", u)


def case_rrule_until_exdate():
    c = "rrule_until_exdate"
    k = "ue-ic"
    u = ic_uid(k)
    put(c, "icloud", "rrule-until-exdate-01", ic_cal([ic_vevent(
        k, u, "Frühschwimmen", w_local(D(2026, 12, 3, 7, 30, "TH"), D(2026, 12, 3, 8)) + [
            "RRULE:FREQ=WEEKLY;UNTIL=20270128T063000Z",
            "EXDATE;TZID=Europe/Vienna:20261217T073000",
            "EXDATE;TZID=Europe/Vienna:20261231T073000"],
        alarms=[ic_alarm(k, 15)])]), "normal", u)
    k = "ue-g"
    u = g_uid(k)
    put(c, "google", "rrule-until-exdate-01", g_cal([g_vevent(
        k, u, "Orchesterprobe", w_local(D(2026, 12, 2, 19, wd="WE"), D(2026, 12, 2, 20)) + [
            "RRULE:FREQ=WEEKLY;UNTIL=20270210T180000Z;BYDAY=WE",
            "EXDATE;TZID=Europe/Vienna:20261223T190000",
            "EXDATE;TZID=Europe/Vienna:20261230T190000"])], vtz=True), "normal", u)


def case_monthly_byday():
    c = "monthly_byday_last_friday"
    k = "mo-ic"
    u = ic_uid(k)
    put(c, "icloud", "monthly-last-friday-01", ic_cal([ic_vevent(
        k, u, "Monatsabschluss", w_local(D(2026, 11, 27, 15, wd="FR"), D(2026, 11, 27, 16)) +
        ["RRULE:FREQ=MONTHLY;BYDAY=-1FR"], alarms=[ic_alarm(k, 60)])]), "normal", u)
    k = "mo-g"
    u = g_uid(k)
    put(c, "google", "monthly-last-friday-01", g_cal([g_vevent(
        k, u, "Stammtisch", w_local(D(2026, 11, 27, 19, wd="FR"), D(2026, 11, 27, 22)) +
        ["RRULE:FREQ=MONTHLY;COUNT=12;BYDAY=-1FR"], location="Gasthaus Zum Beispiel")], vtz=True),
        "normal", u)


def case_overrides_in_resource():
    c = "recurrence_overrides"
    # 01: independent per provider
    k = "ov-ic"
    u = ic_uid(k)
    tz = "Europe/Vienna"
    master = ic_vevent(
        k, u, "Statusrunde", w_local(D(2026, 11, 10, 9, wd="TU"), D(2026, 11, 10, 9, 45)) +
        ["RRULE:FREQ=WEEKLY;COUNT=6"], alarms=[ic_alarm(k, 15)])
    moved = ic_vevent(
        k + "/moved", u, "Statusrunde", w_local(D(2026, 11, 25, 10, wd="WE"), D(2026, 11, 25, 10, 45)),
        recurrence_id=f"RECURRENCE-ID;TZID={tz}:20261124T090000", seq=1,
        alarms=[ic_alarm(k + "/moved", 15)])
    cancelled = ic_vevent(
        k + "/cancel", u, "Statusrunde", w_local(D(2026, 12, 1, 9, wd="TU"), D(2026, 12, 1, 9, 45)),
        recurrence_id=f"RECURRENCE-ID;TZID={tz}:20261201T090000", seq=1, status="CANCELLED")
    put(c, "icloud", "overrides-01", ic_cal([master, moved, cancelled]), "normal", u,
        note="master + one moved + one STATUS:CANCELLED override in one resource")

    k = "ov-g"
    u = g_uid(k)
    master = g_vevent(
        k, u, "Kundenrunde", w_local(D(2026, 11, 11, 14, wd="WE"), D(2026, 11, 11, 15)) +
        ["RRULE:FREQ=WEEKLY;COUNT=6;BYDAY=WE"])
    moved = g_vevent(
        k + "/moved", u, "Kundenrunde", w_local(D(2026, 11, 26, 15, wd="TH"), D(2026, 11, 26, 16)),
        recurrence_id=f"RECURRENCE-ID;TZID={tz}:20261125T140000", seq=1)
    cancelled = g_vevent(
        k + "/cancel", u, "Kundenrunde", w_local(D(2026, 12, 9, 14, wd="WE"), D(2026, 12, 9, 15)),
        recurrence_id=f"RECURRENCE-ID;TZID={tz}:20261209T140000", seq=1, status="CANCELLED")
    put(c, "google", "overrides-01", g_cal([master, moved, cancelled], vtz=True), "normal", u)

    # 02: same UID on both providers
    grp("g-overrides-same-uid", "same_uid",
        "Series with moved+cancelled overrides, same UID; iCloud TZID vs Google UTC")
    k = "ov-both"
    u = g_uid(k)
    s = D(2026, 11, 16, 17, wd="MO")
    master = ic_vevent(
        k + "/ic", u, "Lerngruppe", w_local(s, s + timedelta(hours=1)) + ["RRULE:FREQ=WEEKLY;COUNT=5"],
        alarms=[ic_alarm(k, 30)])
    moved = ic_vevent(
        k + "/ic/moved", u, "Lerngruppe", w_local(D(2026, 12, 1, 17, 30, "TU"), D(2026, 12, 1, 18, 30)),
        recurrence_id=f"RECURRENCE-ID;TZID={tz}:20261130T170000", seq=1)
    cancelled = ic_vevent(
        k + "/ic/cancel", u, "Lerngruppe", w_local(D(2026, 12, 14, 17, wd="MO"), D(2026, 12, 14, 18)),
        recurrence_id=f"RECURRENCE-ID;TZID={tz}:20261214T170000", seq=1, status="CANCELLED")
    put(c, "icloud", "overrides-02-same-uid", ic_cal([master, moved, cancelled]), "normal", u,
        group="g-overrides-same-uid")
    su = v2u(s)
    master = g_vevent(
        k, u, "Lerngruppe", w_utc(su, su + timedelta(hours=1)) + ["RRULE:FREQ=WEEKLY;COUNT=5"],
        alarms=[g_alarm_display(30), g_alarm_email(60)])
    moved = g_vevent(
        k + "/moved", u, "Lerngruppe", w_vienna_as_utc(D(2026, 12, 1, 17, 30), D(2026, 12, 1, 18, 30)),
        recurrence_id="RECURRENCE-ID:20261130T160000Z", seq=1)
    cancelled = g_vevent(
        k + "/cancel", u, "Lerngruppe", w_vienna_as_utc(D(2026, 12, 14, 17), D(2026, 12, 14, 18)),
        recurrence_id="RECURRENCE-ID:20261214T160000Z", seq=1, status="CANCELLED")
    put(c, "google", "overrides-02-same-uid", g_cal([master, moved, cancelled]), "normal", u,
        group="g-overrides-same-uid")


def case_dst_series():
    c = "dst_series"
    k = "dst-1"
    u = ic_uid(k)
    put(c, "icloud", "dst-series-01-march", ic_cal([ic_vevent(
        k, u, "Samstagslauf", w_local(D(2027, 3, 20, 10, wd="SA"), D(2027, 3, 20, 11)) +
        ["RRULE:FREQ=WEEKLY;COUNT=4"], alarms=[ic_alarm(k, 30)])]), "normal", u,
        note="weekly series crossing the 2027-03-28 spring-forward")
    k = "dst-2"
    u = ic_uid(k)
    put(c, "icloud", "dst-series-02-october", ic_cal([ic_vevent(
        k, u, "Abendkurs Töpfern", w_local(D(2027, 10, 23, 18, wd="SA"), D(2027, 10, 23, 19)) +
        ["RRULE:FREQ=WEEKLY;COUNT=4"])]), "normal", u,
        note="weekly series crossing the 2027-10-31 fall-back")
    k = "dst-3"
    u = ic_uid(k)
    put(c, "icloud", "dst-series-03-change-day", ic_cal([ic_vevent(
        k, u, "Sonntagsfrühstück", w_local(D(2026, 10, 25, 9, wd="SU"), D(2026, 10, 25, 10)) +
        ["RRULE:FREQ=WEEKLY;COUNT=3"])]), "normal", u,
        note="DTSTART on the 2026-10-25 fall-back day itself")
    k = "dst-4"
    u = g_uid(k)
    put(c, "google", "dst-series-04-march", g_cal([g_vevent(
        k, u, "Spieleabend", w_local(D(2027, 3, 26, 20, wd="FR"), D(2027, 3, 26, 22)) +
        ["RRULE:FREQ=WEEKLY;COUNT=4;BYDAY=FR"])], vtz=True), "normal", u,
        note="weekly TZID series crossing the 2027-03-28 spring-forward")


def case_exdate_conflict():
    c = "exdate_conflict"
    grp("g-exdate-conflict", "same_uid",
        "Same series UID; providers disagree on EXDATE (common: 2026-11-25; "
        "iCloud also drops 12-23, Google also drops 12-09)")
    k = "exc"
    u = g_uid(k)
    tz = "Europe/Vienna"
    s = D(2026, 11, 11, 12, wd="WE")
    put(c, "icloud", "exdate-conflict-01", ic_cal([ic_vevent(
        k + "/ic", u, "Mittwochsrunde", w_local(s, s + timedelta(minutes=45)) + [
            "RRULE:FREQ=WEEKLY;COUNT=8",
            f"EXDATE;TZID={tz}:20261125T120000", f"EXDATE;TZID={tz}:20261223T120000"])]),
        "normal", u, group="g-exdate-conflict")
    su = v2u(s)
    put(c, "google", "exdate-conflict-01", g_cal([g_vevent(
        k, u, "Mittwochsrunde", w_utc(su, su + timedelta(minutes=45)) + [
            "RRULE:FREQ=WEEKLY;COUNT=8",
            "EXDATE:20261125T110000Z,20261209T110000Z"])]), "normal", u,
        group="g-exdate-conflict")


def case_thisandfuture():
    c = "recurrence_thisandfuture"
    k = "taf-ic"
    u = ic_uid(k)
    tz = "Europe/Vienna"
    master = ic_vevent(
        k, u, "Wochenreview", w_local(D(2026, 11, 30, 10, wd="MO"), D(2026, 11, 30, 11)) +
        ["RRULE:FREQ=WEEKLY;COUNT=10"])
    fut = ic_vevent(
        k + "/f", u, "Wochenreview (neu: Strategie)",
        w_local(D(2026, 12, 14, 11, wd="MO"), D(2026, 12, 14, 12)),
        recurrence_id=f"RECURRENCE-ID;RANGE=THISANDFUTURE;TZID={tz}:20261214T100000", seq=1)
    put(c, "icloud", "thisandfuture-01", ic_cal([master, fut]), "unsupported:thisandfuture", u)
    k = "taf-g"
    u = g_uid(k)
    master = g_vevent(
        k, u, "Retro", w_utc(D(2026, 12, 1, 9, wd="TU"), D(2026, 12, 1, 10)) +
        ["RRULE:FREQ=WEEKLY;COUNT=8"])
    fut = g_vevent(
        k + "/f", u, "Retro (neu: Planung)", w_utc(D(2026, 12, 15, 10, wd="TU"), D(2026, 12, 15, 11)),
        recurrence_id="RECURRENCE-ID;RANGE=THISANDFUTURE:20261215T090000Z", seq=1)
    put(c, "google", "thisandfuture-01", g_cal([master, fut]), "unsupported:thisandfuture", u)


def case_orphan_override():
    c = "orphan_override"
    k = "orph-ic"
    u = ic_uid(k)
    put(c, "icloud", "orphan-override-01", ic_cal([ic_vevent(
        k, u, "Teamdinner (verschoben)", w_local(D(2026, 12, 4, 18, 30), D(2026, 12, 4, 21)),
        recurrence_id="RECURRENCE-ID;TZID=Europe/Vienna:20261203T180000", seq=1)]),
        "unsupported:orphan_override", u)
    k = "orph-g"
    u = g_uid(k)
    put(c, "google", "orphan-override-01", g_cal([g_vevent(
        k, u, "Kaffeerunde (verschoben)", w_utc(D(2026, 12, 11, 8), D(2026, 12, 11, 8, 30)),
        recurrence_id="RECURRENCE-ID:20261210T080000Z", seq=1)]), "unsupported:orphan_override", u)


def case_two_uids():
    c = "two_uids"
    k = "two-ic"
    a, b = ic_uid(k + "/a"), ic_uid(k + "/b")
    put(c, "icloud", "two-uids-01", ic_cal([
        ic_vevent(k + "/a", a, "Vortrag Teil 1", w_local(D(2026, 12, 7, 9), D(2026, 12, 7, 10))),
        ic_vevent(k + "/b", b, "Vortrag Teil 2", w_local(D(2026, 12, 7, 11), D(2026, 12, 7, 12)))]),
        "unsupported:multiple_uids", a, uids=[a, b])
    k = "two-g"
    a, b = g_uid(k + "/a"), g_uid(k + "/b")
    put(c, "google", "two-uids-01", g_cal([
        g_vevent(k + "/a", a, "Workshop Block A", w_utc(D(2026, 12, 8, 8), D(2026, 12, 8, 9))),
        g_vevent(k + "/b", b, "Workshop Block B", w_utc(D(2026, 12, 8, 10), D(2026, 12, 8, 11)))]),
        "unsupported:multiple_uids", a, uids=[a, b])


def case_no_uid():
    c = "no_uid"
    put(c, "icloud", "no-uid-01", ic_cal([ic_vevent(
        "nouid-ic", None, "Termin ohne UID", w_local(D(2026, 12, 9, 13), D(2026, 12, 9, 14)))]),
        "unsupported:no_uid", None)
    put(c, "google", "no-uid-01", g_cal([g_vevent(
        "nouid-g", None, "Termin ohne UID", w_utc(D(2026, 12, 9, 12), D(2026, 12, 9, 13)))]),
        "unsupported:no_uid", None)


def case_vtodo():
    c = "vtodo"
    k = "todo-ic"
    u = ic_uid(k)
    cr, mo, st = stamps(k)
    au = ic_uid(k + "/alarm")
    put(c, "icloud", "vtodo-01", ic_cal([[
        "BEGIN:VTODO", f"CREATED:{cr}", f"DTSTAMP:{st}", "DUE;TZID=Europe/Vienna:20261215T170000",
        f"LAST-MODIFIED:{mo}", "PERCENT-COMPLETE:0", "SEQUENCE:0", "STATUS:NEEDS-ACTION",
        "SUMMARY:Geschenke besorgen", f"UID:{u}", "X-APPLE-SORT-ORDER:1",
        "BEGIN:VALARM", f"X-WR-ALARMUID:{au}", f"UID:{au}", "TRIGGER;VALUE=DATE-TIME:20261215T080000Z",
        "DESCRIPTION:Erinnerung", "ACTION:DISPLAY", "END:VALARM", "END:VTODO"]]),
        "unsupported:vtodo", u)
    k = "todo-g"
    u = g_uid(k)
    cr, mo, st = stamps(k)
    put(c, "google", "vtodo-01", g_cal([[
        "BEGIN:VTODO", "DUE:20261216T120000Z", f"DTSTAMP:{st}", f"UID:{u}", f"CREATED:{cr}",
        f"LAST-MODIFIED:{mo}", "SEQUENCE:0", "STATUS:NEEDS-ACTION", "SUMMARY:Rechnung prüfen",
        "END:VTODO"]]), "unsupported:vtodo", u)


def case_rdate():
    c = "rdate"
    k = "rd-ic"
    u = ic_uid(k)
    put(c, "icloud", "rdate-01", ic_cal([ic_vevent(
        k, u, "Adventsingen", w_local(D(2026, 12, 3, 18, wd="TH"), D(2026, 12, 3, 19)) +
        ["RDATE;TZID=Europe/Vienna:20261210T180000,20261217T180000"])]), "unsupported:rdate", u)
    k = "rd-g"
    u = g_uid(k)
    put(c, "google", "rdate-01", g_cal([g_vevent(
        k, u, "Sonderproben", w_utc(D(2026, 12, 3, 17, wd="TH"), D(2026, 12, 3, 18)) + [
            "RRULE:FREQ=WEEKLY;COUNT=2", "RDATE;VALUE=DATE-TIME:20261224T170000Z"])]),
        "unsupported:rdate", u, note="RRULE combined with RDATE")


def case_floating():
    c = "floating_time"
    k = "fl-ic"
    u = ic_uid(k)
    put(c, "icloud", "floating-01", ic_cal([ic_vevent(
        k, u, "Meditation", w_float(D(2026, 12, 1, 10), D(2026, 12, 1, 11)))], tzs=()),
        "unsupported:floating_time", u, note="no TZID and no Z: floating local time (providers anchor it to a zone: not copied)")
    k = "fl-g"
    u = g_uid(k)
    put(c, "google", "floating-01", g_cal([g_vevent(
        k, u, "Lesezeit", w_float(D(2026, 12, 2, 10), D(2026, 12, 2, 11)), alarms=[])]),
        "unsupported:floating_time", u, note="no TZID and no Z: floating local time (providers anchor it to a zone: not copied)")


def case_birthdays():
    c = "birthdays"

    def ic_b(key, name, ymd):
        u = ic_uid(key)
        d = date(*ymd)
        return u, ic_cal([ic_vevent(
            key, u, f"{name}'s Birthday", w_date(d, d + timedelta(days=1)) + ["RRULE:FREQ=YEARLY"],
            transp="TRANSPARENT", alarms=[ic_alarm(key, 540, 0)])], tzs=())

    def g_b(key, name, ymd, *, uid=None, selfonly=False):
        u = uid or g_uid(key)
        d = date(*ymd)
        extra = []
        if selfonly:
            extra = [g_org(ME_G, ME_G), g_att(ME_G, ME_G, "ACCEPTED")]
        return u, g_cal([g_vevent(
            key, u, f"{name}'s birthday", w_date(d, d + timedelta(days=1)) + ["RRULE:FREQ=YEARLY"],
            transp="TRANSPARENT", extra=extra, alarms=[])])

    grp("g-bday-anna", "birthday_match", "Same person+month-day, different UIDs, same year")
    iu, ib = ic_b("bd-anna", "Anna Beispiel", (1990, 3, 14))
    put(c, "icloud", "birthday-01-anna", ib, "birthday", iu, group="g-bday-anna")
    gu, gb = g_b("bd-anna/g", "Anna Beispiel", (1990, 3, 14))
    put(c, "google", "birthday-01-anna", gb, "birthday", gu, group="g-bday-anna")

    grp("g-bday-lukas", "birthday_match",
        "Same person+month-day, different UIDs, different DTSTART year (iCloud 1604 = year unknown)")
    iu, ib = ic_b("bd-lukas", "Lukas Mustermann", (1604, 7, 22))
    put(c, "icloud", "birthday-02-lukas", ib, "birthday", iu, group="g-bday-lukas")
    gu, gb = g_b("bd-lukas/g", "Lukas Mustermann", (1985, 7, 22))
    put(c, "google", "birthday-02-lukas", gb, "birthday", gu, group="g-bday-lukas")

    iu, ib = ic_b("bd-sabine", "Sabine Testfrau", (1988, 5, 30))
    put(c, "icloud", "birthday-03-sabine-icloud-only", ib, "birthday", iu)
    gu, gb = g_b("bd-tobias", "Tobias Exempel", (1979, 9, 9), selfonly=True)
    put(c, "google", "birthday-04-tobias-self-attendee", gb, "birthday", gu,
        note="ORGANIZER and ATTENDEE are both the calendar owner (Google contact birthdays)")
    gu = "birthday_" + hashlib.sha1(b"bd-greta").hexdigest()[:20] + "@google.com"
    gu, gb = g_b("bd-greta", "Greta Probelauf", (1995, 12, 24), uid=gu)
    put(c, "google", "birthday-05-greta-birthday-uid", gb, "birthday", gu,
        note="UID starts with birthday_")

    # yearly TIMED events are not birthdays
    c = "yearly_timed"
    k = "ann-ic"
    u = ic_uid(k)
    put(c, "icloud", "yearly-timed-01-anniversary", ic_cal([ic_vevent(
        k, u, "Hochzeitstag", w_local(D(2021, 6, 12, 9), D(2021, 6, 12, 9, 30)) +
        ["RRULE:FREQ=YEARLY"], alarms=[ic_alarm(k, 1440)])]), "normal", u,
        note="yearly RRULE but timed (09:00): not a birthday")
    k = "ann-g"
    u = g_uid(k)
    put(c, "google", "yearly-timed-01-anniversary", g_cal([g_vevent(
        k, u, "Firmenjubiläum", w_local(D(2019, 9, 2, 9), D(2019, 9, 2, 9, 30)) +
        ["RRULE:FREQ=YEARLY;BYMONTH=9"])], vtz=True), "normal", u,
        note="yearly RRULE but timed (09:00): not a birthday")


def case_guests():
    c = "guests"
    hotel = ("Hotel Beispiel", "hotel@hotel.example")

    k = "gu-1"
    u = ic_uid(k)
    put(c, "icloud", "guest-01-invite-needs-action", ic_cal([ic_vevent(
        k, u, "Hotel Beispiel - Zimmerreservierung", w_local(D(2027, 2, 12, 15), D(2027, 2, 12, 16)),
        location="Hotel Beispiel, Musterweg 1, 5020 Salzburg",
        extra=[ic_org(*hotel), ic_att(*hotel, "ACCEPTED"), ic_att("Me", ME_IC, "NEEDS-ACTION")],
        alarms=[ic_alarm(k, 1440)])]), "guest", u, note="external organizer, me NEEDS-ACTION")

    k = "gu-2"
    u = g_uid(k)
    put(c, "google", "guest-02-invite-accepted", g_cal([g_vevent(
        k, u, "Hotel Beispiel - Abendessen", w_utc(D(2027, 2, 12, 17), D(2027, 2, 12, 19)),
        extra=[g_org(*hotel), g_att(*hotel, "ACCEPTED"), g_att(ME_G, ME_G, "ACCEPTED")])]),
        "guest", u, note="external organizer, me ACCEPTED")

    k = "gu-3"
    u = g_uid(k)
    put(c, "google", "guest-03-invite-tentative", g_cal([g_vevent(
        k, u, "Fortbildung Projektmanagement", w_utc(D(2027, 1, 20, 8), D(2027, 1, 20, 16)),
        extra=[g_org("Kursleitung", "kurs@kurs.example"),
               g_att("Kursleitung", "kurs@kurs.example", "ACCEPTED"),
               g_att(ME_G, ME_G, "TENTATIVE")])]), "guest", u, note="external organizer, me TENTATIVE")

    k = "gu-4"
    u = ic_uid(k)
    put(c, "icloud", "guest-04-i-organise-two-guests", ic_cal([ic_vevent(
        k, u, "Grillabend im Garten", w_local(D(2027, 5, 29, 18), D(2027, 5, 29, 22)),
        extra=[ic_org("Me", ME_IC), ic_att("Me", ME_IC, "ACCEPTED"),
               ic_att("Alice Sample", "alice.sample@example.org", "NEEDS-ACTION"),
               ic_att("Bob Sample", "bob.sample@example.net", "ACCEPTED")],
        alarms=[ic_alarm(k, 60)])]), "guest", u, note="I organise, two external attendees")

    k = "gu-5"
    u = ic_uid(k)
    put(c, "icloud", "guest-05-organiser-self-only", ic_cal([ic_vevent(
        k, u, "Planung Urlaub (nur ich)", w_local(D(2027, 1, 7, 19), D(2027, 1, 7, 20)),
        extra=[ic_org("Me", ME_IC), ic_att("Me", ME_IC, "ACCEPTED")])]), "normal", u,
        note="ORGANIZER and sole ATTENDEE are the owner: not a guest event")

    k = "gu-6"
    u = g_uid(k)
    conf = "https://meet.example.com/abc-defg-hij"
    put(c, "google", "guest-06-conference-attendees", g_cal([g_vevent(
        k, u, "Architektur-Review", w_utc(D(2027, 1, 12, 13), D(2027, 1, 12, 14)),
        desc=f"Beitreten: {conf}\nDial-in: +43 1 5550100, PIN 1234567;",
        extra=[g_org("Me", ME_G), g_att("Me", ME_G, "ACCEPTED"),
               g_att("Carla Muster", "carla@firma.example", "ACCEPTED"),
               g_att("Dieter Probe", "dieter@firma.example", "NEEDS-ACTION"),
               f"X-GOOGLE-CONFERENCE:{conf}"],
        alarms=[g_alarm_display(10), g_alarm_email(30)])]), "guest", u,
        note="conference link + attendee list")

    k = "gu-7"
    u = g_uid(k)
    put(c, "google", "guest-07-organiser-self-only", g_cal([g_vevent(
        k, u, "Wochenplanung (nur ich)", w_utc(D(2027, 1, 11, 7), D(2027, 1, 11, 7, 30)),
        extra=[g_org("Me", ME_G), g_att("Me", ME_G, "ACCEPTED")])]), "normal", u,
        note="ORGANIZER and sole ATTENDEE are the owner: not a guest event")


def _long_text():
    base = ("Agenda für das Treffen: Punkt eins, Budget und Zeitplan; Punkt zwei, "
            "Risiken & Maßnahmen. Bitte Unterlagen vorab lesen, Fragen sammeln.\n")
    s = (base * 4)[:429]
    assert len(s) == 429 and not s.endswith("\\")
    return s


def case_long_description():
    c = "long_description"
    d = _long_text()
    k = "long-ic"
    u = ic_uid(k)
    put(c, "icloud", "long-description-01", ic_cal([ic_vevent(
        k, u, "Lange Beschreibung (iCloud)", w_local(D(2026, 12, 10, 9), D(2026, 12, 10, 10)), desc=d)]),
        "normal", u, note="DESCRIPTION is 429 characters before escaping/folding")
    k = "long-g"
    u = g_uid(k)
    put(c, "google", "long-description-01", g_cal([g_vevent(
        k, u, "Lange Beschreibung (Google)", w_utc(D(2026, 12, 10, 8), D(2026, 12, 10, 9)), desc=d)]),
        "normal", u, note="DESCRIPTION is 429 characters before escaping/folding")


def case_attach_url():
    c = "attach_url"
    k = "att-ic"
    u = ic_uid(k)
    put(c, "icloud", "attach-uri-01", ic_cal([ic_vevent(
        k, u, "Besprechung mit Anhang", w_local(D(2026, 12, 11, 10), D(2026, 12, 11, 11)),
        extra=["ATTACH;VALUE=URI:https://files.example.com/docs/agenda-2026-12-with-a-rather-long-name.pdf"])]),
        "normal", u)
    k = "url-g"
    u = g_uid(k)
    put(c, "google", "url-01", g_cal([g_vevent(
        k, u, "Webinar Energieeffizienz", w_utc(D(2026, 12, 11, 14), D(2026, 12, 11, 15)),
        extra=["URL:https://events.example.com/webinar/energieeffizienz?ref=cal&id=4711",
               "ATTACH;FILENAME=Folien;FMTTYPE=application/pdf:https://docs.example.com/d/folien-4711/view"])]),
        "normal", u)


CASES = [
    case_icloud_only_timed, case_google_only_timed, case_tz_gmt_offset, case_same_uid_both,
    case_strict_duplicate, case_loose_duplicate, case_allday_single, case_allday_multi,
    case_rrule_weekly_count, case_rrule_until_exdate, case_monthly_byday,
    case_overrides_in_resource, case_dst_series, case_exdate_conflict, case_thisandfuture,
    case_orphan_override, case_two_uids, case_no_uid, case_vtodo, case_rdate, case_floating,
    case_birthdays, case_guests, case_long_description, case_attach_url,
]


def build():
    FILES.clear(); META.clear(); GROUPS.clear()
    for fn in CASES:
        fn()
    for g in GROUPS.values():
        g["files"].sort()
    expected = {
        "description": "Synthetic corpus; every name/address/domain is invented. "
                       "kind = expected classifier result. group = same real-world event.",
        "self_addresses": {"icloud": ME_IC, "google": ME_G},
        "kinds": ["normal", "guest", "birthday", "unsupported:<reason>"],
        "group_relations": {
            "same_uid": "identical UID on both sides (must be recognised as the same event)",
            "strict_duplicate": "different UIDs, same instant/date, same title, same duration",
            "loose_duplicate": "different UIDs, same start, title reworded or differing end",
            "birthday_match": "yearly all-day birthdays matching by name + month-day",
        },
        "files": {rel: META[rel] for rel in sorted(META)},
        "groups": dict(sorted(GROUPS.items())),
    }
    blob = (json.dumps(expected, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return dict(FILES), blob


# --------------------------------------------------------------------------
# validation (--check)
# --------------------------------------------------------------------------

def check_file(rel, data):
    errs = []
    try:
        txt = data.decode("utf-8")
    except UnicodeDecodeError as e:
        return [f"{rel}: not UTF-8 ({e})"], []
    if re.search(rb"(?<!\r)\n|\r(?!\n)", data):
        errs.append(f"{rel}: bare LF or CR")
    if not data.endswith(b"\r\n"):
        errs.append(f"{rel}: missing final CRLF")
    for i, ln in enumerate(data.split(b"\r\n")[:-1], 1):
        if len(ln) > 75:
            errs.append(f"{rel}:{i}: physical line {len(ln)} octets > 75")
        if ln == b"":
            errs.append(f"{rel}:{i}: empty line")
    unfolded = re.sub(r"\r\n[ \t]", "", txt)
    lines = unfolded.split("\r\n")[:-1]
    if not lines or lines[0] != "BEGIN:VCALENDAR" or lines[-1] != "END:VCALENDAR":
        errs.append(f"{rel}: must start/end with VCALENDAR")
    stack, uids = [], []
    for ln in lines:
        if ln.startswith("BEGIN:"):
            stack.append(ln[6:])
        elif ln.startswith("END:"):
            if not stack or stack.pop() != ln[4:]:
                errs.append(f"{rel}: unbalanced {ln}")
        elif ln.startswith("UID:") and stack and stack[-1] in ("VEVENT", "VTODO"):
            uids.append(ln[4:])
    if stack:
        errs.append(f"{rel}: unclosed {stack}")
    if "VERSION:2.0" not in lines or not any(x.startswith("PRODID:") for x in lines):
        errs.append(f"{rel}: VERSION/PRODID missing")
    want = "Apple" if rel.startswith("icloud/") else "Google Inc"
    if not any(x.startswith("PRODID:") and want in x for x in lines):
        errs.append(f"{rel}: PRODID should mention {want}")
    # synthetic-only hygiene: only invented domains
    for dom in re.findall(r"[\w.+-]+@([\w.-]+)", unfolded):
        if dom not in ALLOWED_MAIL_DOMAINS and not dom.endswith(ALLOWED_HOST_SUFFIXES):
            errs.append(f"{rel}: non-synthetic mail domain {dom}")
    for host in re.findall(r"https?://([^/\s:\\?]+)", unfolded):
        if not host.endswith(ALLOWED_HOST_SUFFIXES):
            errs.append(f"{rel}: non-synthetic host {host}")
    return errs, uids


def check(root=OUT):
    errs = []
    exp_path = root / "EXPECTED.json"
    if not exp_path.exists():
        return [f"{exp_path} missing"]
    exp = json.loads(exp_path.read_text("utf-8"))
    if exp.get("self_addresses") != {"icloud": ME_IC, "google": ME_G}:
        errs.append("self_addresses wrong")
    on_disk = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.glob("*/*.ics")}
    if set(on_disk) != set(exp["files"]):
        errs.append(f"file set mismatch: only-disk={sorted(set(on_disk) - set(exp['files']))} "
                    f"only-expected={sorted(set(exp['files']) - set(on_disk))}")
    for rel, data in sorted(on_disk.items()):
        e, uids = check_file(rel, data)
        errs += e
        m = exp["files"].get(rel)
        if not m:
            continue
        if m["side"] != rel.split("/")[0]:
            errs.append(f"{rel}: side mismatch")
        if not KIND_RE.match(m["kind"]):
            errs.append(f"{rel}: bad kind {m['kind']}")
        if sorted(set(uids)) != sorted(m["uids"]) or (uids[:1] or [None])[0] != m["uid"]:
            errs.append(f"{rel}: uid mismatch file={uids} expected={m['uids']}")
        if m["group"] and (m["group"] not in exp["groups"] or rel not in exp["groups"][m["group"]]["files"]):
            errs.append(f"{rel}: group inconsistent")
    for gid, g in exp["groups"].items():
        if len(g["files"]) < 2:
            errs.append(f"group {gid}: fewer than 2 files")
        members = [exp["files"][f] for f in g["files"]]
        if g["relation"] == "same_uid" and len({m["uid"] for m in members}) != 1:
            errs.append(f"group {gid}: same_uid group has differing UIDs")
        if g["relation"] in ("strict_duplicate", "loose_duplicate", "birthday_match") \
                and len({m["uid"] for m in members}) != len(members):
            errs.append(f"group {gid}: duplicate group must have distinct UIDs")
        if {m["side"] for m in members} != {"icloud", "google"}:
            errs.append(f"group {gid}: should span both sides")
    fresh_files, fresh_json = build()
    if fresh_files != on_disk:
        errs.append("on-disk .ics differ from generator output (regenerate)")
    if fresh_json != exp_path.read_bytes():
        errs.append("EXPECTED.json differs from generator output (regenerate)")
    return errs


def write(root=OUT):
    files, blob = build()
    for sub in ("icloud", "google"):
        (root / sub).mkdir(parents=True, exist_ok=True)
        for old in (root / sub).glob("*.ics"):
            old.unlink()
    for rel, data in files.items():
        (root / rel).write_bytes(data)
    (root / "EXPECTED.json").write_bytes(blob)
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="validate committed corpus, do not write")
    args = ap.parse_args(argv)
    if args.check:
        errs = check()
        for e in errs:
            print("FAIL", e)
        n = len(list(OUT.glob("*/*.ics")))
        print(f"{'FAILED' if errs else 'OK'}: {n} files, {len(errs)} problems")
        return 1 if errs else 0
    files = write()
    per_case: dict[str, int] = {}
    for m in META.values():
        per_case[m["case"]] = per_case.get(m["case"], 0) + 1
    for case, n in per_case.items():
        print(f"{n:3d}  {case}")
    print(f"{len(files):3d}  total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
