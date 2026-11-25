"""Tests for calsync.reconcile. Synthetic fixtures only (fake people, no real data)."""
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

# python -I ignores PYTHONPATH, so make the src layout importable explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from calsync import ics, reconcile

ME = "me@example.invalid"


def vevent(uid, summary, dtstart="DTSTART:20260115T100000Z", dtend="DTEND:20260115T110000Z", extra=()):
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{summary}", dtstart]
    if dtend:
        lines.append(dtend)
    lines += list(extra)
    lines.append("END:VEVENT")
    return "\r\n".join(lines) + "\r\n"


def cal(*events):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def snapshot(d: Path):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir())}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.a = self.root / "icloud"
        self.b = self.root / "google"
        self.a.mkdir()
        self.b.mkdir()

    def put(self, side, name, *events):
        (side / f"{name}.ics").write_bytes(cal(*events).encode())

    def build(self, **kw):
        kw.setdefault("self_addresses", [ME])
        return reconcile.build_manifest(self.a, self.b, **kw)

    def entry(self, m, eid_part):
        found = [e for e in m["entries"] if eid_part in e["id"]]
        self.assertEqual(len(found), 1, [e["id"] for e in m["entries"]])
        return found[0]


class ClassificationTests(Base):
    def test_identical(self):
        ev = vevent("u1@example.invalid", "Dentist Alice Example")
        self.put(self.a, "u1", ev)
        self.put(self.b, "u1", ev.replace("Dentist", "Dentist"))
        m = self.build()
        e = self.entry(m, "u1@")
        self.assertEqual(e["classification"], "identical")
        self.assertEqual(e["proposed_action"], "skip_identical")
        self.assertNotIn("decision", e)
        self.assertEqual(m["blocking_manual_review"], 0)

    def test_uid_match_diff_lists_fields(self):
        self.put(self.a, "u1", vevent("u1@example.invalid", "Lunch Bob Sample"))
        self.put(self.b, "u1", vevent("u1@example.invalid", "Lunch Bob Sample!", dtend="DTEND:20260115T113000Z"))
        e = self.entry(self.build(), "u1@")
        self.assertEqual(e["classification"], "uid_match_diff")
        self.assertEqual(e["differing_fields"], ["dtend", "summary"])
        self.assertEqual(e["proposed_action"], "manual_review")
        self.assertIsNone(e["decision"])

    def test_dup_candidate_different_uid_same_start_and_title(self):
        self.put(self.a, "x", vevent("a1@example.invalid", "Team   Sync"))
        self.put(self.b, "y", vevent("b1@example.invalid", "team sync"))
        m = self.build()
        self.assertEqual(len(m["entries"]), 1)
        e = m["entries"][0]
        self.assertEqual(e["classification"], "dup_candidate")
        self.assertEqual(e["proposed_action"], "manual_review")

    def test_dup_candidate_across_timezone_representation(self):
        self.put(self.a, "x", vevent("a1@example.invalid", "Standup",
                                     dtstart="DTSTART;TZID=Europe/Vienna:20260115T110000", dtend=""))
        self.put(self.b, "y", vevent("b1@example.invalid", "Standup", dtend=""))  # 10:00Z == 11:00 Vienna
        e = self.build()["entries"][0]
        self.assertEqual(e["classification"], "dup_candidate")

    def test_time_match_title_mismatch(self):
        self.put(self.a, "x", vevent("a1@example.invalid", "Yoga"))
        self.put(self.b, "y", vevent("b1@example.invalid", "Pilates"))
        m = self.build()
        self.assertEqual(len(m["entries"]), 1)
        e = m["entries"][0]
        self.assertEqual(e["classification"], "time_match_title_mismatch")
        self.assertEqual(e["proposed_action"], "manual_review")

    def test_only_a_only_b_copy(self):
        self.put(self.a, "x", vevent("a1@example.invalid", "Only A", dtstart="DTSTART:20260201T100000Z"))
        self.put(self.b, "y", vevent("b1@example.invalid", "Only B", dtstart="DTSTART:20260301T100000Z"))
        m = self.build()
        self.assertEqual(self.entry(m, "a1@")["classification"], "only_a")
        self.assertEqual(self.entry(m, "a1@")["proposed_action"], "copy_a_to_b")
        self.assertEqual(self.entry(m, "b1@")["classification"], "only_b")
        self.assertEqual(self.entry(m, "b1@")["proposed_action"], "copy_b_to_a")
        self.assertEqual(m["blocking_manual_review"], 0)

    def test_recurring_series_compare_rrule_exdate_overrides(self):
        base = ["RRULE:FREQ=WEEKLY;BYDAY=MO"]
        self.put(self.a, "s", vevent("s1@example.invalid", "Weekly", extra=base + ["EXDATE:20260126T100000Z"]))
        self.put(self.b, "s", vevent("s1@example.invalid", "Weekly", extra=["RRULE:BYDAY=MO;FREQ=WEEKLY"]))
        e = self.entry(self.build(), "s1@")
        self.assertTrue(e["recurring"])
        self.assertEqual(e["classification"], "uid_match_diff")
        self.assertEqual(e["differing_fields"], ["exdates"])

    def test_recurring_override_only_one_side_blocks(self):
        master = vevent("s1@example.invalid", "Weekly", extra=["RRULE:FREQ=WEEKLY"])
        ovr = vevent("s1@example.invalid", "Weekly moved", dtstart="DTSTART:20260122T100000Z",
                     extra=["RECURRENCE-ID:20260122T100000Z"])
        self.put(self.a, "s", master, ovr)
        self.put(self.b, "s", master)
        m = self.build()
        masters = [e for e in m["entries"] if e["id"].endswith("|")]
        self.assertEqual(masters[0]["differing_fields"], ["overrides"])
        o = [e for e in m["entries"] if "20260122" in e["id"]][0]
        self.assertEqual(o["classification"], "only_a")
        self.assertIn("series_partial", o["flags"])
        self.assertEqual(o["proposed_action"], "manual_review")

    def test_birthday_like_series_and_helper(self):
        bday = vevent("bd1@example.invalid", "Alice Example birthday",
                      dtstart="DTSTART;VALUE=DATE:20000302", dtend="DTEND;VALUE=DATE:20000303",
                      extra=["RRULE:FREQ=YEARLY"])
        weekly = vevent("w@example.invalid", "Weekly", extra=["RRULE:FREQ=WEEKLY"])
        self.put(self.a, "bd", bday)
        self.put(self.a, "w", weekly)
        m = self.build()
        e = self.entry(m, "bd1@")
        self.assertTrue(e["birthday_like"])
        self.assertIn("birthday", e["note"])
        self.assertTrue(e["recurring"])
        self.assertEqual(m["summary"]["birthday_series_a"], 1)
        got = reconcile.birthday_series(ics.read_dir(self.a))
        self.assertEqual([x.uid for x in got], ["bd1@example.invalid"])

    def test_duplicate_uid_within_side_forces_review(self):
        self.put(self.a, "x1", vevent("d@example.invalid", "Dup"))
        self.put(self.a, "x2", vevent("d@example.invalid", "Dup"))
        e = self.entry(self.build(), "d@")
        self.assertIn("multiple_events_a", e["flags"])
        self.assertEqual(e["proposed_action"], "manual_review")


class GuestTests(Base):
    guests = ["ORGANIZER:mailto:carol@example.invalid",
              f"ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:{ME}",
              "ATTENDEE;PARTSTAT=ACCEPTED:mailto:carol@example.invalid"]

    def test_guests_only_a_is_manual_review(self):
        self.put(self.a, "g", vevent("g1@example.invalid", "Offsite", extra=self.guests))
        e = self.entry(self.build(), "g1@")
        self.assertEqual(e["classification"], "only_a")
        self.assertTrue(e["has_guests"])
        self.assertTrue(e["invited_not_organizer"])
        self.assertEqual(e["proposed_action"], "manual_review")
        gs = e["guest_status"]["a"]
        self.assertEqual(gs["self_partstat"], "NEEDS-ACTION")
        self.assertEqual(gs["by_partstat"], {"ACCEPTED": 1, "NEEDS-ACTION": 1})

    def test_identical_with_guests_is_skip_and_own_event_not_invited(self):
        mine = [f"ORGANIZER:mailto:{ME}", f"ATTENDEE;PARTSTAT=ACCEPTED:mailto:{ME}",
                "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:dave@example.invalid"]
        ev = vevent("g2@example.invalid", "My meeting", extra=mine)
        self.put(self.a, "g", ev)
        self.put(self.b, "g", ev)
        e = self.entry(self.build(), "g2@")
        self.assertTrue(e["has_guests"])
        self.assertFalse(e["invited_not_organizer"])
        self.assertEqual(e["proposed_action"], "skip_identical")

    def test_guests_never_autoresolved_in_any_classification(self):
        self.put(self.a, "g", vevent("g1@example.invalid", "Guesty", extra=self.guests))
        self.put(self.b, "g", vevent("g1@example.invalid", "Guesty changed", extra=self.guests))
        self.put(self.b, "o", vevent("o1@example.invalid", "Other", dtstart="DTSTART:20260909T100000Z",
                                     extra=self.guests))
        m = self.build()
        for e in m["entries"]:
            self.assertEqual(e["proposed_action"], "manual_review", e["id"])


class ApprovalTests(Base):
    def manifest_file(self):
        self.put(self.a, "g", vevent("g1@example.invalid", "Offsite", extra=GuestTests.guests))
        self.put(self.a, "c", vevent("c1@example.invalid", "Plain", dtstart="DTSTART:20260501T100000Z"))
        m = self.build()
        p = self.root / "manifest.json"
        reconcile.write_manifest(p, m)
        return p

    def set_decisions(self, p, decision="skip"):
        m = reconcile.load_manifest(p)
        for e in m["entries"]:
            if e["proposed_action"] == "manual_review":
                e["decision"] = decision
        reconcile.write_manifest(p, m)

    def test_approval_refused_until_decisions_made(self):
        p = self.manifest_file()
        self.assertEqual(reconcile.load_manifest(p)["blocking_manual_review"], 1)
        with self.assertRaises(reconcile.ApprovalError):
            reconcile.approve_manifest(p, "Tester", "nope")
        self.assertFalse(reconcile.verify_approval(p))
        self.assertNotIn("approval", reconcile.load_manifest(p))
        self.set_decisions(p, "bogus")
        with self.assertRaises(reconcile.ApprovalError):
            reconcile.approve_manifest(p, "Tester", "nope")
        self.set_decisions(p, "keep_both")
        reconcile.approve_manifest(p, "Tester", "reviewed")
        self.assertTrue(reconcile.verify_approval(p))
        ap = reconcile.load_manifest(p)["approval"]
        self.assertEqual(ap["approver"], "Tester")
        self.assertEqual(ap["manifest_sha256"], reconcile.load_manifest(p)["manifest_sha256"])

    def test_decision_must_be_possible(self):
        p = self.manifest_file()
        self.set_decisions(p, "copy_b_to_a")  # entry only exists on side a
        with self.assertRaises(reconcile.ApprovalError):
            reconcile.approve_manifest(p, "Tester", "x")

    def test_edited_proposed_action_refused(self):
        p = self.manifest_file()
        m = reconcile.load_manifest(p)
        for e in m["entries"]:
            if e["proposed_action"] == "manual_review":
                e["proposed_action"] = "copy_a_to_b"
        reconcile.write_manifest(p, m)
        with self.assertRaises(reconcile.ApprovalError):
            reconcile.approve_manifest(p, "Tester", "x")

    def test_no_blocking_entries_approves_directly(self):
        self.put(self.a, "c", vevent("c1@example.invalid", "Plain"))
        p = self.root / "m.json"
        reconcile.write_manifest(p, self.build())
        reconcile.approve_manifest(p, "Tester", "ok")
        self.assertTrue(reconcile.verify_approval(p))

    def test_approval_invalidated_by_edit(self):
        p = self.manifest_file()
        self.set_decisions(p, "skip")
        reconcile.approve_manifest(p, "Tester", "ok")
        self.assertTrue(reconcile.verify_approval(p))
        m = reconcile.load_manifest(p)
        m["entries"][0]["decision"] = "keep_both" if m["entries"][0].get("decision") != "keep_both" else "skip"
        if "decision" not in m["entries"][0]:
            m["entries"][0]["classification"] = "only_b"
        reconcile.write_manifest(p, m)
        self.assertFalse(reconcile.verify_approval(p))

    def test_approval_invalidated_by_forged_hash_only(self):
        p = self.manifest_file()
        self.set_decisions(p, "skip")
        reconcile.approve_manifest(p, "Tester", "ok")
        m = reconcile.load_manifest(p)
        m["entries"][0]["summary_tamper"] = "x"
        m["approval"]["manifest_sha256"] = "0" * 64
        reconcile.write_manifest(p, m)
        self.assertFalse(reconcile.verify_approval(p))

    def test_generated_at_and_approval_excluded_from_hash(self):
        m = self.build(now="2020-01-01T00:00:00Z")
        h = reconcile.compute_hash(m)
        m["generated_at"] = "2030-01-01T00:00:00Z"
        m["approval"] = {"x": 1}
        self.assertEqual(reconcile.compute_hash(m), h)
        self.assertEqual(m["manifest_sha256"], h)


class SafetyTests(Base):
    def populate(self):
        self.put(self.a, "1", vevent("u1@example.invalid", "Same"))
        self.put(self.b, "1", vevent("u1@example.invalid", "Same"))
        self.put(self.a, "2", vevent("u2@example.invalid", "A only", dtstart="DTSTART:20260601T100000Z"))
        self.put(self.b, "3", vevent("u3@example.invalid", "Guests", dtstart="DTSTART:20260701T100000Z",
                                     extra=GuestTests.guests))

    def test_inputs_untouched(self):
        self.populate()
        before = (snapshot(self.a), snapshot(self.b))
        names = (sorted(p.name for p in self.a.iterdir()), sorted(p.name for p in self.b.iterdir()))
        m = self.build()
        p = self.root / "out" / "m.json"
        p.parent.mkdir()
        reconcile.write_manifest(p, m)
        reconcile.load_manifest(p)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reconcile.main(["summary", str(p)]), 0)
        self.assertEqual((snapshot(self.a), snapshot(self.b)), before)
        self.assertEqual((sorted(q.name for q in self.a.iterdir()), sorted(q.name for q in self.b.iterdir())), names)

    def test_determinism(self):
        self.populate()
        m1 = self.build(now="2026-01-01T00:00:00Z")
        m2 = self.build(now="2027-06-06T06:06:06Z")
        self.assertEqual(m1["manifest_sha256"], m2["manifest_sha256"])
        self.assertEqual(reconcile.compute_hash(m1), reconcile.compute_hash(m2))
        body = lambda m: {k: v for k, v in m.items() if k != "generated_at"}
        self.assertEqual(json.dumps(body(m1), sort_keys=True), json.dumps(body(m2), sort_keys=True))
        # file creation order must not matter
        other = Path(tempfile.mkdtemp(dir=self.root))
        for f in sorted(self.a.iterdir(), reverse=True):
            (other / f.name).write_bytes(f.read_bytes())
        m3 = reconcile.build_manifest(other, self.b, self_addresses=[ME])
        self.assertEqual(m3["manifest_sha256"], m1["manifest_sha256"])

    def test_content_change_changes_hash(self):
        self.populate()
        h1 = self.build()["manifest_sha256"]
        self.put(self.a, "2", vevent("u2@example.invalid", "A only edited", dtstart="DTSTART:20260601T100000Z"))
        self.assertNotEqual(h1, self.build()["manifest_sha256"])

    def test_cli_flow(self):
        self.populate()
        out = self.root / "m.json"
        before = (snapshot(self.a), snapshot(self.b))
        run = lambda *a: self._run(list(a))
        self.assertEqual(run("build", str(self.a), str(self.b), "-o", str(out), "--self-address", ME)[0], 0)
        # refuse overwrite and refuse output inside an input dir
        self.assertEqual(run("build", str(self.a), str(self.b), "-o", str(out))[0], 2)
        self.assertEqual(run("build", str(self.a), str(self.b), "-o", str(self.a / "m.json"))[0], 2)
        self.assertFalse((self.a / "m.json").exists())
        self.assertEqual(run("verify", str(out))[0], 1)
        self.assertEqual(run("approve", str(out), "--by", "T", "--note", "n")[0], 1)  # guest entry undecided
        m = reconcile.load_manifest(out)
        for e in m["entries"]:
            if e["proposed_action"] == "manual_review":
                e["decision"] = "copy_b_to_a"
        reconcile.write_manifest(out, m)
        self.assertEqual(run("approve", str(out), "--by", "T", "--note", "n")[0], 0)
        self.assertEqual(run("verify", str(out))[0], 0)
        code, text = run("summary", str(out))
        self.assertEqual(code, 0)
        self.assertIn("approval: VALID", text)
        self.assertEqual((snapshot(self.a), snapshot(self.b)), before)

    def _run(self, argv):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = reconcile.main(argv)
        return code, buf.getvalue()

    def test_missing_directory(self):
        with self.assertRaises(NotADirectoryError):
            reconcile.build_manifest(self.root / "nope", self.b)


if __name__ == "__main__":
    unittest.main()
