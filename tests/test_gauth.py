import json, os, stat, sys, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from calsync.gauth import GoogleTokenSource
from calsync.store import AuthError, StoreError

NOW = 1_800_000_000
AT_OLD, AT_NEW, RT, CS = "ya29.OLD-ACCESS-TOKEN", "ya29.NEW-ACCESS-TOKEN", "1//REFRESH-SECRET", "CLIENT-SECRET-VALUE"
SECRETS = (AT_OLD, AT_NEW, RT, CS)


class Endpoint:
    """Stub OAuth token endpoint."""

    def __init__(self):
        self.calls = 0
        self.bodies = []
        self.status = 200
        self.payload = {"access_token": AT_NEW, "expires_in": 3599, "scope": "cal", "token_type": "Bearer"}
        self.delay = 0.0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.bodies.append(self.rfile.read(n).decode())
                outer.calls += 1
                time.sleep(outer.delay)
                raw = json.dumps(outer.payload).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/token"
        threading.Thread(target=self.srv.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True).start()

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


class GAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="calsync-gauth-"))
        self.ep = Endpoint()
        self.addCleanup(self.ep.close)
        self.tf = self.tmp / "google.json"
        self.now = NOW
        self.clock = lambda: self.now

    def write_token(self, expires_at, **extra):
        tok = {"access_token": AT_OLD, "expires_at": expires_at, "expires_in": 3599, "refresh_token": RT,
               "scope": "cal", "token_type": "Bearer", "refresh_token_expires_in": 604799, "x_unknown": {"a": 1}}
        tok.update(extra)
        self.tf.write_text(json.dumps(tok))
        os.chmod(self.tf, 0o644)

    def src(self, **kw):
        return GoogleTokenSource(self.tf, "cid", CS, token_url=self.ep.url, clock=self.clock, **kw)

    def assert_no_secrets(self, text):
        for s in SECRETS:
            self.assertNotIn(s, text)

    def test_valid_token_no_refresh(self):
        self.write_token(NOW + 3000)
        self.assertEqual(self.src().headers(), {"Authorization": "Bearer " + AT_OLD})
        self.assertEqual(self.ep.calls, 0)

    def test_expired_refreshes_and_persists_atomically(self):
        self.write_token(NOW + 60)             # inside the 120 s skew -> refresh
        s = self.src()
        self.assertEqual(s.headers(), {"Authorization": "Bearer " + AT_NEW})
        self.assertEqual(self.ep.calls, 1)
        tok = json.loads(self.tf.read_text())
        self.assertEqual(tok["access_token"], AT_NEW)
        self.assertEqual(tok["expires_at"], NOW + 3599)
        self.assertEqual(tok["expires_in"], 3599)
        self.assertEqual(tok["refresh_token"], RT)               # retained
        self.assertEqual(tok["x_unknown"], {"a": 1})             # unknown keys retained
        self.assertEqual(tok["refresh_token_expires_in"], 604799)
        self.assertEqual(stat.S_IMODE(os.stat(self.tf).st_mode), 0o600)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["google.json"])   # no tmp leftovers
        self.assertIn("grant_type=refresh_token", self.ep.bodies[0])
        s.headers(); self.assertEqual(self.ep.calls, 1)           # cached via file
        st = s.status()
        self.assertEqual(st["last_refresh_ok"], NOW)
        self.assertIsNone(st["last_error"])
        self.assertTrue(st["token_file_exists"])

    def test_refresh_boundary(self):
        self.write_token(NOW + 120)            # exactly at the skew: not < clock -> still valid
        self.src().headers(); self.assertEqual(self.ep.calls, 0)
        self.write_token(NOW + 119)
        self.src().headers(); self.assertEqual(self.ep.calls, 1)

    def test_invalid_grant_is_autherror_file_untouched(self):
        self.write_token(NOW - 10)
        before, mt = self.tf.read_bytes(), os.stat(self.tf).st_mtime_ns
        self.ep.status, self.ep.payload = 400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
        s = self.src()
        with self.assertRaises(AuthError) as cm:
            s.headers()
        msg = str(cm.exception)
        self.assertIn("7 days", msg); self.assertIn("expired/revoked", msg)
        self.assert_no_secrets(msg)
        self.assertFalse(cm.exception.transient)
        self.assertEqual(self.tf.read_bytes(), before)
        self.assertEqual(os.stat(self.tf).st_mtime_ns, mt)
        st = s.status()
        self.assertIn("invalid_grant", st["last_error"])
        self.assert_no_secrets(json.dumps(st))

    def test_http_401_is_autherror(self):
        self.write_token(NOW - 10)
        self.ep.status, self.ep.payload = 401, {"error": "invalid_client"}
        with self.assertRaises(AuthError) as cm:
            self.src().headers()
        self.assert_no_secrets(str(cm.exception))

    def test_5xx_is_transient_storeerror(self):
        self.write_token(NOW - 10)
        before = self.tf.read_bytes()
        self.ep.status, self.ep.payload = 503, {"error": "backend", "detail": RT}
        with self.assertRaises(StoreError) as cm:
            self.src().headers()
        self.assertNotIsInstance(cm.exception, AuthError)
        self.assertTrue(cm.exception.transient)
        self.assert_no_secrets(str(cm.exception))
        self.assertEqual(self.tf.read_bytes(), before)

    def test_connection_refused_is_transient(self):
        self.write_token(NOW - 10)
        s = GoogleTokenSource(self.tf, "cid", CS, token_url="http://127.0.0.1:1/token", clock=self.clock)
        with self.assertRaises(StoreError) as cm:
            s.headers()
        self.assertTrue(cm.exception.transient)
        self.assertNotIsInstance(cm.exception, AuthError)
        self.assert_no_secrets(str(cm.exception))

    def test_malformed_response_is_transient(self):
        self.write_token(NOW - 10)
        self.ep.payload = {"nope": 1}
        with self.assertRaises(StoreError) as cm:
            self.src().headers()
        self.assertTrue(cm.exception.transient)

    def test_other_process_already_refreshed_no_http(self):
        # file is stale when the source first looks, fresh by the time headers() runs the check
        self.write_token(NOW + 3000, access_token="ya29.OTHER-PROCESS-TOKEN")
        s = self.src()
        self.assertEqual(s.headers(), {"Authorization": "Bearer ya29.OTHER-PROCESS-TOKEN"})
        self.assertEqual(self.ep.calls, 0)

    def test_other_process_refreshes_during_our_http_call(self):
        self.write_token(NOW - 10)
        s = self.src()
        orig = s._post_refresh

        def racing(rt):
            r = orig(rt)
            # vdirsyncer wins the race and writes a token that lives longer than ours
            self.write_token(NOW + 3599 + 500, access_token="ya29.VDS-TOKEN", refresh_token="1//ROTATED")
            return r
        s._post_refresh = racing
        self.assertEqual(s.headers(), {"Authorization": "Bearer ya29.VDS-TOKEN"})
        self.assertEqual(json.loads(self.tf.read_text())["refresh_token"], "1//ROTATED")   # not clobbered

    def test_threads_exactly_one_refresh(self):
        self.write_token(NOW - 10)
        self.ep.delay = 0.3
        s = self.src()
        out, errs = [], []

        def w():
            try:
                out.append(s.headers()["Authorization"])
            except Exception as e:      # pragma: no cover
                errs.append(e)
        ts = [threading.Thread(target=w) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(self.ep.calls, 1)
        self.assertEqual(set(out), {"Bearer " + AT_NEW})

    def test_persist_failure_still_returns_token_without_refreshing_again(self):
        self.write_token(NOW - 10)
        s = self.src()
        s._write_atomic = lambda tok: (_ for _ in ()).throw(PermissionError("ro"))
        h = s.headers()
        self.assertEqual(h, {"Authorization": "Bearer " + AT_NEW})
        s.headers()
        self.assertEqual(self.ep.calls, 1)
        self.assertIn("persisted", s.status()["last_error"])

    def test_missing_file(self):
        s = self.src()
        with self.assertRaises(AuthError):
            s.headers()
        st = s.status()
        self.assertFalse(st["token_file_exists"]); self.assertIsNone(st["access_expires_at"])

    def test_status_fields_and_no_secrets(self):
        self.write_token(NOW + 1000)
        st = self.src().status()
        self.assertEqual(st["access_expires_at"], NOW + 1000)
        self.assertEqual(st["seconds_left"], 1000)
        self.assertEqual(st["refresh_token_expires_hint"], (NOW + 1000 - 3599) + 604799)
        self.assertIsNone(st["last_refresh_ok"])
        self.assert_no_secrets(json.dumps(st))

    def test_status_hint_none_without_field(self):
        tok = {"access_token": AT_OLD, "expires_at": NOW + 5, "expires_in": 3599, "refresh_token": RT}
        self.tf.write_text(json.dumps(tok))
        self.assertIsNone(self.src().status()["refresh_token_expires_hint"])

    def test_token_url_must_be_https_or_loopback(self):
        with self.assertRaises(ValueError):
            GoogleTokenSource(self.tf, "c", "s", token_url="http://example.com/token")

    def test_token_url_env(self):
        os.environ["GOOGLE_TOKEN_URL"] = self.ep.url
        try:
            self.assertEqual(GoogleTokenSource(self.tf, "c", "s").token_url, self.ep.url)
        finally:
            del os.environ["GOOGLE_TOKEN_URL"]


if __name__ == "__main__":
    unittest.main()
