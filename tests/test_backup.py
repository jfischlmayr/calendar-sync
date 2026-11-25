"""Tests for calsync.backup (synthetic data, throwaway age keys, no network)."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from calsync import backup  # noqa: E402

SECRET = "ya29.SECRET-ACCESS-TOKEN-do-not-leak-0123456789"
REFRESH = "1//REFRESH-TOKEN-do-not-leak-abcdef"


def vevent(uid, summary="x"):
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{summary}\r\nDTSTART:20300101T100000Z\r\n"
            f"DTEND:20300101T110000Z\r\nEND:VEVENT\r\n")


def ics_file(*uids):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//\r\n" + "".join(vevent(u) for u in uids) + "END:VCALENDAR\r\n"


def make_keypair(directory: Path):
    ident = directory / "identity.txt"
    subprocess.run(["age-keygen", "-o", str(ident)], check=True, capture_output=True)
    pub = subprocess.run(["age-keygen", "-y", str(ident)], check=True, capture_output=True, text=True).stdout.strip()
    return ident, pub


def make_data(root: Path):
    d = root / "data"
    for rel, text in {
        "mirror/work/a/1.ics": ics_file("u1"),
        "mirror/work/a/2.ics": ics_file("u2", "u3"),
        "mirror/work/b/1.ics": ics_file("u1"),
        "mirror/home/a/1.ics": ics_file("h1"),
        "status/work/status.json": '{"x": 1}',
        "state/health.json": '{"ok": true}',
        "state/last_uids.json": '["u1"]',
        "manifests/m1.json": "{}",
        "tokens/google.json": json.dumps({"token": SECRET, "refresh_token": REFRESH}),
        "backups/old.tar": "must not be archived",
        "logs/sync.log": "must not be archived",
    }.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return d


def flip_byte(path: Path, index: int):
    b = bytearray(path.read_bytes())
    b[index] ^= 0x01
    path.write_bytes(bytes(b))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="calsync-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ident, self.pub = make_keypair(self.tmp)
        self.data = make_data(self.tmp)
        self.out = self.tmp / "out"
        for k in ("CALSYNC_BACKUP_REMOTE", "CALSYNC_BACKUP_LOCAL_DIR"):
            old = os.environ.pop(k, None)
            if old is not None:
                self.addCleanup(os.environ.__setitem__, k, old)

    def create(self, now=None, **kw):
        return backup.create_backup(self.data, self.out, [self.pub], now=now, **kw)


class RoundtripTests(Base):
    def test_create_verify_roundtrip(self):
        a = self.create()
        self.assertRegex(a.name, r"^calsync-\d{8}T\d{6}Z\.tar\.gz\.age$")
        man_p, sha_p, _ = backup._sidecars(a)
        self.assertTrue(man_p.name.endswith(".manifest.json") and sha_p.name.endswith(".sha256"))
        man = json.loads(man_p.read_text())
        self.assertEqual(man["counts"]["work"]["a"], {"files": 2, "events": 3, "uids": 3})
        self.assertEqual(man["counts"]["work"]["b"], {"files": 1, "events": 1, "uids": 1})
        self.assertEqual(man["counts"]["home"]["a"]["events"], 1)
        self.assertEqual(man["totals"]["events"], 5)
        self.assertEqual(man["archive"]["sha256"], backup._sha256_file(a))
        self.assertIn("tool", man)
        paths = {f["path"] for f in man["files"]}
        self.assertIn("tokens/google.json", paths)
        self.assertFalse(any(p.startswith(("backups/", "logs/")) for p in paths))
        # .sha256 is sha256sum compatible
        if shutil.which("sha256sum"):
            subprocess.run(["sha256sum", "-c", sha_p.name], cwd=a.parent, check=True, capture_output=True)
        rep = backup.verify_backup(a, self.ident)
        self.assertEqual(rep["events"], 5)
        self.assertEqual(rep["pairs"]["work"]["a"]["uids"], 3)
        # only the three published files; no plaintext tarball or temp leftovers
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         sorted([a.name, man_p.name, sha_p.name]))
        self.assertEqual(stat.S_IMODE(os.stat(a).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o700)
        self.assertNotIn(b"BEGIN:VCALENDAR", a.read_bytes())

    def test_wrong_identity_fails(self):
        a = self.create()
        other, _ = make_keypair(Path(tempfile.mkdtemp(dir=self.tmp)))
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(a, other)

    def test_never_overwrites_existing_backup(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        a = self.create(now=now)
        before = a.read_bytes()
        with self.assertRaises(backup.BackupError):
            self.create(now=now)
        self.assertEqual(a.read_bytes(), before)


class TamperTests(Base):
    def test_flip_byte_in_blob(self):
        a = self.create()
        flip_byte(a, a.stat().st_size // 2)
        with self.assertRaisesRegex(backup.BackupError, "sha256 mismatch"):
            backup.verify_backup(a, self.ident)

    def test_flip_byte_in_blob_with_recomputed_sidecars_is_caught_by_age(self):
        a = self.create()
        flip_byte(a, a.stat().st_size - 20)
        man_p, sha_p, _ = backup._sidecars(a)
        man = json.loads(man_p.read_text())
        man["archive"]["sha256"] = backup._sha256_file(a)
        mb = json.dumps(man, sort_keys=True, indent=2).encode() + b"\n"
        man_p.write_bytes(mb)
        sha_p.write_text(f"{man['archive']['sha256']}  {a.name}\n{backup._sha256(mb)}  {man_p.name}\n")
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(a, self.ident)

    def test_flip_byte_in_manifest(self):
        a = self.create()
        man_p, _, _ = backup._sidecars(a)
        flip_byte(man_p, 40)
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(a, self.ident)

    def test_forged_manifest_with_valid_checksums_is_caught(self):
        a = self.create()
        man_p, sha_p, _ = backup._sidecars(a)
        man = json.loads(man_p.read_text())
        man["counts"]["work"]["a"]["events"] += 1
        mb = json.dumps(man, sort_keys=True, indent=2).encode() + b"\n"
        man_p.write_bytes(mb)
        sha_p.write_text(f"{backup._sha256_file(a)}  {a.name}\n{backup._sha256(mb)}  {man_p.name}\n")
        with self.assertRaisesRegex(backup.BackupError, "differs from the authenticated manifest"):
            backup.verify_backup(a, self.ident)

    def test_truncated_blob(self):
        a = self.create()
        a.write_bytes(a.read_bytes()[:-50])
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(a, self.ident)

    def test_missing_sidecar(self):
        a = self.create()
        backup._sidecars(a)[1].unlink()
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(a, self.ident)


class RefusalTests(Base):
    def test_empty_data_dir(self):
        good = self.create()
        before = good.read_bytes()
        empty = self.tmp / "empty"
        for sub in backup.INCLUDE_DIRS:
            (empty / sub).mkdir(parents=True)
        with self.assertRaisesRegex(backup.BackupError, "empty backup"):
            backup.create_backup(empty, self.out, [self.pub])
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.tmp / "nope", self.out, [self.pub])
        with self.assertRaises(backup.BackupError):  # only excluded dirs present
            only_logs = self.tmp / "only_logs"
            (only_logs / "logs").mkdir(parents=True)
            (only_logs / "logs" / "x.log").write_text("x")
            backup.create_backup(only_logs, self.out, [self.pub])
        self.assertEqual(good.read_bytes(), before)
        self.assertEqual(len(backup.list_backups(self.out)), 1)

    def test_no_recipients(self):
        for recips in ([], [""], ["  "], None):
            with self.assertRaisesRegex(backup.BackupError, "recipient"):
                backup.create_backup(self.data, self.out, recips)
        self.assertFalse(self.out.exists())

    def test_private_identity_rejected_as_recipient(self):
        secret = [l for l in self.ident.read_text().splitlines() if l.startswith("AGE-SECRET-KEY")][0]
        with self.assertRaises(backup.BackupError) as cm:
            backup.create_backup(self.data, self.out, [secret])
        self.assertNotIn(secret, str(cm.exception))

    def test_age_failure_publishes_nothing(self):
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.data, self.out, ["age1notarealrecipient"])
        self.assertEqual(list(self.out.iterdir()), [])


class RestoreTests(Base):
    def test_restore_refuses_non_empty_target(self):
        a = self.create()
        target = self.tmp / "target"
        target.mkdir()
        (target / "keep.txt").write_text("precious")
        with self.assertRaisesRegex(backup.BackupError, "not empty"):
            backup.restore_backup(a, self.ident, target)
        self.assertEqual((target / "keep.txt").read_text(), "precious")
        self.assertFalse((target / "mirror").exists())

    def test_restore_ok_modes_and_content(self):
        a = self.create()
        target = self.tmp / "restored"
        rep = backup.restore_backup(a, self.ident, target)
        self.assertEqual(rep["events"], 5)
        self.assertEqual(stat.S_IMODE(os.stat(target / "tokens").st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(target / "tokens" / "google.json").st_mode), 0o600)
        for rel in ("mirror/work/a/2.ics", "tokens/google.json", "state/health.json", "status/work/status.json"):
            self.assertEqual((target / rel).read_bytes(), (self.data / rel).read_bytes())
        self.assertFalse((target / "logs").exists())
        self.assertFalse((target / "backups").exists())

    def test_restore_empty_existing_target_and_force(self):
        a = self.create()
        target = self.tmp / "t2"
        target.mkdir()
        backup.restore_backup(a, self.ident, target)
        (target / "extra.txt").write_text("e")
        (target / "state" / "health.json").write_text("changed")
        backup.restore_backup(a, self.ident, target, force=True)
        self.assertEqual((target / "state" / "health.json").read_text(), '{"ok": true}')
        self.assertEqual((target / "extra.txt").read_text(), "e")  # never deleted

    def test_tampered_archive_restores_nothing(self):
        a = self.create()
        flip_byte(a, a.stat().st_size // 2)
        target = self.tmp / "t3"
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(a, self.ident, target)
        self.assertFalse(target.exists())


class SecretTests(Base):
    def test_no_secrets_in_plaintext_files(self):
        a = self.create()
        man_p, sha_p, _ = backup._sidecars(a)
        for p in (man_p, sha_p):
            raw = p.read_text()
            self.assertNotIn(SECRET, raw)
            self.assertNotIn(REFRESH, raw)
        self.assertNotIn(SECRET.encode(), a.read_bytes())

    def test_cli_does_not_print_secrets(self):
        env = self.cli_env()
        r = subprocess.run([sys.executable, "-m", "calsync.backup", "create"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        a = backup.list_backups(self.out)[0]
        r2 = subprocess.run([sys.executable, "-m", "calsync.backup", "verify", str(a), "--identity", str(self.ident)],
                            env=env, capture_output=True, text=True)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        ident_secret = [l for l in self.ident.read_text().splitlines() if l.startswith("AGE-SECRET-KEY")][0]
        for out in (r.stdout, r.stderr, r2.stdout, r2.stderr):
            for s in (SECRET, REFRESH, ident_secret):
                self.assertNotIn(s, out)

    def cli_env(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), CALSYNC_DATA=str(self.data),
                   CALSYNC_BACKUP_DIR=str(self.out), CALSYNC_BACKUP_RECIPIENTS=self.pub)
        env.pop("CALSYNC_BACKUP_REMOTE", None)
        env.pop("CALSYNC_BACKUP_LOCAL_DIR", None)
        return env


class PruneTests(Base):
    def setUp(self):
        super().setUp()
        self.now = datetime(2031, 6, 1, tzinfo=timezone.utc)
        self.archives = [self.create(now=self.now - timedelta(days=d)) for d in (40, 30, 20, 10, 1)]
        # oldest .. newest

    def test_never_deletes_newest_even_if_ancient(self):
        for a in self.archives:
            backup._mark(a, "verified")
        res = backup.prune(self.out, older_than_days=0, min_newer_verified=1, now=self.now + timedelta(days=999))
        self.assertIn(self.archives[-1].name, res["kept"])
        self.assertTrue(self.archives[-1].exists())
        self.assertEqual(len(backup.list_backups(self.out)), 1)

    def test_requires_newer_verified(self):
        # nothing verified -> nothing pruned
        res = backup.prune(self.out, older_than_days=5, min_newer_verified=1, now=self.now)
        self.assertEqual(res["deleted"], [])
        self.assertEqual(len(backup.list_backups(self.out)), 5)
        # verify only the 10-day-old one: older ones may go, 1-day one is newest, 10-day is too young? (age 10 > 5 but
        # needs a newer verified one -> only the newest, which is unverified) -> kept
        backup._mark(self.archives[3], "verified")
        res = backup.prune(self.out, older_than_days=5, min_newer_verified=1, now=self.now)
        self.assertEqual(sorted(res["deleted"]), sorted(a.name for a in self.archives[:3]))
        self.assertTrue(self.archives[3].exists() and self.archives[4].exists())
        for a in self.archives[:3]:
            self.assertFalse(any(s.exists() for s in backup._sidecars(a)))

    def test_min_count_and_age(self):
        for a in self.archives[2:]:
            backup._mark(a, "verified")
        res = backup.prune(self.out, older_than_days=25, min_newer_verified=3, now=self.now)
        self.assertEqual(sorted(res["deleted"]), sorted(a.name for a in self.archives[:2]))  # 40d, 30d: 3 newer verified
        res = backup.prune(self.out, older_than_days=15, min_newer_verified=3, now=self.now)
        self.assertEqual(res["deleted"], [])  # the 20d one has only 2 newer verified

    def test_corrupted_backup_does_not_count_as_verified(self):
        for a in self.archives[1:]:
            backup._mark(a, "verified")
        flip_byte(self.archives[1], 100)  # blob no longer matches its sidecar/marker
        res = backup.prune(self.out, older_than_days=5, min_newer_verified=4, now=self.now)
        self.assertEqual(res["deleted"], [])

    def test_dry_run_and_foreign_files(self):
        for a in self.archives:
            backup._mark(a, "verified")
        foreign = self.out / "notes.txt"
        foreign.write_text("keep")
        res = backup.prune(self.out, older_than_days=5, min_newer_verified=1, now=self.now, dry_run=True)
        self.assertTrue(res["deleted"])
        self.assertEqual(len(backup.list_backups(self.out)), 5)
        backup.prune(self.out, older_than_days=5, min_newer_verified=1, now=self.now)
        self.assertTrue(foreign.exists())

    def test_invalid_retention_rejected(self):
        with self.assertRaises(backup.BackupError):
            backup.prune(self.out, older_than_days=5, min_newer_verified=0)


class UploadTests(Base):
    def test_unset_returns_offhost_not_configured(self):
        a = self.create()
        res = backup.upload(a)  # env cleared in setUp
        self.assertEqual(res["status"], "offhost_not_configured")
        self.assertFalse(res["offhost"])
        self.assertEqual(res["targets"], [])
        self.assertEqual(backup.upload(a, "", local_dir="")["status"], "offhost_not_configured")

    def test_local_dir_target(self):
        a = self.create()
        dest = self.tmp / "mnt" / "backups"
        res = backup.upload(a, "", local_dir=str(dest))
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["offhost"])
        self.assertEqual(res["targets"][0]["type"], "local_dir")
        self.assertTrue(res["targets"][0]["verified"])
        man_p, sha_p, _ = backup._sidecars(a)
        for p in (a, man_p, sha_p):
            self.assertEqual((dest / p.name).read_bytes(), p.read_bytes())
        self.assertEqual([p.name for p in dest.iterdir() if p.name.startswith(".")], [])
        backup.verify_backup(dest / a.name, self.ident)  # the copy is restorable
        # uploaded marker makes it count as verified for retention
        self.assertTrue(backup._is_verified(a))

    def test_env_driven(self):
        a = self.create()
        dest = self.tmp / "envdest"
        os.environ["CALSYNC_BACKUP_LOCAL_DIR"] = str(dest)
        try:
            self.assertEqual(backup.upload(a)["status"], "ok")
        finally:
            del os.environ["CALSYNC_BACKUP_LOCAL_DIR"]
        self.assertTrue((dest / a.name).exists())

    def test_local_dir_same_as_staging_fails(self):
        a = self.create()
        self.assertEqual(backup.upload(a, "", local_dir=str(self.out))["status"], "failed")

    def test_refuses_to_ship_corrupt_set(self):
        a = self.create()
        flip_byte(a, 50)
        with self.assertRaises(backup.BackupError):
            backup.upload(a, "", local_dir=str(self.tmp / "d"))

    def test_rclone_success_and_size_mismatch(self):
        a = self.create()
        remote_root = self.tmp / "remote"
        bindir = self.tmp / "bin"
        bindir.mkdir()
        fake = bindir / "rclone"
        fake.write_text(FAKE_RCLONE.replace("@PY@", sys.executable))
        fake.chmod(0o755)
        old = {k: os.environ.get(k) for k in ("PATH", "FAKE_RCLONE_ROOT", "FAKE_RCLONE_TRUNCATE")}
        os.environ.update(PATH=f"{bindir}:{os.environ['PATH']}", FAKE_RCLONE_ROOT=str(remote_root))
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])
        res = backup.upload(a, "fake:cal", local_dir="")
        self.assertEqual(res["status"], "ok", res)
        self.assertTrue(res["targets"][0]["verified"])
        self.assertEqual((remote_root / "cal" / a.name).read_bytes(), a.read_bytes())
        os.environ["FAKE_RCLONE_TRUNCATE"] = "1"
        res = backup.upload(a, "fake:cal2", local_dir="")
        self.assertEqual(res["status"], "failed")
        # inline connection strings are never echoed
        res = backup.upload(a, ":sftp,pass=hunter2:x", local_dir="")
        self.assertNotIn("hunter2", json.dumps(res))

    def test_rclone_missing(self):
        a = self.create()
        old = os.environ["PATH"]
        os.environ["PATH"] = str(self.tmp)  # no rclone here
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        res = backup.upload(a, "fake:cal", local_dir="")
        self.assertEqual(res["status"], "failed")


FAKE_RCLONE = """#!@PY@
import json, os, shutil, sys
root = os.environ["FAKE_RCLONE_ROOT"]
def loc(p):
    if p.startswith("fake:"):
        return os.path.join(root, p.split(":", 1)[1])
    if p.startswith(":"):
        return os.path.join(root, "inline-remote", p.rsplit(":", 1)[1].lstrip("/"))
    return p
a = sys.argv[1:]
cmd = a[0]
if cmd == "copyto":
    dst = loc(a[2]); os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(a[1], dst)
    if os.environ.get("FAKE_RCLONE_TRUNCATE"):
        open(dst, "r+b").truncate(3)
elif cmd == "lsjson":
    p = loc(a[1])
    if not os.path.isfile(p): sys.exit(3)
    print(json.dumps([{"Name": os.path.basename(p), "Size": os.path.getsize(p)}]))
elif cmd == "check":
    inc = [a[i + 1].lstrip("/") for i, x in enumerate(a) if x == "--include"]
    src, dst = a[-2], loc(a[-1])
    for n in inc:
        if open(os.path.join(src, n), "rb").read() != open(os.path.join(dst, n), "rb").read(): sys.exit(1)
else:
    sys.exit(2)
"""


class CliAndDrillTests(Base):
    def env(self, **extra):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), CALSYNC_DATA=str(self.data),
                   CALSYNC_BACKUP_DIR=str(self.out), CALSYNC_BACKUP_RECIPIENTS=self.pub)
        env.pop("CALSYNC_BACKUP_REMOTE", None)
        env.pop("CALSYNC_BACKUP_LOCAL_DIR", None)
        env.update(extra)
        return env

    def cli(self, *args, **extra):
        return subprocess.run([sys.executable, "-m", "calsync.backup", *args], env=self.env(**extra),
                              capture_output=True, text=True)

    def test_cli_flow_and_exit_codes(self):
        r = self.cli("create")
        self.assertEqual(r.returncode, 0, r.stderr)
        a = backup.list_backups(self.out)[0]
        st = json.loads((self.out / backup.STATUS_FILE).read_text())
        self.assertEqual(st["create"]["status"], "ok")
        r = self.cli("verify", "--identity", str(self.ident), "--mark")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("VERIFY OK", r.stdout)
        self.assertTrue(backup._is_verified(a))
        r = self.cli("upload")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["status"], "offhost_not_configured")
        self.assertEqual(self.cli("upload", "--require-offhost").returncode, 3)
        st = json.loads((self.out / backup.STATUS_FILE).read_text())
        self.assertEqual(st["upload"]["status"], "offhost_not_configured")
        r = self.cli("upload", CALSYNC_BACKUP_LOCAL_DIR=str(self.tmp / "mnt"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cli("prune", "--days", "0", "--min-verified", "1", "--dry-run").returncode, 0)
        target = self.tmp / "rt"
        r = self.cli("restore", str(a), "--identity", str(self.ident), "--target", str(target))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cli("restore", str(a), "--identity", str(self.ident), "--target", str(target)).returncode, 1)
        # failures exit non-zero
        flip_byte(a, 100)
        self.assertEqual(self.cli("verify", "--identity", str(self.ident)).returncode, 1)
        self.assertEqual(self.cli("create", CALSYNC_BACKUP_RECIPIENTS="").returncode, 1)
        st = json.loads((self.out / backup.STATUS_FILE).read_text())
        self.assertEqual(st["create"]["status"], "failed")
        self.assertEqual(st["verify"]["status"], "failed")

    def drill(self, archive, identity):
        return subprocess.run([str(ROOT / "scripts" / "restore-drill.sh"), str(archive), str(identity)],
                              env=self.env(), capture_output=True, text=True)

    def test_restore_drill_script(self):
        a = self.create()
        r = self.drill(a, self.ident)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DRILL OK", r.stdout)
        self.assertIn("work/a: 2 .ics files", r.stdout)
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        self.assertEqual(self.drill(a, self.tmp / "missing").returncode, 2)
        flip_byte(a, a.stat().st_size // 2)
        r = self.drill(a, self.ident)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("DRILL OK", r.stdout)
        leftovers = [p for p in Path("/dev/shm").glob("calsync-drill.*")] if Path("/dev/shm").is_dir() else []
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
