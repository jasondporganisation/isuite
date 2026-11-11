import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone

from isuite import bot, wording, run, schedule
from isuite.bot import Deps
from isuite.config import Config
from isuite.db import connect
from isuite.llm import FakeLLM
from isuite.telegram_api import FakeTelegram, TelegramError

ADMIN, AGENT = 1, 100
LEADS = json.dumps({"leads": [
    {"name": "Tan", "phone": "9123 4567", "consent_ticked": True, "notes": ""},
    {"name": "Lim", "phone": "81234567", "consent_ticked": None, "notes": ""},
]})


def msg(uid, text=None, **extra):
    m = {"from": {"id": uid, "first_name": "Sam"}, "chat": {"id": uid, "type": "private"}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"message": m}


def cb(uid, data):
    return {"callback_query": {"id": "cb", "from": {"id": uid}, "data": data,
                               "message": {"chat": {"id": uid, "type": "private"}}}}


def _no_serve(cfg):
    raise AssertionError("run.main reached the polling loop")


class E2ETests(unittest.TestCase):
    def setUp(self):
        # Drafts fall back when the fake LLM runs dry; their one-line stderr note is expected.
        self.stderr = io.StringIO()
        quiet = contextlib.redirect_stderr(self.stderr)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)
        self.tg = FakeTelegram()
        self.clock = [datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)]  # Tue 10:00 SGT
        self.deps = Deps(self.conn, Config("t", "k", ":memory:", {ADMIN}), self.tg,
                         FakeLLM([LEADS]), lambda: self.clock[0])

    def feed(self, *updates):
        self.tg.queue.extend(updates)
        run.step(self.deps, None)

    def run_jobs(self):
        now = self.clock[0]
        for name in schedule.due_jobs(now, schedule.load_last_run(self.conn)):
            schedule.run_job(self.deps, name, now)

    def test_e2e_full_day(self):
        self._full_day()

    def test_e2e_full_day_a_year_ahead(self):
        self.clock[0] = datetime(2027, 11, 10, 2, 0, tzinfo=timezone.utc)
        self._full_day()

    def _full_day(self):
        tg = self.tg
        start = self.clock[0]
        self.feed(msg(ADMIN, "/invite"))
        code = tg.sent[-1]["text"].split()[-1]
        self.feed(msg(AGENT, f"/start {code}"))
        self.feed(msg(AGENT, "roadshow Sat Tampines"))
        tg.files["p1"] = b"img"
        self.feed(msg(AGENT, photo=[{"file_id": "p1"}]))
        self.feed(cb(AGENT, "ok"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 2)

        # next day 08:30 SGT: brief
        self.clock[0] = start + timedelta(hours=22, minutes=30)
        before = len(tg.sent)
        self.run_jobs()
        brief = [m for m in tg.sent[before:] if m["chat_id"] == AGENT]
        tan = next(m for m in brief if m["text"].startswith("Tan"))
        lim = next(m for m in brief if m["text"].startswith("Lim"))
        urls = [v for row in tan["buttons"] for _, v in row if v.startswith("https://wa.me/")]
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith("https://wa.me/6591234567?text="))
        self.assertIn(wording.CHECK_CONSENT, lim["text"])
        self.assertFalse([v for row in lim["buttons"] for _, v in row if v.startswith("https://")])

        # tap messaged
        cid = self.conn.execute("SELECT id FROM contacts WHERE name='Tan'").fetchone()["id"]
        self.feed(cb(AGENT, f"out:{cid}:messaged"))
        self.assertEqual(tg.sent[-1]["text"], wording.outcome_saved("messaged"))

        # 3 days later: follow-up due again
        self.clock[0] += timedelta(days=3)
        before = len(tg.sent)
        self.run_jobs()
        again = [m["text"] for m in tg.sent[before:] if m["chat_id"] == AGENT]
        self.assertTrue(any(t.startswith("Tan") and "follow-up 1" in t for t in again))

        # admin team view: counts only, no lead names
        self.feed(msg(ADMIN, "/team"))
        team = tg.sent[-1]["text"]
        self.assertIn("Sam: 2 leads, 1 messaged", team)
        self.assertNotIn("Tan", team)
        self.assertNotIn("Lim", team)

    def test_main_refuses_without_token(self):
        for env, var in (({}, "ISUITE_TELEGRAM_TOKEN"),
                         ({"ISUITE_TELEGRAM_TOKEN": "secret-tok"}, "ANTHROPIC_API_KEY")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = run.main([], env=env, serve=_no_serve)
            self.assertEqual(code, 2)
            self.assertIn(var, err.getvalue())
            self.assertNotIn("secret-tok", err.getvalue())

    def test_main_bad_admins_value_is_a_clean_exit_not_a_traceback(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run.main([], env={"ISUITE_ADMINS": "12,not-a-number-77",
                                     "ISUITE_TELEGRAM_TOKEN": "t", "ANTHROPIC_API_KEY": "k"},
                            serve=_no_serve)
        self.assertEqual(code, 2)
        self.assertIn("ISUITE_ADMINS", err.getvalue())
        self.assertNotIn("not-a-number-77", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_step_survives_poll_error_and_bad_update(self):
        sleeps = []

        class Boom(FakeTelegram):
            def get_updates(self, offset, timeout=30):
                raise TelegramError("token 123:abc leaked")

        self.deps.tg = Boom()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = run.step(self.deps, 7, sleep=sleeps.append)
        self.assertEqual((out, sleeps), (7, [5]))
        self.assertIn("TelegramError", err.getvalue())
        self.assertNotIn("123:abc", err.getvalue())

        self.deps.tg = self.tg
        self.tg.queue.append({"update_id": 41, "message": {"chat": {"type": "private"}}})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run.step(self.deps, None), 42)

    def test_offset_is_stored_and_a_restart_resumes_from_it(self):
        self.tg.queue.append({"update_id": 41, "message": {"chat": {"type": "private"}}})
        self.assertEqual(run.step(self.deps, None), 42)
        self.assertEqual(schedule.load_offset(self.conn), 42)

        seen = []

        class Spy(FakeTelegram):
            def get_updates(self, offset, timeout=30):
                seen.append(offset)
                return []

        self.deps.tg = Spy()
        run.step(self.deps, schedule.load_offset(self.conn))
        self.assertEqual(seen, [42])

    def test_offset_is_stored_before_the_update_is_handled(self):
        stored_during = []
        from isuite import run as run_mod
        real = run_mod.handle_update

        def spy(deps, update):
            stored_during.append(schedule.load_offset(deps.conn))

        run_mod.handle_update = spy
        self.addCleanup(setattr, run_mod, "handle_update", real)
        self.tg.queue.append({"update_id": 9})
        run.step(self.deps, None)
        self.assertEqual(stored_during, [10])

    def test_load_offset_missing_or_unreadable_is_none(self):
        self.assertIsNone(schedule.load_offset(self.conn))
        self.conn.execute("INSERT INTO jobs(name, last) VALUES ('tg_offset', 'junk')")
        self.assertIsNone(schedule.load_offset(self.conn))

    def test_failed_offset_store_does_not_stop_the_update(self):
        orig = schedule.store_offset
        schedule.store_offset = lambda *a: (_ for _ in ()).throw(RuntimeError("disk full"))
        self.addCleanup(setattr, schedule, "store_offset", orig)
        self.tg.queue.append(msg(ADMIN, "/invite") | {"update_id": 3})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(run.step(self.deps, None), 4)
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("RuntimeError", err.getvalue())
        self.assertNotIn("disk full", err.getvalue())

    def test_offset_row_does_not_change_due_jobs(self):
        now = datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)
        before = schedule.due_jobs(now, schedule.load_last_run(self.conn))
        schedule.store_offset(self.conn, 42)
        self.assertEqual(schedule.due_jobs(now, schedule.load_last_run(self.conn)), before)

    def test_step_survives_job_error(self):
        orig = schedule.run_job
        schedule.run_job = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        self.addCleanup(setattr, schedule, "run_job", orig)
        self.tg.queue.append({"update_id": 5})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(run.step(self.deps, None), 6)
        self.assertIn("RuntimeError", err.getvalue())

    def test_job_error_rolls_back_a_half_written_change(self):
        def write_then_raise(deps, name, now):
            deps.conn.execute("INSERT INTO jobs(name, last) VALUES ('ghost', 'x')")
            raise RuntimeError("boom")

        orig = schedule.run_job
        schedule.run_job = write_then_raise
        self.addCleanup(setattr, schedule, "run_job", orig)
        self.tg.queue.append({"update_id": 5})
        with contextlib.redirect_stderr(io.StringIO()):
            run.step(self.deps, None)
        self.assertFalse(self.conn.in_transaction)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM jobs WHERE name='ghost'").fetchone())

    # ---------- photo albums: one summary per album ----------

    def _join(self, uid):
        self.feed(msg(ADMIN, "/invite"))
        code = self.tg.sent[-1]["text"].split()[-1]
        self.feed(msg(uid, f"/start {code}"))

    def _album_photo(self, uid, file_id, group="g1"):
        self.tg.files[file_id] = b"img"
        return msg(uid, photo=[{"file_id": file_id}], media_group_id=group)

    def _lead_json(self, name, phone):
        return json.dumps({"leads": [{"name": name, "phone": phone, "consent_ticked": None,
                                      "notes": ""}]})

    def _summaries(self, uid):
        return [m for m in self.tg.sent if m["chat_id"] == uid
                and m["text"].startswith("I found these")]

    def _fresh_llm(self, *leads):
        self.deps.llm = FakeLLM([self._lead_json(n, p) for n, p in leads])

    def _later(self, seconds):
        self.clock[0] += timedelta(seconds=seconds)

    def test_album_in_one_step_waits_for_the_quiet_period_then_sends_one_summary(self):
        self._join(AGENT)
        self._fresh_llm(("One", "91111111"), ("Two", "92222222"), ("Three", "93333333"))
        self.feed(self._album_photo(AGENT, "p1"), self._album_photo(AGENT, "p2"),
                  self._album_photo(AGENT, "p3"))
        self.assertEqual(self._summaries(AGENT), [])  # not yet: more photos may follow
        self._later(2)
        self.feed()
        self.assertEqual(self._summaries(AGENT), [])
        self._later(1)
        self.feed()
        found = self._summaries(AGENT)
        self.assertEqual(len(found), 1)
        for line in ("1. One,", "2. Two,", "3. Three,"):
            self.assertIn(line, found[0]["text"])
        self.assertEqual(found[0]["buttons"], [[("Looks right \u2713", "ok"), ("Cancel", "cancel")]])
        self._later(10)
        self.feed()
        self.assertEqual(len(self._summaries(AGENT)), 1)

    def test_album_split_across_two_steps_gets_one_summary(self):
        self._join(AGENT)
        self._fresh_llm(("One", "91111111"), ("Two", "92222222"), ("Three", "93333333"))
        self.feed(self._album_photo(AGENT, "p1"), self._album_photo(AGENT, "p2"))
        self._later(1)  # within 3 seconds
        self.feed(self._album_photo(AGENT, "p3"))
        self.assertEqual(self._summaries(AGENT), [])
        self._later(3)
        self.feed()
        found = self._summaries(AGENT)
        self.assertEqual(len(found), 1)
        for line in ("1. One,", "2. Two,", "3. Three,"):
            self.assertIn(line, found[0]["text"])
        self.feed()  # nothing new: no repeat
        self.assertEqual(len(self._summaries(AGENT)), 1)

    def test_short_poll_while_a_summary_waits_and_long_poll_otherwise(self):
        self._join(AGENT)
        seen = []
        real = self.tg.get_updates

        def spy(offset, timeout=30):
            seen.append(timeout)
            return real(offset, timeout)

        self.tg.get_updates = spy
        self._fresh_llm(("One", "91111111"))
        self.feed()
        self.assertEqual(seen, [30])
        self.feed(self._album_photo(AGENT, "p1"))
        self.assertEqual(seen[-1], 30)  # polled before the photo was staged
        self.feed()
        self.assertEqual(seen[-1], run.ALBUM_POLL_SECONDS)
        self.assertEqual(run.ALBUM_POLL_SECONDS, 2)
        self._later(3)
        self.feed()  # summary goes out in this step
        self.feed()
        self.assertEqual(seen[-1], 30)

    def _blocked_tg(self):
        class Blocked(FakeTelegram):
            def send(self, chat_id, text, buttons=None):
                raise TelegramError("Forbidden: bot was blocked by the user 91111111")

        blocked = Blocked()
        blocked.queue, blocked.files = self.tg.queue, self.tg.files
        return blocked

    def _payload(self):
        return json.loads(self.conn.execute("SELECT payload FROM pending").fetchone()[0])

    def test_failed_summary_send_keeps_the_flag_and_retries_up_to_3_times(self):
        self._join(AGENT)
        self._fresh_llm(("One", "91111111"))
        self.feed(self._album_photo(AGENT, "p1"))
        self._later(3)
        self.deps.tg = self._blocked_tg()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.feed()
            self.assertTrue(self._payload()["unsent"])  # attempt 1 failed: still flagged
            self.assertTrue(bot.summaries_waiting(self.deps))
            self.feed()
            self.assertTrue(self._payload()["unsent"])  # attempt 2 failed: still flagged
            self.feed()  # attempt 3 failed: given up
        self.assertEqual(err.getvalue().count("TelegramError"), 3)
        self.assertNotIn("blocked by", err.getvalue())
        payload = self._payload()
        self.assertFalse(payload["unsent"])
        self.assertEqual(len(payload["leads"]), 1)  # still waiting, shown with the next lead
        self.assertFalse(bot.summaries_waiting(self.deps))
        with contextlib.redirect_stderr(io.StringIO()) as err2:
            self.feed()  # a fourth flush does not send again
        self.assertEqual(err2.getvalue(), "")

    def test_summary_that_fails_once_is_sent_on_the_retry_and_only_once(self):
        self._join(AGENT)
        self._fresh_llm(("One", "91111111"))
        self.feed(self._album_photo(AGENT, "p1"))
        self._later(3)
        real = self.deps.tg
        self.deps.tg = self._blocked_tg()
        with contextlib.redirect_stderr(io.StringIO()):
            self.feed()
        self.deps.tg = real
        self.feed()
        self.assertEqual(len(self._summaries(AGENT)), 1)
        self.assertFalse(self._payload()["unsent"])
        self.feed()
        self.assertEqual(len(self._summaries(AGENT)), 1)

    def test_single_photo_still_gets_its_summary_immediately(self):
        self._join(AGENT)
        self._fresh_llm(("One", "91111111"))
        self.tg.files["p1"] = b"img"
        self.feed(msg(AGENT, photo=[{"file_id": "p1"}]))
        self.assertEqual(len(self._summaries(AGENT)), 1)
        self.feed()
        self.assertEqual(len(self._summaries(AGENT)), 1)

    def test_contact_in_same_step_summary_covers_all_and_clears_the_flag(self):
        self._join(AGENT)
        self._fresh_llm(("One", "91111111"))
        self.feed(self._album_photo(AGENT, "p1"),
                  msg(AGENT, contact={"first_name": "Two", "phone_number": "92222222"}))
        found = self._summaries(AGENT)
        self.assertEqual(len(found), 1)
        self.assertIn("One,", found[0]["text"])
        self.assertIn("Two,", found[0]["text"])

    def test_other_agents_album_never_appears_in_this_agents_summary(self):
        self._join(AGENT)
        self._join(300)
        self._fresh_llm(("Mine", "91111111"), ("Theirs", "92222222"))
        self.feed(self._album_photo(AGENT, "p1", "g1"), self._album_photo(300, "p2", "g2"))
        self._later(3)
        self.feed()
        mine, theirs = self._summaries(AGENT), self._summaries(300)
        self.assertEqual((len(mine), len(theirs)), (1, 1))
        self.assertIn("Mine", mine[0]["text"])
        self.assertNotIn("Theirs", mine[0]["text"])
        self.assertIn("Theirs", theirs[0]["text"])
        self.assertNotIn("Mine", theirs[0]["text"])

    def test_paused_agents_flagged_row_is_not_sent_a_summary(self):
        from isuite.bot import handle_update
        self._join(AGENT)
        self._fresh_llm(("One", "91111111"))
        handle_update(self.deps, self._album_photo(AGENT, "p1"))  # staged, summary unsent
        self.conn.execute("UPDATE agents SET status='paused'")
        self.conn.commit()
        self.feed()
        self.assertEqual(self._summaries(AGENT), [])

    def test_unreadable_album_photo_still_gets_its_reply(self):
        self._join(AGENT)
        self.deps.llm = FakeLLM(["not json"])
        self.feed(self._album_photo(AGENT, "p1"))
        self.assertEqual(self.tg.sent[-1]["text"], wording.PHOTO_UNREADABLE)

    def test_flush_error_is_logged_by_type_and_does_not_stop_the_loop(self):
        orig = bot.flush_summaries
        bot.flush_summaries = lambda deps: (_ for _ in ()).throw(RuntimeError("secret"))
        self.addCleanup(setattr, bot, "flush_summaries", orig)
        self.tg.queue.append({"update_id": 5})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(run.step(self.deps, None), 6)
        self.assertIn("RuntimeError", err.getvalue())
        self.assertNotIn("secret", err.getvalue())

    def test_dry_run_prints_day_without_env(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run.main(["--dry-run"], env={}), 0)
        text = out.getvalue()
        self.assertIn("Good morning", text)
        self.assertIn("buttons:", text)


if __name__ == "__main__":
    unittest.main()
