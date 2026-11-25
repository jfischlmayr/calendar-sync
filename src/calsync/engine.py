"""Plan/apply sync engine (replaces vdirsyncer for every write).

Why a custom engine: vdirsyncer pairs items by href/UID only, so on a first sync of two
populated calendars it copies every one-sided item, duplicates different-UID twins, copies
guest invitations (which can send notifications / break RSVP state), copies birthday series
and cannot honour any exemption or review decision. Here the *plan is the execution*:
nothing is written that is not an operation in a plan, and the plan is built from explicit
classification (exempt kinds), duplicate analysis, persisted links and operator decisions.

Pure part:   ``build_plan(...)``   - no I/O, deterministic, used for dry-run audit and apply.
Impure part: ``apply_plan(...)``   - conditional writes (If-None-Match/If-Match), verify-after-
                                    write, per-op persisted state, caps, fail-closed on anomalies.

Exempt kinds (never created, updated or deleted on either side, in any mode):
  guest        organiser/attendee other than me -> RSVP + notifications must stay untouched
  birthday     yearly all-day series / contact-linked birthdays -> separate ledger, no duplicates
  unsupported  orphan overrides, RANGE=THISANDFUTURE, RDATE, EXRULE, no UID, VTODO, unknown TZ ...
  ignored      operator decision
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import canary as _canary
from . import icsmodel as im
from .store import AuthError, CalendarStore, Item, PreconditionFailed, RateLimited, StoreError
from .util import Blocked

STATE_VERSION = 1
OP_ORDER = {"create": 0, "update": 1, "delete": 2}
FLAP_LIMIT = 4              # updates of one link per hour before it is frozen as flapping
MAX_REJECTIONS = 2          # a provider-rejected op is retried once, then left for the operator


class EngineBlocked(Blocked):
    """A safety guard refused the run. Nothing (further) was written."""


class VerifyFailed(EngineBlocked):
    """A write was read back and differs from its source: stop writing to this pair."""


# ------------------------------------------------------------------------------ settings

@dataclass
class EngineConfig:
    self_addresses: frozenset[str] = frozenset()
    mode: str = "mirror"                  # mirror (audit only) | seed (write side b only) | seed_a (side a only) | live
    propagate_deletes: bool = False       # off by default: deletions are reported, not executed
    max_creates: int = 25
    max_updates: int = 25
    max_deletes: int = 3
    max_delete_pct: float = 10.0
    allow_extra_deletes: int = 0          # one-shot acknowledgement (ack-deletes)
    max_burst: int = 100                  # steady-state creates+updates per cycle above which approval is required again

    def may_write(self, dst: str) -> bool:
        return self.mode == "live" or (self.mode == "seed" and dst == "b") or (self.mode == "seed_a" and dst == "a")


# ------------------------------------------------------------------------------ data classes

@dataclass(frozen=True)
class Op:
    kind: str                 # create | update | delete
    dst: str                  # side that is written: 'a' | 'b'
    link: str                 # link id ("" for create)
    src_id: str               # public id of the source item ("" for delete)
    src_href: str
    src_etag: str
    src_full: str
    dst_href: str             # "" for create
    dst_etag: str
    dst_full: str
    retarget_uid: str         # "" unless an adopted duplicate has a different UID on dst
    reason: str

    @property
    def sig(self) -> str:
        """Approval key. A create is approved as "copy item X to side D" - editing X afterwards
        does not invalidate that (the current version is copied). Updates and deletes are bound
        to the exact content they were planned from."""
        if self.kind == "create":
            parts = [self.kind, self.dst, self.src_id]
        else:
            parts = [self.kind, self.dst, self.link, self.src_id, self.src_href, self.src_full,
                     self.dst_href, self.dst_full, self.retarget_uid]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


@dataclass
class Plan:
    ops: list[Op] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)       # state-only changes (adopt/rebase/drop/relink)
    stats: dict = field(default_factory=dict)
    sides: dict = field(default_factory=dict)               # {'a': {href: Resource}, 'b': {...}}

    def sig_set(self) -> set[str]:
        return {o.sig for o in self.ops}

    def plan_sha(self) -> str:
        return hashlib.sha256(json.dumps(sorted(self.sig_set())).encode()).hexdigest()


def new_state() -> dict:
    return {"version": STATE_VERSION, "initialized": False, "links": {}, "last_counts": {}}


def link_id(a_uid: str, b_uid: str) -> str:
    return hashlib.sha1(f"{a_uid}\x00{b_uid}".encode(), usedforsecurity=False).hexdigest()[:16]  # stable link id, not a security control; changing it would orphan stored links


# ------------------------------------------------------------------------------ analysis

def analyze(items: Iterable[Item], side: str, cfg: EngineConfig, ignored: set[str] = frozenset()) -> dict[str, im.Resource]:
    out: dict[str, im.Resource] = {}
    for it in items:
        r = im.parse_resource(it.data, it.href, it.etag)
        r.kind, r.reasons = im.classify(r, cfg.self_addresses)
        r.core, r.full, r.strict = im.core_fp(r), im.full_fp(r), im.strict_key(r)
        r.ident = im.item_id(side, r.uid or f"href:{it.href}")
        if r.ident in ignored and r.kind != "unsupported":
            r.kind, r.reasons = "ignored", ["operator_decision"]
        out[it.href] = r
    by_uid: dict[str, list[str]] = {}
    for h, r in out.items():
        if r.uid:
            by_uid.setdefault(r.uid, []).append(h)
    for uid, hrefs in by_uid.items():
        if len(hrefs) > 1:
            for h in hrefs:
                out[h].kind, out[h].reasons = "unsupported", sorted(set(out[h].reasons) | {"duplicate_uid_in_calendar"})
    return out


def _finding(kind: str, **kw) -> dict:
    return {"kind": kind, **kw}


def _decision_index(decisions: list[dict]) -> dict:
    idx: dict[tuple, dict] = {}
    for d in decisions or []:
        idx[(d.get("action"), d.get("side", ""), d.get("id", ""), d.get("a", ""), d.get("b", ""))] = d
    return idx


def ignored_ids(decisions: list[dict]) -> set[str]:
    return {d["id"] for d in decisions or [] if d.get("action") == "ignore" and d.get("id")}


# ------------------------------------------------------------------------------ planning

def build_plan(a_items: Iterable[Item], b_items: Iterable[Item], state: dict, decisions: list[dict],
               cfg: EngineConfig, today=None, now_ts: float = 0.0) -> Plan:
    ign = ignored_ids(decisions)
    A = analyze(a_items, "a", cfg, ign)
    B = analyze(b_items, "b", cfg, ign)
    plan = Plan(sides={"a": A, "b": B})
    sides = {"a": A, "b": B}
    other = {"a": "b", "b": "a"}
    by_uid = {s: {r.uid: r for r in rs.values() if r.uid} for s, rs in sides.items()}
    consumed = {"a": set(), "b": set()}
    survivors: dict[str, list] = {"a": [], "b": []}     # surviving halves of links whose other side vanished
    links: dict = state.get("links", {})

    def dec(action, **kw):
        for d in reversed(decisions or []):          # the newest matching decision wins (an older stale one must not shadow it)
            if d.get("action") == action and all(d.get(k) == v for k, v in kw.items()):
                return d
        return None

    def bound(d, *res) -> bool:
        """A decision is valid only while the item(s) it was made for are unchanged."""
        if d is None:
            return False
        fps = d.get("fp", [])
        if isinstance(fps, str):
            fps = [fps]
        fps = [f for f in fps if isinstance(f, str) and len(f) >= 8]
        return bool(fps) and all(any(r.full.startswith(f) for f in fps) for r in res)

    def add_op(kind, dst, lid, src, dstres, reason, retarget=""):
        plan.ops.append(Op(kind=kind, dst=dst, link=lid, src_id=src.ident if src else "",
                           src_href=src.href if src else "", src_etag=src.etag if src else "",
                           src_full=src.full if src else "", dst_href=dstres.href if dstres else "",
                           dst_etag=dstres.etag if dstres else "", dst_full=dstres.full if dstres else "",
                           retarget_uid=retarget, reason=reason))

    def held(src, other_res) -> bool:
        """True (and a finding is recorded) if copying ``src`` is known to be rewritten by the receiving provider."""
        why = _canary.provider_rewrite_risks(src)
        if why:
            plan.findings.append(_finding("held_provider_rewrite", side=other_res, ids=[src.ident],
                                          detail=",".join(why) + "; not copied automatically (provider would alter it)"))
        return bool(why)

    # ---- 1. existing links ------------------------------------------------------
    for lid, L in sorted(links.items()):
        ra = A.get(L["a_href"]) if A.get(L["a_href"]) and A[L["a_href"]].uid == L["a_uid"] else by_uid["a"].get(L["a_uid"])
        rb = B.get(L["b_href"]) if B.get(L["b_href"]) and B[L["b_href"]].uid == L["b_uid"] else by_uid["b"].get(L["b_uid"])
        if ra:
            consumed["a"].add(ra.href)
        if rb:
            consumed["b"].add(rb.href)
        if ra and rb:
            if ra.kind != "normal" or rb.kind != "normal":
                plan.findings.append(_finding("frozen_link", ids=[ra.ident, rb.ident],
                                              detail=f"a:{ra.kind} b:{rb.kind}; link untouched"))
                continue
            if ra.href != L["a_href"] or rb.href != L["b_href"]:
                plan.changes.append({"t": "relink", "link": lid, "a_href": ra.href, "b_href": rb.href})
            cha, chb = ra.full != L["a_fp"], rb.full != L["b_fp"]
            if not cha and not chb:
                if ra.etag != L.get("a_etag") or rb.etag != L.get("b_etag"):
                    plan.changes.append({"t": "rebase", "link": lid, "a": [ra.etag, ra.full], "b": [rb.etag, rb.full]})
                continue
            rt_b = L["b_uid"] if L["a_uid"] != L["b_uid"] else ""
            rt_a = L["a_uid"] if L["a_uid"] != L["b_uid"] else ""
            same = ra.core == rb.core and im.alarm_fp(ra) == im.alarm_fp(rb)
            if same:                 # nothing to write (e.g. identical edit on both sides, or volatile-only change)
                plan.changes.append({"t": "rebase", "link": lid, "a": [ra.etag, ra.full], "b": [rb.etag, rb.full]})
                continue
            if cha and chb:
                da, db = dec("prefer", side="a", id=ra.ident), dec("prefer", side="b", id=rb.ident)
                if bound(da, ra) and not db:
                    if not held(ra, "b"):
                        add_op("update", "b", lid, ra, rb, "prefer_a_decision", rt_b)
                elif bound(db, rb) and not da:
                    if not held(rb, "a"):
                        add_op("update", "a", lid, rb, ra, "prefer_b_decision", rt_a)
                else:
                    plan.findings.append(_finding("conflict", ids=[ra.ident, rb.ident], fp=[ra.full[:16], rb.full[:16]],
                                                  detail="edited on both sides since last sync; neither overwritten"))
                continue
            recent = [t for t in L.get("upd", []) if now_ts and now_ts - t < 3600]
            if len(recent) >= FLAP_LIMIT:
                plan.findings.append(_finding("flapping_link", ids=[ra.ident, rb.ident],
                                              detail=f"{len(recent)} updates in the last hour; frozen (provider rewrites data?)"))
                continue
            if cha:      # also when only non-core properties (alarms ...) changed
                if not held(ra, "b"):
                    add_op("update", "b", lid, ra, rb, "changed_on_a", rt_b)
            elif not held(rb, "a"):
                add_op("update", "a", lid, rb, ra, "changed_on_b", rt_a)
        elif ra or rb:
            present, gone = ("a", "b") if ra else ("b", "a")
            r = ra or rb
            if r.kind != "normal":
                plan.findings.append(_finding("frozen_link", ids=[r.ident], detail=f"{present}:{r.kind}; counterpart missing"))
                continue
            survivors[present].append(r)
            fp_base = L["a_fp"] if present == "a" else L["b_fp"]
            if r.full != fp_base:
                plan.findings.append(_finding("conflict_deleted_vs_edited", ids=[r.ident], fp=[r.full[:16]],
                                              detail=f"deleted on {gone}, edited on {present}; nothing changed"))
            elif cfg.propagate_deletes:
                add_op("delete", present, lid, None, r, f"deleted_on_{gone}")
            else:
                plan.findings.append(_finding("pending_delete", ids=[r.ident],
                                              detail=f"deleted on {gone}; propagate_deletes is off"))
        else:
            plan.changes.append({"t": "drop", "link": lid})

    # ---- 2. unlinked items -------------------------------------------------------
    free = {s: [r for h, r in sorted(sides[s].items()) if h not in consumed[s]] for s in sides}

    # 2a. UID twins
    for r in list(free["a"]):
        twin = by_uid["b"].get(r.uid) if r.uid else None
        if twin is None or twin.href in consumed["b"]:
            continue
        consumed["a"].add(r.href)
        consumed["b"].add(twin.href)
        if r.kind != "normal" or twin.kind != "normal":
            plan.findings.append(_finding("exempt_pair", ids=[r.ident, twin.ident],
                                          detail=f"same UID on both sides, a:{r.kind} b:{twin.kind}; untouched"))
        elif r.core == twin.core:
            plan.changes.append({"t": "adopt", "via": "uid", "a": r.href, "b": twin.href})
        else:
            da, db = dec("prefer", side="a", id=r.ident), dec("prefer", side="b", id=twin.ident)
            if bound(da, r) and not db:
                plan.changes.append({"t": "adopt", "via": "uid", "a": r.href, "b": twin.href, "no_baseline": True})
                add_op("update", "b", "uid:" + r.uid, r, twin, "prefer_a_decision")
            elif bound(db, twin) and not da:
                plan.changes.append({"t": "adopt", "via": "uid", "a": r.href, "b": twin.href, "no_baseline": True})
                add_op("update", "a", "uid:" + r.uid, twin, r, "prefer_b_decision")
            else:
                plan.findings.append(_finding("uid_conflict", ids=[r.ident, twin.ident], fp=[r.full[:16], twin.full[:16]],
                                              detail="same UID, different content; neither overwritten"))
    free = {s: [r for r in free[s] if r.href not in consumed[s]] for s in sides}

    # 2b. operator link decisions
    for d in decisions or []:
        if d.get("action") != "link":
            continue
        ra = next((r for r in free["a"] if r.ident == d.get("a")), None)
        rb = next((r for r in free["b"] if r.ident == d.get("b")), None)
        if ra and rb and ra.kind == rb.kind == "normal" and bound(d, ra, rb):
            consumed["a"].add(ra.href)
            consumed["b"].add(rb.href)
            nb = ra.core != rb.core or im.alarm_fp(ra) != im.alarm_fp(rb)
            plan.changes.append({"t": "adopt", "via": "decision", "a": ra.href, "b": rb.href, "no_baseline": nb})
            if nb:
                plan.findings.append(_finding("duplicate_content_differs", ids=[ra.ident, rb.ident], fp=[ra.full[:16], rb.full[:16]],
                                              detail="linked by decision but content differs; use `prefer` to pick a side"))
    free = {s: [r for r in free[s] if r.href not in consumed[s]] for s in sides}

    # 2c. strict different-UID twins (same instants + title + recurrence shape): adopt, never copy
    fa = [r for r in free["a"] if r.kind == "normal" and r.strict]
    fb = [r for r in free["b"] if r.kind == "normal" and r.strict]
    sa: dict[str, list] = {}
    sb: dict[str, list] = {}
    for r in fa:
        sa.setdefault(r.strict, []).append(r)
    for r in fb:
        sb.setdefault(r.strict, []).append(r)
    for k in sorted(set(sa) & set(sb)):
        if len(sa[k]) == 1 and len(sb[k]) == 1:
            ra, rb = sa[k][0], sb[k][0]
            consumed["a"].add(ra.href)
            consumed["b"].add(rb.href)
            nb = ra.core != rb.core
            plan.changes.append({"t": "adopt", "via": "duplicate", "a": ra.href, "b": rb.href, "no_baseline": nb})
            if nb:    # strict key ignores description/location: never bake a difference into the baseline
                plan.findings.append(_finding("duplicate_content_differs", ids=[ra.ident, rb.ident], fp=[ra.full[:16], rb.full[:16]],
                                              detail="same time+title but description/location differ; linked without baseline, use `prefer`"))
        else:
            for r in sa[k] + sb[k]:
                consumed[("a" if r in sa[k] else "b")].add(r.href)
            plan.findings.append(_finding("ambiguous_duplicate", ids=[r.ident for r in sa[k] + sb[k]],
                                          detail="several items share instants+title; not copied, not linked"))
    free = {s: [r for r in free[s] if r.href not in consumed[s]] for s in sides}

    # 2d. loose suspects: same start and similar title against ANY item (also exempt ones) on the other side
    suspects = {"a": set(), "b": set()}
    for s in ("a", "b"):
        o = other[s]
        pool = free[o] + survivors[o]      # any kind: never copy a normal item next to an exempt/surviving look-alike
        by_start: dict[str, list] = {}
        by_day: dict[str, list] = {}
        for r in pool:
            by_start.setdefault(im.start_key(r), []).append(r)
            by_day.setdefault(im.day_key(r), []).append(r)
        for r in free[s]:
            if r.kind != "normal":
                continue
            cands = list(by_start.get(im.start_key(r), []))
            if not im.start_key(r) or im.start_key(r)[0] in "XF?":      # unresolved zone: fall back to the calendar day
                cands += by_day.get(im.day_key(r), [])
            for cand in cands + [c for c in by_day.get(im.day_key(r), []) if im.start_key(c)[:1] in ("X", "F", "?")]:
                if r.master and cand.master and im.titles_similar(r.master.summary, cand.master.summary):
                    suspects[s].add(r.href)
                    plan.findings.append(_finding("suspect_duplicate", side=s, ids=[r.ident, cand.ident], fp=[r.full[:16]],
                                                  detail=f"similar to {o}:{cand.kind} item at same start; not copied"))
                    break

    # 2e. one-sided items -> create on the other side
    for s in ("a", "b"):
        o = other[s]
        for r in free[s]:
            if r.kind != "normal":
                plan.findings.append(_finding(f"exempt_{r.kind}", side=s, ids=[r.ident], detail=",".join(r.reasons)))
                continue
            if r.href in suspects[s] and not bound(dec("copy", side=s, id=r.ident), r):
                continue
            if held(r, o):
                continue
            add_op("create", o, "", r, None, "only_on_" + s)

    # ---- 3. stats -----------------------------------------------------------------
    plan.ops.sort(key=lambda o: (OP_ORDER[o.kind], o.dst, o.src_id, o.dst_href))
    plan.findings.sort(key=lambda f: (f["kind"], f.get("side", ""), f.get("ids", [])))
    kinds = {s: _count(r.kind for r in rs.values()) for s, rs in sides.items()}
    plan.stats = {
        "resources": {s: len(rs) for s, rs in sides.items()},
        "kinds": kinds,
        "components": {s: sum(len(r.comps) for r in rs.values()) for s, rs in sides.items()},
        "recurring_series": {s: sum(1 for r in rs.values() if r.master and r.master.rrule) for s, rs in sides.items()},
        "overrides": {s: sum(len(r.overrides) for r in rs.values()) for s, rs in sides.items()},
        "links": len(links),
        "ops": _count(f"{o.kind}_{o.dst}" for o in plan.ops),
        "findings": _count(f["kind"] for f in plan.findings),
        "adopt": _count(c.get("via", "") for c in plan.changes if c["t"] == "adopt"),
        "state_changes": _count(c["t"] for c in plan.changes),
    }
    if today is not None:      # events (resources/series) vs occurrences in the next 12 months, reporting only
        from datetime import timedelta
        end = today + timedelta(days=365)
        occ: dict = {}
        for sd, rs in sides.items():
            tot, inexact = 0, 0
            for r in rs.values():
                if r.comps and r.kind in ("normal", "guest", "birthday"):
                    n, exact = im.count_occurrences(r, today, end)
                    tot += n
                    inexact += 0 if exact else 1
            occ[sd] = {"occurrences_next_12m": tot, "series_not_expanded": inexact}
        plan.stats["occurrences"] = occ
    return plan


def _count(it) -> dict:
    out: dict = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items()))


# ------------------------------------------------------------------------------ guards

def check_outage_and_deletions(plan: Plan, state: dict, cfg: EngineConfig) -> None:
    """Raise EngineBlocked if a side looks like an outage or too many items vanished."""
    last = state.get("last_counts", {})
    for s in ("a", "b"):
        n = len(plan.sides[s])
        prev = last.get(s, 0)
        if prev and n == 0 and not cfg.allow_extra_deletes:
            raise EngineBlocked(f"side {s} returned ZERO resources but {prev} were known: treating as outage")
        if prev and n < prev:
            vanished = prev - n
            limit = max(cfg.max_deletes, int(prev * cfg.max_delete_pct / 100))
            if vanished > limit + cfg.allow_extra_deletes:
                raise EngineBlocked(f"{vanished} resources vanished from side {s} since last good run (limit {limit}); "
                                    "acknowledge with `calsync ack-deletes` after checking the provider")
    pending = sum(1 for o in plan.ops if o.kind == "delete")
    if pending > cfg.max_deletes + cfg.allow_extra_deletes:
        raise EngineBlocked(f"{pending} deletions planned (limit {cfg.max_deletes})")


# ------------------------------------------------------------------------------ apply

@dataclass
class ApplyResult:
    done: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    executed: set = field(default_factory=set)       # signatures of ops that were carried out


def _bump(d: dict, k: str) -> None:
    d[k] = d.get(k, 0) + 1


def apply_state_changes(plan: Plan, state: dict, now: float) -> None:
    """Pure bookkeeping (no provider writes): adoptions, rebases, relinks, drops."""
    A, B = plan.sides["a"], plan.sides["b"]
    ha = {r.href: r for r in A.values()}
    hb = {r.href: r for r in B.values()}
    for c in plan.changes:
        t = c["t"]
        if t == "adopt":
            ra, rb = ha[c["a"]], hb[c["b"]]
            lid = link_id(ra.uid, rb.uid)
            state["links"][lid] = {
                "a_href": ra.href, "b_href": rb.href, "a_uid": ra.uid, "b_uid": rb.uid,
                "a_etag": ra.etag, "b_etag": rb.etag, "a_fp": ra.full, "b_fp": rb.full,
                "via": c["via"], "ts": now,
            }
            if c.get("no_baseline"):   # decision-driven overwrite follows: keep the loser's old fp as baseline
                state["links"][lid]["a_fp"] = state["links"][lid]["b_fp"] = ""
        elif t == "rebase":
            L = state["links"].get(c["link"])
            if L:
                L["a_etag"], L["a_fp"] = c["a"]
                L["b_etag"], L["b_fp"] = c["b"]
        elif t == "relink":
            L = state["links"].get(c["link"])
            if L:
                L["a_href"], L["b_href"] = c["a_href"], c["b_href"]
        elif t == "drop":
            state["links"].pop(c["link"], None)


def apply_plan(plan: Plan, stores: dict[str, CalendarStore], state: dict, cfg: EngineConfig, *,
               approved: dict[str, set[str]] | None, save: Callable[[dict], None], now: float,
               verify: bool = True) -> ApplyResult:
    """Execute ops. ``approved`` maps a destination side to the op signatures the operator approved
    for that direction; while a direction is not ``initialized`` only approved ops run (others are
    deferred), afterwards all ops run subject to caps. State is persisted after every successful write."""
    res = ApplyResult()
    apply_state_changes(plan, state, now)
    save(state)
    caps = {"create": cfg.max_creates, "update": cfg.max_updates, "delete": cfg.max_deletes + cfg.allow_extra_deletes}
    used = {"create": 0, "update": 0, "delete": 0}
    A, B = plan.sides["a"], plan.sides["b"]
    src_by_side = {"a": {r.href: r for r in A.values()}, "b": {r.href: r for r in B.values()}}
    for op in plan.ops:
        if not cfg.may_write(op.dst):
            _bump(res.skipped, "mode_forbids_side_" + op.dst)
            continue
        if not state.get("initialized", {}).get(op.dst) and op.sig not in (approved or {}).get(op.dst, ()):
            _bump(res.skipped, "unapproved_before_initialization")
            continue
        if state.get("rejected", {}).get(op.sig, {}).get("n", 0) >= MAX_REJECTIONS:
            _bump(res.skipped, "rejected_by_provider_twice")
            continue
        if used[op.kind] >= caps[op.kind]:
            _bump(res.skipped, "cap_" + op.kind)
            continue
        src_side = "b" if op.dst == "a" else "a"
        src = src_by_side[src_side].get(op.src_href) if op.src_href else None
        store = stores[op.dst]
        try:
            if op.kind == "delete":
                store.delete(op.dst_href, op.dst_etag)
                state["links"].pop(op.link, None)
            else:
                if src is None or src.kind != "normal":
                    raise EngineBlocked("internal error: refusing to write from a non-normal source")
                payload = im.prepare_outgoing(src.data, op.retarget_uid or None)
                if op.retarget_uid and im.parse_resource(payload).uid != op.retarget_uid:
                    raise StoreError("could not rewrite UID for adopted duplicate", transient=False, status=0)
                if op.kind == "create":
                    item = store.create(payload)
                else:
                    item = store.update(op.dst_href, op.dst_etag, payload)
                written = im.parse_resource(item.data, item.href, item.etag)
                written.kind, written.reasons = im.classify(written, cfg.self_addresses)
                written.core, written.full = im.core_fp(written), im.full_fp(written)
                _record_link(state, op, src, written, src_side, now)
                save(state)
                if verify and written.core != src.core:
                    raise VerifyFailed(f"{op.kind} {op.dst}:{src.ident}: stored resource differs from source "
                                       "(provider altered data); pair blocked until reviewed")
            _bump(res.done, f"{op.kind}_{op.dst}")
            res.executed.add(op.sig)
            used[op.kind] += 1
            save(state)
        except PreconditionFailed:
            _bump(res.skipped, "precondition_failed_replan")
        except VerifyFailed:
            raise
        except StoreError as e:
            if isinstance(e, (AuthError, RateLimited)) or e.transient or e.status in (None, 401, 403, 429):
                save(state)
                raise
            # the provider refused THIS item (e.g. 400/404/409/415): isolate it, keep going
            rej = state.setdefault("rejected", {}).setdefault(op.sig, {"n": 0})
            rej.update(n=rej["n"] + 1, status=e.status, ts=now, kind=op.kind, dst=op.dst, src=op.src_id)
            res.errors.append(f"{op.kind}_{op.dst} {op.src_id}: provider status {e.status}")
            _bump(res.skipped, "write_rejected")
            save(state)
    return res


def _record_link(state: dict, op: Op, src: im.Resource, written: im.Resource, src_side: str, now: float) -> None:
    a, b = (src, written) if src_side == "a" else (written, src)
    if op.kind == "create":
        lid = link_id(a.uid, b.uid)
        via = "created_" + op.dst
    else:
        lid = op.link if op.link in state["links"] else link_id(a.uid, b.uid)
        via = state["links"].get(lid, {}).get("via", "uid")
        upd = (state["links"].get(lid, {}).get("upd", []) + [now])[-8:]
        if op.link.startswith("uid:"):
            via = "uid"
    state["links"][lid] = {
        "a_href": a.href, "b_href": b.href, "a_uid": a.uid, "b_uid": b.uid,
        "a_etag": a.etag, "b_etag": b.etag, "a_fp": a.full, "b_fp": b.full, "via": via, "ts": now,
    }
    if op.kind == "update":
        state["links"][lid]["upd"] = upd
    if op.link.startswith("uid:"):
        state["links"].pop(op.link, None)
