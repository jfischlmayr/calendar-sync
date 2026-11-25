"""Read-only: histogram of why the remaining planned creates fail the canary rules (counts only, no titles).
Usage: docker compose exec -T calsync python - PAIR [canary|batch] < scripts/remaining_histogram.py"""
import collections, datetime, sys
from calsync import canary, config, syncrun
from calsync.util import now

if len(sys.argv) < 2:
    raise SystemExit("usage: PAIR argument required")
pair = sys.argv[1]
profile = sys.argv[2] if len(sys.argv) > 2 else "canary"
print("calsync from", canary.__file__, "profile", profile)
s = config.load(); s.ensure_dirs()
p = next(x for x in s.pairs if x.name == pair)
stores = syncrun.make_stores(s, p, read_only=True)
plan, st, cfg = syncrun.read_plan(s, p, stores)
today = datetime.datetime.fromtimestamp(now(), datetime.timezone.utc).date()
for d in ("a", "b"):
    src = "b" if d == "a" else "a"
    by = {r.href: r for r in plan.sides[src].values()}
    h = collections.Counter(); n = 0; combos = collections.Counter(); yrs = collections.Counter()
    for o in plan.ops:
        if o.kind != "create" or o.dst != d: continue
        n += 1
        r = by[o.src_href]
        why = canary.ineligible_reasons(r, today, profile=profile, self_addresses=cfg.self_addresses)
        for w in why: h[w] += 1
        combos[",".join(why) or "ELIGIBLE"] += 1
        c = r.comps[0] if r.comps else None
        if c: yrs[c.start[1:5] if c.start[:1] in "UD" else "?"] += 1
    print("direction", d, "creates", n)
    print(" reasons", dict(h.most_common()))
    print(" combos", dict(combos.most_common(12)))
    print(" start years", dict(sorted(yrs.items())))
