"""Regression tests: a transient provider failure must not leave the read-only vdirsyncer mirror in a state
that makes vdirsyncer try to write the (read-only) remote ("Attempted change on ... read-only"/PartialSync).

Part 1 (pure filesystem, no binaries): scoped, inode-preserving snapshot/rollback.
Part 2 (real vdirsyncer 0.21 + Radicale, skipped if absent): the production failure and its recovery.
No live calendars, no credentials; the remote is a throw-away local Radicale and every test asserts that the
remote was never written."""
import hashlib, os, shutil, sqlite3, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_integration as TI                                   # noqa: E402  (real-Radicale scaffolding)
from calsync import config, mirrorsafe, runner                  # noqa: E402
from calsync.util import write_json, now                        # noqa: E402


def _fp(root: Path) -> dict:
    return {f.relative_to(root).as_posix(): (f.stat().st_mtime_ns, f.stat().st_ino, f.read_bytes())
            for f in sorted(root.rglob("*")) if f.is_file()}


class ScopedRollbackUnit(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.s = config.Settings(data_dir=Path(t.name) / "data", run_dir=Path(t.name) / "run")
        self.s.ensure_dirs()
        self.p = config.Pair(name="cal", icloud_collection="x", google_collection="y", mode="mirror")
        self.q = config.Pair(name="cal_a", icloud_collection="x", google_collection="y", mode="mirror")  # name-prefix neighbour
        for pr in (self.p, self.q):
            for side in "ab":
                d = self.s.mirror / pr.name / side
                d.mkdir(parents=True)
                (d / "1.ics").write_text(f"{pr.name}{side}-old")
                (self.s.status / "mirror").mkdir(parents=True, exist_ok=True)
                (self.s.status / "mirror" / f"m_{pr.name}_{side}.items").write_text(f"status-{pr.name}{side}-old")
                (self.s.status / "mirror" / f"m_{pr.name}_{side}.collections").write_text("c")

    def test_status_entries_are_exact_name_matches(self):
        root = self.s.status / "mirror"
        names = {e.name for e in mirrorsafe.side_status_entries(root, self.p, "a")}
        self.assertEqual(names, {"m_cal_a.items", "m_cal_a.collections"})      # NOT m_cal_a_a.* of pair cal_a
        self.assertEqual({e.name for e in mirrorsafe.side_status_entries(root, self.p, "a", keep_collections=False)}, {"m_cal_a.items"})

    def test_rollback_restores_same_inodes_and_only_touches_this_pair(self):
        before_mirror = _fp(self.s.mirror / "cal")
        other_status = _fp(self.s.status / "mirror")
        other_before = {k: v for k, v in other_status.items() if "cal_a_" in k}
        other_mirror = _fp(self.s.mirror / "cal_a")
        snap = mirrorsafe.snapshot(self.s, self.p)
        # simulate a half-finished run: a rewritten (new inode) file, a new file, a deleted file, new status content
        f = self.s.mirror / "cal" / "a" / "1.ics"
        f.unlink(); f.write_text("changed")
        (self.s.mirror / "cal" / "a" / "2.ics").write_text("new")
        (self.s.mirror / "cal" / "b" / "1.ics").unlink()
        (self.s.status / "mirror" / "m_cal_a.items").write_text("status-new")
        (self.s.status / "mirror" / "m_cal_b").mkdir()
        (self.s.status / "mirror" / "m_cal_b" / "cal.items").write_text("created by failed run")
        (self.s.mirror / "cal_a" / "a" / "1.ics").write_text("neighbour pair modified meanwhile")
        bad = mirrorsafe.rollback(self.s, self.p, snap)
        self.assertEqual(bad, [])
        self.assertEqual(_fp(self.s.mirror / "cal"), before_mirror)            # content, mtime AND inode identical
        self.assertFalse((self.s.status / "mirror" / "m_cal_b").exists())
        self.assertEqual((self.s.status / "mirror" / "m_cal_a.items").read_text(), "status-cal-a-old".replace("cal-a", "cala"))
        self.assertEqual({k: v for k, v in _fp(self.s.status / "mirror").items() if "cal_a_" in k}, other_before)
        self.assertEqual((self.s.mirror / "cal_a" / "a" / "1.ics").read_text(), "neighbour pair modified meanwhile")
        self.assertNotEqual(_fp(self.s.mirror / "cal_a"), other_mirror)        # untouched by rollback of `cal`
        self.assertFalse(mirrorsafe.pending_rebuild(self.s, self.p))

    def test_copy_fallback_is_detected_and_marks_rebuild(self):
        with patch.object(mirrorsafe.os, "link", side_effect=OSError("no hardlinks")):
            snap = mirrorsafe.snapshot(self.s, self.p)
        self.assertFalse(snap.linked)
        # a restore of copies yields new inodes -> status cannot be trusted -> rebuild pending for both sides
        self.assertEqual(sorted(mirrorsafe.rollback(self.s, self.p, snap)), ["a", "b"])
        self.assertEqual(set(mirrorsafe.pending_rebuild(self.s, self.p)), {"a", "b"})
        self.assertEqual(mirrorsafe.pending_rebuild(self.s, self.q), {})

    def test_first_run_without_mirror_is_not_rolled_back(self):
        shutil.rmtree(self.s.mirror / "cal")
        snap = mirrorsafe.snapshot(self.s, self.p)
        (self.s.mirror / "cal" / "a").mkdir(parents=True)
        (self.s.mirror / "cal" / "a" / "x.ics").write_text("partial first fetch")
        self.assertEqual(set(mirrorsafe.rollback(self.s, self.p, snap)), {"a", "b"})
        self.assertEqual(set(mirrorsafe.pending_rebuild(self.s, self.p)), {"a", "b"})
        self.assertTrue((self.s.mirror / "cal" / "a" / "x.ics").exists())

    def test_runner_rollback_never_raises_and_marks_rebuild(self):
        snap = mirrorsafe.snapshot(self.s, self.p)
        with patch.object(mirrorsafe, "rollback", side_effect=OSError("disk full")):
            runner._rollback(self.s, self.p, snap)
        self.assertEqual(set(mirrorsafe.pending_rebuild(self.s, self.p)), {"a", "b"})

    def test_interrupted_swap_is_recovered(self):
        trash = mirrorsafe.scratch(self.s) / "trash-cal-a"
        os.rename(self.s.mirror / "cal" / "a", trash)             # crash between the two renames
        mirrorsafe.recover_swaps(self.s, self.p)
        self.assertEqual((self.s.mirror / "cal" / "a" / "1.ics").read_text(), "cala-old")
        self.assertFalse(trash.exists())

    def test_interrupted_pair_rollback_is_recovered(self):
        trash = mirrorsafe.scratch(self.s) / "trash-cal"
        os.rename(self.s.mirror / "cal", trash)
        mirrorsafe.recover_swaps(self.s, self.p)
        self.assertEqual((self.s.mirror / "cal" / "a" / "1.ics").read_text(), "cala-old")
        self.assertEqual(set(mirrorsafe.pending_rebuild(self.s, self.p)), {"a", "b"})
        self.assertFalse(trash.exists())

    def test_scratch_is_outside_the_backup_set(self):
        from calsync import backup
        self.assertEqual(mirrorsafe.scratch(self.s).parent, self.s.backups)
        self.assertNotIn("backups", backup.INCLUDE_DIRS)


@unittest.skipUnless(TI.VDS and TI.RAD, "vdirsyncer/radicale not available")
class MirrorRollbackReal(unittest.TestCase):
    """Reuses the Radicale scaffolding of test_integration without re-running its tests."""
    for _n in ("setUpClass", "start_server", "tearDownClass", "req", "setUp", "tearDown", "put", "ev", "delete",
               "remote_uids", "reset_backoff"):
        locals()[_n] = TI.Integration.__dict__[_n]
    del _n

    def setUp(self):
        TI.Integration.__dict__["setUp"](self)
        self.pair2 = config.Pair(name="cal_y", icloud_collection="x", google_collection="y", mode="mirror")

    # ---- helpers
    def remote_state(self):
        """Hash of everything Radicale stores: any write by vdirsyncer/calsync would change it."""
        h = hashlib.sha256()
        for f in sorted((self.tmp / "rad").rglob("*")):
            if f.is_file():
                h.update(f.relative_to(self.tmp / "rad").as_posix().encode() + f.read_bytes())
        return h.hexdigest()

    def cycle(self, pair=None):
        write_json(self.s.state / f"pair_{(pair or self.pair).name}.json", {"failures": 0, "next_allowed": 0})
        return self.runner.run_pair(self.s, pair or self.pair)

    def seed(self, n=3):
        for i in range(n):
            self.put(self.ca, f"a{i}"); self.put(self.cb, f"b{i}")

    def stale_hash(self, pair, side, ident):
        """What the first production failure needed: a status hash_b that does not match the file. The
        matching etag (mtime;inode) hides it until a rollback changes the inode."""
        con = sqlite3.connect(self.s.status / "mirror" / f"m_{pair.name}_{side}.items")
        con.execute("update status set hash_b='stale-or-legacy-hash' where ident=?", (ident,)); con.commit(); con.close()

    def fail_b(self):
        class Ctx:
            def __enter__(c): self.good = os.environ["CALSYNC_TEST_B_URL"]; os.environ["CALSYNC_TEST_B_URL"] = "http://127.0.0.1:1/x/"
            def __exit__(c, *a): os.environ["CALSYNC_TEST_B_URL"] = self.good
        return Ctx()

    def mirror_fp(self, pair=None):
        return _fp(self.s.mirror / (pair or self.pair).name)

    def assert_healthy(self, r):
        self.assertNotIn("error", r, r)
        self.assertNotIn("blocked", r, r)

    # ---- tests
    def test_old_behaviour_reproduced_by_copytree_restore(self):
        """Documents the root cause: restoring with copytree (new inodes) turns a masked stale hash into a
        permanent PartialSync error. The new code must never produce this state by itself."""
        import shutil as sh
        self.seed(); self.assert_healthy(self.cycle())
        self.stale_hash(self.pair, "b", "b0")
        self.assert_healthy(self.cycle())                          # masked while the etag matches
        mdir = self.s.mirror / "cal_x"
        sh.copytree(mdir, mdir.with_name("copy")); sh.rmtree(mdir); sh.copytree(mdir.with_name("copy"), mdir)
        before = self.remote_state()
        with patch.object(runner, "_heal_pending", lambda *a, **k: None), patch.object(runner.mirrorsafe, "snapshot", lambda s, p: None):
            r = self.cycle()
        self.assertIn("read-only", r["error"])                  # == "Attempted change on ... which is read-only"
        self.assertEqual(self.remote_state(), before)           # vdirsyncer refused: the remote was never written

    def test_transient_failure_after_one_side_mirrored_then_retry_recovers(self):
        self.seed(); self.assert_healthy(self.cycle())
        self.stale_hash(self.pair, "b", "b0")                    # latent condition masked by (mtime;inode)
        self.assert_healthy(self.cycle())
        self.put(self.ca, "a0", "edited-A"); self.put(self.ca, "a7")
        before_remote, before_fp = self.remote_state(), self.mirror_fp()
        with self.fail_b():
            r = self.cycle()                                       # A mirrors fine, B hits a transient failure
        self.assertIn("error", r); self.assertNotIn("read-only", r["error"])
        self.assertEqual(self.mirror_fp(), before_fp)            # rollback restored content, mtime AND inodes
        self.assertEqual(mirrorsafe.pending_rebuild(self.s, self.pair), {})
        r = self.cycle()                                           # retry: no PartialSync, picks up the A edits
        self.assert_healthy(r); self.assertNotIn("mirror_rebuilt", r)
        self.assertEqual(r["counts"], {"a": 4, "b": 3})
        self.assertEqual(self.remote_state(), before_remote)     # never a remote write
        self.assert_healthy(self.cycle())

    def test_non_exact_rollback_forces_scoped_rebuild_and_recovers(self):
        self.seed(); self.assert_healthy(self.cycle())
        self.stale_hash(self.pair, "b", "b0")
        self.assert_healthy(self.cycle())
        self.cycle(self.pair2)                                     # an unrelated healthy pair
        other_status, other_mirror = _fp(self.s.status / "mirror"), self.mirror_fp(self.pair2)
        other_status = {k: v for k, v in other_status.items() if "cal_y" in k}
        before_remote = self.remote_state()
        with self.fail_b(), patch.object(mirrorsafe.os, "link", side_effect=OSError("no hardlinks")):
            r = self.cycle()
        self.assertIn("error", r)
        self.assertEqual(set(mirrorsafe.pending_rebuild(self.s, self.pair)), {"a", "b"})   # inodes changed -> status not trusted
        r = self.cycle()
        self.assert_healthy(r)
        self.assertEqual(set(r["mirror_rebuilt"]), {"a", "b"})
        self.assertEqual(r["counts"], {"a": 3, "b": 3})
        self.assertEqual(mirrorsafe.pending_rebuild(self.s, self.pair), {})
        self.assertEqual(list(mirrorsafe.scratch(self.s).iterdir()), [])        # staging cleaned up
        r = self.cycle(); self.assert_healthy(r); self.assertNotIn("mirror_rebuilt", r)   # steady state: no refetch
        self.assertEqual(self.remote_state(), before_remote)
        self.assertEqual({k: v for k, v in _fp(self.s.status / "mirror").items() if "cal_y" in k}, other_status)
        self.assertEqual(self.mirror_fp(self.pair2), other_mirror)

    def test_partial_sync_triggers_one_immediate_read_only_self_heal(self):
        """State that production already has (damaged status, rollback long done): healed in the same run."""
        self.seed(); self.assert_healthy(self.cycle())
        self.stale_hash(self.pair, "b", "b0")
        mdir = self.s.mirror / "cal_x"                           # new inodes, as the old rollback left them
        shutil.copytree(mdir, mdir.with_name("copy")); shutil.rmtree(mdir); shutil.copytree(mdir.with_name("copy"), mdir)
        shutil.rmtree(mdir.with_name("copy"))
        before = self.remote_state()
        r = self.cycle()
        self.assert_healthy(r)
        self.assertEqual(set(r["mirror_rebuilt"]), {"b"})        # only the affected side
        self.assertEqual(r["counts"], {"a": 3, "b": 3})
        self.assertEqual(self.remote_state(), before)
        r = self.cycle(); self.assert_healthy(r); self.assertNotIn("mirror_rebuilt", r)

    def test_self_heal_is_bounded_when_rebuild_does_not_cure_partial_sync(self):
        self.seed(); self.assert_healthy(self.cycle())
        self.stale_hash(self.pair, "b", "b0")
        mdir = self.s.mirror / "cal_x"
        shutil.copytree(mdir, mdir.with_name("copy")); shutil.rmtree(mdir); shutil.copytree(mdir.with_name("copy"), mdir)
        shutil.rmtree(mdir.with_name("copy"))
        before, calls = self.remote_state(), []
        with patch.object(runner, "_rebuild_side", lambda *a, **k: calls.append(a[2]) or {}):
            r = self.cycle()
        self.assertIn("read-only", r["error"])
        self.assertEqual(calls, ["b"])                            # exactly one heal attempt, no loop
        self.assertEqual(self.remote_state(), before)
        self.assertEqual(self.cycle().get("error"), None)         # next cycle (real rebuild) heals it

    def test_failed_rebuild_keeps_previous_mirror_and_status(self):
        self.seed(); self.assert_healthy(self.cycle())
        mirrorsafe.mark_rebuild(self.s, self.pair, "b", "test")
        fp, st, before = self.mirror_fp(), _fp(self.s.status / "mirror"), self.remote_state()
        with self.fail_b():
            r = self.cycle()
        self.assertIn("rebuild", r["error"]); self.assertIn("previous mirror kept", r["error"])
        self.assertEqual(self.mirror_fp(), fp)
        self.assertEqual(_fp(self.s.status / "mirror"), st)
        self.assertEqual(set(mirrorsafe.pending_rebuild(self.s, self.pair)), {"b"})   # retried later
        self.assertEqual(list(mirrorsafe.scratch(self.s).iterdir()), [])
        self.assert_healthy(self.cycle())                          # provider back: rebuild completes
        self.assertEqual(mirrorsafe.pending_rebuild(self.s, self.pair), {})
        self.assertEqual(self.remote_state(), before)

    def test_rebuild_never_silently_publishes_mass_deletion_or_empty_read(self):
        for i in range(10): self.put(self.ca, f"a{i}")
        self.put(self.cb, "b0")
        self.assert_healthy(self.cycle())
        fp = self.mirror_fp()
        for i in range(5): self.delete(self.ca, f"a{i}")           # 5 > limit 3
        mirrorsafe.mark_rebuild(self.s, self.pair, "a", "test")
        r = self.cycle()
        self.assertIn("vanished", r["blocked"])
        self.assertEqual(self.mirror_fp(), fp)                    # last known good mirror kept (10 items on A)
        self.assertEqual(len(list((self.s.mirror / "cal_x" / "a").glob("*.ics"))), 10)
        self.assertEqual(set(mirrorsafe.pending_rebuild(self.s, self.pair)), {"a"})
        for i in range(5, 10): self.delete(self.ca, f"a{i}")        # remote completely empty
        r = self.cycle()
        self.assertIn("ZERO", r["blocked"])
        self.assertEqual(self.mirror_fp(), fp)
        self.assertEqual(self.remote_uids(self.ca), set())          # we only ever read

    def test_acknowledged_deletions_are_published_by_rebuild(self):
        for i in range(10): self.put(self.ca, f"a{i}")
        self.put(self.cb, "b0")
        self.assert_healthy(self.cycle())
        for i in range(5): self.delete(self.ca, f"a{i}")
        mirrorsafe.mark_rebuild(self.s, self.pair, "a", "test")
        write_json(self.s.state / "ack_deletes_cal_x.json", {"max": 5, "expires": now() + 600})
        before = self.remote_state()
        r = self.cycle()
        self.assert_healthy(r)
        self.assertEqual(r["mirror_rebuilt"]["a"]["vanished"], 5)
        self.assertEqual(r["counts"]["a"], 5)
        self.assertEqual(self.remote_state(), before)

    def test_remote_is_never_written_by_failed_run_rollback_or_rebuild(self):
        self.seed(); self.assert_healthy(self.cycle())
        before = self.remote_state()
        for _ in range(2):
            with self.fail_b(), patch.object(mirrorsafe.os, "link", side_effect=OSError("x")):
                self.cycle()
            self.cycle()
        self.assertEqual(self.remote_state(), before)


if __name__ == "__main__":
    unittest.main()
