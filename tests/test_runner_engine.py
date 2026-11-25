"""Runner integration with the engine (vdirsyncer mirror stubbed, providers faked)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import MemoryStore, simple

from calsync import config, health, runner, syncrun
from calsync.store import AuthError, StoreError
from calsync.util import now, read_json, write_json


class RunnerEngine(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.s = config.Settings(data_dir=Path(t.name), run_dir=Path(t.name) / "run", min_interval_s=0,
                                 self_addresses=("me@example.com",), audit_interval_s=3600)
        self.s.ensure_dirs()
        self.p = config.Pair(name="cal", icloud_collection="x", google_collection="y", mode="mirror")
        self.s.pairs = [self.p]
        for side in "ab":
            (self.s.mirror / "cal" / side).mkdir(parents=True)
        self.a = MemoryStore("a", {"/a/1.ics": simple("a1")}, read_only=False)
        self.b = MemoryStore("b", {"/b/1.ics": simple("b1", start="20260302T100000Z")})
        patches = [patch.object(runner, "_mirror", lambda *a, **k: None),
                   patch.object(syncrun, "make_stores", lambda s, p, read_only: {"a": self.a, "b": self.b})]
        for x in patches:
            x.start()
            self.addCleanup(x.stop)
        self.alerts = []
        pa = patch.object(runner.alertmod, "alert", lambda *a, **k: self.alerts.append(a[1:3]))
        pa.start()
        self.addCleanup(pa.stop)

    def test_config_loads_engine_settings(self):
        f = Path(self.s.data_dir) / "c.toml"
        f.write_text('[global]\nself_addresses = ["A@x.com", " b@y.com "]\npropagate_deletes = true\nmax_creates_per_run = 3\n'
                     '[[pair]]\nname="cal_x"\nicloud_collection="i"\ngoogle_collection="g"\nmode="mirror"\nbirthday_view_collection="v"\n')
        s = config.load(f, env={"CALSYNC_DATA": str(self.s.data_dir)})
        self.assertEqual(s.self_addresses, ("a@x.com", "b@y.com"))
        self.assertTrue(s.propagate_deletes)
        self.assertEqual(s.max_creates_per_run, 3)
        self.assertEqual(s.pairs[0].birthday_view_collection, "v")
        self.assertFalse(s.write_enabled)

    def test_mirror_mode_runs_audit_read_only_and_records_health(self):
        self.a.read_only = self.b.read_only = True
        r = runner.run_cycle(self.s)["cal"]
        self.assertEqual(r["engine_audit"]["stats"]["ops"], {"create_a": 1, "create_b": 1})
        h = read_json(self.s.state / "health.json")
        self.assertEqual(h["pairs"]["cal"]["engine_audit"]["stats"]["ops"], {"create_a": 1, "create_b": 1})
        self.assertEqual(self.a.writes() + self.b.writes(), [])
        n = len(self.a.log)
        write_json(self.s.state / "pair_cal.json", {"failures": 0, "next_allowed": 0})
        runner.run_cycle(self.s)                       # within audit_interval_s: not repeated
        self.assertEqual(len(self.a.log), n)

    def test_audit_failure_does_not_fail_mirror_run_and_auth_alerts(self):
        self.b.fail["list_items"] = AuthError("refresh token expired/revoked")
        r = runner.run_cycle(self.s)["cal"]
        self.assertNotIn("error", r)
        h = read_json(self.s.state / "health.json")["pairs"]["cal"]
        self.assertIn("AUTH", h["engine_audit_error"])
        self.assertTrue(any(k.startswith("auth_") for k, _ in self.alerts))

    def test_auth_error_in_live_run_blocks_pair_and_degrades_health(self):
        self.p.mode = "live"
        self.s.write_enabled = True
        write_json(self.s.state / "backup_ok.json", {"ts": now(), "offhost": True})
        rep = syncrun.audit_pair(self.s, self.p, {"a": self.a, "b": self.b}, write_plan=True)
        for d in ("a", "b"):
            syncrun.approve_plan(self.s, self.p, "t", "n", rep["plan_file"][d], d)
        self.b.fail["list_items"] = AuthError("invalid_grant")
        r = runner.run_cycle(self.s)["cal"]
        self.assertIn("AUTH", r["error"])
        ok, body = health.evaluate(self.s)
        self.assertFalse(ok)
        self.assertEqual(self.a.writes() + self.b.writes(), [])

    def test_live_run_through_runner_applies_and_second_run_is_noop(self):
        self.p.mode = "live"
        self.s.write_enabled = True
        write_json(self.s.state / "backup_ok.json", {"ts": now(), "offhost": True})
        rep = syncrun.audit_pair(self.s, self.p, {"a": self.a, "b": self.b}, write_plan=True)
        for d in ("a", "b"):
            syncrun.approve_plan(self.s, self.p, "t", "n", rep["plan_file"][d], d)
        r = runner.run_cycle(self.s)["cal"]
        self.assertEqual(r["applied"], {"create_a": 1, "create_b": 1})
        write_json(self.s.state / "pair_cal.json", {"failures": 0, "next_allowed": 0})
        self.assertEqual(runner.run_cycle(self.s)["cal"]["applied"], {})

    def test_plan_approval_requires_matching_sha_and_detects_tampering(self):
        rep = syncrun.audit_pair(self.s, self.p, {"a": self.a, "b": self.b}, write_plan=True)
        with self.assertRaises(runner.Blocked):
            syncrun.approve_plan(self.s, self.p, "t", "n", "deadbeef", "b")
        self.assertEqual(syncrun.approved_by_dst(self.s, self.p), {})
        syncrun.approve_plan(self.s, self.p, "t", "n", rep["plan_file"]["b"], "b")
        self.assertEqual(set(syncrun.approved_by_dst(self.s, self.p)), {"b"})        # direction a stays unapproved
        doc = read_json(syncrun.plan_file(self.s, self.p, "b"))
        doc["sigs"].append("0" * 24)                   # tamper: add an op after approval
        write_json(syncrun.plan_file(self.s, self.p, "b"), doc)
        self.assertEqual(syncrun.approved_by_dst(self.s, self.p), {})
        self.assertEqual(oct(syncrun.plan_file(self.s, self.p, "b").stat().st_mode & 0o777), oct(0o600))

    def test_transient_store_error_in_live_run_backs_off_without_block(self):
        self.p.mode = "live"
        self.s.write_enabled = True
        write_json(self.s.state / "backup_ok.json", {"ts": now(), "offhost": True})
        rep = syncrun.audit_pair(self.s, self.p, {"a": self.a, "b": self.b}, write_plan=True)
        for d in ("a", "b"):
            syncrun.approve_plan(self.s, self.p, "t", "n", rep["plan_file"][d], d)
        self.a.fail["list_items"] = StoreError("503")
        r = runner.run_cycle(self.s)["cal"]
        self.assertIn("error", r)
        self.assertEqual(self.a.writes() + self.b.writes(), [])


if __name__ == "__main__":
    unittest.main()
