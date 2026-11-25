"""Read-only: group the deferred (not batch-eligible) planned creates of a pair by structural properties.
No titles, no addresses, no URLs are printed - only counts and property classes plus opaque ids.
Usage: docker compose exec -T calsync python - PAIR < scripts/deferred_review.py"""
import collections, datetime, re, sys
from calsync import canary, config, syncrun
from calsync.util import now

if len(sys.argv) < 2:
    raise SystemExit("usage: PAIR argument required")
pair = sys.argv[1]
s = config.load(); s.ensure_dirs()
p = next(x for x in s.pairs if x.name == pair)
stores = syncrun.make_stores(s, p, read_only=True)
plan, st, cfg = syncrun.read_plan(s, p, stores)
today = datetime.datetime.fromtimestamp(now(), datetime.timezone.utc).date()
rows = []
for o in plan.ops:
    if o.kind != "create":
        continue
    src = "b" if o.dst == "a" else "a"
    r = {x.href: x for x in plan.sides[src].values()}[o.src_href]
    why = canary.ineligible_reasons(r, today, profile="batch", self_addresses=cfg.self_addresses)
    if not why:
        continue
    c = r.comps[0]
    words = sorted({w for w in canary.SPECIAL_TITLE_WORDS if w in c.summary.casefold()})
    url = next((v for n, _p, v in c.props if n == "URL"), "")
    scheme = re.match(r"^([a-z][a-z0-9+.-]*):", url.strip(), re.I)
    rows.append(dict(dst=o.dst, id=r.ident[:8], why=",".join(why), words=",".join(words) or "-",
                     allday=c.allday, rrule=bool(c.rrule), cls=c.cls or "-", status=c.status or "-",
                     url=("-" if not url.strip() else (scheme.group(1).lower() if scheme else "relative")),
                     year=c.start[1:5], span_zero=(c.start == c.end),
                     alarms=len(c.alarms), attendees=len(c.attendees), org=bool(c.organizer),
                     desc_len=len(c.description), has_loc=bool(c.location)))
by = collections.Counter((r["dst"], r["why"], r["words"], r["allday"], r["cls"], r["url"], r["rrule"]) for r in rows)
for k, n in sorted(by.items()):
    print(n, dict(zip(("dst", "why", "words", "allday", "class", "url", "rrule"), k)))
print("total", len(rows))
for r in rows:
    print(r["dst"], r["id"], r["year"], "zero" if r["span_zero"] else "", "att", r["attendees"], "org", r["org"], "alarms", r["alarms"], "desc", r["desc_len"])
