"""Crash-safe, pair-scoped snapshot/rollback and rebuild publishing for the read-only vdirsyncer mirror.

Why this exists (vdirsyncer 0.21 internals, verified against the real binary):
  * The filesystem storage's etag is f"{st_mtime_ns};{st_ino}". The sqlite status (`<pair>.items`, or
    `<pair>/<collection>.items`) stores etag AND content hash per item. Only when the etag differs does
    vdirsyncer re-read the file and compare hashes; while the etag matches, a stale/legacy `hash_b`
    stays invisible.
  * A rollback that restores the mirror with `shutil.copytree` keeps mtime but creates NEW inodes, so every
    etag mismatches, every file is re-hashed, any item whose stored hash_b differs is "changed locally" and
    vdirsyncer tries to write the read-only remote: `PartialSync` ("Attempted change on ... read-only")
    on every following run until someone resets the status by hand.

So (1) the snapshot is a hardlink farm (same inode and mtime; vdirsyncer only ever replaces files via
temp-file+rename or unlinks them, it never rewrites in place) and the restore is a directory rename, which
reproduces mirror+status exactly; (2) only this pair's side files in the shared status dir are touched;
(3) if exactness cannot be proven (hardlinks unsupported, partial restore, ...) the side is marked
"rebuild pending" instead of trusting the status; (4) a rebuild fetches the remote read-only into scratch
staging with a fresh status and publishes it (directory swap) only if the read was complete and passes the
deletion guard. The previous mirror is never discarded on failure. Nothing here talks to a provider."""
from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import Pair, Settings
from .util import log, now, read_json, write_json

SIDES = ("a", "b")


def scratch(s: Settings) -> Path:
    d = s.backups / "mirror-scratch"    # same /data volume; excluded by both backup implementations
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def status_root(s: Settings) -> Path:
    return s.status / "mirror"


def _base(p: Pair, side: str) -> str:
    return f"m_{p.name}_{side}"


def side_status_entries(root: Path, p: Pair, side: str, *, keep_collections: bool = True) -> list[Path]:
    """Status entries that belong to exactly this pair+side: `m_<pair>_<side>` (dir, one file per
    collection) and `m_<pair>_<side>.<ext>` (`.items`, `.collections`, ...). Exact-name match, never a
    prefix glob, so other pairs are never touched."""
    base = _base(p, side)
    out = []
    if root.is_dir():
        for e in sorted(root.iterdir()):
            if e.name == base or (e.name.startswith(base + ".") and (keep_collections or not e.name.endswith(".collections"))):
                out.append(e)
    return out


# ---- rebuild-pending markers -----------------------------------------------------------------------

def _marker(s: Settings, p: Pair) -> Path:
    return s.state / f"mirror_rebuild_{p.name}.json"


def pending_rebuild(s: Settings, p: Pair) -> dict:
    return read_json(_marker(s, p), {}) or {}


def mark_rebuild(s: Settings, p: Pair, side: str, reason: str) -> None:
    m = pending_rebuild(s, p)
    m[side] = {"reason": reason[:300], "ts": now()}
    write_json(_marker(s, p), m)
    log.warning("mirror %s/%s: rebuild pending (%s)", p.name, side, reason[:200])


def clear_rebuild(s: Settings, p: Pair, side: str) -> None:
    m = pending_rebuild(s, p)
    m.pop(side, None)
    if m:
        write_json(_marker(s, p), m)
    else:
        _marker(s, p).unlink(missing_ok=True)


# ---- snapshot / scoped rollback ---------------------------------------------------------------------

@dataclass
class Snapshot:
    root: Path
    had_mirror: bool = False
    fingerprints: dict = field(default_factory=dict)     # rel path under mirror/<pair> -> (mtime_ns, ino)
    linked: bool = True


def _fp(path: Path) -> tuple[int, int]:
    st = os.lstat(path)
    return (st.st_mtime_ns, st.st_ino)


def _link_or_copy(snap: Snapshot):
    def fn(src, dst):
        try:
            os.link(src, dst, follow_symlinks=False)
        except OSError:
            snap.linked = False                           # e.g. no hardlinks on this fs: exactness unprovable
            shutil.copy2(src, dst)
    return fn


def snapshot(s: Settings, p: Pair) -> Snapshot:
    root = scratch(s) / f"rollback-{p.name}"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    snap = Snapshot(root=root)
    src = s.mirror / p.name
    if src.exists():
        shutil.copytree(src, root / "mirror", symlinks=True, copy_function=_link_or_copy(snap))
        snap.had_mirror = True
        for f in src.rglob("*"):
            if f.is_file() and not f.is_symlink():
                snap.fingerprints[f.relative_to(src).as_posix()] = _fp(f)
    (root / "status").mkdir()
    for side in SIDES:                                    # real copies: sqlite files are modified in place
        for e in side_status_entries(status_root(s), p, side):
            if e.is_dir():
                shutil.copytree(e, root / "status" / e.name, symlinks=True)
            else:
                shutil.copy2(e, root / "status" / e.name)
    return snap


def discard(snap: Snapshot | None) -> None:
    if snap is not None:
        shutil.rmtree(snap.root, ignore_errors=True)


def _swap_in(new: Path, live: Path, trash: Path) -> None:
    """Replace `live` by `new` (same filesystem): live -> trash, new -> live, drop trash. If the second
    rename fails the original is moved back."""
    shutil.rmtree(trash, ignore_errors=True)
    had = live.exists()
    if had:
        os.rename(live, trash)
    try:
        os.rename(new, live)
    except BaseException:
        if had:
            os.rename(trash, live)
        raise
    shutil.rmtree(trash, ignore_errors=True)


def rollback(s: Settings, p: Pair, snap: Snapshot) -> list[str]:
    """Restore this pair's mirror and this pair's side status files to the snapshot state. Returns the
    sides that are marked rebuild-pending because the restored state could not be proven identical."""
    already_pending = set(pending_rebuild(s, p))
    for side in SIDES:
        if side not in already_pending:
            mark_rebuild(s, p, side, "rollback in progress; exact restore not yet verified")
    if not snap.had_mirror:
        return list(SIDES)                                # first fetch incomplete: rebuild before trusting it
    live = s.mirror / p.name
    _swap_in(snap.root / "mirror", live, scratch(s) / f"trash-{p.name}")
    root = status_root(s)
    root.mkdir(parents=True, exist_ok=True)
    for side in SIDES:
        for e in side_status_entries(root, p, side):      # also removes entries created by the failed run
            shutil.rmtree(e) if e.is_dir() and not e.is_symlink() else e.unlink()
        for e in side_status_entries(snap.root / "status", p, side):
            os.replace(e, root / e.name)                  # snapshot copy is expendable afterwards
    bad = []
    for side in SIDES:
        if not _exact(live, snap, side):
            bad.append(side)
            mark_rebuild(s, p, side, "rollback could not prove inode-identical restore")
        elif side not in already_pending:
            clear_rebuild(s, p, side)
    return bad


def _exact(live: Path, snap: Snapshot, side: str) -> bool:
    want = {k: v for k, v in snap.fingerprints.items() if k.split("/", 1)[0] == side}
    have = {}
    d = live / side
    if d.exists():
        for f in d.rglob("*"):
            if f.is_file() and not f.is_symlink():
                have[f"{side}/{f.relative_to(d).as_posix()}"] = _fp(f)
    return snap.linked and have == want


# ---- rebuild publishing ----------------------------------------------------------------------------

def recover_swaps(s: Settings, p: Pair) -> None:
    """A crash between the two renames of a directory swap leaves the old tree in scratch/trash-* and no
    live dir: put it back (the pending marker then forces a rebuild anyway)."""
    sc = scratch(s)
    pair_trash = sc / f"trash-{p.name}"
    pair_live = s.mirror / p.name
    if pair_trash.exists():
        for side in SIDES:
            mark_rebuild(s, p, side, "interrupted pair rollback")
        if not pair_live.exists():
            pair_live.parent.mkdir(parents=True, exist_ok=True)
            os.rename(pair_trash, pair_live)
        else:
            shutil.rmtree(pair_trash)
    for side in SIDES:
        trash = sc / f"trash-{p.name}-{side}"
        live = s.mirror / p.name / side
        if trash.exists():
            if live.exists():
                shutil.rmtree(trash, ignore_errors=True)
            else:
                live.parent.mkdir(parents=True, exist_ok=True)
                os.rename(trash, live)
                log.warning("recovered interrupted mirror swap for %s/%s", p.name, side)
    # Pair-wide trash is recovered above; do not discard the only remaining mirror.


def same_fs(a: Path, b: Path) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev


def publish_side(s: Settings, p: Pair, side: str, stage_side_dir: Path, stage_status: Path) -> None:
    """Atomically(ish) publish a COMPLETE staged side: mirror dir swap, then this side's status files.
    A crash in between is covered by the pending marker (set by the caller, cleared by it afterwards)."""
    live = s.mirror / p.name / side
    live.parent.mkdir(parents=True, exist_ok=True)
    _swap_in(stage_side_dir, live, scratch(s) / f"trash-{p.name}-{side}")
    root = status_root(s)
    root.mkdir(parents=True, exist_ok=True)
    for e in side_status_entries(root, p, side, keep_collections=False):
        shutil.rmtree(e) if e.is_dir() and not e.is_symlink() else e.unlink()
    for e in side_status_entries(stage_status, p, side, keep_collections=False):
        os.replace(e, root / e.name)                      # the discover cache (.collections) is config-keyed: left alone
