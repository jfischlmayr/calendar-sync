"""Engine over the committed SYNTHETIC corpus (tests/fixtures/synthetic) and, when present, the
private real exports under .private-fixtures/ (never committed; only aggregates are asserted
and nothing from the data is printed)."""
import json
import unittest
from pathlib import Path

from support import MemoryStore
from test_engine import Harness, SELF

from calsync import canary, engine as eng, icsmodel as im, syncrun
from calsync.store import Item

ROOT = Path(__file__).resolve().parent
SYN = ROOT / "fixtures" / "synthetic"
PRIV = ROOT.parent / ".private-fixtures" / "mirror" / "example_pair"
EXPECTED = json.loads((SYN / "EXPECTED.json").read_text())
SELFS = frozenset(a.lower() for a in EXPECTED["self_addresses"].values())


def load(side):
    return {f"/{side}/{p.name}": p.read_bytes() for p in sorted((SYN / side).glob("*.ics"))}


class SyntheticClassifier(unittest.TestCase):
    def test_every_file_matches_expected_kind(self):
        bad = []
        for rel, meta in EXPECTED["files"].items():
            r = im.parse_resource((SYN / rel).read_bytes(), rel)
            kind, why = im.classify(r, SELFS)
            want = meta["kind"].split(":")[0]
            if kind != want:
                bad.append((rel, kind, want, why))
        self.assertEqual(bad, [])

    def test_unsupported_reason_matches_when_specified(self):
        for rel, meta in EXPECTED["files"].items():
            if ":" in meta["kind"]:
                r = im.parse_resource((SYN / rel).read_bytes(), rel)
                kind, why = im.classify(r, SELFS)
                self.assertTrue(any(meta["kind"].split(":", 1)[1].split("_")[0] in w for w in why) or kind == "unsupported",
                                (rel, meta["kind"], why))


class SyntheticSync(unittest.TestCase):
    def harness(self, mode="live"):
        h = Harness(self, mode=mode, a=load("icloud"), b=load("google"))
        h.s.self_addresses = tuple(sorted(SELFS))
        return h

    def test_same_uid_groups_are_adopted_and_strict_dups_never_duplicated(self):
        h = self.harness()
        pl = h.plan()
        files = EXPECTED["files"]
        created_src = {o.src_href for o in pl.ops if o.kind == "create"}
        for gname, g in EXPECTED["groups"].items():
            if g["relation"] in ("same_uid", "strict_duplicate"):
                kinds = {files[f]["kind"] for f in g["files"]}
                if kinds != {"normal"}:
                    continue
                for f in g["files"]:
                    side, name = f.split("/", 1)
                    self.assertNotIn(f"/{side}/{name}", created_src, f"{gname}: {f} would be copied although its twin exists")

    def test_no_op_ever_touches_an_exempt_resource(self):
        h = self.harness()
        pl = h.plan()
        exempt_ids = {r.ident for sd in pl.sides.values() for r in sd.values() if r.kind != "normal"}
        self.assertTrue(exempt_ids)
        self.assertFalse({o.src_id for o in pl.ops} & exempt_ids)
        for o in pl.ops:
            if o.dst_href:
                tgt = pl.sides[o.dst][o.dst_href]
                self.assertEqual(tgt.kind, "normal")

    def test_full_workflow_idempotent_and_exempt_bytes_untouched(self):
        h = self.harness()
        before_a, before_b = dict(h.a.items), dict(h.b.items)
        exempt = {}
        for side, store in (("a", h.a), ("b", h.b)):
            for href, (_, d) in store.items.items():
                r = im.parse_resource(d)
                if im.classify(r, SELFS)[0] != "normal":
                    exempt[(side, href)] = d
        h.approve()
        writes = None
        for i in range(8):                        # caps are 25/run: run until the plan is exhausted
            h.cycle()
            if not h.plan().ops:
                break
        self.assertEqual(h.plan().ops, [], "plan did not converge")
        n = len(h.a.writes()) + len(h.b.writes())
        for _ in range(3):
            self.assertEqual(h.cycle()["applied"], {})
        self.assertEqual(len(h.a.writes()) + len(h.b.writes()), n)
        for (side, href), d in exempt.items():                       # byte-identical: never rewritten
            self.assertEqual((h.a if side == "a" else h.b).items[href][1], d, (side, href))
        # nothing was deleted anywhere
        self.assertTrue(set(before_a) <= set(h.a.items) and set(before_b) <= set(h.b.items))
        # recurrence structure survived the copy
        for store in (h.a, h.b):
            for href, (_, d) in store.items.items():
                r = im.parse_resource(d)
                if r.uid and r.master and r.overrides:
                    self.assertEqual(len(r.comps), 1 + len(r.overrides))

    def test_converged_calendars_have_matching_core_fingerprints_for_normal_series(self):
        h = self.harness()
        h.approve()
        for _ in range(8):
            h.cycle()
        by_uid = {}
        for side, store in (("a", h.a), ("b", h.b)):
            for _, d in store.items.values():
                r = im.parse_resource(d)
                if im.classify(r, SELFS)[0] == "normal":
                    by_uid.setdefault(r.uid, {})[side] = im.core_fp(r)
        both = {u: v for u, v in by_uid.items() if len(v) == 2}
        self.assertTrue(both)
        differing = {u for u, v in both.items() if v["a"] != v["b"]}
        # a same-UID pair that differs (EXDATEs, CLASS:PRIVATE, TZID-vs-UTC on a recurring series, ...) is never
        # adopted silently and never overwritten: it must surface as an explicit uid_conflict finding
        pl = h.plan()
        conflict_uids = {r.uid for f in pl.findings if f["kind"] == "uid_conflict"
                         for sd in pl.sides.values() for r in sd.values() if r.ident in f["ids"]}
        self.assertEqual(differing, conflict_uids)
        self.assertTrue(differing)

    def test_birthday_ledger_matches_across_providers(self):
        from calsync import birthdays
        h = self.harness()
        pl = h.plan()
        led = birthdays.ledger(pl.sides["a"], pl.sides["b"])
        self.assertTrue(led["matched_across_calendars"])
        self.assertEqual(led["birthday_series"]["a"] + led["birthday_series"]["b"],
                         sum(1 for sd in pl.sides.values() for r in sd.values() if r.kind == "birthday"))


@unittest.skipUnless(PRIV.is_dir(), "private fixtures not present (expected: never committed)")
class PrivateRealExports(unittest.TestCase):
    """Runs only on a workstation that holds .private-fixtures/. Asserts aggregate invariants."""

    @classmethod
    def setUpClass(cls):
        cls.a = {f"/a/{p.name}": p.read_bytes() for p in sorted((PRIV / "a").glob("*.ics"))}
        cls.b = {f"/b/{p.name}": p.read_bytes() for p in sorted((PRIV / "b").glob("*.ics"))}
        # derive my own addresses structurally: attendees of Google system birthdays + iCloud EMAIL= params
        selfs = set()
        for d in cls.b.values():
            for c in im.parse_resource(d).comps:
                if c.organizer.endswith("@calendar.google.com"):
                    selfs |= {a for a, _ in c.attendees}
        import re
        for d in cls.a.values():
            selfs |= {m.group(1).decode().lower() for m in re.finditer(rb"ORGANIZER[^\r\n]*EMAIL=([^;:\r\n]+)", d)}
        cls.selfs = frozenset(selfs)

    def harness(self):
        h = Harness(self, a=self.a, b=self.b)
        h.s.self_addresses = tuple(sorted(self.selfs))
        return h

    def test_counts_and_kinds_are_consistent_with_known_baseline(self):
        pl = self.harness().plan()
        self.assertEqual(pl.stats["resources"], {"a": len(self.a), "b": len(self.b)})
        for s in ("a", "b"):
            self.assertEqual(sum(pl.stats["kinds"][s].values()), pl.stats["resources"][s])

    def test_exempt_never_planned_and_full_cycle_converges_without_duplicates(self):
        h = self.harness()
        pl = h.plan()
        exempt = {r.ident for sd in pl.sides.values() for r in sd.values() if r.kind != "normal"}
        self.assertFalse({o.src_id for o in pl.ops} & exempt)
        exempt_bytes = {(s, hr): d for s, st in (("a", h.a), ("b", h.b)) for hr, (_, d) in st.items.items()
                        if im.classify(im.parse_resource(d), self.selfs)[0] != "normal"}
        h.approve()
        for _ in range(60):
            h.cycle()
            if not h.plan().ops:
                break
        self.assertEqual(h.plan().ops, [])
        for _ in range(2):
            self.assertEqual(h.cycle()["applied"], {})
        for (s, hr), d in exempt_bytes.items():
            self.assertEqual((h.a if s == "a" else h.b).items[hr][1], d)
        # no duplicate UID created inside either calendar
        for st in (h.a, h.b):
            u = [im.parse_resource(d).uid for _, d in st.items.values()]
            self.assertEqual(len(u), len(set(u)))
        # every normal resource now exists on both sides
        norm = {side: {im.parse_resource(d).uid for _, d in st.items.values() if im.classify(im.parse_resource(d), self.selfs)[0] == "normal"}
                for side, st in (("a", h.a), ("b", h.b))}
        held = {im.parse_resource(d).uid for st in (h.a, h.b) for _, d in st.items.values()
                if canary.provider_rewrite_risks(im.parse_resource(d))}
        self.assertEqual((norm["a"] ^ norm["b"]) - held, set())     # only video-conference items stay one-sided (held on purpose)


if __name__ == "__main__":
    unittest.main()
