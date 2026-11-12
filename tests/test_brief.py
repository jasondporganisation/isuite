import contextlib
import io
import sqlite3
import unittest
from unittest import mock
from datetime import date, datetime, timedelta, timezone

from isuite import wording, numbers, schedule
from isuite.bot import Deps
from isuite.brief import send_brief
from isuite.config import Config
from isuite.drafts import DraftBudget
from isuite.contacts import LeadDraft, add_or_merge, record_outcome
from isuite.templates import FALLBACK, FALLBACK_NO_PLACE
from isuite.db import connect
from isuite.llm import FakeLLM
from isuite.roster import get_agent
from isuite.telegram_api import FakeTelegram, TelegramError

NOW = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)  # Tue 10:00 SGT
TODAY = date(2026, 11, 10)
WEEK = date(2026, 11, 9)  # Monday


class BriefBase(unittest.TestCase):
    def setUp(self):
        # Drafts fall back when the fake LLM runs dry; their one-line stderr note is expected.
        self.stderr = io.StringIO()
        quiet = contextlib.redirect_stderr(self.stderr)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)
        self.tg = FakeTelegram()
        self.llm = FakeLLM([])
        self.deps = Deps(self.conn, Config("t", "k", ":memory:", set()), self.tg, self.llm,
                         lambda: NOW)

    def agent(self, tg_id, status="team", name=None, **cols):
        cur = self.conn.execute(
            "INSERT INTO agents(telegram_id, name, status) VALUES (?,?,?)",
            (tg_id, name or f"Agent{tg_id}", status))
        for k, v in cols.items():
            self.conn.execute(f"UPDATE agents SET {k}=? WHERE id=?", (v, cur.lastrowid))
        self.conn.commit()
        return get_agent(self.conn, tg_id)

    def lead(self, agent, name, phone, consent="yes", day=TODAY):
        cid, _ = add_or_merge(self.conn, agent.id, LeadDraft(name, phone, consent), "text", day, NOW)
        return cid

    def act(self, agent, cid, kind, at):
        self.conn.execute(
            "INSERT INTO activity(agent_id, contact_id, kind, at) VALUES (?,?,?,?)",
            (agent.id, cid, kind, at.isoformat()))
        self.conn.commit()


class BriefTests(BriefBase):
    def test_brief_lists_due_with_buttons(self):
        a = self.agent(1, name="Sam")
        cid = self.lead(a, "Tan Mei", "91234567")
        self.assertEqual(send_brief(self.deps, a, TODAY), 1)
        header, msg = self.tg.sent
        self.assertEqual(header["chat_id"], 1)
        self.assertEqual(header["text"], "Good morning Sam! 1 person to message today.")
        self.assertIn("Tan Mei", msg["text"])
        self.assertIn("first message", msg["text"])
        row1, row2 = msg["buttons"]
        self.assertEqual(row1[0][0], "Open WhatsApp")
        self.assertTrue(row1[0][1].startswith("https://wa.me/6591234567?text="))
        self.assertEqual([b[1] for b in row2], [f"out:{cid}:{o}" for o in
                         ("messaged", "replied", "booked", "not_keen")])
        self.assertEqual([b[0] for b in row2], ["Sent", "Replied", "Booked", "Not keen"])

    def test_brief_header_plural(self):
        self.assertEqual(wording.brief_header("Sam", 2, 10),
                         "Good morning Sam! 2 people to message today.")

    def test_brief_header_greeting_bands(self):
        for hour, greeting in ((0, "Good morning"), (11, "Good morning"), (12, "Good afternoon"),
                               (17, "Good afternoon"), (18, "Good evening"), (23, "Good evening")):
            self.assertEqual(wording.brief_header("Sam", 1, hour),
                             f"{greeting} Sam! 1 person to message today.", hour)

    def test_send_brief_greets_by_sgt_hour_of_the_injected_clock(self):
        a = self.agent(1, name="Sam")
        self.lead(a, "Tan Mei", "91234567")
        # 15:00 UTC is 23:00 SGT: evening, and the SGT date has rolled over
        self.deps.now = lambda: datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
        send_brief(self.deps, a, TODAY)
        self.assertEqual(self.tg.sent[0]["text"], "Good evening Sam! 1 person to message today.")
        self.tg.sent.clear()
        self.deps.now = lambda: datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)  # 14:00 SGT
        send_brief(self.deps, a, TODAY)
        self.assertEqual(self.tg.sent[0]["text"], "Good afternoon Sam! 1 person to message today.")

    def test_brief_nothing_due(self):
        a = self.agent(1)
        self.assertEqual(send_brief(self.deps, a, TODAY), 0)
        self.assertEqual([m["text"] for m in self.tg.sent], [wording.NOTHING_TODAY])

    def test_brief_consent_unknown_has_no_wa_link(self):
        a = self.agent(1)
        cid = self.lead(a, "Lim", "81234567", consent="unknown")
        send_brief(self.deps, a, TODAY)
        msg = self.tg.sent[1]
        self.assertIn(wording.CHECK_CONSENT, msg["text"])
        self.assertEqual(msg["buttons"][0], [("Has consent ✓", f"consent:{cid}")])
        self.assertEqual(len(msg["buttons"]), 2)
        flat = [v for row in msg["buttons"] for _, v in row]
        self.assertFalse(any(v.startswith("https://") for v in flat))
        self.assertEqual(self.llm.calls, [])

    def test_brief_bad_number_says_check_number(self):
        a = self.agent(1)
        self.lead(a, "Odd", "12345")
        send_brief(self.deps, a, TODAY)
        msg = self.tg.sent[1]
        self.assertIn(wording.CHECK_NUMBER, msg["text"])
        self.assertEqual(len(msg["buttons"]), 1)
        self.assertEqual(self.llm.calls, [])

    def test_brief_caps_at_15(self):
        a = self.agent(1)
        for i in range(17):
            self.lead(a, f"P{i}", f"9123{i:04d}", consent="unknown")
        self.assertEqual(send_brief(self.deps, a, TODAY), 15)
        self.assertEqual(len(self.tg.sent), 17)
        self.assertEqual(self.tg.sent[0]["text"], wording.brief_header("Agent1", 17, 10))
        self.assertEqual(self.tg.sent[-1]["text"], "+2 more. Send /today to see them later.")

    def test_new_leads_are_not_cut_by_the_cap(self):
        a = self.agent(1)
        for i in range(20):
            self.lead(a, f"Old{i}", f"9100{i:04d}", consent="unknown", day=TODAY - timedelta(days=5))
        for i in range(3):
            self.lead(a, f"Fresh{i}", f"9200{i:04d}", consent="unknown")
        send_brief(self.deps, a, TODAY)
        first15 = " ".join(m["text"] for m in self.tg.sent[1:16])
        for i in range(3):
            self.assertIn(f"Fresh{i}", first15)

    def fallback_text(self, step):
        return FALLBACK_NO_PLACE[step].format(first_name="Tan Mei", agent_name="Sam")

    def test_late_first_sent_does_not_jump_to_the_final_template(self):
        a = self.agent(1, name="Sam")
        cid = self.lead(a, "Tan Mei", "91234567", day=date(2026, 11, 1))
        record_outcome(self.conn, a.id, cid, "messaged", date(2026, 11, 10), NOW)
        row = self.conn.execute("SELECT chase_step FROM contacts").fetchone()
        self.assertEqual(row["chase_step"], 3)
        send_brief(self.deps, a, date(2026, 11, 15))
        msg = self.tg.sent[1]
        self.assertIn("follow-up 1", msg["text"])
        self.assertIn(self.fallback_text(1), msg["text"])
        self.assertNotIn(self.fallback_text(3), msg["text"])
        self.assertIn("Follow-up number: 1", self.llm.calls[0]["user"])

    def test_on_time_flow_walks_templates_0_to_3(self):
        a = self.agent(1, name="Sam")
        cid = self.lead(a, "Tan Mei", "91234567", day=TODAY)
        for day, step in ((0, 0), (3, 1), (7, 2), (14, 3)):
            when = TODAY + timedelta(days=day)
            self.tg.sent.clear()
            send_brief(self.deps, a, when)
            text = self.tg.sent[1]["text"]
            self.assertIn(self.fallback_text(step), text)
            self.assertIn("first message" if step == 0 else f"follow-up {step}", text)
            record_outcome(self.conn, a.id, cid, "messaged", when, NOW)

    def test_recaptured_stale_lead_is_briefed_with_the_first_message(self):
        a = self.agent(1, name="Sam")
        cid = self.lead(a, "Tan Mei", "91234567", day=TODAY)
        for day in (0, 3, 7, 14):
            when = TODAY + timedelta(days=day)
            record_outcome(self.conn, a.id, cid, "messaged", when, NOW)
        later = TODAY + timedelta(days=30)
        self.lead(a, "Tan Mei", "91234567", day=later)
        send_brief(self.deps, a, later)
        text = self.tg.sent[1]["text"]
        self.assertIn(self.fallback_text(0), text)
        self.assertIn("first message", text)

    def test_one_failed_send_does_not_end_the_agents_brief(self):
        a = self.agent(1, name="Sam")
        for i, (n, ph) in enumerate([("Alpha One", "91110001"), ("Beta Two", "91110002"),
                                     ("Gamma Three", "91110003"), ("Delta Four", "91110004")]):
            self.lead(a, n, ph)
        sends = []

        class FlakyTG(FakeTelegram):
            def send(self, chat_id, text, buttons=None):
                sends.append(text)
                if len(sends) == 3:  # header is 1st, contact 1 is 2nd, contact 2 is 3rd
                    raise TelegramError("Beta Two 91110002 blocked")
                super().send(chat_id, text, buttons)

        self.deps.tg = FlakyTG()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            delivered = send_brief(self.deps, a, TODAY)
        self.assertEqual(delivered, 3)
        texts = [m["text"] for m in self.deps.tg.sent]
        self.assertEqual(len(texts), 4)  # header + 3 delivered contacts
        self.assertFalse(any("Beta" in t for t in texts))
        self.assertTrue(all(any(n in t for t in texts) for n in ("Alpha", "Gamma", "Delta")))
        lines = [l for l in err.getvalue().splitlines() if "isuite brief error" in l]
        self.assertEqual(lines, ["isuite brief error: TelegramError"])
        self.assertNotIn("Beta", err.getvalue())
        self.assertNotIn("9111", err.getvalue())

    def test_failed_send_still_sends_the_more_line(self):
        a = self.agent(1, name="Sam")
        for i in range(17):
            self.lead(a, f"Person{i:02d}", f"9100{i:04d}")
        calls = []

        class FlakyTG(FakeTelegram):
            def send(self, chat_id, text, buttons=None):
                calls.append(1)
                if len(calls) == 2:
                    raise TelegramError("x")
                super().send(chat_id, text, buttons)

        self.deps.tg = FlakyTG()
        delivered = send_brief(self.deps, a, TODAY)
        self.assertEqual(delivered, 14)
        self.assertEqual(self.deps.tg.sent[-1]["text"], wording.brief_more(2))

    def test_brief_header_failure_is_not_swallowed(self):
        a = self.agent(1)
        self.lead(a, "Tan", "91234567")

        class DeadTG(FakeTelegram):
            def send(self, *a, **k):
                raise TelegramError("blocked")

        self.deps.tg = DeadTG()
        with self.assertRaises(TelegramError):
            send_brief(self.deps, a, TODAY)

    def test_brief_only_own_contacts(self):
        a, b = self.agent(1), self.agent(2)
        self.lead(b, "Theirs", "91234567")
        self.assertEqual(send_brief(self.deps, a, TODAY), 0)


class DraftBudgetBriefTests(BriefBase):
    def three_agents(self):
        agents = [self.agent(i, name=f"Agent{i}") for i in (1, 2, 3)]
        for a in agents:
            self.lead(a, f"Lead A{a.id}", f"9{a.id}000001")
            self.lead(a, f"Lead B{a.id}", f"9{a.id}000002")
        return agents

    def fallback_text(self, a):
        return FALLBACK_NO_PLACE[0].format(first_name="Lead", agent_name=a.name)

    def test_one_ai_error_switches_rest_of_job_to_fallbacks(self):
        agents = self.three_agents()
        self.llm = FakeLLM([])  # raises on the first call
        self.deps.llm = self.llm
        schedule.run_job(self.deps, "brief", NOW)
        self.assertEqual(len(self.llm.calls), 1)
        texts = [m["text"] for m in self.tg.sent if "Lead" in m["text"]]
        self.assertEqual(len(texts), 6)  # every contact of every agent still got a message
        for a in agents:
            self.assertEqual(sum(1 for m in self.tg.sent if m["chat_id"] == a.telegram_id), 3)

    def test_ai_drafts_still_used_while_the_ai_works(self):
        self.three_agents()
        self.llm = FakeLLM(["Hi Lead, lovely meeting you."] * 6)
        self.deps.llm = self.llm
        schedule.run_job(self.deps, "brief", NOW)
        self.assertEqual(len(self.llm.calls), 6)

    def test_job_budget_is_shared_and_time_limited(self):
        self.three_agents()
        self.llm = FakeLLM(["Hi Lead, lovely meeting you."] * 6)
        self.deps.llm = self.llm
        now = [0.0]

        def clock():
            now[0] += 40  # every look at the clock costs 40s
            return now[0]

        with mock.patch("isuite.schedule.DraftBudget", lambda: DraftBudget(clock=clock)):
            schedule.run_job(self.deps, "brief", NOW)
        self.assertLess(len(self.llm.calls), 6)
        self.assertGreaterEqual(len([m for m in self.tg.sent if "Lead" in m["text"]]), 6)

    def test_send_brief_without_budget_makes_its_own(self):
        a = self.agent(1, name="Sam")
        self.lead(a, "Lead A", "91234567")
        self.lead(a, "Lead B", "91234568")
        self.llm = FakeLLM([])
        self.deps.llm = self.llm
        send_brief(self.deps, a, TODAY)
        self.assertEqual(len(self.llm.calls), 1)  # second contact skipped the AI
        send_brief(self.deps, a, TODAY)  # a new call gets a new budget
        self.assertEqual(len(self.llm.calls), 2)


class NumbersTests(BriefBase):
    def test_followed_up_48h_percent(self):
        a = self.agent(1)
        c1 = self.lead(a, "A", "91234561", day=date(2026, 11, 9))
        c2 = self.lead(a, "B", "91234562", day=date(2026, 11, 9))
        c3 = self.lead(a, "C", "91234563", day=date(2026, 11, 9))
        self.act(a, c1, "messaged", datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc))  # +2d ok
        self.act(a, c2, "messaged", datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc))  # +3d late
        stats = numbers.agent_week(self.conn, a.id, WEEK)
        self.assertEqual(stats["followed_up_48h"], 33)
        self.assertEqual(stats["leads_captured"], 3)
        self.assertEqual(stats["messaged"], 2)
        self.assertEqual(numbers.agent_week(self.conn, a.id, WEEK - timedelta(days=7))
                         ["followed_up_48h"], 0)

    def test_counts_use_sgt_week_and_ignore_merged(self):
        a = self.agent(1)
        c = self.lead(a, "A", "91234561", day=date(2026, 11, 9))
        self.act(a, c, "merged", NOW)
        # 2026-11-08 17:00 UTC is Mon 01:00 SGT -> in week; 16:00 UTC is Sun 00:00... out
        self.act(a, c, "replied", datetime(2026, 11, 8, 16, 30, tzinfo=timezone.utc))
        self.act(a, c, "booked", datetime(2026, 11, 8, 15, 59, tzinfo=timezone.utc))
        self.act(a, c, "closed", datetime(2026, 11, 15, 15, 59, tzinfo=timezone.utc))
        self.act(a, c, "roadshow", NOW)
        stats = numbers.agent_week(self.conn, a.id, WEEK)
        self.assertEqual((stats["leads_captured"], stats["replied"], stats["booked"],
                          stats["closed"], stats["roadshows"]), (1, 1, 0, 1, 1))

    def test_team_view_has_no_contact_fields(self):
        a = self.agent(1, name="Sam")
        self.lead(a, "Secret", "91234567")
        rows = numbers.team_view(self.conn, WEEK)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"name", "leads_captured", "messaged", "followed_up_48h",
                                        "replied", "booked", "closed", "roadshows"})
        self.assertNotIn("Secret", repr(rows))

    def test_left_agent_not_in_team_view(self):
        self.agent(1, status="team", name="Stay")
        self.agent(2, status="trial", name="Gone")
        self.agent(3, status="paused", name="Paused")
        self.assertEqual([r["name"] for r in numbers.team_view(self.conn, WEEK)], ["Stay"])


class ScheduleTests(BriefBase):
    def test_brief_due_at_0830_sgt_when_server_utc(self):
        early = datetime(2026, 11, 10, 0, 29, tzinfo=timezone.utc)
        on = datetime(2026, 11, 10, 0, 30, tzinfo=timezone.utc)
        self.assertNotIn("brief", schedule.due_jobs(early, {}))
        self.assertIn("brief", schedule.due_jobs(on, {}))
        self.assertNotIn("brief", schedule.due_jobs(on, {"brief": "2026-11-10"}))
        self.assertIn("brief", schedule.due_jobs(on + timedelta(days=1), {"brief": "2026-11-10"}))

    def test_each_job_runs_inside_a_wait_budget_that_is_always_ended(self):
        a = self.agent(1)
        self.lead(a, "Tan", "91234567")
        for name, when in (("brief", NOW), ("scorecard", NOW), ("nudge", NOW)):
            self.tg.budget_calls.clear()
            self.tg.budget_seconds.clear()
            schedule.run_job(self.deps, name, when)
            self.assertEqual(self.tg.budget_calls, ["begin", "end"], name)
            self.assertEqual(self.tg.budget_seconds, [schedule.JOB_WAIT_BUDGET], name)
        self.assertEqual(0 < schedule.JOB_WAIT_BUDGET <= 60, True)

    def test_the_wait_budget_ends_even_when_the_job_raises(self):
        self.agent(1)
        self.tg.budget_calls.clear()
        with mock.patch("isuite.schedule.brief.send_brief", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                schedule.run_job(self.deps, "brief", NOW)
        self.assertEqual(self.tg.budget_calls, ["begin", "end"])
        with self.assertRaises(ValueError):
            schedule.run_job(self.deps, "bogus", NOW)

    def test_brief_not_due_from_noon_even_with_empty_jobs_table(self):
        # 03:59 UTC = 11:59 SGT, 04:00 UTC = 12:00 SGT, 15:10 UTC = 23:10 SGT
        self.assertIn("brief", schedule.due_jobs(datetime(2026, 11, 10, 3, 59, tzinfo=timezone.utc), {}))
        self.assertNotIn("brief", schedule.due_jobs(datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc), {}))
        self.assertNotIn("brief", schedule.due_jobs(datetime(2026, 11, 10, 15, 10, tzinfo=timezone.utc), {}))
        self.assertEqual(schedule.BRIEF_UNTIL_HOUR, 12)

    def test_scorecard_sunday_2000_sgt(self):
        sun_1959 = datetime(2026, 11, 15, 11, 59, tzinfo=timezone.utc)
        sun_2000 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        self.assertNotIn("scorecard", schedule.due_jobs(sun_1959, {}))
        self.assertIn("scorecard", schedule.due_jobs(sun_2000, {}))
        self.assertNotIn("scorecard", schedule.due_jobs(sun_2000, {"scorecard": "2026-11-15"}))
        self.assertNotIn("scorecard", schedule.due_jobs(NOW, {}))  # a Tuesday

    def test_nudge_hourly_marker(self):
        self.assertIn("nudge", schedule.due_jobs(NOW, {}))
        self.assertNotIn("nudge", schedule.due_jobs(NOW, {"nudge": "2026-11-10T10"}))
        self.assertIn("nudge", schedule.due_jobs(NOW + timedelta(hours=1),
                                                 {"nudge": "2026-11-10T10"}))

    def test_run_job_stores_markers(self):
        for name, marker in (("brief", "2026-11-10"), ("scorecard", "2026-11-10"),
                             ("nudge", "2026-11-10T10")):
            schedule.run_job(self.deps, name, NOW)
            self.assertEqual(schedule.load_last_run(self.conn)[name], marker)
        schedule.run_job(self.deps, "nudge", NOW + timedelta(hours=1))
        self.assertEqual(schedule.load_last_run(self.conn)["nudge"], "2026-11-10T11")

    def test_hourly_job_expires_stale_leads_for_every_agent(self):
        a, b = self.agent(1), self.agent(2, status="paused")
        old = TODAY - timedelta(days=15)
        c1 = self.lead(a, "Old", "91234561", day=old)
        c2 = self.lead(b, "OldB", "91234562", day=old)
        c3 = self.lead(a, "New", "91234563")
        schedule.run_job(self.deps, "nudge", datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc))  # 23:00 SGT
        stages = {r["id"]: r["stage"] for r in self.conn.execute("SELECT id, stage FROM contacts")}
        self.assertEqual(stages, {c1: "stale", c2: "stale", c3: "new"})

    def test_old_trial_rows_are_paused_with_a_fresh_30_day_clock(self):
        a = self.agent(1, status="trial", trial_ends_on="2027-03-01")
        b = self.agent(2, status="trial", trial_ends_on="2026-11-09")
        schedule.run_job(self.deps, "nudge", NOW)
        for i in (a.id, b.id):
            row = get_agent(self.conn, i)
            self.assertEqual((row.status, row.paused_on), ("paused", "2026-11-10"))

    def test_paused_agent_gets_no_brief(self):
        paused = self.agent(1, status="paused")
        left = self.agent(2, status="left")
        team = self.agent(3, status="team")
        trial = self.agent(4, status="trial")
        for a in (paused, left, team, trial):
            self.lead(a, f"L{a.id}", f"9123456{a.id}")
        schedule.run_job(self.deps, "brief", NOW)
        self.assertEqual({m["chat_id"] for m in self.tg.sent}, {3})

    def test_brief_one_blocked_agent_does_not_stop_others(self):
        self.agent(1)
        self.agent(2)
        real = self.tg.send

        def send(chat_id, text, buttons=None):
            if chat_id == 1:
                raise TelegramError("blocked")
            real(chat_id, text, buttons)

        self.tg.send = send
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            schedule.run_job(self.deps, "brief", NOW)
        self.assertEqual([m["chat_id"] for m in self.tg.sent], [2])
        self.assertEqual(err.getvalue().strip(), "isuite job error: TelegramError")
        self.assertEqual(schedule.load_last_run(self.conn)["brief"], "2026-11-10")

    def test_marker_is_stored_before_any_message_goes_out(self):
        sunday = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)  # 20:00 SGT
        for name, when, marker in (("brief", NOW, "2026-11-10"), ("scorecard", sunday, "2026-11-15"),
                                   ("nudge", NOW, "2026-11-10T10")):
            for table in ("activity", "contacts", "agents"):
                self.conn.execute(f"DELETE FROM {table}")
            self.tg.sent.clear()
            a = self.agent(1, roadshow_on="2026-11-09")
            self.agent(2, roadshow_on="2026-11-09")
            if name != "nudge":  # a lead captured today would cancel the roadshow nudge
                self.lead(a, "Tan", "91234567")
            seen = []
            real = self.tg.send

            def send(chat_id, text, buttons=None):
                seen.append(schedule.load_last_run(self.conn).get(name))
                if len(seen) == 2:
                    raise TelegramError("blocked part-way")
                real(chat_id, text, buttons)

            self.tg.send = send
            with contextlib.redirect_stderr(io.StringIO()):
                schedule.run_job(self.deps, name, when)
            self.tg.send = real
            self.assertTrue(seen, name)
            self.assertEqual(set(seen), {marker}, name)
            self.assertNotIn(name, schedule.due_jobs(when, schedule.load_last_run(self.conn)))

    def test_failed_marker_store_sends_nothing(self):
        for i, (name, when) in enumerate((
                ("brief", NOW), ("scorecard", datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)),
                ("nudge", NOW)), start=10):
            a = self.agent(i, roadshow_on="2026-11-09")
            self.lead(a, "Tan", f"9123{i:04d}")
            with mock.patch.object(schedule, "_store", side_effect=sqlite3.OperationalError("disk")):
                with self.assertRaises(sqlite3.OperationalError):
                    schedule.run_job(self.deps, name, when)
            self.assertEqual(self.tg.sent, [], name)
            self.assertEqual(self.llm.calls, [], name)
        self.assertEqual(schedule.load_last_run(self.conn), {})

    def test_scorecard_sends_each_agent_their_week(self):
        a = self.agent(1)
        self.lead(a, "X", "91234567")
        schedule.run_job(self.deps, "scorecard", datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(self.tg.sent[0]["text"], wording.week_summary(
            numbers.agent_week(self.conn, a.id, WEEK)))
        self.assertIn("1 leads", self.tg.sent[0]["text"])

    def test_hourly_job_deletes_expired_pending_for_all_agents(self):
        a, b, c = self.agent(1), self.agent(2), self.agent(3, status="paused")
        stamps = [(a, NOW - timedelta(hours=25)), (b, NOW - timedelta(hours=1)), (c, None)]
        for ag, at in stamps:
            self.conn.execute("INSERT INTO pending VALUES (?,?,?)",
                              (ag.id, '{"leads": []}', at.isoformat() if at else "garbage"))
        self.conn.commit()
        schedule.run_job(self.deps, "nudge", datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc))  # 23:00 SGT, outside the window
        left = [r[0] for r in self.conn.execute("SELECT agent_id FROM pending")]
        self.assertEqual(left, [b.id])

    def test_hourly_job_purges_a_paused_agent_after_30_days_and_export_is_empty(self):
        gone = self.agent(1, status="paused", paused_on="2026-10-10")   # 31 days ago
        kept = self.agent(2, status="paused", paused_on="2026-10-11")   # exactly 30 days
        for a in (gone, kept):
            self.lead(a, "Tan", f"9{a.id:07d}")
        schedule.run_job(self.deps, "nudge", NOW)
        count = lambda a: self.conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE agent_id=?", (a.id,)).fetchone()[0]
        self.assertEqual((count(gone), count(kept)), (0, 1))
        self.assertEqual(get_agent(self.conn, 1).status, "paused")

    def test_old_trial_row_is_paused_today_and_purged_30_days_later(self):
        a = self.agent(1, status="trial", trial_ends_on="2026-09-26")
        self.lead(a, "Tan", "91234567")
        count = lambda: self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        schedule.run_job(self.deps, "nudge", NOW)
        row = get_agent(self.conn, 1)
        self.assertEqual((row.status, row.paused_on), ("paused", "2026-11-10"))
        self.assertEqual(count(), 1)  # data intact: a real chance to export
        for days, left in ((30, 1), (31, 0)):
            schedule.run_job(self.deps, "nudge", NOW + timedelta(days=days))
            self.assertEqual(count(), left, days)

    def test_paused_row_without_paused_on_starts_its_clock_and_is_not_purged(self):
        a = self.agent(1, status="paused", trial_ends_on="2026-01-01")
        self.lead(a, "Tan", "91234567")
        schedule.run_job(self.deps, "nudge", NOW)
        self.assertEqual(get_agent(self.conn, 1).paused_on, "2026-11-10")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 1)

    def test_paused_row_with_an_unreadable_paused_on_is_repaired_not_kept_or_purged(self):
        bad = ("garbage", "", "2026-13-45", "10/10/2026", "  ")
        for i, value in enumerate(bad, start=1):
            a = self.agent(i, status="paused")
            self.conn.execute("UPDATE agents SET paused_on=? WHERE id=?", (value, a.id))
            self.lead(a, "Tan", f"9123456{i}")
        self.conn.commit()
        schedule.run_job(self.deps, "nudge", NOW)
        for i in range(1, len(bad) + 1):
            self.assertEqual(get_agent(self.conn, i).paused_on, "2026-11-10")  # clock starts today
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], len(bad))
        later = datetime(2026, 12, 11, 4, 0, tzinfo=timezone.utc)  # 31 days on: now purged
        schedule.run_job(self.deps, "nudge", later)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 0)

    def test_repair_leaves_a_good_paused_on_and_other_statuses_alone(self):
        good = self.agent(1, status="paused", paused_on="2026-10-20")
        team = self.agent(2, status="team")
        self.conn.execute("UPDATE agents SET paused_on='garbage' WHERE id=?", (team.id,))
        self.conn.commit()
        schedule.run_job(self.deps, "nudge", NOW)
        self.assertEqual(get_agent(self.conn, good.id).paused_on, "2026-10-20")
        self.assertEqual(get_agent(self.conn, team.id).paused_on, "garbage")

    def test_hourly_job_pauses_then_keeps_the_data_for_the_grace_period(self):
        a = self.agent(1, status="trial", trial_ends_on="2026-10-01")
        self.lead(a, "Tan", "91234567")
        schedule.run_job(self.deps, "nudge", datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(get_agent(self.conn, 1).status, "paused")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 1)

    def test_one_failing_cleanup_step_does_not_skip_the_rest_or_the_nudges(self):
        a, b = self.agent(1, roadshow_on="2026-11-09"), self.agent(2)
        old = self.lead(b, "Old", "91234561", day=TODAY - timedelta(days=15))
        self.conn.execute("INSERT INTO pending VALUES (?,?,?)",
                          (a.id, '{"leads": []}', (NOW - timedelta(hours=25)).isoformat()))
        self.conn.commit()

        def boom(conn, today):
            raise RuntimeError("secret detail")

        err = io.StringIO()
        with mock.patch("isuite.schedule.purge_paused", boom), contextlib.redirect_stderr(err):
            schedule.run_job(self.deps, "nudge", NOW)
        self.assertIn("isuite job error: RuntimeError", err.getvalue())
        self.assertNotIn("secret detail", err.getvalue())
        self.assertEqual(self.conn.execute("SELECT stage FROM contacts WHERE id=?", (old,))
                         .fetchone()[0], "stale")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0], 0)
        self.assertEqual([m["text"] for m in self.tg.sent], [wording.ROADSHOW_NUDGE])

    def test_a_cleanup_step_that_writes_then_raises_leaves_nothing_to_commit(self):
        a = self.agent(1)

        def write_then_raise(conn, today):
            conn.execute("INSERT INTO activity (agent_id, contact_id, kind, at) "
                         "VALUES (?,NULL,'ghost',?)", (a.id, NOW.isoformat()))
            raise RuntimeError("after the write")

        with mock.patch("isuite.schedule.purge_paused", write_then_raise), \
                contextlib.redirect_stderr(io.StringIO()):
            schedule.run_job(self.deps, "nudge", NOW)
        self.conn.commit()  # any later commit must find nothing half-written
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM activity WHERE kind='ghost'").fetchone()[0], 0)

    def test_nudge_once_per_roadshow(self):
        yesterday = "2026-11-09"
        quiet = self.agent(1, roadshow_on=yesterday)
        busy = self.agent(2, roadshow_on=yesterday)
        other_day = self.agent(3, roadshow_on="2026-11-10")
        c = self.lead(busy, "Got", "91234567", day=date(2026, 11, 9))
        self.act(busy, c, "captured", datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc))
        schedule.run_job(self.deps, "nudge", NOW)
        self.assertEqual([(m["chat_id"], m["text"]) for m in self.tg.sent],
                         [(1, wording.ROADSHOW_NUDGE)])
        self.assertEqual(wording.ROADSHOW_NUDGE,
                         "You did a roadshow yesterday and logged 0 leads. Anyone you spoke to?")
        self.assertIsNone(get_agent(self.conn, 1).roadshow_on)
        self.assertIsNone(get_agent(self.conn, 2).roadshow_on)
        self.assertEqual(get_agent(self.conn, 3).roadshow_on, "2026-11-10")
        schedule.run_job(self.deps, "nudge", NOW + timedelta(hours=1))
        self.assertEqual(len(self.tg.sent), 1)
        self.assertEqual((quiet.id, other_day.id), (1, 3))

    def test_no_nudge_at_midnight_and_roadshow_kept(self):
        a = self.agent(1, roadshow_on="2026-11-09")
        schedule.run_job(self.deps, "nudge", datetime(2026, 11, 9, 16, 30, tzinfo=timezone.utc))  # 00:30 SGT
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(get_agent(self.conn, a.telegram_id).roadshow_on, "2026-11-09")

    def test_nudge_at_0905_next_day(self):
        self.agent(1, roadshow_on="2026-11-09")
        schedule.run_job(self.deps, "nudge", datetime(2026, 11, 10, 1, 5, tzinfo=timezone.utc))
        self.assertEqual([m["text"] for m in self.tg.sent], [wording.ROADSHOW_NUDGE])

    def test_no_nudge_from_2100(self):
        self.agent(1, roadshow_on="2026-11-09")
        schedule.run_job(self.deps, "nudge", datetime(2026, 11, 10, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(self.tg.sent, [])

    def test_old_roadshow_still_nudges_once(self):
        self.agent(1, roadshow_on="2026-11-07")
        schedule.run_job(self.deps, "nudge", NOW)
        schedule.run_job(self.deps, "nudge", NOW + timedelta(hours=1))
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIsNone(get_agent(self.conn, 1).roadshow_on)

    def test_failed_nudge_send_keeps_roadshow_for_retry(self):
        self.agent(1, roadshow_on="2026-11-09")
        real = self.tg.send

        def boom(*a, **k):
            raise TelegramError("blocked")

        self.tg.send = boom
        with contextlib.redirect_stderr(io.StringIO()):
            schedule.run_job(self.deps, "nudge", NOW)
        self.assertEqual(get_agent(self.conn, 1).roadshow_on, "2026-11-09")
        self.tg.send = real
        schedule.run_job(self.deps, "nudge", NOW + timedelta(hours=1))
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIsNone(get_agent(self.conn, 1).roadshow_on)

    def test_stale_roadshow_on_paused_and_left_cleared_without_sending(self):
        self.agent(1, status="paused", roadshow_on="2026-11-09")
        self.agent(2, status="left", roadshow_on="2026-11-09")
        schedule.run_job(self.deps, "nudge", NOW)
        self.assertEqual(self.tg.sent, [])
        self.assertIsNone(get_agent(self.conn, 1).roadshow_on)
        self.assertIsNone(get_agent(self.conn, 2).roadshow_on)


if __name__ == "__main__":
    unittest.main()
