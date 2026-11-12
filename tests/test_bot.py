import contextlib
import io
import json
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

from isuite import bot, wording, drafts, roster
from isuite.bot import Deps, handle_update
from isuite.config import Config
from isuite.contacts import LeadDraft, add_or_merge
from isuite.db import connect
from isuite.llm import FakeLLM, LLMError
from isuite.telegram_api import FakeTelegram, TelegramError

NOW = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)  # 10:00 SGT
ADMIN, A, B = 1, 100, 200
LEADS = json.dumps({"leads": [
    {"name": "Tan", "phone": "9123 4567", "consent_ticked": True, "notes": ""},
    {"name": "Lim", "phone": "81234567", "consent_ticked": None, "notes": ""},
]})


STATS = {"leads_captured": 1, "messaged": 1, "followed_up_48h": 100, "replied": 0,
         "booked": 0, "closed": 0, "roadshows": 0}


def msg(uid, text=None, **extra):
    m = {"from": {"id": uid, "first_name": "Sam"}, "chat": {"id": uid, "type": "private"}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"message": m}


def cb(uid, data):
    return {"callback_query": {"id": "cb1", "from": {"id": uid}, "data": data,
                               "message": {"chat": {"id": uid, "type": "private"}}}}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)
        self.tg = FakeTelegram()
        self.llm = FakeLLM([])
        self.cfg = Config("t", "k", ":memory:", {ADMIN})
        self.deps = Deps(self.conn, self.cfg, self.tg, self.llm, lambda: NOW)

    def send(self, update):
        handle_update(self.deps, update)

    def last(self):
        return self.tg.sent[-1]

    def join(self, uid):
        code = roster.create_invite(self.conn, ADMIN, NOW)
        self.send(msg(uid, f"/start {code}"))
        return roster.get_agent(self.conn, uid)

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_stranger_gets_need_invite_and_nothing_stored(self):
        self.send(msg(A, "lead: Tan 91234567"))
        self.assertEqual(self.last()["text"], wording.NEED_INVITE)
        self.assertEqual((self.count("agents"), self.count("contacts"), self.count("pending")),
                         (0, 0, 0))

    def test_bad_or_used_code_gets_need_invite(self):
        self.send(msg(A, "/start NOPE1234"))
        self.assertEqual(self.last()["text"], wording.NEED_INVITE)
        self.send(msg(A, "/start"))
        self.assertEqual(self.last()["text"], wording.NEED_INVITE)
        code = roster.create_invite(self.conn, ADMIN, NOW)
        self.send(msg(A, f"/start {code}"))
        self.send(msg(B, f"/start {code}"))
        self.assertEqual(self.last()["text"], wording.NEED_INVITE)
        self.assertIsNone(roster.get_agent(self.conn, B))

    def test_start_creates_agent_with_plain_welcome(self):
        agent = self.join(A)
        self.assertEqual(agent.status, "team")
        text = self.last()["text"].lower()
        self.assertIn("never your clients", text)
        self.assertIn("responsible for the client details", text)
        self.assertIn("never sends messages for you", text)

    def test_photo_creates_pending_with_buttons(self):
        self.join(A)
        self.tg.files["f2"] = b"big"
        self.llm._responses.append(LEADS)
        self.send(msg(A, photo=[{"file_id": "f1"}, {"file_id": "f2"}]))
        self.assertEqual(self.llm.calls[0]["image"], b"big")
        out = self.last()
        self.assertIn("Tan, 9123 4567, consent ✓", out["text"])
        self.assertIn("Lim, 81234567, consent ?", out["text"])
        self.assertIn(wording.CHECK_CONSENT, out["text"])
        self.assertEqual(out["buttons"], [[("Looks right ✓", "ok"), ("Cancel", "cancel")]])
        self.assertEqual(self.count("pending"), 1)

    def test_photo_bad_json_asks_to_type(self):
        self.join(A)
        self.tg.files["f1"] = b"x"
        self.llm._responses.append("not json")
        self.send(msg(A, photo=[{"file_id": "f1"}]))
        self.assertEqual(self.last()["text"], wording.PHOTO_UNREADABLE)
        self.assertEqual(self.count("pending"), 0)

    def test_photo_zero_leads_asks_to_type(self):
        self.join(A)
        self.tg.files["f1"] = b"x"
        self.llm._responses.append('{"leads": []}')
        self.send(msg(A, photo=[{"file_id": "f1"}]))
        self.assertEqual(self.last()["text"], wording.PHOTO_UNREADABLE)

    def test_photo_with_ai_down_says_so_not_unreadable(self):
        self.join(A)
        self.tg.files["f1"] = b"x"
        self.assertEqual(self.llm._responses, [])  # FakeLLM raises LLMError when empty
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.send(msg(A, photo=[{"file_id": "f1"}]))
        self.assertEqual(self.last()["text"], wording.AI_DOWN)
        self.assertNotEqual(wording.AI_DOWN, wording.PHOTO_UNREADABLE)
        self.assertEqual(err.getvalue(), "isuite ai error: LLMError\n")
        self.assertEqual(self.count("pending"), 0)

    def test_photo_the_ai_rejected_as_an_image_says_unreadable_other_errors_say_ai_down(self):
        class Rejecting:
            def __init__(self, status):
                self.status = status

            def complete(self, *a, **k):
                raise LLMError("Claude API HTTP", status=self.status)

        self.join(A)
        self.tg.files["f1"] = b"x"
        for status, want in ((400, wording.PHOTO_UNREADABLE), (413, wording.PHOTO_UNREADABLE),
                             (500, wording.AI_DOWN), (429, wording.AI_DOWN), (401, wording.AI_DOWN),
                             (None, wording.AI_DOWN)):
            self.deps.llm = Rejecting(status)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.send(msg(A, photo=[{"file_id": "f1"}]))
            self.assertEqual(self.last()["text"], want, status)
            self.assertEqual(err.getvalue(), "isuite ai error: LLMError\n")

    def test_typed_lead_with_ai_down_says_so_not_no_lead(self):
        self.join(A)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.send(msg(A, "lead: Tan 91234567"))
        self.assertEqual(self.last()["text"], wording.AI_DOWN)
        self.assertEqual(err.getvalue(), "isuite ai error: LLMError\n")

    def test_empty_ai_result_keeps_the_old_messages(self):
        self.join(A)
        self.tg.files["f1"] = b"x"
        self.llm._responses.extend(['{"leads": []}', '{"leads": []}'])
        self.send(msg(A, photo=[{"file_id": "f1"}]))
        self.assertEqual(self.last()["text"], wording.PHOTO_UNREADABLE)
        self.send(msg(A, "lead: nothing here"))
        self.assertEqual(self.last()["text"], wording.NO_LEADS_TEXT)

    def test_contact_lead_shows_check_consent_button(self):
        self.join(A)
        self.send(msg(A, contact={"first_name": "Amy", "last_name": "Goh",
                                  "phone_number": "+6591234567"}))
        out = self.last()
        self.assertIn("Amy Goh, +6591234567, consent ?", out["text"])
        self.assertIn(wording.CHECK_CONSENT, out["text"])
        self.assertEqual(out["buttons"][0][0][1], "ok")
        self.send(cb(A, "ok"))
        self.assertEqual(self.conn.execute("SELECT source, consent FROM contacts").fetchone()[:],
                         ("contact", "unknown"))
        self.assertTrue(self.last()["buttons"][0][0][1].startswith("consent:"))

    def test_lead_text_goes_through_extraction(self):
        self.join(A)
        self.llm._responses.append(LEADS)
        self.send(msg(A, "lead: Tan 9123 4567, met at Tampines"))
        self.assertEqual(self.llm.calls[0]["user"], "Tan 9123 4567, met at Tampines")
        self.assertIn("Tan", self.last()["text"])
        self.llm._responses.append("garbage")
        self.send(msg(A, "lead: Tan"))
        self.assertIn("lead: Name 91234567", self.last()["text"])

    def test_new_intake_appends_to_pending(self):
        self.join(A)
        self.send(msg(A, contact={"first_name": "One", "phone_number": "91111111"}))
        self.send(msg(A, contact={"first_name": "Two", "phone_number": "92222222"}))
        self.tg.files["f1"] = b"img"
        self.llm._responses.append(LEADS)
        self.send(msg(A, photo=[{"file_id": "f1"}]))
        self.assertEqual(self.count("pending"), 1)
        summary = self.last()["text"]
        for line in ("1. One, 91111111", "2. Two, 92222222", "3. Tan, 9123 4567", "4. Lim, 81234567"):
            self.assertIn(line, summary)
        self.assertEqual(self.last()["buttons"], [[("Looks right ✓", "ok"), ("Cancel", "cancel")]])
        self.send(cb(A, "ok"))
        self.assertTrue(self.last()["text"].startswith("Saved 4 leads (0 merged)"))
        rows = self.conn.execute("SELECT name, source FROM contacts ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("One", "contact"), ("Two", "contact"),
                                                    ("Tan", "photo"), ("Lim", "photo")])
        self.assertEqual(self.count("pending"), 0)
        self.send(cb(A, "ok"))
        self.assertEqual(self.last()["text"], wording.NOTHING_PENDING)

    def test_cancel_clears_every_appended_lead(self):
        self.join(A)
        self.send(msg(A, contact={"first_name": "One", "phone_number": "91111111"}))
        self.send(msg(A, contact={"first_name": "Two", "phone_number": "92222222"}))
        self.send(cb(A, "cancel"))
        self.assertEqual(self.last()["text"], wording.CANCELLED)
        self.assertEqual((self.count("pending"), self.count("contacts")), (0, 0))
        self.send(msg(A, contact={"first_name": "Three", "phone_number": "93333333"}))
        self.assertNotIn("One", self.last()["text"])
        self.send(cb(A, "ok"))
        self.assertEqual([r[0] for r in self.conn.execute("SELECT name FROM contacts")], ["Three"])

    def test_pending_payload_is_redacted(self):
        self.join(A)
        self.send(msg(A, contact={"first_name": "Tan", "last_name": "S1234567D",
                                  "phone_number": "91234567"}))
        self.llm._responses.append(json.dumps({"leads": [
            {"name": "Lim", "phone": "81234567", "notes": "policy no 00 1234 5678"}]}))
        self.send(msg(A, "lead: Lim 81234567"))
        payload = self.conn.execute("SELECT payload FROM pending").fetchone()[0]
        for secret in ("S1234567D", "1234 5678"):
            self.assertNotIn(secret, payload)
            self.assertNotIn(secret, self.last()["text"])

    def test_roadshow_text_is_redacted(self):
        self.join(A)
        self.send(msg(A, "roadshow Tampines S1234567D"))
        detail = self.conn.execute("SELECT detail FROM activity WHERE kind='roadshow'").fetchone()[0]
        self.assertNotIn("S1234567D", detail)
        self.assertNotIn("S1234567D", self.last()["text"])

    def test_roadshow_logs_activity_and_sets_day(self):
        agent = self.join(A)
        self.send(msg(A, "roadshow Sat Tampines"))
        self.assertEqual(roster.get_agent(self.conn, A).roadshow_on, "2026-11-10")
        row = self.conn.execute("SELECT kind, agent_id FROM activity").fetchone()
        self.assertEqual((row["kind"], row["agent_id"]), ("roadshow", agent.id))

    def test_roadshow_needs_the_word_on_its_own(self):
        self.join(A)
        self.send(msg(A, "roadshows are tiring"))
        self.assertEqual(self.last()["text"], wording.HELP_UNKNOWN)
        self.assertIsNone(roster.get_agent(self.conn, A).roadshow_on)
        self.send(msg(A, "Roadshow"))
        self.assertEqual(self.last()["text"], wording.roadshow_on(""))
        self.send(msg(A, "roadshow\tTampines"))
        self.assertEqual(self.last()["text"], wording.roadshow_on("Tampines"))

    def test_roadshow_accepts_a_colon_or_dash(self):
        self.join(A)
        for text, place in (("roadshow: Bedok", "Bedok"), ("Roadshow-Bedok", "Bedok"),
                            ("roadshow - Bedok Mall", "Bedok Mall"), ("ROADSHOW :Bedok", "Bedok"),
                            ("roadshow:", ""), ("roadshow Sat Tampines", "Sat Tampines")):
            self.send(msg(A, text))
            self.assertEqual(self.last()["text"], wording.roadshow_on(place), text)
        self.send(msg(A, "roadshows: are tiring"))
        self.assertEqual(self.last()["text"], wording.HELP_UNKNOWN)

    # ---------- a photo sent "as a file" ----------

    def _doc(self, mime="image/png", size=1000, file_id="d1", **extra):
        return msg(A, document={"file_id": file_id, "mime_type": mime, "file_size": size}, **extra)

    def test_image_document_is_read_like_a_photo_with_its_own_type(self):
        self.join(A)
        for mime in ("image/jpeg", "image/png", "image/webp"):
            self.tg.files["d1"] = b"docbytes"
            self.llm._responses.append(LEADS)
            self.send(self._doc(mime))
            self.assertEqual(self.llm.calls[-1]["image"], b"docbytes")
            self.assertEqual(self.llm.calls[-1]["image_type"], mime)
            self.assertIn("Tan, 9123 4567", self.last()["text"])
        self.assertEqual(self.llm.calls[0]["image_type"], "image/jpeg")

    def test_plain_photo_still_says_jpeg(self):
        self.join(A)
        self.tg.files["f1"] = b"x"
        self.llm._responses.append(LEADS)
        self.send(msg(A, photo=[{"file_id": "f1"}]))
        self.assertEqual(self.llm.calls[0]["image_type"], "image/jpeg")

    def test_document_over_5mb_is_refused_without_downloading(self):
        self.join(A)
        asked = []
        self.tg.download_file = lambda fid: asked.append(fid)
        self.send(self._doc(size=5 * 1024 * 1024 + 1))
        self.assertEqual(self.last()["text"], wording.FILE_TOO_BIG)
        self.assertEqual((asked, self.llm.calls, self.count("pending")), ([], [], 0))
        self.tg.files["d1"] = b"ok"
        del self.tg.download_file
        self.llm._responses.append(LEADS)
        self.send(self._doc(size=5 * 1024 * 1024))  # exactly 5 MB is fine
        self.assertIn("Tan", self.last()["text"])

    def test_document_with_a_missing_or_odd_file_size_is_refused_without_downloading(self):
        self.join(A)
        asked = []
        self.tg.download_file = lambda fid: asked.append(fid)
        for doc in ({"file_id": "d1", "mime_type": "image/png"},
                    {"file_id": "d1", "mime_type": "image/png", "file_size": "100"},
                    {"file_id": "d1", "mime_type": "image/png", "file_size": None},
                    {"file_id": "d1", "mime_type": "image/png", "file_size": 1.5}):
            self.send(msg(A, document=doc))
            self.assertEqual(self.last()["text"], wording.FILE_TOO_BIG, doc)
        self.assertEqual((asked, self.llm.calls, self.count("pending")), ([], [], 0))

    def test_photo_takes_the_largest_size_within_5mb(self):
        self.join(A)
        big = 5 * 1024 * 1024 + 1
        asked = []
        self.tg.files.update({"s": b"small", "m": b"mid", "l": b"large"})
        real = self.tg.download_file
        self.tg.download_file = lambda fid: (asked.append(fid), real(fid))[1]
        self.llm._responses.append(LEADS)
        self.send(msg(A, photo=[{"file_id": "s", "file_size": 100},
                                {"file_id": "m", "file_size": 5 * 1024 * 1024},
                                {"file_id": "l", "file_size": big}]))
        self.assertEqual(asked, ["m"])
        self.assertEqual(self.llm.calls[-1]["image"], b"mid")
        self.llm._responses.append(LEADS)  # sizes without file_size count as fine
        asked.clear()
        self.send(msg(A, photo=[{"file_id": "s"}, {"file_id": "l"}]))
        self.assertEqual(asked, ["l"])

    def test_photo_with_every_size_too_big_is_refused_without_downloading(self):
        self.join(A)
        asked = []
        self.tg.download_file = lambda fid: asked.append(fid)
        big = 5 * 1024 * 1024 + 1
        self.send(msg(A, photo=[{"file_id": "a", "file_size": big},
                                {"file_id": "b", "file_size": big + 5}]))
        self.assertEqual(self.last()["text"], wording.FILE_TOO_BIG)
        self.assertEqual((asked, self.llm.calls, self.count("pending")), ([], [], 0))

    def test_other_documents_get_a_short_pointer(self):
        self.join(A)
        for doc in ({"file_id": "d", "mime_type": "application/pdf", "file_size": 10},
                    {"file_id": "d"}, {"file_id": "d", "mime_type": "image/gif"}):
            self.send(msg(A, document=doc))
            self.assertEqual(self.last()["text"], wording.FILE_NOT_PHOTO)
        self.assertEqual((self.llm.calls, self.count("pending")), ([], 0))

    def test_image_is_never_written_to_the_database(self):
        self.join(A)
        self.tg.files["d1"] = b"UNIQUEIMAGEBYTES"
        self.llm._responses.append(LEADS)
        self.send(self._doc())
        dump = "\n".join(self.conn.iterdump())
        self.assertNotIn("UNIQUEIMAGEBYTES", dump)

    def test_document_in_an_album_waits_for_the_flush(self):
        self.join(A)
        self.tg.files["d1"] = b"x"
        self.llm._responses.append(LEADS)
        before = len(self.tg.sent)
        self.send(self._doc(media_group_id="g"))
        self.assertEqual(len(self.tg.sent), before)
        self.assertTrue(json.loads(self.conn.execute("SELECT payload FROM pending")
                                   .fetchone()[0])["unsent"])

    def test_export_after_the_purge_says_no_contacts(self):
        agent = self.join(A)
        add_or_merge(self.conn, agent.id, LeadDraft("Tan", "91234567", "unknown"),
                     "text", NOW.date(), NOW)
        self.conn.execute("UPDATE agents SET status='paused', paused_on='2026-10-11'")
        self.conn.commit()  # exactly 30 days before NOW: still held
        roster.purge_paused(self.conn, NOW.date())
        self.send(msg(A, "/export"))
        self.assertIn("Tan", self.last()["text"])
        self.conn.execute("UPDATE agents SET paused_on='2026-10-10'")  # 31 days
        self.conn.commit()
        roster.purge_paused(self.conn, NOW.date())
        self.send(msg(A, "/export"))
        self.assertEqual(self.last()["text"], wording.NO_CONTACTS)
        self.assertEqual(roster.get_agent(self.conn, A).status, "paused")

    def test_ok_saves_and_merges(self):
        agent = self.join(A)
        add_or_merge(self.conn, agent.id, LeadDraft("Tan", "91234567", "unknown"),
                     "text", NOW.date(), NOW)
        self.llm._responses.append(LEADS)
        self.send(msg(A, "lead: Tan 9123 4567, Lim 81234567"))
        self.send(cb(A, "ok"))
        self.assertEqual(self.last()["text"], "Saved 2 leads (1 merged)")
        self.assertEqual(self.count("contacts"), 2)
        self.assertEqual(self.count("pending"), 0)
        self.assertEqual(self.tg.answered[-1]["callback_id"], "cb1")

    def test_cancel_clears_pending_and_empty_cases(self):
        self.join(A)
        self.send(cb(A, "ok"))
        self.assertEqual(self.last()["text"], wording.NOTHING_PENDING)
        self.send(msg(A, contact={"first_name": "One", "phone_number": "91111111"}))
        self.send(cb(A, "cancel"))
        self.assertEqual((self.count("pending"), self.count("contacts")), (0, 0))
        self.send(cb(A, "cancel"))
        self.assertEqual(self.last()["text"], wording.NOTHING_PENDING)

    def _contact(self, uid):
        agent = self.join(uid)
        cid, _ = add_or_merge(self.conn, agent.id, LeadDraft("Tan", "91234567", "unknown"),
                              "text", NOW.date(), NOW)
        return cid

    def test_outcome_and_consent_callbacks(self):
        cid = self._contact(A)
        self.send(cb(A, f"out:{cid}:booked"))
        self.assertEqual(self.last()["text"], wording.outcome_saved("booked"))
        self.assertEqual(self.conn.execute("SELECT stage FROM contacts").fetchone()[0], "booked")
        self.send(cb(A, f"consent:{cid}"))
        self.assertEqual(self.conn.execute("SELECT consent FROM contacts").fetchone()[0], "yes")

    def test_recorded_no_consent_cannot_be_flipped(self):
        self.join(A)
        self.tg.files["f1"] = b"img"
        self.llm._responses.append(json.dumps({"leads": [
            {"name": "Tan", "phone": "91234567", "consent_ticked": False, "notes": ""}]}))
        self.send(msg(A, photo=[{"file_id": "f1"}]))
        self.send(cb(A, "ok"))
        cid = self.conn.execute("SELECT id FROM contacts").fetchone()[0]
        # the same person forwarded later: merged, consent stays no, no button offered
        self.send(msg(A, contact={"first_name": "Tan", "phone_number": "+6591234567"}))
        self.send(cb(A, "ok"))
        self.assertEqual(self.last()["text"], "Saved 1 lead (1 merged)")
        self.assertIsNone(self.last()["buttons"])
        # an old consent button changes nothing and says why
        self.send(cb(A, f"consent:{cid}"))
        self.assertEqual(self.last()["text"], wording.CONSENT_KEPT_NO)
        self.assertEqual(self.conn.execute("SELECT consent FROM contacts").fetchone()[0], "no")
        self.send(msg(A, "/today"))
        self.assertEqual(self.last()["text"], wording.NOTHING_TODAY)

    def test_consent_button_only_for_stored_unknown(self):
        agent = self.join(A)
        add_or_merge(self.conn, agent.id, LeadDraft("Tan", "91234567", "yes"), "text",
                     NOW.date(), NOW)
        self.send(msg(A, contact={"first_name": "Tan", "phone_number": "91234567"}))
        self.send(msg(A, contact={"first_name": "Lim", "phone_number": "81234567"}))
        self.send(msg(A, contact={"first_name": "Lim", "phone_number": "81234567"}))
        self.send(cb(A, "ok"))
        buttons = self.last()["buttons"]
        lim = self.conn.execute("SELECT id FROM contacts WHERE name='Lim'").fetchone()[0]
        self.assertEqual(buttons, [[(wording.consent_button("Lim"), f"consent:{lim}")]])

    def test_double_sent_tap_answers_already_noted(self):
        cid = self._contact(A)
        self.send(cb(A, f"out:{cid}:messaged"))
        self.assertEqual(self.last()["text"], wording.outcome_saved("messaged"))
        self.send(cb(A, f"out:{cid}:messaged"))
        self.assertEqual(self.last()["text"], wording.ALREADY_NOTED)
        row = self.conn.execute("SELECT chase_step, next_due_on FROM contacts").fetchone()
        self.assertEqual(tuple(row), (1, "2026-11-13"))

    def test_outcome_callback_for_other_agents_contact_is_refused(self):
        cid = self._contact(A)
        self.join(B)
        self.send(cb(B, f"out:{cid}:booked"))
        self.assertEqual(self.last()["text"], wording.NOT_FOUND)
        self.send(cb(B, f"consent:{cid}"))
        self.assertEqual(self.last()["text"], wording.NOT_FOUND)
        row = self.conn.execute("SELECT stage, consent FROM contacts WHERE id=?", (cid,)).fetchone()
        self.assertEqual((row["stage"], row["consent"]), ("new", "unknown"))

    def test_malformed_callback_is_polite(self):
        self.join(A)
        for data in ("out:abc:booked", "out:1:bogus", "zzz", "out:1"):
            self.send(cb(A, data))
            self.assertEqual(self.last()["text"], wording.NOT_FOUND)

    def test_export_lists_own_contacts_only(self):
        a = self.join(A)
        b = self.join(B)
        add_or_merge(self.conn, a.id, LeadDraft("Tan", "91234567", "yes"), "text", NOW.date(), NOW)
        add_or_merge(self.conn, b.id, LeadDraft("Other", "81234567", "yes"), "text", NOW.date(), NOW)
        self.send(msg(A, "/export"))
        self.assertEqual(self.last()["text"], "Tan, +6591234567, new")
        self.conn.execute("DELETE FROM activity WHERE agent_id=?", (a.id,))
        self.conn.execute("DELETE FROM contacts WHERE agent_id=?", (a.id,))
        self.send(msg(A, "/export"))
        self.assertEqual(self.last()["text"], wording.NO_CONTACTS)

    def test_today_sends_brief_and_week_sends_numbers(self):
        a = self.join(A)
        self.send(msg(A, "/today"))
        self.assertEqual(self.last()["text"], wording.NOTHING_TODAY)
        add_or_merge(self.conn, a.id, LeadDraft("Tan", "91234567", "unknown"), "text", NOW.date(), NOW)
        self.send(msg(A, "/today"))
        self.assertEqual(self.tg.sent[-2]["text"], wording.brief_header("Sam", 1, 10))
        self.assertIn("Tan", self.last()["text"])
        self.send(msg(A, "/week"))
        self.assertEqual(self.last()["text"], wording.week_summary(
            {"leads_captured": 1, "messaged": 0, "followed_up_48h": 0, "replied": 0,
             "booked": 0, "closed": 0, "roadshows": 0}))

    def test_non_admin_team_refused(self):
        self.join(A)
        for c in ("/team", "/invite", "/left 1"):
            self.send(msg(A, c))
            self.assertEqual(self.last()["text"], wording.ADMIN_ONLY)

    def test_admin_commands(self):
        self.send(msg(ADMIN, "/team"))
        self.assertEqual(self.last()["text"], wording.NO_TEAM)
        self.send(msg(ADMIN, "/invite"))
        code = self.last()["text"].split()[-1]
        self.send(msg(A, f"/start {code}"))
        self.assertIsNotNone(roster.get_agent(self.conn, A))
        self.send(msg(ADMIN, f"/left {A}"))
        self.assertEqual(roster.get_agent(self.conn, A).status, "paused")
        self.send(msg(ADMIN, "/left 999"))
        self.assertEqual(self.last()["text"], wording.LEFT_UNKNOWN)
        self.send(msg(ADMIN, "/left x"))
        self.assertEqual(self.last()["text"], wording.LEFT_USAGE)

    def set_status(self, uid, status):
        self.conn.execute("UPDATE agents SET status=? WHERE telegram_id=?", (status, uid))
        self.conn.commit()

    def test_start_from_paused_agent_gets_paused_active_gets_welcome(self):
        self.join(A)
        self.send(msg(A, "/start"))
        self.assertEqual(self.last()["text"], wording.WELCOME)
        self.set_status(A, "paused")
        self.send(msg(A, "/start"))
        self.assertEqual(self.last()["text"], wording.paused(None))
        code = roster.create_invite(self.conn, ADMIN, NOW)
        self.send(msg(A, f"/start {code}"))
        self.assertEqual(self.last()["text"], wording.paused(None))

    def test_paused_agent_today_and_week_get_paused_and_no_contacts(self):
        a = self.join(A)
        add_or_merge(self.conn, a.id, LeadDraft("Tan", "91234567", "yes"), "text", NOW.date(), NOW)
        self.set_status(A, "paused")
        n = len(self.tg.sent)
        for c in ("/today", "/week"):
            self.send(msg(A, c))
            self.assertEqual(self.last()["text"], wording.paused(None))
        self.assertEqual(len(self.tg.sent), n + 2)
        for m in self.tg.sent[n:]:
            self.assertNotIn("Tan", m["text"])
            self.assertIsNone(m["buttons"])

    def test_admin_who_is_not_an_agent_gets_admin_no_agent(self):
        self.tg.files["p1"] = b"img"
        for update in (msg(ADMIN, "/today"), msg(ADMIN, "/week"),
                       msg(ADMIN, photo=[{"file_id": "p1"}])):
            self.send(update)
            self.assertEqual(self.last()["text"], wording.ADMIN_NO_AGENT)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual((self.count("contacts"), self.count("pending")), (0, 0))

    def test_left_pauses_at_once_and_tells_the_agent_the_last_day(self):
        self.join(A)
        self.send(msg(ADMIN, f"/left {A}"))
        agent = roster.get_agent(self.conn, A)
        self.assertEqual((agent.status, agent.paused_on, agent.trial_ends_on),
                         ("paused", "2026-11-10", None))
        self.assertEqual(self.last()["text"], wording.left_done("Sam"))
        self.assertIn("30 days", self.last()["text"])
        told = [m for m in self.tg.sent if m["chat_id"] == A][-1]["text"]  # the agent is told too
        self.assertEqual(told, wording.paused("2026-12-10"))
        self.assertNotIn("trial", self.last()["text"].lower())
        self.send(msg(A, "/today"))  # extras stop at once
        self.assertEqual(self.last()["text"], wording.paused("2026-12-10"))
        self.assertIn("10 Dec 2026", self.last()["text"])
        self.assertIn("open edition", self.last()["text"])

    def test_admin_is_told_plainly_when_the_leaver_could_not_be_reached(self):
        self.join(A)

        class Unreachable(FakeTelegram):
            def send(self, chat_id, text, buttons=None):
                if chat_id == A:
                    raise TelegramError("blocked")
                return super().send(chat_id, text, buttons)

        self.tg = self.deps.tg = Unreachable()
        with contextlib.redirect_stderr(io.StringIO()):
            self.send(msg(ADMIN, f"/left {A}"))
        self.assertEqual(roster.get_agent(self.conn, A).status, "paused")  # still paused
        text = self.last()["text"]
        self.assertEqual(text, wording.left_done("Sam", told=False))
        self.assertNotIn("have been told", text)
        self.assertIn("could not be reached", text)
        self.assertIn("30 days", text)
        self.assertIn("have been told", wording.left_done("Sam"))

    def test_left_twice_does_not_move_the_pause_date(self):
        self.join(A)
        self.send(msg(ADMIN, f"/left {A}"))
        self.conn.execute("UPDATE agents SET paused_on='2026-11-01' WHERE telegram_id=?", (A,))
        self.send(msg(ADMIN, f"/left {A}"))
        self.assertEqual(roster.get_agent(self.conn, A).paused_on, "2026-11-01")
        self.assertEqual(self.last()["text"], wording.left_not_on_team("Sam", "paused"))
        self.assertIn("Nothing changed", self.last()["text"])

    def test_old_trial_row_is_treated_like_a_paused_one(self):
        self.join(A)
        self.set_status(A, "trial")
        self.send(msg(A, "/today"))
        self.assertEqual(self.last()["text"], wording.paused(None))
        self.send(msg(A, "/export"))
        self.assertEqual(self.last()["text"], wording.NO_CONTACTS)

    def test_left_on_paused_agent_leaves_them_paused(self):
        self.join(A)
        self.set_status(A, "paused")
        self.send(msg(ADMIN, f"/left {A}"))
        agent = roster.get_agent(self.conn, A)
        self.assertEqual((agent.status, agent.trial_ends_on), ("paused", None))
        self.assertEqual(self.last()["text"], wording.left_not_on_team("Sam", "paused"))

    def test_rejoin_from_left_trial_and_paused_restores_team(self):
        self.join(A)
        for how in ("left", "trial", "paused"):
            self.send(msg(ADMIN, f"/left {A}"))
            if how in ("trial", "paused"):
                self.set_status(A, how)
            self.send(msg(ADMIN, f"/rejoin {A}"))
            agent = roster.get_agent(self.conn, A)
            self.assertEqual((agent.status, agent.left_at, agent.trial_ends_on),
                             ("team", None, None), how)
            self.assertEqual(self.last()["text"], wording.rejoin_done("Sam"))
            self.conn.execute("UPDATE agents SET status='team' WHERE telegram_id=?", (A,))
            self.conn.commit()
        self.send(msg(ADMIN, f"/left {A}"))
        self.set_status(A, "paused")
        self.send(msg(ADMIN, f"/rejoin {A}"))
        self.send(msg(ADMIN, "/team"))
        self.assertIn("Sam", self.last()["text"])
        self.tg.sent.clear()
        brief_deps_agent = roster.get_agent(self.conn, A)
        self.send(msg(A, "/today"))
        self.assertTrue(self.tg.sent)
        self.assertNotEqual(self.tg.sent[0]["text"], wording.paused(None))
        self.assertEqual(brief_deps_agent.status, "team")

    def test_rejoin_when_already_team_unknown_or_bad_id(self):
        self.join(A)
        self.send(msg(ADMIN, f"/rejoin {A}"))
        self.assertEqual(self.last()["text"], wording.rejoin_already("Sam"))
        self.send(msg(ADMIN, "/rejoin 999"))
        self.assertEqual(self.last()["text"], wording.LEFT_UNKNOWN)
        self.send(msg(ADMIN, "/rejoin x"))
        self.assertEqual(self.last()["text"], wording.REJOIN_USAGE)
        self.send(msg(ADMIN, "/rejoin " + "9" * 40))
        self.assertEqual(self.last()["text"], wording.LEFT_UNKNOWN)

    def test_rejoin_and_agents_refused_for_non_admin(self):
        self.join(A)
        self.join(B)
        self.send(msg(ADMIN, f"/left {B}"))
        for c in (f"/rejoin {B}", "/agents"):
            self.send(msg(A, c))
            self.assertEqual(self.last()["text"], wording.ADMIN_ONLY)
        self.assertEqual(roster.get_agent(self.conn, B).status, "paused")
        self.set_status(A, "paused")
        for c in (f"/rejoin {B}", "/agents"):
            self.send(msg(A, c))
            self.assertEqual(self.last()["text"], wording.paused(None))
        self.assertEqual(roster.get_agent(self.conn, B).status, "paused")

    def test_agents_lists_every_status_for_admins_only(self):
        self.send(msg(ADMIN, "/agents"))
        self.assertEqual(self.last()["text"], wording.NO_AGENTS)
        self.join(A)
        self.join(B)
        self.conn.execute("INSERT INTO agents(telegram_id, name, status) VALUES (300,'Cy','left')")
        self.conn.execute("INSERT INTO agents(telegram_id, name, status) VALUES (400,'Di','paused')")
        self.conn.commit()
        add_or_merge(self.conn, roster.get_agent(self.conn, A).id,
                     LeadDraft("Tan", "91234567", "yes"), "text", NOW.date(), NOW)
        self.send(msg(ADMIN, f"/left {B}"))
        self.send(msg(ADMIN, "/agents"))
        lines = self.last()["text"].split("\n")
        self.assertEqual(len(lines), 4)
        self.assertIn("100", lines[0])
        self.assertIn("team", lines[0])
        self.assertIn("200", lines[1])
        self.assertIn("paused", lines[1])
        self.assertIn("data kept until 2026-12-10", lines[1])
        self.assertNotIn("trial", self.last()["text"])
        self.assertIn("left", lines[2])
        self.assertIn("paused", lines[3])
        self.assertNotIn("Tan", self.last()["text"])
        self.assertNotIn("91234567", self.last()["text"])

    def test_paused_admin_can_still_use_admin_commands(self):
        self.join(A)
        admin_agent = self.join(ADMIN)
        self.send(msg(ADMIN, f"/left {A}"))
        self.set_status(ADMIN, "paused")
        self.send(msg(ADMIN, f"/rejoin {A}"))
        self.assertEqual(roster.get_agent(self.conn, A).status, "team")
        for c in ("/agents", "/team", "/invite", f"/left {A}"):
            self.send(msg(ADMIN, c))
            self.assertNotIn(self.last()["text"], (wording.paused(None), wording.ADMIN_ONLY), c)
        self.send(msg(ADMIN, "/today"))
        self.assertEqual(self.last()["text"], wording.paused(None))
        self.assertEqual(admin_agent.status, "team")

    def test_paused_agent_only_gets_export(self):
        a = self.join(A)
        add_or_merge(self.conn, a.id, LeadDraft("Tan", "91234567", "yes"), "text", NOW.date(), NOW)
        self.conn.execute("UPDATE agents SET status='paused' WHERE id=?", (a.id,))
        self.send(msg(A, "lead: Tan 91234567"))
        self.assertEqual(self.last()["text"], wording.paused(None))
        self.send(cb(A, "ok"))
        self.assertEqual(self.last()["text"], wording.paused(None))
        self.send(msg(A, "/export"))
        self.assertIn("Tan", self.last()["text"])
        self.assertEqual(self.llm.calls, [])

    def test_unknown_message_gets_help_and_other_updates_ignored(self):
        self.join(A)
        self.send(msg(A, "hello"))
        self.assertEqual(self.last()["text"], wording.HELP_UNKNOWN)
        n = len(self.tg.sent)
        self.send({"edited_message": {}})
        self.assertEqual(len(self.tg.sent), n)

    def test_handler_never_raises(self):
        self.join(A)

        class Boom:
            def complete(self, *a, **k):
                raise RuntimeError("boom")

        self.deps.llm = Boom()
        self.send(msg(A, contact={"first_name": "x"}))  # no llm needed
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.send(msg(A, "lead: Tan 91234567"))
        self.assertEqual(self.last()["text"], wording.SORRY)
        self.assertIn("RuntimeError", err.getvalue())

    def test_failed_handler_leaves_no_half_write_for_the_next_commit(self):
        a = self.join(A)

        def write_then_raise(deps, agent, chat_id):
            deps.conn.execute(
                "INSERT INTO activity (agent_id, contact_id, kind, at) VALUES (?,NULL,'ghost',?)",
                (agent.id, NOW.isoformat()))
            deps.conn.execute("UPDATE agents SET status='paused' WHERE id=?", (agent.id,))
            raise RuntimeError("after the write")

        with mock.patch("isuite.bot._cmd_week", write_then_raise):
            with contextlib.redirect_stderr(io.StringIO()):
                self.send(msg(A, "/week"))
        self.assertEqual(self.last()["text"], wording.SORRY)
        self.assertFalse(self.conn.in_transaction)
        self.send(msg(ADMIN, "/invite"))  # a later, successful handler commits
        self.assertEqual(self.count("activity"), 0)
        # re-read from the database: the half-written 'paused' must be gone, not just absent from
        # the stale object (this assertion fails if the rollback is removed)
        self.assertEqual(roster.get_agent(self.conn, A).status, "team")

    def test_failing_rollback_does_not_hide_the_apology(self):
        self.join(A)

        class BadConn:
            def __init__(self, real):
                self.real = real

            def __getattr__(self, name):
                return getattr(self.real, name)

            def rollback(self):
                raise RuntimeError("rollback broke")

        def boom(deps, agent, chat_id):
            raise RuntimeError("x")

        self.deps.conn = BadConn(self.conn)
        with mock.patch("isuite.bot._cmd_week", boom), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.send(msg(A, "/week"))
        self.assertEqual(self.last()["text"], wording.SORRY)
        self.assertIn("RuntimeError", err.getvalue())
        self.assertNotIn("rollback broke", err.getvalue())

    def test_stderr_has_type_only_not_message(self):
        self.join(A)

        class Boom:
            def complete(self, *a, **k):
                raise RuntimeError("SECRET-MARKER Tan 91234567")

        self.deps.llm = Boom()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.send(msg(A, "lead: Tan 91234567"))
        self.assertIn("RuntimeError", err.getvalue())
        self.assertNotIn("SECRET-MARKER", err.getvalue())

    def test_group_chats_are_ignored_completely(self):
        a = self.join(A)
        add_or_merge(self.conn, a.id, LeadDraft("Tan", "91234567", "yes"), "text", NOW.date(), NOW)
        n = len(self.tg.sent)
        group = {"id": -500, "type": "group"}
        self.tg.files["f1"] = b"x"
        self.llm._responses.append(LEADS)
        for text, extra in (("/export", {}), (None, {"photo": [{"file_id": "f1"}]}),
                            ("hello", {}), ("/invite", {})):
            u = msg(A, text, **extra)
            u["message"]["chat"] = group
            self.send(u)
        stranger = msg(999, "hi")
        stranger["message"]["chat"] = group
        self.send(stranger)
        self.assertEqual(len(self.tg.sent), n)
        self.assertEqual(self.count("pending"), 0)
        self.assertEqual(self.llm.calls, [])
        # callback from a group message changes nothing
        self.send(msg(A, contact={"first_name": "One", "phone_number": "91111111"}))
        c = cb(A, "ok")
        c["callback_query"]["message"]["chat"] = group
        n = len(self.tg.sent)
        self.send(c)
        self.assertEqual(len(self.tg.sent), n)
        self.assertEqual((self.count("pending"), self.count("contacts")), (1, 1))

    def test_ok_is_atomic_pending_kept_on_failure(self):
        self.join(A)
        three = json.dumps({"leads": [
            {"name": "A", "phone": "91111111"}, {"name": "B", "phone": "92222222"},
            {"name": "C", "phone": "93333333"}]})
        self.llm._responses.append(three)
        self.send(msg(A, "lead: A 91111111, B 92222222, C 93333333"))
        import isuite.bot as botmod
        real = botmod.contacts.add_or_merge
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("db hiccup")
            return real(*a, **k)

        err = io.StringIO()
        with mock.patch.object(botmod.contacts, "add_or_merge", flaky), \
                contextlib.redirect_stderr(err):
            self.send(cb(A, "ok"))
        self.assertEqual(self.last()["text"], wording.SORRY)
        self.assertEqual(self.count("pending"), 1)
        self.send(cb(A, "ok"))
        self.assertEqual(self.last()["text"], "Saved 3 leads (1 merged)")
        self.assertEqual(self.count("contacts"), 3)
        self.assertEqual(self.count("pending"), 0)

    def test_failing_answer_callback_does_not_drop_the_tap(self):
        self.join(A)
        self.send(msg(A, contact={"first_name": "One", "phone_number": "91111111"}))

        def late(callback_id, text=""):
            raise TelegramError("query is too old")

        self.tg.answer_callback = late
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.send(cb(A, "ok"))
        self.assertEqual(self.count("contacts"), 1)
        self.assertTrue(self.last()["text"].startswith("Saved 1 lead"))
        self.assertEqual(err.getvalue(), "isuite error: TelegramError\n")
        group = cb(A, "ok")
        group["callback_query"]["message"]["chat"] = {"id": -5, "type": "group"}
        with contextlib.redirect_stderr(io.StringIO()):
            self.send(group)  # group taps are still ignored, without a crash

    # ---------- used buttons come off the message ----------

    def test_looks_right_takes_the_buttons_off_the_tapped_message(self):
        self.join(A)
        self.send(msg(A, contact={"first_name": "One", "phone_number": "91111111"}))
        tap = cb(A, "ok")
        tap["callback_query"]["message"]["message_id"] = 77
        self.send(tap)
        self.assertEqual(self.tg.cleared, [{"chat_id": tap["callback_query"]["message"]["chat"]["id"],
                                            "message_id": 77}])
        self.assertEqual(self.count("contacts"), 1)

    def test_cancel_takes_the_buttons_off_too(self):
        self.join(A)
        self.send(msg(A, contact={"first_name": "One", "phone_number": "91111111"}))
        tap = cb(A, "cancel")
        tap["callback_query"]["message"]["message_id"] = 78
        self.send(tap)
        self.assertEqual([c["message_id"] for c in self.tg.cleared], [78])
        self.assertEqual(self.count("contacts"), 0)

    def test_consent_and_outcome_taps_keep_their_buttons(self):
        self.join(A)
        self.send(msg(A, contact={"first_name": "One", "phone_number": "91111111"}))
        self.send(cb(A, "ok"))
        self.tg.cleared.clear()
        cid = self.deps.conn.execute("SELECT id FROM contacts").fetchone()["id"]
        for data in (f"consent:{cid}", f"out:{cid}:messaged"):
            tap = cb(A, data)
            tap["callback_query"]["message"]["message_id"] = 80
            self.send(tap)
        self.assertEqual(self.tg.cleared, [])

    def test_a_failed_save_keeps_the_buttons_so_the_agent_can_retry(self):
        self.join(A)
        self.send(msg(A, contact={"first_name": "One", "phone_number": "91111111"}))
        real = bot._confirm_pending

        def boom(*a, **k):
            raise RuntimeError("db is busy")

        bot._confirm_pending = boom
        self.addCleanup(setattr, bot, "_confirm_pending", real)
        tap = cb(A, "ok")
        tap["callback_query"]["message"]["message_id"] = 82
        with contextlib.redirect_stderr(io.StringIO()):
            self.send(tap)
        self.assertEqual(self.tg.cleared, [])
        self.assertEqual(self.count("pending"), 1)

    def test_a_refused_button_removal_does_not_drop_the_save(self):
        self.join(A)
        self.send(msg(A, contact={"first_name": "One", "phone_number": "91111111"}))

        def refuse(chat_id, message_id):
            raise TelegramError("message can't be edited", code=400)

        self.tg.clear_buttons = refuse
        tap = cb(A, "ok")
        tap["callback_query"]["message"]["message_id"] = 79
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.send(tap)
        self.assertEqual(self.count("contacts"), 1)
        self.assertTrue(self.last()["text"].startswith("Saved 1 lead"))
        self.assertEqual(err.getvalue(), "isuite error: TelegramError\n")

    def test_a_tap_with_no_usable_message_id_still_works(self):
        self.join(A)
        for bad in (None, "77", True, 1.5):
            self.send(msg(A, contact={"first_name": "One", "phone_number": "91111111"}))
            tap = cb(A, "ok")
            if bad is None:
                tap["callback_query"]["message"].pop("message_id", None)
            else:
                tap["callback_query"]["message"]["message_id"] = bad
            self.send(tap)
        self.assertEqual(self.tg.cleared, [])
        self.assertEqual(self.count("contacts"), 1)

    def test_a_stranger_or_paused_agent_cannot_clear_buttons(self):
        tap = cb(999999, "ok")
        tap["callback_query"]["message"]["message_id"] = 81
        self.send(tap)
        self.assertEqual(self.tg.cleared, [])

    def test_huge_ids_give_not_found(self):
        self.join(A)
        self.send(cb(A, "out:" + "9" * 40 + ":booked"))
        self.assertEqual(self.last()["text"], wording.NOT_FOUND)
        self.send(cb(A, "consent:" + "9" * 40))
        self.assertEqual(self.last()["text"], wording.NOT_FOUND)
        self.send(msg(ADMIN, "/left " + "9" * 40))
        self.assertEqual(self.last()["text"], wording.LEFT_UNKNOWN)

    # ---------- waiting lists expire after 24 hours ----------

    def _later(self, **delta):
        self.deps.now = lambda: NOW + timedelta(**delta)

    def _stage_two(self):
        self.join(A)
        self.send(msg(A, contact={"first_name": "Old", "phone_number": "91111111"}))

    def test_waiting_list_just_under_24h_still_saves(self):
        self._stage_two()
        self._later(hours=23, minutes=59)
        self.send(cb(A, "ok"))
        self.assertTrue(self.last()["text"].startswith("Saved 1 lead"))
        self.assertEqual(self.count("contacts"), 1)

    def test_ok_on_expired_list_saves_nothing_and_says_so(self):
        self._stage_two()
        self._later(hours=24, minutes=1)
        self.send(cb(A, "ok"))
        self.assertEqual(self.last()["text"], wording.PENDING_EXPIRED)
        self.assertEqual((self.count("contacts"), self.count("pending")), (0, 0))

    def test_cancel_on_expired_list_deletes_it(self):
        self._stage_two()
        self._later(days=3)
        self.send(cb(A, "cancel"))
        self.assertEqual(self.last()["text"], wording.CANCELLED)
        self.assertEqual(self.count("pending"), 0)

    def test_new_lead_on_expired_list_starts_fresh(self):
        self._stage_two()
        self._later(days=2)
        self.send(msg(A, contact={"first_name": "New", "phone_number": "92222222"}))
        text = self.last()["text"]
        self.assertIn("1. New, 92222222", text)
        self.assertNotIn("Old", text)
        self.send(cb(A, "ok"))
        self.assertEqual([r[0] for r in self.conn.execute("SELECT name FROM contacts")], ["New"])

    def test_unparseable_created_at_counts_as_expired(self):
        self._stage_two()
        self.conn.execute("UPDATE pending SET created_at='garbage'")
        self.conn.commit()
        self.send(cb(A, "ok"))
        self.assertEqual(self.last()["text"], wording.PENDING_EXPIRED)
        self.assertEqual(self.count("contacts"), 0)

    def test_copy_has_no_banned_terms(self):
        strings = [v for v in vars(wording).values() if isinstance(v, str)]
        strings += [wording.paused("2026-12-10"), wording.paused(None), wording.PAUSED_DELETED, wording.PAUSED_DELETING, wording.pending_summary([{"name": "A", "phone_raw": "1", "consent": "unknown"}]),
                    wording.saved(1, 0), wording.roadshow_on("x"), wording.left_done("x"),
                    wording.left_not_on_team("x", "paused"), wording.rejoin_done("x"),
                    wording.rejoin_already("x"), wording.agent_line("x", 1, "paused", "2027-01-01"),
                    wording.invite_code("X")]
        for s in strings:
            self.assertEqual(drafts.check_copy(s), [], s)

    def test_paused_wording_names_the_last_day_the_data_is_kept(self):
        self.assertEqual(
            wording.paused("2026-12-10"),
            "Your iSuite is paused. Your contacts will be deleted after 10 Dec 2026, "
            "so please send /export to take a copy before then. "
            "You can carry on with the free open edition of iSuite, which you run yourself.")
        self.assertIn("after 5 Jan 2027", wording.paused("2027-01-05"))
        none = ("Your iSuite is paused. Ask your leader if you would like to come back. "
                "You can carry on with the free open edition of iSuite, which you run yourself.")
        self.assertEqual(wording.paused(None), none)
        self.assertEqual(wording.paused("garbage"), none)
        self.assertIn("deleted", wording.PAUSED_DELETED)
        self.assertIn("paused", wording.PAUSED_DELETED)
        self.assertEqual(drafts.check_copy(wording.PAUSED_DELETED), [])

    def test_paused_agent_is_told_the_last_kept_day_then_that_it_is_gone(self):
        agent = self.join(A)
        self.set_status(A, "paused")
        # NOW is 10 Nov 2026. Paused 11 Oct: the last day kept is 10 Nov (30 days), so on
        # 10 Nov the data is still there and the date shown is today.
        for paused_on, want in (("2026-11-01", wording.paused("2026-12-01")),
                                ("2026-10-11", wording.paused("2026-11-10")),
                                ("2026-10-10", wording.PAUSED_DELETED),
                                ("2026-10-01", wording.PAUSED_DELETED)):
            self.conn.execute("UPDATE agents SET paused_on=?", (paused_on,))
            self.conn.commit()
            self.send(msg(A, "/today"))
            self.assertEqual(self.last()["text"], want, paused_on)
        self.assertIn("1 Dec 2026", wording.paused("2026-12-01"))

    def test_deleted_is_only_said_when_the_contacts_are_really_gone(self):
        agent = self.join(A)
        other = self.join(ADMIN)  # another agent's contacts do not count
        add_or_merge(self.conn, other.id, LeadDraft("Lee", "98765432", "yes"), "text", NOW.date(), NOW)
        add_or_merge(self.conn, agent.id, LeadDraft("Tan", "91234567", "yes"), "text", NOW.date(), NOW)
        self.set_status(A, "paused")
        self.conn.execute("UPDATE agents SET paused_on='2026-10-01' WHERE telegram_id=?", (A,))
        self.conn.commit()  # the date has passed but the hourly purge has not run yet
        self.send(msg(A, "/today"))
        self.assertEqual(self.last()["text"], wording.PAUSED_DELETING)
        self.assertNotIn("have been deleted", self.last()["text"])
        self.assertNotRegex(self.last()["text"], r"\d{4}")  # no date
        roster.purge_paused(self.conn, NOW.date())
        self.send(msg(A, "/today"))
        self.assertEqual(self.last()["text"], wording.PAUSED_DELETED)
        self.assertEqual(self.count("contacts"), 1)  # the other agent's contact is untouched
        for text in (wording.PAUSED_DELETING, wording.PAUSED_DELETED):
            self.assertEqual(drafts.check_copy(text), [])
            self.assertNotIn("\u2014", text)
            self.assertNotIn("\u2013", text)
            self.assertNotIn("-", text)

    def test_paused_agent_with_no_readable_date_gets_the_no_date_text(self):
        self.join(A)
        self.set_status(A, "paused")
        for bad in (None, "garbage", ""):
            self.conn.execute("UPDATE agents SET paused_on=?", (bad,))
            self.conn.commit()
            self.send(msg(A, "/today"))
            self.assertEqual(self.last()["text"], wording.paused(None), repr(bad))

    def test_wording_has_no_em_or_en_dashes(self):
        outputs = [v for v in vars(wording).values() if isinstance(v, str)]
        for table in ("_CONSENT", "_OUTCOME"):
            outputs += list(getattr(wording, table).values())
        outputs += [
            wording.paused("2026-12-10"), wording.paused(None), wording.PAUSED_DELETED,
            wording.PAUSED_DELETING,
            wording.pending_summary([{"name": "A", "phone_raw": "1", "consent": "unknown"}]),
            wording.saved(1, 0), wording.saved(3, 1), wording.roadshow_on("x"),
            wording.roadshow_on(""), wording.left_done("x"), wording.left_not_on_team("x", "paused"),
            wording.rejoin_done("x"), wording.rejoin_already("x"),
            wording.agent_line("x", 1, "paused", "2027-01-01"), wording.invite_code("X"),
            wording.consent_label("unknown"), wording.consent_button("x"), wording.consent_saved(),
            wording.outcome_saved("messaged"), wording.greeting(9), wording.brief_header("x", 1, 9),
            wording.brief_header("x", 5, 19), wording.brief_more(2),
            wording.brief_contact("x", "new", 0, "b"),
            wording.week_summary(STATS), wording.team_summary([dict(STATS, name="x")])]
        for s in outputs:
            self.assertNotIn("\u2014", s, s)
            self.assertNotIn("\u2013", s, s)


if __name__ == "__main__":
    unittest.main()
