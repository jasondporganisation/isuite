import json
import unittest
from datetime import datetime, timezone

from isuite import drafts, roster, wording
from isuite.bot import Deps, handle_update
from isuite.config import Config, load_config
from isuite.db import connect
from isuite.llm import FakeLLM
from isuite.telegram_api import FakeTelegram

NOW = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)
OWNER, STRANGER = 7, 8


def msg(uid, text=None, **extra):
    m = {"from": {"id": uid, "first_name": "Sam"}, "chat": {"id": uid, "type": "private"}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"message": m}


def cb(uid, data):
    return {"callback_query": {"id": "c", "from": {"id": uid}, "data": data,
                               "message": {"chat": {"id": uid, "type": "private"}}}}


class SoloConfigTests(unittest.TestCase):
    def test_owner_is_read_and_becomes_the_only_admin(self):
        cfg = load_config({"ISUITE_OWNER": "7"})
        self.assertEqual((cfg.owner_id, cfg.admin_ids), (7, {7}))

    def test_no_owner_means_team_mode_as_before(self):
        cfg = load_config({"ISUITE_ADMINS": "1,2"})
        self.assertEqual((cfg.owner_id, cfg.admin_ids), (None, {1, 2}))

    def test_owner_and_admins_together_is_a_clean_error_that_hides_values(self):
        with self.assertRaises(ValueError) as cm:
            load_config({"ISUITE_OWNER": "7", "ISUITE_ADMINS": "123456"})
        self.assertIn("ISUITE_OWNER", str(cm.exception))
        self.assertIn("ISUITE_ADMINS", str(cm.exception))
        self.assertNotIn("123456", str(cm.exception))

    def test_owner_must_be_a_number_and_the_value_is_not_echoed(self):
        for bad in ("abc", "7,8", "-5", "0"):
            with self.assertRaises(ValueError) as cm:
                load_config({"ISUITE_OWNER": bad})
            self.assertIn("ISUITE_OWNER", str(cm.exception))
            self.assertNotIn(bad, str(cm.exception).replace("ISUITE_OWNER", ""))

    def test_blank_owner_counts_as_not_set(self):
        self.assertIsNone(load_config({"ISUITE_OWNER": "  "}).owner_id)


class SoloBotTests(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)
        self.tg = FakeTelegram()
        self.llm = FakeLLM([])
        self.cfg = Config("t", "k", ":memory:", {OWNER}, owner_id=OWNER)
        self.deps = Deps(self.conn, self.cfg, self.tg, self.llm, lambda: NOW)

    def send(self, update):
        handle_update(self.deps, update)

    def last(self):
        return self.tg.sent[-1]

    def agents(self):
        return self.conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]

    def test_start_without_code_enrols_the_owner(self):
        self.send(msg(OWNER, "/start"))
        agent = roster.get_agent(self.conn, OWNER)
        self.assertEqual((agent.status, agent.name), ("team", "Sam"))
        self.assertEqual(self.last()["text"], wording.SOLO_WELCOME)
        self.assertNotIn("leader", self.last()["text"].lower())

    def test_first_ordinary_message_enrols_and_is_handled(self):
        self.llm = self.deps.llm = FakeLLM([json.dumps({"leads": [
            {"name": "Tan", "phone": "91234567", "consent_ticked": True, "notes": ""}]})])
        self.send(msg(OWNER, "lead: Tan 91234567"))
        self.assertEqual(self.agents(), 1)
        self.assertIn("Tan", self.last()["text"])
        self.send(msg(OWNER, "/start"))
        self.send(msg(OWNER, "hello"))
        self.assertEqual(self.agents(), 1)

    def test_start_with_a_code_is_ignored_and_still_enrols(self):
        self.send(msg(OWNER, "/start ABCDEFGH"))
        self.assertEqual(self.agents(), 1)
        self.assertEqual(self.last()["text"], wording.SOLO_WELCOME)

    def test_anyone_else_gets_the_private_copy_reply_and_nothing_is_stored(self):
        self.tg.files["p"] = b"img"
        for update in (msg(STRANGER, "/start"), msg(STRANGER, "/start ABCDEFGH"),
                       msg(STRANGER, "lead: Tan 91234567"), msg(STRANGER, "/today"),
                       msg(STRANGER, photo=[{"file_id": "p"}]), cb(STRANGER, "ok")):
            self.send(update)
            self.assertEqual(self.last()["text"], wording.SOLO_PRIVATE)
        self.assertEqual(self.agents(), 0)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM invites").fetchone()[0], 0)

    def test_an_invite_cannot_be_used_even_if_one_exists(self):
        code = roster.create_invite(self.conn, OWNER, NOW)
        self.send(msg(STRANGER, f"/start {code}"))
        self.assertIsNone(roster.get_agent(self.conn, STRANGER))

    def test_team_only_commands_say_so_and_do_nothing(self):
        self.send(msg(OWNER, "/start"))
        for cmd in ("/invite", "/team", "/left 7", "/rejoin 7", "/agents", "/invite@MyBot"):
            self.send(msg(OWNER, cmd))
            self.assertEqual(self.last()["text"], wording.SOLO_TEAM_ONLY, cmd)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM invites").fetchone()[0], 0)
        self.assertEqual(roster.get_agent(self.conn, OWNER).status, "team")

    def test_team_only_command_as_first_message_still_enrols_the_owner(self):
        self.send(msg(OWNER, "/team"))
        self.assertEqual(self.last()["text"], wording.SOLO_TEAM_ONLY)
        self.assertIsNotNone(roster.get_agent(self.conn, OWNER))

    def test_slash_help_is_the_plain_list_and_other_text_says_it_was_not_understood(self):
        self.send(msg(OWNER, "/help"))
        self.assertEqual(self.last()["text"], wording.HELP)
        self.send(msg(OWNER, "Met Alan at a wedding 91234567"))
        text = self.last()["text"]
        self.assertTrue(text.startswith("Sorry, I did not understand that."))
        self.assertIn("lead: Name 91234567", text)
        self.assertIn(wording.HELP, text)

    def test_help_and_export_work_for_the_owner(self):
        self.send(msg(OWNER, "hi"))
        self.assertEqual(self.last()["text"], wording.HELP_UNKNOWN)
        self.send(msg(OWNER, "/export"))
        self.assertEqual(self.last()["text"], wording.NO_CONTACTS)

    def test_group_chats_are_still_ignored(self):
        update = msg(OWNER, "/start")
        update["message"]["chat"]["type"] = "group"
        self.send(update)
        self.assertEqual((self.agents(), len(self.tg.sent)), (0, 0))

    def test_solo_welcome_does_not_over_promise_privacy(self):
        text = wording.SOLO_WELCOME
        self.assertIn("not on anyone else's server", text)
        self.assertIn("Telegram", text)
        self.assertIn("AI service", text)
        self.assertNotIn("only for you. Your contacts stay", text)
        self.assertEqual(drafts.check_copy(text), [])

    def test_solo_wording_is_plain(self):
        for text in (wording.SOLO_WELCOME, wording.SOLO_PRIVATE, wording.SOLO_TEAM_ONLY):
            self.assertNotIn("—", text)
            self.assertNotIn("–", text)


class TeamModeUnchangedTests(unittest.TestCase):
    def test_without_owner_a_stranger_still_needs_an_invite(self):
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        tg = FakeTelegram()
        deps = Deps(conn, Config("t", "k", ":memory:", {1}), tg, FakeLLM([]), lambda: NOW)
        handle_update(deps, msg(STRANGER, "/start"))
        self.assertEqual(tg.sent[-1]["text"], wording.NEED_INVITE)
