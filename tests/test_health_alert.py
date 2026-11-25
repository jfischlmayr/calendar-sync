"""health.py (evaluate + HTTP endpoint) and alert.py (dedupe, recovery, webhook,
redaction). Tempdirs, loopback stub servers and fake env secrets only."""
import json, os, shutil, socket, sys, tempfile, threading, unittest, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from calsync import alert, config, health  # noqa: E402
from calsync.util import now, read_json, write_json  # noqa: E402

FAKE_ENV = {
    "ICLOUD_USER": "someone@icloud.example",
    "ICLOUD_APP_PASSWORD": "abcd-efgh-ijkl-mnop",
    "GOOGLE_CLIENT_SECRET": "GOCSPX-fake-client-secret",
    "SMTP_PASSWORD": "smtp-pass-fake-123",
}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="calsync-ha-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.s = config.Settings(data_dir=self.tmp / "data", run_dir=self.tmp / "run",
                                 interval_s=300, alert_repeat_s=3600)
        self.s.ensure_dirs()
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("ALERT_WEBHOOK_URL", "ALERT_WEBHOOK_URL_FILE", "SMTP_HOST", "ALERT_EMAIL_TO")
                 and not k.endswith("_FILE")}
        clean.update(FAKE_ENV)
        p = mock.patch.dict(os.environ, clean, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def pair(self, name="cal_a", mode="live"):
        p = config.Pair(name=name, icloud_collection="x", google_collection="y", mode=mode)
        self.s.pairs.append(p)
        return p


class Evaluate(Base):
    def test_inert_without_active_pairs(self):
        ok, body = health.evaluate(self.s)
        self.assertTrue(ok)
        self.assertEqual(body["state"], "inert")
        self.pair("cal_off", mode="disabled")     # disabled pairs do not count
        ok, body = health.evaluate(self.s)
        self.assertTrue(ok)
        self.assertEqual(body["state"], "inert")

    def test_degraded_when_never_succeeded(self):
        self.pair()
        ok, body = health.evaluate(self.s)
        self.assertFalse(ok)
        self.assertEqual(body["state"], "degraded")

    def test_ok_when_fresh(self):
        self.pair()
        health.update(self.s, "cal_a", last_success=now())
        ok, body = health.evaluate(self.s)
        self.assertTrue(ok)
        self.assertEqual(body["state"], "ok")

    def test_degraded_when_stale(self):
        self.pair()
        # threshold = max(3*interval, 900) = 900s with interval 300
        health.update(self.s, "cal_a", last_success=now() - 901)
        self.assertFalse(health.evaluate(self.s)[0])
        health.update(self.s, "cal_a", last_success=now() - 800)
        self.assertTrue(health.evaluate(self.s)[0])
        # larger interval raises the threshold
        self.s.interval_s = 1200
        health.update(self.s, "cal_a", last_success=now() - 3000)
        self.assertTrue(health.evaluate(self.s)[0])
        health.update(self.s, "cal_a", last_success=now() - 3700)
        self.assertFalse(health.evaluate(self.s)[0])

    def test_degraded_when_blocked_even_if_fresh(self):
        self.pair()
        health.update(self.s, "cal_a", last_success=now(), blocked="deletion guard")
        ok, body = health.evaluate(self.s)
        self.assertFalse(ok)
        self.assertEqual(body["state"], "degraded")
        health.update(self.s, "cal_a", blocked=None)
        self.assertTrue(health.evaluate(self.s)[0])

    def test_one_bad_pair_degrades_all(self):
        self.pair("cal_a"); self.pair("cal_b")
        health.update(self.s, "cal_a", last_success=now())
        self.assertFalse(health.evaluate(self.s)[0])
        health.update(self.s, "cal_b", last_success=now())
        self.assertTrue(health.evaluate(self.s)[0])

    def test_update_global_and_pair_fields(self):
        health.update(self.s, None, started=1.0)
        health.update(self.s, "cal_a", last_error="x")
        h = read_json(self.s.state / "health.json")
        self.assertEqual(h["started"], 1.0)
        self.assertEqual(h["pairs"]["cal_a"]["last_error"], "x")
        self.assertIn("updated", h)


class Endpoint(Base):
    def setUp(self):
        super().setUp()
        self.s.health_port = free_port()
        self.srv = health.serve(self.s)
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.s.health_port}{path}", timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            e.close()
            return e.code, body

    def test_healthz_inert_200(self):
        code, body = self.get("/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {"ok": True, "state": "inert"})

    def test_healthz_codes_follow_evaluate(self):
        self.pair()
        self.assertEqual(self.get("/healthz")[0], 503)        # never succeeded
        health.update(self.s, "cal_a", last_success=now())
        code, body = self.get("/healthz")
        self.assertEqual((code, json.loads(body)), (200, {"ok": True, "state": "ok"}))
        health.update(self.s, "cal_a", blocked="guard")
        code, body = self.get("/healthz")
        self.assertEqual((code, json.loads(body)["state"]), (503, "degraded"))
        health.update(self.s, "cal_a", blocked=None, last_success=now() - 5000)
        self.assertEqual(self.get("/healthz")[0], 503)

    def test_status_is_200_even_when_degraded(self):
        self.pair()
        code, body = self.get("/status")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["state"], "degraded")

    def test_unknown_path_404(self):
        self.assertEqual(self.get("/nope")[0], 404)
        self.assertEqual(self.get("/")[0], 404)

    def test_status_contains_no_env_secrets(self):
        self.pair()
        health.update(self.s, None, started=now(), writes_enabled=True)
        health.update(self.s, "cal_a", last_success=now(), counts={"a": 3, "b": 3}, changes=0)
        os.environ["ALERT_WEBHOOK_URL"] = "http://hooks.example/secret-token-98765"
        for path in ("/status", "/healthz"):
            code, body = self.get(path)
            self.assertEqual(code, 200)
            for k, v in {**FAKE_ENV, "ALERT_WEBHOOK_URL": os.environ["ALERT_WEBHOOK_URL"]}.items():
                self.assertNotIn(v, body, f"{k} leaked via {path}")
        # status exposes state only, not config / paths
        body = self.get("/status")[1]
        self.assertNotIn(str(self.tmp), body)


class WebhookStub:
    def __init__(self, status=200):
        self.posts = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.posts.append({"path": self.path, "headers": dict(self.headers), "raw": self.rfile.read(n)})
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def json(self, i=-1):
        return json.loads(self.posts[i]["raw"])


class Alerts(Base):
    def setUp(self):
        super().setUp()
        self.hook = WebhookStub()
        self.addCleanup(self.hook.close)
        self.url = f"http://127.0.0.1:{self.hook.port}/hook-TOKEN7788"
        os.environ["ALERT_WEBHOOK_URL"] = self.url
        self.t = [1_000_000.0]
        p = mock.patch.object(alert, "now", lambda: self.t[0])
        p.start()
        self.addCleanup(p.stop)

    def test_webhook_payload(self):
        self.assertTrue(alert.alert(self.s, "k1", "subj", "the body"))
        self.assertEqual(len(self.hook.posts), 1)
        p = self.hook.posts[0]
        self.assertEqual(p["path"], "/hook-TOKEN7788")
        self.assertEqual(self.hook.json(), {"title": "[calsync] subj", "message": "the body",
                                            "text": "[calsync] subj\nthe body"})
        self.assertEqual(p["headers"]["Title"], "[calsync] subj")

    def test_dedupe_within_repeat_window(self):
        self.assertTrue(alert.alert(self.s, "k1", "s", "b"))
        self.t[0] += 3599
        self.assertFalse(alert.alert(self.s, "k1", "s", "b"))
        self.assertEqual(len(self.hook.posts), 1)
        self.t[0] += 2                                   # now beyond alert_repeat_s
        self.assertTrue(alert.alert(self.s, "k1", "s", "b"))
        self.assertEqual(len(self.hook.posts), 2)

    def test_dedupe_is_per_key(self):
        self.assertTrue(alert.alert(self.s, "k1", "s", "b"))
        self.assertTrue(alert.alert(self.s, "k2", "s", "b"))
        self.assertFalse(alert.alert(self.s, "k1", "s", "b"))
        self.assertEqual(len(self.hook.posts), 2)

    def test_dedupe_state_persisted_on_disk(self):
        alert.alert(self.s, "k1", "s", "b")
        self.assertEqual(read_json(self.s.state / "alerts.json"), {"k1": self.t[0]})

    def test_recovery_notice(self):
        alert.alert(self.s, "k1", "FAIL x", "broken")
        self.assertTrue(alert.alert(self.s, "k1", "x", "healthy again", recovery=True))
        self.assertEqual(len(self.hook.posts), 2)
        self.assertEqual(self.hook.json()["title"], "[calsync] RECOVERED x")
        self.assertEqual(self.hook.json()["message"], "healthy again")
        self.assertEqual(read_json(self.s.state / "alerts.json"), {})
        # after recovery the next failure alerts immediately (no dedupe carry-over)
        self.assertTrue(alert.alert(self.s, "k1", "FAIL x", "broken again"))
        self.assertEqual(len(self.hook.posts), 3)

    def test_recovery_without_prior_alert_is_silent(self):
        self.assertTrue(alert.alert(self.s, "k1", "x", "ok", recovery=True))
        self.assertEqual(self.hook.posts, [])
        # second recovery after a recovery is also silent
        alert.alert(self.s, "k1", "F", "b")
        alert.alert(self.s, "k1", "x", "ok", recovery=True)
        n = len(self.hook.posts)
        alert.alert(self.s, "k1", "x", "ok", recovery=True)
        self.assertEqual(len(self.hook.posts), n)

    def test_secret_env_values_redacted_in_body(self):
        body = (f"login {FAKE_ENV['ICLOUD_USER']} pw={FAKE_ENV['ICLOUD_APP_PASSWORD']} "
                f"g={FAKE_ENV['GOOGLE_CLIENT_SECRET']} smtp={FAKE_ENV['SMTP_PASSWORD']} url={self.url}")
        alert.alert(self.s, "k1", "s", body)
        raw = self.hook.posts[0]["raw"].decode()
        for v in (*FAKE_ENV.values(), self.url):
            self.assertNotIn(v, raw)
        self.assertIn("***", self.hook.json()["message"])
        self.assertIn("pw=***", self.hook.json()["message"])

    def test_secret_env_values_redacted_in_recovery_body(self):
        alert.alert(self.s, "k1", "s", "b")
        alert.alert(self.s, "k1", "s", f"ok {FAKE_ENV['ICLOUD_APP_PASSWORD']}", recovery=True)
        self.assertNotIn(FAKE_ENV["ICLOUD_APP_PASSWORD"], self.hook.posts[-1]["raw"].decode())

    def test_short_secret_values_not_redacted(self):
        # documented redact() behaviour: values < 4 chars are left alone (avoid mangling text)
        os.environ["SMTP_PASSWORD"] = "ab"
        alert.alert(self.s, "k1", "s", "abc")
        self.assertEqual(self.hook.json()["message"], "abc")

    def test_secret_env_values_redacted_in_subject(self):
        """alert() only redacts the body; the subject goes out verbatim (callers currently
        pass safe subjects, so this is latent). Remove expectedFailure once fixed."""
        alert.alert(self.s, "k1", f"fail {FAKE_ENV['ICLOUD_APP_PASSWORD']}", "b")
        self.assertNotIn(FAKE_ENV["ICLOUD_APP_PASSWORD"], self.hook.posts[0]["raw"].decode())

    # ---- delivery failure ---------------------------------------------------
    def test_no_exception_when_webhook_unreachable(self):
        os.environ["ALERT_WEBHOOK_URL"] = f"http://127.0.0.1:{free_port()}/x-SECRETPATH"
        with self.assertLogs("calsync", "ERROR") as cm:
            r = alert.alert(self.s, "k1", "s", f"b {FAKE_ENV['ICLOUD_APP_PASSWORD']}")
            alert.alert(self.s, "k1", "s", "b", recovery=True)
        self.assertTrue(r)
        self.assertNotIn(FAKE_ENV["ICLOUD_APP_PASSWORD"], "\n".join(cm.output))
        self.assertIn("webhook alert failed", "\n".join(cm.output))

    def test_no_exception_when_webhook_returns_500(self):
        bad = WebhookStub(status=500)
        self.addCleanup(bad.close)
        os.environ["ALERT_WEBHOOK_URL"] = f"http://127.0.0.1:{bad.port}/h"
        with self.assertLogs("calsync", "ERROR"):
            self.assertTrue(alert.alert(self.s, "k1", "s", "b"))
        self.assertEqual(len(bad.posts), 1)

    def test_no_exception_without_any_channel(self):
        del os.environ["ALERT_WEBHOOK_URL"]
        with self.assertLogs("calsync", "ERROR") as cm:
            self.assertTrue(alert.alert(self.s, "k1", "s", f"b {FAKE_ENV['ICLOUD_APP_PASSWORD']}"))
        self.assertNotIn(FAKE_ENV["ICLOUD_APP_PASSWORD"], "\n".join(cm.output))

    def test_no_exception_when_smtp_unreachable(self):
        del os.environ["ALERT_WEBHOOK_URL"]
        os.environ.update(SMTP_HOST="127.0.0.1", SMTP_PORT=str(free_port()), ALERT_EMAIL_TO="a@b.example")
        with self.assertLogs("calsync", "ERROR") as cm:
            self.assertTrue(alert.alert(self.s, "k1", "s", "b"))
        self.assertIn("smtp alert failed", "\n".join(cm.output))

    def test_failed_delivery_should_not_suppress_retry(self):
        """alert() ignores _send()'s result and stamps the dedupe time even when NO channel
        delivered, so a transient webhook outage silences the alert for alert_repeat_s.
        Remove expectedFailure once fixed."""
        os.environ["ALERT_WEBHOOK_URL"] = f"http://127.0.0.1:{free_port()}/dead"
        with self.assertLogs("calsync", "ERROR"):
            alert.alert(self.s, "k1", "s", "b")
        os.environ["ALERT_WEBHOOK_URL"] = self.url           # webhook is back
        self.t[0] += 60
        alert.alert(self.s, "k1", "s", "b")
        self.assertEqual(len(self.hook.posts), 1)


if __name__ == "__main__":
    unittest.main()
