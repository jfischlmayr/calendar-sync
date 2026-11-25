"""Read-only: batch-eligible creates for a direction, with feature flags and opaque ids (NO titles).
Usage: docker compose exec -T calsync python - PAIR DIRECTION [FLAG] < scripts/list_candidates.py   (FLAG filters, e.g. allday|rrule|url|sloc|email|tentative|long)"""
import collections, datetime, sys
from calsync import canary, config, syncrun

pair, d = sys.argv[1], sys.argv[2]
want = sys.argv[3] if len(sys.argv) > 3 else None
s = config.load(); p = next(x for x in s.pairs if x.name == pair)
plan, st, cfg = syncrun.read_plan(s, p, syncrun.make_stores(s, p, read_only=True))
today = datetime.date.today()
by = {r.href: r for r in plan.sides["b" if d == "a" else "a"].values()}
cnt = collections.Counter(); rows = []
for o in plan.ops:
    if o.kind != "create" or o.dst != d: continue
    r = by[o.src_href]
    if canary.ineligible_reasons(r, today, profile="batch", self_addresses=cfg.self_addresses): continue
    c = r.comps[0]; names = {n for n, _, _ in c.props}
    fl = [f for f, ok in (("allday", c.allday), ("rrule", bool(c.rrule)), ("url", "URL" in names),
          ("sloc", "X-APPLE-STRUCTURED-LOCATION" in names), ("travel", any(n.startswith("X-APPLE-TRAVEL") for n in names)),
          ("email", any(a[0] == "EMAIL" for a in c.alarms)), ("tentative", c.status == "TENTATIVE"),
          ("long", c.end > c.start and False), ("desc", bool(c.description)), ("loc", bool(c.location)), ("noalarm", not c.alarms)) if ok]
    for f in fl: cnt[f] += 1
    rows.append((c.start, r.ident, fl))
rows.sort(reverse=True)
print("eligible", len(rows), dict(cnt))
for st_, i, fl in rows:
    if want is None or want in fl:
        if want is not None: print(i, st_, ",".join(fl))
