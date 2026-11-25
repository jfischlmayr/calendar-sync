"""Orchestration of the plan/apply engine for one pair: provider stores, persisted link
state, operator decisions, approved plans, write gates, reports.

Everything that can write to a provider funnels through ``run_pair_engine`` ->
``engine.apply_plan``. Modes:

  mirror   audit only. Providers are read, a plan is computed and reported, NOTHING is written.
  seed     writes only to side B (Google). Side A (iCloud) is never written.
  seed_a   writes only to side A (iCloud). Side B (Google) is never written (needs no approval for B).
  live     bidirectional.

Writes additionally need: CALSYNC_ALLOW_WRITES=1, no PAUSE, a fresh verified OFF-HOST backup,
no BLOCK marker, and - until the pair is initialised for that direction - an operator-approved
plan (``calsync plan`` + ``calsync approve-plan``) whose signed op list contains the op.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import canary as canary_mod
from . import engine as eng
from . import icsmodel as im
from .config import Pair, Settings
from .store import AuthError, CalendarStore
from .util import Blocked, log, now, read_json, write_json


# ------------------------------------------------------------------------------ stores

def make_stores(s: Settings, p: Pair, *, read_only: bool) -> dict[str, CalendarStore]:
    """Real provider stores (a = iCloud, b = Google). Tests inject their own."""
    from . import caldav, gauth
    from .config import env_secret
    tu = _test_urls()
    if tu:
        return {"a": caldav.CalDAVStore(f"{p.name}-a", tu[0], lambda: {}, read_only=read_only),
                "b": caldav.CalDAVStore(f"{p.name}-b", tu[1], lambda: {}, read_only=read_only)}
    user, pw = env_secret("ICLOUD_USER"), env_secret("ICLOUD_APP_PASSWORD")
    if not (user and pw):
        raise AuthError("ICLOUD_USER/ICLOUD_APP_PASSWORD missing")
    basic = caldav.basic_headers(user, pw)
    a_url = caldav.discover_icloud_collection(p.icloud_url or s.icloud_url, user, pw, p.icloud_collection)
    tok = gauth.GoogleTokenSource(s.tokens / "google.json", env_secret("GOOGLE_CLIENT_ID") or "",
                                  env_secret("GOOGLE_CLIENT_SECRET") or "")
    return {"a": caldav.CalDAVStore(f"{p.name}-icloud", a_url, basic, read_only=read_only),
            "b": caldav.CalDAVStore(f"{p.name}-google", caldav.google_collection_url(p.google_collection),
                                    tok.headers, read_only=read_only)}


def make_view_store(s: Settings, p: Pair) -> CalendarStore | None:
    """Optional READ-ONLY store of the iCloud birthday-view calendar (ledger only)."""
    if not p.birthday_view_collection:
        return None
    from . import caldav
    from .config import env_secret
    tu = os.environ.get("CALSYNC_TEST_V_URL")
    if tu:
        return caldav.CalDAVStore(f"{p.name}-view", tu, lambda: {}, read_only=True)
    user, pw = env_secret("ICLOUD_USER"), env_secret("ICLOUD_APP_PASSWORD")
    if not (user and pw):
        raise AuthError("ICLOUD_USER/ICLOUD_APP_PASSWORD missing")
    url = caldav.discover_icloud_collection(p.icloud_url or s.icloud_url, user, pw, p.birthday_view_collection)
    return caldav.CalDAVStore(f"{p.name}-birthday-view", url, caldav.basic_headers(user, pw), read_only=True)


def _test_urls():
    a, b = os.environ.get("CALSYNC_TEST_A_URL"), os.environ.get("CALSYNC_TEST_B_URL")
    return (a, b) if a and b else None


# ------------------------------------------------------------------------------ state files

def engine_config(s: Settings, p: Pair) -> eng.EngineConfig:
    ack = read_json(s.state / f"ack_deletes_{p.name}.json", {})
    extra = ack.get("max", 0) if ack.get("expires", 0) > now() else 0
    return eng.EngineConfig(
        self_addresses=frozenset(a.lower() for a in s.self_addresses), mode=p.mode,
        propagate_deletes=s.propagate_deletes, max_creates=s.max_creates_per_run,
        max_updates=s.max_updates_per_run, max_deletes=s.max_delete_abs, max_delete_pct=s.max_delete_pct,
        allow_extra_deletes=extra, max_burst=s.max_burst_ops)


def state_path(s: Settings, p: Pair) -> Path:
    return s.state / f"engine_{p.name}.json"


def load_state(s: Settings, p: Pair) -> dict:
    st = read_json(state_path(s, p))
    if st is None:
        st = eng.new_state()
    if not isinstance(st.get("initialized"), dict):          # migrate bool -> per direction
        st["initialized"] = {"a": bool(st.get("initialized")), "b": bool(st.get("initialized"))}
    return st


def load_decisions(s: Settings, p: Pair) -> list[dict]:
    d = read_json(s.decisions / f"{p.name}.json", [])
    return d if isinstance(d, list) else []


def block_marker(s: Settings, p: Pair) -> Path:
    return s.state / f"BLOCK_{p.name}"


# ------------------------------------------------------------------------------ plan files / approval

def _plan_sha(sigs: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(sigs)).encode()).hexdigest()


DIRECTIONS = ("a", "b")        # destination side of the ops: a = iCloud, b = Google
MAX_CANARY = 10                # largest filtered (canary) plan
MAX_BATCH = 25                 # largest filtered plan of the wider "batch" profile


def plan_file(s: Settings, p: Pair, direction: str) -> Path:
    assert direction in DIRECTIONS
    return s.plans / f"{p.name}.{direction}.json"


def write_plan_file(s: Settings, p: Pair, plan: eng.Plan, direction: str, only: list[str] | None = None) -> dict:
    """Store the ops that WRITE TO ``direction`` for approval. Each direction is approved separately.

    ``only`` restricts the plan to the *create* ops of exactly these source ids (canary). Every id must be a
    planned create for this direction, otherwise nothing is written. Ops outside the selection are not in
    the approved set and therefore never run, and the direction is not marked initialised."""
    ops_all = [o for o in plan.ops if o.dst == direction]
    if only is not None:
        want = set(only)
        if not want:
            raise Blocked("empty selection")
        creates = {o.src_id: o for o in ops_all if o.kind == "create"}
        unknown = sorted(want - set(creates))
        if unknown:
            raise Blocked(f"not a planned create for direction {direction}: {', '.join(unknown)}")
        ops_all = [creates[i] for i in sorted(want)]
    sigs = sorted(o.sig for o in ops_all)
    ops = _count_ops(ops_all)
    doc = {"version": 2, "pair": p.name, "direction": direction, "generated_at": now(),
           "plan_sha256": _plan_sha(sigs), "sigs": sigs, "ops": ops, "findings": plan.stats.get("findings", {})}
    if only is not None:
        doc["selection"] = sorted(only)
        doc["selection_fp"] = {o.src_id: o.src_full[:16] for o in ops_all}
    write_json(plan_file(s, p, direction), doc)
    os.chmod(plan_file(s, p, direction), 0o600)
    return doc


def _count_ops(ops) -> dict:
    out: dict = {}
    for o in ops:
        out[o.kind] = out.get(o.kind, 0) + 1
    return out


def approve_plan(s: Settings, p: Pair, by: str, note: str, expect_sha: str, direction: str) -> dict:
    doc = read_json(plan_file(s, p, direction))
    if not doc:
        raise Blocked(f"no plan file for direction {direction}; run `calsync plan` first")
    if not by.strip():
        raise Blocked("approver required")
    if _plan_sha(doc["sigs"]) != doc["plan_sha256"]:
        raise Blocked("plan file inconsistent")
    if doc["plan_sha256"] != expect_sha:
        raise Blocked("plan_sha256 mismatch: the plan changed since you reviewed it")
    doc["approval"] = {"by": by.strip(), "note": note, "at": now(), "plan_sha256": doc["plan_sha256"]}
    write_json(plan_file(s, p, direction), doc)
    os.chmod(plan_file(s, p, direction), 0o600)
    return doc


def approved_by_dst(s: Settings, p: Pair) -> dict[str, set[str]]:
    """{direction: approved op signatures}; only directions with a valid, untampered approval."""
    out: dict[str, set[str]] = {}
    for d in DIRECTIONS:
        doc = read_json(plan_file(s, p, d))
        try:
            ap = doc["approval"]
            if doc["direction"] == d and ap["plan_sha256"] == doc["plan_sha256"] == _plan_sha(doc["sigs"]):
                out[d] = set(doc["sigs"])
        except (TypeError, KeyError):
            pass
    return out


def bind_selection(s: Settings, p: Pair, plan: eng.Plan, appr: dict[str, set[str]]) -> dict[str, set[str]]:
    """A canary approval is also bound to the content the operator saw: a selected create whose source was
    edited since ``calsync plan`` (fingerprint changed) loses its approval and is skipped."""
    out = {d: set(v) for d, v in appr.items()}
    for d in out:
        sel = (read_json(plan_file(s, p, d)) or {}).get("selection_fp")
        if not sel:
            continue
        for o in plan.ops:
            if o.dst == d and o.kind == "create" and o.src_id in sel and o.src_full[:16] != sel[o.src_id]:
                out[d].discard(o.sig)
                log.warning("%s: canary item %s changed since planning: approval withdrawn", p.name, o.src_id)
    return out


# ------------------------------------------------------------------------------ gates

def gate_failures(s: Settings, p: Pair) -> list[str]:
    f: list[str] = []
    if not s.write_enabled:
        f.append("CALSYNC_ALLOW_WRITES!=1")
    if p.mode not in ("seed", "seed_a", "live"):
        f.append(f"pair mode is {p.mode}")
    if (s.state / "PAUSE").exists():
        f.append("state/PAUSE kill-switch present")
    if block_marker(s, p).exists():
        f.append(f"BLOCK marker present: {read_json(block_marker(s, p), {}).get('reason', 'see state')}")
    b = read_json(s.state / "backup_ok.json", {})
    age = now() - b.get("ts", 0)
    if age > s.backup_max_age_h * 3600 or age < -300:
        f.append("no fresh verified backup")
    elif not b.get("offhost") and os.environ.get("CALSYNC_BACKUP_ALLOW_LOCAL_ONLY") != "1":
        f.append("latest backup is not off-host")
    if not s.self_addresses:
        f.append("global.self_addresses is empty (guest detection would treat every attendee as foreign)")
    return f


# ------------------------------------------------------------------------------ reports

def public_report(p: Pair, plan: eng.Plan, extra: dict | None = None) -> dict:
    """Counts and opaque ids only: no titles, no addresses."""
    return {"pair": p.name, "mode": p.mode, "generated_at": now(), "plan_sha256": plan.plan_sha(),
            "stats": plan.stats, "findings": [{k: v for k, v in f.items()} for f in plan.findings],
            "ops": [{"kind": o.kind, "dst": o.dst, "src_id": o.src_id, "reason": o.reason, "sig": o.sig} for o in plan.ops],
            **(extra or {})}


def detail_report(plan: eng.Plan) -> dict:
    """Contains titles/times/addresses: written 0600 under /data/reports, never logged."""
    def row(r: im.Resource):
        m = r.master or (r.comps[0] if r.comps else None)
        return {"id": r.ident, "fp": r.full[:16], "kind": r.kind, "reasons": r.reasons, "href": r.href,
                "title": m.summary if m else "", "start": m.start if m else "", "rrule": m.rrule if m else "",
                "overrides": len(r.overrides), "exdates": len(m.exdates) if m else 0}
    return {"a": [row(r) for r in plan.sides["a"].values()], "b": [row(r) for r in plan.sides["b"].values()]}


def write_reports(s: Settings, p: Pair, plan: eng.Plan, detail: bool, extra: dict | None = None) -> dict:
    s.reports.mkdir(parents=True, exist_ok=True)
    pub = public_report(p, plan, extra)
    stamp = time_stamp()
    path = s.reports / f"{p.name}-engine-{stamp}.json"
    write_json(path, pub)
    os.chmod(path, 0o600)
    pub["report"] = path.name
    if detail:
        dp = s.reports / f"{p.name}-engine-detail-{stamp}.json"
        write_json(dp, detail_report(plan))
        os.chmod(dp, 0o600)
        pub["detail_report"] = dp.name
    return pub


def time_stamp() -> str:
    import datetime
    return datetime.datetime.fromtimestamp(now(), datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ------------------------------------------------------------------------------ core run

def read_plan(s: Settings, p: Pair, stores: dict[str, CalendarStore]) -> tuple[eng.Plan, dict, eng.EngineConfig]:
    cfg = engine_config(s, p)
    a_items = stores["a"].list_items()
    b_items = stores["b"].list_items()
    st = load_state(s, p)
    import datetime
    today = datetime.datetime.fromtimestamp(now(), datetime.timezone.utc).date()
    return eng.build_plan(a_items, b_items, st, load_decisions(s, p), cfg, today=today, now_ts=now()), st, cfg


def audit_pair(s: Settings, p: Pair, stores: dict[str, CalendarStore] | None = None, *, detail: bool = False,
               write_plan: bool | tuple = False, view: CalendarStore | None = None,
               only: list[str] | None = None, canary: int | None = None, since_days_ago: int = 0,
               profile: str = "canary") -> dict:
    """Read-only. Never writes to a provider (stores are created read_only).
    ``only`` / ``canary`` (needs exactly one direction in ``write_plan``) store a filtered plan, see
    :func:`write_plan_file` and :mod:`calsync.canary`. ``profile="batch"`` selects the wider eligibility
    whitelist and the larger size limit (MAX_BATCH) for the bulk migration after a successful canary."""
    from . import birthdays
    stores = stores or make_stores(s, p, read_only=True)
    plan, st, cfg = read_plan(s, p, stores)
    view = view or make_view_store(s, p)
    view_res = eng.analyze(view.list_items(), "v", cfg) if view is not None else None
    extra = {"initialized": st["initialized"], "approved_directions": sorted(approved_by_dst(s, p)),
             "gates": gate_failures(s, p) if p.mode in ("seed", "seed_a", "live") else ["pair is not in a write mode"],
             "birthdays": birthdays.ledger(plan.sides["a"], plan.sides["b"], view_res)}
    sel_detail = None
    if write_plan:
        dirs = DIRECTIONS if write_plan is True else tuple(write_plan)
        filtered = only is not None or canary is not None
        if profile not in canary_mod.PROFILES:
            raise Blocked(f"unknown profile {profile!r}")
        if profile != "canary" and not filtered:
            raise Blocked("--profile only applies to --canary / --only")
        if since_days_ago and not filtered:
            raise Blocked("--since-days-ago only applies to --canary / --only")
        if since_days_ago and profile == "batch":
            raise Blocked("--since-days-ago is a canary option: the batch profile has no date window")
        if filtered and len(dirs) != 1:
            raise Blocked("a filtered plan (--only/--canary) needs exactly one direction")
        if filtered:
            import datetime
            today = datetime.datetime.fromtimestamp(now(), datetime.timezone.utc).date()
            limit = MAX_BATCH if profile == "batch" else MAX_CANARY
            if since_days_ago and not 1 <= since_days_ago <= canary_mod.MAX_SINCE_DAYS:
                raise Blocked(f"--since-days-ago must be between 1 and {canary_mod.MAX_SINCE_DAYS}")
            kw = dict(since_days_ago=since_days_ago, profile=profile, self_addresses=cfg.self_addresses)
            if canary is not None:
                if only is not None:
                    raise Blocked("use either --only or --canary")
                if not 1 <= canary <= limit:
                    raise Blocked(f"--canary must be between 1 and {limit}")
                picked = canary_mod.pick(plan, dirs[0], today, canary, **kw)
                if len(picked) < canary:
                    raise Blocked(f"only {len(picked)} safe {profile} candidates for direction {dirs[0]}, need {canary}: "
                                  "not selecting a weaker selection")
                only = [i for i, _s, _t in picked]
                sel_detail = [{"id": i, "start_utc": st_, "title": ti} for i, st_, ti in picked]
            elif len(set(only)) > limit:
                raise Blocked(f"--only is limited to {limit} ids with profile {profile}; "
                              "this is a small content-approved selection, not a bulk approval")
            bad = canary_mod.check_selected(plan, dirs[0], set(only), today, **kw)
            if bad:
                raise Blocked(f"selection contains non-{profile}-safe items: " +
                              "; ".join(f"{i}: {','.join(w)}" for i, w in sorted(bad.items())))
            if sel_detail is None:          # --only: titles go to the same 0600 detail file as --canary
                src_side = "b" if dirs[0] == "a" else "a"
                by_id = {r.ident: r for r in plan.sides[src_side].values()}
                sel_detail = [{"id": i, "start_utc": by_id[i].comps[0].start, "title": by_id[i].comps[0].summary}
                              for i in sorted(set(only)) if i in by_id]
        extra["plan_file"] = {d: write_plan_file(s, p, plan, d, only)["plan_sha256"] for d in dirs}
        if only is not None:
            extra["selection"] = sorted(only)

    rep = write_reports(s, p, plan, detail, extra)
    if sel_detail:          # titles: 0600, never printed by the CLI
        dp = s.reports / f"{p.name}-canary-{time_stamp()}.json"
        write_json(dp, {"direction": write_plan[0] if not isinstance(write_plan, bool) else "", "profile": profile,
                        "items": sel_detail})
        os.chmod(dp, 0o600)
        rep["canary_detail"] = dp.name
    return rep


def run_pair_engine(s: Settings, p: Pair, stores: dict[str, CalendarStore] | None = None) -> dict:
    """One engine cycle for a seed/live pair. Raises Blocked / eng.EngineBlocked / StoreError."""
    fails = gate_failures(s, p)
    if fails:
        raise Blocked(f"{p.name}: write gates closed: " + "; ".join(fails))
    from . import icsmodel
    if not icsmodel.tz_selfcheck():
        raise Blocked(f"{p.name}: time-zone database missing (zoneinfo): refusing to classify events")
    stores = stores or make_stores(s, p, read_only=False)
    plan, st, cfg = read_plan(s, p, stores)
    eng.check_outage_and_deletions(plan, st, cfg)
    appr = bind_selection(s, p, plan, approved_by_dst(s, p))
    needs = {o.dst for o in plan.ops if cfg.may_write(o.dst) and not st["initialized"].get(o.dst)}
    missing = sorted(needs - set(appr))
    if missing:
        raise Blocked(f"{p.name}: first-time ops for direction(s) {missing} but no approved plan "
                      "(calsync plan --direction ... / approve-plan)")
    burst = [o for o in plan.ops if cfg.may_write(o.dst) and o.kind in ("create", "update") and st["initialized"].get(o.dst)]
    if len(burst) > cfg.max_burst and not all(o.sig in appr.get(o.dst, ()) for o in burst):
        raise Blocked(f"{p.name}: {len(burst)} create/update ops in one cycle exceed max_burst_ops={cfg.max_burst}; "
                      "review (`calsync audit`) and approve a fresh plan")
    save = lambda state: write_json(state_path(s, p), state)
    try:
        res = eng.apply_plan(plan, stores, st, cfg, approved=appr, save=save, now=now())
    except eng.VerifyFailed as e:
        write_json(block_marker(s, p), {"reason": str(e), "ts": now()})
        save(st)
        raise
    # A direction becomes initialised only when EVERY planned op for it is done (approved and executed, or
    # rejected twice by the provider). An unapproved op (e.g. a brand-new event after approval) holds it back.
    for dst in DIRECTIONS:
        if cfg.may_write(dst) and not st["initialized"].get(dst) and dst in appr:
            outstanding = [o for o in plan.ops if o.dst == dst and o.sig not in res.executed
                           and st.get("rejected", {}).get(o.sig, {}).get("n", 0) < eng.MAX_REJECTIONS]
            if not outstanding:
                st["initialized"][dst] = True
    if cfg.allow_extra_deletes:                      # the acknowledgement is one-shot
        (s.state / f"ack_deletes_{p.name}.json").unlink(missing_ok=True)
    st["last_counts"] = {
        d: len(plan.sides[d]) + res.done.get(f"create_{d}", 0) - res.done.get(f"delete_{d}", 0) for d in ("a", "b")}
    save(st)
    write_reports(s, p, plan, False, {"applied": res.done, "skipped": res.skipped})
    return {"mode": p.mode, "applied": res.done, "skipped": res.skipped, "findings": plan.stats["findings"],
            "initialized": st["initialized"], "rejected_ops": len(st.get("rejected", {})), "errors": res.errors}
