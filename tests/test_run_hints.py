import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from isuite import run
from isuite.config import Config
from isuite.telegram_api import TelegramError

TOKEN = "123456789:AAFakeTokenForTestsOnly_x-y"


class HintTests(unittest.TestCase):
    def setUp(self):
        run.reset_hints()

    def hint(self, exc):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run.hint_once(exc)
        return err.getvalue()

    def test_a_clash_with_another_program_is_explained_in_plain_words(self):
        text = self.hint(TelegramError("Telegram getUpdates HTTP 409", code=409))
        self.assertIn("Another program is using this bot", text)
        self.assertIn("new bot", text)

    def test_a_refused_token_points_back_to_setup(self):
        for code in (401, 404):
            run.reset_hints()
            self.assertIn("python3 -m isuite.setup",
                          self.hint(TelegramError("Telegram getUpdates HTTP", code=code)))

    def test_no_connection_says_it_will_keep_trying(self):
        text = self.hint(TelegramError("Telegram getUpdates request failed: URLError", code=0))
        self.assertIn("keep trying", text)

    def test_an_odd_reply_with_no_number_is_not_called_a_connection_problem(self):
        self.assertEqual(self.hint(TelegramError("Telegram getUpdates failed: bad reply")), "")

    def test_each_hint_is_shown_once_and_never_carries_the_error_text(self):
        boom = TelegramError(f"Telegram getUpdates failed: {TOKEN}", code=409)
        first, second = self.hint(boom), self.hint(boom)
        self.assertTrue(first)
        self.assertEqual(second, "")
        self.assertNotIn(TOKEN[:12], first)

    def test_other_errors_and_other_codes_get_no_hint(self):
        self.assertEqual(self.hint(ValueError("x")), "")
        self.assertEqual(self.hint(TelegramError("Telegram getUpdates HTTP 500", code=500)), "")

    def test_step_gives_the_hint_when_polling_fails(self):
        class Tg:
            def get_updates(self, offset, timeout=30):
                raise TelegramError("Telegram getUpdates HTTP 409", code=409)
        deps = mock.Mock(tg=Tg())
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                mock.patch("isuite.run.bot.summaries_waiting", return_value=False):
            self.assertEqual(run.step(deps, 7, sleep=lambda s: None), 7)
        self.assertIn("isuite loop error: TelegramError", err.getvalue())
        self.assertIn("Another program is using this bot", err.getvalue())


class TelegramErrorCodeTests(unittest.TestCase):
    def test_code_is_kept_and_defaults_to_none(self):
        self.assertEqual(TelegramError("x", code=409).code, 409)
        self.assertIsNone(TelegramError("x").code)


class ServeMessagesTests(unittest.TestCase):
    def test_serve_says_it_is_running_and_says_when_it_stops(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(telegram_token=TOKEN, anthropic_key="sk-ant-fake-key-for-tests-only",
                         db_path=str(Path(d) / "t.db"), admin_ids=set(), owner_id=1)
            said = []
            with mock.patch("isuite.run.step", side_effect=KeyboardInterrupt):
                self.assertEqual(run._serve(cfg, out=said.append), 0)
        text = "\n".join(said)
        self.assertIn("iSuite is running", text)
        self.assertIn("Control and C", text)
        self.assertIn("iSuite has stopped", text)
        self.assertNotIn(TOKEN[:12], text)
