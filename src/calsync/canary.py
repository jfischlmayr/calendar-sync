"""Canary selection for the first writes of a direction.

The first-time plan of a populated pair contains hundreds of creates. Approving "the plan" and
letting the cap pick the first N would copy whatever sorts first by opaque id. This module picks the
*safest* N creates by hard, explainable rules, so an operator can approve a plan that contains
exactly those ops (``calsync plan --canary N``) and nothing else.

Eligible = a plain, single, timed, future, attendee-free event whose every property is on a short
whitelist. Anything unusual (recurrence, overrides, organiser/attendees, attachments, conference
data, privacy class, e-mail alarms, all-day, unresolved zone, long span, historic, far future,
non-confirmed status) is simply not a candidate.

A second, wider profile (``profile="batch"``) exists for the *bulk migration after a successful canary*: it admits
history and the plain shapes that make up most of a real calendar (any date, single/multi-day all-day events,
tentative status, a finite plain recurrence, URL / Apple location+travel metadata, e-mail alarms that only
address the owner). It is still a whitelist: organiser/attendees, overrides, exdates, privacy class, birthday /
anniversary titles, attachments, conference data, other X-properties, cancelled events, unresolved or floating
times and every exempt kind (guest, birthday, unsupported) are rejected exactly as for a canary.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from . import icsmodel as im

PROFILES = ("canary", "batch")

MIN_LEAD_DAYS = 2            # at least this far in the future (no "happening now" edge cases)
MAX_LEAD_DAYS = 270          # not so far out that nobody would notice a problem
MAX_SPAN = timedelta(hours=12)
MAX_SINCE_DAYS = 90          # hard ceiling for the opt-in recent-past window (--since-days-ago)
PAST_GUARD_DAYS = 2          # recent-past events must also be at least this many days behind us
# Properties a canary VEVENT may carry. ORGANIZER / ATTENDEE / RRULE / ATTACH / CONFERENCE / URL ... are not here.
ALLOWED_PROPS = frozenset({
    "UID", "DTSTAMP", "DTSTART", "DTEND", "DURATION", "SUMMARY", "DESCRIPTION", "LOCATION", "STATUS", "TRANSP",
    "SEQUENCE", "CREATED", "LAST-MODIFIED", "CATEGORIES", "CLASS", "PRIORITY",
})
ALLOWED_PREFIXES = ("X-APPLE-", "X-WR-", "X-MOZ-", "X-MICROSOFT-", "X-GOOGLE-")
FORBIDDEN_X = ("CONFERENCE", "ATTENDEE", "ORGANIZER", "INVITE", "TRAVEL", "STRUCTURED-LOCATION", "MEETING", "TEAMS",
               "SKYPE", "HANGOUT", "ZOOM", "CONF", "LINK", "URL")
# A one-off event that is really a birthday / anniversary is a "special" record (it would sit next to the
# contact-linked birthday series on the other side): never a canary, whatever the engine classified it as.
SPECIAL_TITLE_WORDS = ("geburtstag", "birthday", "bday", "b-day", "jahrestag", "anniversary", "hochzeitstag",
                       "namenstag", "jubiläum", "jubilaeum")
SAFE_ALARM_ACTIONS = frozenset({"DISPLAY", "AUDIO"})
# --- batch profile additions (never active for the canary profile)
MAX_BATCH_SPAN = timedelta(days=100)      # long holidays / trips are fine; presentation only
BATCH_EXTRA_PROPS = frozenset({"URL"})
# Apple location / travel-time metadata is harmless presentation data; every other X- rule stays.
BATCH_FORBIDDEN_X = tuple(f for f in FORBIDDEN_X if f not in ("TRAVEL", "STRUCTURED-LOCATION"))
_VALARM = re.compile(rb"BEGIN:VALARM(.*?)END:VALARM", re.S | re.I)
_ALARM_ATT = re.compile(rb"^ATTENDEE[^:\r\n]*:(?:mailto:)?([^\r\n]+)", re.I | re.M)
# Providers rewrite descriptions that contain a video-conference link (Google appends its own "-::~:~::~" footer to
# Teams/Meet/Zoom invitations), so the stored copy would never equal the source: not an automatic candidate.
_CONF_TEXT = re.compile(r"teams\.microsoft\.com|teams\.live\.com|zoom\.(us|com)/|meet\.google\.com|webex\.com|gotomeet|"
                        r"whereby\.com|bluejeans\.com|skype\.com/|jit\.si|video call|videoanruf|microsoft teams meeting", re.I)
_URL_OK = re.compile(r"^(https?://\S+)?$", re.I)      # empty (Apple writes empty URLs) or http(s); message:/other schemes stay out


def email_alarm_attendees(data: bytes) -> set[str] | None:
    """Addresses named by EMAIL alarms of ``data`` (unfolded); None if there is no EMAIL alarm."""
    unfolded = re.sub(rb"\r?\n[ \t]", b"", data)
    out: set[str] | None = None
    for blk in _VALARM.findall(unfolded):
        if re.search(rb"^ACTION:EMAIL\s*$", blk, re.I | re.M):
            out = (out or set()) | {m.decode("utf-8", "replace").strip().lower() for m in _ALARM_ATT.findall(blk)}
    return out


def _finite_plain_rrule(c) -> bool:
    """A single-master series that ends (COUNT/UNTIL), is timed with a named zone, has no exdates/overrides."""
    parts = dict(x.split("=", 1) for x in c.rrule.split(";") if "=" in x)
    return (bool(parts) and ("COUNT" in parts or "UNTIL" in parts) and parts.get("FREQ") in ("DAILY", "WEEKLY", "MONTHLY")
            and not c.exdates and not c.has_rdate and not c.has_exrule and not c.rid and not c.allday
            and c.tz not in ("", "Z"))


def _start_dt(n: str) -> datetime | None:
    """'U20261105T100000' -> naive UTC datetime; anything else (all-day, floating, unresolved) -> None."""
    if not n.startswith("U"):
        return None
    try:
        return datetime.strptime(n[1:], "%Y%m%dT%H%M%S")
    except ValueError:
        return None


def ineligible_reasons(r: im.Resource, today: date, *, min_lead: int = MIN_LEAD_DAYS,
                       max_lead: int = MAX_LEAD_DAYS, since_days_ago: int = 0, profile: str = "canary",
                       self_addresses: frozenset[str] = frozenset()) -> list[str]:
    """Why ``r`` must not be selected ([] = eligible). Pure.

    ``since_days_ago`` (canary profile, opt-in, 1..MAX_SINCE_DAYS) additionally admits events that started between
    that many days and PAST_GUARD_DAYS days ago. Every other predicate is unchanged: it only widens the date window.
    ``profile="batch"`` is the wider whitelist described in the module docstring (any date; see there)."""
    if profile not in PROFILES:
        return ["unknown_profile"]
    batch = profile == "batch"
    why: list[str] = []
    if batch and since_days_ago:
        why.append("since_days_ago_not_for_batch")
    if r.kind != "normal":
        return [f"kind_{r.kind}"]
    if r.parse_errors or r.other_components:
        why.append("parse_or_foreign_components")
    if len(r.comps) != 1 or r.overrides:
        why.append("not_single_component")
        return why
    c = r.comps[0]
    if c.rrule or c.exdates or c.has_rdate or c.has_exrule or c.rid:
        if not (batch and c.rrule and _finite_plain_rrule(c)):
            why.append("recurrence")
    if c.organizer or c.attendees:
        why.append("has_organizer_or_attendees")
    if c.markers:
        why.append("special_marker")
    if _CONF_TEXT.search(c.description) or _CONF_TEXT.search(c.location) or _CONF_TEXT.search(c.summary):
        why.append("conference_link_in_text")
    if any(w in c.summary.casefold() for w in SPECIAL_TITLE_WORDS):
        why.append("special_title")
    if c.allday and not batch:
        why.append("all_day")
    if c.cls not in ("", "PUBLIC"):
        why.append("privacy_class")
    allowed_status = ("", "CONFIRMED", "TENTATIVE") if batch else ("", "CONFIRMED")
    if c.status not in allowed_status:
        why.append("status_not_confirmed")
    forbidden_x = BATCH_FORBIDDEN_X if batch else FORBIDDEN_X
    for name, params, value in c.props:
        if name in ALLOWED_PROPS:
            continue
        if batch and name == "RRULE" and "recurrence" not in why:      # finite plain series, checked above
            continue
        if batch and name in BATCH_EXTRA_PROPS:
            if name == "URL" and not _URL_OK.match(value.strip()):
                why.append("property_url_scheme")
            continue
        if name.startswith(ALLOWED_PREFIXES) and not any(f in name for f in forbidden_x):
            continue
        why.append("property_" + name.lower())
    for action, _trigger in c.alarms:
        if action in SAFE_ALARM_ACTIONS:
            continue
        if batch and action == "EMAIL":
            att = email_alarm_attendees(r.data)
            if att is not None and att <= self_addresses:         # only ever mails the owner
                continue
            why.append("email_alarm_to_others")
            continue
        why.append("non_display_alarm")
    s, e = _start_dt(c.start), _start_dt(c.end)
    if not any(n in ("DTEND", "DURATION") for n, _p, _v in c.props):
        why.append("no_explicit_end")
    if batch:
        s, e = (s, e) if not c.allday else (_day(c.start), _day(c.end))
        if s is None or e is None:
            why.append("time_not_resolved_instant")
        elif e <= s or e - s > MAX_BATCH_SPAN:
            why.append("span")
    elif s is None or e is None:
        why.append("time_not_resolved_instant")
    else:
        if e <= s or e - s > MAX_SPAN:
            why.append("span")
        lead = (s.date() - today).days
        recent_past = bool(since_days_ago) and -since_days_ago <= lead <= -PAST_GUARD_DAYS
        if lead < min_lead and not recent_past:
            why.append("not_in_future")
        elif lead > max_lead:
            why.append("too_far_out")
    if since_days_ago and not 1 <= since_days_ago <= MAX_SINCE_DAYS:
        why.append("since_days_ago_out_of_range")
    return sorted(set(why))


def _day(n: str) -> datetime | None:
    """'D20261105' -> naive datetime at 00:00 (all-day dates); anything else -> None."""
    if not n.startswith("D"):
        return None
    try:
        return datetime.strptime(n[1:], "%Y%m%d")
    except ValueError:
        return None


def check_selected(plan, direction: str, ids, today: date, *, since_days_ago: int = 0, profile: str = "canary",
                   self_addresses: frozenset[str] = frozenset()) -> dict[str, list[str]]:
    """{src_id: reasons} for every selected create that is NOT canary-eligible ({} = all fine)."""
    src_side = "b" if direction == "a" else "a"
    by_href = {r.href: r for r in plan.sides[src_side].values()}
    bad: dict[str, list[str]] = {}
    for op in plan.ops:
        if op.kind == "create" and op.dst == direction and op.src_id in ids:
            r = by_href.get(op.src_href)
            why = (ineligible_reasons(r, today, since_days_ago=since_days_ago, profile=profile,
                                      self_addresses=self_addresses) if r is not None else ["source_missing"])
            if why:
                bad[op.src_id] = why
    return bad


def pick(plan, direction: str, today: date, n: int, *, min_lead: int = MIN_LEAD_DAYS,
         max_lead: int = MAX_LEAD_DAYS, since_days_ago: int = 0, profile: str = "canary",
         self_addresses: frozenset[str] = frozenset()) -> list[tuple[str, str, str]]:
    """The ``n`` safest creates that write to ``direction``: [(src_id, start, summary), ...], soonest first
    (ties by id). Only ``create`` ops whose source is eligible are considered. Returns fewer than ``n`` if
    fewer exist - the caller must treat that as "cannot select a canary"."""
    src_side = "b" if direction == "a" else "a"
    by_href = {r.href: r for r in plan.sides[src_side].values()}
    cands = []
    for op in plan.ops:
        if op.kind != "create" or op.dst != direction:
            continue
        r = by_href.get(op.src_href)
        if r is None or ineligible_reasons(r, today, min_lead=min_lead, max_lead=max_lead,
                                         since_days_ago=since_days_ago, profile=profile,
                                         self_addresses=self_addresses):
            continue
        cands.append((r.comps[0].start, r.ident, r.comps[0].summary))
    cands.sort(reverse=bool(since_days_ago) or profile == "batch")   # recent-past / batch: the most recent first
    return [(i, s, t) for s, i, t in cands[:n]]


_CONF_PROPS = ("CONFERENCE", "TEAMS", "SKYPE", "ZOOM", "HANGOUT", "MEETING", "ONLINE")


def provider_rewrite_risks(r: im.Resource) -> list[str]:
    """Why copying ``r`` automatically could make the receiving provider store something different (so verify-after-write
    would fail and BLOCK the whole pair): video-conference links/data. Providers append their own footer or attach their own
    conference object, so the copy can never equal the source. Such an item is *held* with a finding instead of written -
    in steady state too, not only for the first migration. [] = no known risk. Pure."""
    why: list[str] = []
    for c in r.comps:
        if _CONF_TEXT.search(c.description) or _CONF_TEXT.search(c.location) or _CONF_TEXT.search(c.summary):
            why.append("conference_link_in_text")
        for name, _params, _value in c.props:
            if name.startswith("X-") and any(f in name for f in _CONF_PROPS):
                why.append("conference_property")
    return sorted(set(why))
