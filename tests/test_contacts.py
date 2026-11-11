import unittest
from datetime import date, datetime, timedelta, timezone

from isuite.contacts import (LeadDraft, add_or_merge, due_today, expire_stale, messages_sent,
                             record_outcome,
                             set_consent)
from isuite.db import connect
from isuite.followup import next_due, sg_today

D = date(2026, 11, 10)
NOW = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)


def NOW_AT(d):
    """10:00 SGT on date d, as an aware UTC time."""
    return datetime(d.year, d.month, d.day, 2, 0, tzinfo=timezone.utc)


class ContactsTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)
        for i, tg in ((1, 111), (2, 222)):
            self.conn.execute("INSERT INTO agents (id, telegram_id, status) VALUES (?,?,'team')", (i, tg))
        self.conn.commit()

    def add(self, agent, name, phone, consent="unknown", notes="", today=D):
        return add_or_merge(self.conn, agent, LeadDraft(name, phone, consent, notes), "photo", today, NOW)

    def row(self, cid):
        return self.conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()

    def test_add_then_contact_merges_same_phone(self):
        cid, created = self.add(1, "Mei Ling", "9123 4567")
        self.assertTrue(created)
        cid2, created2 = self.add(1, "Mei Ling Tan", "+65 9123-4567", notes="met at Tampines")
        self.assertEqual(cid, cid2)
        self.assertFalse(created2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 1)
        r = self.row(cid)
        self.assertIn("Tampines", r["notes"])
        self.assertEqual(r["name"], "Mei Ling")
        self.assertEqual(r["phone"], "+6591234567")
        self.assertEqual((r["stage"], r["captured_on"], r["next_due_on"]), ("new", "2026-11-10", "2026-11-10"))

    def test_merge_fills_empty_name_and_upgrades_consent_and_dedups_notes(self):
        cid, _ = self.add(1, "", "91234567", notes="a")
        self.add(1, "Mei", "91234567", consent="yes", notes="b")
        self.add(1, "Mei", "91234567", consent="yes", notes="b")
        r = self.row(cid)
        self.assertEqual((r["name"], r["consent"], r["notes"]), ("Mei", "yes", "a; b"))

    def test_merge_never_downgrades_no_consent(self):
        cid, _ = self.add(1, "A", "91234567", consent="no")
        self.add(1, "A", "91234567", consent="yes")
        self.assertEqual(self.row(cid)["consent"], "no")

    def test_merge_incoming_no_overrides_yes(self):
        cid, _ = self.add(1, "A", "91234567", consent="yes")
        self.add(1, "A", "91234567", consent="no")
        self.assertEqual(self.row(cid)["consent"], "no")
        self.add(1, "A", "91234567", consent="yes")
        self.assertEqual(self.row(cid)["consent"], "no")
        cid2, _ = self.add(1, "B", "92345678", consent="yes")
        self.add(1, "B", "92345678", consent="unknown")
        self.assertEqual(self.row(cid2)["consent"], "yes")

    def test_messaged_after_terminal_stage_does_not_resume_chase(self):
        for stage, phone in (("replied", "91111111"), ("booked", "92222222"),
                             ("closed", "93333333"), ("not_keen", "94444444")):
            cid, _ = self.add(1, stage, phone)
            record_outcome(self.conn, 1, cid, stage, D, NOW)
            n = self.conn.execute("SELECT COUNT(*) FROM activity WHERE contact_id=? AND kind='messaged'", (cid,)).fetchone()[0]
            record_outcome(self.conn, 1, cid, "messaged", D, NOW)
            r = self.row(cid)
            self.assertEqual((r["stage"], r["next_due_on"], r["chase_step"]), (stage, None, 0))
            self.assertNotIn(cid, [x["id"] for x in due_today(self.conn, 1, date(2027, 1, 1))])
            n2 = self.conn.execute("SELECT COUNT(*) FROM activity WHERE contact_id=? AND kind='messaged'", (cid,)).fetchone()[0]
            self.assertEqual(n2, n + 1)

    def test_messaged_after_stale_stays_stale(self):
        cid, _ = self.add(1, "A", "91234567")
        for day in (0, 3, 7, 14, 20):
            record_outcome(self.conn, 1, cid, "messaged", D + timedelta(days=day), NOW)
        r = self.row(cid)
        self.assertEqual((r["stage"], r["chase_step"], r["next_due_on"]), ("stale", 4, None))

    def test_phone_less_leads_coexist(self):
        a, ca = self.add(1, "X", "")
        b, cb = self.add(1, "Y", "abc")
        self.assertTrue(ca and cb)
        self.assertNotEqual(a, b)
        self.assertEqual(self.row(a)["phone_ok"], 0)
        self.assertIsNone(self.row(a)["phone"])

    def test_name_and_notes_redacted(self):
        cid, _ = self.add(1, "Tan S1234567D", "91234567", notes="NRIC S1234567D")
        r = self.row(cid)
        self.assertIn("[ID removed]", r["notes"])
        self.assertNotIn("S1234567D", r["notes"])
        self.assertNotIn("S1234567D", r["name"])

    def test_chase_dates_3_7_14(self):
        cid, _ = self.add(1, "A", "91234567")
        expected = [(0, "messaged", "2026-11-13"), (3, "messaged", "2026-11-17"),
                    (7, "messaged", "2026-11-24"), (14, "stale", None)]
        for i, (day, stage, due) in enumerate(expected, 1):
            self.assertTrue(record_outcome(self.conn, 1, cid, "messaged", D + timedelta(days=day), NOW))
            r = self.row(cid)
            self.assertEqual((r["chase_step"], r["stage"], r["next_due_on"]), (i, stage, due))
        kinds = [x["kind"] for x in self.conn.execute("SELECT kind FROM activity ORDER BY id")]
        self.assertEqual(kinds, ["captured"] + ["messaged"] * 4)

    def test_double_sent_tap_is_ignored(self):
        cid, _ = self.add(1, "A", "91234567")
        self.assertTrue(record_outcome(self.conn, 1, cid, "messaged", D, NOW))
        self.assertFalse(record_outcome(self.conn, 1, cid, "messaged", D, NOW))
        self.assertFalse(record_outcome(self.conn, 1, cid, "messaged", D + timedelta(days=2), NOW))
        r = self.row(cid)
        self.assertEqual((r["chase_step"], r["next_due_on"]), (1, "2026-11-13"))
        n = self.conn.execute("SELECT COUNT(*) FROM activity WHERE kind='messaged'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_late_sent_tap_skips_passed_chase_days(self):
        cid, _ = self.add(1, "A", "91234567", today=date(2026, 11, 1))
        record_outcome(self.conn, 1, cid, "messaged", D, NOW)  # +3 and +7 already passed
        r = self.row(cid)
        self.assertEqual((r["chase_step"], r["stage"], r["next_due_on"]), (3, "messaged", "2026-11-15"))
        self.assertEqual(due_today(self.conn, 1, date(2026, 11, 11)), [])
        record_outcome(self.conn, 1, cid, "messaged", date(2026, 11, 15), NOW)
        self.assertEqual((self.row(cid)["stage"], self.row(cid)["next_due_on"]), ("stale", None))

    def test_late_sent_tap_with_no_chase_day_left_goes_stale(self):
        cid, _ = self.add(1, "A", "91234567", today=date(2026, 10, 1))
        record_outcome(self.conn, 1, cid, "messaged", D, NOW)
        r = self.row(cid)
        self.assertEqual((r["chase_step"], r["stage"], r["next_due_on"]), (4, "stale", None))

    def test_sent_on_a_chase_day_moves_to_the_next_one(self):
        cid, _ = self.add(1, "A", "91234567")
        record_outcome(self.conn, 1, cid, "messaged", D, NOW)
        record_outcome(self.conn, 1, cid, "messaged", date(2026, 11, 13), NOW)
        self.assertEqual((self.row(cid)["chase_step"], self.row(cid)["next_due_on"]), (2, "2026-11-17"))

    def test_next_due(self):
        self.assertEqual(next_due(D, 0), D)
        self.assertEqual(next_due(D, 3), date(2026, 11, 24))
        self.assertIsNone(next_due(D, 4))

    def test_replied_stops_chasing(self):
        cid, _ = self.add(1, "A", "91234567")
        record_outcome(self.conn, 1, cid, "messaged", D, NOW)
        record_outcome(self.conn, 1, cid, "replied", D, NOW)
        r = self.row(cid)
        self.assertEqual((r["stage"], r["next_due_on"]), ("replied", None))
        self.assertEqual(due_today(self.conn, 1, date(2027, 1, 1)), [])

    def test_due_today_isolated_by_agent(self):
        cid, _ = self.add(1, "A", "91234567")
        self.assertEqual(len(due_today(self.conn, 1, D)), 1)
        self.assertEqual(due_today(self.conn, 2, D), [])
        with self.assertRaises(KeyError):
            record_outcome(self.conn, 2, cid, "messaged", D, NOW)
        with self.assertRaises(KeyError):
            set_consent(self.conn, 2, cid, "yes")
        self.assertEqual(self.row(cid)["chase_step"], 0)
        # same phone for agent 2 is a separate contact
        _, created = self.add(2, "A", "91234567")
        self.assertTrue(created)

    def test_due_today_order_consent_first(self):
        old_unknown, _ = self.add(1, "Old", "91111111", "unknown", today=date(2026, 11, 1))
        new_yes, _ = self.add(1, "NewYes", "92222222", "yes", today=date(2026, 11, 9))
        old_yes, _ = self.add(1, "OldYes", "93333333", "yes", today=date(2026, 11, 2))
        self.add(1, "Future", "98888888", "yes", today=date(2026, 11, 20))
        ids = [r["id"] for r in due_today(self.conn, 1, D)]
        self.assertEqual(ids, [new_yes, old_yes, old_unknown])

    def test_messages_sent_counts_this_agents_messaged_rows_only(self):
        cid, _ = self.add(1, "A", "91234567")
        self.assertEqual(messages_sent(self.conn, 1, cid), 0)
        record_outcome(self.conn, 1, cid, "messaged", D, NOW)
        record_outcome(self.conn, 1, cid, "messaged", date(2026, 11, 13), NOW)
        record_outcome(self.conn, 1, cid, "replied", date(2026, 11, 14), NOW)
        self.assertEqual(messages_sent(self.conn, 1, cid), 2)
        self.assertEqual(messages_sent(self.conn, 2, cid), 0)

    def _go_stale(self, phone, consent="yes"):
        cid, _ = self.add(1, "A", phone, consent)
        for day in (0, 3, 7, 14):
            record_outcome(self.conn, 1, cid, "messaged", D + timedelta(days=day), NOW_AT(D + timedelta(days=day)))
        self.assertEqual(self.row(cid)["stage"], "stale")
        return cid

    def test_recapture_of_a_stale_lead_restarts_the_chase(self):
        cid = self._go_stale("91234567")
        later = D + timedelta(days=30)
        cid2, created = self.add(1, "A", "91234567", "yes", today=later)
        self.assertEqual((cid2, created), (cid, False))
        r = self.row(cid)
        self.assertEqual((r["stage"], r["captured_on"], r["chase_step"], r["next_due_on"]),
                         ("new", later.isoformat(), 0, later.isoformat()))
        self.assertEqual([x["id"] for x in due_today(self.conn, 1, later)], [cid])
        self.assertEqual(messages_sent(self.conn, 1, cid), 0)  # first-message wording again

    def test_recapture_on_the_day_of_the_last_sent_gets_first_message_wording(self):
        cid = self._go_stale("91234567")
        last_day = D + timedelta(days=14)  # the stale-making "Sent" tap was today
        self.add(1, "A", "91234567", "yes", today=last_day)
        self.assertEqual(self.row(cid)["stage"], "new")
        self.assertEqual(messages_sent(self.conn, 1, cid), 0)
        record_outcome(self.conn, 1, cid, "messaged", last_day, NOW_AT(last_day))
        self.assertEqual(messages_sent(self.conn, 1, cid), 1)  # only the new tap counts
        self.assertEqual(messages_sent(self.conn, 2, cid), 0)

    def test_a_first_sent_on_the_capture_day_still_counts(self):
        cid, _ = self.add(1, "A", "91234567", "yes")
        record_outcome(self.conn, 1, cid, "messaged", D, NOW)
        self.assertEqual(messages_sent(self.conn, 1, cid), 1)
        self.add(1, "A", "91234567", "yes", notes="more")  # merging a live lead restarts nothing
        self.assertEqual(messages_sent(self.conn, 1, cid), 1)

    def test_recapture_restart_then_walks_templates_from_the_first(self):
        cid = self._go_stale("91234567")
        later = D + timedelta(days=30)
        self.add(1, "A", "91234567", "yes", today=later)
        record_outcome(self.conn, 1, cid, "messaged", later, NOW_AT(later))
        self.assertEqual(messages_sent(self.conn, 1, cid), 1)

    def test_recapture_does_not_restart_other_stages(self):
        for stage in ("replied", "booked", "not_keen", "new", "messaged"):
            phone = {"replied": "91111111", "booked": "92222222", "not_keen": "93333333",
                     "new": "94444444", "messaged": "95555555"}[stage]
            cid, _ = self.add(1, "A", phone, "yes")
            if stage == "messaged":
                record_outcome(self.conn, 1, cid, "messaged", D, NOW)
            elif stage != "new":
                record_outcome(self.conn, 1, cid, stage, D, NOW)
            before = tuple(self.row(cid))
            self.add(1, "A", phone, "yes", today=D + timedelta(days=30))
            r = self.row(cid)
            self.assertEqual((r["stage"], r["captured_on"], r["chase_step"], r["next_due_on"]),
                             (before[7], before[8], before[10], before[9]), stage)

    def test_stale_no_consent_lead_stays_out_after_recapture(self):
        cid = self._go_stale("91234567", consent="no")
        later = D + timedelta(days=30)
        self.add(1, "A", "91234567", "yes", today=later)
        self.assertEqual(self.row(cid)["consent"], "no")
        self.assertEqual(due_today(self.conn, 1, later), [])

    def test_due_today_newest_first_then_id(self):
        a, _ = self.add(1, "A", "91111111", "yes", today=date(2026, 11, 9))
        b, _ = self.add(1, "B", "92222222", "yes", today=date(2026, 11, 9))
        c, _ = self.add(1, "C", "93333333", "yes", today=D)
        self.assertEqual([r["id"] for r in due_today(self.conn, 1, D)], [c, a, b])

    def test_due_today_leaves_out_leads_past_the_chase_window(self):
        d15, _ = self.add(1, "D15", "91111111", "yes", today=D - timedelta(days=15))
        d14, _ = self.add(1, "D14", "92222222", "yes", today=D - timedelta(days=14))
        ids = [r["id"] for r in due_today(self.conn, 1, D)]
        self.assertEqual(ids, [d14])
        self.assertNotIn(d15, ids)

    def test_expire_stale_flips_only_new_and_messaged_past_the_window(self):
        old = D - timedelta(days=15)
        new_old, _ = self.add(1, "N", "91111111", today=old)
        msg_old, _ = self.add(1, "M", "92222222", today=old)
        record_outcome(self.conn, 1, msg_old, "messaged", old, NOW)
        other_agent, _ = self.add(2, "O", "93333333", today=old)
        keep = {}
        for i, outcome in enumerate(("replied", "booked", "not_keen")):
            cid, _ = self.add(1, outcome, f"9444444{i}", today=old)
            record_outcome(self.conn, 1, cid, outcome, old, NOW)
            keep[outcome] = cid
        edge, _ = self.add(1, "Edge", "95555555", today=D - timedelta(days=14))
        fresh, _ = self.add(1, "Fresh", "96666666", today=D)
        self.assertEqual(expire_stale(self.conn, D), 3)
        for cid in (new_old, msg_old, other_agent):
            r = self.row(cid)
            self.assertEqual((r["stage"], r["next_due_on"]), ("stale", None))
        for outcome, cid in keep.items():
            self.assertEqual(self.row(cid)["stage"], outcome)
        for cid in (edge, fresh):
            self.assertEqual(self.row(cid)["stage"], "new")
            self.assertIsNotNone(self.row(cid)["next_due_on"])
        self.assertEqual(expire_stale(self.conn, D), 0)

    def test_due_today_excludes_consent_no(self):
        cid, _ = self.add(1, "A", "91234567", "no")
        self.assertEqual(due_today(self.conn, 1, D), [])
        self.assertEqual(set_consent(self.conn, 1, cid, "yes"), "no")
        self.assertEqual(self.row(cid)["consent"], "no")
        self.assertEqual(due_today(self.conn, 1, D), [])

    def test_set_consent_only_changes_unknown(self):
        cid, _ = self.add(1, "A", "91234567", "unknown")
        self.assertEqual(set_consent(self.conn, 1, cid, "yes"), "yes")
        self.assertEqual(len(due_today(self.conn, 1, D)), 1)
        self.assertEqual(set_consent(self.conn, 1, cid, "no"), "yes")
        self.assertEqual(self.row(cid)["consent"], "yes")

    def test_sg_today_late_evening(self):
        utc = timezone.utc
        self.assertEqual(sg_today(datetime(2026, 11, 10, 15, 30, tzinfo=utc)), date(2026, 11, 10))
        self.assertEqual(sg_today(datetime(2026, 11, 10, 16, 30, tzinfo=utc)), date(2026, 11, 11))


if __name__ == "__main__":
    unittest.main()
