"""Sync cycle: lock -> read-only vdirsyncer mirror of both remotes (backup source) -> per mode:
mirror: periodic read-only engine audit; seed/live: plan/apply engine (syncrun) with all gates.
vdirsyncer NEVER writes to a provider any more (it only fills the local filesystem mirror)."""
from __future__ import annotations

import fcntl
import os
import random
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from . import alert as alertmod
from . import health, ics, mirrorsafe, syncrun, vdsconf
from .config import Pair, Settings
from .store import AuthError
from .util import Blocked, log, now, read_json, redact, write_json

VDIRSYNCER = os.environ.get("VDIRSYNCER_BIN", "vdirsyncer")
VDS_TIMEOUT = int(os.environ.get("CALSYNC_VDS_TIMEOUT", "300"))


@contextmanager
def single_instance(s: Settings):
    s.state.mkdir(parents=True, exist_ok=True)
    f = open(s.state / "lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise Blocked("another calsync instance holds the lock")
    f.write(str(os.getpid()))
    f.flush()
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def vds(s: Settings, conf: str, *args: str) -> tuple[int, str]:
    """Run vdirsyncer with a throw-away 0600 config, no stdin (so it can never
    answer 'create collection?' prompts), output redacted."""
    s.run_dir.mkdir(parents=True, exist_ok=True)
    assert "--force-delete" not in args
    cfg = s.run_dir / f"vds-{os.getpid()}-{random.randrange(1<<30)}.conf"
    fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(conf)
    try:
        p = subprocess.run(
            [VDIRSYNCER, "-c", str(cfg), *args], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=VDS_TIMEOUT,
        )
        return p.returncode, redact(p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, "vdirsyncer timed out"
    finally:
        cfg.unlink(missing_ok=True)


class StaleMirrorStatus(RuntimeError):
    """vdirsyncer wanted to write the READ-ONLY remote because the local mirror no longer matches its
    status (PartialSync). The remote was not written; the status is untrustworthy -> rebuild the side."""

    def __init__(self, side: str, msg: str):
        super().__init__(msg)
        self.side = side


def _is_partial_sync(out: str) -> bool:
    return "Attempted change on" in out and "read-only" in out


def _sync_side(s: Settings, p: Pair, side: str) -> None:
    conf, pair = vdsconf.mirror_conf(p, s, side)
    rc, out = vds(s, conf, "discover", pair)
    if rc == 0:
        rc, out = vds(s, conf, "sync", pair)
    if rc != 0:
        msg = f"mirror {p.name}/{side} failed: {out.strip()[-600:]}"
        if _is_partial_sync(out):
            raise StaleMirrorStatus(side, msg)
        raise RuntimeError(msg)


def _mirror(s: Settings, p: Pair, side: str) -> None:
    _sync_side(s, p, side)


def _rebuild_side(s: Settings, p: Pair, side: str) -> dict:
    """Fresh READ-ONLY fetch of one remote into scratch staging (own empty status), then atomic publish.
    The live mirror/status stay untouched unless the staged read is complete and passes the deletion guard;
    on any failure they are kept as they were. Not run on a normal cycle (only after a rollback that could
    not be proven exact, or after PartialSync)."""
    sc = mirrorsafe.scratch(s)
    stage = sc / f"rebuild-{p.name}-{side}"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    live_side = s.mirror / p.name / side
    live_side.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not mirrorsafe.same_fs(stage, live_side.parent):
            raise RuntimeError("scratch is not on the mirror filesystem; refusing a non-atomic publish")
        mroot, sroot = stage / "mirror" / p.name, stage / "status"
        conf, pair = vdsconf.mirror_conf(p, s, side, mirror_root=mroot, status_root=sroot)
        rc, out = vds(s, conf, "discover", pair)
        if rc == 0:
            rc, out = vds(s, conf, "sync", pair)
        if rc != 0:
            raise RuntimeError(f"mirror rebuild {p.name}/{side} failed, previous mirror kept: {out.strip()[-500:]}")
        new_uids, old_uids = ics.uid_set(mroot / side), ics.uid_set(live_side) if live_side.exists() else set()
        ack = read_json(s.state / f"ack_deletes_{p.name}.json", {})
        extra = ack.get("max", 0) if ack.get("expires", 0) > now() else 0
        vanished = len(old_uids - new_uids)
        if old_uids and not new_uids and not extra:
            raise Blocked(f"mirror rebuild {p.name}/{side}: remote returned ZERO items but {len(old_uids)} are mirrored: treating as outage, mirror kept")
        limit = max(s.max_delete_abs, int(len(old_uids) * s.max_delete_pct / 100))
        if vanished > limit + extra:
            raise Blocked(f"mirror rebuild {p.name}/{side}: {vanished} items vanished (limit {limit}), mirror kept; "
                          "acknowledge with `calsync ack-deletes` after checking the provider")
        mirrorsafe.publish_side(s, p, side, mroot / side, sroot)
        mirrorsafe.clear_rebuild(s, p, side)
        return {"items": len(new_uids), "vanished": vanished, "added": len(new_uids - old_uids)}
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _heal_pending(s: Settings, p: Pair, res: dict) -> None:
    mirrorsafe.recover_swaps(s, p)
    for side in mirrorsafe.SIDES:
        if side in mirrorsafe.pending_rebuild(s, p):
            r = _rebuild_side(s, p, side)
            res.setdefault("mirror_rebuilt", {})[side] = r
            log.warning("mirror %s/%s rebuilt from remote (read-only): %s", p.name, side, r)


def uids(s: Settings, p: Pair, side: str) -> set[str]:
    return ics.uid_set(s.mirror / p.name / side)


def export_collection(s: Settings, side: str, collection: str, out_dir: Path) -> int:
    """Read-only export; returns number of .ics files. out_dir must be empty or absent."""
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise RuntimeError(f"{out_dir} not empty; refusing to export into it")
    target = out_dir / "c" if not vdsconf._test_urls() else out_dir
    conf, pair = vdsconf.export_conf(s, side, collection, target)
    shutil.rmtree(s.run_dir / "export-status", ignore_errors=True)
    rc, out = vds(s, conf, "discover", pair)
    if rc == 0:
        rc, out = vds(s, conf, "sync", pair)
    if rc != 0:
        raise RuntimeError(f"export failed: {out.strip()[-500:]}")
    n = len(list(target.glob("*.ics")))
    if target != out_dir:
        for f in target.glob("*.ics"):
            f.rename(out_dir / f.name)
        shutil.rmtree(target, ignore_errors=True)
    return n


# ---- one pair ---------------------------------------------------------------

def _rollback(s: Settings, p: Pair, snap) -> None:
    """Scoped restore of this pair only. Never raises: if the restore fails or cannot be proven exact the
    pair's sides are marked rebuild-pending instead (the next run rebuilds them from the remote)."""
    if snap is None:
        return
    try:
        mirrorsafe.rollback(s, p, snap)
    except Exception as e:
        log.error("rollback of %s failed (%s); sides marked for rebuild", p.name, redact(str(e))[:200])
        for side in mirrorsafe.SIDES:
            try:
                mirrorsafe.mark_rebuild(s, p, side, f"rollback failed: {type(e).__name__}")
            except Exception:
                pass


def _maybe_audit(s: Settings, p: Pair, res: dict) -> None:
    """Read-only engine audit for mirror-mode pairs at most every audit_interval_s. Failures are
    recorded in health but never fail the mirror run (the audit is advisory)."""
    f = s.state / f"audit_{p.name}.json"
    last = read_json(f, {"ts": 0})
    if now() - last.get("ts", 0) < s.audit_interval_s:
        return
    try:
        rep = syncrun.audit_pair(s, p)
        summary = {"ts": now(), "stats": rep["stats"], "plan_sha256": rep["plan_sha256"], "report": rep["report"]}
        write_json(f, {"ts": now()})
        res["engine_audit"] = summary                 # persisted to health by run_pair
        res["engine_audit_error"] = None
    except Exception as e:  # advisory only
        write_json(f, {"ts": now() - s.audit_interval_s + 600})      # retry in ~10 min
        msg = ("AUTH: " if isinstance(e, AuthError) else "") + redact(str(e))[:300]
        health.update(s, p.name, engine_audit_error=msg)
        log.warning("engine audit failed for %s: %s", p.name, msg)
        if isinstance(e, AuthError):
            alertmod.alert(s, f"auth_{p.name}", f"AUTH FAILURE {p.name}", msg)


def run_pair(s: Settings, p: Pair, _healed: bool = False) -> dict:
    ps = s.state / f"pair_{p.name}.json"
    st = read_json(ps, {"failures": 0, "next_allowed": 0})
    if now() < st["next_allowed"] and not _healed:
        return {"skipped": "rate-limit/backoff"}
    snap = None
    res: dict = {"mode": p.mode}
    try:
        _heal_pending(s, p, res)                    # own atomic publishes, outside the snapshot/rollback window
        snap = mirrorsafe.snapshot(s, p)
        _mirror(s, p, "a")
        _mirror(s, p, "b")
        cur = {"a": uids(s, p, "a"), "b": uids(s, p, "b")}
        res["counts"] = {k: len(v) for k, v in cur.items()}
        res["guest_events"] = {k: sum(e.has_guests for e in ics.read_dir(s.mirror / p.name / k)) for k in ("a", "b")}
        if p.mode in ("seed", "seed_a", "live"):
            r = syncrun.run_pair_engine(s, p)       # gates, guards, approved plan, conditional writes
            res.update(r)
            if r.get("applied"):
                _mirror(s, p, "a")
                _mirror(s, p, "b")
                cur = {"a": uids(s, p, "a"), "b": uids(s, p, "b")}
                res["counts"] = {k: len(v) for k, v in cur.items()}
        else:
            _maybe_audit(s, p, res)
        if not (s.state / f"last_uids_{p.name}.json").exists() or p.mode in ("seed", "seed_a", "live"):
            write_json(s.state / f"last_uids_{p.name}.json", {k: sorted(v) for k, v in cur.items()})
        st.update(failures=0, next_allowed=max(st["next_allowed"], now() + s.min_interval_s))
        write_json(ps, st)
        health.update(s, p.name, last_success=now(), blocked=None, last_error=None, **res)
        alertmod.alert(s, f"fail_{p.name}", p.name, "sync healthy again", recovery=True)
        return res
    except StaleMirrorStatus as e:
        _rollback(s, p, snap)
        mirrorsafe.mark_rebuild(s, p, e.side, "PartialSync: local mirror no longer matches vdirsyncer status")
        if not _healed:                             # one immediate, bounded self-heal attempt (read-only rebuild)
            mirrorsafe.discard(snap)
            snap = None
            return run_pair(s, p, _healed=True)
        st["failures"] += 1
        st["next_allowed"] = max(st["next_allowed"], now() + min(s.max_backoff_s, s.interval_s * 2 ** min(st["failures"], 6)))
        write_json(ps, st)
        msg = redact(str(e))
        health.update(s, p.name, last_error=msg, failures=st["failures"], last_attempt=now())
        log.error("FAILED %s: %s", p.name, msg)
        if st["failures"] >= s.alert_after_failures:
            alertmod.alert(s, f"fail_{p.name}", f"sync FAILING {p.name} ({st['failures']}x)", msg)
        return {"error": msg}
    except Blocked as e:
        _rollback(s, p, snap)
        st["failures"] += 1
        write_json(ps, st)
        health.update(s, p.name, blocked=str(e), last_error=str(e), last_attempt=now())
        log.error("BLOCKED %s", e)
        alertmod.alert(s, f"blocked_{p.name}", f"BLOCKED {p.name}", str(e))
        return {"blocked": str(e)}
    except AuthError as e:
        _rollback(s, p, snap)
        st["failures"] += 1
        st["next_allowed"] = max(st["next_allowed"], now() + min(s.max_backoff_s, s.interval_s * 2 ** min(st["failures"], 6)))
        write_json(ps, st)
        msg = "AUTH: " + redact(str(e))
        health.update(s, p.name, blocked=msg, last_error=msg, failures=st["failures"], last_attempt=now())
        log.error("AUTH FAILURE %s: %s", p.name, msg)
        alertmod.alert(s, f"auth_{p.name}", f"AUTH FAILURE {p.name}", msg)
        return {"error": msg}
    except Exception as e:
        _rollback(s, p, snap)
        st["failures"] += 1
        st["next_allowed"] = max(st["next_allowed"], now() + min(s.max_backoff_s, s.interval_s * 2 ** min(st["failures"], 6)))
        write_json(ps, st)
        msg = redact(str(e))
        health.update(s, p.name, last_error=msg, failures=st["failures"], last_attempt=now())
        log.error("FAILED %s: %s", p.name, msg)
        if st["failures"] >= s.alert_after_failures:
            alertmod.alert(s, f"fail_{p.name}", f"sync FAILING {p.name} ({st['failures']}x)", msg)
        return {"error": msg}
    finally:
        mirrorsafe.discard(snap)


def run_cycle(s: Settings, only: str | None = None) -> dict:
    out = {}
    with single_instance(s):
        for p in s.pairs:
            if p.mode == "disabled" or (only and p.name != only):
                continue
            out[p.name] = run_pair(s, p)
        health.update(s, None, last_cycle=now())
    return out


def serve(s: Settings) -> None:
    health.serve(s)
    log.info("calsync serving: %d pair(s), interval %ss, writes_enabled=%s", len(s.pairs), s.interval_s, s.write_enabled)
    health.update(s, None, started=now(), writes_enabled=s.write_enabled)
    while True:
        try:
            r = run_cycle(s)
            if r:
                log.info("cycle: %s", r)
            from . import scheduler_backup
            scheduler_backup.maybe_run(s)
        except Blocked as e:
            log.warning("cycle skipped: %s", e)
        except Exception as e:
            log.exception("cycle crashed: %s", redact(str(e)))
            alertmod.alert(s, "cycle_crash", "cycle crashed", redact(str(e)))
        time.sleep(s.interval_s * random.uniform(0.9, 1.1))
