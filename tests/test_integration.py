"""End-to-end tests against a local Radicale (stand-in for iCloud=A and Google=B): the real
vdirsyncer 0.21 read-only mirror, and the plan/apply engine over the real CalDAV store.
Skipped if vdirsyncer/radicale are absent. Env: VDIRSYNCER_BIN, RADICALE_BIN."""
import os, shutil, socket, subprocess, sys, tempfile, time, unittest, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
VDS = os.environ.get("VDIRSYNCER_BIN") or shutil.which("vdirsyncer")
RAD = os.environ.get("RADICALE_BIN") or shutil.which("radicale")

EV = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//t//EN
BEGIN:VEVENT
UID:{uid}
DTSTAMP:20260101T000000Z
DTSTART:{start}
DTEND:{end}
SUMMARY:{title}
END:VEVENT
END:VCALENDAR
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


@unittest.skipUnless(VDS and RAD, "vdirsyncer/radicale not available")
class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="calsync-it-"))
        cls.port = free_port()
        (cls.tmp / "rad.conf").write_text(
            f"[server]\nhosts = 127.0.0.1:{cls.port}\n[auth]\ntype = none\n[storage]\nfilesystem_folder = {cls.tmp}/rad\n[logging]\nlevel = error\n")
        cls.start_server()
        cls.base = f"http://127.0.0.1:{cls.port}/u/"
        cls.req("MKCOL", cls.base)

    @classmethod
    def start_server(cls):
        cls.proc = subprocess.Popen([RAD, "--config", str(cls.tmp / "rad.conf")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close(); break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(); shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def req(cls, method, url, data=None, headers=None):
        r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r) as x: return x.status
        except urllib.error.HTTPError as e: return e.code

    def setUp(self):
        from calsync import config
        self.n = f"t{time.monotonic_ns()}"
        self.ca, self.cb = f"{self.base}{self.n}a/", f"{self.base}{self.n}b/"
        self.req("MKCALENDAR", self.ca); self.req("MKCALENDAR", self.cb)
        d = self.tmp / self.n
        self.s = config.Settings(data_dir=d / "data", run_dir=d / "run", min_interval_s=0, self_addresses=("me@example.com",))
        self.s.ensure_dirs()
        self.pair = config.Pair(name="cal_x", icloud_collection="x", google_collection="y", mode="mirror")
        self.s.pairs = [self.pair]
        os.environ.update(CALSYNC_TEST_A_URL=self.ca, CALSYNC_TEST_B_URL=self.cb, VDIRSYNCER_BIN=VDS)
        from calsync import runner
        runner.VDIRSYNCER = VDS
        self.runner = runner

    def tearDown(self):
        for k in ("CALSYNC_TEST_A_URL", "CALSYNC_TEST_B_URL"): os.environ.pop(k, None)

    def put(self, cal, uid, title="T"):
        """Create or edit in place (a second resource with the same UID would be a 409)."""
        d = self.tmp / "rad" / "collection-root" / "u" / cal.rstrip("/").split("/")[-1]
        name = next((p.name for p in d.glob("*.ics") if f"UID:{uid}\r\n" in p.read_text() or f"UID:{uid}\n" in p.read_text()), f"{uid}.ics")
        self.assertIn(self.req("PUT", f"{cal}{name}", self.ev(uid, title), {"Content-Type": "text/calendar"}), (201, 204))

    @staticmethod
    def ev(uid, title):
        import zlib
        h = zlib.crc32(uid.encode()) % 28 * 86400 + 1772359200   # distinct start time per uid
        import datetime as dt
        f = lambda x: dt.datetime.fromtimestamp(x, dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return EV.format(uid=uid, title=title if title != "T" else "T-" + uid, start=f(h), end=f(h + 3600)).encode()

    def delete(self, cal, uid): self.req("DELETE", f"{cal}{uid}.ics")

    def remote_uids(self, cal):
        from calsync import ics
        d = self.tmp / "rad" / "collection-root" / "u" / cal.rstrip("/").split("/")[-1]
        return {e.uid for p in d.glob("*.ics") for e in ics.parse(p.read_text())}

    def open_gates(self):
        from calsync import syncrun
        from calsync.util import write_json, now
        self.s.write_enabled = True
        write_json(self.s.state / "backup_ok.json", {"ts": now(), "offhost": True})
        rep = syncrun.audit_pair(self.s, self.pair, syncrun.make_stores(self.s, self.pair, read_only=True), write_plan=True)
        for d in ("a", "b"):
            syncrun.approve_plan(self.s, self.pair, "test", "ok", rep["plan_file"][d], d)

    def reset_backoff(self):
        (self.s.state / "pair_cal_x.json").unlink(missing_ok=True)

    def summaries(self, cal):
        from calsync import ics
        d = self.tmp / "rad" / "collection-root" / "u" / cal.rstrip("/").split("/")[-1]
        return sorted(e.summary for p in d.glob("*.ics") for e in ics.parse(p.read_text()))

    # ---------------------------------------------------------------
    def test_mirror_mode_never_writes_remote(self):
        self.put(self.ca, "a1"); self.put(self.cb, "b1")
        r = self.runner.run_cycle(self.s)
        self.assertNotIn("error", r["cal_x"], r)
        self.assertEqual(self.remote_uids(self.ca), {"a1"}); self.assertEqual(self.remote_uids(self.cb), {"b1"})
        self.assertEqual(r["cal_x"]["counts"], {"a": 1, "b": 1})
        # the advisory engine audit ran read-only and sees both one-sided events
        self.assertEqual(r["cal_x"]["engine_audit"]["stats"]["ops"], {"create_a": 1, "create_b": 1})

    def test_write_gates_closed_blocks_seed(self):
        self.put(self.ca, "a1"); self.put(self.cb, "b1")
        self.pair.mode = "seed"
        r = self.runner.run_cycle(self.s)
        self.assertIn("blocked", r["cal_x"]); self.assertIn("write gates closed", r["cal_x"]["blocked"])
        self.assertEqual(self.remote_uids(self.cb), {"b1"})

    def test_first_write_needs_approved_plan(self):
        self.put(self.ca, "a1")
        from calsync.util import write_json, now
        self.s.write_enabled = True; self.pair.mode = "seed"
        write_json(self.s.state / "backup_ok.json", {"ts": now(), "offhost": True})
        r = self.runner.run_cycle(self.s)
        self.assertIn("no approved plan", r["cal_x"]["blocked"])
        self.assertEqual(self.remote_uids(self.cb), set())

    def test_seed_copies_a_to_b_and_never_writes_a(self):
        self.put(self.ca, "a1"); self.put(self.cb, "b1")
        self.open_gates(); self.pair.mode = "seed"
        r = self.runner.run_cycle(self.s)
        self.assertNotIn("blocked", r["cal_x"], r); self.assertNotIn("error", r["cal_x"], r)
        self.assertEqual(r["cal_x"]["applied"], {"create_b": 1})
        self.assertEqual(self.remote_uids(self.cb), {"a1", "b1"})
        self.assertEqual(self.remote_uids(self.ca), {"a1"})   # b1 NOT pushed into the iCloud side
        self.put(self.cb, "b2"); self.reset_backoff()
        self.runner.run_cycle(self.s)
        self.assertEqual(self.remote_uids(self.ca), {"a1"})

    def test_live_two_cycles_idempotent_then_changes_propagate(self):
        self.put(self.ca, "a1", "from-A"); self.put(self.cb, "b1", "from-B")
        self.open_gates(); self.pair.mode = "live"
        r = self.runner.run_cycle(self.s)["cal_x"]
        self.assertEqual(r["applied"], {"create_a": 1, "create_b": 1}, r)
        self.assertEqual(self.remote_uids(self.ca), {"a1", "b1"}); self.assertEqual(self.remote_uids(self.cb), {"a1", "b1"})
        for _ in range(2):                                    # idempotence: nothing further is written
            self.reset_backoff()
            r = self.runner.run_cycle(self.s)["cal_x"]
            self.assertEqual(r["applied"], {}, r)
        self.assertEqual(self.summaries(self.ca), ["from-A", "from-B"])
        self.put(self.ca, "a1", "A-edited"); self.reset_backoff()
        self.assertEqual(self.runner.run_cycle(self.s)["cal_x"]["applied"], {"update_b": 1})
        self.assertEqual(self.summaries(self.cb), ["A-edited", "from-B"])

    def test_deletion_guard_and_outage_guard(self):
        for i in range(10): self.put(self.ca, f"a{i}")
        self.open_gates(); self.pair.mode = "live"
        self.assertNotIn("blocked", self.runner.run_cycle(self.s)["cal_x"])
        self.assertEqual(len(self.remote_uids(self.cb)), 10)
        for i in range(5): self.delete(self.ca, f"a{i}")      # 5 > limit 3
        self.reset_backoff()
        r = self.runner.run_cycle(self.s)
        self.assertIn("vanished", r["cal_x"]["blocked"])
        self.assertEqual(len(self.remote_uids(self.cb)), 10)
        self.reset_backoff()
        self.assertIn("blocked", self.runner.run_cycle(self.s)["cal_x"])
        from calsync.util import write_json, now
        write_json(self.s.state / "ack_deletes_cal_x.json", {"max": 5, "expires": now() + 600})
        self.reset_backoff()
        r = self.runner.run_cycle(self.s)
        self.assertNotIn("blocked", r["cal_x"], r)
        self.assertEqual(len(self.remote_uids(self.cb)), 10)   # deletions are reported, never executed by default

    def test_empty_remote_is_treated_as_outage(self):
        for i in range(2): self.put(self.ca, f"a{i}")
        self.open_gates(); self.pair.mode = "live"
        self.runner.run_cycle(self.s)
        for i in range(2): self.delete(self.ca, f"a{i}")
        self.reset_backoff()
        r = self.runner.run_cycle(self.s)
        self.assertTrue("ZERO" in r["cal_x"].get("blocked", "") or "completely emptied" in r["cal_x"].get("error", ""), r)
        self.assertEqual(len(self.remote_uids(self.cb)), 2)

    def test_conflict_is_not_resolved(self):
        self.put(self.ca, "c1", "orig")
        self.open_gates(); self.pair.mode = "live"
        self.runner.run_cycle(self.s)
        self.put(self.ca, "c1", "edit-A"); self.put(self.cb, "c1", "edit-B"); self.reset_backoff()
        r = self.runner.run_cycle(self.s)["cal_x"]
        self.assertEqual(r["findings"], {"conflict": 1})
        self.assertEqual((self.summaries(self.ca), self.summaries(self.cb)), (["edit-A"], ["edit-B"]))

    def test_guest_invitation_and_birthday_never_copied(self):
        inv = EV.replace("SUMMARY:{title}", "SUMMARY:{title}\nORGANIZER:mailto:hotel@hotel.example\nATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:me@example.com")
        self.assertIn(self.req("PUT", f"{self.ca}inv.ics", inv.format(uid="inv", title="Hotel", start="20260401T100000Z", end="20260401T110000Z").encode(),
                               {"Content-Type": "text/calendar"}), (201, 204))
        bd = EV.replace("DTSTART:{start}", "DTSTART;VALUE=DATE:20000505").replace("DTEND:{end}", "DTEND;VALUE=DATE:20000506\nRRULE:FREQ=YEARLY")
        self.req("PUT", f"{self.cb}bd.ics", bd.format(uid="bd@google.com", title="Max", start="x", end="y").encode(), {"Content-Type": "text/calendar"})
        self.put(self.ca, "plain")
        self.open_gates(); self.pair.mode = "live"
        self.runner.run_cycle(self.s)
        self.assertEqual(self.remote_uids(self.cb), {"plain", "bd@google.com"})
        self.assertEqual(self.remote_uids(self.ca), {"plain", "inv"})

    def test_provider_outage_then_restart_recovers_without_duplicates(self):
        for i in range(3): self.put(self.ca, f"a{i}")
        self.open_gates(); self.pair.mode = "live"
        type(self).proc.terminate(); type(self).proc.wait()           # provider goes offline
        r = self.runner.run_cycle(self.s)["cal_x"]
        self.assertIn("error", r)
        type(self).start_server()                                    # provider returns; storage persisted
        self.reset_backoff()
        r = self.runner.run_cycle(self.s)["cal_x"]
        self.assertEqual(r.get("applied"), {"create_b": 3}, r)
        self.reset_backoff()
        self.assertEqual(self.runner.run_cycle(self.s)["cal_x"]["applied"], {})
        self.assertEqual(len(self.remote_uids(self.cb)), 3)

    def test_restart_with_fresh_process_state_does_not_resync(self):
        self.put(self.ca, "a1")
        self.open_gates(); self.pair.mode = "live"
        self.runner.run_cycle(self.s)
        import importlib
        importlib.reload(self.runner)                               # simulate container restart
        self.runner.VDIRSYNCER = VDS
        self.reset_backoff()
        self.assertEqual(self.runner.run_cycle(self.s)["cal_x"]["applied"], {})

    def test_pause_switch_and_lock(self):
        self.put(self.ca, "a1"); self.open_gates(); self.pair.mode = "live"
        (self.s.state / "PAUSE").touch()
        self.assertIn("PAUSE", self.runner.run_cycle(self.s)["cal_x"]["blocked"])
        with self.runner.single_instance(self.s):
            with self.assertRaises(self.runner.Blocked):
                self.runner.run_cycle(self.s)

    def test_export_is_read_only_and_complete(self):
        self.put(self.ca, "a1"); self.put(self.ca, "a2")
        out = self.tmp / self.n / "export"
        self.assertEqual(self.runner.export_collection(self.s, "icloud", "ignored", out), 2)
        self.assertEqual(self.remote_uids(self.ca), {"a1", "a2"})
        with self.assertRaises(RuntimeError):      # refuses non-empty target
            self.runner.export_collection(self.s, "icloud", "ignored", out)

    def test_no_prompt_creation_of_missing_collection(self):
        # neither vdirsyncer nor the engine may create a remote calendar implicitly
        self.assertEqual(self.req("PROPFIND", f"{self.base}{self.n}zzz/"), 404)
        os.environ["CALSYNC_TEST_B_URL"] = f"{self.base}{self.n}zzz/"
        r = self.runner.run_cycle(self.s)
        self.assertIn("error", r["cal_x"])
        self.assertEqual(self.req("PROPFIND", f"{self.base}{self.n}zzz/"), 404)


if __name__ == "__main__":
    unittest.main()
