"""Release-candidate invariants: inert defaults, no published port, loopback health endpoint,
private restore permissions, stable non-security SHA-1 identifiers, alert URL scheme check.
Synthetic data only; loopback only; no network."""
import hashlib
import os
import re
import socket
import stat
import sys
import tempfile
import tomllib
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from calsync import alert, backup, config, engine, health  # noqa: E402
from calsync.caldav import CalDAVStore  # noqa: E402
from test_backup import make_data, make_keypair  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Defaults(unittest.TestCase):
    def test_example_config_is_inert_and_generic(self):
        raw = tomllib.loads((ROOT / "config" / "calsync.example.toml").read_text())
        self.assertTrue(raw["pair"])
        self.assertTrue(all(p["mode"] == "disabled" for p in raw["pair"]))
        for a in raw["global"]["self_addresses"]:
            self.assertRegex(a, r"@(gmail|icloud)\.example$")
        s = config.load(ROOT / "config" / "calsync.example.toml", env={})
        self.assertFalse(s.write_enabled)
        self.assertEqual(s.health_bind, "127.0.0.1")
        self.assertFalse(s.health_status_public)

    def test_compose_publishes_no_port_and_defaults_writes_off(self):
        text = (ROOT / "compose.yaml").read_text()
        code = "\n".join(re.sub(r"\s+#.*$", "", l) for l in text.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotRegex(code, r"(?m)^\s*(ports|expose):")
        self.assertIn("CALSYNC_ALLOW_WRITES: ${CALSYNC_ALLOW_WRITES:-0}", code)
        self.assertIn("TZ: ${TZ:-UTC}", code)
        self.assertNotIn("Europe/", code)

    def test_compose_env_names_match_env_example(self):
        code = (ROOT / "compose.yaml").read_text()
        example = (ROOT / ".env.example").read_text()
        declared = {m.group(1) for m in re.finditer(r"(?m)^([A-Z][A-Z0-9_]*)=", example)}
        for target, source in re.findall(r"(?m)^\s+([A-Z][A-Z0-9_]*): \$\{([A-Z][A-Z0-9_]*)", code):
            if target.startswith(("ICLOUD", "GOOGLE_CLIENT", "CALSYNC_ALLOW", "CALSYNC_INTERVAL")):
                self.assertEqual(target, source, "compose must pass the variable the app reads")
                self.assertIn(source, declared, f"{source} missing from .env.example")

    def test_dockerfile_does_not_expose_a_port_or_bake_private_files(self):
        text = (ROOT / "Dockerfile").read_text()
        self.assertNotRegex(text, r"(?m)^\s*EXPOSE\b")
        copies = re.findall(r"(?m)^COPY\s+(.+)$", text)
        self.assertFalse([c for c in copies if re.search(r"\.env|\.age|tokens|private|readonly-production", c)])
        self.assertIn("config/calsync.example.toml", text)


class HealthEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="calsync-hh-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.s = config.Settings(data_dir=self.tmp / "data", run_dir=self.tmp / "run")
        self.s.ensure_dirs()
        self.s.health_port = free_port()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.s.health_port}{path}", timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def run_server(self):
        srv = health.serve(self.s)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return srv

    def test_default_bind_is_loopback_and_status_available_there(self):
        srv = self.run_server()
        self.assertEqual(srv.server_address[0], "127.0.0.1")
        self.assertEqual(self.get("/healthz"), 200)
        self.assertEqual(self.get("/status"), 200)

    def test_status_hidden_on_non_loopback_bind_unless_opted_in(self):
        self.s.health_bind = "0.0.0.0"
        self.run_server()
        self.assertEqual(self.get("/healthz"), 200)
        self.assertEqual(self.get("/status"), 404)

    def test_status_served_on_non_loopback_with_explicit_opt_in(self):
        self.s.health_bind, self.s.health_status_public = "0.0.0.0", True
        self.run_server()
        self.assertEqual(self.get("/status"), 200)

    def test_env_controls_bind(self):
        s = config.load("/nonexistent.toml", env={"CALSYNC_HEALTH_BIND": "0.0.0.0", "CALSYNC_HEALTH_STATUS": "1"})
        self.assertEqual((s.health_bind, s.health_status_public), ("0.0.0.0", True))


class RestorePermissions(unittest.TestCase):
    def test_restored_directories_are_private(self):
        tmp = Path(tempfile.mkdtemp(prefix="calsync-rp-"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        ident, pub = make_keypair(tmp)
        data = make_data(tmp)
        arch = backup.create_backup(data, tmp / "out", [pub])
        target = tmp / "restored"
        backup.restore_backup(arch, ident, target)
        for p in [target, *[d for d in target.rglob("*") if d.is_dir()]]:
            self.assertEqual(stat.S_IMODE(p.stat().st_mode) & 0o077, 0, f"{p} is group/world accessible")
        for f in target.rglob("*"):
            if f.is_file():
                self.assertEqual(stat.S_IMODE(f.stat().st_mode) & 0o077, 0, f"{f} is group/world accessible")


class StableIdentifiers(unittest.TestCase):
    """SHA-1 derives identifiers only (not a security control). The values must never change."""

    def test_link_id_is_unchanged(self):
        self.assertEqual(engine.link_id("a-uid", "b-uid"), hashlib.sha1(b"a-uid\x00b-uid").hexdigest()[:16])

    def test_resource_name_is_unchanged(self):
        uid = "uid-1@example.invalid"
        want = "calsync-" + hashlib.sha1(uid.encode()).hexdigest()[:24] + ".ics"
        calls = []

        class Http:
            def send(self, method, url, body=None, headers=None, expect=()):
                calls.append(url)
                raise RuntimeError("stop")

        st = CalDAVStore("t", "http://127.0.0.1:1/cal/", lambda: {})
        st._http = Http()
        st._guard_write = lambda: None
        with self.assertRaises(RuntimeError):
            st.create(b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:" + uid.encode() + b"\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        self.assertTrue(calls and calls[0].endswith("/cal/" + want))


class AlertScheme(unittest.TestCase):
    def test_non_http_webhook_scheme_is_not_opened(self):
        with mock.patch.dict(os.environ, {"ALERT_WEBHOOK_URL": "file:///etc/passwd"}, clear=True), \
                mock.patch("urllib.request.urlopen") as uo:
            self.assertEqual(alert._send("s", "b"), [])
            uo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
