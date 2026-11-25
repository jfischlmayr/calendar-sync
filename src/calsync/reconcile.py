"""Read-only reconcile manifest for the first live sync of two populated calendars.

Side A / side B are directories of ``*.ics`` files (vdirsyncer filesystem storage).
Nothing here ever writes into those directories: they are only read. The output is
a JSON manifest that a human reviews, annotates with per-entry ``decision`` values
and finally approves. The approval is bound to the SHA-256 of the manifest content,
so any later edit invalidates it.

Classification (identity of an event = UID + RECURRENCE-ID):
  identical                 same identity on both sides, same normalised key fields
  uid_match_diff            same identity, key fields differ (``differing_fields``)
  dup_candidate             different UID, same start instant, same casefolded summary
  time_match_title_mismatch different UID, same start instant, different summary
  only_a / only_b           present on one side only

Proposed actions never auto-resolve anything that involves guests, a field
difference, a duplicate candidate or a title mismatch (those are ``manual_review``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import ics

MANIFEST_VERSION = 1
DECISIONS = ("copy_a_to_b", "copy_b_to_a", "skip", "keep_both")
_EXCLUDED_FROM_HASH = ("generated_at", "manifest_sha256", "approval")
_PENDING_PARTSTATS = ("NEEDS-ACTION", "TENTATIVE")
# Flags that force manual review even for an otherwise copyable only_a/only_b entry.
_BLOCKING_FLAGS = ("multiple_events_a", "multiple_events_b", "missing_uid", "series_partial")


class ApprovalError(Exception):
    """Raised when a manifest may not be approved."""


# --------------------------------------------------------------------------- helpers

def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_hash(manifest: dict) -> str:
    """SHA-256 over canonical JSON, excluding generated_at, manifest_sha256 and approval."""
    body = {k: v for k, v in manifest.items() if k not in _EXCLUDED_FROM_HASH}
    return _sha(_canonical(body))


def _norm_summary(s: str) -> str:
    return " ".join(s.split())


def _norm_rrule(r: str) -> str:
    return ";".join(sorted(p.strip().upper() for p in r.split(";") if p.strip()))


def _rrule_parts(r: str) -> dict[str, str]:
    out = {}
    for p in r.split(";"):
        k, _, v = p.partition("=")
        if k.strip():
            out[k.strip().upper()] = v.strip().upper()
    return out


def _start_norm(dtstart: str) -> str:
    """Comparable start instant: UTC for timed events, plain date for all-day."""
    s = dtstart
    if s.startswith("DATE:"):
        return "D" + s[5:]
    tz = ""
    if s.startswith("TZID="):
        tz, _, s = s[5:].partition(":")
    if len(s) == 8 and s.isdigit():
        return "D" + s
    if s.endswith("Z"):
        return "U" + s[:-1]
    if tz:
        try:
            from zoneinfo import ZoneInfo

            dt = datetime.strptime(s, "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo(tz))
            return dt.astimezone(timezone.utc).strftime("U%Y%m%dT%H%M%S")
        except Exception:  # unknown zone / odd format: keep distinguishable, never guess
            return f"F{s}@{tz}"
    return "F" + s


def _addr(a: str) -> str:
    return a.strip().lower().removeprefix("mailto:")


def _is_birthday_like(ev: ics.Event) -> bool:
    return (
        ev.all_day
        and not ev.recurrence_id
        and _rrule_parts(ev.rrule).get("FREQ") == "YEARLY"
    )


def birthday_series(events: Iterable[ics.Event]) -> list[ics.Event]:
    """Yearly all-day recurring series (RRULE FREQ=YEARLY), e.g. birthdays."""
    return [e for e in events if _is_birthday_like(e)]


# --------------------------------------------------------------------------- reading

def _read_side(path: str | Path) -> tuple[list[ics.Event], dict]:
    p = Path(path)
    if not p.is_dir():
        raise NotADirectoryError(f"not a directory: {p}")
    events: list[ics.Event] = []
    tree: list[tuple[str, str]] = []
    no_events: list[str] = []
    for f in sorted(p.glob("*.ics")):
        raw = f.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        tree.append((f.name, digest))
        evs = ics.parse(raw.decode("utf-8", "replace"), source=f.name, sha256=digest)
        if not evs:
            no_events.append(f.name)
        events += evs
    info = {
        "file_count": len(tree),
        "event_count": len(events),
        "files_without_events": no_events,
        "tree_sha256": _sha(_canonical(tree)),
    }
    return events, info


def _overrides_by_uid(events: Sequence[ics.Event]) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for e in events:
        if e.uid and e.recurrence_id:
            out.setdefault(e.uid, set()).add(e.recurrence_id)
    return {k: sorted(v) for k, v in out.items()}


def _key_fields(ev: ics.Event, overrides: dict[str, list[str]]) -> dict:
    return {
        "uid": ev.uid,
        "summary": _norm_summary(ev.summary),
        "dtstart": ev.dtstart,
        "dtend": ev.dtend,
        "rrule": _norm_rrule(ev.rrule),
        "exdates": sorted(ev.exdates),
        "recurrence_id": ev.recurrence_id,
        "status": ev.status.upper(),
        # series masters also compare their set of RECURRENCE-ID overrides
        "overrides": overrides.get(ev.uid, []) if (ev.rrule and not ev.recurrence_id) else [],
    }


def _group(events: Sequence[ics.Event], overrides: dict[str, list[str]]):
    """Group events by (uid, recurrence_id); each member carries its key fields + sha."""
    groups: dict[tuple[str, str], list[tuple[str, dict, ics.Event]]] = {}
    for i, e in enumerate(events):
        uid = e.uid or f"NOUID:{e.source}#{i}"
        kf = _key_fields(e, overrides)
        groups.setdefault((uid, e.recurrence_id), []).append((_sha(_canonical(kf)), kf, e))
    for members in groups.values():
        members.sort(key=lambda m: (m[0], m[2].source))
    return groups


# --------------------------------------------------------------------------- entries

def _guest_status(members, selfset: set[str]) -> dict:
    ev = members[0][2]
    counts: dict[str, int] = {}
    self_ps = ""
    for addr, ps in ev.attendees:
        counts[ps or "UNSPECIFIED"] = counts.get(ps or "UNSPECIFIED", 0) + 1
        if _addr(addr) in selfset and not self_ps:
            self_ps = ps or "UNSPECIFIED"
    return {
        "organizer": ev.organizer,
        "attendees": len(ev.attendees),
        "by_partstat": dict(sorted(counts.items())),
        "self_partstat": self_ps,
    }


def _side(members, selfset: set[str]) -> dict:
    sha, kf, ev = members[0]
    return {
        "uid": ev.uid,
        "recurrence_id": ev.recurrence_id,
        "sources": sorted({m[2].source for m in members}),
        "resource_sha256": sorted({m[2].sha256 for m in members}),
        "key_sha256": sha,
        "summary": ev.summary,
        "dtstart": ev.dtstart,
        "dtend": ev.dtend,
        "rrule": ev.rrule,
        "exdates": sorted(ev.exdates),
        "status": ev.status,
        "organizer": ev.organizer,
        "attendees": [[_addr(a), p] for a, p in ev.attendees],
        "guest_status": _guest_status(members, selfset),
    }


def _invited_not_organizer(members, selfset: set[str]) -> bool:
    for _, _, ev in members:
        if not ev.organizer:
            continue
        if _addr(ev.organizer) not in selfset:
            return True
        for addr, ps in ev.attendees:
            if _addr(addr) in selfset and ps.upper() in _PENDING_PARTSTATS:
                return True
    return False


def _propose(entry: dict) -> str:
    c = entry["classification"]
    if c == "identical":
        return "skip_identical"
    if c in ("only_a", "only_b"):
        if entry["has_guests"] or any(f in _BLOCKING_FLAGS for f in entry["flags"]):
            return "manual_review"
        return "copy_a_to_b" if c == "only_a" else "copy_b_to_a"
    return "manual_review"


def _make_entry(eid, classification, a_members, b_members, selfset, differing=(), flags=()):
    sides = {"a": a_members, "b": b_members}
    entry: dict[str, Any] = {
        "id": eid,
        "classification": classification,
        "differing_fields": sorted(differing),
        "flags": sorted(set(flags)),
    }
    for k, m in sides.items():
        if m and len(m) > 1:
            entry["flags"] = sorted(set(entry["flags"]) | {f"multiple_events_{k}"})
        if m and m[0][2].uid == "":
            entry["flags"] = sorted(set(entry["flags"]) | {"missing_uid"})
    present = [m for m in sides.values() if m]
    entry["recurring"] = any(e.is_recurring or bool(e.recurrence_id) for m in present for _, _, e in m)
    entry["birthday_like"] = any(_is_birthday_like(e) for m in present for _, _, e in m)
    if entry["birthday_like"]:
        entry["note"] = "birthday-like yearly all-day series"
    entry["has_guests"] = any(e.has_guests for m in present for _, _, e in m)
    entry["invited_not_organizer"] = any(_invited_not_organizer(m, selfset) for m in present)
    entry["guest_status"] = {k: (_guest_status(m, selfset) if m else None) for k, m in sides.items()}
    entry["a"] = _side(a_members, selfset) if a_members else None
    entry["b"] = _side(b_members, selfset) if b_members else None
    entry["proposed_action"] = _propose(entry)
    if entry["proposed_action"] == "manual_review":
        entry["decision"] = None
    return entry


def _eid(key: tuple[str, str]) -> str:
    return f"{key[0]}|{key[1]}"


def build_manifest(
    dir_a: str | Path,
    dir_b: str | Path,
    *,
    label_a: str = "icloud",
    label_b: str = "google",
    self_addresses: Iterable[str] = (),
    now: str | None = None,
) -> dict:
    """Build the read-only reconcile manifest. Only reads dir_a / dir_b."""
    selfset = {_addr(s) for s in self_addresses if s and s.strip()}
    ev_a, info_a = _read_side(dir_a)
    ev_b, info_b = _read_side(dir_b)
    ov_a, ov_b = _overrides_by_uid(ev_a), _overrides_by_uid(ev_b)
    ga, gb = _group(ev_a, ov_a), _group(ev_b, ov_b)
    uids_a = {k[0] for k in ga}
    uids_b = {k[0] for k in gb}

    entries: list[dict] = []
    left_a: list[tuple[str, str]] = []
    left_b: list[tuple[str, str]] = []

    for key in sorted(set(ga) | set(gb)):
        ma, mb = ga.get(key), gb.get(key)
        if ma and mb:
            ka, kb = ma[0][1], mb[0][1]
            differing = [f for f in ka if ka[f] != kb[f]]
            flags = []
            if len(ma) > 1:
                differing.append("multiple_events_a")
            if len(mb) > 1:
                differing.append("multiple_events_b")
            cls = "uid_match_diff" if differing else "identical"
            entries.append(_make_entry(_eid(key), cls, ma, mb, selfset, differing, flags))
        elif ma:
            left_a.append(key)
        else:
            left_b.append(key)

    # Different-UID candidates among the leftovers, matched on the start instant.
    by_start: dict[str, list[tuple[str, str]]] = {}
    for kb in left_b:
        by_start.setdefault(_start_norm(gb[kb][0][2].dtstart), []).append(kb)
    paired_a: set = set()
    paired_b: set = set()
    for ka in left_a:
        sa = ga[ka][0][2]
        for kb in by_start.get(_start_norm(sa.dtstart), []):
            sb = gb[kb][0][2]
            same_title = sa.key_time_title()[1] == sb.key_time_title()[1]
            cls = "dup_candidate" if same_title else "time_match_title_mismatch"
            differing = [] if same_title else ["summary"]
            entries.append(
                _make_entry(f"pair:{_eid(ka)}~{_eid(kb)}", cls, ga[ka], gb[kb], selfset, differing)
            )
            paired_a.add(ka)
            paired_b.add(kb)

    for ka in left_a:
        if ka in paired_a:
            continue
        flags = ["series_partial"] if ka[1] and ka[0] in uids_b else []
        entries.append(_make_entry(_eid(ka), "only_a", ga[ka], None, selfset, flags=flags))
    for kb in left_b:
        if kb in paired_b:
            continue
        flags = ["series_partial"] if kb[1] and kb[0] in uids_a else []
        entries.append(_make_entry(_eid(kb), "only_b", None, gb[kb], selfset, flags=flags))

    entries.sort(key=lambda e: e["id"])

    classes = ("identical", "uid_match_diff", "dup_candidate", "time_match_title_mismatch", "only_a", "only_b")
    actions = ("copy_a_to_b", "copy_b_to_a", "skip_identical", "manual_review")
    summary = {
        "total_entries": len(entries),
        "by_classification": {c: sum(e["classification"] == c for e in entries) for c in classes},
        "by_proposed_action": {a: sum(e["proposed_action"] == a for e in entries) for a in actions},
        "recurring": sum(e["recurring"] for e in entries),
        "birthday_like": sum(e["birthday_like"] for e in entries),
        "birthday_series_a": len(birthday_series(ev_a)),
        "birthday_series_b": len(birthday_series(ev_b)),
        "has_guests": sum(e["has_guests"] for e in entries),
        "invited_not_organizer": sum(e["invited_not_organizer"] for e in entries),
        "blocking_manual_review": sum(e["proposed_action"] == "manual_review" for e in entries),
    }
    manifest = {
        "version": MANIFEST_VERSION,
        "labels": {"a": label_a, "b": label_b},
        "self_addresses": sorted(selfset),
        "inputs": {"a": info_a, "b": info_b},
        "summary": summary,
        "blocking_manual_review": summary["blocking_manual_review"],
        "entries": entries,
    }
    manifest["manifest_sha256"] = compute_hash(manifest)
    manifest["generated_at"] = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return manifest


# --------------------------------------------------------------------------- I/O

def write_manifest(path: str | Path, manifest: dict) -> None:
    """Atomically write the manifest as pretty JSON (sorted keys)."""
    p = Path(path)
    data = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_manifest(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- approval

def unresolved_manual_review(manifest: dict) -> list[str]:
    """IDs of manual_review entries without a valid human ``decision``."""
    out = []
    for e in manifest.get("entries", []):
        if e.get("proposed_action") == "manual_review" and e.get("decision") not in DECISIONS:
            out.append(e["id"])
    return out


def _check_approvable(manifest: dict) -> None:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ApprovalError("manifest has no entries list")
    manual = 0
    for e in entries:
        if _propose(e) != e.get("proposed_action"):
            raise ApprovalError(f"proposed_action of {e.get('id')!r} was edited; rebuild the manifest and use 'decision'")
        if e["proposed_action"] == "manual_review":
            manual += 1
            d = e.get("decision")
            if d == "copy_a_to_b" and not e.get("a"):
                raise ApprovalError(f"decision copy_a_to_b impossible for {e['id']!r}: no side-a event")
            if d == "copy_b_to_a" and not e.get("b"):
                raise ApprovalError(f"decision copy_b_to_a impossible for {e['id']!r}: no side-b event")
    if manual != manifest.get("blocking_manual_review"):
        raise ApprovalError("blocking_manual_review does not match entries; rebuild the manifest")
    pending = unresolved_manual_review(manifest)
    if pending:
        raise ApprovalError(
            f"{len(pending)} manual_review entries lack a decision in {DECISIONS}: first {pending[0]!r}"
        )


def approve_manifest(path: str | Path, approver: str, note: str) -> dict:
    """Add an approval block bound to the current content hash. Raises ApprovalError."""
    if not approver or not approver.strip():
        raise ApprovalError("approver is required")
    m = load_manifest(path)
    _check_approvable(m)
    m.pop("approval", None)
    m["manifest_sha256"] = compute_hash(m)
    m["approval"] = {
        "approver": approver.strip(),
        "note": note,
        "approved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest_sha256": m["manifest_sha256"],
    }
    write_manifest(path, m)
    return m


def verify_approval(path: str | Path) -> bool:
    """True only if an approval exists and matches the current content hash."""
    try:
        m = load_manifest(path)
    except (OSError, ValueError):
        return False
    return verify_approval_obj(m)


def verify_approval_obj(m: dict) -> bool:
    ap = m.get("approval")
    if not isinstance(ap, dict) or not ap.get("manifest_sha256"):
        return False
    cur = compute_hash(m)
    return ap["manifest_sha256"] == cur and m.get("manifest_sha256") == cur


# --------------------------------------------------------------------------- CLI

def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _summary_text(m: dict) -> str:
    s = m["summary"]
    lines = [
        f"manifest_sha256: {m.get('manifest_sha256')}",
        f"generated_at:    {m.get('generated_at')}",
        f"sides:           a={m['labels']['a']} ({m['inputs']['a']['event_count']} events), "
        f"b={m['labels']['b']} ({m['inputs']['b']['event_count']} events)",
        f"entries:         {s['total_entries']}",
    ]
    lines += [f"  {k}: {v}" for k, v in s["by_classification"].items()]
    lines += ["actions:"] + [f"  {k}: {v}" for k, v in s["by_proposed_action"].items()]
    lines += [
        f"recurring: {s['recurring']}  birthday_like: {s['birthday_like']} "
        f"(series a={s['birthday_series_a']}, b={s['birthday_series_b']})",
        f"has_guests: {s['has_guests']}  invited_not_organizer: {s['invited_not_organizer']}",
        f"blocking_manual_review: {m['blocking_manual_review']}  "
        f"unresolved: {len(unresolved_manual_review(m))}",
        f"approval: {'VALID' if verify_approval_obj(m) else ('STALE/INVALID' if m.get('approval') else 'none')}",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="calsync.reconcile", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build a read-only reconcile manifest")
    b.add_argument("dir_a")
    b.add_argument("dir_b")
    b.add_argument("-o", "--output", required=True)
    b.add_argument("--label-a", default="icloud")
    b.add_argument("--label-b", default="google")
    b.add_argument("--self-address", action="append", default=[], dest="self_addresses")
    b.add_argument("--force", action="store_true", help="overwrite an existing manifest")
    a = sub.add_parser("approve", help="approve a manifest (all manual reviews need a decision)")
    a.add_argument("manifest")
    a.add_argument("--by", required=True)
    a.add_argument("--note", required=True)
    v = sub.add_parser("verify", help="exit 0 if approval is valid for current content, else 1")
    v.add_argument("manifest")
    s = sub.add_parser("summary", help="print manifest summary")
    s.add_argument("manifest")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "build":
            out = Path(args.output)
            for d in (args.dir_a, args.dir_b):
                if _inside(out, Path(d)):
                    print(f"refusing to write output inside input dir {d}", file=sys.stderr)
                    return 2
            if out.exists() and not args.force:
                print(f"{out} exists; use --force to overwrite (decisions/approval would be lost)", file=sys.stderr)
                return 2
            m = build_manifest(
                args.dir_a, args.dir_b, label_a=args.label_a, label_b=args.label_b,
                self_addresses=args.self_addresses,
            )
            write_manifest(out, m)
            print(_summary_text(m))
            return 0
        if args.cmd == "approve":
            m = approve_manifest(args.manifest, args.by, args.note)
            print(f"approved {m['manifest_sha256']}")
            return 0
        if args.cmd == "verify":
            ok = verify_approval(args.manifest)
            print("approval valid" if ok else "approval missing or invalid")
            return 0 if ok else 1
        if args.cmd == "summary":
            print(_summary_text(load_manifest(args.manifest)))
            return 0
    except ApprovalError as exc:
        print(f"approval refused: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
