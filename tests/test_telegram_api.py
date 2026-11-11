import io
import json
import unittest
import urllib.error
from unittest import mock

from isuite.telegram_api import BotAPI, FakeTelegram, TelegramError, split_text

TOKEN = "123:SECRET"


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(result):
    return Resp(json.dumps({"ok": True, "result": result}).encode())


def body(req):
    return json.loads(req.data.decode())


class BotAPITests(unittest.TestCase):
    def test_send_builds_inline_keyboard(self):
        with mock.patch("urllib.request.urlopen", return_value=ok({})) as m:
            BotAPI(TOKEN).send(5, "hi", [[("Yes", "y"), ("Open", "https://wa.me/65?text=a")],
                                         [("No", "n")]])
        req = m.call_args[0][0]
        self.assertEqual(req.full_url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        b = body(req)
        self.assertEqual(b["chat_id"], 5)
        self.assertNotIn("parse_mode", b)
        self.assertEqual(b["reply_markup"]["inline_keyboard"], [
            [{"text": "Yes", "callback_data": "y"},
             {"text": "Open", "url": "https://wa.me/65?text=a"}],
            [{"text": "No", "callback_data": "n"}]])

    def test_send_without_buttons_has_no_markup(self):
        with mock.patch("urllib.request.urlopen", return_value=ok({})) as m:
            BotAPI(TOKEN).send(5, "hi")
        self.assertNotIn("reply_markup", body(m.call_args[0][0]))

    def test_long_text_split_buttons_on_last(self):
        text = "\n".join(["x" * 100] * 80)  # ~8080 chars
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: ok({})) as m:
            BotAPI(TOKEN).send(5, text, [[("A", "a")]])
        bodies = [body(c[0][0]) for c in m.call_args_list]
        self.assertGreater(len(bodies), 1)
        self.assertTrue(all(len(b["text"]) <= 4096 for b in bodies))
        self.assertEqual("".join(b["text"] for b in bodies), text)
        self.assertTrue(all("reply_markup" not in b for b in bodies[:-1]))
        self.assertIn("reply_markup", bodies[-1])

    def test_split_text_hard_splits_long_line(self):
        chunks = split_text("y" * 5000)
        self.assertEqual([len(c) for c in chunks], [4096, 904])

    def test_get_updates_passes_offset_and_timeout(self):
        with mock.patch("urllib.request.urlopen", return_value=ok([{"update_id": 1}])) as m:
            out = BotAPI(TOKEN).get_updates(7, timeout=25)
        self.assertEqual(out, [{"update_id": 1}])
        req = m.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/getUpdates"))
        b = body(req)
        self.assertEqual(b["offset"], 7)
        self.assertEqual(b["timeout"], 25)
        self.assertEqual(b["allowed_updates"], ["message", "callback_query"])
        self.assertEqual(m.call_args[1]["timeout"], 35)

    def test_get_updates_omits_none_offset(self):
        with mock.patch("urllib.request.urlopen", return_value=ok([])) as m:
            BotAPI(TOKEN).get_updates(None)
        self.assertNotIn("offset", body(m.call_args[0][0]))

    def test_answer_callback(self):
        with mock.patch("urllib.request.urlopen", return_value=ok(True)) as m:
            BotAPI(TOKEN).answer_callback("cb1", "done")
        self.assertTrue(m.call_args[0][0].full_url.endswith("/answerCallbackQuery"))
        self.assertEqual(body(m.call_args[0][0]), {"callback_query_id": "cb1", "text": "done"})

    def test_download_file_two_calls(self):
        replies = [ok({"file_path": "photos/a.jpg"}), Resp(b"JPEGDATA")]
        with mock.patch("urllib.request.urlopen", side_effect=replies) as m:
            data = BotAPI(TOKEN).download_file("fid")
        self.assertEqual(data, b"JPEGDATA")
        self.assertEqual(m.call_count, 2)
        self.assertTrue(m.call_args_list[0][0][0].full_url.endswith("/getFile"))
        self.assertEqual(body(m.call_args_list[0][0][0]), {"file_id": "fid"})
        self.assertEqual(m.call_args_list[1][0][0],
                         f"https://api.telegram.org/file/bot{TOKEN}/photos/a.jpg")

    def test_ok_false_raises_without_token(self):
        reply = Resp(json.dumps({"ok": False, "description": "chat not found"}).encode())
        with mock.patch("urllib.request.urlopen", return_value=reply):
            with self.assertRaises(TelegramError) as cm:
                BotAPI(TOKEN).send(1, "x")
        self.assertIn("sendMessage", str(cm.exception))
        self.assertIn("chat not found", str(cm.exception))
        self.assertNotIn("SECRET", str(cm.exception))

    def test_http_error_hides_token(self):
        err = urllib.error.HTTPError(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                                     401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(TelegramError) as cm:
                BotAPI(TOKEN).send(1, "x")
        self.assertIn("401", str(cm.exception))
        self.assertNotIn("SECRET", str(cm.exception))

    def test_network_error_hides_token(self):
        err = urllib.error.URLError(f"failed https://api.telegram.org/bot{TOKEN}/x")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(TelegramError) as cm:
                BotAPI(TOKEN).get_updates(None)
        self.assertNotIn("SECRET", str(cm.exception))

    def test_file_download_error_hides_token(self):
        replies = [ok({"file_path": "p.jpg"}),
                   urllib.error.HTTPError("https://x/bot123:SECRET/p", 404, "nf", {}, None)]
        with mock.patch("urllib.request.urlopen", side_effect=replies):
            with self.assertRaises(TelegramError) as cm:
                BotAPI(TOKEN).download_file("f")
        self.assertNotIn("SECRET", str(cm.exception))


class RetryTests(unittest.TestCase):
    def _429(self, payload=b'{"ok":false,"error_code":429,"parameters":{"retry_after":3}}'):
        return urllib.error.HTTPError(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                                      429, "Too Many", {}, io.BytesIO(payload))

    def test_429_waits_retry_after_then_retries_once(self):
        sleeps = []
        with mock.patch("urllib.request.urlopen", side_effect=[self._429(), ok({})]) as m:
            BotAPI(TOKEN, sleep=sleeps.append).send(1, "x")
        self.assertEqual(sleeps, [3])
        self.assertEqual(m.call_count, 2)

    def test_second_429_raises_after_one_sleep(self):
        sleeps = []
        with mock.patch("urllib.request.urlopen", side_effect=[self._429(), self._429()]) as m:
            with self.assertRaises(TelegramError) as cm:
                BotAPI(TOKEN, sleep=sleeps.append).send(1, "x")
        self.assertEqual(sleeps, [3])
        self.assertEqual(m.call_count, 2)
        self.assertNotIn("SECRET", str(cm.exception))
        self.assertNotIn("api.telegram.org", str(cm.exception))

    def test_long_retry_after_does_not_wait_or_retry(self):
        sleeps = []
        err = self._429(b'{"parameters":{"retry_after":600}}')
        with mock.patch("urllib.request.urlopen", side_effect=[err]) as m:
            with self.assertRaises(TelegramError) as cm:
                BotAPI(TOKEN, sleep=sleeps.append).send(1, "x")
        self.assertEqual(sleeps, [])
        self.assertEqual(m.call_count, 1)
        self.assertIn("429", str(cm.exception))

    def test_unparseable_429_body_waits_one_second(self):
        sleeps = []
        with mock.patch("urllib.request.urlopen",
                        side_effect=[self._429(b"<html>nope"), ok({})]) as m:
            BotAPI(TOKEN, sleep=sleeps.append).send(1, "x")
        self.assertEqual(sleeps, [1])
        self.assertEqual(m.call_count, 2)

    def test_bad_retry_after_values_wait_one_second(self):
        for bad in (b'{"parameters":{"retry_after":"soon"}}', b'{"parameters":[]}', b"[]",
                    b'{"parameters":{"retry_after":-5}}', b'{"parameters":{"retry_after":true}}'):
            sleeps = []
            with mock.patch("urllib.request.urlopen", side_effect=[self._429(bad), ok({})]):
                BotAPI(TOKEN, sleep=sleeps.append).send(1, "x")
            self.assertEqual(sleeps, [1], bad)

    def test_other_http_errors_do_not_retry(self):
        sleeps = []
        err = urllib.error.HTTPError("u", 500, "x", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=[err]) as m:
            with self.assertRaises(TelegramError):
                BotAPI(TOKEN, sleep=sleeps.append).send(1, "x")
        self.assertEqual(sleeps, [])
        self.assertEqual(m.call_count, 1)


class WaitBudgetTests(unittest.TestCase):
    def _429(self, wait=20):
        return urllib.error.HTTPError(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage", 429, "Too Many", {},
            io.BytesIO(b'{"parameters":{"retry_after":%d}}' % wait))

    def test_a_flood_limit_cannot_block_a_job_past_its_wait_budget(self):
        sleeps = []
        api = BotAPI(TOKEN, sleep=sleeps.append)
        api.begin_wait_budget(45)
        # 15 sends, each rate-limited on both tries: without a budget this would wait 15 x 20 s.
        failures = 0

        def always_429(*a, **k):
            raise self._429()

        with mock.patch("urllib.request.urlopen", side_effect=always_429):
            for _ in range(15):
                try:
                    api.send(1, "x")
                except TelegramError:
                    failures += 1
        self.assertEqual(failures, 15)  # each send fails on its own, none is skipped
        self.assertLessEqual(sum(sleeps), 45)
        self.assertEqual(sleeps, [20, 20])  # a third wait of 20 would pass 45, so it is not made

    def test_a_send_that_fits_the_remaining_budget_still_retries(self):
        sleeps = []
        api = BotAPI(TOKEN, sleep=sleeps.append)
        api.begin_wait_budget(45)
        with mock.patch("urllib.request.urlopen",
                        side_effect=[self._429(20), ok({}), self._429(20), ok({})]):
            api.send(1, "a")
            api.send(1, "b")
        self.assertEqual(sleeps, [20, 20])

    def test_without_a_budget_the_per_call_rule_is_unchanged(self):
        sleeps = []
        api = BotAPI(TOKEN, sleep=sleeps.append)
        calls = []

        def alternate(*a, **k):
            calls.append(1)
            if len(calls) % 2:
                raise self._429(20)
            return ok({})

        with mock.patch("urllib.request.urlopen", side_effect=alternate):
            for _ in range(3):
                api.send(1, "x")
        self.assertEqual(sleeps, [20, 20, 20])

    def test_ending_the_budget_lifts_it_and_a_new_one_starts_full(self):
        sleeps = []
        api = BotAPI(TOKEN, sleep=sleeps.append)
        api.begin_wait_budget(25)
        with mock.patch("urllib.request.urlopen", side_effect=[self._429(20), ok({})]):
            api.send(1, "a")
        with mock.patch("urllib.request.urlopen", side_effect=[self._429(20)]) as m:
            with self.assertRaises(TelegramError):
                api.send(1, "b")  # 5 s left: no wait, no second try
            self.assertEqual(m.call_count, 1)
        self.assertEqual(sleeps, [20])
        api.end_wait_budget()
        with mock.patch("urllib.request.urlopen", side_effect=[self._429(20), ok({})]):
            api.send(1, "c")
        api.begin_wait_budget(25)
        with mock.patch("urllib.request.urlopen", side_effect=[self._429(20), ok({})]):
            api.send(1, "d")
        self.assertEqual(sleeps, [20, 20, 20])

    def test_fake_telegram_has_the_same_calls(self):
        t = FakeTelegram()
        t.begin_wait_budget(30)
        t.end_wait_budget()
        self.assertEqual(t.budget_calls, ["begin", "end"])


class FakeTelegramTests(unittest.TestCase):
    def test_fake_records_sends(self):
        t = FakeTelegram()
        t.send(1, "hello", [[("A", "a")]])
        t.send(2, "plain")
        t.answer_callback("c", "ok")
        self.assertEqual(t.sent, [
            {"chat_id": 1, "text": "hello", "buttons": [[("A", "a")]]},
            {"chat_id": 2, "text": "plain", "buttons": None}])
        self.assertEqual(t.answered, [{"callback_id": "c", "text": "ok"}])

    def test_fake_queue_and_files(self):
        t = FakeTelegram()
        t.queue.append({"update_id": 1})
        t.files["f"] = b"x"
        self.assertEqual(t.get_updates(None), [{"update_id": 1}])
        self.assertEqual(t.get_updates(2), [])
        self.assertEqual(t.download_file("f"), b"x")
        with self.assertRaises(TelegramError):
            t.download_file("nope")


if __name__ == "__main__":
    unittest.main()
