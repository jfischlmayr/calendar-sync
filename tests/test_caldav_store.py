"""CalDAVStore tests: real local Radicale (roundtrip, byte-exactness, batching) and an
in-process stub server for failure modes. Env: RADICALE_BIN."""
import os, shutil, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from calsync import caldav
from calsync.caldav import CalDAVStore, basic_headers, discover_icloud_collection, google_collection_url
from calsync.store import AuthError, PreconditionFailed, RateLimited, StoreError

RAD = os.environ.get("RADICALE_BIN") or shutil.which("radicale")

EV = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\nUID:{uid}\r\n"
      "DTSTAMP:20260101T000000Z\r\nDTSTART:20260301T100000Z\r\nDTEND:20260301T110000Z\r\n"
      "SUMMARY:{title}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

COMPLEX = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
    "BEGIN:VTIMEZONE\r\nTZID:Europe/Vienna\r\n"
    "BEGIN:STANDARD\r\nDTSTART:19701025T030000\r\nRRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\n"
    "TZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nTZNAME:CET\r\nEND:STANDARD\r\n"
    "BEGIN:DAYLIGHT\r\nDTSTART:19700329T020000\r\nRRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\r\n"
    "TZOFFSETFROM:+0100\r\nTZOFFSETTO:+0200\r\nTZNAME:CEST\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n"
    "BEGIN:VEVENT\r\nUID:complex-1@example\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART;TZID=Europe/Vienna:20260302T090000\r\nDTEND;TZID=Europe/Vienna:20260302T100000\r\n"
    "RRULE:FREQ=WEEKLY;COUNT=6\r\nEXDATE;TZID=Europe/Vienna:20260309T090000\r\n"
    "SUMMARY:Grüße aus Wien äöü €\r\n"
    "DESCRIPTION:Zeile 1\\nZeile 2 mit langem Text der gefaltet werden muss weil er \r\n"
    " über 75 Oktette lang ist ÄÖÜ\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:complex-1@example\r\nDTSTAMP:20260101T000000Z\r\n"
    "RECURRENCE-ID;TZID=Europe/Vienna:20260316T090000\r\n"
    "DTSTART;TZID=Europe/Vienna:20260316T140000\r\nDTEND;TZID=Europe/Vienna:20260316T150000\r\n"
    "SUMMARY:Verschoben\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


class CountingOpener:
    """Wraps a real (no-redirect) opener and records every request."""

    def __init__(self):
        self.inner = caldav._build_opener()
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append((req.get_method(), req.full_url))
        return self.inner.open(req, timeout=timeout)


def http(method, url, data=None, headers=None):
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r) as x: return x.status
    except urllib.error.HTTPError as e: return e.code


@unittest.skipUnless(RAD, "radicale not available")
class RadicaleStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="calsync-cd-"))
        cls.port = free_port()
        (cls.tmp / "rad.conf").write_text(
            f"[server]\nhosts = 127.0.0.1:{cls.port}\n[auth]\ntype = none\n[storage]\nfilesystem_folder = {cls.tmp}/rad\n[logging]\nlevel = error\n")
        cls.proc = subprocess.Popen([RAD, "--config", str(cls.tmp / "rad.conf")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close(); break
            except OSError:
                time.sleep(0.1)
        cls.base = f"http://127.0.0.1:{cls.port}/u/"
        http("MKCOL", cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(); shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.url = f"{self.base}t{time.monotonic_ns()}/"
        self.assertIn(http("MKCALENDAR", self.url), (201, 200))
        self.opener = CountingOpener()
        self.store = CalDAVStore("rad", self.url, basic_headers("u", "pw"), opener=self.opener)

    def ev(self, uid, title="T"):
        return EV.format(uid=uid, title=title).encode()

    def test_empty_collection_lists_empty(self):
        self.assertEqual(self.store.list_items(), [])

    def test_roundtrip(self):
        it = self.store.create(self.ev("a1", "one"))
        self.assertTrue(it.etag)
        self.assertRegex(it.href.rsplit("/", 1)[1], r"^calsync-[0-9a-f]{24}\.ics$")
        self.assertTrue(it.href.startswith("/u/"))
        self.assertIn(b"SUMMARY:one", it.data)
        lst = self.store.list_items()
        self.assertEqual(lst, [it])                       # same href, etag and bytes as after create
        up = self.store.update(it.href, it.etag, self.ev("a1", "two"))
        self.assertNotEqual(up.etag, it.etag)
        self.assertIn(b"SUMMARY:two", up.data)
        self.assertEqual(self.store.list_items(), [up])
        self.store.delete(up.href, up.etag)
        self.assertEqual(self.store.list_items(), [])

    def test_deterministic_filename(self):
        import hashlib
        it = self.store.create(self.ev("same-uid"))
        self.assertTrue(it.href.endswith("calsync-" + hashlib.sha1(b"same-uid").hexdigest()[:24] + ".ics"))

    def test_double_create_precondition_failed(self):
        self.store.create(self.ev("dup", "first"))
        with self.assertRaises(PreconditionFailed):
            self.store.create(self.ev("dup", "second"))
        self.assertIn(b"SUMMARY:first", self.store.list_items()[0].data)

    def test_create_without_uid_value_error(self):
        with self.assertRaises(ValueError):
            self.store.create(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")

    def test_stale_etag_update_and_delete(self):
        it = self.store.create(self.ev("s1", "v1"))
        v2 = self.store.update(it.href, it.etag, self.ev("s1", "v2"))
        with self.assertRaises(PreconditionFailed):
            self.store.update(it.href, it.etag, self.ev("s1", "v3"))
        with self.assertRaises(PreconditionFailed):
            self.store.delete(it.href, it.etag)
        self.assertEqual(self.store.list_items(), [v2])

    def test_update_delete_missing_is_permanent_error(self):
        it = self.store.create(self.ev("m1"))
        self.store.delete(it.href, it.etag)
        for fn in (lambda: self.store.update(it.href, it.etag, self.ev("m1", "x")),
                   lambda: self.store.delete(it.href, it.etag)):
            with self.assertRaises(StoreError) as cm:
                fn()
            self.assertIn(cm.exception.status, (404, 412))

    def test_href_outside_collection_rejected(self):
        with self.assertRaises(ValueError):
            self.store.delete("/u/other/x.ics", '"e"')
        with self.assertRaises(ValueError):
            self.store.update(self.store._base_path + "a/b.ics", '"e"', self.ev("x"))

    def test_byte_exact_complex_event(self):
        data = COMPLEX.encode("utf-8")
        it = self.store.create(data)
        got = self.store.list_items()[0]
        self.assertEqual(got.data, it.data)               # list (XML) == GET: byte exact incl. CRLF
        self.assertEqual(got.etag, it.etag)
        d = got.data
        for needle in (b"BEGIN:VTIMEZONE", b"RRULE:FREQ=WEEKLY;COUNT=6", b"EXDATE;TZID=Europe/Vienna:20260309T090000",
                       b"RECURRENCE-ID;TZID=Europe/Vienna:20260316T090000", "Grüße".encode(), "€".encode()):
            self.assertIn(needle, d)
        self.assertIn(b"\r\n", d)
        self.assertNotRegex(d, rb"(?<!\r)\n")            # no bare LF introduced anywhere
        self.assertEqual(d.count(b"BEGIN:VEVENT"), 2)

    def test_listing_120_items_batched(self):
        for i in range(120):
            self.store.create(self.ev(f"bulk-{i}", f"s{i}"))
        self.opener.requests.clear()
        items = self.store.list_items()
        self.assertEqual(len(items), 120)
        self.assertEqual(len({i.href for i in items}), 120)
        self.assertEqual({i.data.split(b"SUMMARY:")[1].split(b"\r\n")[0] for i in items}, {f"s{i}".encode() for i in range(120)})
        methods = [m for m, _ in self.opener.requests]
        self.assertEqual(methods.count("PROPFIND"), 1)
        self.assertEqual(methods.count("REPORT"), 3)      # 50 + 50 + 20

    def test_read_only_refuses_writes_without_network(self):
        it = self.store.create(self.ev("ro1"))
        ro = CalDAVStore("ro", self.url, basic_headers("u", "pw"), read_only=True, opener=(op := CountingOpener()))
        for fn in (lambda: ro.create(self.ev("ro2")), lambda: ro.update(it.href, it.etag, self.ev("ro1", "x")),
                   lambda: ro.delete(it.href, it.etag)):
            with self.assertRaises(RuntimeError):
                fn()
        self.assertEqual(op.requests, [])
        self.assertEqual(len(ro.list_items()), 1)          # reads still work
        self.assertEqual([m for m, _ in op.requests], ["PROPFIND", "REPORT"])

    def test_non_calendar_resources_skipped(self):
        self.store.create(self.ev("k1"))
        self.assertIn(http("PUT", self.url + "note.txt", b"hi", {"Content-Type": "text/plain"}), (201, 204, 403, 400, 415))
        self.assertEqual(len(self.store.list_items()), 1)


# ---------------------------------------------------------------------------
class Stub:
    """Scriptable in-process HTTP server. ``script(method, path, headers, body) -> (status, headers, bytes)``."""

    def __init__(self, script):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def handle_any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                st, hd, out = script(self.command, self.path, dict(self.headers), body)
                self.send_response(st)
                for k, v in hd.items(): self.send_header(k, v)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                if self.command != "HEAD": self.wfile.write(out)
            do_GET = do_PUT = do_DELETE = do_PROPFIND = do_REPORT = do_POST = handle_any
            def log_message(self, *a): pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True).start()

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


NS = 'xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"'
TOKEN = "SUPER-SECRET-BEARER-TOKEN-123"


def ms(*responses):
    return ('<?xml version="1.0"?><d:multistatus ' + NS + ">" + "".join(responses) + "</d:multistatus>").encode()


def propfind_resp(href, etag='"e"', ct="text/calendar; charset=utf-8", collection=False):
    rt = "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>" if collection else "<d:resourcetype/>"
    cts = f"<d:getcontenttype>{ct}</d:getcontenttype>" if ct else ""
    return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:getetag>{etag}</d:getetag>{rt}{cts}"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")


def multiget_resp(href, etag='"e"', data=None, status="200 OK"):
    data = data if data is not None else EV.format(uid=href.rsplit("/", 1)[-1], title="x")
    return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:getetag>{etag}</d:getetag>"
            f"<c:calendar-data>{data}</c:calendar-data></d:prop><d:status>HTTP/1.1 {status}</d:status></d:propstat></d:response>")


XML = {"Content-Type": "application/xml"}


class StubStore(unittest.TestCase):
    def serve(self, script):
        s = Stub(script); self.addCleanup(s.close); self.stub = s
        self.url = f"{s.base}/cal/u/"
        self.store = CalDAVStore("stub", self.url, lambda: {"Authorization": "Bearer " + TOKEN})
        return s

    def assert_clean(self, exc):
        m = str(exc)
        self.assertNotIn(TOKEN, m); self.assertNotIn("Authorization", m)
        self.assertLessEqual(len(m), 400)

    def test_happy_path_literal_crlf_preserved(self):
        data = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:a\r\nSUMMARY:ä &amp; b\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"

        def script(m, p, h, b):
            if m == "PROPFIND":
                return 207, XML, ms(propfind_resp("/cal/u/", collection=True), propfind_resp("/cal/u/a.ics"),
                                    propfind_resp("/cal/u/sub/", collection=True), propfind_resp("/cal/u/x.vcf", ct="text/vcard"))
            return 207, XML, ms(multiget_resp("/cal/u/a.ics", data=data))
        self.serve(script)
        (it,) = self.store.list_items()
        self.assertEqual(it.href, "/cal/u/a.ics"); self.assertEqual(it.etag, '"e"')
        self.assertEqual(it.data, data.replace("&amp;", "&").encode("utf-8"))
        self.assertIn(b"\r\n", it.data)

    def test_href_decoding_and_auth_header_sent(self):
        def script(m, p, h, b):
            if m == "PROPFIND":
                return 207, XML, ms(propfind_resp("/cal/u/"), propfind_resp("/cal/u/a%20b%40c.ics"))
            return 207, XML, ms(multiget_resp("/cal/u/a%20b%40c.ics"))
        s = self.serve(script)
        (it,) = self.store.list_items()
        self.assertEqual(it.href, "/cal/u/a b@c.ics")
        self.assertIn("a%20b%40c.ics", s.requests[1][3].decode())          # raw href reused in multiget
        self.assertEqual(s.requests[0][2]["Authorization"], "Bearer " + TOKEN)

    def test_status_mapping(self):
        cases = [
            (401, {}, b"Authorization: Bearer " + TOKEN.encode(), AuthError, False),
            (403, {}, b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}', RateLimited, True),
            (403, {}, b'{"reason":"userRateLimitExceeded"}', RateLimited, True),
            (403, {}, b"forbidden", AuthError, False),
            (429, {"Retry-After": "7"}, b"slow down", RateLimited, True),
            (503, {"Retry-After": "11"}, b"", RateLimited, True),
            (500, {}, b"boom", StoreError, True),
            (502, {}, b"bad gw", StoreError, True),
            (404, {}, b"nope", StoreError, False),
            (400, {}, b"bad", StoreError, False),
        ]
        for st, hd, body, cls, transient in cases:
            with self.subTest(status=st, body=body[:20]):
                self.serve(lambda m, p, h, b, st=st, hd=hd, body=body: (st, hd, body))
                with self.assertRaises(StoreError) as cm:
                    self.store.list_items()
                e = cm.exception
                self.assertIs(type(e), cls)
                self.assertEqual(e.transient, transient)
                self.assertEqual(e.status, st)
                self.assert_clean(e)
                if st == 429: self.assertEqual(e.retry_after, 7.0)
                if st == 503: self.assertEqual(e.retry_after, 11.0)

    def test_retry_after_http_date_and_garbage(self):
        self.serve(lambda m, p, h, b: (429, {"Retry-After": "garbage"}, b""))
        with self.assertRaises(RateLimited) as cm: self.store.list_items()
        self.assertIsNone(cm.exception.retry_after)

    def test_long_error_body_truncated(self):
        self.serve(lambda m, p, h, b: (500, {}, b"A" * 5000))
        with self.assertRaises(StoreError) as cm: self.store.list_items()
        self.assertLessEqual(len(str(cm.exception)), 300)

    def test_truncated_multistatus_fewer_responses(self):
        def script(m, p, h, b):
            if m == "PROPFIND":
                return 207, XML, ms(propfind_resp("/cal/u/"), *[propfind_resp(f"/cal/u/{i}.ics") for i in range(3)])
            return 207, XML, ms(*[multiget_resp(f"/cal/u/{i}.ics") for i in range(2)])
        self.serve(script)
        with self.assertRaises(StoreError) as cm: self.store.list_items()
        self.assertIn("2 of 3", str(cm.exception))

    def test_multiget_missing_propstat_200(self):
        def script(m, p, h, b):
            if m == "PROPFIND":
                return 207, XML, ms(propfind_resp("/cal/u/"), propfind_resp("/cal/u/0.ics"))
            return 207, XML, ms("<d:response><d:href>/cal/u/0.ics</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response>")
        self.serve(script)
        with self.assertRaises(StoreError): self.store.list_items()

    def test_multiget_propstat_404_for_data(self):
        def script(m, p, h, b):
            if m == "PROPFIND":
                return 207, XML, ms(propfind_resp("/cal/u/"), propfind_resp("/cal/u/0.ics"))
            return 207, XML, ms("<d:response><d:href>/cal/u/0.ics</d:href><d:propstat><d:prop><c:calendar-data/></d:prop>"
                                "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>")
        self.serve(script)
        with self.assertRaises(StoreError): self.store.list_items()

    def test_multiget_unrequested_or_duplicate_href(self):
        for resp in ([multiget_resp("/cal/u/0.ics"), multiget_resp("/cal/u/0.ics")],
                     [multiget_resp("/cal/u/0.ics"), multiget_resp("/cal/u/other.ics")]):
            def script(m, p, h, b, resp=resp):
                if m == "PROPFIND":
                    return 207, XML, ms(propfind_resp("/cal/u/"), propfind_resp("/cal/u/0.ics"), propfind_resp("/cal/u/1.ics"))
                return 207, XML, ms(*resp)
            self.serve(script)
            with self.assertRaises(StoreError): self.store.list_items()

    def test_duplicate_href_in_propfind(self):
        self.serve(lambda m, p, h, b: (207, XML, ms(propfind_resp("/cal/u/"), propfind_resp("/cal/u/0.ics"), propfind_resp("/cal/u/0.ics"))))
        with self.assertRaises(StoreError): self.store.list_items()

    def test_malformed_and_truncated_xml(self):
        good = ms(propfind_resp("/cal/u/"), propfind_resp("/cal/u/0.ics"))
        for body in (b"<not xml", good[:len(good) // 2], b"<html>login</html>", b""):
            with self.subTest(body=body[:15]):
                self.serve(lambda m, p, h, b, body=body: (207, XML, body))
                with self.assertRaises(StoreError): self.store.list_items()

    def test_malformed_multiget_xml(self):
        def script(m, p, h, b):
            if m == "PROPFIND":
                return 207, XML, ms(propfind_resp("/cal/u/"), propfind_resp("/cal/u/0.ics"))
            return 207, XML, b"<d:multistatus xmlns:d='DAV:'><d:response>"
        self.serve(script)
        with self.assertRaises(StoreError): self.store.list_items()

    def test_doctype_entity_refused(self):
        self.serve(lambda m, p, h, b: (207, XML, b'<!DOCTYPE x [<!ENTITY a "b">]>' + ms()))
        with self.assertRaises(StoreError): self.store.list_items()

    def test_propfind_non_207_is_error(self):
        self.serve(lambda m, p, h, b: (200, XML, ms()))
        with self.assertRaises(StoreError): self.store.list_items()

    def test_redirect_to_other_host_not_followed_no_auth_leak(self):
        other = Stub(lambda m, p, h, b: (200, {}, b"pwned")); self.addCleanup(other.close)
        self.serve(lambda m, p, h, b: (302, {"Location": f"http://localhost:{other.port}/steal"}, b""))
        for fn in (self.store.list_items, lambda: self.store.create(EV.format(uid="u", title="t").encode()),
                   lambda: self.store.update("/cal/u/a.ics", '"e"', b"x"), lambda: self.store.delete("/cal/u/a.ics", '"e"')):
            with self.assertRaises(StoreError) as cm: fn()
            self.assertFalse(cm.exception.transient)
            self.assertIn("redirect", str(cm.exception)); self.assert_clean(cm.exception)
        self.assertEqual(other.requests, [])

    def test_redirect_to_other_port_same_host_refused(self):
        other = Stub(lambda m, p, h, b: (200, {}, b"x")); self.addCleanup(other.close)
        self.serve(lambda m, p, h, b: (301, {"Location": f"http://127.0.0.1:{other.port}/x"}, b""))
        with self.assertRaises(StoreError): self.store.list_items()
        self.assertEqual(other.requests, [])

    def test_same_origin_redirect_followed(self):
        def script(m, p, h, b):
            if p.startswith("/cal/u/") and m == "PROPFIND":
                return 301, {"Location": "/cal/moved/"}, b""
            if m == "PROPFIND":
                return 207, XML, ms()
            return 500, {}, b""
        s = self.serve(script)
        self.assertEqual(self.store.list_items(), [])
        self.assertEqual([r[1] for r in s.requests], ["/cal/u/", "/cal/moved/"])

    def test_redirect_loop_bounded(self):
        s = self.serve(lambda m, p, h, b: (302, {"Location": "/cal/u/"}, b""))
        with self.assertRaises(StoreError): self.store.list_items()
        self.assertLessEqual(len(s.requests), 5)

    def test_connection_refused_transient(self):
        port = free_port()
        st = CalDAVStore("x", f"http://127.0.0.1:{port}/c/", lambda: {"Authorization": "Bearer " + TOKEN})
        with self.assertRaises(StoreError) as cm: st.list_items()
        self.assertTrue(cm.exception.transient)
        self.assertNotIsInstance(cm.exception, (AuthError, PreconditionFailed))
        self.assert_clean(cm.exception)

    def test_timeout_transient(self):
        def script(m, p, h, b):
            time.sleep(1.0); return 207, XML, ms()
        s = self.serve(script)
        st = CalDAVStore("x", self.url, lambda: {"Authorization": "Bearer " + TOKEN}, timeout=0.2)
        with self.assertRaises(StoreError) as cm: st.list_items()
        self.assertTrue(cm.exception.transient)

    def test_secrets_scrubbed_from_error_text(self):
        body = f"echo Authorization: Bearer {TOKEN} and again {TOKEN} basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==".encode()
        self.serve(lambda m, p, h, b: (500, {}, body))
        with self.assertRaises(StoreError) as cm: self.store.list_items()
        self.assert_clean(cm.exception)
        self.assertNotIn("QWxhZGRpbjpvcGVuIHNlc2FtZQ", str(cm.exception))
        # basic credentials echoed by a server
        self.serve(lambda m, p, h, b: (500, {}, b"user alice pass hunter2-pw"))
        st = CalDAVStore("b", self.url, basic_headers("alice", "hunter2-pw"))
        with self.assertRaises(StoreError) as cm: st.list_items()
        self.assertNotIn("hunter2-pw", str(cm.exception))

    def test_put_precondition_and_missing(self):
        self.serve(lambda m, p, h, b: (412, {}, b""))
        with self.assertRaises(PreconditionFailed):
            self.store.create(EV.format(uid="x", title="t").encode())
        with self.assertRaises(PreconditionFailed):
            self.store.update("/cal/u/a.ics", '"e"', b"x")
        with self.assertRaises(PreconditionFailed):
            self.store.delete("/cal/u/a.ics", '"e"')

    def test_write_headers_and_etag_sent_verbatim(self):
        def script(m, p, h, b):
            if m == "PUT": return 204, {}, b""
            if m == "GET": return 200, {"ETag": '"new"'}, b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
            return 204, {}, b""
        s = self.serve(script)
        it = self.store.create(EV.format(uid="x", title="t").encode())
        self.assertEqual(s.requests[0][2]["If-None-Match"], "*")
        self.assertEqual(it.etag, '"new"')
        self.store.update(it.href, 'W/"abc"', b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
        self.assertEqual(s.requests[2][2]["If-Match"], 'W/"abc"')
        self.store.delete(it.href, '"q"')
        self.assertEqual(s.requests[-1][0], "DELETE"); self.assertEqual(s.requests[-1][2]["If-Match"], '"q"')

    def test_https_required(self):
        with self.assertRaises(ValueError):
            CalDAVStore("x", "http://example.com/c/", lambda: {})
        with self.assertRaises(ValueError):
            CalDAVStore("x", "ftp://127.0.0.1/c/", lambda: {})
        CalDAVStore("x", "https://example.com/c", lambda: {})

    def test_headers_fn_called_per_request(self):
        n = []
        def hf():
            n.append(1); return {"Authorization": f"Bearer t{len(n)}"}
        s = self.serve(lambda m, p, h, b: (207, XML, ms()))
        st = CalDAVStore("x", self.url, hf)
        st.list_items(); st.list_items()
        self.assertEqual([r[2]["Authorization"] for r in s.requests], ["Bearer t1", "Bearer t2"])


class CanonicalisingServer(unittest.TestCase):
    serve = StubStore.serve
    def test_create_found_by_uid_when_server_renames_the_resource(self):
        stored = {}
        data = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:u-ren\r\nSUMMARY:x\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"

        def script(m, p, h, b):
            if m == "PUT":
                stored["body"] = b
                return 201, {"ETag": '"p"'}, b""
            if m == "GET":
                return 404, {}, b"gone"            # canonical name differs from the one we PUT to
            if m == "PROPFIND":
                return 207, XML, ms(propfind_resp("/cal/u/", collection=True), propfind_resp("/cal/u/canonical-1.ics", '"c1"'))
            if m == "REPORT":
                return 207, XML, ms(multiget_resp("/cal/u/canonical-1.ics", '"c1"', data))
            return 500, {}, b""
        self.serve(script)
        it = self.store.create(data.encode())
        self.assertEqual((it.href, it.etag), ("/cal/u/canonical-1.ics", '"c1"'))
        self.assertEqual(stored["body"], data.encode())


class Helpers(unittest.TestCase):
    def test_basic_headers(self):
        import base64
        h = basic_headers("üser", "p:w")()
        self.assertEqual(base64.b64decode(h["Authorization"].split()[1]).decode(), "üser:p:w")

    def test_google_url(self):
        os.environ.pop("GOOGLE_CALDAV_BASE", None)
        self.assertEqual(google_collection_url("a.b@group.calendar.google.com"),
                         "https://apidata.googleusercontent.com/caldav/v2/a.b%40group.calendar.google.com/events/")
        os.environ["GOOGLE_CALDAV_BASE"] = "http://127.0.0.1:9/dav/"
        try:
            self.assertEqual(google_collection_url("x@y"), "http://127.0.0.1:9/dav/x%40y/events/")
        finally:
            del os.environ["GOOGLE_CALDAV_BASE"]


class Discovery(unittest.TestCase):
    def srv(self, cals):
        def script(m, p, h, b):
            if m != "PROPFIND": return 405, {}, b""
            body = b.decode()
            if "current-user-principal" in body:
                return 207, XML, ms("<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>/123/principal/</d:href>"
                                    "</d:current-user-principal></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
            if "calendar-home-set" in body:
                return 207, XML, ms("<d:response><d:href>/123/principal/</d:href><d:propstat><d:prop><c:calendar-home-set><d:href>/123/calendars/</d:href>"
                                    "</c:calendar-home-set></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
            rs = [propfind_resp("/123/calendars/", ct="")]
            for href, is_cal in cals:
                rt = "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>" if is_cal else "<d:resourcetype><d:collection/></d:resourcetype>"
                rs.append(f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{rt}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
            return 207, XML, ms(*rs)
        s = Stub(script); self.addCleanup(s.close); return s

    def test_found(self):
        s = self.srv([("/123/calendars/AAA-1/", True), ("/123/calendars/BBB-2/", True), ("/123/calendars/inbox/", False)])
        u = discover_icloud_collection(s.base + "/", "u", "p", "BBB-2")
        self.assertEqual(u, s.base + "/123/calendars/BBB-2/")
        self.assertEqual(s.requests[0][2]["Authorization"].split()[0], "Basic")

    def test_not_found_and_noncalendar_ignored(self):
        s = self.srv([("/123/calendars/AAA-1/", True), ("/123/calendars/BBB-2/", False)])
        with self.assertRaises(StoreError): discover_icloud_collection(s.base + "/", "u", "p", "BBB-2")

    def test_ambiguous(self):
        s = self.srv([("/123/calendars/x/dup/", True), ("/123/calendars/y/dup/", True)])
        with self.assertRaises(StoreError) as cm: discover_icloud_collection(s.base + "/", "u", "p", "dup")
        self.assertIn("ambiguous", str(cm.exception))

    def test_auth_error(self):
        s = Stub(lambda m, p, h, b: (401, {}, b"no")); self.addCleanup(s.close)
        with self.assertRaises(AuthError) as cm: discover_icloud_collection(s.base + "/", "u", "pw-secret", "x")
        self.assertNotIn("pw-secret", str(cm.exception))

    def test_home_on_foreign_site_refused(self):
        def script(m, p, h, b):
            if "current-user-principal" in b.decode():
                return 207, XML, ms("<d:response><d:href>http://evil.example:9/p/</d:href><d:propstat><d:prop><d:current-user-principal>"
                                    "<d:href>http://localhost:9/p/</d:href></d:current-user-principal></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
            return 207, XML, ms()
        s = Stub(script); self.addCleanup(s.close)
        with self.assertRaises(StoreError): discover_icloud_collection(s.base + "/", "u", "p", "x")
        self.assertEqual(len(s.requests), 1)


if __name__ == "__main__":
    unittest.main()
