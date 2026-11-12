import base64
import io
import json
import unittest
import urllib.error
import urllib.request
from unittest import mock

from isuite.extract import ExtractError, extract_leads
from isuite.llm import ClaudeClient, FakeLLM, LLMError

TWO = json.dumps({"leads": [
    {"name": "Tan", "phone": "91234567", "consent_ticked": True, "notes": "CI"},
    {"name": "Lim", "phone": None, "consent_ticked": None, "notes": ""},
]})


class ExtractTests(unittest.TestCase):
    def test_extract_from_photo_maps_consent(self):
        leads = extract_leads(FakeLLM([TWO]), image=b"img")
        self.assertEqual([l.consent for l in leads], ["yes", "unknown"])
        self.assertEqual(leads[0].phone_raw, "91234567")
        self.assertEqual(leads[1].phone_raw, "")

    def test_image_type_defaults_to_jpeg_and_can_be_set(self):
        llm = FakeLLM([TWO, TWO])
        extract_leads(llm, image=b"img")
        extract_leads(llm, image=b"img", image_type="image/webp")
        self.assertEqual([c["image_type"] for c in llm.calls], ["image/jpeg", "image/webp"])

    def test_false_consent_is_no(self):
        r = json.dumps({"leads": [{"name": "A", "phone": "1", "consent_ticked": False}]})
        self.assertEqual(extract_leads(FakeLLM([r]), text="x")[0].consent, "no")

    def test_extract_strips_code_fence(self):
        leads = extract_leads(FakeLLM(["```json\n" + TWO + "\n```"]), text="x")
        self.assertEqual(len(leads), 2)

    def test_extract_bad_json_raises_extract_error(self):
        with self.assertRaises(ExtractError):
            extract_leads(FakeLLM(["not json"]), text="x")

    def test_missing_leads_list_raises(self):
        with self.assertRaises(ExtractError):
            extract_leads(FakeLLM(['{"foo": 1}']), text="x")

    def test_empty_entries_skipped_and_output_redacted(self):
        r = json.dumps({"leads": [
            {"name": "", "phone": None},
            {"name": "Bob S1234567D", "phone": "9", "notes": "policy no: ABC123"},
        ]})
        leads = extract_leads(FakeLLM([r]), text="x")
        self.assertEqual(len(leads), 1)
        self.assertNotIn("S1234567D", leads[0].name)
        self.assertNotIn("ABC123", leads[0].notes)

    def test_phone_that_looks_like_nric_is_blanked(self):
        r = json.dumps({"leads": [
            {"name": "Bob", "phone": "S1234567D"}, {"name": "Amy", "phone": "S 1234567 D"},
            {"name": "Kim", "phone": "9123 4567"}]})
        leads = extract_leads(FakeLLM([r]), text="Bob, Amy, Kim 9123 4567")
        self.assertEqual([l.phone_raw for l in leads], ["", "", "9123 4567"])

    def test_reading_leads_keeps_thinking_on(self):
        llm = FakeLLM(['{"leads":[]}'])
        extract_leads(llm, text="Mei 91234567")
        self.assertTrue(llm.calls[0]["thinking"])

    def test_a_typed_number_the_ai_changed_is_dropped(self):
        llm = FakeLLM([json.dumps({"leads": [
            {"name": "Priya", "phone": "65911122222"},
            {"name": "Mei", "phone": "8123 4567"},
            {"name": "Siti", "phone": "+65 82223233"},
            {"name": "Bo", "phone": "6598765432"}]})])
        leads = extract_leads(llm, text="Priya 6591112222, Mei 81234567, "
                                        "Siti 8222 3233, Bo 98765432")
        self.assertEqual([d.phone_raw for d in leads],
                         ["", "8123 4567", "+65 82223233", "6598765432"])

    def test_an_extra_digit_borrowed_from_the_next_number_is_dropped(self):
        llm = FakeLLM([json.dumps({"leads": [
            {"name": "Tan", "phone": "912345678"}, {"name": "Lim", "phone": "8123-4567"}]})])
        leads = extract_leads(llm, text="Tan 9123 4567, Lim 8123-4567")
        self.assertEqual([d.phone_raw for d in leads], ["", "8123-4567"])

    def test_a_number_read_from_a_photo_is_not_checked_against_text(self):
        llm = FakeLLM([json.dumps({"leads": [{"name": "Mei", "phone": "81234567"}]})])
        self.assertEqual(extract_leads(llm, image=b"IMG")[0].phone_raw, "81234567")

    def test_extraction_allows_long_sheets(self):
        llm = FakeLLM([TWO])
        extract_leads(llm, image=b"img")
        self.assertEqual(llm.calls[0]["max_tokens"], 4096)

    def test_lead_reading_uses_a_45_second_timeout_and_no_retry(self):
        for kwargs in ({"image": b"img"}, {"text": "Tan 91234567"}):
            llm = FakeLLM([TWO])
            extract_leads(llm, **kwargs)
            self.assertEqual((llm.calls[0]["timeout"], llm.calls[0]["retries"]), (45, 0))

    def test_text_is_redacted_before_sending(self):
        llm = FakeLLM([TWO])
        extract_leads(llm, text="Tan S1234567D 91234567")
        user = llm.calls[0]["user"]
        self.assertIn("[ID removed]", user)
        self.assertNotIn("S1234567D", user)

    def test_image_passed_through(self):
        llm = FakeLLM([TWO])
        extract_leads(llm, image=b"\xff\xd8abc")
        self.assertEqual(llm.calls[0]["image"], b"\xff\xd8abc")


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return json.dumps(self._p).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


OK = {"content": [{"type": "text", "text": "hello"}]}


class ClaudeClientTests(unittest.TestCase):
    def test_claude_client_builds_request(self):
        seen = {}

        def fake(req, timeout=None):
            seen["req"] = req
            return _Resp(OK)

        with mock.patch("urllib.request.urlopen", fake):
            out = ClaudeClient("sk-test").complete("sys", "hi", image=b"IMG")
        req = seen["req"]
        self.assertEqual(out, "hello")
        self.assertEqual(req.full_url, "https://api.anthropic.com/v1/messages")
        h = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(h["x-api-key"], "sk-test")
        self.assertEqual(h["anthropic-version"], "2023-06-01")
        self.assertEqual(h["content-type"], "application/json")
        body = json.loads(req.data)
        self.assertEqual(body["model"], "claude-haiku-5-5")
        self.assertEqual(body["system"], "sys")
        self.assertEqual(body["messages"][0]["content"][0], {
            "type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(b"IMG").decode()}})
        self.assertEqual(body["messages"][0]["content"][1], {"type": "text", "text": "hi"})

    def _http(self, code):
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b""))

    def test_retries_once_on_429_then_succeeds(self):
        calls = [self._http(429), _Resp(OK)]
        sleeps = []

        def fake(req, timeout=None):
            r = calls.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch("urllib.request.urlopen", fake):
            out = ClaudeClient("k", sleep=sleeps.append).complete("s", "u")
        self.assertEqual(out, "hello")
        self.assertEqual(sleeps, [2])

    def test_second_failure_raises_without_key(self):
        def fake(req, timeout=None):
            raise self._http(503)

        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(LLMError) as cm:
                ClaudeClient("sk-secret", sleep=lambda s: None).complete("s", "u")
        self.assertNotIn("sk-secret", str(cm.exception))

    def test_llm_error_carries_the_http_status_but_not_the_reply(self):
        def fake(req, timeout=None):
            raise self._http(413)

        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(LLMError) as cm:
                ClaudeClient("k", sleep=lambda s: None).complete("s", "u")
        self.assertEqual(cm.exception.status, 413)
        self.assertEqual(str(cm.exception), "Claude API HTTP 413")
        self.assertIsNone(LLMError("x").status)

    def test_400_does_not_retry(self):
        n = []

        def fake(req, timeout=None):
            n.append(1)
            raise self._http(400)

        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(LLMError):
                ClaudeClient("k", sleep=lambda s: None).complete("s", "u")
        self.assertEqual(len(n), 1)

    def test_timeout_is_passed_to_urlopen(self):
        seen = []

        def fake(req, timeout=None):
            seen.append(timeout)
            return _Resp(OK)

        with mock.patch("urllib.request.urlopen", fake):
            ClaudeClient("k").complete("s", "u")
            ClaudeClient("k").complete("s", "u", timeout=12)
        self.assertEqual(seen, [60, 12])

    def test_retries_zero_makes_one_attempt_only(self):
        n, sleeps = [], []

        def fake(req, timeout=None):
            n.append(1)
            raise self._http(429)

        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(LLMError):
                ClaudeClient("k", sleep=sleeps.append).complete("s", "u", retries=0)
        self.assertEqual(len(n), 1)
        self.assertEqual(sleeps, [])

    def test_retries_two_makes_three_attempts(self):
        n = []

        def fake(req, timeout=None):
            n.append(1)
            raise self._http(503)

        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(LLMError):
                ClaudeClient("k", sleep=lambda s: None).complete("s", "u", retries=2)
        self.assertEqual(len(n), 3)

    def test_odd_reply_shapes_raise_llm_error_never_attribute_error(self):
        for payload in ([], "x", None, {"content": "x"}, {"content": None}, {"content": ["x"]},
                        {"content": [None]}, {"content": [{"type": "text"}]},
                        {"content": [{"type": "text", "text": 5}]},
                        {"content": [{"type": "image"}]}, {}):
            with mock.patch("urllib.request.urlopen", lambda req, timeout=None, p=payload: _Resp(p)):
                with self.assertRaises(LLMError, msg=repr(payload)) as cm:
                    ClaudeClient("sk-secret").complete("s", "u")
            self.assertNotIn("sk-secret", str(cm.exception))
            self.assertNotIn("hello", str(cm.exception))

    def test_reply_text_is_not_leaked_in_shape_errors(self):
        payload = {"content": [{"type": "text", "text": 12345, "secret": "TOPSECRET"}]}
        with mock.patch("urllib.request.urlopen", lambda req, timeout=None: _Resp(payload)):
            with self.assertRaises(LLMError) as cm:
                ClaudeClient("k").complete("s", "u")
        self.assertNotIn("TOPSECRET", str(cm.exception))

    def test_thinking_is_switched_off_only_when_asked(self):
        seen = {}

        def fake(req, timeout=None):
            seen["req"] = req
            return _Resp(OK)

        with mock.patch("urllib.request.urlopen", fake):
            ClaudeClient("k").complete("s", "u", thinking=False)
        self.assertEqual(json.loads(seen["req"].data)["thinking"], {"type": "disabled"})
        with mock.patch("urllib.request.urlopen", fake):
            ClaudeClient("k").complete("s", "u")
        self.assertNotIn("thinking", json.loads(seen["req"].data))

    def test_a_cut_off_reply_raises_and_its_text_is_not_used(self):
        payload = {"stop_reason": "max_tokens",
                   "content": [{"type": "text", "text": "Hi Wei Ming, it's J"}]}
        with mock.patch("urllib.request.urlopen", lambda req, timeout=None: _Resp(payload)):
            with self.assertRaises(LLMError) as cm:
                ClaudeClient("k").complete("s", "u")
        self.assertNotIn("Wei Ming", str(cm.exception))

    def test_a_cut_off_reply_is_its_own_error_type(self):
        from isuite.llm import LLMCutOff
        payload = {"stop_reason": "max_tokens", "content": [{"type": "thinking"}]}
        with mock.patch("urllib.request.urlopen", lambda req, timeout=None: _Resp(payload)):
            with self.assertRaises(LLMCutOff):
                ClaudeClient("k").complete("s", "u")

    def test_a_finished_reply_is_returned(self):
        payload = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "hi"}]}
        with mock.patch("urllib.request.urlopen", lambda req, timeout=None: _Resp(payload)):
            self.assertEqual(ClaudeClient("k").complete("s", "u"), "hi")

    def test_non_text_blocks_before_a_text_block_are_skipped(self):
        payload = {"content": [{"type": "thinking"}, {"type": "text", "text": "hi"}]}
        with mock.patch("urllib.request.urlopen", lambda req, timeout=None: _Resp(payload)):
            self.assertEqual(ClaudeClient("k").complete("s", "u"), "hi")

    def test_fake_llm_records_timeout_and_retries(self):
        llm = FakeLLM(["a", "b"])
        llm.complete("s", "u")
        llm.complete("s", "u", timeout=12, retries=0)
        self.assertEqual([(c["timeout"], c["retries"]) for c in llm.calls], [(60, 1), (12, 0)])


if __name__ == "__main__":
    unittest.main()
