import contextlib
import io
import time
import unittest

from isuite.drafts import DRAFT_TIMEOUT, DraftBudget, check_copy, draft_with_link, make_draft
from isuite.llm import FakeLLM
from isuite.templates import FALLBACK


class DraftsTest(unittest.TestCase):
    def setUp(self):
        # Drafts fall back when the fake LLM runs dry; their one-line stderr note is expected.
        self.stderr = io.StringIO()
        quiet = contextlib.redirect_stderr(self.stderr)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_check_copy_flags_banned(self):
        found = check_copy("This plan is guaranteed and risk free")
        self.assertIn("guaranteed", found)
        self.assertIn("risk free", found)

    def test_house_terms_given_as_a_lone_string_count_as_one_term(self):
        from unittest import mock
        from isuite import house_terms
        with mock.patch.object(house_terms, "EXTRA_BANNED", "ACME"):
            self.assertEqual(check_copy("hello from acme"), ["ACME"])
            self.assertEqual(check_copy("plain text a c m e"), [])  # not one letter at a time
        with mock.patch.object(house_terms, "EXTRA_BANNED_WORDS", "ACME"):
            self.assertEqual(check_copy("hello ACME team"), ["ACME"])
            self.assertEqual(check_copy("ACMES and acme"), [])  # whole word, same capitals

    def test_empty_and_blank_house_terms_are_skipped(self):
        from unittest import mock
        from isuite import house_terms
        for value in ("", "   ", ("",), (" ", "\t"), (None,), None, ()):
            with mock.patch.object(house_terms, "EXTRA_BANNED", value), \
                    mock.patch.object(house_terms, "EXTRA_BANNED_WORDS", value):
                self.assertEqual(check_copy("an ordinary friendly message"), [], repr(value))
        with mock.patch.object(house_terms, "EXTRA_BANNED", ("", "  Acme ", "")):
            self.assertEqual(check_copy("from acme"), ["Acme"])

    def test_check_copy_ignores_ge_inside_words(self):
        self.assertEqual(check_copy("Great to meet you, George"), [])

    def test_check_copy_flags_wider_terms_and_urls(self):
        for text in ("It is risk free", "zero risk lah", "assured payout",
                     "see http://x.sg", "see https://x.sg", "visit www.example.com"):
            self.assertNotEqual(check_copy(text), [], text)

    def test_draft_with_url_falls_back(self):
        out = make_draft(FakeLLM(["Hi Mei! Details at https://example.com"]), agent_name="Sam",
                         first_name="Mei", where="the fair", sent_count=1, notes="")
        self.assertEqual(out, FALLBACK[1].format(
            first_name="Mei", agent_name="Sam", where="the fair"))

    def test_every_fallback_passes_copy_check(self):
        for step, template in FALLBACK.items():
            text = template.format(first_name="Mei", agent_name="Sam", where="the fair")
            self.assertEqual(check_copy(text), [], step)

    def test_llm_draft_used_when_clean(self):
        llm = FakeLLM(['  "Hi Mei, lovely to meet you at the fair!"  '])
        out = make_draft(llm, agent_name="Sam", first_name="Mei", where="the fair",
                         sent_count=0, notes="")
        self.assertEqual(out, "Hi Mei, lovely to meet you at the fair!")
        self.assertEqual(len(llm.calls), 1)

    def test_banned_draft_falls_back(self):
        out = make_draft(FakeLLM(["Guaranteed returns!"]), agent_name="Sam",
                         first_name="Mei", where="the fair", sent_count=0, notes="")
        self.assertEqual(out, FALLBACK[0].format(
            first_name="Mei", agent_name="Sam", where="the fair"))

    def test_overlong_draft_falls_back(self):
        out = make_draft(FakeLLM(["hello " * 100]), agent_name="Sam",
                         first_name="Mei", where="the fair", sent_count=0, notes="")
        self.assertIn("Hi Mei, this is Sam.", out)

    def test_llm_error_falls_back(self):
        out = make_draft(FakeLLM([]), agent_name="Sam", first_name="Mei",
                         where="the fair", sent_count=2, notes="")
        self.assertEqual(out, FALLBACK[2].format(
            first_name="Mei", agent_name="Sam", where="the fair"))
        self.assertEqual(self.stderr.getvalue(), "isuite draft error: LLMError\n")

    def test_draft_error_logs_type_name_only(self):
        class Boom:
            def complete(self, *a, **k):
                raise RuntimeError("invalid x-api-key sk-SECRET")

        out = make_draft(Boom(), agent_name="Sam", first_name="Mei", where="the fair",
                         sent_count=0, notes="")
        self.assertEqual(out, FALLBACK[0].format(
            first_name="Mei", agent_name="Sam", where="the fair"))
        self.assertIn("RuntimeError", self.stderr.getvalue())
        self.assertNotIn("SECRET", self.stderr.getvalue())

    def test_step_above_three_uses_step_three(self):
        out = make_draft(FakeLLM([""]), agent_name="Sam", first_name="Mei",
                         where="x", sent_count=7, notes="")
        self.assertEqual(out, FALLBACK[3].format(
            first_name="Mei", agent_name="Sam", where="x"))

    def test_all_fallbacks_pass_copy_check(self):
        for step, tpl in FALLBACK.items():
            text = tpl.format(first_name="Mei", agent_name="Sam", where="the roadshow")
            self.assertEqual(check_copy(text), [], step)

    def test_draft_with_link_has_wa_link(self):
        row = {"name": "Mei Tan", "phone": "+6591234567", "chase_step": 0,
               "notes": "Met at Bishan fair; likes hiking"}
        llm = FakeLLM(["Hi Mei!"])
        text, link = draft_with_link(llm, row, "Sam")
        self.assertEqual(text, "Hi Mei!")
        self.assertTrue(link.startswith("https://wa.me/6591234567?text="))
        self.assertIn("Bishan fair", llm.calls[0]["user"])
        self.assertIn("Mei", llm.calls[0]["user"])

    def test_banned_where_replaced_in_fallback(self):
        row = {"name": "Mei Tan", "phone": "+6591234567", "chase_step": 0,
               "notes": "Met at a risk free roadshow"}
        text, link = draft_with_link(FakeLLM(["Guaranteed returns!"]), row, "Sam")
        self.assertNotIn("risk free", text)
        self.assertEqual(check_copy(text), [])
        self.assertIn("the roadshow", text)

    def test_make_draft_result_always_passes_copy_check(self):
        out = make_draft(FakeLLM([]), agent_name="Sam Tan", first_name="Mei",
                         where="guaranteed event", sent_count=0, notes="")
        self.assertEqual(check_copy(out), [])

    def test_draft_with_link_defaults(self):
        row = {"name": "", "phone": None, "chase_step": 0, "notes": None}
        text, link = draft_with_link(FakeLLM([]), row, "Sam")
        self.assertEqual(link, "")
        self.assertIn("Hi there,", text)
        self.assertIn("the roadshow", text)


class DraftBudgetTests(unittest.TestCase):
    def setUp(self):
        self.stderr = io.StringIO()
        quiet = contextlib.redirect_stderr(self.stderr)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    KW = dict(agent_name="Sam", first_name="Mei", where="the fair", sent_count=0, notes="")

    def fallback(self):
        return FALLBACK[0].format(first_name="Mei", agent_name="Sam", where="the fair")

    def test_draft_asks_for_short_timeout_and_no_retry(self):
        llm = FakeLLM(["Hi Mei, nice to meet you at the fair."])
        make_draft(llm, **self.KW)
        self.assertEqual(DRAFT_TIMEOUT, 12)
        self.assertEqual((llm.calls[0]["timeout"], llm.calls[0]["retries"]), (12, 0))

    def test_ai_error_trips_budget_and_later_drafts_skip_the_ai(self):
        llm = FakeLLM([])  # first call raises LLMError
        budget = DraftBudget()
        self.assertTrue(budget.allowed())
        self.assertEqual(make_draft(llm, budget=budget, **self.KW), self.fallback())
        self.assertFalse(budget.allowed())
        self.assertEqual(make_draft(llm, budget=budget, **self.KW), self.fallback())
        self.assertEqual(len(llm.calls), 1)

    def test_final_follow_up_wording_matches_the_expected_text(self):
        self.assertEqual(
            FALLBACK[3].format(first_name="Mei", agent_name="Sam", where="x"),
            "Hi Mei, Sam here. This is my last note for now, so I won't message again. "
            "If you ever want to chat, you're welcome to reach out anytime. Take care!")

    def test_default_budget_is_180_seconds_on_an_injectable_clock(self):
        now = [500.0]
        budget = DraftBudget(clock=lambda: now[0])
        now[0] += 179
        self.assertTrue(budget.allowed())
        now[0] += 1
        self.assertFalse(budget.allowed())
        import inspect
        self.assertIs(inspect.signature(DraftBudget).parameters["clock"].default, time.monotonic)

    def test_used_up_time_means_no_ai_call(self):
        now = [1000.0]
        budget = DraftBudget(seconds=90, clock=lambda: now[0])
        llm = FakeLLM(["Hi Mei, nice to meet you at the fair.", "Hi again Mei."])
        make_draft(llm, budget=budget, **self.KW)
        now[0] += 89
        self.assertTrue(budget.allowed())
        now[0] += 1
        self.assertFalse(budget.allowed())
        self.assertEqual(make_draft(llm, budget=budget, **self.KW), self.fallback())
        self.assertEqual(len(llm.calls), 1)

    def test_wording_check_failure_does_not_trip_the_budget(self):
        budget = DraftBudget()
        llm = FakeLLM(["Guaranteed returns!", "Hi Mei, nice to meet you at the fair."])
        self.assertEqual(make_draft(llm, budget=budget, **self.KW), self.fallback())
        self.assertTrue(budget.allowed())
        self.assertEqual(make_draft(llm, budget=budget, **self.KW),
                         "Hi Mei, nice to meet you at the fair.")
        self.assertEqual(len(llm.calls), 2)

    def test_no_budget_always_asks_the_ai(self):
        llm = FakeLLM(["Hi Mei, nice to meet you at the fair."])
        self.assertEqual(make_draft(llm, **self.KW), "Hi Mei, nice to meet you at the fair.")


if __name__ == "__main__":
    unittest.main()
