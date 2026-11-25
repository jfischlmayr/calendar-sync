"""Regression tests for defects found by the independent adversarial review of the engine."""
import unittest

from support import ME, MemoryStore, ics, simple, vevent
from test_engine import Harness, SELF, uids

from calsync import engine as eng, icsmodel as im, syncrun
from calsync.store import StoreError
from calsync.util import now, read_json, write_json


def rich(uid, title="Zahnarzt", desc="Versicherung beachten", loc="Praxis Mitte", extra=()):
    return ics(vevent(uid, title, "20260301T100000Z", "20260301T110000Z", extra=[f"DESCRIPTION:{desc}", f"LOCATION:{loc}", *extra]))


def bare(uid, title="Zahnarzt"):
    return ics(vevent(uid, title, "20260301T100000Z", "20260301T110000Z"))


class Review(unittest.TestCase):
    def test_1_adopted_duplicate_with_different_description_never_overwrites(self):
        h = Harness(self, a={"/a/1.ics": rich("u-apple")}, b={"/b/1.ics": bare("g1@google.com")})
        h.approve()
        r = h.cycle()
        self.assertEqual(r["applied"], {})
        self.assertEqual(r["findings"].get("duplicate_content_differs"), 1)
        a_before = h.a.datas()[0]
        # unrelated edit on B (alarm only): previously this wiped A's description/location
        hb = h.b.hrefs()[0]
        h.b.put_raw(hb, h.b.items[hb][1].replace(b"-PT15M", b"-PT30M"))
        r = h.cycle()
        self.assertEqual(r["applied"], {})
        self.assertEqual(h.a.datas()[0], a_before)
        self.assertIn(b"Versicherung", h.a.datas()[0])
        # operator picks a side explicitly
        pl = h.plan()
        ra = next(iter(pl.sides["a"].values()))
        write_json(h.s.decisions / "cal.json", [{"action": "prefer", "side": "a", "id": ra.ident, "fp": [ra.full[:16]]}])
        self.assertEqual(h.cycle()["applied"], {"update_b": 1})
        self.assertIn(b"Versicherung", h.b.datas()[0])
        self.assertEqual(uids(h.b), ["g1@google.com"])

    def test_2_delete_and_recreate_with_new_uid_is_not_duplicated(self):
        h = Harness(self, a={"/a/1.ics": bare("a1")})
        h.approve()
        h.cycle()
        hb = h.b.hrefs()[0]
        h.b.remove_raw(hb)
        h.b.put_raw("/b/new.ics", bare("g-new@google.com"))        # user re-created the event in Google
        r = h.cycle()
        self.assertEqual(r["applied"], {})
        self.assertEqual(uids(h.a), ["a1"])
        self.assertEqual(r["findings"].get("suspect_duplicate"), 1)
        self.assertEqual(r["findings"].get("pending_delete"), 1)

    def test_3_seed_approval_does_not_cover_live_direction_a(self):
        h = Harness(self, mode="seed", a={"/a/1.ics": simple("a1")}, b={"/b/1.ics": simple("b1", start="20260302T100000Z")})
        rep = syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",))
        syncrun.approve_plan(h.s, h.p, "t", "n", rep["plan_file"]["b"], "b")
        self.assertEqual(set(syncrun.approved_by_dst(h.s, h.p)), {"b"})

    def test_4_ack_deletes_is_one_shot(self):
        h = Harness(self, a={f"/a/{i}.ics": simple(f"e{i}", start=f"2026030{i + 1}T100000Z") for i in range(9)})
        h.approve()
        h.cycle()
        h.s.propagate_deletes = True
        for i in range(5):
            h.a.remove_raw(f"/a/{i}.ics")
        write_json(h.s.state / "ack_deletes_cal.json", {"max": 5, "expires": now() + 600})
        self.assertEqual(h.cycle()["applied"], {"delete_b": 5})
        self.assertFalse((h.s.state / "ack_deletes_cal.json").exists())
        for i in range(5, 9):
            h.a.remove_raw(f"/a/{i}.ics")
        with self.assertRaises(eng.EngineBlocked):                # 4 deleted: still above the plain cap of 3
            h.cycle()

    def test_vanish_guard_applies_even_when_deletes_are_not_propagated(self):
        h = Harness(self, a={f"/a/{i}.ics": simple(f"e{i}", start=f"2026030{i + 1}T100000Z") for i in range(8)})
        h.approve()
        h.cycle()
        for i in range(5):                       # 5 > max(3, 10%) vanish from A, propagate_deletes is off
            h.a.remove_raw(f"/a/{i}.ics")
        with self.assertRaises(eng.EngineBlocked) as cm:
            h.cycle()
        self.assertIn("vanished", str(cm.exception))

    def test_5_alarm_only_and_privacy_changes_propagate(self):
        h = Harness(self, a={"/a/1.ics": bare("e1")})
        h.approve()
        h.cycle()
        ha = h.a.hrefs()[0]
        h.a.put_raw(ha, h.a.items[ha][1].replace(b"-PT15M", b"-PT45M"))
        self.assertEqual(h.cycle()["applied"], {"update_b": 1})
        self.assertIn(b"-PT45M", h.b.datas()[0])
        h.a.put_raw(ha, h.a.items[ha][1].replace(b"SUMMARY:Zahnarzt", b"CLASS:PRIVATE\r\nSUMMARY:Zahnarzt"))
        self.assertEqual(h.cycle()["applied"], {"update_b": 1})
        self.assertIn(b"CLASS:PRIVATE", h.b.datas()[0])

    def test_5b_provider_dropping_class_private_fails_verify(self):
        h = Harness(self, a={"/a/1.ics": ics(vevent("e1", extra=["CLASS:PRIVATE"]))},
                    mutate_b=lambda d: d.replace(b"CLASS:PRIVATE\r\n", b""))
        h.approve()
        with self.assertRaises(eng.VerifyFailed):
            h.cycle()

    def test_6_unresolved_timezone_lookalike_is_not_copied(self):
        from unittest.mock import patch
        a = ics(vevent("n1", "Zahnarzt", "20260301T110000", "20260301T120000", tzid="Europe/Vienna"), tz=True)
        b = ics(vevent("g1@google.com", "Zahnarzt", "20260301T100000Z", "20260301T110000Z"))
        with patch.object(im, "_zone", lambda name: None):          # image without tzdata
            h = Harness(self, a={"/a/1.ics": a}, b={"/b/1.ics": b})
            pl = h.plan()
        self.assertEqual([o for o in pl.ops if o.kind == "create"], [])
        self.assertEqual(pl.stats["findings"].get("suspect_duplicate"), 1)

    def test_6b_tz_selfcheck_blocks_run(self):
        from unittest.mock import patch
        h = Harness(self, a={"/a/1.ics": simple("a1")})
        h.approve()
        with patch.object(im, "_zone", lambda name: None):
            with self.assertRaises(syncrun.Blocked):
                h.cycle()

    def test_floating_nested_and_barecr_are_unsupported(self):
        floating = ics(vevent("f1", start="20260301T100000", end="20260301T110000"))
        nested = ics(vevent("p1", extra=["BEGIN:PARTICIPANT", "UID:x", "PARTICIPANT-TYPE:ATTENDEE", "CALENDAR-ADDRESS:mailto:boss@corp.example", "END:PARTICIPANT"]))
        barecr = simple("c1").replace(b"SUMMARY:Meeting\r\n", b"SUMMARY:Meeting\r")
        for name, d in {"floating": floating, "nested": nested, "barecr": barecr}.items():
            with self.subTest(name):
                self.assertEqual(im.classify(im.parse_resource(d), frozenset(SELF))[0], "unsupported")

    def test_flapping_link_is_frozen(self):
        h = Harness(self, a={"/a/1.ics": bare("e1")})
        h.approve()
        h.cycle()
        ha = h.a.hrefs()[0]
        n, seen = 0, False
        for i in range(8):
            h.a.put_raw(ha, h.a.items[ha][1].replace(b"-PT15M" if i % 2 == 0 else b"-PT20M", b"-PT20M" if i % 2 == 0 else b"-PT15M"))
            r = h.cycle()
            n += r["applied"].get("update_b", 0)
            seen = seen or r["findings"].get("flapping_link") == 1
        self.assertLessEqual(n, eng.FLAP_LIMIT)
        self.assertTrue(seen)

    def test_lost_response_after_create_recovers_without_duplicate(self):
        h = Harness(self, a={"/a/1.ics": simple("e1")})
        h.approve()
        h.b.lose_response["create"] = StoreError("timeout after PUT", transient=True)
        with self.assertRaises(StoreError):
            h.cycle()
        self.assertEqual(len(h.b.items), 1)                         # the write DID happen
        r = h.cycle()
        self.assertEqual(r["applied"], {})
        self.assertEqual(len(h.b.items), 1)

    def test_create_409_uid_conflict_is_isolated(self):
        h = Harness(self, a={"/a/1.ics": simple("e1"), "/a/2.ics": simple("e2", start="20260302T100000Z")})
        h.b.put_raw("/b/x.ics", ics(vevent("e1", extra=["X-PHANTOM:1"])).replace(b"SUMMARY:Meeting", b"SUMMARY:Other"))
        # plan sees UID twin with different content -> uid_conflict, only e2 is created
        h.approve()
        r = h.cycle()
        self.assertEqual(r["applied"], {"create_b": 1})
        self.assertEqual(r["findings"].get("uid_conflict"), 1)


if __name__ == "__main__":
    unittest.main()
