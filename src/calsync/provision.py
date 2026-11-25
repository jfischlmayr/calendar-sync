"""Gated, additive-only remote writes: create calendars (CalDAV MKCALENDAR on iCloud,
Calendar REST API on Google) and copy approved items with If-None-Match:* (can never
overwrite). Everything defaults to DRY-RUN; `execute=True` additionally requires the
global write gates (CALSYNC_ALLOW_WRITES=1, fresh backup, no PAUSE)."""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from . import caldav, ics
from .config import Settings, env_secret
from .gauth import GoogleTokenSource
from .store import StoreError
from .util import now, read_json

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "a": "http://apple.com/ns/ical/"}

DEFAULT_GOOGLE_API = "https://www.googleapis.com/calendar/v3"
_MAX_JSON = 4 << 20                      # cap for a Google REST JSON response
_MAX_PAGES = 20                          # calendarList pagination bound
_ANY_STATUS = tuple(range(100, 600))     # DAV.req returns every non-redirect status to its caller
_XML_CT = "application/xml; charset=utf-8"
_BAD_TEXT = re.compile(r"[\x00-\x1f\x7f\ufffe\uffff]")
# iCloud discovery legitimately hands out per-user shard hosts (pNN-caldav.icloud.com). Only these
# suffixes may be used as sibling hosts of a base on the same suffix; everything else must be the
# exact same origin as the configured base.
_SIBLING_SUFFIXES = ("icloud.com",)


class OriginError(ValueError):
    """A server-supplied or caller-supplied URL points outside the configured origin."""


class GateClosed(Exception):
    pass


def require_gates(s: Settings) -> None:
    f = []
    if not s.write_enabled:
        f.append("CALSYNC_ALLOW_WRITES!=1")
    if (s.state / "PAUSE").exists():
        f.append("PAUSE file present")
    b = read_json(s.state / "backup_ok.json", {})
    age = now() - b.get("ts", 0)
    if age > s.backup_max_age_h * 3600 or age < -300:        # stale, missing or future-dated
        f.append("no fresh verified backup")
    if f:
        raise GateClosed("; ".join(f))


def _clean_text(value: str, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 255 or _BAD_TEXT.search(value):
        raise ValueError(f"invalid {what}: must be 1-255 printable characters without control characters")
    return value


def _xml_text(s: str) -> str:
    """Escape for XML element text and attribute values."""
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def _is_ip(host: str) -> bool:
    return bool(re.fullmatch(r"[\d.:]+", host))


def _suffix_member(host: str) -> str | None:
    for suf in _SIBLING_SUFFIXES:
        if host == suf or host.endswith("." + suf):
            return suf
    return None


def _origin_ok(base: urllib.parse.SplitResult, target: urllib.parse.SplitResult) -> bool:
    if target.username is not None or target.password is not None or not target.hostname:
        return False
    if caldav._origin(base) == caldav._origin(target):
        return True
    bh, th = (base.hostname or "").lower(), (target.hostname or "").lower()
    suf = _suffix_member(bh)
    return bool(suf and suf == _suffix_member(th) and base.scheme == target.scheme == "https"
                and not _is_ip(bh) and not _is_ip(th) and (target.port or 443) == 443)


class DAV:
    """Minimal CalDAV client for provisioning, on top of the hardened core transport
    (caldav._Http: per-request auth, no automatic redirects, same-origin manual redirects,
    bounded bodies, credential scrubbing). Every URL - including server-supplied hrefs - is
    resolved against the base and refused unless it stays on the configured origin (or an
    iCloud shard host), BEFORE any request or credential leaves the process."""

    def __init__(self, base: str, user: str | None, password: str | None, *, opener=None, timeout: float = 30):
        parts = caldav._check_url(base)           # https only (http for 127.0.0.1/localhost)
        if parts.username is not None or parts.password is not None:
            raise OriginError("base URL must not contain credentials")
        self.base, self.user, self.pw = base, user, password
        self._parts = parts
        hdrs = caldav.basic_headers(user, password or "") if user else (lambda: {})
        self._http = caldav._Http("provision", hdrs, opener, timeout)

    def _url(self, url: str) -> str:
        """Resolve ``url`` against the base and enforce the origin policy."""
        if not isinstance(url, str) or re.search(r"[\x00-\x1f\x7f]", url):
            raise OriginError("invalid URL")
        full = urllib.parse.urljoin(self.base, url.strip())
        try:
            parts = caldav._check_url(full)
            ok = _origin_ok(self._parts, parts)
        except ValueError:
            ok = False
        if not ok:
            raise OriginError("refusing URL outside the configured CalDAV origin")
        return full

    def req(self, method: str, url: str, body: bytes | None = None, headers: dict | None = None):
        """-> (status, body, headers) for every non-redirect status; transport failures raise StoreError."""
        full = self._url(url)
        h = {"Content-Type": _XML_CT, **(headers or {})}
        st, rh, data = self._http.send(method, full, body, h, expect=_ANY_STATUS)
        return st, data, rh

    def _parse(self, data: bytes):
        if b"<!doctype" in data.lower():
            raise self._http.err("response contains DOCTYPE, refusing", transient=False)
        return self._http.parse_multistatus(data)    # also refuses ENTITY, malformed XML, wrong root

    def propfind(self, url: str, props: str, depth: str = "0"):
        body = (f'<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
                f'xmlns:a="http://apple.com/ns/ical/"><d:prop>{props}</d:prop></d:propfind>').encode()
        st, data, _ = self.req("PROPFIND", url, body, {"Depth": depth})
        if st not in (200, 207):
            raise RuntimeError(f"PROPFIND -> {st}")
        return self._parse(data)

    def calendar_home(self) -> str:
        t = self.propfind("", "<d:current-user-principal/>")
        h = t.find(".//d:current-user-principal/d:href", NS)
        principal = self._url((h.text or "").strip() if h is not None else "")
        t = self.propfind(principal, "<c:calendar-home-set/>")
        home = t.find(".//c:calendar-home-set/d:href", NS)
        if home is None or not (home.text or "").strip():
            raise RuntimeError("no calendar-home-set")
        return self._url(urllib.parse.urljoin(principal, home.text.strip()))

    def list_calendars(self, home: str) -> dict[str, str]:
        """display name -> absolute href"""
        home = self._url(home)
        t = self.propfind(home, "<d:displayname/><d:resourcetype/>", "1")
        out = {}
        for r in t.findall("d:response", NS):
            if r.find(".//c:calendar", NS) is None:
                continue
            href = r.find("d:href", NS)
            if href is None or not (href.text or "").strip():
                continue
            n = r.find(".//d:displayname", NS)
            out[(n.text or "") if n is not None else ""] = self._url(urllib.parse.urljoin(home, href.text.strip()))
        return out

    def mkcalendar(self, home: str, display: str) -> str:
        _clean_text(display, "calendar name")
        url = self._url(urllib.parse.urljoin(home, str(uuid.uuid4()).upper() + "/"))
        body = (
            '<?xml version="1.0" encoding="utf-8"?><c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:set><d:prop>'
            f"<d:displayname>{_xml_text(display)}</d:displayname>"
            '<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>'
            "</d:prop></d:set></c:mkcalendar>"
        ).encode()
        st, _, _ = self.req("MKCALENDAR", url, body)
        if st != 201:
            raise RuntimeError(f"MKCALENDAR -> {st}")
        return url


def _dav(s: Settings) -> DAV:
    return DAV(s.icloud_url, env_secret("ICLOUD_USER"), env_secret("ICLOUD_APP_PASSWORD"))


def create_icloud_calendar(s: Settings, display: str, *, execute: bool, dav: DAV | None = None) -> dict:
    _clean_text(display, "calendar name")
    dav = dav or _dav(s)
    home = dav.calendar_home()
    existing = dav.list_calendars(home)
    if display in existing:
        return {"action": "exists", "url": existing[display]}
    if not execute:
        return {"action": "would_create", "display": display, "home": home}
    require_gates(s)
    return {"action": "created", "url": dav.mkcalendar(home, display)}


def copy_items(s: Settings, src_dir: Path, uids: list[str], target_url: str, *, execute: bool,
               dav: DAV | None = None) -> dict:
    """PUT selected UIDs' resources to target with If-None-Match:* (never overwrites)."""
    dav = dav or _dav(s)
    target_url = dav._url(target_url)      # refuse a foreign origin before anything is sent (even for dry-run)
    if not target_url.endswith("/"):
        target_url += "/"
    want = set(uids)
    todo = []
    for p in sorted(Path(src_dir).glob("*.ics")):
        evs = ics.parse(p.read_text(errors="replace"))
        if evs and evs[0].uid in want:
            todo.append((p, evs[0].uid))
    plan = {"count": len(todo), "uids": [u for _, u in todo], "missing": sorted(want - {u for _, u in todo})}
    if not execute:
        return {"action": "dry_run", **plan}
    require_gates(s)
    if plan["missing"]:
        raise RuntimeError(f"UIDs not found in source: {plan['missing']}")
    done, skipped = [], []
    for p, uid in todo:
        st, _, _ = dav.req("PUT", urllib.parse.urljoin(target_url, uuid.uuid4().hex.upper() + ".ics"), p.read_bytes(),
                           {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
        (done if st in (201, 204) else skipped).append((uid, st))
    return {"action": "copied", "done": len(done), "failed": skipped}


# ---- Google --------------------------------------------------------------

def resolve_timezone(tz: str | None = None) -> str:
    """Explicit argument, else CALSYNC_TIMEZONE, else TZ, else UTC. Validated against the tz database."""
    name = (tz or os.environ.get("CALSYNC_TIMEZONE") or os.environ.get("TZ") or "UTC").strip().lstrip(":")
    if name != "UTC":
        try:
            ZoneInfo(name)
        except Exception:
            raise ValueError(f"unknown time zone {name!r}") from None
    return name


class _GoogleClient:
    """Google Calendar REST over the core transport: token via GoogleTokenSource (shared token file),
    https-or-loopback base only, no redirects off-origin, bounded responses."""

    def __init__(self, s: Settings):
        base = (os.environ.get("GOOGLE_API_BASE") or DEFAULT_GOOGLE_API).rstrip("/")
        parts = caldav._check_url(base)
        if parts.username is not None or parts.password is not None:
            raise OriginError("GOOGLE_API_BASE must not contain credentials")
        cid, secret = env_secret("GOOGLE_CLIENT_ID"), env_secret("GOOGLE_CLIENT_SECRET")
        if not cid or not secret:
            raise RuntimeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required")
        self.base = base
        self._http = caldav._Http("google", GoogleTokenSource(s.tokens / "google.json", cid, secret).headers, None, 30)

    def call(self, method: str, path: str, body: dict | None = None):
        if not path.startswith("/"):
            raise ValueError("path must start with /")
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Accept": "application/json"}
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        _, _, raw = self._http.send(method, self.base + path, data, hdrs, expect=(200,))
        if len(raw) > _MAX_JSON:
            raise StoreError("google: response too large", transient=False)
        try:
            out = json.loads(raw)
        except ValueError:
            raise StoreError("google: malformed JSON response", transient=True) from None
        if not isinstance(out, dict):
            raise StoreError("google: unexpected JSON structure", transient=False)
        return out


def _g(s: Settings, method: str, path: str, body: dict | None = None, *, client: _GoogleClient | None = None):
    return (client or _GoogleClient(s)).call(method, path, body)


def _google_calendar_list(s: Settings, client: _GoogleClient) -> list[dict]:
    items: list[dict] = []
    path = "/users/me/calendarList"
    for _ in range(_MAX_PAGES):
        page = _g(s, "GET", path, client=client)
        items += [i for i in page.get("items", []) if isinstance(i, dict)]
        tok = page.get("nextPageToken")
        if not tok:
            return items
        path = "/users/me/calendarList?pageToken=" + urllib.parse.quote(str(tok), safe="")
    raise StoreError("google: calendarList pagination did not terminate", transient=False)


def create_google_calendar(s: Settings, summary: str, tz: str | None = None, *, execute: bool) -> dict:
    _clean_text(summary, "calendar name")
    tz = resolve_timezone(tz)
    client = _GoogleClient(s)
    for it in _google_calendar_list(s, client):
        if it.get("summary") == summary:
            return {"action": "exists", "id": it["id"]}
    if not execute:
        return {"action": "would_create", "summary": summary}
    require_gates(s)
    c = _g(s, "POST", "/calendars", {"summary": summary, "timeZone": tz}, client=client)
    return {"action": "created", "id": c["id"]}
