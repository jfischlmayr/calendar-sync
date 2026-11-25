"""Regression tests for the live-activation round (found with the real-provider self-test on disposable calendars)."""
import unittest

from support import ics, simple, vevent
from test_engine import Harness, uids

from calsync import canary
from calsync import icsmodel as im
from calsync.util import write_json


def conf(uid, where="description", start="20260301T100000Z"):
    link = "Join https://teams.microsoft.com/l/meetup-join/abc"
    extra = [f"DESCRIPTION:{link}"] if where == "description" else [f"LOCATION:{link}"]
    return ics(vevent(uid, "Call", start, start[:9] + "11" + start[11:], extra=extra))


class DecisionOrder(unittest.TestCase):
    def synced(self, **kw):
        h = Harness(self, **kw)
        h.approve()
        h.cycle()
        return h

    def test_newest_prefer_decision_wins_over_a_stale_one(self):
        h = self.synced(a={"/a/1.ics": simple("e1", "Base")})
        h.a.put_raw(h.a.hrefs()[0], simple("e1", "Edit A"))
        h.b.put_raw(h.b.hrefs()[0], simple("e1", "Edit B"))
        ra = next(iter(h.plan().sides["a"].values()))
        stale = {"action": "prefer", "side": "a", "id": ra.ident, "fp": ["0123456789abcdef"]}     # recorded for an older version
        fresh = {"action": "prefer", "side": "a", "id": ra.ident, "fp": [ra.full[:16]]}
        write_json(h.s.decisions / "cal.json", [stale, fresh])
        self.assertEqual(h.cycle()["applied"], {"update_b": 1})
        self.assertIn(b"Edit A", h.b.datas()[0])

    def test_a_stale_decision_alone_stays_inert(self):
        h = self.synced(a={"/a/1.ics": simple("e1", "Base")})
        h.a.put_raw(h.a.hrefs()[0], simple("e1", "Edit A"))
        h.b.put_raw(h.b.hrefs()[0], simple("e1", "Edit B"))
        ra = next(iter(h.plan().sides["a"].values()))
        write_json(h.s.decisions / "cal.json", [{"action": "prefer", "side": "a", "id": ra.ident, "fp": ["0123456789abcdef"]}])
        self.assertEqual(h.cycle()["applied"], {})


class HeldProviderRewrite(unittest.TestCase):
    def test_conference_item_is_never_created_and_does_not_block_initialisation(self):
        h = Harness(self, a={"/a/1.ics": conf("c1"), "/a/2.ics": simple("e2", start="20260302T100000Z")})
        h.approve()
        r = h.cycle()
        self.assertEqual(r["applied"], {"create_b": 1})
        self.assertEqual(r["findings"].get("held_provider_rewrite"), 1)
        self.assertEqual(uids(h.b), ["e2"])
        self.assertEqual(r["initialized"], {"a": True, "b": True})
        self.assertEqual(h.cycle()["applied"], {})           # stays held, no write, no block
        self.assertFalse((h.s.state / "BLOCK_cal").exists())

    def test_location_text_is_also_held(self):
        h = Harness(self, b={"/b/1.ics": conf("c1", "location")})
        h.approve()
        r = h.cycle()
        self.assertEqual((r["applied"], r["findings"].get("held_provider_rewrite")), ({}, 1))
        self.assertEqual(uids(h.a), [])

    def test_edit_that_adds_a_conference_link_is_held_not_written(self):
        h = Harness(self, a={"/a/1.ics": simple("e1", "Plain")})
        h.approve()
        h.cycle()
        before = list(h.b.datas())
        h.a.put_raw(h.a.hrefs()[0], conf("e1"))
        r = h.cycle()
        self.assertEqual(r["applied"], {})
        self.assertEqual(r["findings"].get("held_provider_rewrite"), 1)
        self.assertEqual(list(h.b.datas()), before)

    def test_plain_items_are_unaffected(self):
        r = im.parse_resource(simple("e1"))
        self.assertEqual(canary.provider_rewrite_risks(r), [])

    def test_conference_property_is_a_risk(self):
        r = im.parse_resource(ics(vevent("e1", extra=["X-GOOGLE-CONFERENCE:https://meet.example/x"])))
        self.assertEqual(canary.provider_rewrite_risks(r), ["conference_property"])


class PersistentIgnore(unittest.TestCase):
    def test_ignored_special_items_keep_their_side_and_let_the_pair_initialise(self):
        h = Harness(self, a={"/a/1.ics": simple("sp", "Special"), "/a/2.ics": simple("ok", "Normal", start="20260302T100000Z")},
                    b={"/b/1.ics": simple("sp2", "Special B", start="20260303T100000Z")})
        pl = h.plan()
        ids = [r.ident for sd in pl.sides.values() for r in sd.values() if r.uid in ("sp", "sp2")]
        write_json(h.s.decisions / "cal.json", [{"action": "ignore", "id": i, "side": "", "fp": ["x"]} for i in ids])
        h.approve()
        r = h.cycle()
        self.assertEqual(r["applied"], {"create_b": 1})            # only the normal item
        self.assertEqual(r["findings"], {"exempt_ignored": 2})
        self.assertEqual(r["initialized"], {"a": True, "b": True})
        self.assertEqual(sorted(uids(h.a)), ["ok", "sp"])
        self.assertEqual(sorted(uids(h.b)), ["ok", "sp2"])
        # persistent: editing an ignored item later still never copies it
        h.a.put_raw("/a/1.ics", simple("sp", "Special edited"))
        self.assertEqual(h.cycle()["applied"], {})
        self.assertEqual(sorted(uids(h.b)), ["ok", "sp2"])


if __name__ == "__main__":
    unittest.main()
