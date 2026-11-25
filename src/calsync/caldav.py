"""CalDAV implementation of ``store.CalendarStore`` (stdlib urllib only).

Design rules:
  * list_items() is all-or-nothing (PROPFIND listing must be fully covered by multiget).
  * writes are conditional (If-None-Match: * / If-Match: etag) and re-read afterwards.
  * credentials come from ``headers_fn`` per request and never reach exception messages.
  * redirects are handled manually: only same scheme+host+port, never to another origin.
"""
from __future__ import annotations

import base64
import email.utils
import hashlib
import http.client
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable

from .store import AuthError, Item, PreconditionFailed, RateLimited, StoreError

D = "{DAV:}"
C = "{urn:ietf:params:xml:ns:caldav}"
BATCH = 50
MAX_BODY = 64 << 20
MAX_REDIRECTS = 3
_LOOPBACK = {"127.0.0.1", "localhost"}
_XML_CT = "application/xml; charset=utf-8"
_ICS_CT = "text/calendar; charset=utf-8"
_NS_DECL = 'xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"'


# ------------------------------------------------------------------ helpers
def basic_headers(user: str, password: str) -> Callable[[], dict]:
    tok = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return lambda: {"Authorization": "Basic " + tok}


def google_collection_url(calendar_id: str) -> str:
    base = os.environ.get("GOOGLE_CALDAV_BASE") or "https://apidata.googleusercontent.com/caldav/v2"
    return f"{base.rstrip('/')}/{urllib.parse.quote(calendar_id, safe='')}/events/"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _build_opener():
    return urllib.request.build_opener(_NoRedirect)


def _origin(parts: urllib.parse.SplitResult) -> tuple:
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return (parts.scheme, (parts.hostname or "").lower(), port)


def _check_url(url: str) -> urllib.parse.SplitResult:
    p = urllib.parse.urlsplit(url)
    if not (p.scheme == "https" or (p.scheme == "http" and p.hostname in _LOOPBACK)) or not p.hostname:
        raise ValueError("CalDAV URL must be https (http only for 127.0.0.1/localhost)")
    return p


def _same_site(a: urllib.parse.SplitResult, b: urllib.parse.SplitResult) -> bool:
    if a.scheme != b.scheme or not a.hostname or not b.hostname:
        return False
    ha, hb = a.hostname.lower(), b.hostname.lower()
    if ha == hb:
        return True
    if ha in _LOOPBACK or hb in _LOOPBACK or re.fullmatch(r"[\d.:]+", ha) or re.fullmatch(r"[\d.:]+", hb):
        return False
    return ha.split(".")[-2:] == hb.split(".")[-2:]


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _retry_after(v: str | None) -> float | None:
    if not v:
        return None
    v = v.strip()
    if re.fullmatch(r"\d+(\.\d+)?", v):
        return float(v)
    try:
        dt = email.utils.parsedate_to_datetime(v)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


_CDATA_RE = re.compile(rb"<!\[CDATA\[(.*?)\]\]>", re.S)
_TEXT_RE = re.compile(rb"(?<=>)[^<]+(?=<)")


def _preserve_cr(raw: bytes) -> bytes:
    """XML parsers normalise literal CRLF to LF. Escape CR in text nodes so calendar-data
    keeps its exact line endings regardless of whether the server escaped them."""
    if b"\r" not in raw:
        return raw
    raw = _CDATA_RE.sub(lambda m: m.group(1).replace(b"&", b"&amp;").replace(b"<", b"&lt;")
                        .replace(b">", b"&gt;"), raw)
    return _TEXT_RE.sub(lambda m: m.group(0).replace(b"\r", b"&#13;"), raw)


def _status_code(el) -> int | None:
    if el is None or not el.text:
        return None
    m = re.match(r"\s*HTTP/\d(?:\.\d)?\s+(\d{3})", el.text)
    return int(m.group(1)) if m else None


def _ok_props(resp) -> dict:
    props = {}
    for ps in resp.findall(D + "propstat"):
        if _status_code(ps.find(D + "status")) != 200:
            continue
        prop = ps.find(D + "prop")
        if prop is not None:
            for ch in prop:
                props[ch.tag] = ch
    return props


class _Http:
    """Single-request transport: auth headers per request, error mapping, manual redirects."""

    def __init__(self, label: str, headers_fn: Callable[[], dict], opener, timeout: float):
        self.label = label
        self._headers_fn = headers_fn
        self._opener = opener or _build_opener()
        self._timeout = timeout
        self._secrets: set[str] = set()

    # -- redaction
    def _note_secrets(self, auth: dict) -> None:
        for v in auth.values():
            if not isinstance(v, str):
                continue
            self._secrets.add(v)
            if " " in v:
                tail = v.split(" ", 1)[1]
                self._secrets.add(tail)
                if v.lower().startswith("basic "):
                    try:
                        dec = base64.b64decode(tail).decode("utf-8", "replace")
                        self._secrets.add(dec)
                        if ":" in dec:
                            self._secrets.add(dec.split(":", 1)[1])
                    except Exception:
                        pass
        self._secrets.discard("")

    def scrub(self, text: str) -> str:
        for s in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(s, "[redacted]")
        text = re.sub(r"(?i)\b(proxy-)?authorization\b[^\n]*", "[redacted]", text)
        text = re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]+", "[redacted]", text)
        text = re.sub(r"ya29\.[\w.\-]+", "[redacted]", text)
        text = re.sub(r"(?i)(access_token|refresh_token|client_secret)\W+[\w.\-/+=]+", "[redacted]", text)
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()[:200]

    def err(self, msg: str, **kw) -> StoreError:
        return StoreError(f"{self.label}: {self.scrub(msg)}", **kw)

    # -- transport
    @staticmethod
    def _read(resp) -> bytes:
        data = resp.read(MAX_BODY + 1)
        if len(data) > MAX_BODY:
            raise StoreError("response too large", transient=False)
        return data

    def send(self, method: str, url: str, body: bytes | None = None, headers: dict | None = None,
             expect: tuple = (200, 201, 204)):
        origin = _origin(urllib.parse.urlsplit(url))
        hops = 0
        while True:
            auth = self._headers_fn()
            self._note_secrets(auth)
            hdrs = {"User-Agent": "calsync/1", **(headers or {}), **auth}
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            try:
                try:
                    resp = self._opener.open(req, timeout=self._timeout)
                    try:
                        status, rh, data = resp.status, resp.headers, self._read(resp)
                    finally:
                        resp.close()
                except urllib.error.HTTPError as e:
                    status, rh = e.code, e.headers
                    try:
                        data = self._read(e)
                    except Exception:
                        data = b""
                    finally:
                        e.close()
            except StoreError:
                raise
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                reason = getattr(e, "reason", None) or ""
                raise self.err(f"{method} failed ({type(e).__name__}: {reason})", transient=True) from None
            if status in (301, 302, 303, 307, 308):
                loc = rh.get("Location")
                if not loc:
                    raise self.err(f"{method} redirect (HTTP {status}) without Location", transient=False,
                                   status=status)
                nxt = urllib.parse.urljoin(url, loc)
                try:
                    same = _origin(urllib.parse.urlsplit(nxt)) == origin
                except ValueError:
                    same = False
                if not same:
                    raise self.err(f"{method} refused redirect to a different origin (HTTP {status})",
                                   transient=False, status=status)
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise self.err(f"{method} too many redirects", transient=False, status=status)
                url = nxt
                if status == 303:
                    method, body, headers = "GET", None, None
                continue
            if status in expect:
                return status, rh, data
            self._raise_http(method, status, rh, data)

    def _raise_http(self, method: str, status: int, rh, data: bytes):
        snippet = self.scrub(data[:2000].decode("utf-8", "replace"))
        text = f"{method} -> HTTP {status}" + (f": {snippet}" if snippet else "")
        msg = f"{self.label}: {text}"
        if status == 401:
            raise AuthError(msg, status=status)
        if status == 403:
            if "ratelimitexceeded" in data.decode("utf-8", "replace").lower():
                raise RateLimited(msg, retry_after=_retry_after(rh.get("Retry-After")), status=403)
            raise AuthError(msg, status=status)
        if status == 412:
            raise PreconditionFailed(msg)
        if status in (429, 503):
            raise RateLimited(msg, retry_after=_retry_after(rh.get("Retry-After")), status=status)
        if status >= 500 or status == 408:
            raise StoreError(msg, transient=True, status=status)
        raise StoreError(msg, transient=False, status=status)

    # -- xml
    def parse_multistatus(self, data: bytes):
        head = data[:4096].lower()
        if b"<!doctype" in head or b"<!entity" in data.lower():
            raise self.err("multistatus contains DOCTYPE/ENTITY, refusing", transient=False)
        try:
            # DOCTYPE/ENTITY were refused above and the body is size-bounded; the stdlib parser never fetches
            # external entities, so entity-expansion and XXE vectors are closed here.
            root = ET.fromstring(_preserve_cr(data))  # nosec B314
        except ET.ParseError:
            raise self.err("malformed XML in multistatus response", transient=True) from None
        if root.tag != D + "multistatus":
            raise self.err("unexpected XML root in response", transient=True)
        return root


# ------------------------------------------------------------------ store
class CalDAVStore:
    def __init__(self, name: str, collection_url: str, headers_fn: Callable[[], dict], *,
                 read_only: bool = False, opener=None, timeout: float = 30):
        if not collection_url.endswith("/"):
            collection_url += "/"
        parts = _check_url(collection_url)
        self.name = name
        self.collection_url = collection_url
        self.read_only = read_only
        self._parts = parts
        self._base_path = urllib.parse.unquote(parts.path)
        self._http = _Http(name, headers_fn, opener, timeout)

    # -- path handling
    def _paths(self, href_text: str) -> tuple[str, str]:
        """(raw absolute path, decoded absolute path) of an href found in a response."""
        p = urllib.parse.urlsplit(urllib.parse.urljoin(self.collection_url, href_text.strip()))
        return p.path, urllib.parse.unquote(p.path)

    def _url_for_href(self, href: str) -> str:
        if not isinstance(href, str) or re.search(r"[\x00-\x1f\x7f]", href):
            raise ValueError("invalid href")
        rel = href[len(self._base_path):] if href.startswith(self._base_path) else None
        if not rel or "/" in rel or rel in (".", ".."):
            raise ValueError("href is not a resource of this collection")
        return f"{self._parts.scheme}://{self._parts.netloc}{urllib.parse.quote(href, safe='/')}"

    @staticmethod
    def _check_etag(etag: str) -> None:
        if not isinstance(etag, str) or not etag or re.search(r"[\x00-\x1f\x7f]", etag):
            raise ValueError("invalid etag")

    # -- listing
    def list_items(self) -> list[Item]:
        h = self._http
        body = (f'<?xml version="1.0" encoding="utf-8"?><d:propfind {_NS_DECL}><d:prop>'
                "<d:getetag/><d:resourcetype/><d:getcontenttype/></d:prop></d:propfind>").encode()
        _, _, data = h.send("PROPFIND", self.collection_url, body,
                            {"Depth": "1", "Content-Type": _XML_CT}, expect=(207,))
        root = h.parse_multistatus(data)
        listing: dict[str, str] = {}      # decoded -> raw path
        for resp in root.findall(D + "response"):
            href_el = resp.find(D + "href")
            if href_el is None or not (href_el.text or "").strip():
                raise h.err("PROPFIND response without href", transient=True)
            raw, dec = self._paths(href_el.text)
            if dec.rstrip("/") == self._base_path.rstrip("/"):
                continue
            props = _ok_props(resp)
            rt = props.get(D + "resourcetype")
            if rt is not None and rt.find(D + "collection") is not None:
                continue
            if not dec.startswith(self._base_path) or "/" in dec[len(self._base_path):]:
                raise h.err("PROPFIND returned a resource outside the collection", transient=False)
            ct = props.get(D + "getcontenttype")
            ctt = (ct.text or "").strip().lower() if ct is not None else ""
            if ctt:
                if not ctt.startswith("text/calendar"):
                    continue
            elif not dec.lower().endswith(".ics"):
                continue
            if dec in listing:
                raise h.err("PROPFIND returned duplicate hrefs", transient=True)
            listing[dec] = raw
        items: list[Item] = []
        raws = list(listing.values())
        for i in range(0, len(raws), BATCH):
            items.extend(self._multiget(raws[i:i + BATCH]))
        if len(items) != len(listing) or {it.href for it in items} != set(listing):
            raise h.err(f"multiget returned {len(items)} of {len(listing)} listed resources", transient=True)
        return sorted(items, key=lambda it: it.href)

    def _multiget(self, raws: list[str]) -> list[Item]:
        h = self._http
        want = {urllib.parse.unquote(r): r for r in raws}
        body = (f'<?xml version="1.0" encoding="utf-8"?><c:calendar-multiget {_NS_DECL}><d:prop>'
                "<d:getetag/><c:calendar-data/></d:prop>"
                + "".join(f"<d:href>{_esc(r)}</d:href>" for r in raws)
                + "</c:calendar-multiget>").encode()
        _, _, data = h.send("REPORT", self.collection_url, body,
                            {"Depth": "1", "Content-Type": _XML_CT}, expect=(207,))
        root = h.parse_multistatus(data)
        out: dict[str, Item] = {}
        for resp in root.findall(D + "response"):
            href_el = resp.find(D + "href")
            if href_el is None or not (href_el.text or "").strip():
                raise h.err("multiget response without href", transient=True)
            _, dec = self._paths(href_el.text)
            if dec not in want:
                raise h.err("multiget returned an unrequested resource", transient=True)
            if dec in out:
                raise h.err("multiget returned duplicate hrefs", transient=True)
            props = _ok_props(resp)
            etag, cal = props.get(D + "getetag"), props.get(C + "calendar-data")
            if etag is None or not (etag.text or "").strip() or cal is None or not cal.text \
                    or "BEGIN:VCALENDAR" not in cal.text:
                raise h.err("multiget returned a resource without 200 etag/calendar-data", transient=True)
            out[dec] = Item(href=dec, etag=etag.text, data=cal.text.encode("utf-8"))
        if set(out) != set(want):
            raise h.err(f"multiget returned {len(out)} of {len(want)} requested resources", transient=True)
        return list(out.values())

    # -- writes
    def _guard_write(self) -> None:
        if self.read_only:
            raise RuntimeError(f"store {self.name!r} is read-only")

    @staticmethod
    def _first_uid(data: bytes) -> str:
        """UID of the VEVENT (not of a VALARM/VTIMEZONE/other nested component)."""
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("data must be bytes")
        from . import icsmodel
        uid = icsmodel.parse_resource(bytes(data)).uid
        if not uid:
            raise ValueError("calendar data has no (single) event UID")
        return uid

    def create(self, data: bytes) -> Item:
        self._guard_write()
        uid = self._first_uid(data)
        # SHA-1 only derives a stable, filesystem-safe resource name from the UID (an identifier, not a
        # security control). Changing it would re-name every created resource, so it is kept as is.
        fname = "calsync-" + hashlib.sha1(uid.encode("utf-8"), usedforsecurity=False).hexdigest()[:24] + ".ics"
        url = self.collection_url + fname
        _, ph, _ = self._http.send("PUT", url, bytes(data), {
            "Content-Type": _ICS_CT, "If-None-Match": "*"}, expect=(200, 201, 204))
        return self._reread(url, ph, uid)

    def update(self, href: str, etag: str, data: bytes) -> Item:
        self._guard_write()
        self._check_etag(etag)
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("data must be bytes")
        url = self._url_for_href(href)
        _, ph, _ = self._http.send("PUT", url, bytes(data), {
            "Content-Type": _ICS_CT, "If-Match": etag}, expect=(200, 201, 204))
        try:
            uid = self._first_uid(bytes(data))
        except ValueError:
            uid = ""
        return self._reread(url, ph, uid)

    def delete(self, href: str, etag: str) -> None:
        self._guard_write()
        self._check_etag(etag)
        url = self._url_for_href(href)
        self._http.send("DELETE", url, None, {"If-Match": etag}, expect=(200, 202, 204))

    def _reread(self, url: str, put_headers, uid: str = "") -> Item:
        """Read back what the server stored. Some servers (Google) may canonicalise the resource
        name of a PUT; if the resource is not found at the URL we sent, locate it by UID in a fresh
        listing instead of failing (the write already happened)."""
        try:
            _, gh, data = self._http.send("GET", url, expect=(200,))
            etag = gh.get("ETag") or put_headers.get("ETag")
            if etag:
                return Item(href=urllib.parse.unquote(urllib.parse.urlsplit(url).path), etag=etag, data=data)
        except StoreError as e:
            if e.status not in (404, 410) or not uid:
                raise
        if uid:
            hits = [it for it in self.list_items() if self._first_uid(it.data) == uid]
            if len(hits) == 1:
                return hits[0]
            raise self._http.err("written resource cannot be located by UID after PUT "
                                 f"({len(hits)} matches)", transient=False)
        raise self._http.err("server returned no ETag for written resource", transient=False)


# ------------------------------------------------------------------ iCloud discovery
def discover_icloud_collection(base_url: str, user: str, password: str, collection_id: str,
                               opener=None) -> str:
    """principal -> calendar-home-set -> calendar list; return the absolute collection URL
    whose last path segment equals ``collection_id``."""
    if not collection_id:
        raise ValueError("collection_id required")
    base = _check_url(base_url)
    h = _Http("icloud-discovery", basic_headers(user, password), opener, 30)

    def propfind(url: str, props: str, depth: str):
        body = (f'<?xml version="1.0" encoding="utf-8"?><d:propfind {_NS_DECL}><d:prop>{props}</d:prop>'
                "</d:propfind>").encode()
        _, _, data = h.send("PROPFIND", url, body, {"Depth": depth, "Content-Type": _XML_CT}, expect=(207,))
        return h.parse_multistatus(data)

    def follow(cur: str, href: str) -> str:
        nxt = urllib.parse.urljoin(cur, href.strip())
        p = _check_url(nxt)
        if not _same_site(base, p):
            raise h.err("discovery pointed to a different site, refusing", transient=False)
        return nxt

    root = propfind(base_url, "<d:current-user-principal/>", "0")
    el = root.find(f".//{D}current-user-principal/{D}href")
    principal = follow(base_url, el.text) if el is not None and (el.text or "").strip() else base_url
    root = propfind(principal, "<c:calendar-home-set/>", "0")
    el = root.find(f".//{C}calendar-home-set/{D}href")
    if el is None or not (el.text or "").strip():
        raise h.err("no calendar-home-set found", transient=False)
    home = follow(principal, el.text)
    if not home.endswith("/"):
        home += "/"
    root = propfind(home, "<d:resourcetype/><d:displayname/>", "1")
    found: dict[str, str] = {}
    seen: list[str] = []
    for resp in root.findall(D + "response"):
        href_el = resp.find(D + "href")
        if href_el is None or not (href_el.text or "").strip():
            continue
        rt = _ok_props(resp).get(D + "resourcetype")
        if rt is None or rt.find(C + "calendar") is None:
            continue
        url = follow(home, href_el.text)
        dec = urllib.parse.unquote(urllib.parse.urlsplit(url).path).rstrip("/")
        last = dec.rsplit("/", 1)[-1]
        seen.append(last)
        if last == collection_id:
            found[dec] = url if url.endswith("/") else url + "/"
    if not found:
        raise h.err(f"calendar {collection_id!r} not found among {len(seen)} calendars", transient=False)
    if len(found) > 1:
        raise h.err(f"calendar {collection_id!r} is ambiguous ({len(found)} matches)", transient=False)
    return next(iter(found.values()))
