"""Fail-closed regression tests for backup.py / scheduler_backup.py / health.py.
Synthetic data and throwaway age keys only; no network."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from calsync import backup, config, health, scheduler_backup  # noqa: E402
from calsync.util import now, read_json, write_json  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_backup import ics_file, make_data, make_keypair  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="calsync-fc-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ident, self.pub = make_keypair(self.tmp)
        self.data = make_data(self.tmp)
        self.out = self.tmp / "out"
        env = {k: v for k, v in os.environ.items() if not k.startswith("CALSYNC_BACKUP")}
        env["PYTHONPATH"] = str(ROOT / "src")
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def settings(self):
        s = config.Settings(data_dir=self.data, run_dir=self.tmp / "run", interval_s=300, alert_repeat_s=3600)
        s.ensure_dirs()
        return s


def big_mirror(data: Path, pair="work", n=12):
    """Replace the mirror by one with n events (so shrink detection has a baseline)."""
    shutil.rmtree(data / "mirror")
    d = data / "mirror" / pair / "a"
    d.mkdir(parents=True)
    for i in range(n):
        (d / f"{i}.ics").write_text(ics_file(f"u{i}"))


class EmptyOrShrunkMirror(Base):
    def test_empty_mirror_after_good_backup_is_refused(self):
        big_mirror(self.data)
        good = backup.create_backup(self.data, self.out, [self.pub])
        shutil.rmtree(self.data / "mirror")           # outage: mirror wiped, state/ etc. still present
        (self.data / "mirror").mkdir()
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.data, self.out, [self.pub],
                                 now=datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(backup.list_backups(self.out), [good])

    def test_heavily_shrunk_mirror_is_refused(self):
        big_mirror(self.data, n=12)
        backup.create_backup(self.data, self.out, [self.pub])
        for f in sorted((self.data / "mirror" / "work" / "a").glob("*.ics"))[:9]:
            f.unlink()                                  # 12 -> 3 events
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.data, self.out, [self.pub],
                                 now=datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(len(backup.list_backups(self.out)), 1)

    def test_small_change_and_first_backup_still_ok(self):
        big_mirror(self.data, n=12)
        backup.create_backup(self.data, self.out, [self.pub])
        (self.data / "mirror" / "work" / "a" / "0.ics").unlink()
        backup.create_backup(self.data, self.out, [self.pub], now=datetime(2099, 1, 1, tzinfo=timezone.utc))
        # no baseline -> an empty first backup is not blocked
        shutil.rmtree(self.data / "mirror")
        (self.data / "mirror").mkdir()
        backup.create_backup(self.data, self.tmp / "out2", [self.pub])

    def test_explicit_override_allows_shrink(self):
        big_mirror(self.data)
        backup.create_backup(self.data, self.out, [self.pub])
        shutil.rmtree(self.data / "mirror")
        (self.data / "mirror").mkdir()
        with mock.patch.dict(os.environ, {"CALSYNC_BACKUP_ALLOW_SHRINK": "1"}):
            backup.create_backup(self.data, self.out, [self.pub], now=datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(len(backup.list_backups(self.out)), 2)


class UploadStatusRedaction(Base):
    def test_rclone_failure_detail_does_not_leak_inline_credentials(self):
        a = backup.create_backup(self.data, self.out, [self.pub])
        remote = ":s3,access_key_id=AKIAFAKE,secret_access_key=TOPSECRETKEY123:bucket/path/"
        base = remote.rstrip("/")

        def runner(cmd, timeout):
            # rclone echoes the remote it failed on, here without the trailing slash
            return subprocess.CompletedProcess(cmd, 1, "", f"Failed to create file system for \"{base}\": denied\n")

        res = backup.upload(a, remote, local_dir="", runner=runner)
        self.assertEqual(res["status"], "failed")
        self.assertNotIn("TOPSECRETKEY123", json.dumps(res))
        self.assertNotIn("AKIAFAKE", json.dumps(res))


class SchedulerFailClosed(Base):
    def test_future_dated_backup_ok_does_not_block_backups(self):
        s = self.settings()
        write_json(s.state / "backup_ok.json", {"ts": now() + 30 * 86400, "offhost": True})
        os.environ["CALSYNC_BACKUP_AGE_RECIPIENTS"] = self.pub
        with mock.patch.object(scheduler_backup, "run_backup", return_value=(True, "ok")) as rb:
            scheduler_backup.maybe_run(s)
        rb.assert_called_once()

    def test_uploads_the_archive_it_just_created_not_newest_by_name(self):
        s = self.settings()
        # a stray/clock-skewed archive whose NAME sorts newer than anything created now
        backup.create_backup(self.data, s.data_dir / "backups", [self.pub],
                             now=datetime(2099, 1, 1, tzinfo=timezone.utc))
        dest = self.tmp / "offhost"
        os.environ.update(CALSYNC_BACKUP_AGE_RECIPIENTS=self.pub, CALSYNC_BACKUP_LOCAL_DIR=str(dest))
        ok, info = scheduler_backup.run_backup(s)
        self.assertTrue(ok, info)
        shipped = sorted(p.name for p in dest.glob("*.tar.gz.age"))
        self.assertEqual(len(shipped), 1)
        self.assertNotIn("20990101T000000Z", shipped[0])   # the fresh backup, not the skewed one
        self.assertTrue(read_json(s.state / "backup_ok.json")["offhost"])

    def test_local_only_is_recorded_as_not_offhost(self):
        s = self.settings()
        os.environ["CALSYNC_BACKUP_AGE_RECIPIENTS"] = self.pub
        ok, _ = scheduler_backup.run_backup(s)
        self.assertTrue(ok)
        self.assertIs(read_json(s.state / "backup_ok.json")["offhost"], False)

    def test_failed_backup_leaves_previous_marker_untouched(self):
        s = self.settings()
        write_json(s.state / "backup_ok.json", {"ts": 123.0, "offhost": True})
        os.environ["CALSYNC_BACKUP_AGE_RECIPIENTS"] = "AGE-SECRET-KEY-1FAKE"   # refused as recipient
        ok, _ = scheduler_backup.run_backup(s)
        self.assertFalse(ok)
        self.assertEqual(read_json(s.state / "backup_ok.json"), {"ts": 123.0, "offhost": True})


class HealthClockSkew(Base):
    def test_future_last_success_is_not_healthy(self):
        s = self.settings()
        s.pairs.append(config.Pair(name="cal_a", icloud_collection="x", google_collection="y", mode="mirror"))
        health.update(s, "cal_a", last_success=now() + 10 * 86400)
        ok, body = health.evaluate(s)
        self.assertFalse(ok)
        self.assertEqual(body["state"], "degraded")

    def test_mirror_pair_never_succeeded_or_stale_is_degraded(self):
        s = self.settings()
        s.pairs.append(config.Pair(name="cal_m", icloud_collection="x", google_collection="y", mode="mirror"))
        self.assertFalse(health.evaluate(s)[0])
        health.update(s, "cal_m", last_success=now() - 5000)
        self.assertFalse(health.evaluate(s)[0])


if __name__ == "__main__":
    unittest.main()
