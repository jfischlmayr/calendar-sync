"""Engine tests against in-memory provider fakes: classification, first-time reconciliation,
exemptions, idempotence, change/deletion propagation, outages, restart, verify-after-write."""
import tempfile
import unittest
from pathlib import Path

from support import ME, MemoryStore, ics, simple, vevent

from calsync import config, engine as eng, icsmodel as im, syncrun
from calsync.store import PreconditionFailed, StoreError
from calsync.util import now, read_json, write_json

SELF = (ME, "me@gmail.example")


def items_of(store):
    return store.list_items()


class Harness:
    def __init__(self, tc: unittest.TestCase, mode="live", a=None, b=None, mutate_b=None, mutate_a=None):
        self.tc = tc
        t = tempfile.TemporaryDirectory()
        tc.addCleanup(t.cleanup)
        self.s = config.Settings(data_dir=Path(t.name), run_dir=Path(t.name) / "run", write_enabled=True,
                                 self_addresses=SELF, max_delete_abs=3)
        self.s.ensure_dirs()
        self.p = config.Pair(name="cal", icloud_collection="x", google_collection="y", mode=mode)
        self.a = MemoryStore("a", a, mutate=mutate_a)
        self.b = MemoryStore("b", b, mutate=mutate_b)
        write_json(self.s.state / "backup_ok.json", {"ts": now(), "offhost": True})

    @property
    def stores(self):
        return {"a": self.a, "b": self.b}

    def plan(self):
        plan, st, cfg = syncrun.read_plan(self.s, self.p, self.stores)
        return plan

    def approve(self, dirs=("a", "b")):
        rep = syncrun.audit_pair(self.s, self.p, self.stores, write_plan=tuple(dirs))
        for d in dirs:
            syncrun.approve_plan(self.s, self.p, "tester", "ok", rep["plan_file"][d], d)
        return rep

    def cycle(self):
        return syncrun.run_pair_engine(self.s, self.p, self.stores)

    def state(self):
        return syncrun.load_state(self.s, self.p)


def uids(store):
    return sorted(im.parse_resource(d).uid for d in store.datas())


class ClassificationTests(unittest.TestCase):
    def kind(self, data, selfs=frozenset(SELF)):
        r = im.parse_resource(data)
        return im.classify(r, selfs)

    def test_normal(self):
        self.assertEqual(self.kind(simple("u1"))[0], "normal")

    def test_valarm_email_attendee_is_not_a_guest(self):
        d = ics(vevent("u1", extra=["BEGIN:VALARM", "ACTION:EMAIL", "ATTENDEE:mailto:x@y.example", "TRIGGER:-PT5M", "END:VALARM"]))
        self.assertEqual(self.kind(d)[0], "normal")

    def test_external_organizer_is_guest(self):
        d = ics(vevent("u1", organizer="hotel@hotel.example", attendees=[(ME, "NEEDS-ACTION")]))
        self.assertEqual(self.kind(d), ("guest", ["organizer_not_self"]))

    def test_external_attendee_is_guest(self):
        d = ics(vevent("u1", organizer=ME, attendees=[(ME, "ACCEPTED"), ("other@x.example", "NEEDS-ACTION")]))
        self.assertEqual(self.kind(d)[0], "guest")

    def test_self_only_organizer_is_normal(self):
        d = ics(vevent("u1", organizer=ME, attendees=[(ME, "ACCEPTED")]))
        self.assertEqual(self.kind(d)[0], "normal")

    def test_unknown_self_makes_every_attendee_guest(self):
        d = ics(vevent("u1", organizer=ME, attendees=[(ME, "ACCEPTED")]))
        self.assertEqual(self.kind(d, frozenset())[0], "guest")

    def test_yearly_allday_is_birthday_even_with_google_system_organizer(self):
        d = ics(vevent("u1", allday=True, start="20240101", end="20240102", rrule="FREQ=YEARLY",
                       organizer="abc@calendar.google.com", attendees=[("me@gmail.example", "ACCEPTED")]))
        k, why = self.kind(d)
        self.assertEqual(k, "birthday")
        self.assertIn("google_system_organizer", why)

    def test_yearly_timed_is_normal(self):
        self.assertEqual(self.kind(ics(vevent("u1", rrule="FREQ=YEARLY")))[0], "normal")

    def test_unsupported_cases(self):
        cases = {
            "orphan_override": ics(vevent("u1", rid="20260301T100000Z")),
            "two_uids": ics(vevent("u1"), vevent("u2")),
            "no_uid": ics(vevent("u1").replace("UID:u1\r\n", "")),
            "range": ics(vevent("u1"), vevent("u1", rid="20260301T100000Z").replace("RECURRENCE-ID:", "RECURRENCE-ID;RANGE=THISANDFUTURE:")),
            "rdate": ics(vevent("u1", extra=["RDATE:20260401T100000Z"])),
            "vtodo": b"BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:t\r\nSUMMARY:x\r\nEND:VTODO\r\nEND:VCALENDAR\r\n",
            "unknown_tz": ics(vevent("u1", start="20260301T100000", end="20260301T110000", tzid="Mars/Olympus")),
            "garbage": b"hello",
        }
        for name, d in cases.items():
            with self.subTest(name):
                self.assertEqual(self.kind(d)[0], "unsupported", name)

    def test_series_with_overrides_in_one_resource_is_normal(self):
        d = ics(vevent("u1", rrule="FREQ=WEEKLY;COUNT=5", exdates=["20260315T100000Z"]),
                vevent("u1", rid="20260308T100000Z", start="20260308T120000Z"),
                vevent("u1", rid="20260322T100000Z", status="CANCELLED"))
        self.assertEqual(self.kind(d)[0], "normal")


class TimeModelTests(unittest.TestCase):
    def core(self, d):
        return im.core_fp(im.parse_resource(d))

    def test_vienna_local_equals_utc(self):
        a = ics(vevent("u", "Zahnarzt", "20260301T110000", "20260301T120000", tzid="Europe/Vienna"), tz=True)
        b = ics(vevent("g@google.com", "Zahnarzt", "20260301T100000Z", "20260301T110000Z"))
        self.assertEqual(self.core(a), self.core(b))

    def test_dst_summer_offset(self):
        a = ics(vevent("u", "x", "20260715T110000", "20260715T120000", tzid="Europe/Vienna"), tz=True)
        b = ics(vevent("u", "x", "20260715T090000Z", "20260715T100000Z"))
        self.assertEqual(self.core(a), self.core(b))

    def test_gmt_offset_tzid_with_vtimezone(self):
        vt = ["BEGIN:VTIMEZONE", "TZID:GMT+0200", "BEGIN:STANDARD", "DTSTART:19700101T000000", "TZOFFSETFROM:+0200",
              "TZOFFSETTO:+0200", "END:STANDARD", "END:VTIMEZONE"]
        a = ("\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", *vt]) + "\r\n" +
             vevent("u", "x", "20260715T110000", "20260715T120000", tzid="GMT+0200") + "\r\nEND:VCALENDAR\r\n").encode()
        b = ics(vevent("u", "x", "20260715T090000Z", "20260715T100000Z"))
        self.assertEqual(self.core(a), self.core(b))

    def test_duration_equals_dtend(self):
        a = ics(vevent("u", "x", "20260301T100000Z", extra=["DURATION:PT1H"]).replace("DTEND", "X-NO"))
        b = ics(vevent("u", "x", "20260301T100000Z", "20260301T110000Z"))
        self.assertEqual(self.core(a), self.core(b))

    def test_allday_default_end(self):
        a = ics(vevent("u", "x", "20260301", allday=True))
        b = ics(vevent("u", "x", "20260301", "20260302", allday=True))
        self.assertEqual(self.core(a), self.core(b))

    def test_different_exdates_differ(self):
        a = ics(vevent("u", rrule="FREQ=WEEKLY", exdates=["20260308T100000Z"]))
        b = ics(vevent("u", rrule="FREQ=WEEKLY"))
        self.assertNotEqual(self.core(a), self.core(b))

    def test_full_fp_ignores_volatile_but_sees_alarm_and_text(self):
        base = vevent("u", "x")
        a = ics(base)
        self.assertEqual(im.full_fp(im.parse_resource(a)),
                         im.full_fp(im.parse_resource(a.replace(b"20260101T000000Z", b"20270101T000000Z"))))
        self.assertNotEqual(im.full_fp(im.parse_resource(a)), im.full_fp(im.parse_resource(a.replace(b"-PT15M", b"-PT30M"))))
        self.assertNotEqual(im.full_fp(im.parse_resource(a)), im.full_fp(im.parse_resource(a.replace(b"SUMMARY:x", b"SUMMARY:y"))))

    def test_title_similarity(self):
        self.assertTrue(im.titles_similar("Geburtstag Alex", "Alex Geburtstag"))
        self.assertTrue(im.titles_similar("Zahnarzt", "Zahnarzt Dr. M"))
        self.assertFalse(im.titles_similar("Zahnarzt", "Steuerberater"))

    def test_prepare_outgoing_preserves_bytes_and_strips_calendar_props(self):
        d = ics(vevent("u", "Äpfel, Birnen"), calprops=["X-WR-CALNAME:Privat", "X-WR-TIMEZONE:Europe/Vienna", "METHOD:PUBLISH",
                                                     "X-KEEP:1"])
        out = im.prepare_outgoing(d)
        self.assertNotIn(b"X-WR-CALNAME", out)
        self.assertNotIn(b"METHOD", out)
        self.assertIn(b"X-KEEP:1", out)
        self.assertIn("Äpfel".encode(), out)
        self.assertEqual(out.count(b"\r\n"), d.count(b"\r\n") - 3)
        self.assertIn(b"UID:new\r\n", im.prepare_outgoing(d, "new"))
        self.assertNotIn(b"UID:u\r\n", im.prepare_outgoing(d, "new"))


class FirstSyncTests(unittest.TestCase):
    def test_one_sided_events_become_creates_both_directions(self):
        h = Harness(self, a={"/a/1.ics": simple("a1", "A only")}, b={"/b/1.ics": simple("b1", "B only", "20260302T100000Z")})
        pl = h.plan()
        self.assertEqual(pl.stats["ops"], {"create_a": 1, "create_b": 1})

    def test_uid_twin_with_other_timezone_notation_is_adopted_not_copied(self):
        a = ics(vevent("same", "Zahnarzt", "20260301T110000", "20260301T120000", tzid="Europe/Vienna"), tz=True)
        b = ics(vevent("same", "Zahnarzt", "20260301T100000Z", "20260301T110000Z"),
                prodid="-//Google Inc//EN", calprops=["X-WR-CALNAME:Privat"])
        h = Harness(self, a={"/a/1.ics": a}, b={"/b/1.ics": b})
        pl = h.plan()
        self.assertEqual(pl.ops, [])
        self.assertEqual(pl.stats["adopt"], {"uid": 1})

    def test_uid_conflict_is_reported_and_untouched(self):
        h = Harness(self, a={"/a/1.ics": simple("same", "Version A")}, b={"/b/1.ics": simple("same", "Version B")})
        pl = h.plan()
        self.assertEqual(pl.ops, [])
        self.assertEqual(pl.stats["findings"], {"uid_conflict": 1})

    def test_different_uid_strict_duplicate_adopted_and_never_duplicated(self):
        h = Harness(self, a={"/a/1.ics": simple("uuid-1", "Arzt")}, b={"/b/1.ics": simple("x@google.com", "Arzt")})
        h.cycle_ready = None
        pl = h.plan()
        self.assertEqual(pl.ops, [])
        self.assertEqual(pl.stats["adopt"], {"duplicate": 1})

    def test_loose_duplicate_is_suspect_not_copied(self):
        h = Harness(self, a={"/a/1.ics": simple("u1", "Alex Geburtstagsfeier")},
                    b={"/b/1.ics": simple("u2", "Geburtstagsfeier Alex 18 Uhr", end="20260301T123000Z")})
        pl = h.plan()
        self.assertEqual(pl.ops, [])
        self.assertEqual(pl.stats["findings"], {"suspect_duplicate": 2})

    def test_guest_birthday_unsupported_are_never_planned(self):
        guest = ics(vevent("g1", "Hotel", organizer="hotel@hotel.example", attendees=[(ME, "NEEDS-ACTION")]))
        bday = ics(vevent("b1", "Max", start="20000102", end="20000103", allday=True, rrule="FREQ=YEARLY"))
        bad = ics(vevent("o1", rid="20260301T100000Z"))
        h = Harness(self, a={"/a/g.ics": guest, "/a/b.ics": bday, "/a/o.ics": bad},
                    b={"/b/g.ics": guest.replace(b"g1", b"g2"), "/b/b.ics": bday.replace(b"b1", b"gb@google.com")})
        pl = h.plan()
        self.assertEqual(pl.ops, [])
        self.assertEqual(pl.stats["findings"].get("exempt_guest"), 2)
        self.assertEqual(pl.stats["findings"].get("exempt_birthday"), 2)
        self.assertEqual(pl.stats["findings"].get("exempt_unsupported"), 1)

    def test_normal_event_not_copied_next_to_exempt_lookalike(self):
        guest = ics(vevent("g1", "Hotel Stay", "20260301T100000Z", "20260301T110000Z", organizer="hotel@hotel.example",
                           attendees=[(ME, "NEEDS-ACTION")]))
        h = Harness(self, a={"/a/1.ics": simple("n1", "Hotel Stay")}, b={"/b/g.ics": guest})
        self.assertEqual([o for o in h.plan().ops if o.kind == "create"], [])

    def test_duplicate_uid_inside_one_calendar_is_unsupported(self):
        h = Harness(self, a={"/a/1.ics": simple("d"), "/a/2.ics": simple("d")})
        pl = h.plan()
        self.assertEqual(pl.ops, [])
        self.assertEqual(pl.stats["findings"], {"exempt_unsupported": 2})

    def test_first_write_requires_approved_plan(self):
        h = Harness(self, a={"/a/1.ics": simple("a1")})
        with self.assertRaises(syncrun.Blocked) as cm:
            h.cycle()
        self.assertIn("no approved plan", str(cm.exception))
        self.assertEqual(h.b.writes(), [])

    def test_new_event_after_approval_is_held_back_until_reapproved(self):
        h = Harness(self, a={"/a/1.ics": simple("a1")})
        h.approve(("b",))
        h.a.put_raw("/a/2.ics", simple("a2", "Later", "20260305T100000Z"))
        r = h.cycle()
        self.assertEqual(r["applied"], {"create_b": 1})
        self.assertEqual(r["skipped"], {"unapproved_before_initialization": 1})
        self.assertFalse(h.state()["initialized"]["b"])        # unapproved op keeps the direction un-initialised
        self.assertEqual(h.cycle()["applied"], {})
        h.approve(("b",))                                      # operator reviews the remainder
        self.assertEqual(h.cycle()["applied"], {"create_b": 1})
        self.assertTrue(h.state()["initialized"]["b"])
        h.a.put_raw("/a/3.ics", simple("a3", "Steady", "20260306T100000Z"))
        self.assertEqual(h.cycle()["applied"], {"create_b": 1})   # steady state: no approval needed any more
        self.assertEqual(uids(h.b), ["a1", "a2", "a3"])

    def test_empty_approval_does_not_open_the_gate_for_later_events(self):
        h = Harness(self, a={})
        h.approve(("b",))
        for i in range(30):
            h.a.put_raw(f"/a/{i}.ics", simple(f"n{i}", start=f"2026{3 + i // 28:02d}{i % 28 + 1:02d}T100000Z"))
        for _ in range(3):
            self.assertEqual(h.cycle()["applied"], {})
        self.assertEqual(h.b.writes(), [])
        self.assertFalse(h.state()["initialized"]["b"])

    def test_burst_guard_after_initialisation(self):
        h = Harness(self, a={"/a/0.ics": simple("e0")})
        h.s.max_burst_ops = 5
        h.approve(("b",))
        h.cycle()
        self.assertTrue(h.state()["initialized"]["b"])
        for i in range(1, 9):
            h.a.put_raw(f"/a/{i}.ics", simple(f"e{i}", start=f"2026040{i}T100000Z"))
        with self.assertRaises(syncrun.Blocked) as cm:
            h.cycle()
        self.assertIn("max_burst_ops", str(cm.exception))
        h.approve(("b",))                                      # fresh review lifts it
        self.assertEqual(h.cycle()["applied"], {"create_b": 8})

    def test_item_edited_after_approval_copies_current_version(self):
        h = Harness(self, a={"/a/1.ics": simple("a1", "Original")})
        h.approve()
        h.a.put_raw("/a/1.ics", simple("a1", "Edited"))
        r = h.cycle()
        self.assertEqual(r["applied"], {"create_b": 1})
        self.assertIn(b"Edited", h.b.datas()[0])

    def test_item_removed_before_first_run_means_nothing_copied(self):
        h = Harness(self, a={"/a/1.ics": simple("a1")})
        h.approve()
        h.a.remove_raw("/a/1.ics")
        h.a.put_raw("/a/2.ics", simple("a2", "Other", "20260305T100000Z"))
        r = h.cycle()
        self.assertEqual(r["applied"], {})
        self.assertEqual(h.b.writes(), [])

    def test_gates(self):
        h = Harness(self, a={"/a/1.ics": simple("a1")})
        h.s.write_enabled = False
        self.assertIn("CALSYNC_ALLOW_WRITES!=1", syncrun.gate_failures(h.s, h.p))
        h.s.write_enabled = True
        (h.s.state / "PAUSE").touch()
        self.assertTrue(any("PAUSE" in f for f in syncrun.gate_failures(h.s, h.p)))
        (h.s.state / "PAUSE").unlink()
        write_json(h.s.state / "backup_ok.json", {"ts": now() - 100 * 3600, "offhost": True})
        self.assertTrue(any("backup" in f for f in syncrun.gate_failures(h.s, h.p)))
        write_json(h.s.state / "backup_ok.json", {"ts": now() + 10 * 3600, "offhost": True})   # future timestamp
        self.assertTrue(any("backup" in f for f in syncrun.gate_failures(h.s, h.p)))
        write_json(h.s.state / "backup_ok.json", {"ts": now(), "offhost": False})
        self.assertTrue(any("off-host" in f for f in syncrun.gate_failures(h.s, h.p)))
        write_json(h.s.state / "backup_ok.json", {"ts": now(), "offhost": True})
        h.s.self_addresses = ()
        self.assertTrue(any("self_addresses" in f for f in syncrun.gate_failures(h.s, h.p)))
        h.s.self_addresses = SELF
        h.p.mode = "mirror"
        self.assertTrue(any("mode" in f for f in syncrun.gate_failures(h.s, h.p)))


class SyncCycleTests(unittest.TestCase):
    def synced(self, a=None, b=None, **kw):
        h = Harness(self, a=a or {}, b=b or {}, **kw)
        h.approve()
        h.cycle()
        return h

    def test_two_cycles_idempotent_and_copies_present(self):
        h = Harness(self, a={"/a/1.ics": simple("a1", "A")}, b={"/b/1.ics": simple("b1", "B", "20260302T100000Z")})
        h.approve()
        r1 = h.cycle()
        self.assertEqual(r1["applied"], {"create_a": 1, "create_b": 1})
        self.assertEqual(uids(h.a), ["a1", "b1"])
        self.assertEqual(uids(h.b), ["a1", "b1"])
        nw = len(h.a.writes()) + len(h.b.writes())
        for _ in range(3):
            r = h.cycle()
            self.assertEqual(r["applied"], {}, r)
        self.assertEqual(len(h.a.writes()) + len(h.b.writes()), nw)
        self.assertEqual(h.plan().ops, [])

    def test_seed_mode_writes_only_side_b(self):
        h = Harness(self, mode="seed", a={"/a/1.ics": simple("a1")}, b={"/b/1.ics": simple("b1", start="20260302T100000Z")})
        h.approve(("b",))
        h.a.read_only = True            # any write to A raises RuntimeError
        r = h.cycle()
        self.assertEqual(r["applied"], {"create_b": 1})
        self.assertIn("mode_forbids_side_a", r["skipped"])
        self.assertEqual(h.a.writes(), [])
        self.assertFalse(h.state()["initialized"]["a"])

    def test_going_live_after_seed_needs_its_own_approval_for_side_a(self):
        h = Harness(self, mode="seed", a={"/a/1.ics": simple("a1")}, b={"/b/1.ics": simple("b1", start="20260302T100000Z")})
        h.approve(("b",))                      # approval covers ONLY the Google-bound ops
        h.cycle()
        self.assertTrue(h.state()["initialized"]["b"])
        h.p.mode = "live"
        with self.assertRaises(syncrun.Blocked) as cm:
            h.cycle()
        self.assertIn("['a']", str(cm.exception))
        self.assertEqual(h.a.writes(), [])
        h.approve(("a",))
        self.assertEqual(h.cycle()["applied"], {"create_a": 1})

    def test_change_propagates_a_to_b_and_b_to_a_without_ping_pong(self):
        h = self.synced(a={"/a/1.ics": simple("e1", "Old")})
        href_a = h.a.hrefs()[0]
        h.a.put_raw(href_a, simple("e1", "New from A"))
        self.assertEqual(h.cycle()["applied"], {"update_b": 1})
        self.assertIn(b"New from A", h.b.datas()[0])
        self.assertEqual(h.cycle()["applied"], {})
        href_b = h.b.hrefs()[0]
        h.b.put_raw(href_b, h.b.datas()[0].replace(b"New from A", b"Edited in Google"))
        self.assertEqual(h.cycle()["applied"], {"update_a": 1})
        self.assertIn(b"Edited in Google", h.a.datas()[0])
        self.assertEqual(h.cycle()["applied"], {})

    def test_provider_rewriting_data_does_not_ping_pong(self):
        # Google-like provider adds X-WR props, swaps PRODID, adds an alarm on every store
        def google(d):
            return d.replace(b"PRODID:-//Apple Inc.//Test//EN", b"PRODID:-//Google Inc//EN\r\nX-WR-CALNAME:Privat").replace(
                b"END:VEVENT", b"BEGIN:VALARM\r\nACTION:EMAIL\r\nTRIGGER:-PT10M\r\nEND:VALARM\r\nEND:VEVENT")
        h = Harness(self, a={"/a/1.ics": simple("e1", "A")}, mutate_b=google)
        h.approve()
        h.cycle()
        for _ in range(4):
            self.assertEqual(h.cycle()["applied"], {})
        self.assertEqual(len(h.a.writes()), 0)

    def test_conflict_both_edited_nothing_overwritten(self):
        h = self.synced(a={"/a/1.ics": simple("e1", "Base")})
        h.a.put_raw(h.a.hrefs()[0], simple("e1", "Edit A"))
        h.b.put_raw(h.b.hrefs()[0], simple("e1", "Edit B"))
        before = (list(h.a.datas()), list(h.b.datas()))
        r = h.cycle()
        self.assertEqual(r["applied"], {})
        self.assertEqual(r["findings"], {"conflict": 1})
        self.assertEqual((list(h.a.datas()), list(h.b.datas())), before)

    def test_prefer_decision_resolves_conflict_and_is_bound_to_fingerprint(self):
        h = self.synced(a={"/a/1.ics": simple("e1", "Base")})
        h.a.put_raw(h.a.hrefs()[0], simple("e1", "Edit A"))
        h.b.put_raw(h.b.hrefs()[0], simple("e1", "Edit B"))
        pl = h.plan()
        rid = next(r for r in pl.sides["a"].values()).ident
        fp = next(r for r in pl.sides["a"].values()).full[:16]
        write_json(h.s.decisions / "cal.json", [{"action": "prefer", "side": "a", "id": rid, "fp": [fp]}])
        self.assertEqual(h.cycle()["applied"], {"update_b": 1})
        self.assertIn(b"Edit A", h.b.datas()[0])
        # stale decision (item changed again) must be inert
        h.a.put_raw(h.a.hrefs()[0], simple("e1", "Edit A2"))
        h.b.put_raw(h.b.hrefs()[0], simple("e1", "Edit B2"))
        write_json(h.s.decisions / "cal.json", [{"action": "prefer", "side": "a", "id": rid, "fp": [fp]}])
        self.assertEqual(h.cycle()["applied"], {})

    def test_deletions_not_propagated_by_default(self):
        h = self.synced(a={"/a/1.ics": simple("e1"), "/a/2.ics": simple("e2", start="20260302T100000Z")})
        h.a.remove_raw("/a/1.ics")
        r = h.cycle()
        self.assertEqual(r["applied"], {})
        self.assertEqual(r["findings"], {"pending_delete": 1})
        self.assertEqual(uids(h.b), ["e1", "e2"])

    def test_deletion_propagates_when_enabled_within_caps(self):
        h = self.synced(a={f"/a/{i}.ics": simple(f"e{i}", start=f"2026030{i + 1}T100000Z") for i in range(8)})
        h.s.propagate_deletes = True
        h.a.remove_raw("/a/0.ics")
        self.assertEqual(h.cycle()["applied"], {"delete_b": 1})
        self.assertEqual(len(h.b.items), 7)
        self.assertEqual(h.cycle()["applied"], {})

    def test_deleted_on_one_side_edited_on_other_is_conflict_not_delete(self):
        h = self.synced(a={"/a/1.ics": simple("e1"), "/a/2.ics": simple("e2", start="20260302T100000Z")})
        h.s.propagate_deletes = True
        h.a.remove_raw("/a/1.ics")
        hb = next(hr for hr, (_, d) in h.b.items.items() if b"UID:e1" in d)
        h.b.put_raw(hb, h.b.items[hb][1].replace(b"SUMMARY:Meeting", b"SUMMARY:Edited"))
        r = h.cycle()
        self.assertEqual(r["applied"], {})
        self.assertEqual(r["findings"], {"conflict_deleted_vs_edited": 1})
        self.assertEqual(len(h.b.items), 2)

    def test_mass_deletion_blocked(self):
        h = self.synced(a={f"/a/{i}.ics": simple(f"e{i}", start=f"2026030{i + 1}T100000Z") for i in range(8)})
        h.s.propagate_deletes = True
        for i in range(5):
            h.a.remove_raw(f"/a/{i}.ics")
        with self.assertRaises(eng.EngineBlocked):
            h.cycle()
        self.assertEqual(len(h.b.items), 8)

    def test_empty_side_is_outage_not_mass_delete(self):
        h = self.synced(a={"/a/1.ics": simple("e1"), "/a/2.ics": simple("e2", start="20260302T100000Z")})
        h.s.propagate_deletes = True
        for hr in h.a.hrefs():
            h.a.remove_raw(hr)
        with self.assertRaises(eng.EngineBlocked) as cm:
            h.cycle()
        self.assertIn("ZERO", str(cm.exception))
        self.assertEqual(len(h.b.items), 2)

    def test_offline_outage_writes_nothing_and_recovers(self):
        h = Harness(self, a={"/a/1.ics": simple("a1")}, b={"/b/1.ics": simple("b1", start="20260302T100000Z")})
        h.approve()
        h.b.fail["list_items"] = StoreError("down")
        with self.assertRaises(StoreError):
            h.cycle()
        self.assertEqual(h.a.writes() + h.b.writes(), [])
        self.assertFalse(h.state()["links"])
        self.assertEqual(h.cycle()["applied"], {"create_a": 1, "create_b": 1})

    def test_crash_midway_then_restart_no_duplicates(self):
        a = {f"/a/{i}.ics": simple(f"e{i}", start=f"2026030{i + 1}T100000Z") for i in range(6)}
        h = Harness(self, a=a)
        h.approve()
        h.b.fail_after_writes = 3
        with self.assertRaises(StoreError):
            h.cycle()
        self.assertEqual(len(h.b.items), 3)
        # restart: fresh process state is read from disk only
        h.b.fail_after_writes = None
        r = h.cycle()
        self.assertEqual(r["applied"], {"create_b": 3})
        self.assertEqual(uids(h.b), sorted(f"e{i}" for i in range(6)))
        self.assertEqual(h.cycle()["applied"], {})

    def test_crash_after_put_before_state_save_recovers_via_uid_adoption(self):
        h = Harness(self, a={"/a/1.ics": simple("e1")})
        h.approve()
        h.b.put_raw("/cal/orphan.ics", simple("e1"))           # PUT happened, state never saved
        r = h.cycle()
        self.assertEqual(r["applied"], {})
        self.assertEqual(len(h.b.items), 1)

    def test_caps_limit_ops_per_run(self):
        a = {f"/a/{i}.ics": simple(f"e{i}", start=f"2026030{i + 1}T100000Z") for i in range(6)}
        h = Harness(self, a=a)
        h.s.max_creates_per_run = 2
        h.approve()
        self.assertEqual(h.cycle()["applied"], {"create_b": 2})
        self.assertFalse(h.state()["initialized"]["b"])
        h.cycle()
        h.cycle()
        self.assertTrue(h.state()["initialized"]["b"])
        self.assertEqual(len(h.b.items), 6)

    def test_provider_rejecting_one_item_does_not_block_the_rest(self):
        a = {f"/a/{i}.ics": simple(f"e{i}", start=f"2026030{i + 1}T100000Z") for i in range(4)}
        h = Harness(self, a=a)
        h.approve()
        orig = h.b.create
        def picky(data):
            if b"UID:e1" in data:
                raise StoreError("bad request", transient=False, status=400)
            return orig(data)
        h.b.create = picky
        r = h.cycle()
        self.assertEqual(r["applied"], {"create_b": 3})
        self.assertEqual(r["skipped"], {"write_rejected": 1})
        self.assertEqual(r["rejected_ops"], 1)
        r = h.cycle()                                          # second rejection
        self.assertEqual(r["skipped"], {"write_rejected": 1})
        r = h.cycle()                                          # then it is left alone (no endless retries)
        self.assertEqual(r["skipped"], {"rejected_by_provider_twice": 1})
        self.assertEqual(uids(h.b), ["e0", "e2", "e3"])

    def test_auth_and_ratelimit_errors_abort_the_run(self):
        from calsync.store import AuthError, RateLimited
        for exc in (AuthError("invalid_grant"), RateLimited("429")):
            h = Harness(self, a={"/a/1.ics": simple("e1")})
            h.approve()
            h.b.fail["create"] = exc
            with self.assertRaises(type(exc)):
                h.cycle()

    def test_precondition_failure_skips_and_replans(self):
        h = self.synced(a={"/a/1.ics": simple("e1", "Old")})
        h.a.put_raw(h.a.hrefs()[0], simple("e1", "New"))
        orig = h.b.update
        def racing(href, etag, data):
            h.b.put_raw(href, h.b.datas()[0])      # concurrent edit bumps etag
            return orig(href, etag, data)
        h.b.update = racing
        r = h.cycle()
        self.assertEqual(r["skipped"], {"precondition_failed_replan": 1})
        h.b.update = orig
        r = h.cycle()                              # now B changed (etag only; same content): update goes through
        self.assertEqual(r["applied"], {"update_b": 1})

    def test_verify_after_write_blocks_pair_when_provider_mangles(self):
        def mangle(d):
            return d.replace(b"SUMMARY:Meeting", b"SUMMARY:Meeting (changed by provider)")
        h = Harness(self, a={"/a/1.ics": simple("e1")}, mutate_b=mangle)
        h.approve()
        with self.assertRaises(eng.VerifyFailed):
            h.cycle()
        self.assertTrue(syncrun.block_marker(h.s, h.p).exists())
        with self.assertRaises(syncrun.Blocked):
            h.cycle()
        self.assertEqual(len(h.b.writes()), 1)

    def test_event_turning_into_guest_event_freezes_link(self):
        h = self.synced(a={"/a/1.ics": simple("e1")})
        inv = ics(vevent("e1", organizer="boss@corp.example", attendees=[(ME, "NEEDS-ACTION")]))
        h.a.put_raw(h.a.hrefs()[0], inv)
        r = h.cycle()
        self.assertEqual(r["applied"], {})
        self.assertEqual(r["findings"], {"frozen_link": 1})

    def test_guest_events_never_written_in_any_cycle(self):
        guest = ics(vevent("g1", "Hotel", organizer="hotel@hotel.example", attendees=[(ME, "NEEDS-ACTION")]))
        h = Harness(self, a={"/a/g.ics": guest, "/a/n.ics": simple("n1")}, b={"/b/g.ics": guest.replace(b"g1", b"g2")})
        before_a, before_b = dict(h.a.items), dict(h.b.items)
        h.approve()
        h.cycle()
        h.cycle()
        for href, val in before_a.items():
            self.assertEqual(h.a.items[href], val)
        for href, val in before_b.items():
            self.assertEqual(h.b.items[href], val)
        self.assertEqual([x for x in h.b.writes() if b"Hotel" in h.b.items[x[1]][1]], [])

    def test_birthdays_never_copied_and_not_duplicated(self):
        bd = lambda uid, n: ics(vevent(uid, n, start="19900505", end="19900506", allday=True, rrule="FREQ=YEARLY"))
        h = Harness(self, a={"/a/1.ics": bd("i1", "Alex"), "/a/2.ics": simple("n1")}, b={"/b/1.ics": bd("g1@google.com", "Alex"),
                                                                                           "/b/2.ics": bd("g2@google.com", "Marie")})
        h.approve()
        h.cycle()
        self.assertEqual(sorted(uids(h.a)), ["i1", "n1"])
        self.assertEqual(sorted(uids(h.b)), ["g1@google.com", "g2@google.com", "n1"])

    def test_recurring_series_with_overrides_moves_as_one_unit(self):
        series = ics(
            vevent("s1", "Weekly", "20260302T090000", "20260302T100000", tzid="Europe/Vienna", rrule="FREQ=WEEKLY;COUNT=6",
                   exdates=["20260316T090000"]),
            vevent("s1", "Weekly (moved)", "20260309T140000", "20260309T150000", tzid="Europe/Vienna", rid="20260309T090000"),
            vevent("s1", "Weekly", "20260323T090000", "20260323T100000", tzid="Europe/Vienna", rid="20260323T090000", status="CANCELLED"),
            tz=True)
        h = Harness(self, a={"/a/s.ics": series})
        h.approve()
        h.cycle()
        out = h.b.datas()[0]
        r = im.parse_resource(out)
        self.assertEqual(len(r.comps), 3)
        self.assertEqual(len(r.overrides), 1 + 1)
        self.assertIn(b"EXDATE;TZID=Europe/Vienna:20260316T090000", out)
        self.assertIn(b"STATUS:CANCELLED", out)
        self.assertEqual(im.core_fp(r), im.core_fp(im.parse_resource(series)))
        # editing only an override on the other side propagates the whole series back
        h.b.put_raw(h.b.hrefs()[0], out.replace(b"Weekly (moved)", b"Weekly (moved again)"))
        self.assertEqual(h.cycle()["applied"], {"update_a": 1})
        self.assertIn(b"Weekly (moved again)", h.a.datas()[0])
        self.assertEqual(im.parse_resource(h.a.datas()[0]).uid, "s1")

    def test_adopted_duplicate_updates_keep_each_sides_uid(self):
        h = Harness(self, a={"/a/1.ics": simple("uuid-1", "Arzt")}, b={"/b/1.ics": simple("x@google.com", "Arzt")})
        h.approve()
        h.cycle()
        self.assertEqual((len(h.a.items), len(h.b.items)), (1, 1))
        h.a.put_raw("/a/1.ics", simple("uuid-1", "Arzt neu", start="20260301T100000Z"))
        self.assertEqual(h.cycle()["applied"], {"update_b": 1})
        self.assertEqual(uids(h.b), ["x@google.com"])
        self.assertIn(b"Arzt neu", h.b.datas()[0])
        self.assertEqual(h.cycle()["applied"], {})

    def test_link_decision_adopts_reworded_pair(self):
        h = Harness(self, a={"/a/1.ics": simple("u1", "Alex Geburtstagsfeier")},
                    b={"/b/1.ics": simple("u2", "Geburtstagsfeier Alex 18 Uhr", end="20260301T123000Z")})
        pl = h.plan()
        ra, rb = next(iter(pl.sides["a"].values())), next(iter(pl.sides["b"].values()))
        write_json(h.s.decisions / "cal.json", [{"action": "link", "a": ra.ident, "b": rb.ident, "fp": [ra.full[:16], rb.full[:16]]}])
        self.assertEqual(h.plan().stats["adopt"], {"decision": 1})

    def test_copy_decision_overrides_suspect(self):
        h = Harness(self, a={"/a/1.ics": simple("u1", "Alex Geburtstagsfeier")},
                    b={"/b/1.ics": simple("u2", "Geburtstagsfeier Alex 18 Uhr", end="20260301T123000Z")})
        ra = next(iter(h.plan().sides["a"].values()))
        write_json(h.s.decisions / "cal.json", [{"action": "copy", "side": "a", "id": ra.ident, "fp": [ra.full[:16]]}])
        self.assertEqual(h.plan().stats["ops"], {"create_b": 1})

    def test_ignore_decision_exempts_item(self):
        h = Harness(self, a={"/a/1.ics": simple("u1")})
        ra = next(iter(h.plan().sides["a"].values()))
        write_json(h.s.decisions / "cal.json", [{"action": "ignore", "id": ra.ident}])
        self.assertEqual(h.plan().ops, [])

    def test_audit_is_read_only_and_report_has_no_titles(self):
        secret = "Strictly-Confidential-Title"
        h = Harness(self, mode="mirror", a={"/a/1.ics": simple("a1", secret)})
        h.a.read_only = h.b.read_only = True
        rep = syncrun.audit_pair(h.s, h.p, h.stores, detail=False)
        self.assertEqual(h.a.writes() + h.b.writes(), [])
        txt = (h.s.reports / rep["report"]).read_text()
        self.assertNotIn(secret, txt)
        rep = syncrun.audit_pair(h.s, h.p, h.stores, detail=True)
        self.assertIn(secret, (h.s.reports / rep["detail_report"]).read_text())
        self.assertEqual((h.s.reports / rep["detail_report"]).stat().st_mode & 0o777, 0o600)

    def test_mirror_mode_run_refuses_to_write(self):
        h = Harness(self, mode="mirror", a={"/a/1.ics": simple("a1")})
        with self.assertRaises(syncrun.Blocked):
            h.cycle()
        self.assertEqual(h.b.writes(), [])


if __name__ == "__main__":
    unittest.main()
