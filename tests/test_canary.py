"""Canary selection + filtered plan approval: the first writes of a direction must be exactly the
operator-approved handful, never "whatever sorts first" out of hundreds of creates."""
import datetime
import unittest

from support import ME, MemoryStore, ics, simple, vevent
from test_engine import Harness

from calsync import canary, engine as eng, icsmodel as im, syncrun
from calsync.util import Blocked, now


def day(n, hh=10):
    d = datetime.datetime.fromtimestamp(now(), datetime.timezone.utc) + datetime.timedelta(days=n)
    return d.strftime("%Y%m%d") + f"T{hh:02d}0000Z"


TODAY = datetime.datetime.fromtimestamp(now(), datetime.timezone.utc).date()


def res(data):
    r = im.parse_resource(data, "/x.ics", "e")
    r.kind, r.reasons = im.classify(r, frozenset({ME}))
    return r


def why(data):
    return canary.ineligible_reasons(res(data), TODAY)


class Eligibility(unittest.TestCase):
    def test_plain_future_timed_event_is_eligible(self):
        self.assertEqual(why(simple("ok", start=day(5))), [])

    def test_each_risky_feature_disqualifies(self):
        cases = {
            "recurrence": simple("r", start=day(5), rrule="FREQ=WEEKLY;COUNT=3"),
            "has_organizer_or_attendees": simple("o", start=day(5), organizer=ME, attendees=[(ME, "ACCEPTED")]),
            "all_day": simple("d", start=day(5)[:8], allday=True),
            "privacy_class": simple("p", start=day(5), extra=["CLASS:PRIVATE"]),
            "status_not_confirmed": simple("s", start=day(5), status="TENTATIVE"),
            "property_attach": simple("at", start=day(5), extra=["ATTACH:https://example.com/f"]),
            "property_url": simple("u", start=day(5), extra=["URL:https://example.com"]),
            "property_x-google-conference": None,
            "not_in_future": simple("past", start=day(-3)),
            "too_far_out": simple("far", start=day(400)),
            "span": ics(vevent("long", "Long", day(5), end=day(8))),
            "non_display_alarm": ics(vevent("em", "E", day(5), end=day(5, 11), alarm=False,
                                            extra=["BEGIN:VALARM", "ACTION:EMAIL", "TRIGGER:-PT15M", "END:VALARM"])),
        }
        for reason, data in cases.items():
            if data is None:
                continue
            with self.subTest(reason):
                self.assertIn(reason, why(data))

    def test_meeting_link_x_properties_and_missing_end_disqualify(self):
        for prop in ("X-MICROSOFT-SKYPETEAMSMEETINGURL:https://t.example", "X-MICROSOFT-ONLINEMEETINGCONFLINK:https://t.example",
                     "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC"):
            with self.subTest(prop):
                self.assertTrue(why(simple("m", start=day(5), extra=[prop])))
        noend = ics(vevent("ne", "No end", day(5), end=None, alarm=False))
        self.assertIn("no_explicit_end", why(noend))

    def test_conference_x_property_disqualifies(self):
        self.assertTrue(why(simple("c", start=day(5), extra=["X-GOOGLE-CONFERENCE:https://meet.example"])))

    def test_oneoff_birthday_titled_event_is_never_a_canary(self):
        for title in ("Onkel Stefan Geburtstag", "Anna's BIRTHDAY dinner", "Hochzeitstag"):
            with self.subTest(title):
                self.assertIn("special_title", why(simple("bd", title, start=day(20))))

    def test_tomorrow_is_too_close(self):
        self.assertIn("not_in_future", why(simple("t", start=day(1))))

    def test_override_series_and_exempt_kinds_are_never_candidates(self):
        series = ics(vevent("s", "S", day(5), end=day(5, 11), rrule="FREQ=DAILY;COUNT=3"),
                     vevent("s", "S", day(6, 12), end=day(6, 13), rid=day(6)))
        self.assertIn("not_single_component", why(series))
        guest = simple("g", start=day(5), organizer="boss@other.example", attendees=[(ME, "NEEDS-ACTION")])
        self.assertEqual(why(guest), ["kind_guest"])
        bday = simple("b", start="20260315", allday=True, rrule="FREQ=YEARLY")
        self.assertEqual(why(bday), ["kind_birthday"])

    def test_floating_and_unresolved_zone_are_not_instants(self):
        floating = ics(vevent("f", "F", day(5)[:-1].replace("T", "T"), end=day(5, 11)[:-1]))
        self.assertTrue(why(floating))


def why_past(data, since=60):
    return canary.ineligible_reasons(res(data), TODAY, since_days_ago=since)


class RecentPast(unittest.TestCase):
    def test_flag_is_opt_in_and_only_widens_the_date_window(self):
        old = simple("p", start=day(-20))
        self.assertIn("not_in_future", why(old))                      # default: never
        self.assertEqual(why_past(old), [])                           # opt-in: ok
        self.assertIn("not_in_future", why_past(simple("p2", start=day(-61))))      # outside the window
        self.assertIn("not_in_future", why_past(simple("p3", start=day(-1))))       # guard: yesterday still too close
        self.assertIn("not_in_future", why_past(simple("p4", start=day(0))))
        self.assertEqual(why_past(simple("p5", start=day(-2))), [])

    def test_ceiling_and_range(self):
        self.assertIn("since_days_ago_out_of_range", why_past(simple("c", start=day(-20)), since=canary.MAX_SINCE_DAYS + 1))
        self.assertIn("since_days_ago_out_of_range", why_past(simple("c", start=day(-20)), since=-5))

    def test_every_other_predicate_still_applies_to_recent_past_events(self):
        cases = {
            "recurrence": simple("r", start=day(-20), rrule="FREQ=WEEKLY;COUNT=3"),
            "has_organizer_or_attendees": simple("o", start=day(-20), organizer=ME, attendees=[(ME, "ACCEPTED")]),
            "all_day": simple("d", start=day(-20)[:8], allday=True),
            "property_url": simple("u", start=day(-20), extra=["URL:https://example.com"]),
            "property_x-apple-structured-location": simple("sl", start=day(-20), extra=["X-APPLE-STRUCTURED-LOCATION;VALUE=URI:geo:1,2"]),
            "special_title": simple("b", "Tante Anna Geburtstag", start=day(-20)),
            "privacy_class": simple("p", start=day(-20), extra=["CLASS:PRIVATE"]),
            "span": ics(vevent("long", "Long", day(-20), end=day(-17))),
        }
        for reason, data in cases.items():
            with self.subTest(reason):
                self.assertIn(reason, why_past(data))
        floating = ics(vevent("f", "F", "20260101T100000", end="20260101T110000"))
        self.assertTrue(why_past(floating))                              # floating: unsupported kind
        guest = simple("g", start=day(-20), organizer="boss@other.example", attendees=[(ME, "NEEDS-ACTION")])
        self.assertEqual(why_past(guest), ["kind_guest"])


def mixed_a(n_good=8):
    a = {f"/a/good{i}.ics": simple(f"good{i}", f"Dentist {i}", start=day(10 + i, 9 + (i % 5))) for i in range(n_good)}
    a["/a/rec.ics"] = simple("rec", "Weekly", start=day(3), rrule="FREQ=WEEKLY;COUNT=4")
    a["/a/near.ics"] = simple("near", "Soon", start=day(3))        # eligible, nearest -> picked first
    a["/a/att.ics"] = simple("att", "With others", start=day(4), organizer=ME, attendees=[(ME, "ACCEPTED"), ("x@y.example", "ACCEPTED")])
    a["/a/old.ics"] = simple("old", "History", start=day(-400))
    a["/a/priv.ics"] = simple("priv", "Private", start=day(5), extra=["CLASS:PRIVATE"])
    a["/a/guest.ics"] = simple("guest", "Invitation", start=day(6), organizer="boss@other.example", attendees=[(ME, "NEEDS-ACTION")])
    a["/a/bday.ics"] = simple("bday", "Birthday", start="20260315", allday=True, rrule="FREQ=YEARLY")
    return a


class CanaryPlan(unittest.TestCase):
    def harness(self, **kw):
        h = Harness(self, mode="seed", a=kw.pop("a", mixed_a()), b=kw.pop("b", {"/b/1.ics": simple("b1", start=day(20))}))
        h.s.max_creates_per_run = 5
        return h

    def test_canary_selects_only_eligible_soonest_first_and_is_deterministic(self):
        h = self.harness()
        picked = canary.pick(h.plan(), "b", TODAY, 5)
        self.assertEqual([t for _i, _s, t in picked][0], "Soon")
        titles = {t for _i, _s, t in picked}
        self.assertTrue(titles.isdisjoint({"Weekly", "With others", "History", "Private", "Invitation", "Birthday"}))
        self.assertEqual(picked, canary.pick(h.plan(), "b", TODAY, 5))

    def test_approved_canary_writes_exactly_the_selection_and_nothing_else(self):
        h = self.harness()
        rep = syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), canary=5)
        self.assertEqual(len(rep["selection"]), 5)
        syncrun.approve_plan(h.s, h.p, "t", "canary", rep["plan_file"]["b"], "b")
        out = h.cycle()
        self.assertEqual(out["applied"], {"create_b": 5})
        self.assertEqual(len(h.b.writes()), 5)
        self.assertFalse(h.a.writes())
        created = {im.item_id("a", u) for u in
                   (im.parse_resource(d).uid for d in h.b.datas()) if u != "b1"}
        self.assertEqual(created, set(rep["selection"]))
        self.assertFalse(out["initialized"]["b"])                       # 5 of many: direction stays uninitialised
        n = len(h.b.writes())
        for _ in range(3):                                              # further cycles: nothing more is created
            self.assertEqual(h.cycle()["applied"], {})
        self.assertEqual(len(h.b.writes()), n)
        by_uid = {im.parse_resource(d).uid for d in h.b.datas()}
        self.assertTrue({"rec", "att", "old", "priv", "guest", "bday"}.isdisjoint(by_uid))

    def test_selection_is_bound_by_sha_and_unapproved_ops_do_not_run(self):
        h = self.harness()
        rep = syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), canary=5)
        full = syncrun.audit_pair(h.s, h.p, h.stores)["plan_sha256"]
        with self.assertRaises(Blocked):
            syncrun.approve_plan(h.s, h.p, "t", "x", full, "b")          # sha of the full plan is not the stored one
        with self.assertRaises(Blocked):
            h.cycle()                                                   # no approval -> nothing runs
        self.assertFalse(h.b.writes())

    def test_not_enough_candidates_refuses_instead_of_weakening(self):
        h = self.harness(a={"/a/1.ics": simple("ok1", start=day(9)), "/a/2.ics": simple("rec", start=day(9), rrule="FREQ=DAILY;COUNT=2")})
        with self.assertRaises(Blocked):
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), canary=5)
        self.assertFalse(syncrun.plan_file(h.s, h.p, "b").exists())

    def test_only_rejects_unknown_exempt_and_multi_direction(self):
        h = self.harness()
        with self.assertRaises(Blocked):
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), only=["000000000000"])
        guest_id = im.item_id("a", "guest")
        with self.assertRaises(Blocked):
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), only=[guest_id])
        with self.assertRaises(Blocked):
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("a", "b"), canary=2)
        with self.assertRaises(Blocked):
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), only=[])
        self.assertFalse(syncrun.plan_file(h.s, h.p, "b").exists())

    def test_only_applies_the_same_safety_rules_as_canary(self):
        h = self.harness()
        for uid in ("rec", "att", "old", "priv"):
            with self.subTest(uid), self.assertRaises(Blocked):
                syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), only=[im.item_id("a", uid)])
        self.assertFalse(syncrun.plan_file(h.s, h.p, "b").exists())

    def test_canary_size_must_be_sane(self):
        h = self.harness()
        for n in (0, -1, -3, syncrun.MAX_CANARY + 1):
            with self.subTest(n), self.assertRaises(Blocked):
                syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), canary=n)
        with self.assertRaises(Blocked):
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), only=[im.item_id("a", f"x{i}") for i in range(syncrun.MAX_CANARY + 1)])
        self.assertFalse(syncrun.plan_file(h.s, h.p, "b").exists())

    def test_item_edited_after_planning_loses_its_approval(self):
        h = self.harness()
        rep = syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), canary=3)
        syncrun.approve_plan(h.s, h.p, "t", "x", rep["plan_file"]["b"], "b")
        victim = rep["selection"][0]
        href = next(hf for hf, d in ((hf, d) for hf, (_e, d) in h.a.items.items())
                    if im.item_id("a", im.parse_resource(d).uid) == victim)
        uid = im.parse_resource(h.a.items[href][1]).uid
        h.a.put_raw(href, simple(uid, "EDITED after approval", start=day(30), rrule="FREQ=WEEKLY;COUNT=9"))
        self.assertEqual(h.cycle()["applied"], {"create_b": 2})
        self.assertNotIn(uid, {im.parse_resource(d).uid for d in h.b.datas()})

    def past_harness(self):
        a = {f"/a/p{i}.ics": simple(f"past{i}", f"Besuch {i}", start=day(-5 - 7 * i, 9 + i)) for i in range(8)}
        a["/a/old.ics"] = simple("ancient", "Ancient", start=day(-300))
        a["/a/prec.ics"] = simple("prec", "Weekly past", start=day(-9), rrule="FREQ=WEEKLY;COUNT=2")
        a["/a/pguest.ics"] = simple("pguest", "Invite", start=day(-8), organizer="boss@other.example", attendees=[(ME, "NEEDS-ACTION")])
        a["/a/pbday.ics"] = simple("pbday", "Onkel Geburtstag", start=day(-7))
        return self.harness(a=a)

    def test_recent_past_canary_requires_opt_in_and_writes_exactly_five(self):
        h = self.past_harness()
        with self.assertRaises(Blocked):                                 # without the flag: 0 future candidates
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), canary=5)
        with self.assertRaises(Blocked):                                 # flag without a filtered plan
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), since_days_ago=60)
        rep = syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), canary=5, since_days_ago=60)
        self.assertEqual(len(rep["selection"]), 5)
        syncrun.approve_plan(h.s, h.p, "t", "recent-past canary", rep["plan_file"]["b"], "b")
        self.assertEqual(h.cycle()["applied"], {"create_b": 5})
        created = {im.parse_resource(d).uid for d in h.b.datas()} - {"b1"}
        self.assertEqual({im.item_id("a", u) for u in created}, set(rep["selection"]))
        self.assertTrue(created.isdisjoint({"ancient", "prec", "pguest", "pbday"}))
        n = len(h.b.writes())
        self.assertEqual(h.cycle()["applied"], {})                       # nothing else is approved
        self.assertEqual(len(h.b.writes()), n)
        self.assertFalse(h.a.writes())

    def test_recent_past_picks_most_recent_first_and_refuses_when_short(self):
        h = self.past_harness()
        picked = canary.pick(h.plan(), "b", TODAY, 3, since_days_ago=60)
        self.assertEqual([t for _i, _s, t in picked], ["Besuch 0", "Besuch 1", "Besuch 2"])
        with self.assertRaises(Blocked):
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), canary=5, since_days_ago=20)   # only 3 in window
        with self.assertRaises(Blocked):
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), canary=5, since_days_ago=canary.MAX_SINCE_DAYS + 1)

    def test_only_with_since_days_ago_still_rejects_unsafe_ids(self):
        h = self.past_harness()
        for uid in ("ancient", "prec", "pbday"):
            with self.subTest(uid), self.assertRaises(Blocked):
                syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), only=[im.item_id("a", uid)], since_days_ago=60)
        rep = syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), only=[im.item_id("a", "past1")], since_days_ago=60)
        self.assertEqual(rep["selection"], [im.item_id("a", "past1")])

    def test_only_accepts_an_explicit_selection(self):
        h = self.harness()
        ids = [im.item_id("a", "good0"), im.item_id("a", "good1")]
        rep = syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), only=ids)
        syncrun.approve_plan(h.s, h.p, "t", "x", rep["plan_file"]["b"], "b")
        self.assertEqual(h.cycle()["applied"], {"create_b": 2})

    def test_canary_never_writes_side_a_even_if_selected_for_a(self):
        h = self.harness(b={"/b/%d.ics" % i: simple(f"bb{i}", start=day(10 + i)) for i in range(6)})
        rep = syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("a",), canary=5)
        syncrun.approve_plan(h.s, h.p, "t", "x", rep["plan_file"]["a"], "a")
        with self.assertRaises(Blocked):                                # b has unapproved first-time ops: whole run refused
            h.cycle()
        self.assertFalse(h.a.writes() or h.b.writes())


def bwhy(data, selfs=frozenset({ME})):
    return canary.ineligible_reasons(res(data), TODAY, profile="batch", self_addresses=selfs)


def email_alarm(att=ME):
    lines = ["BEGIN:VALARM", "ACTION:EMAIL", "TRIGGER:-PT10M", "SUMMARY:x", "DESCRIPTION:x"]
    if att:
        lines.append(f"ATTENDEE:mailto:{att}")
    return lines + ["END:VALARM"]


def tzics(uid, start, end, **kw):
    return ics(vevent(uid, "Serie", start, end=end, tzid="Europe/Vienna", **kw), tz=True)


class BatchProfile(unittest.TestCase):
    """The wider whitelist for the bulk migration: history, all-day, finite recurrence, owner-only e-mail alarms."""

    def test_history_is_eligible_only_in_the_batch_profile(self):
        old = simple("h", start=day(-900))
        self.assertIn("not_in_future", why(old))
        self.assertEqual(bwhy(old), [])
        self.assertEqual(bwhy(simple("f", start=day(900))), [])           # no future ceiling either

    def test_all_day_events_single_and_multi_day(self):
        one = ics(vevent("d1", "Ganztag", "20200102", end="20200103", allday=True))
        self.assertIn("all_day", why(one))
        self.assertEqual(bwhy(one), [])
        three = ics(vevent("d3", "Urlaub", "20200102", end="20200105", allday=True, alarm=False))
        self.assertEqual(bwhy(three), [])
        too_long = ics(vevent("dl", "Sabbatical", "20200102", end="20200601", allday=True))
        self.assertEqual(bwhy(ics(vevent("dm", "Urlaub", "20200102", end="20200201", allday=True))), [])
        self.assertIn("span", bwhy(too_long))
        noend = ics(vevent("dn", "x", "20200102", end=None, allday=True))
        self.assertIn("no_explicit_end", bwhy(noend))

    def test_yearly_all_day_is_still_exempt_birthday(self):
        self.assertEqual(bwhy(simple("y", start="20200315", allday=True, rrule="FREQ=YEARLY")), ["kind_birthday"])

    def test_status_tentative_ok_cancelled_not(self):
        self.assertEqual(bwhy(simple("t", start=day(-30), status="TENTATIVE")), [])
        self.assertIn("status_not_confirmed", bwhy(simple("c", start=day(-30), status="CANCELLED")))

    def test_url_apple_location_and_travel_metadata_allowed_other_properties_not(self):
        for prop in ("URL:https://example.com/x", "URL;VALUE=URI:", "X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-TITLE=Home:geo:48.2,16.3",
                     "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC", "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT20M"):
            with self.subTest(prop):
                self.assertEqual(bwhy(simple("m", start=day(-30), extra=[prop])), [])
                self.assertTrue(why(simple("m", start=day(5), extra=[prop])))        # canary: still rejected
        for prop in ("URL:javascript:alert(1)", "URL:message://%3cabc@mail%3e", "ATTACH:https://example.com/f", "X-MICROSOFT-SKYPETEAMSMEETINGURL:https://t",
                     "X-GOOGLE-CONFERENCE:https://meet.example", "X-APPLE-CONFERENCE-URL:https://z", "GEO:1;2", "RELATED-TO:x",
                     "ORGANIZER:mailto:" + ME):
            with self.subTest(prop):
                self.assertTrue(bwhy(simple("n", start=day(-30), extra=[prop])))

    def test_email_alarms_only_when_they_address_the_owner(self):
        mine = ics(vevent("e1", "E", day(-30), end=day(-30, 11), alarm=False, extra=email_alarm(ME)))
        self.assertEqual(bwhy(mine), [])
        self.assertIn("non_display_alarm", why(ics(vevent("e1", "E", day(5), end=day(5, 11), alarm=False, extra=email_alarm(ME)))))
        other = ics(vevent("e2", "E", day(-30), end=day(-30, 11), alarm=False, extra=email_alarm("someone@else.example")))
        self.assertIn("email_alarm_to_others", bwhy(other))
        self.assertIn("email_alarm_to_others", bwhy(mine, selfs=frozenset()))       # unknown own addresses: fail closed
        folded = ics(vevent("e3", "E", day(-30), end=day(-30, 11), alarm=False,
                            extra=["BEGIN:VALARM", "ACTION:EMAIL", "TRIGGER:-PT10M", "ATTENDEE:mailto:someone@el", " se.example",
                                   "END:VALARM"]))
        self.assertIn("email_alarm_to_others", bwhy(folded))
        audio_proc = ics(vevent("e4", "E", day(-30), end=day(-30, 11), alarm=False,
                                extra=["BEGIN:VALARM", "ACTION:PROCEDURE", "TRIGGER:-PT1M", "END:VALARM"]))
        self.assertIn("non_display_alarm", bwhy(audio_proc))

    def test_only_finite_plain_recurrence(self):
        ok = tzics("r1", "20210906T080000", "20210906T090000", rrule="FREQ=DAILY;COUNT=5")
        self.assertEqual(bwhy(ok), [])
        self.assertIn("recurrence", why(tzics("r1", "20210906T080000", "20210906T090000", rrule="FREQ=DAILY;COUNT=5")))
        until = tzics("r2", "20130924T140500", "20130924T150500", rrule="FREQ=WEEKLY;UNTIL=20131126T150500Z")
        self.assertEqual(bwhy(until), [])
        for name, data in {
            "infinite": tzics("r3", "20210906T080000", "20210906T090000", rrule="FREQ=WEEKLY"),
            "yearly": tzics("r4", "20210906T080000", "20210906T090000", rrule="FREQ=YEARLY;COUNT=3"),
            "exdate": tzics("r5", "20210906T080000", "20210906T090000", rrule="FREQ=DAILY;COUNT=5", exdates=["20210907T080000"]),
            "utc": simple("r6", start="20210906T080000Z", rrule="FREQ=DAILY;COUNT=5"),
            "override": ics(vevent("r7", "S", "20210906T080000", end="20210906T090000", tzid="Europe/Vienna", rrule="FREQ=DAILY;COUNT=5"),
                            vevent("r7", "S", "20210907T100000", end="20210907T110000", tzid="Europe/Vienna", rid="20210907T080000"), tz=True),
            "allday": simple("r8", start="20210906", allday=True, rrule="FREQ=WEEKLY;COUNT=3"),
        }.items():
            with self.subTest(name):
                self.assertTrue(bwhy(data), name)

    def test_everything_else_stays_rejected_in_the_batch_profile(self):
        cases = {
            "has_organizer_or_attendees": simple("o", start=day(-30), organizer=ME, attendees=[(ME, "ACCEPTED")]),
            "privacy_class": simple("p", start=day(-30), extra=["CLASS:PRIVATE"]),
            "special_title": simple("b", "Tante Anna Geburtstag", start=day(-30)),
            "span": ics(vevent("long", "Long", day(-400), end=day(-20))),
        }
        for reason, data in cases.items():
            with self.subTest(reason):
                self.assertIn(reason, bwhy(data))
        self.assertEqual(bwhy(ics(vevent("fl", "F", "20200101T100000", end="20200101T110000"))), ["kind_unsupported"])   # floating
        self.assertEqual(bwhy(simple("g", start=day(-30), organizer="boss@other.example", attendees=[(ME, "NEEDS-ACTION")])), ["kind_guest"])
        self.assertEqual(bwhy(simple("g", start="20200101", allday=True, rrule="FREQ=YEARLY")), ["kind_birthday"])

    def test_conference_links_in_the_text_are_never_automatic_candidates(self):
        for text in ("----( Video Call )---- https://teams.microsoft.com/meet/123", "Join https://zoom.us/j/99",
                     "https://meet.google.com/abc-defg-hij", "Microsoft Teams meeting"):
            with self.subTest(text):
                d = ics(vevent("cf", "Call", day(-30), end=day(-30, 11), extra=[f"DESCRIPTION:{text}"]))
                self.assertIn("conference_link_in_text", bwhy(d))
                d5 = ics(vevent("cf", "Call", day(5), end=day(5, 11), extra=[f"DESCRIPTION:{text}"]))
                self.assertIn("conference_link_in_text", why(d5))
        loc = ics(vevent("cf2", "Call", day(-30), end=day(-30, 11), extra=["LOCATION:https://teams.microsoft.com/l/x"]))
        self.assertIn("conference_link_in_text", bwhy(loc))
        ok = ics(vevent("cf3", "Meeting", day(-30), end=day(-30, 11), extra=["DESCRIPTION:Bring the notes, room 4"]))
        self.assertEqual(bwhy(ok), [])

    def test_since_days_ago_is_a_canary_option_only_and_unknown_profile_is_rejected(self):
        r = res(simple("x", start=day(-30)))
        self.assertIn("since_days_ago_not_for_batch", canary.ineligible_reasons(r, TODAY, profile="batch", since_days_ago=60))
        self.assertEqual(canary.ineligible_reasons(r, TODAY, profile="nope"), ["unknown_profile"])


def history_a():
    a = {f"/a/h{i}.ics": simple(f"hist{i}", f"Termin {i}", start=day(-300 + i, 8 + i % 6)) for i in range(30)}
    a["/a/ad.ics"] = ics(vevent("allday1", "Ganztag", "20200102", end="20200103", allday=True))
    a["/a/rec.ics"] = tzics("fin", "20210906T080000", "20210906T090000", rrule="FREQ=DAILY;COUNT=3")
    a["/a/inf.ics"] = tzics("inf", "20210906T080000", "20210906T090000", rrule="FREQ=WEEKLY")
    a["/a/att.ics"] = simple("att", "Mit anderen", start=day(-50), organizer=ME, attendees=[(ME, "ACCEPTED"), ("x@y.example", "ACCEPTED")])
    a["/a/bday.ics"] = simple("bday", "Anna Geburtstag", start=day(-60))
    a["/a/guest.ics"] = simple("guest", "Einladung", start=day(-70), organizer="boss@other.example", attendees=[(ME, "NEEDS-ACTION")])
    a["/a/yearly.ics"] = simple("yearly", "Jahrestag-Serie", start="20200315", allday=True, rrule="FREQ=YEARLY")
    return a


class BatchPlans(unittest.TestCase):
    def harness(self, mode="seed", **kw):
        h = Harness(self, mode=mode, a=kw.pop("a", history_a()), b=kw.pop("b", {"/b/1.ics": simple("b1", start=day(20))}))
        h.s.max_creates_per_run = 25
        return h

    def plan_batch(self, h, n, direction="b"):
        return syncrun.audit_pair(h.s, h.p, h.stores, write_plan=(direction,), canary=n, profile="batch")

    def test_batch_of_history_writes_exactly_the_selection_and_nothing_else(self):
        h = self.harness()
        rep = self.plan_batch(h, 12)
        self.assertEqual(len(rep["selection"]), 12)
        syncrun.approve_plan(h.s, h.p, "t", "batch", rep["plan_file"]["b"], "b")
        out = h.cycle()
        self.assertEqual(out["applied"], {"create_b": 12})
        self.assertFalse(out["initialized"]["b"])
        written = {im.parse_resource(d).uid for d in h.b.datas()} - {"b1"}
        self.assertEqual({im.item_id("a", u) for u in written}, set(rep["selection"]))
        self.assertTrue(written.isdisjoint({"att", "bday", "guest", "yearly", "inf"}))
        self.assertFalse(h.a.writes())
        n = len(h.b.writes())
        self.assertEqual(h.cycle()["applied"], {})                         # no ping-pong, nothing else approved
        self.assertEqual(len(h.b.writes()), n)

    def test_second_batch_needs_its_own_approval(self):
        h = self.harness()
        r1 = self.plan_batch(h, 5)
        syncrun.approve_plan(h.s, h.p, "t", "b1", r1["plan_file"]["b"], "b")
        self.assertEqual(h.cycle()["applied"], {"create_b": 5})
        r2 = self.plan_batch(h, 5)
        self.assertFalse(set(r2["selection"]) & set(r1["selection"]))        # already linked: never planned again
        with self.assertRaises(Blocked):                                    # the new plan file replaced the approval
            h.cycle()
        syncrun.approve_plan(h.s, h.p, "t", "b2", r2["plan_file"]["b"], "b")
        self.assertEqual(h.cycle()["applied"], {"create_b": 5})

    def test_only_with_batch_profile_selects_all_day_and_finite_recurrence(self):
        h = self.harness()
        ids = [im.item_id("a", "allday1"), im.item_id("a", "fin")]
        rep = syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), only=ids, profile="batch")
        syncrun.approve_plan(h.s, h.p, "t", "x", rep["plan_file"]["b"], "b")
        self.assertEqual(h.cycle()["applied"], {"create_b": 2})
        for bad in ("inf", "att", "bday", "guest", "yearly"):
            with self.subTest(bad), self.assertRaises(Blocked):
                syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), only=[im.item_id("a", bad)], profile="batch")
        with self.assertRaises(Blocked):                                      # the strict canary profile still refuses history
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), only=[im.item_id("a", "hist3")])

    def test_batch_size_and_flag_combinations_are_limited(self):
        h = self.harness()
        for n in (0, -1, syncrun.MAX_BATCH + 1):
            with self.subTest(n), self.assertRaises(Blocked):
                self.plan_batch(h, n)
        with self.assertRaises(Blocked):
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), canary=5, since_days_ago=30, profile="batch")
        with self.assertRaises(Blocked):
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), profile="batch")          # profile without a filtered plan
        with self.assertRaises(Blocked):
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), canary=3, profile="loose")
        with self.assertRaises(Blocked):
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("a", "b"), canary=3, profile="batch")
        with self.assertRaises(Blocked):                                      # canary profile keeps its size limit of 10
            syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), canary=11, since_days_ago=90)
        self.assertEqual(len(self.plan_batch(h, syncrun.MAX_BATCH)["selection"]), syncrun.MAX_BATCH)

    def test_refuses_when_fewer_candidates_than_requested(self):
        h = self.harness(a={"/a/1.ics": simple("one", start=day(-9)), "/a/2.ics": simple("two", start=day(-9), organizer=ME,
                                                                                   attendees=[(ME, "ACCEPTED"), ("x@y.example", "ACCEPTED")])})
        with self.assertRaises(Blocked):
            self.plan_batch(h, 2)
        self.assertFalse(syncrun.plan_file(h.s, h.p, "b").exists())

    def test_only_writes_titles_to_the_detail_file_not_the_return_value_of_the_cli(self):
        h = self.harness()
        rep = syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), only=[im.item_id("a", "allday1")], profile="batch")
        self.assertIn("canary_detail", rep)
        import json
        doc = json.loads((h.s.reports / rep["canary_detail"]).read_text())
        self.assertEqual(doc["profile"], "batch")
        self.assertEqual([i["title"] for i in doc["items"]], ["Ganztag"])


class SeedA(unittest.TestCase):
    """seed_a = side A (iCloud) only, with its own approval; side B is never written and needs no approval."""

    def harness(self, mode="seed_a"):
        b = {f"/b/g{i}.ics": simple(f"g{i}", f"Google {i}", start=day(-100 + i)) for i in range(8)}
        h = Harness(self, mode=mode, a={"/a/1.ics": simple("ia", "Apple", start=day(-40))}, b=b)
        h.s.max_creates_per_run = 25
        return h

    def test_seed_a_writes_only_side_a_and_only_the_approved_batch(self):
        h = self.harness()
        rep = syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("a",), canary=4, profile="batch")
        syncrun.approve_plan(h.s, h.p, "t", "x", rep["plan_file"]["a"], "a")
        out = h.cycle()
        self.assertEqual(out["applied"], {"create_a": 4})
        self.assertFalse(h.b.writes())
        self.assertEqual(len(h.a.writes()), 4)
        self.assertFalse(out["initialized"]["a"])
        self.assertEqual(h.cycle()["applied"], {})
        self.assertEqual(len(h.a.writes()), 4)

    def test_seed_a_ignores_an_approved_b_plan(self):
        h = self.harness()
        rep = syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("b",), canary=1, profile="batch")
        syncrun.approve_plan(h.s, h.p, "t", "x", rep["plan_file"]["b"], "b")
        with self.assertRaises(Blocked):                  # direction a has first-time ops but no approved plan
            h.cycle()
        self.assertFalse(h.a.writes() or h.b.writes())

    def test_seed_still_never_writes_a_and_mirror_nothing(self):
        for mode in ("seed", "mirror"):
            h = self.harness(mode=mode)
            rep = syncrun.audit_pair(h.s, h.p, h.stores, write_plan=("a",), canary=2, profile="batch")
            syncrun.approve_plan(h.s, h.p, "t", "x", rep["plan_file"]["a"], "a")
            with self.assertRaises(Blocked):               # seed: b has unapproved first-time ops; mirror: gates closed
                h.cycle()
            self.assertFalse(h.a.writes() or h.b.writes(), mode)

    def test_seed_a_is_a_valid_mode_and_gated_like_the_others(self):
        from calsync import config
        self.assertIn("seed_a", config.MODES)
        h = self.harness()
        self.assertEqual(syncrun.gate_failures(h.s, h.p), [])
        h.s.write_enabled = False
        self.assertTrue(syncrun.gate_failures(h.s, h.p))
        h.s.write_enabled = True
        (h.s.state / "PAUSE").touch()
        self.assertTrue(syncrun.gate_failures(h.s, h.p))
        (h.s.state / "PAUSE").unlink()
        with self.assertRaises(Blocked):                 # no approval: refuses
            h.cycle()


if __name__ == "__main__":
    unittest.main()
