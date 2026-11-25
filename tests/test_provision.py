"""provision.py against a local Radicale (CalDAV, stand-in for iCloud) and a stub
Google Calendar API. Tempdirs only; no real network or credentials.

Radicale note: with 'auth none' and an anonymous DAV the principal discovery returns
401, but DAV(base, "u", "x") (any basic-auth user) makes the real
DAV.calendar_home() work (-> /u/), so no monkeypatching of calendar_home is needed."""
import json, os, shutil, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from calsync import caldav, config, provision  # noqa: E402
from calsync.store import AuthError, StoreError  # noqa: E402
from calsync.util import now, write_json  # noqa: E402

RAD = os.environ.get("RADICALE_BIN") or shutil.which("radicale")

EV = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\nUID:{uid}\r\n"
      "DTSTAMP:20260101T000000Z\r\nDTSTART:20260301T100000Z\r\nDTEND:20260301T110000Z\r\n"
      "SUMMARY:{title}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_settings(tmp: Path, **kw) -> config.Settings:
    s = config.Settings(data_dir=tmp / "data", run_dir=tmp / "run", **kw)
    s.ensure_dirs()
    return s


def open_gates(s):
    s.write_enabled = True
    write_json(s.state / "backup_ok.json", {"ts": now(), "offhost": True})


class RecDAV(provision.DAV):
    """Records every request (method, url, headers)."""
    def __init__(self, *a):
        super().__init__(*a)
        self.log = []

    def req(self, method, url, body=None, headers=None):
        self.log.append((method, url, dict(headers or {})))
        return super().req(method, url, body, headers)


@unittest.skipUnless(RAD, "radicale not available")
class ICloudAgainstRadicale(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="calsync-prov-"))
        cls.port = free_port()
        (cls.tmp / "rad.conf").write_text(
            f"[server]\nhosts = 127.0.0.1:{cls.port}\n[auth]\ntype = none\n"
            f"[storage]\nfilesystem_folder = {cls.tmp}/rad\n[logging]\nlevel = error\n")
        cls.proc = subprocess.Popen([RAD, "--config", str(cls.tmp / "rad.conf")],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        cls.root = f"http://127.0.0.1:{cls.port}/"
        cls.home = cls.root + "u/"

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.n = f"t{time.monotonic_ns()}"
        d = self.tmp / self.n
        self.s = make_settings(d, icloud_url=self.root)
        self.dav = RecDAV(self.root, "u", "x")
        self.name = f"Cal {self.n}"

    def cals(self):
        return self.dav.list_calendars(self.home)

    def methods(self):
        return [m for m, _, _ in self.dav.log]

    # ---- calendar creation ------------------------------------------------
    def test_calendar_home_discovery_works_with_basic_user(self):
        self.assertEqual(self.dav.calendar_home(), self.home)

    def test_dry_run_creates_nothing(self):
        before = set(self.cals())
        r = provision.create_icloud_calendar(self.s, self.name, execute=False, dav=self.dav)
        self.assertEqual(r["action"], "would_create")
        self.assertEqual(r["display"], self.name)
        self.assertEqual(set(self.cals()), before)
        self.assertNotIn("MKCALENDAR", self.methods())

    def test_dry_run_ignores_closed_gates_and_open_gates(self):
        open_gates(self.s)
        r = provision.create_icloud_calendar(self.s, self.name, execute=False, dav=self.dav)
        self.assertEqual(r["action"], "would_create")
        self.assertNotIn(self.name, self.cals())

    def test_execute_gates_closed_raises_and_creates_nothing(self):
        before = set(self.cals())
        # 1) default: write flag off, no backup
        with self.assertRaises(provision.GateClosed) as cm:
            provision.create_icloud_calendar(self.s, self.name, execute=True, dav=self.dav)
        self.assertIn("CALSYNC_ALLOW_WRITES", str(cm.exception))
        self.assertIn("backup", str(cm.exception))
        # 2) flag on but backup stale
        self.s.write_enabled = True
        write_json(self.s.state / "backup_ok.json", {"ts": now() - 30 * 3600})
        with self.assertRaises(provision.GateClosed):
            provision.create_icloud_calendar(self.s, self.name, execute=True, dav=self.dav)
        # 3) all open but PAUSE present
        open_gates(self.s)
        (self.s.state / "PAUSE").write_text("")
        with self.assertRaises(provision.GateClosed) as cm:
            provision.create_icloud_calendar(self.s, self.name, execute=True, dav=self.dav)
        self.assertIn("PAUSE", str(cm.exception))
        # 4) backup file without ts
        (self.s.state / "PAUSE").unlink()
        write_json(self.s.state / "backup_ok.json", {})
        with self.assertRaises(provision.GateClosed):
            provision.create_icloud_calendar(self.s, self.name, execute=True, dav=self.dav)
        self.assertEqual(set(self.cals()), before)
        self.assertNotIn("MKCALENDAR", self.methods())

    def test_execute_gates_open_creates_calendar_idempotently(self):
        open_gates(self.s)
        name = f"Cal & <{self.n}>"   # exercises XML escaping
        r = provision.create_icloud_calendar(self.s, name, execute=True, dav=self.dav)
        self.assertEqual(r["action"], "created")
        cals = self.cals()
        self.assertEqual(cals.get(name), r["url"])
        # VEVENT component set
        t = self.dav.propfind(r["url"], "<c:supported-calendar-component-set/>")
        comps = {c.get("name") for c in t.iter("{urn:ietf:params:xml:ns:caldav}comp")}
        self.assertEqual(comps, {"VEVENT"})
        # second call: exists, no duplicate, no further MKCALENDAR
        n_mk = self.methods().count("MKCALENDAR")
        r2 = provision.create_icloud_calendar(self.s, name, execute=True, dav=self.dav)
        self.assertEqual(r2, {"action": "exists", "url": r["url"]})
        self.assertEqual(self.methods().count("MKCALENDAR"), n_mk)
        self.assertEqual(sum(1 for k in self.cals() if k == name), 1)
        # dry-run on existing also reports exists
        self.assertEqual(provision.create_icloud_calendar(self.s, name, execute=False, dav=self.dav)["action"], "exists")

    def test_exists_does_not_need_gates(self):
        open_gates(self.s)
        provision.create_icloud_calendar(self.s, self.name, execute=True, dav=self.dav)
        self.s.write_enabled = False
        r = provision.create_icloud_calendar(self.s, self.name, execute=True, dav=self.dav)
        self.assertEqual(r["action"], "exists")

    # ---- copy_items --------------------------------------------------------
    def mk_target(self):
        open_gates(self.s)
        return provision.create_icloud_calendar(self.s, self.name, execute=True, dav=self.dav)["url"]

    def mk_src(self, **uid_title):
        src = self.tmp / self.n / "src"
        src.mkdir(parents=True, exist_ok=True)
        for uid, title in uid_title.items():
            (src / f"{uid}.ics").write_bytes(EV.format(uid=uid, title=title).encode())
        return src

    def target_events(self, url):
        """[(href, UID, SUMMARY)] of the resources in the target collection."""
        t = self.dav.propfind(url, "<d:getetag/>", "1")
        out = []
        for r in t.findall("d:response", provision.NS):
            href = r.find("d:href", provision.NS).text
            if not href.endswith(".ics"):
                continue
            st, body, _ = self.dav.req("GET", href)
            txt = body.decode()
            uid = [l[4:] for l in txt.splitlines() if l.startswith("UID:")][0]
            summ = [l[8:] for l in txt.splitlines() if l.startswith("SUMMARY:")][0]
            out.append((href, uid, summ))
        return out

    def test_copy_items_dry_run_lists_plan_and_writes_nothing(self):
        target = self.mk_target()
        src = self.mk_src(u1="One", u2="Two", u3="Three")
        self.dav.log.clear()
        r = provision.copy_items(self.s, src, ["u1", "u3", "nope"], target, execute=False, dav=self.dav)
        self.assertEqual(r, {"action": "dry_run", "count": 2, "uids": ["u1", "u3"], "missing": ["nope"]})
        self.assertEqual(self.dav.log, [])          # no HTTP at all
        self.assertEqual(self.target_events(target), [])

    def test_copy_items_gates_closed(self):
        target = self.mk_target()
        src = self.mk_src(u1="One")
        self.s.write_enabled = False
        self.dav.log.clear()
        with self.assertRaises(provision.GateClosed):
            provision.copy_items(self.s, src, ["u1"], target, execute=True, dav=self.dav)
        self.assertEqual(self.dav.log, [])
        self.assertEqual(self.target_events(target), [])

    def test_copy_items_execute_missing_uid_aborts_before_any_put(self):
        target = self.mk_target()
        src = self.mk_src(u1="One")
        self.dav.log.clear()
        with self.assertRaises(RuntimeError):
            provision.copy_items(self.s, src, ["u1", "ghost"], target, execute=True, dav=self.dav)
        self.assertNotIn("PUT", self.methods())

    def test_copy_items_execute_puts_with_if_none_match(self):
        target = self.mk_target()
        src = self.mk_src(u1="One", u2="Two")
        self.dav.log.clear()
        r = provision.copy_items(self.s, src, ["u1", "u2"], target, execute=True, dav=self.dav)
        self.assertEqual(r, {"action": "copied", "done": 2, "failed": []})
        puts = [(u, h) for m, u, h in self.dav.log if m == "PUT"]
        self.assertEqual(len(puts), 2)
        for u, h in puts:
            self.assertEqual(h.get("If-None-Match"), "*")
            self.assertTrue(u.startswith(target) and u.endswith(".ics"))
        self.assertEqual({e[1]: e[2] for e in self.target_events(target)}, {"u1": "One", "u2": "Two"})

    def test_if_none_match_same_resource_name_never_overwrites(self):
        """Same resource name reused -> server answers 412; copy_items reports it as failed."""
        target = self.mk_target()
        src = self.mk_src(u1="Original")
        fixed = mock.Mock(hex="ABCDEF0123")
        with mock.patch.object(provision.uuid, "uuid4", return_value=fixed):
            r1 = provision.copy_items(self.s, src, ["u1"], target, execute=True, dav=self.dav)
            (src / "u1.ics").write_bytes(EV.format(uid="u1", title="CHANGED").encode())
            r2 = provision.copy_items(self.s, src, ["u1"], target, execute=True, dav=self.dav)
        self.assertEqual(r1["done"], 1)
        self.assertEqual(r2, {"action": "copied", "done": 0, "failed": [("u1", 412)]})
        ev = self.target_events(target)
        self.assertEqual([(e[1], e[2]) for e in ev], [("u1", "Original")])

    def test_copy_items_rerun_does_not_duplicate_uid_on_target(self):
        """copy_items generates a NEW resource name per call, so If-None-Match:* alone
        cannot prevent a same-UID resubmission. Protection comes only from the server's
        CalDAV no-uid-conflict (409), which Radicale (and iCloud) enforce. Verified here:
        re-run does not overwrite or duplicate the UID, and the 409 is surfaced as failed."""
        target = self.mk_target()
        src = self.mk_src(u1="Original")
        r1 = provision.copy_items(self.s, src, ["u1"], target, execute=True, dav=self.dav)
        self.assertEqual((r1["done"], r1["failed"]), (1, []))
        (src / "u1.ics").write_bytes(EV.format(uid="u1", title="CHANGED").encode())
        self.dav.log.clear()
        r2 = provision.copy_items(self.s, src, ["u1"], target, execute=True, dav=self.dav)
        put_urls = [u for m, u, _ in self.dav.log if m == "PUT"]
        self.assertEqual(len(put_urls), 1)
        # different resource name than the first copy (this is why If-None-Match can't help)
        first_href = self.target_events(target)[0][0]
        self.assertNotEqual(put_urls[0].rsplit("/", 1)[-1], first_href.rsplit("/", 1)[-1])
        self.assertEqual(r2["done"], 0)
        self.assertEqual(r2["failed"], [("u1", 409)])
        ev = self.target_events(target)
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0][1], ev[0][2]), ("u1", "Original"))

    def test_copy_items_does_not_precheck_target_for_existing_uid(self):
        """Documents the design gap: copy_items never reads the target, so it always
        attempts a PUT for every UID (relies entirely on the server's 409)."""
        target = self.mk_target()
        src = self.mk_src(u1="Original")
        provision.copy_items(self.s, src, ["u1"], target, execute=True, dav=self.dav)
        self.dav.log.clear()
        provision.copy_items(self.s, src, ["u1"], target, execute=True, dav=self.dav)
        self.assertEqual(self.methods(), ["PUT"])


# ---- Google ---------------------------------------------------------------

class GoogleStub:
    def __init__(self, existing=None):
        self.calendars = list(existing or [])
        self.requests = []   # (method, path, auth, body)
        self.queries = []
        self.pages = None    # optional list of calendarList pages (token = index)
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                path, _, query = self.path.partition("?")
                outer.queries.append(query)
                outer.requests.append((self.command, path, self.headers.get("Authorization"), raw))
                if path == "/token":
                    return self._send(200, {"access_token": "ACCESS-123", "expires_in": 3600})
                if path == "/users/me/calendarList" and self.command == "GET":
                    if outer.pages:
                        tok = urllib.parse.parse_qs(query).get("pageToken", ["0"])[0]
                        return self._send(200, outer.pages[int(tok)])
                    return self._send(200, {"items": outer.calendars})
                if path == "/calendars" and self.command == "POST":
                    body = json.loads(raw)
                    c = {"id": f"id{len(outer.calendars)}@group.calendar.google.com", "summary": body["summary"],
                         "timeZone": body.get("timeZone")}
                    outer.calendars.append(c)
                    return self._send(200, c)
                self._send(404, {"error": "nf"})

            do_GET = do_POST = _do

            def _send(self, code, obj):
                d = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(d)))
                self.end_headers()
                self.wfile.write(d)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def posts(self):
        return [r for r in self.requests if r[0] == "POST" and r[1] == "/calendars"]


class GoogleProvision(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="calsync-g-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.s = make_settings(self.tmp)
        (self.s.tokens / "google.json").write_text(json.dumps(
            {"refresh_token": "REFRESH-XYZ", "access_token": "old", "expires_at": 1}))
        self.stub = GoogleStub([{"id": "primary@x", "summary": "Existing"}])
        self.addCleanup(self.stub.close)
        env = {"GOOGLE_API_BASE": f"http://127.0.0.1:{self.stub.port}",
               "GOOGLE_TOKEN_URL": f"http://127.0.0.1:{self.stub.port}/token",
               "GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csecret-value"}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)

    def test_dry_run_no_post_and_bearer_auth(self):
        r = provision.create_google_calendar(self.s, "New Cal", execute=False)
        self.assertEqual(r, {"action": "would_create", "summary": "New Cal"})
        self.assertEqual(self.stub.posts(), [])
        gets = [x for x in self.stub.requests if x[1] == "/users/me/calendarList"]
        self.assertEqual(len(gets), 1)
        self.assertEqual(gets[0][2], "Bearer ACCESS-123")
        # token refresh used the refresh token, with the expired stored token ignored
        tok = [x for x in self.stub.requests if x[1] == "/token"]
        self.assertEqual(len(tok), 1)
        self.assertIn(b"refresh_token=REFRESH-XYZ", tok[0][3])
        self.assertIn(b"grant_type=refresh_token", tok[0][3])

    def test_existing_summary_not_recreated(self):
        for ex in (False, True):
            open_gates(self.s)
            r = provision.create_google_calendar(self.s, "Existing", execute=ex)
            self.assertEqual(r, {"action": "exists", "id": "primary@x"})
        self.assertEqual(self.stub.posts(), [])

    def test_execute_gates_closed(self):
        with self.assertRaises(provision.GateClosed):
            provision.create_google_calendar(self.s, "New Cal", execute=True)
        self.s.write_enabled = True   # backup still missing
        with self.assertRaises(provision.GateClosed):
            provision.create_google_calendar(self.s, "New Cal", execute=True)
        open_gates(self.s)
        (self.s.state / "PAUSE").write_text("")
        with self.assertRaises(provision.GateClosed):
            provision.create_google_calendar(self.s, "New Cal", execute=True)
        self.assertEqual(self.stub.posts(), [])

    def test_execute_gates_open_creates_once(self):
        open_gates(self.s)
        r = provision.create_google_calendar(self.s, "New Cal", "Europe/Berlin", execute=True)
        self.assertEqual(r["action"], "created")
        posts = self.stub.posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], "Bearer ACCESS-123")
        self.assertEqual(json.loads(posts[0][3]), {"summary": "New Cal", "timeZone": "Europe/Berlin"})
        r2 = provision.create_google_calendar(self.s, "New Cal", execute=True)
        self.assertEqual(r2["action"], "exists")
        self.assertEqual(len(self.stub.posts()), 1)

    def test_valid_stored_token_is_used_without_refresh(self):
        (self.s.tokens / "google.json").write_text(json.dumps(
            {"refresh_token": "R", "access_token": "STORED", "expires_at": now() + 3600}))
        provision.create_google_calendar(self.s, "New Cal", execute=False)
        self.assertEqual([x for x in self.stub.requests if x[1] == "/token"], [])
        self.assertEqual(self.stub.requests[0][2], "Bearer STORED")
        # token file is not rewritten (vdirsyncer owns it)
        self.assertEqual(json.loads((self.s.tokens / "google.json").read_text())["access_token"], "STORED")


# ---- transport hardening (stub servers on 127.0.0.1 only) ------------------------

class Stub:
    """Tiny recording HTTP server. ``respond(handler, body)`` answers each request."""

    def __init__(self, respond):
        self.requests = []   # (method, path, headers(lower-case keys), body)
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                outer.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, raw))
                respond(self, raw)

            do_GET = do_POST = do_PUT = do_PROPFIND = do_MKCALENDAR = _do

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def send(h, code, body=b"", headers=None):
    h.send_response(code)
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def ms(inner: str, pre: str = "") -> bytes:
    return (f'<?xml version="1.0"?>{pre}<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f"{inner}</d:multistatus>").encode()


def principal_ms(href: str) -> bytes:
    return ms(f"<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>{href}"
              "</d:href></d:current-user-principal></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")


class TransportBase(unittest.TestCase):
    def stub(self, respond):
        s = Stub(respond)
        self.addCleanup(s.close)
        return s

    def dav(self, stub, user="u", pw="SECRET-PW"):
        return provision.DAV(stub.url, user, pw)


class DavOriginPolicy(TransportBase):
    def test_server_supplied_cross_origin_principal_refused_before_any_request(self):
        other = self.stub(lambda h, b: send(h, 207, ms("")))
        a = self.stub(lambda h, b: send(h, 207, principal_ms(other.url + "p/")))
        with self.assertRaises(provision.OriginError):
            self.dav(a).calendar_home()
        self.assertEqual(len(a.requests), 1)               # only the initial PROPFIND
        self.assertEqual(other.requests, [])               # nothing (and so no credentials) reached the other origin
        self.assertTrue(a.requests[0][2]["authorization"].startswith("Basic "))

    def test_cross_origin_calendar_home_set_refused(self):
        other = self.stub(lambda h, b: send(h, 207, ms("")))

        def respond(h, b):
            if b"current-user-principal" in b:
                return send(h, 207, principal_ms("/p/"))
            send(h, 207, ms("<d:response><d:href>/p/</d:href><d:propstat><d:prop><c:calendar-home-set><d:href>"
                            f"{other.url}home/</d:href></c:calendar-home-set></d:prop><d:status>HTTP/1.1 200 OK"
                            "</d:status></d:propstat></d:response>"))
        a = self.stub(respond)
        with self.assertRaises(provision.OriginError):
            self.dav(a).calendar_home()
        self.assertEqual(other.requests, [])

    def test_cross_origin_calendar_href_in_listing_refused(self):
        other = self.stub(lambda h, b: send(h, 207, ms("")))
        a = self.stub(lambda h, b: send(h, 207, ms(
            f"<d:response><d:href>{other.url}cal/</d:href><d:propstat><d:prop><d:displayname>X</d:displayname>"
            "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK"
            "</d:status></d:propstat></d:response>")))
        with self.assertRaises(provision.OriginError):
            self.dav(a).list_calendars(a.url + "home/")
        self.assertEqual(other.requests, [])

    def test_foreign_target_and_request_urls_refused_without_traffic(self):
        other = self.stub(lambda h, b: send(h, 200))
        a = self.stub(lambda h, b: send(h, 200))
        dav = self.dav(a)
        s = make_settings(Path(tempfile.mkdtemp(prefix="calsync-p-")))
        self.addCleanup(shutil.rmtree, s.data_dir.parent, ignore_errors=True)
        with self.assertRaises(provision.OriginError):          # dry-run too
            provision.copy_items(s, s.data_dir, ["u"], other.url + "cal/", execute=False, dav=dav)
        for bad in (other.url + "x", "https://evil.invalid/x", "file:///etc/passwd", "//127.0.0.1:1/x",
                    a.url.replace("http://", "http://user:pw@") + "x", "http://127.0.0.1:1/x\r\nX: y"):
            with self.assertRaises(provision.OriginError, msg=bad):
                dav.req("GET", bad)
        self.assertEqual(other.requests + a.requests, [])

    def test_base_url_policy(self):
        for bad in ("http://example.com/", "ftp://example.com/", "file:///tmp/x", "http://203.0.113.5/"):
            with self.assertRaises(ValueError, msg=bad):
                provision.DAV(bad, "u", "p")
        with self.assertRaises(provision.OriginError):
            provision.DAV("http://u:p@127.0.0.1:9/", None, None)

    def test_icloud_shard_hosts_only_over_https_and_only_for_icloud(self):
        dav = provision.DAV("https://caldav.icloud.com/", "u", "p")
        self.assertTrue(dav._url("https://p12-caldav.icloud.com:443/123/calendars/").startswith("https://p12-"))
        self.assertEqual(dav._url("/a/b"), "https://caldav.icloud.com/a/b")
        for bad in ("http://p12-caldav.icloud.com/x", "https://caldav.icloud.com.evil.example/x",
                    "https://evil-icloud.com/x", "https://p12-caldav.icloud.com:8443/x", "https://example.com/x"):
            with self.assertRaises(provision.OriginError, msg=bad):
                dav._url(bad)
        other = provision.DAV("https://dav.example.com/", "u", "p")
        with self.assertRaises(provision.OriginError):
            other._url("https://p12-caldav.icloud.com/x")


class DavRedirects(TransportBase):
    def test_cross_origin_redirect_refused_and_no_credentials_follow(self):
        other = self.stub(lambda h, b: send(h, 200, b"x"))
        a = self.stub(lambda h, b: send(h, 302, headers={"Location": other.url + "steal"}))
        with self.assertRaises(StoreError) as cm:
            self.dav(a).req("PROPFIND", "x", b"<a/>", {"Depth": "0"})
        self.assertIn("different origin", str(cm.exception))
        self.assertEqual(other.requests, [])
        self.assertNotIn("SECRET-PW", str(cm.exception))

    def test_same_origin_redirect_followed_with_auth_and_bounded(self):
        def respond(h, b):
            if h.path == "/old":
                return send(h, 307, headers={"Location": "/new"})
            if h.path == "/loop":
                return send(h, 302, headers={"Location": "/loop"})
            send(h, 200, b"ok")
        a = self.stub(respond)
        dav = self.dav(a)
        st, body, _ = dav.req("GET", "/old")
        self.assertEqual((st, body), (200, b"ok"))
        self.assertEqual([r[1] for r in a.requests], ["/old", "/new"])
        self.assertTrue(all(r[2]["authorization"].startswith("Basic ") for r in a.requests))
        with self.assertRaises(StoreError) as cm:
            dav.req("GET", "/loop")
        self.assertIn("too many redirects", str(cm.exception))


class DavXmlAndBounds(TransportBase):
    def test_doctype_and_entity_refused(self):
        bombs = [
            ms("", pre='<!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'),
            ms("<d:response/>", pre='<!DOCTYPE x SYSTEM "http://127.0.0.1:1/evil.dtd">'),
            ms("", pre="<!-- " + "p" * 6000 + " --><!DOCTYPE x []>"),       # DOCTYPE beyond the first 4 KiB
            ms("", pre='<!ENTITY e "v">'),
        ]
        for body in bombs:
            a = self.stub(lambda h, b, body=body: send(h, 207, body))
            with self.assertRaises(StoreError, msg=body[:60]):
                self.dav(a).propfind("x", "<d:displayname/>")

    def test_wrong_root_and_malformed_xml_refused(self):
        for body in (b"<html/>", b"<d:multistatus", b"not xml"):
            a = self.stub(lambda h, b, body=body: send(h, 207, body))
            with self.assertRaises(StoreError):
                self.dav(a).propfind("x", "<d:displayname/>")

    def test_oversized_response_is_bounded(self):
        a = self.stub(lambda h, b: send(h, 207, ms("<!--" + "x" * 5000 + "-->")))
        with mock.patch.object(caldav, "MAX_BODY", 1000):
            with self.assertRaises(StoreError) as cm:
                self.dav(a).propfind("x", "<d:displayname/>")
        self.assertIn("too large", str(cm.exception))

    def test_error_status_is_reported_without_credentials(self):
        a = self.stub(lambda h, b: send(h, 500, b"boom " + h.headers["Authorization"].encode()))
        with self.assertRaises(RuntimeError) as cm:
            self.dav(a).propfind("x", "<d:displayname/>")
        self.assertNotIn("SECRET-PW", str(cm.exception))
        self.assertNotIn("Basic", str(cm.exception))

    def test_mkcalendar_escapes_name_and_rejects_control_chars(self):
        a = self.stub(lambda h, b: send(h, 201))
        dav = self.dav(a)
        name = "A & <b> \"q\" 'r'</d:displayname><x/>"
        url = dav.mkcalendar(a.url + "home/", name)
        self.assertTrue(url.startswith(a.url + "home/") and url.endswith("/"))
        root = ET.fromstring(a.requests[0][3])
        self.assertEqual(root.find(".//{DAV:}displayname").text, name)
        self.assertIsNone(root.find(".//x"))
        for bad in ("a\x00b", "a\nb", "a\x7fb", "", "   ", "x" * 256):
            with self.assertRaises(ValueError, msg=repr(bad)):
                dav.mkcalendar(a.url + "home/", bad)
        self.assertEqual(len(a.requests), 1)

    def test_mkcalendar_home_on_foreign_origin_refused(self):
        other = self.stub(lambda h, b: send(h, 201))
        a = self.stub(lambda h, b: send(h, 201))
        with self.assertRaises(provision.OriginError):
            self.dav(a).mkcalendar(other.url + "home/", "Cal")
        self.assertEqual(other.requests + a.requests, [])


class GoogleTransport(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="calsync-gt-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.s = make_settings(self.tmp)
        (self.s.tokens / "google.json").write_text(json.dumps(
            {"refresh_token": "R-T", "access_token": "ACCESS-VALID", "expires_at": now() + 3600}))
        self.env = {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csecret-value"}
        for k in ("CALSYNC_TIMEZONE", "TZ", "GOOGLE_API_BASE", "GOOGLE_TOKEN_URL"):
            os.environ.pop(k, None)
        p = mock.patch.dict(os.environ, self.env)
        p.start()
        self.addCleanup(p.stop)

    def serve(self, respond):
        st = Stub(respond)
        self.addCleanup(st.close)
        os.environ["GOOGLE_API_BASE"] = st.url.rstrip("/")
        return st

    def test_api_base_policy(self):
        for bad in ("http://example.com/calendar/v3", "ftp://x/", "file:///tmp", "http://203.0.113.5/x"):
            os.environ["GOOGLE_API_BASE"] = bad
            with self.assertRaises(ValueError, msg=bad):
                provision.create_google_calendar(self.s, "X", execute=False)
        os.environ["GOOGLE_API_BASE"] = "http://u:p@127.0.0.1:9/x"
        with self.assertRaises(provision.OriginError):
            provision.create_google_calendar(self.s, "X", execute=False)

    def test_default_base_is_https_google(self):
        self.assertTrue(provision.DEFAULT_GOOGLE_API.startswith("https://www.googleapis.com/"))
        c = provision._GoogleClient(self.s)
        self.assertEqual(c.base, provision.DEFAULT_GOOGLE_API)

    def test_missing_client_credentials(self):
        del os.environ["GOOGLE_CLIENT_SECRET"]
        with self.assertRaises(RuntimeError):
            provision.create_google_calendar(self.s, "X", execute=False)

    def test_redirect_to_other_origin_refused_without_bearer_leak(self):
        other = Stub(lambda h, b: send(h, 200, b"{}"))
        self.addCleanup(other.close)
        a = self.serve(lambda h, b: send(h, 302, headers={"Location": other.url + "x"}))
        with self.assertRaises(StoreError):
            provision.create_google_calendar(self.s, "X", execute=False)
        self.assertEqual(other.requests, [])
        self.assertEqual(len(a.requests), 1)

    def test_error_message_has_no_token(self):
        self.serve(lambda h, b: send(h, 403, b"denied: Authorization: Bearer ACCESS-VALID ACCESS-VALID"))
        with self.assertRaises(AuthError) as cm:
            provision.create_google_calendar(self.s, "X", execute=False)
        self.assertNotIn("ACCESS-VALID", str(cm.exception))

    def test_oversized_and_malformed_json_bounded(self):
        self.serve(lambda h, b: send(h, 200, json.dumps({"items": [], "pad": "x" * 5000}).encode()))
        with mock.patch.object(provision, "_MAX_JSON", 1000):
            with self.assertRaises(StoreError):
                provision.create_google_calendar(self.s, "X", execute=False)
        with mock.patch.object(caldav, "MAX_BODY", 1000):
            with self.assertRaises(StoreError):
                provision.create_google_calendar(self.s, "X", execute=False)
        self.serve(lambda h, b: send(h, 200, b"<html>"))
        with self.assertRaises(StoreError):
            provision.create_google_calendar(self.s, "X", execute=False)
        self.serve(lambda h, b: send(h, 200, b"[1]"))
        with self.assertRaises(StoreError):
            provision.create_google_calendar(self.s, "X", execute=False)

    def test_calendar_list_pagination_finds_calendar_on_later_page(self):
        st = GoogleStub()
        self.addCleanup(st.close)
        st.pages = [{"items": [{"id": "a", "summary": "A"}], "nextPageToken": "1"},
                    {"items": [{"id": "b", "summary": "Wanted"}]}]
        os.environ["GOOGLE_API_BASE"] = f"http://127.0.0.1:{st.port}"
        open_gates(self.s)
        self.assertEqual(provision.create_google_calendar(self.s, "Wanted", execute=True), {"action": "exists", "id": "b"})
        self.assertEqual(st.posts(), [])
        self.assertEqual(st.queries, ["", "pageToken=1"])

    def test_calendar_name_validated(self):
        for bad in ("", "a\x00", "a\nb"):
            with self.assertRaises(ValueError):
                provision.create_google_calendar(self.s, bad, execute=False)


class Timezone(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("CALSYNC_TIMEZONE", None)
        os.environ.pop("TZ", None)

    def test_resolution_order(self):
        self.assertEqual(provision.resolve_timezone(), "UTC")
        os.environ["TZ"] = ":Europe/Berlin"
        self.assertEqual(provision.resolve_timezone(), "Europe/Berlin")
        os.environ["CALSYNC_TIMEZONE"] = "America/New_York"
        self.assertEqual(provision.resolve_timezone(), "America/New_York")
        self.assertEqual(provision.resolve_timezone("Asia/Tokyo"), "Asia/Tokyo")

    def test_invalid_zone_rejected(self):
        for bad in ("Not/AZone", "../etc/passwd", "Europe/Vien na"):
            with self.assertRaises(ValueError, msg=bad):
                provision.resolve_timezone(bad)

    def test_no_hardcoded_default_zone(self):
        src = (Path(provision.__file__)).read_text()
        self.assertNotIn("Vienna", src)

    def test_default_post_uses_utc_and_env_zone(self):
        tmp = Path(tempfile.mkdtemp(prefix="calsync-tz-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        s = make_settings(tmp)
        open_gates(s)
        (s.tokens / "google.json").write_text(json.dumps(
            {"refresh_token": "R", "access_token": "A", "expires_at": now() + 3600}))
        st = GoogleStub()
        self.addCleanup(st.close)
        os.environ.update({"GOOGLE_API_BASE": f"http://127.0.0.1:{st.port}", "GOOGLE_CLIENT_ID": "c",
                           "GOOGLE_CLIENT_SECRET": "s"})
        provision.create_google_calendar(s, "One", execute=True)
        os.environ["CALSYNC_TIMEZONE"] = "Europe/Paris"
        provision.create_google_calendar(s, "Two", execute=True)
        with self.assertRaises(ValueError):
            provision.create_google_calendar(s, "Three", "Mars/Base", execute=True)
        self.assertEqual([json.loads(p[3])["timeZone"] for p in st.posts()], ["UTC", "Europe/Paris"])


if __name__ == "__main__":
    unittest.main()
