"""Full-fidelity iCalendar resource model for the sync engine.

A *resource* is one CalDAV object (one ``.ics`` file / href). It holds one *series*:
all VEVENT components sharing a UID, i.e. one master (optional RRULE) plus any
RECURRENCE-ID overrides. The engine always moves whole resources, never single
occurrences, so overrides, EXDATEs and cancelled instances travel together.

This module never re-serialises calendar data. ``prepare_outgoing`` only removes a few
calendar-level lines (METHOD, X-WR-*) and, for adopted duplicates, rewrites UID lines;
every other byte is preserved.

Fingerprints
  core_fp   cross-provider comparable (UTC-normalised instants, normalised text, RRULE,
            EXDATEs, overrides). Used to decide "same event" and to verify after a write.
  full_fp   per-side change detector (all properties except volatile ones). Compared only
            against the *same side's* earlier value, never across providers, so provider
            rewrites (alarms, X-WR props, PRODID) cannot cause ping-pong.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

from .ics import _split, unfold

# ---------------------------------------------------------------------------- constants

VOLATILE_PROPS = frozenset({
    "DTSTAMP", "LAST-MODIFIED", "CREATED", "SEQUENCE", "UID", "PRODID", "METHOD",
    "X-WR-CALNAME", "X-WR-TIMEZONE", "X-WR-CALDESC", "X-WR-RELCALID",
    "X-APPLE-CREATOR-IDENTITY", "X-APPLE-CREATOR-TEAM-IDENTITY", "X-WR-ALARMUID",
    "X-APPLE-LOCAL-DEFAULT-ALARM", "X-APPLE-DEFAULT-ALARM", "X-LIC-LOCATION",
})
VOLATILE_PREFIXES = ("X-APPLE-SUGGESTION-INFO", "X-CALENDARSERVER-", "X-MOZ-", "X-MICROSOFT-CDO-")
STRIP_CALENDAR_LEVEL = frozenset({"METHOD", "X-WR-CALNAME", "X-WR-TIMEZONE", "X-WR-CALDESC", "X-WR-RELCALID"})
BIRTHDAY_WORDS = frozenset({
    "geburtstag", "geburtstags", "birthday", "bday", "b-day", "bd", "geb", "geburtstagskind", "burtstag",
    "von", "of", "s", "'s", "feiert", "hat", "alles", "gute", "zum",
})


# ---------------------------------------------------------------------------- text helpers

def unescape_text(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            out.append("\n" if n in "nN" else n)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def norm_text(s: str) -> str:
    """Normalised free text: unescaped, NFKC, whitespace collapsed (case preserved)."""
    return " ".join(unicodedata.normalize("NFKC", unescape_text(s)).split())


def norm_title(s: str) -> str:
    return norm_text(s).casefold()


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def title_tokens(s: str) -> frozenset[str]:
    t = unicodedata.normalize("NFKD", norm_title(s))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return frozenset(_TOKEN_RE.findall(t))


def titles_similar(a: str, b: str) -> bool:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return norm_title(a) == norm_title(b)
    if ta == tb or ta <= tb or tb <= ta:
        return True
    return len(ta & tb) / len(ta | tb) >= 0.5


# ---------------------------------------------------------------------------- time handling

_OFFSET_RE = re.compile(r"^(?:GMT|UTC)\s*([+-])(\d{1,2}):?(\d{2})?$", re.I)


@lru_cache(maxsize=256)
def _zone(name: str):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def _fixed_offset_from_name(name: str):
    m = _OFFSET_RE.match(name.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))


def _parse_dt(value: str) -> datetime | None:
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%dT%H%MZ", "%Y%m%dT%H%M"):
        try:
            d = datetime.strptime(value, fmt)
            return d.replace(tzinfo=timezone.utc) if value.endswith("Z") else d
        except ValueError:
            continue
    return None


def norm_time(value: str, tzid: str, is_date: bool, vtz: dict[str, object]) -> str:
    """Comparable representation: 'D<date>' all-day, 'U<utc>' instant, 'F<local>' floating,
    'X<local>@<tzid>' for an unresolvable zone (never matched across providers)."""
    v = value.strip()
    if is_date or (len(v) == 8 and v.isdigit()):
        return "D" + v[:8]
    d = _parse_dt(v)
    if d is None:
        return "?" + v
    if d.tzinfo is not None:  # Z
        return d.astimezone(timezone.utc).strftime("U%Y%m%dT%H%M%S")
    if not tzid:
        return "F" + d.strftime("%Y%m%dT%H%M%S")
    tz = _zone(tzid) or _fixed_offset_from_name(tzid) or vtz.get(tzid)
    if tz is None:
        return f"X{d.strftime('%Y%m%dT%H%M%S')}@{tzid}"
    return d.replace(tzinfo=tz).astimezone(timezone.utc).strftime("U%Y%m%dT%H%M%S")


def _to_dt(n: str) -> datetime | None:
    try:
        if n.startswith("D"):
            return datetime.strptime(n[1:], "%Y%m%d")
        if n.startswith(("U", "F")):
            return datetime.strptime(n[1:], "%Y%m%dT%H%M%S")
    except ValueError:
        pass
    return None


def _fmt_like(n: str, d: datetime) -> str:
    return "D" + d.strftime("%Y%m%d") if n.startswith("D") else n[0] + d.strftime("%Y%m%dT%H%M%S")


def _parse_duration(v: str) -> timedelta | None:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m or not any(m.groups()[1:]):
        return None
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


# ---------------------------------------------------------------------------- parsing

@dataclass
class Comp:
    props: list[tuple[str, dict, str]] = field(default_factory=list)    # top-level props of the VEVENT
    alarms: list[tuple[str, str]] = field(default_factory=list)         # (ACTION, TRIGGER)
    uid: str = ""
    rid: str = ""            # normalised RECURRENCE-ID ("" for master)
    rid_range: bool = False
    start: str = ""          # normalised
    end: str = ""            # normalised (computed from DURATION / all-day default)
    allday: bool = False
    summary: str = ""
    location: str = ""
    description: str = ""
    rrule: str = ""
    exdates: tuple[str, ...] = ()
    has_rdate: bool = False
    has_exrule: bool = False
    status: str = ""
    transp: str = ""
    cls: str = ""
    organizer: str = ""
    attendees: tuple[tuple[str, str], ...] = ()
    categories: str = ""
    tz: str = ""             # TZID (or "Z"/"") of DTSTART; part of core_fp for recurring series (DST behaviour)
    markers: tuple[str, ...] = ()   # property names hinting at special (e.g. birthday) events


@dataclass
class Resource:
    href: str
    etag: str
    data: bytes
    comps: list[Comp] = field(default_factory=list)
    other_components: list[str] = field(default_factory=list)   # VTODO/VJOURNAL/VFREEBUSY found
    parse_errors: list[str] = field(default_factory=list)
    kind: str = "normal"           # normal | guest | birthday | unsupported
    reasons: list[str] = field(default_factory=list)

    # filled by parse_resource
    uid: str = ""
    sha: str = ""
    core: str = ""
    full: str = ""
    strict: str = ""
    ident: str = ""      # stable public id (hash of side+uid), never contains titles

    @property
    def master(self) -> Comp | None:
        for c in self.comps:
            if not c.rid:
                return c
        return None

    @property
    def overrides(self) -> list[Comp]:
        return [c for c in self.comps if c.rid]

    @property
    def title(self) -> str:
        m = self.master or (self.comps[0] if self.comps else None)
        return m.summary if m else ""


def _addr(v: str) -> str:
    return v.strip().lower().removeprefix("mailto:")


def _rrule_norm(r: str) -> str:
    parts = {}
    for p in r.split(";"):
        k, _, v = p.partition("=")
        if k.strip():
            parts[k.strip().upper()] = v.strip().upper()
    if parts.get("INTERVAL") == "1":
        parts.pop("INTERVAL")
    return ";".join(f"{k}={parts[k]}" for k in sorted(parts))


def _vtimezones(lines: list[str]) -> dict[str, object]:
    """TZID -> fixed tzinfo, only when the VTIMEZONE has a single distinct UTC offset."""
    out: dict[str, object] = {}
    cur: dict | None = None
    for line in lines:
        name, params, value = _split(line)
        if name == "BEGIN" and value.upper() == "VTIMEZONE":
            cur = {"tzid": "", "offsets": set()}
        elif name == "END" and value.upper() == "VTIMEZONE" and cur is not None:
            if cur["tzid"] and len(cur["offsets"]) == 1:
                out[cur["tzid"]] = next(iter(cur["offsets"]))
            cur = None
        elif cur is not None:
            if name == "TZID":
                cur["tzid"] = value.strip()
            elif name == "TZOFFSETTO":
                m = re.fullmatch(r"([+-])(\d{2})(\d{2})(\d{2})?", value.strip())
                if m:
                    sign = -1 if m.group(1) == "-" else 1
                    cur["offsets"].add(timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))))
    return out


def parse_resource(data: bytes, href: str = "", etag: str = "") -> Resource:
    res = Resource(href=href, etag=etag, data=data, sha=hashlib.sha256(data).hexdigest())
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", "replace")
        res.parse_errors.append("invalid_utf8")
    if re.search(rb"\r(?!\n)", data):
        res.parse_errors.append("bare_cr")
    lines = [ln for ln in unfold(text) if ln]
    vtz = _vtimezones(lines)
    stack: list[str] = []
    cur: Comp | None = None
    alarm: dict | None = None
    raw_props: dict[int, tuple] = {}
    for line in lines:
        name, params, value = _split(line)
        uvalue = value.strip().upper()
        if name == "BEGIN":
            stack.append(uvalue)
            if uvalue == "VEVENT" and len(stack) == 2:
                cur = Comp()
            elif uvalue == "VALARM" and cur is not None:
                alarm = {}
            elif uvalue in ("VTODO", "VJOURNAL", "VFREEBUSY") and len(stack) == 2:
                res.other_components.append(uvalue)
            elif cur is not None and len(stack) == 3 and uvalue not in ("VALARM",):
                res.other_components.append("NESTED-" + uvalue)      # PARTICIPANT, VLOCATION, ...
            continue
        if name == "END":
            if stack:
                top = stack.pop()
                if top == "VEVENT" and cur is not None and len(stack) == 1:
                    res.comps.append(_finish(cur, vtz))
                    cur = None
                elif top == "VALARM" and alarm is not None and cur is not None:
                    cur.alarms.append((alarm.get("ACTION", ""), alarm.get("TRIGGER", "")))
                    alarm = None
            else:
                res.parse_errors.append("unbalanced_end")
            continue
        if cur is None:
            continue
        if alarm is not None and stack and stack[-1] == "VALARM":
            if name in ("ACTION", "TRIGGER"):
                alarm[name] = value.strip().upper()
            continue
        if stack and stack[-1] != "VEVENT":
            continue
        cur.props.append((name, params, value))
    if stack:
        res.parse_errors.append("unbalanced_begin")
    uids = {c.uid for c in res.comps}
    res.uid = next(iter(uids)) if len(uids) == 1 else ""
    return res


def _finish(c: Comp, vtz: dict) -> Comp:
    ex: list[str] = []
    attendees: list[tuple[str, str]] = []
    dtstart = dtend = dur = None
    for name, params, value in c.props:
        tz = params.get("TZID", "")
        isd = params.get("VALUE", "").upper() == "DATE"
        if name == "UID":
            c.uid = value.strip()
        elif name == "RECURRENCE-ID":
            c.rid = norm_time(value, tz, isd, vtz)
            c.rid_range = bool(params.get("RANGE"))
        elif name == "DTSTART":
            dtstart = (value, tz, isd)
        elif name == "DTEND":
            dtend = (value, tz, isd)
        elif name == "DURATION":
            dur = value.strip()
        elif name == "SUMMARY":
            c.summary = norm_text(value)
        elif name == "LOCATION":
            c.location = norm_text(value)
        elif name == "DESCRIPTION":
            c.description = norm_text(value)
        elif name == "RRULE":
            c.rrule = _rrule_norm(value)
        elif name == "EXDATE":
            ex += [norm_time(v, tz, isd or (len(v.strip()) == 8), vtz) for v in value.split(",") if v.strip()]
        elif name == "RDATE":
            c.has_rdate = True
        elif name == "EXRULE":
            c.has_exrule = True
        elif name == "STATUS":
            c.status = value.strip().upper()
        elif name == "TRANSP":
            c.transp = value.strip().upper()
        elif name == "CLASS":
            c.cls = value.strip().upper()
        elif name == "ORGANIZER":
            c.organizer = _addr(value)
        elif name == "ATTENDEE":
            attendees.append((_addr(value), params.get("PARTSTAT", "").upper()))
        elif name == "CATEGORIES":
            c.categories = value.strip().casefold()
        if "BIRTHDAY" in name or "GEBURTSTAG" in name:
            c.markers += (name,)
    if dtstart:
        c.tz = dtstart[1] or ("Z" if dtstart[0].strip().endswith("Z") else "")
        c.start = norm_time(*dtstart[:1], dtstart[1], dtstart[2], vtz)
        c.allday = c.start.startswith("D")
    if dtend:
        c.end = norm_time(dtend[0], dtend[1], dtend[2], vtz)
    elif c.start:
        d = _to_dt(c.start)
        td = _parse_duration(dur) if dur else None
        if d is not None and td is not None:
            c.end = _fmt_like(c.start, d + td)
        elif d is not None and c.allday:
            c.end = _fmt_like(c.start, d + timedelta(days=1))
        else:
            c.end = c.start
    c.exdates = tuple(sorted(set(ex)))
    c.attendees = tuple(sorted(attendees))
    return c


# ---------------------------------------------------------------------------- classification

def _is_yearly_allday(c: Comp) -> bool:
    return c.allday and not c.rid and re.search(r"(^|;)FREQ=YEARLY(;|$)", c.rrule) is not None


def classify(res: Resource, self_addresses: frozenset[str] = frozenset()) -> tuple[str, list[str]]:
    """-> (kind, reasons). kind in normal|guest|birthday|unsupported. Fail-closed: anything
    uncertain is *not* 'normal'."""
    r: list[str] = []
    if res.parse_errors:
        r += [f"parse:{e}" for e in res.parse_errors]
    if res.other_components:
        r.append("non_vevent:" + ",".join(sorted(set(res.other_components))))
    if not res.comps:
        r.append("no_vevent")
    uids = {c.uid for c in res.comps}
    if "" in uids:
        r.append("missing_uid")
    elif len(uids) > 1:
        r.append("multiple_uids")
    masters = [c for c in res.comps if not c.rid]
    if res.comps and len(masters) != 1:
        r.append("no_master" if not masters else "multiple_masters")
    ridkeys = [c.rid for c in res.comps if c.rid]
    if len(ridkeys) != len(set(ridkeys)):
        r.append("duplicate_override")
    for c in res.comps:
        if c.rid_range:
            r.append("recurrence_range")
        if c.has_rdate:
            r.append("rdate")
        if c.has_exrule:
            r.append("exrule")
        if not c.start:
            r.append("no_dtstart")
        if c.start.startswith("X") or c.end.startswith("X") or c.start.startswith("?"):
            r.append("unresolved_timezone")
        if c.start.startswith("F"):
            r.append("floating_time")        # providers anchor floating times to a zone: not faithfully copyable
    if r:
        return "unsupported", sorted(set(r))
    m = masters[0]
    # Birthdays first: Google's contact-linked birthdays carry an ORGANIZER at calendar.google.com
    # and a self ATTENDEE, so they would otherwise look like guest events. Both kinds are exempt
    # from the normal pair; the distinction only matters for the birthday ledger.
    if _is_yearly_allday(m):
        return "birthday", ["yearly_allday_series"] + (["google_system_organizer"] if m.organizer.endswith("@calendar.google.com") else [])
    if m.markers or "birthday" in m.categories or "geburtstag" in m.categories or m.uid.lower().startswith(("birthday", "_birthday")):
        return "birthday", ["birthday_marker"]
    # guest: any organiser/attendee that is not purely me
    for c in res.comps:
        if c.organizer and c.organizer not in self_addresses:
            return "guest", ["organizer_not_self"]
        if any(a not in self_addresses for a, _ in c.attendees):
            return "guest", ["attendee_not_self"]
        if (c.organizer or c.attendees) and not self_addresses:
            return "guest", ["self_addresses_unknown"]
    return "normal", []


# ---------------------------------------------------------------------------- fingerprints

def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def core_fp(res: Resource) -> str:
    rows = []
    for c in sorted(res.comps, key=lambda c: c.rid):
        rows.append([
            c.rid, c.start, c.end, c.rrule, list(c.exdates), c.summary, c.location, c.description,
            c.status or "CONFIRMED", c.transp or "OPAQUE",
            "PRIVATE" if c.cls in ("PRIVATE", "CONFIDENTIAL") else "",      # privacy must not be lost in a copy
            c.tz if c.rrule else "",                                       # a flattened TZID series would drift at DST
        ])
    return _h(rows)


def alarm_fp(res: Resource) -> str:
    return _h([[c.rid, sorted(c.alarms)] for c in sorted(res.comps, key=lambda c: c.rid)])


def day_key(res: Resource) -> str:
    m = res.master
    return m.start[1:9] if m and m.start else ""


def tz_selfcheck() -> bool:
    """False if the time-zone database is missing: every zoned event would be 'unsupported'."""
    return _zone("Europe/Vienna") is not None


def full_fp(res: Resource) -> str:
    rows = []
    for c in sorted(res.comps, key=lambda c: c.rid):
        props = []
        for name, params, value in c.props:
            if name in VOLATILE_PROPS or name.startswith(VOLATILE_PREFIXES):
                continue
            props.append([name, sorted(params.items()), value.strip()])
        props.sort(key=lambda p: json.dumps(p, sort_keys=True))
        rows.append([c.rid, props, sorted(c.alarms)])
    return _h(rows)


def strict_key(res: Resource) -> str:
    """Different-UID duplicate identity: same instants, title, recurrence shape, overrides."""
    m = res.master
    if m is None:
        return ""
    return _h([m.start, m.end, m.allday, norm_title(m.summary), m.rrule, list(m.exdates),
               sorted((c.rid, c.start, c.end, c.status) for c in res.overrides)])


def start_key(res: Resource) -> str:
    m = res.master
    if m is None:
        return ""
    return m.start[:9] if m.allday else m.start


def item_id(side: str, uid: str) -> str:
    return hashlib.sha256(f"{side}|{uid}".encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------- outgoing data

_UID_LINE = re.compile(rb"^(UID)([;:])", re.I)


def prepare_outgoing(data: bytes, uid: str | None = None) -> bytes:
    """Return ``data`` ready to PUT on the other provider. Only removes calendar-level
    METHOD/X-WR-* lines and (if ``uid``) rewrites the UID of every VEVENT; all other bytes
    are preserved, including line endings and folding."""
    nl = b"\r\n" if b"\r\n" in data else b"\n"
    raw_lines = data.split(nl)
    out: list[bytes] = []
    depth = 0
    skip_cont = False
    for ln in raw_lines:
        if ln[:1] in (b" ", b"\t"):
            if skip_cont:
                continue
            out.append(ln)
            continue
        skip_cont = False
        up = ln.upper()
        if up.startswith(b"BEGIN:"):
            depth += 1
        elif up.startswith(b"END:"):
            depth -= 1
        elif depth == 1:
            name = re.split(rb"[;:]", ln, maxsplit=1)[0].decode("ascii", "replace").upper()
            if name in STRIP_CALENDAR_LEVEL:
                skip_cont = True
                continue
        elif depth == 2 and uid is not None and up.startswith(b"UID") and _UID_LINE.match(ln):
            out.append(b"UID:" + uid.encode("utf-8"))
            skip_cont = True
            continue
        out.append(ln)
    return nl.join(out)


# ---------------------------------------------------------------------------- occurrence estimate

_WD = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def count_occurrences(res: Resource, window_start: date, window_end: date) -> tuple[int, bool]:
    """Estimate how many occurrences of ``res`` fall into [window_start, window_end).
    Returns (count, exact). Reporting aid only (never used for sync decisions): supports
    DAILY/WEEKLY(BYDAY)/MONTHLY(BYMONTHDAY)/YEARLY with INTERVAL/COUNT/UNTIL, EXDATEs and
    cancelled overrides; anything fancier returns exact=False and counts the master once."""
    m = res.master
    d0 = _to_dt(m.start) if m and m.start else None
    if m is None or d0 is None:
        return 0, False
    first = d0.date()
    if not m.rrule:
        return (1 if window_start <= first < window_end else 0), True
    r = dict(p.split("=", 1) for p in m.rrule.split(";") if "=" in p)
    freq, interval = r.get("FREQ", ""), int(r.get("INTERVAL", "1") or 1)
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY") or set(r) - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "WKST"}:
        return (1 if window_start <= first < window_end else 0), False
    if (r.get("BYDAY") and freq != "WEEKLY") or (r.get("BYMONTHDAY") and freq != "MONTHLY") or r.get("BYMONTH"):
        return (1 if window_start <= first < window_end else 0), False
    until = None
    if r.get("UNTIL"):
        u = _to_dt("U" + r["UNTIL"].rstrip("Z")) if "T" in r["UNTIL"] else _to_dt("D" + r["UNTIL"])
        until = u.date() if u else None
    limit = int(r["COUNT"]) if r.get("COUNT") else None
    ex = {_to_dt(x).date() for x in m.exdates if _to_dt(x)}
    cancelled = {_to_dt(c.rid).date() for c in res.overrides if c.status == "CANCELLED" and _to_dt(c.rid)}
    days = ([_WD[x[-2:]] for x in r["BYDAY"].split(",") if x[-2:] in _WD] if r.get("BYDAY") else [first.weekday()])
    n = produced = 0
    cur, step = first, 0
    horizon = window_end
    while cur < horizon and step < 20000:
        cand: list[date] = []
        if freq == "DAILY":
            cand = [first + timedelta(days=step * interval)]
        elif freq == "WEEKLY":
            base = first - timedelta(days=first.weekday()) + timedelta(weeks=step * interval)
            cand = sorted(base + timedelta(days=d) for d in days)
        elif freq == "MONTHLY":
            mm = first.month - 1 + step * interval
            y, mo = first.year + mm // 12, mm % 12 + 1
            for dd in ([int(x) for x in r["BYMONTHDAY"].split(",")] if r.get("BYMONTHDAY") else [first.day]):
                try:
                    cand.append(date(y, mo, dd))
                except ValueError:
                    pass
        else:
            try:
                cand = [date(first.year + step * interval, first.month, first.day)]
            except ValueError:
                cand = []
        for c in cand:
            if c < first:
                continue
            if until and c > until:
                horizon = window_start
                break
            produced += 1
            if limit and produced > limit:
                horizon = window_start
                break
            if window_start <= c < window_end and c not in ex and c not in cancelled:
                n += 1
        cur = cand[-1] if cand else cur + timedelta(days=28)
        step += 1
    return n, True
