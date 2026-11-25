from __future__ import annotations

import argparse
import json
import sys

from . import config, health, provision, runner, syncrun
from .util import now, setup_logging, write_json


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="calsync")
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="scheduler loop (5 min default) + health endpoint")
    r = sub.add_parser("run-once"); r.add_argument("--pair")
    sub.add_parser("status")
    sub.add_parser("healthcheck", help="exit 0 if healthy (for Docker HEALTHCHECK)")
    sub.add_parser("list-calendars", help="read-only: list iCloud + Google calendars (names/ids)")
    a = sub.add_parser("ack-deletes", help="one-shot approval of N propagated deletions (expires in 1h)")
    a.add_argument("--pair", required=True); a.add_argument("--max", type=int, required=True)
    e = sub.add_parser("export", help="read-only export of one calendar to a fresh dir (pre-migration backup)")
    e.add_argument("side", choices=["icloud", "google"]); e.add_argument("collection"); e.add_argument("out")
    sub.add_parser("pause"); sub.add_parser("resume")
    u = sub.add_parser("audit", help="READ-ONLY dry run: classify, find duplicates/exemptions, plan; writes redacted report")
    u.add_argument("--pair"); u.add_argument("--detail", action="store_true", help="also write a 0600 detail report with titles")
    pl = sub.add_parser("plan", help="READ-ONLY: compute the plan and store it for approval (data/plans/<pair>.json)")
    pl.add_argument("--pair", required=True)
    pl.add_argument("--direction", required=True, choices=["a", "b", "both"], help="destination side: a = iCloud, b = Google")
    pl.add_argument("--canary", type=int, default=None, help="store a plan with ONLY the N safest creates (single future timed event, no attendees, ...); one direction")
    pl.add_argument("--only", action="append", help="store a plan with ONLY the create of this source id (repeatable); one direction")
    pl.add_argument("--since-days-ago", type=int, default=0, help="opt-in with --canary/--only: also admit events that started 2..N days ago (N<=90); all other canary rules unchanged")
    pl.add_argument("--profile", choices=["canary", "batch"], default="canary", help="eligibility whitelist for --canary/--only: canary (default, strictest, <=10) or batch (history, all-day, finite recurrence, ...; <=25)")
    ap_ = sub.add_parser("approve-plan", help="approve the stored plan (needed once per direction before first writes)")
    ap_.add_argument("--pair", required=True); ap_.add_argument("--direction", required=True, choices=["a", "b"]); ap_.add_argument("--by", required=True); ap_.add_argument("--note", required=True)
    ap_.add_argument("--expect-sha", required=True, help="plan_sha256 printed by `calsync plan` that you reviewed")
    d = sub.add_parser("decide", help="record an operator decision (bound to the item's current fingerprint)")
    d.add_argument("--pair", required=True); d.add_argument("--action", required=True, choices=["ignore", "copy", "prefer", "link"])
    d.add_argument("--side", choices=["a", "b"]); d.add_argument("--id", required=True); d.add_argument("--b-id", help="link: id of the side-b item")
    sub.add_parser("auth-status", help="Google token status (no secrets)")
    c = sub.add_parser("create-calendar", help="additive; dry-run unless --execute")
    c.add_argument("side", choices=["icloud", "google"]); c.add_argument("name"); c.add_argument("--execute", action="store_true")
    c.add_argument("--timezone", help="Google only: IANA zone (default: $CALSYNC_TIMEZONE, then $TZ, then UTC)")
    k = sub.add_parser("copy-items", help="PUT selected UIDs from a mirror dir into a CalDAV collection; dry-run unless --execute")
    k.add_argument("--src", required=True); k.add_argument("--target-url", required=True)
    k.add_argument("--uid", action="append", required=True); k.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)

    s = config.load(args.config)
    s.ensure_dirs()
    setup_logging(s.logs)
    if args.cmd == "serve":
        runner.serve(s); return 0
    if args.cmd == "run-once":
        print(json.dumps(runner.run_cycle(s, args.pair), indent=1, default=str)); return 0
    if args.cmd == "status":
        ok, body = health.evaluate(s); print(json.dumps(body, indent=1, default=str)); return 0 if ok else 1
    if args.cmd == "healthcheck":
        ok, _ = health.evaluate(s); return 0 if ok else 1
    if args.cmd == "ack-deletes":
        write_json(s.state / f"ack_deletes_{args.pair}.json", {"max": args.max, "expires": now() + 3600}); return 0
    if args.cmd in ("pause", "resume"):
        f = s.state / "PAUSE"
        f.touch() if args.cmd == "pause" else f.unlink(missing_ok=True); return 0
    if args.cmd in ("audit", "plan", "approve-plan", "decide", "auth-status"):
        return _engine_cmd(s, args)
    if args.cmd == "list-calendars":
        d = provision.DAV(s.icloud_url, config.env_secret("ICLOUD_USER"), config.env_secret("ICLOUD_APP_PASSWORD"))
        print("iCloud:"); [print(f"  {n!r} -> {h}") for n, h in sorted(d.list_calendars(d.calendar_home()).items())]
        print("Google:")
        for it in provision._g(s, "GET", "/users/me/calendarList").get("items", []):
            print(f"  {it.get('summary')!r} id={it['id']} role={it.get('accessRole')}")
        return 0
    if args.cmd == "export":
        from pathlib import Path
        print(json.dumps({"exported_files": runner.export_collection(s, args.side, args.collection, Path(args.out))})); return 0
    if args.cmd == "create-calendar":
        res = (provision.create_icloud_calendar(s, args.name, execute=args.execute) if args.side == "icloud"
               else provision.create_google_calendar(s, args.name, args.timezone, execute=args.execute))
        print(json.dumps(res)); return 0
    if args.cmd == "copy-items":
        print(json.dumps(provision.copy_items(s, args.src, args.uid, args.target_url, execute=args.execute))); return 0
    return 2


def _pair(s, name):
    for p in s.pairs:
        if p.name == name:
            return p
    raise SystemExit(f"unknown pair {name!r}")


def _engine_cmd(s, args) -> int:
    from . import gauth
    from .util import Blocked
    if args.cmd == "auth-status":
        from .config import env_secret
        st = gauth.GoogleTokenSource(s.tokens / "google.json", env_secret("GOOGLE_CLIENT_ID") or "", env_secret("GOOGLE_CLIENT_SECRET") or "").status()
        print(json.dumps(st, indent=1, default=str)); return 0
    if args.cmd == "audit":
        out = {}
        for p in ([_pair(s, args.pair)] if args.pair else [x for x in s.pairs if x.mode != "disabled"]):
            rep = syncrun.audit_pair(s, p, detail=args.detail)
            out[p.name] = {k: rep[k] for k in ("mode", "plan_sha256", "stats", "initialized", "approved_directions", "gates", "report", "detail_report") if k in rep}
            out[p.name]["birthdays"] = {k: (len(v) if isinstance(v, list) else v) for k, v in rep["birthdays"].items() if k != "note"}
        print(json.dumps(out, indent=1, default=str)); return 0
    p = _pair(s, args.pair)
    try:
        if args.cmd == "plan":
            dirs = ("a", "b") if args.direction == "both" else (args.direction,)
            rep = syncrun.audit_pair(s, p, write_plan=dirs, only=args.only, canary=args.canary, since_days_ago=args.since_days_ago, profile=args.profile)
            out = {"plan_sha256": rep["plan_file"], "ops": rep["stats"]["ops"], "findings": rep["stats"]["findings"]}
            for k in ("selection", "canary_detail"):
                if k in rep:
                    out[k] = rep[k]
            print(json.dumps(out, indent=1)); return 0
        if args.cmd == "approve-plan":
            doc = syncrun.approve_plan(s, p, args.by, args.note, args.expect_sha, args.direction)
            print(json.dumps({"approved": doc["plan_sha256"], "ops": doc["ops"]})); return 0
        if args.cmd == "decide":
            plan, _, _ = syncrun.read_plan(s, p, syncrun.make_stores(s, p, read_only=True))
            res = {r.ident: r for sd in plan.sides.values() for r in sd.values()}
            if args.id not in res:
                raise SystemExit("unknown item id (run `calsync audit` for current ids)")
            dec = {"action": args.action, "id": args.id, "side": args.side or "", "fp": [res[args.id].full[:16]], "at": syncrun.now()}
            if args.action == "link":
                if args.b_id not in res:
                    raise SystemExit("--b-id required/unknown")
                dec.update(a=args.id, b=args.b_id, fp=[res[args.id].full[:16], res[args.b_id].full[:16]])
            cur = syncrun.load_decisions(s, p)
            cur.append(dec)
            syncrun.write_json(s.decisions / f"{p.name}.json", cur)
            print(json.dumps({"recorded": dec["action"], "id": args.id})); return 0
    except Blocked as e:
        print(f"refused: {e}", file=sys.stderr); return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
