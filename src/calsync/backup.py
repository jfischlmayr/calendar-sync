"""Encrypted, verifiable backups of the calsync data directory.

Design rules (NO data loss is the top priority):

* The archive is streamed ``tar -> gzip -> age`` straight into ``age -r <recipient>``.
  The plaintext tarball never touches the disk. Only *recipients* (public keys) are ever
  needed on the host; the private identity is only used by verify/restore (off-host drill).
* Every backup is a unique, never-overwritten file set, published atomically
  (manifest + sha256 first, encrypted archive last, each via ``os.replace``).
* A backup of nothing fails loudly; an existing good backup is never replaced.
* ``MANIFEST.json`` is stored *inside* the encrypted tar as well. verify compares the
  plaintext sidecar against that authenticated copy, so a tampered sidecar is detected
  even when somebody also re-computes the ``.sha256`` file.
* The plaintext manifest carries no secrets: relative paths, sizes, sha256 sums and counts.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from . import ics

TOOL_VERSION = "calsync-backup/1"
FORMAT = 1
INCLUDE_DIRS = ("mirror", "status", "tokens", "state", "manifests")  # backups/ and logs/ are excluded
INNER_MANIFEST = "MANIFEST.json"
ARCHIVE_RE = re.compile(r"^calsync-(\d{8}T\d{6}Z)\.tar\.gz\.age$")
STATUS_FILE = "backup-status.json"
_MAX_MANIFEST = 64 * 1024 * 1024


class BackupError(Exception):
    """Any condition that makes a backup/verify/restore/upload untrustworthy."""


# --------------------------------------------------------------------------- helpers

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sidecars(archive: Path) -> tuple[Path, Path, Path]:
    n = archive.name
    return (archive.with_name(n + ".manifest.json"), archive.with_name(n + ".sha256"),
            archive.with_name(n + ".status.json"))


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _secure_tmp(prefix: str) -> Path:
    """0700 temp dir, preferring RAM-backed /dev/shm (decrypted secrets land here)."""
    parent = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None  # nosec B108 - fixed RAM-backed parent; mkdtemp makes a private 0700 dir
    # mkdtemp creates the directory atomically with mode 0700 (no predictable-name race).
    return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))  # nosec B108


def _clean_recipients(recipients: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for r in recipients or []:
        r = (r or "").strip()
        if not r:
            continue
        if r.startswith("-"):
            raise BackupError("invalid recipient (must not start with '-')")
        if r.startswith("AGE-SECRET-KEY"):
            raise BackupError("a private age identity was passed as recipient; "
                              "only public recipients may be configured on the host")
        if r not in out:
            out.append(r)
    return out


def _safe_rel(name: str) -> str:
    p = PurePosixPath(name)
    if (not name or p.is_absolute() or str(p) != name or "\\" in name or "\x00" in name
            or any(part in ("", ".", "..") for part in p.parts)):
        raise BackupError(f"unsafe path in archive: {name!r}")
    if name != INNER_MANIFEST and p.parts[0] not in INCLUDE_DIRS:
        raise BackupError(f"unexpected top-level entry in archive: {p.parts[0]!r}")
    return name


def _is_token_path(rel: str) -> bool:
    return PurePosixPath(rel).parts[0] == "tokens"


def _parse_ts(name: str) -> datetime:
    m = ARCHIVE_RE.match(name)
    if not m:
        raise BackupError(f"not a calsync archive name: {name}")
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- counting

def _counts(items: Iterable[tuple[str, bytes]]) -> dict:
    """Event/UID counts per mirror pair and side, parsed with calsync.ics."""
    acc: dict[tuple[str, str], dict] = {}
    for rel, data in items:
        parts = PurePosixPath(rel).parts
        if parts[0] != "mirror" or not rel.endswith(".ics"):
            continue
        pair = parts[1] if len(parts) > 2 else "_"
        side = parts[2] if len(parts) > 3 else "_"
        slot = acc.setdefault((pair, side), {"files": 0, "events": 0, "uids": set()})
        events = ics.parse(data.decode("utf-8", "replace"), source=rel)
        slot["files"] += 1
        slot["events"] += len(events)
        slot["uids"].update(e.uid for e in events if e.uid)
    out: dict[str, dict] = {}
    for (pair, side) in sorted(acc):
        s = acc[(pair, side)]
        out.setdefault(pair, {})[side] = {"files": s["files"], "events": s["events"], "uids": len(s["uids"])}
    return out


def _totals(files: list[dict], counts: dict) -> dict:
    sides = [s for p in counts.values() for s in p.values()]
    return {"files": len(files), "bytes": sum(f["size"] for f in files),
            "events": sum(s["events"] for s in sides), "uids": sum(s["uids"] for s in sides)}


# --------------------------------------------------------------------------- create

@dataclass
class Retention:
    days: int
    min_newer_verified: int = 1


def _scan(data_dir: Path):
    files: list[dict] = []
    dirs: list[str] = []
    skipped: list[dict] = []
    for top in INCLUDE_DIRS:
        base = data_dir / top
        if base.is_symlink():
            skipped.append({"path": top, "reason": "symlink"})
            continue
        if not base.is_dir():
            continue
        dirs.append(top)
        for root, dnames, fnames in os.walk(base, followlinks=False):
            rootp = Path(root)
            for d in list(dnames):
                dp = rootp / d
                rel = dp.relative_to(data_dir).as_posix()
                if dp.is_symlink():
                    skipped.append({"path": rel, "reason": "symlink"})
                    dnames.remove(d)
                else:
                    dirs.append(rel)
            dnames.sort()
            for f in sorted(fnames):
                fp = rootp / f
                rel = fp.relative_to(data_dir).as_posix()
                st = os.lstat(fp)
                if not stat.S_ISREG(st.st_mode):
                    skipped.append({"path": rel, "reason": "not a regular file"})
                    continue
                try:
                    data = fp.read_bytes()
                except OSError as e:
                    raise BackupError(f"cannot read {rel}: {e.strerror}") from e
                files.append({"path": rel, "size": len(data), "sha256": _sha256(data),
                              "mode": st.st_mode & 0o777, "mtime": int(st.st_mtime), "_data": data})
    files.sort(key=lambda f: f["path"])
    dirs.sort()
    return files, dirs, skipped


def _encrypt_stream(recipients: list[str], out_path: Path, writer: Callable) -> None:
    cmd = ["age"]
    for r in recipients:
        cmd += ["-r", r]
    cmd += ["-o", str(out_path)]
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err)
        except FileNotFoundError as e:
            raise BackupError("`age` binary not found") from e
        broken = False
        try:
            writer(proc.stdin)
        except BrokenPipeError:
            broken = True
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        try:
            proc.stdin.close()
        except BrokenPipeError:
            broken = True
        rc = proc.wait()
        if rc != 0 or broken:
            err.seek(0)
            raise BackupError("age encryption failed: " + err.read().decode("utf-8", "replace").strip()[:300])


def _check_not_shrunk(out_dir: Path, totals: dict, has_mirror_files: bool) -> None:
    """An outage that empties/wipes the mirror must not yield a "fresh" backup that then ages the
    good ones out. Compare with the newest existing backup's plaintext manifest; override with
    CALSYNC_BACKUP_ALLOW_SHRINK=1 (or tune CALSYNC_BACKUP_SHRINK_RATIO, default 0.5)."""
    if os.environ.get("CALSYNC_BACKUP_ALLOW_SHRINK") == "1" or not out_dir.is_dir():
        return
    try:
        prev = list_backups(out_dir)
        prev_events = json.loads(_sidecars(prev[0])[0].read_text())["totals"]["events"] if prev else 0
    except (OSError, ValueError, KeyError, TypeError):
        return  # no usable baseline
    try:
        ratio = float(os.environ.get("CALSYNC_BACKUP_SHRINK_RATIO", "0.5"))
    except ValueError:
        ratio = 0.5
    new_events = totals["events"]
    if prev_events > 0 and (not has_mirror_files or (prev_events >= 10 and new_events < prev_events * ratio)):
        raise BackupError(f"refusing backup: calendar events dropped {prev_events} -> {new_events} versus the "
                          "newest existing backup (mirror wiped or outage?); fix the mirror or set "
                          "CALSYNC_BACKUP_ALLOW_SHRINK=1 to accept")


def create_backup(data_dir, out_dir, recipients: list[str], *, keep: Retention | None = None,
                  now: datetime | None = None) -> Path:
    """Create ``calsync-<UTC>.tar.gz.age`` (+ ``.manifest.json`` + ``.sha256``) in ``out_dir``."""
    recips = _clean_recipients(recipients)
    if not recips:
        raise BackupError("no backup recipient configured (CALSYNC_BACKUP_RECIPIENTS); refusing to back up")
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    if not data_dir.is_dir():
        raise BackupError(f"data dir does not exist: {data_dir}")
    files, dirs, skipped = _scan(data_dir)
    if not files:
        raise BackupError(f"data dir {data_dir} contains no files in {list(INCLUDE_DIRS)}; "
                          "refusing to create an empty backup")
    now = (now or _utcnow()).astimezone(timezone.utc).replace(microsecond=0)
    name = f"calsync-{now.strftime('%Y%m%dT%H%M%SZ')}.tar.gz.age"
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = out_dir / name
    man_path, sha_path, _ = _sidecars(final)
    if any(p.exists() for p in (final, man_path, sha_path)):
        raise BackupError(f"{name} already exists; refusing to overwrite a backup")

    counts = _counts((f["path"], f["_data"]) for f in files)
    _check_not_shrunk(out_dir, _totals(files, counts), any(f["path"].startswith("mirror/") for f in files))
    inner = {
        "format": FORMAT, "tool": TOOL_VERSION, "created": _iso(now),
        "included": list(INCLUDE_DIRS), "dirs": dirs,
        "files": [{k: f[k] for k in ("path", "size", "sha256", "mode")} for f in files],
        "counts": counts, "totals": _totals(files, counts), "skipped": skipped,
    }
    inner_bytes = json.dumps(inner, sort_keys=True, indent=2).encode()
    mtime = int(now.timestamp())

    tmpdir = Path(tempfile.mkdtemp(prefix=".tmp-backup-", dir=out_dir))  # 0700
    try:
        blob = tmpdir / name

        def write(fp):
            gz = gzip.GzipFile(fileobj=fp, mode="wb", mtime=0)
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                def add(ti: tarfile.TarInfo, data: bytes | None = None):
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    tar.addfile(ti, io.BytesIO(data) if data is not None else None)
                ti = tarfile.TarInfo(INNER_MANIFEST)
                ti.size, ti.mode, ti.mtime = len(inner_bytes), 0o600, mtime
                add(ti, inner_bytes)
                for d in dirs:
                    ti = tarfile.TarInfo(d)
                    ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, (0o700 if _is_token_path(d) else 0o755), mtime
                    add(ti)
                for f in files:
                    ti = tarfile.TarInfo(f["path"])
                    ti.size, ti.mode, ti.mtime = f["size"], f["mode"], f["mtime"]
                    add(ti, f["_data"])
            gz.close()

        _encrypt_stream(recips, blob, write)
        os.chmod(blob, 0o600)
        size, blob_sha = blob.stat().st_size, _sha256_file(blob)
        if size == 0:
            raise BackupError("age produced an empty archive")
        outer = dict(inner, archive={"name": name, "size": size, "sha256": blob_sha})
        man_bytes = json.dumps(outer, sort_keys=True, indent=2).encode() + b"\n"
        sums = f"{blob_sha}  {name}\n{_sha256(man_bytes)}  {man_path.name}\n".encode()
        (tmpdir / "m").write_bytes(man_bytes)
        (tmpdir / "s").write_bytes(sums)
        for p in (tmpdir / "m", tmpdir / "s"):
            os.chmod(p, 0o600)
        with open(blob, "rb") as fh:
            os.fsync(fh.fileno())
        # publish: sidecars first, the encrypted archive last (archive present => set complete)
        os.replace(tmpdir / "m", man_path)
        os.replace(tmpdir / "s", sha_path)
        os.replace(blob, final)
        _fsync_dir(out_dir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if keep is not None:
        prune(out_dir, older_than_days=keep.days, min_newer_verified=keep.min_newer_verified, now=now)
    return final


# --------------------------------------------------------------------------- verify / restore

def _read_sums(path: Path) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in path.read_text().splitlines():
        h, _, n = line.partition("  ")
        if h and n:
            sums[n] = h
    return sums


def _check_sidecars(archive: Path) -> dict:
    """Check .sha256 + plaintext manifest against the encrypted blob; return the manifest."""
    man_path, sha_path, _ = _sidecars(archive)
    for p in (archive, man_path, sha_path):
        if not p.is_file():
            raise BackupError(f"missing backup file: {p.name}")
    sums = _read_sums(sha_path)
    blob_sha = _sha256_file(archive)
    if sums.get(archive.name) != blob_sha:
        raise BackupError(f"sha256 mismatch for {archive.name} (blob corrupted or tampered)")
    man_bytes = man_path.read_bytes()
    if sums.get(man_path.name) != _sha256(man_bytes):
        raise BackupError(f"sha256 mismatch for {man_path.name} (manifest corrupted or tampered)")
    try:
        outer = json.loads(man_bytes)
        arch = outer["archive"]
    except (ValueError, KeyError, TypeError) as e:
        raise BackupError("manifest is not valid") from e
    if arch.get("sha256") != blob_sha or arch.get("size") != archive.stat().st_size or arch.get("name") != archive.name:
        raise BackupError("manifest does not describe this archive")
    return outer


def _decrypt_extract(archive: Path, identity: Path, staging: Path):
    """Stream-decrypt into ``staging`` (0700). Returns (inner_manifest_bytes, files, dirs)."""
    if not Path(identity).is_file():
        raise BackupError("identity file not found")
    files: dict[str, tuple[str, int]] = {}
    dirs: set[str] = set()
    inner_bytes: bytes | None = None
    exc: Exception | None = None
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(["age", "-d", "-i", str(identity), str(archive)],
                                    stdout=subprocess.PIPE, stderr=err)
        except FileNotFoundError as e:
            raise BackupError("`age` binary not found") from e
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|gz") as tar:
                for m in tar:
                    rel = _safe_rel(m.name)
                    if m.isdir():
                        if rel == INNER_MANIFEST:
                            raise BackupError("manifest entry is a directory")
                        dirs.add(rel)
                        (staging / rel).mkdir(parents=True, exist_ok=True, mode=0o700)
                    elif m.isreg():
                        src = tar.extractfile(m)
                        if rel == INNER_MANIFEST:
                            if m.size > _MAX_MANIFEST or inner_bytes is not None:
                                raise BackupError("bad manifest entry")
                            inner_bytes = src.read()
                            continue
                        if rel in files:
                            raise BackupError(f"duplicate entry in archive: {rel}")
                        dest = staging / rel
                        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        h, n = hashlib.sha256(), 0
                        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "wb") as out:
                            for chunk in iter(lambda: src.read(1 << 20), b""):
                                h.update(chunk)
                                n += len(chunk)
                                out.write(chunk)
                        files[rel] = (h.hexdigest(), n)
                    else:
                        raise BackupError(f"unsupported entry type in archive: {rel}")
            while proc.stdout.read(1 << 20):  # drain so age can authenticate the final chunk
                pass
        except BackupError as e:
            exc = e
        except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
            exc = e
        finally:
            if exc is not None:
                proc.kill()
            proc.stdout.close()
            rc = proc.wait()
        if rc != 0 and not isinstance(exc, BackupError):
            err.seek(0)
            raise BackupError("decryption failed: " + err.read().decode("utf-8", "replace").strip()[:300])
    if exc is not None:
        if isinstance(exc, BackupError):
            raise exc
        raise BackupError(f"corrupt archive: {type(exc).__name__}") from exc
    if inner_bytes is None:
        raise BackupError("archive has no inner manifest")
    return inner_bytes, files, dirs


def _compare(outer: dict, inner_bytes: bytes, files: dict, dirs: set, staging: Path) -> dict:
    try:
        inner = json.loads(inner_bytes)
    except ValueError as e:
        raise BackupError("inner manifest is not valid JSON") from e
    if {k: v for k, v in outer.items() if k != "archive"} != inner:
        raise BackupError("plaintext manifest differs from the authenticated manifest inside the archive")
    problems: list[str] = []
    expected = {f["path"]: f for f in inner["files"]}
    for p in sorted(set(expected) - set(files)):
        problems.append(f"missing file: {p}")
    for p in sorted(set(files) - set(expected)):
        problems.append(f"unexpected file: {p}")
    for p in sorted(set(expected) & set(files)):
        if files[p][0] != expected[p]["sha256"] or files[p][1] != expected[p]["size"]:
            problems.append(f"hash/size mismatch: {p}")
    if dirs != set(inner["dirs"]):
        problems.append("directory set differs from manifest")
    if not problems:
        counts = _counts((p, (staging / p).read_bytes()) for p in sorted(files) if p.endswith(".ics"))
        if counts != inner["counts"]:
            problems.append("event/uid counts differ from manifest")
        if _totals(inner["files"], counts) != inner["totals"]:
            problems.append("totals differ from manifest")
    if problems:
        raise BackupError("verification failed: " + "; ".join(problems[:5]) + (" ..." if len(problems) > 5 else ""))
    return inner


def _report(archive: Path, inner: dict) -> dict:
    return {"archive": archive.name, "created": inner["created"], "files": inner["totals"]["files"],
            "bytes": inner["totals"]["bytes"], "events": inner["totals"]["events"],
            "uids": inner["totals"]["uids"], "pairs": inner["counts"]}


def _verified_extract(archive: Path, identity: Path, staging: Path) -> dict:
    outer = _check_sidecars(archive)
    inner_bytes, files, dirs = _decrypt_extract(archive, identity, staging)
    return _compare(outer, inner_bytes, files, dirs, staging)


def verify_backup(archive, identity_file, *, mark: bool = False) -> dict:
    """Decrypt, check every file hash and every event count; raise BackupError on any mismatch."""
    archive = Path(archive)
    staging = _secure_tmp("calsync-verify-")
    try:
        inner = _verified_extract(archive, Path(identity_file), staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if mark:
        _mark(archive, "verified")
    return _report(archive, inner)


def verify_tree(target_dir, manifest: dict) -> None:
    """Re-hash a restored tree against the manifest (extra files in the tree are tolerated)."""
    target = Path(target_dir)
    bad = []
    for f in manifest["files"]:
        p = target / f["path"]
        if not p.is_file() or _sha256_file(p) != f["sha256"]:
            bad.append(f["path"])
    if bad:
        raise BackupError("restored tree differs from manifest: " + ", ".join(bad[:5]))


def restore_backup(archive, identity_file, target_dir, *, force: bool = False) -> dict:
    """Verify, then restore into ``target_dir`` (refuses a non-empty target unless ``force``).

    Existing files are only overwritten (never deleted) with ``force``. tokens/ is 0700, its files 0600.
    """
    archive, target = Path(archive), Path(target_dir)
    if target.exists():
        if not target.is_dir():
            raise BackupError("restore target is not a directory")
        if any(target.iterdir()) and not force:
            raise BackupError(f"restore target {target} is not empty (use force to overwrite)")
    staging = _secure_tmp("calsync-restore-")
    try:
        inner = _verified_extract(archive, Path(identity_file), staging)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        for d in inner["dirs"]:
            dp = target / d
            existed = dp.exists()
            dp.mkdir(parents=True, exist_ok=True, mode=0o700)
            if _is_token_path(d):
                os.chmod(dp, 0o700)
            elif not existed:
                os.chmod(dp, 0o700)   # restored calendar data is private: never world-readable
        for f in inner["files"]:
            dest = target / f["path"]
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            mode = 0o600 if _is_token_path(f["path"]) else (f["mode"] & 0o700) | 0o600   # owner-only: drop group/other bits
            tmp = dest.with_name(f".{dest.name}.restore-tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(fd, "wb") as out, open(staging / f["path"], "rb") as src:
                shutil.copyfileobj(src, out)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, dest)
        verify_tree(target, inner)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return _report(archive, inner)


# --------------------------------------------------------------------------- status markers

def _mark(archive: Path, kind: str, **extra) -> bool:
    """Record that this exact blob was verified/uploaded. Best effort (read-only dirs are fine)."""
    _, _, st_path = _sidecars(archive)
    try:
        cur = json.loads(st_path.read_text()) if st_path.exists() else {}
        cur[kind] = {"at": _iso(_utcnow()), "blob_sha256": _sha256_file(archive), **extra}
        _atomic_write(st_path, json.dumps(cur, sort_keys=True, indent=2).encode())
        return True
    except (OSError, ValueError):
        return False


def _is_verified(archive: Path) -> bool:
    """Intact (sha256 matches sidecar) AND decrypt-verified or off-host-uploaded for this exact blob."""
    _, sha_path, st_path = _sidecars(archive)
    try:
        blob_sha = _sha256_file(archive)
        sums = _read_sums(sha_path)
        if sums.get(archive.name) != blob_sha:
            return False
        st = json.loads(st_path.read_text())
    except (OSError, ValueError):
        return False
    return any(isinstance(st.get(k), dict) and st[k].get("blob_sha256") == blob_sha for k in ("verified", "uploaded"))


def update_status(out_dir, section: str, payload: dict) -> None:
    """Maintain out_dir/backup-status.json for the health module."""
    path = Path(out_dir) / STATUS_FILE
    try:
        cur = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        cur = {}
    cur[section] = payload
    cur["updated"] = _iso(_utcnow())
    _atomic_write(path, json.dumps(cur, sort_keys=True, indent=2).encode())


# --------------------------------------------------------------------------- upload

def _redact(remote: str) -> str:
    return ":<inline-backend>" if remote.startswith(":") else remote


def _run(cmd: list[str], timeout: int):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _upload_rclone(archive: Path, parts: list[Path], remote: str, runner) -> dict:
    tgt = {"type": "rclone", "dest": _redact(remote), "status": "ok", "offhost": True, "verified": False}
    timeout = int(os.environ.get("CALSYNC_BACKUP_RCLONE_TIMEOUT", "1800"))
    base = remote.rstrip("/")

    def scrub(msg: str) -> str:
        # rclone echoes destinations; an inline ':backend,key=secret:' string carries credentials.
        # Remove the full remote, the slash-stripped base, the connection string and every option value.
        if remote.startswith(":"):
            conn = remote[:remote.find(":", 1) + 1] if remote.find(":", 1) > 0 else remote
            for opt in conn.strip(":").split(",")[1:]:
                v = opt.partition("=")[2]
                if len(v) >= 3:
                    msg = msg.replace(v, "<redacted>")
            msg = msg.replace(conn, ":<inline-backend>")
        return msg.replace(remote, _redact(remote)).replace(base, _redact(remote))

    def fail(msg: str):
        tgt.update(status="failed", detail=scrub(msg)[:300])
        return tgt

    def run(cmd):
        try:
            return runner(cmd, timeout)
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 124, "", "timeout")

    for p in parts:
        r = run(["rclone", "copyto", str(p), f"{base}/{p.name}"])
        if r is None:
            return fail("rclone not installed")
        if r.returncode != 0:
            return fail(f"rclone copyto {p.name} failed: {((r.stderr or '').strip().splitlines() or [''])[-1]}")
    for p in parts:  # size-verify by listing
        r = run(["rclone", "lsjson", f"{base}/{p.name}"])
        try:
            listing = json.loads(r.stdout) if r is not None and r.returncode == 0 else None
        except ValueError:
            listing = None
        if not listing or len(listing) != 1 or listing[0].get("Size") != p.stat().st_size:
            return fail(f"remote size verification failed for {p.name}")
    cmd = ["rclone", "check", "--one-way"]
    if os.environ.get("CALSYNC_BACKUP_RCLONE_DOWNLOAD_CHECK") == "1":
        cmd.append("--download")
    for p in parts:
        cmd += ["--include", f"/{p.name}"]
    r = run(cmd + [str(archive.parent), base])
    if r is None or r.returncode != 0:
        return fail("rclone check failed")
    tgt["verified"] = True
    return tgt


def _upload_local(archive: Path, parts: list[Path], local_dir: str) -> dict:
    dest = Path(local_dir)
    tgt = {"type": "local_dir", "dest": str(dest), "status": "ok", "offhost": True, "verified": False}
    try:
        dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        if dest.resolve() == archive.parent.resolve():
            raise BackupError("local backup dir equals the staging dir (not a copy)")
        tgt["same_filesystem"] = dest.stat().st_dev == archive.parent.stat().st_dev
        # sidecars first, archive last; each copy is atomic
        for p in sorted(parts, key=lambda x: x == archive):
            tmp = dest / f".{p.name}.tmp"
            with open(p, "rb") as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, dest / p.name)
        _fsync_dir(dest)
        for p in parts:
            if _sha256_file(dest / p.name) != _sha256_file(p):
                raise BackupError(f"copy verification failed for {p.name}")
        tgt["verified"] = True
    except (OSError, BackupError) as e:
        tgt.update(status="failed", detail=str(e)[:300])
    return tgt


def upload(archive, remote: str | None = None, *, local_dir: str | None = None, runner=None) -> dict:
    """Copy archive+manifest+sha256 off-host. ``None`` arguments fall back to the environment
    (CALSYNC_BACKUP_REMOTE, CALSYNC_BACKUP_LOCAL_DIR); empty string means unset.

    Returns a structured dict; ``status`` is ``ok`` | ``failed`` | ``offhost_not_configured``.
    """
    archive = Path(archive)
    remote = os.environ.get("CALSYNC_BACKUP_REMOTE", "") if remote is None else remote
    local_dir = os.environ.get("CALSYNC_BACKUP_LOCAL_DIR", "") if local_dir is None else local_dir
    remote, local_dir = remote.strip(), local_dir.strip()
    res = {"status": "offhost_not_configured", "archive": archive.name, "at": _iso(_utcnow()),
           "offhost": False, "targets": []}
    if not remote and not local_dir:
        return res
    if remote and (remote.startswith("-") or ":" not in remote):
        raise BackupError("CALSYNC_BACKUP_REMOTE must look like 'remote:path'")
    _check_sidecars(archive)  # never ship a corrupt set
    man, sha, _ = _sidecars(archive)
    parts = [man, sha, archive]
    res["blob_sha256"] = _sha256_file(archive)
    if remote:
        res["targets"].append(_upload_rclone(archive, parts, remote, runner or _run))
    if local_dir:
        res["targets"].append(_upload_local(archive, parts, local_dir))
    if any(t["status"] != "ok" for t in res["targets"]):
        res["status"] = "failed"
    else:
        res["status"] = "ok"
        res["offhost"] = True
        if any(t.get("same_filesystem") for t in res["targets"]):
            res["warnings"] = ["local_dir is on the same filesystem as the staging dir"]
        _mark(archive, "uploaded", targets=[t["dest"] for t in res["targets"]])
    return res


# --------------------------------------------------------------------------- prune

def list_backups(out_dir) -> list[Path]:
    """Complete-looking archives, newest first (by name timestamp, not mtime)."""
    out = [p for p in Path(out_dir).iterdir() if ARCHIVE_RE.match(p.name) and p.is_file() and not p.is_symlink()]
    return sorted(out, key=lambda p: _parse_ts(p.name), reverse=True)


def prune(out_dir, *, older_than_days: int, min_newer_verified: int = 1, now: datetime | None = None,
          dry_run: bool = False) -> dict:
    """Delete local copies older than N days only if >= M NEWER verified backups exist.
    The newest backup is never deleted. Only files named like calsync archives are touched."""
    if older_than_days < 0 or min_newer_verified < 1:
        raise BackupError("retention needs days >= 0 and min_newer_verified >= 1")
    now = now or _utcnow()
    backups = list_backups(out_dir)
    cache: dict[Path, bool] = {}

    def verified(p: Path) -> bool:
        if p not in cache:
            cache[p] = _is_verified(p)
        return cache[p]

    res = {"deleted": [], "kept": [], "reasons": {}}
    for i, p in enumerate(backups):
        reason = None
        if i == 0:
            reason = "newest"
        elif now - _parse_ts(p.name) <= timedelta(days=older_than_days):
            reason = "too_young"
        elif sum(1 for q in backups[:i] if verified(q)) < min_newer_verified:
            reason = "not_enough_newer_verified"
        if reason:
            res["kept"].append(p.name)
            res["reasons"][p.name] = reason
            continue
        if not dry_run:
            p.unlink()  # archive first; leftover sidecars are harmless
            for s in _sidecars(p):
                s.unlink(missing_ok=True)
        res["deleted"].append(p.name)
    return res


# --------------------------------------------------------------------------- CLI

def _env_list(name: str) -> list[str]:
    return [x for x in re.split(r"[\s,]+", os.environ.get(name, "")) if x]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _paths(args):
    data = Path(getattr(args, "data_dir", None) or os.environ.get("CALSYNC_DATA", "/data"))
    out = Path(getattr(args, "out_dir", None) or os.environ.get("CALSYNC_BACKUP_DIR") or data / "backups")
    return data, out


def _identity(args) -> Path:
    ident = args.identity or os.environ.get("CALSYNC_BACKUP_IDENTITY_FILE")
    if not ident:
        raise BackupError("no identity file (--identity / CALSYNC_BACKUP_IDENTITY_FILE)")
    return Path(ident)


def _newest(out: Path) -> Path:
    b = list_backups(out)
    if not b:
        raise BackupError(f"no backups in {out}")
    return b[0]


def _print_report(rep: dict) -> None:
    print(f"archive={rep['archive']} files={rep['files']} events={rep['events']} uids={rep['uids']}")
    for pair, sides in rep["pairs"].items():
        for side, c in sides.items():
            print(f"  {pair}/{side}: files={c['files']} events={c['events']} uids={c['uids']}")


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python3 -m calsync.backup", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="create an encrypted backup (recipients only)")
    c.add_argument("--data-dir")
    c.add_argument("--out-dir")
    c.add_argument("--recipient", action="append", default=[], help="age recipient (repeatable); "
                   "default CALSYNC_BACKUP_RECIPIENTS (alias CALSYNC_BACKUP_AGE_RECIPIENTS)")
    c.add_argument("--prune", action="store_true", help="apply retention afterwards")
    v = sub.add_parser("verify", help="decrypt + check hashes and event counts")
    v.add_argument("archive", nargs="?")
    v.add_argument("--out-dir")
    v.add_argument("--data-dir")
    v.add_argument("--identity")
    v.add_argument("--mark", action="store_true", help="record the result next to the archive")
    r = sub.add_parser("restore", help="verify then restore into an empty directory")
    r.add_argument("archive")
    r.add_argument("--identity")
    r.add_argument("--target", required=True)
    r.add_argument("--force", action="store_true")
    u = sub.add_parser("upload", help="copy off-host (CALSYNC_BACKUP_REMOTE / CALSYNC_BACKUP_LOCAL_DIR)")
    u.add_argument("archive", nargs="?")
    u.add_argument("--out-dir")
    u.add_argument("--data-dir")
    u.add_argument("--require-offhost", action="store_true", help="exit 3 if no off-host target is configured")
    p = sub.add_parser("prune", help="retention for local copies")
    p.add_argument("--out-dir")
    p.add_argument("--data-dir")
    p.add_argument("--days", type=int, default=_env_int("CALSYNC_BACKUP_RETENTION_DAYS", 30))
    p.add_argument("--min-verified", type=int, default=_env_int("CALSYNC_BACKUP_MIN_VERIFIED", 2))
    p.add_argument("--dry-run", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "create":
            data, out = _paths(args)
            try:
                keep = (Retention(_env_int("CALSYNC_BACKUP_RETENTION_DAYS", 30),
                                  _env_int("CALSYNC_BACKUP_MIN_VERIFIED", 2)) if args.prune else None)
                path = create_backup(data, out, args.recipient or _env_list("CALSYNC_BACKUP_RECIPIENTS") or _env_list("CALSYNC_BACKUP_AGE_RECIPIENTS"), keep=keep)
            except BackupError as e:
                if out.is_dir():
                    update_status(out, "create", {"status": "failed", "error": str(e), "at": _iso(_utcnow())})
                raise
            man = json.loads(_sidecars(path)[0].read_text())
            update_status(out, "create", {"status": "ok", "archive": path.name, "at": man["created"],
                                          "totals": man["totals"], "sha256": man["archive"]["sha256"]})
            print(path)
            t = man["totals"]
            print(f"files={t['files']} events={t['events']} uids={t['uids']} bytes_encrypted={man['archive']['size']}")
        elif args.cmd == "verify":
            _, out = _paths(args)
            arch = Path(args.archive) if args.archive else _newest(out)
            try:
                rep = verify_backup(arch, _identity(args), mark=args.mark)
            except BackupError as e:
                if out.is_dir():
                    update_status(out, "verify", {"status": "failed", "archive": arch.name, "error": str(e),
                                                  "at": _iso(_utcnow())})
                raise
            if out.is_dir() and args.mark:
                update_status(out, "verify", {"status": "ok", "archive": arch.name, "at": _iso(_utcnow()),
                                              "events": rep["events"], "files": rep["files"]})
            _print_report(rep)
            print("VERIFY OK")
        elif args.cmd == "restore":
            rep = restore_backup(args.archive, _identity(args), args.target, force=args.force)
            _print_report(rep)
            print(f"RESTORE OK -> {args.target}")
        elif args.cmd == "upload":
            _, out = _paths(args)
            arch = Path(args.archive) if args.archive else _newest(out)
            try:
                res = upload(arch)
            except BackupError as e:
                res = {"status": "failed", "archive": arch.name, "error": str(e), "at": _iso(_utcnow())}
            if out.is_dir():
                update_status(out, "upload", res)
            print(json.dumps(res, sort_keys=True))
            if res["status"] == "failed":
                return 1
            if res["status"] == "offhost_not_configured":
                print("WARNING: no off-host backup target configured (CALSYNC_BACKUP_REMOTE / "
                      "CALSYNC_BACKUP_LOCAL_DIR)", file=sys.stderr)
                return 3 if args.require_offhost else 0
        elif args.cmd == "prune":
            _, out = _paths(args)
            print(json.dumps(prune(out, older_than_days=args.days, min_newer_verified=args.min_verified,
                                   dry_run=args.dry_run), sort_keys=True))
    except BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError) as e:
        print(f"error: {type(e).__name__}: {getattr(e, 'strerror', None) or 'failed'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
