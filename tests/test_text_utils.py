import unittest

from isuite.phone import normalize_sg
from isuite.redact import has_nric, redact
from isuite.whatsapp import wa_link


class TextUtilsTest(unittest.TestCase):
    def test_sg_formats_all_normalize(self):
        for raw in ["9123 4567", "+65 9123-4567", "6591234567", "(65)91234567", "65 9123.4567"]:
            self.assertEqual(normalize_sg(raw), ("+6591234567", True), raw)

    def test_landline_and_8_prefix_valid(self):
        self.assertEqual(normalize_sg("6123 4567"), ("+6561234567", True))
        self.assertEqual(normalize_sg("8123 4567"), ("+6581234567", True))

    def test_foreign_number_flagged(self):
        self.assertEqual(normalize_sg("+60 12-345 6789"), ("+60123456789", False))

    def test_short_number_flagged(self):
        self.assertFalse(normalize_sg("91234")[1])

    def test_empty(self):
        self.assertEqual(normalize_sg(""), ("", False))

    def test_wa_link_encodes_unicode_and_symbols(self):
        self.assertEqual(
            wa_link("+6591234567", "Hi 美玲 😊 Q&A? #1"),
            "https://wa.me/6591234567?text=Hi%20%E7%BE%8E%E7%8E%B2%20%F0%9F%98%8A%20Q%26A%3F%20%231",
        )

    def test_redact_nric(self):
        self.assertEqual(redact("S1234567D and t7654321z"), "[ID removed] and [ID removed]")

    def test_redact_policy_no(self):
        self.assertEqual(
            redact("Policy No: 0012345678 term"), "Policy No: [policy no. removed] term"
        )
        self.assertEqual(redact("policy number 55-A/9"), "policy number [policy no. removed]")
        self.assertEqual(redact("policy # 123"), "policy # [policy no. removed]")

    def test_redact_leaves_phone(self):
        self.assertEqual(redact("call 91234567"), "call 91234567")


    def test_redact_nric_with_spaces_or_hyphens(self):
        for raw in ("S 1234567 D", "S1234567-D", "S-1234567-D", "T 7654321Z", "G1234567 X"):
            self.assertEqual(redact(f"id {raw} ok"), "id [ID removed] ok", raw)

    def test_redact_policy_label_variants(self):
        cases = {
            "policy#123": "policy#[policy no. removed]",
            "policy 12345678": "policy [policy no. removed]",
            "policy AB123456 due": "policy [policy no. removed] due",
            "pol no 12345678": "pol no [policy no. removed]",
            "Pol. no. 1234": "Pol. no. [policy no. removed]",
            "policy no 00 1234 5678": "policy no [policy no. removed]",
            "policy no 00 1234 5678 thanks": "policy no [policy no. removed] thanks",
            "policy number is ABC123": "policy number is [policy no. removed]",
        }
        for raw, want in cases.items():
            self.assertEqual(redact(raw), want, raw)
            self.assertFalse(any(ch.isdigit() for ch in redact(raw)), raw)

    def test_redact_leaves_ordinary_words_and_phones(self):
        for text in ("policy note here", "policy nobody likes", "policy notes here",
                     "policy 2024 review", "policy holders", "call 91234567", "91234567",
                     "Tan 9123 4567", "+65 9123 4567"):
            self.assertEqual(redact(text), text)

    def test_redact_bare_policy_with_a_separator(self):
        for raw in ("Policy: 9876543210", "policy - 9876543210", "policy:AB123456",
                    "policy # 12345678", "policy:\t12345678", "Policy-12345678 due"):
            out = redact(raw)
            self.assertIn("[policy no. removed]", out, raw)
            self.assertFalse(any(ch.isdigit() for ch in out), raw)

    def test_labelled_policy_is_removed_even_when_it_looks_like_a_phone(self):
        for raw in ("policy no 91234567", "policy number: 91234567", "policy # 91234567",
                    "pol no 81234567", "Policy No: 6591234567"):
            out = redact(raw)
            self.assertIn("[policy no. removed]", out, raw)
            self.assertFalse(any(ch.isdigit() for ch in out), raw)

    def test_phone_after_the_bare_word_policy_is_kept(self):
        # Ruling: an 8-digit policy number starting 3/6/8/9 written after the bare word
        # "policy" cannot be told from a phone number, so it is kept.
        for text in ("asked about policy 91234567", "policy 9123 4567", "policy 9123-4567",
                     "policy: 91234567", "policy 65 9123 4567", "policy 6591234567",
                     "policy 81234567", "policy 61234567", "policy 31234567"):
            self.assertEqual(redact(text), text)

    def test_things_that_must_be_removed(self):
        for raw in ("policy 12345678", "policy 21234567", "policy 91234567 12345678",
                    "policy AB91234567", "policy no 00 1234 5678", "pol no. 123456",
                    "policy number is ABC123", "NRIC S1234567D", "policy 7123456789"):
            out = redact(raw)
            self.assertTrue("[policy no. removed]" in out or "[ID removed]" in out, raw)

    def test_things_that_must_survive(self):
        for text in ("policy note", "policy nobody", "policy 2024 review", "call 91234567",
                     "Block 5 1234567", "policy holders", "policy: none", "policy - ok"):
            self.assertEqual(redact(text), text)

    def test_more_policy_label_forms_are_removed(self):
        cases = ["policy no is: 12345678", "policy num 12345678", "policy num: 12345678",
                 "policy nr 12345678", "policy ref 123456789", "policy id 123456789",
                 "policy nos 12345678", "Policy No (12345678)", 'policy no "12345678"',
                 "policy no\n12345678", "policy\nno 12345678", "policy\u3000no\u300012345678",
                 "policy\u00a0no\u202f12345678", "policy no 00\u30001234\u30005678",
                 "policy\u300012345678", "policy no \u201c12345678\u201d"]
        for raw in cases:
            out = redact(raw)
            self.assertIn("[policy no. removed]", out, repr(raw))
            self.assertFalse(any(ch.isdigit() for ch in out), repr(raw))

    def test_a_list_of_policy_numbers_is_removed_whole(self):
        for raw in ("policy numbers 12345678, 87654321", "policy nos 12345678; 87654321",
                    "policy numbers 12345678 and 87654321"):
            out = redact(raw)
            self.assertFalse(any(ch.isdigit() for ch in out), raw)
        self.assertEqual(redact("policy no 12345678, call me at 5 pm"),
                         "policy no [policy no. removed], call me at 5 pm")

    def test_nric_glued_to_following_digits_is_removed(self):
        self.assertEqual(redact("S1234567D91234567"), "[ID removed]91234567")
        self.assertEqual(redact("ic S1234567D, 91234567"), "ic [ID removed], 91234567")
        self.assertEqual(redact("S1234567\u3000D ok"), "[ID removed] ok")

    def test_nric_inside_a_longer_word_is_left_alone(self):
        for text in ("AS1234567D", "1S1234567D", "S1234567Dx", "S1234567DX"):
            self.assertEqual(redact(text), text)

    def test_more_things_that_must_survive(self):
        for text in ("policy-holder John 91234567", "policy: yes", "policy - term life",
                     "policy term 20 years", "policy 2026", "policy 5", "Block 123 #12-34",
                     "call 91234567", "policy notes here", "policy number of claims",
                     "policy idea 2024", "policy 9123\u30004567"):
            self.assertEqual(redact(text), text)

    def _assert_all_numbers_gone(self, raws, digits=("1234", "5678", "4321", "8765")):
        for raw in raws:
            out = redact(raw)
            self.assertIn("[policy no. removed]", out, repr(raw))
            for d in digits:
                self.assertNotIn(d, out, repr(raw))

    def test_an_index_before_the_policy_number_does_not_hide_it(self):
        self._assert_all_numbers_gone([
            "policy no 1: 12345678", "Policy #1: 12345678", "policy no 1 is 12345678",
            "Policy #1: 12345678, policy #2: 87654321", "policy no. 2 - 12345678",
            "policy 1: 12345678", "policy #1: 12345678, #2: 87654321"])

    def test_dash_equals_are_and_bracketed_is_between_label_and_number(self):
        self._assert_all_numbers_gone([
            "policy no - 12345678", "policy no = 12345678", "policy numbers are 12345678",
            "policy no (is) 12345678", "policy no \u2013 12345678", "policy no: = 12345678",
            "policy numbers are 12345678 and 87654321"])

    def test_longer_labels(self):
        self._assert_all_numbers_gone([
            "policy reference: 12345678", "policy reference number 12345678",
            "policy identifier 12345678", "policy certificate no 12345678",
            "policy certificate no. 12345678", "policy contract no 12345678",
            "Policy Contract Number: 12345678", "pol ref 12345678"])

    def test_several_numbers_joined_by_symbols_or_words_all_go(self):
        self._assert_all_numbers_gone([
            "policy no 12345678 & 87654321", "policy no 12345678 / 87654321",
            "policy no 12345678/87654321", "policy no 12345678 or 87654321",
            "policy no 12345678 + 87654321", "policy nos 12345678, 87654321",
            "policy no 12345678, or 87654321", "policy no 12345678 &87654321",
            "policy numbers: 12345678 & 87654321 & 11224433"], digits=("1234", "5678", "4321", "8765", "4433"))

    def test_zero_width_and_full_width_characters_do_not_hide_a_number(self):
        self._assert_all_numbers_gone([
            "policy no 1234\u200b5678", "policy no \uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18",
            "pol\u200bicy no 12345678", "policy no\u200b 12345678", "policy no 1234\u200d\u200c5678",
            "policy no \u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668"], digits=("1234", "5678", "\u0661\u0662"))
        for raw in ("S1234\u200b567D", "\uff33\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff24",
                    "S1234567  D", "S  1234567 D", "S 1234567  D", "S1234567 - D"):
            self.assertEqual(redact(f"id {raw} ok"), "id [ID removed] ok", repr(raw))
            self.assertTrue(has_nric(raw), repr(raw))

    def test_new_forms_do_not_over_redact(self):
        for text in ("policy review on 12 Dec, call 91234567", "policy no change, call 91234567",
                     "policy reference letter sent", "policy identifier unknown",
                     "policy contract signed on Monday", "policy and 91234567",
                     "policy or 91234567", "met 91234567 & 81234567", "policy: yes & no",
                     "policy 5 - ok", "policy is 91234567", "policy are 12", "policy = none",
                     "call 91234567 or 81234567", "Tan 91234567 / Lee 81234567",
                     "policy review 1: 91234567", "policy holder S ok", "S 1234 D",
                     "Block 12 #03-45, call 91234567", "\uff11\uff12 Dec call 91234567"):
            self.assertEqual(redact(text), text, text)  # stored exactly as typed

    def test_phone_after_the_word_policy_survives_next_to_new_separators(self):
        self.assertEqual(redact("policy 91234567 & Lee 81234567"), "policy 91234567 & Lee 81234567")
        self.assertEqual(redact("Tan 91234567 policy no 1: 12345678"),
                         "Tan 91234567 policy no 1: [policy no. removed]")

    def test_index_lists_hyphen_labels_connectives_and_dotted_numbers(self):
        self._assert_all_numbers_gone([
            "policy nos 1 & 2: 12345678", "policy nos. 1,2: 12345678",
            "policy nos 1 and 2: 12345678", "policy nos 1, 2 & 3: 12345678",
            "policy-no 12345678", "policy-number 12345678", "policy_no 12345678",
            "policy-no: 12345678", "policy_number = 12345678", "Policy-Reference-No 12345678",
            "policy no 12345678 and also 87654321", "policy no 12345678 as well as 87654321",
            "policy no 12345678 plus 87654321", "policy no 12345678 then 87654321",
            "policy no 12345678, plus 87654321", "policy no 12345678 and then 87654321",
            "policy no 1.12345678", "policy no 12345678.87654321"])
        self.assertEqual(redact("policy no 1.12345678"), "policy no 1.[policy no. removed]")

    def test_phones_dates_and_amounts_next_to_the_new_forms_stay(self):
        for text in ("policy no 12345678 and call 91234567", "policy no 12345678, hp 91234567",
                     "policy no 12345678 and also call 91234567",
                     "policy no 12345678 plus tel 81234567",
                     "policy no 12345678 then whatsapp 91234567",
                     "policy no 12345678 or mobile 61234567",
                     "policy no 12345678 and contact 91234567",
                     "policy no 12345678, phone: 91234567",
                     "policy no 12345678 +6591234567", "policy no 12345678 and +6591234567",
                     "policy no 12345678 +65 9123 4567",
                     "policy nos 1 & 2 are fine, call 91234567",
                     "policy-holder Tan, call 91234567", "policy-note: call 91234567",
                     "policy_review on 12 Dec, hp 91234567",
                     "policy no 3 and also 5 more, call 91234567",
                     "policy nos 1 & 2: none, call 91234567",
                     "policy no 12345678 then on 12 Dec", "policy no 12345678 plus $500 fee",
                     "policy no 12345678 and also paid $1,200.50", "policy no 12345678 on 12.11.2026",
                     "policy no 12345678. Call 91234567"):
            out = redact(text)
            for phone in ("91234567", "81234567", "61234567", "9123 4567"):
                self.assertEqual(phone in text, phone in out, text)
            self.assertNotIn("12345678", out, text)
        self.assertEqual(redact("policy no 12345678 then on 12 Dec"),
                         "policy no [policy no. removed] then on 12 Dec")
        self.assertEqual(redact("policy no 12345678 plus $500 fee"),
                         "policy no [policy no. removed] plus $500 fee")
        self.assertEqual(redact("policy no 12345678. Call 91234567"),
                         "policy no [policy no. removed]. Call 91234567")
        self.assertEqual(redact("policy no 12345678.\n91234567"),
                         "policy no [policy no. removed].\n91234567")

    def test_text_that_is_not_a_number_is_stored_exactly_as_typed(self):
        for text in ("\U0001F468\u200d\U0001F469\u200d\U0001F467 family",
                     "\uff34\uff41\uff4e\uff0c\u674e\u660e\uff08Ben\uff09",
                     "Tamil \u0be7\u0be8\u0be9 and Arabic \u0661\u0662\u0663 digits",
                     "zero\u200bwidth \u200bin the text\u200b", "\u200b", "", "plain"):
            self.assertEqual(redact(text), text, repr(text))
        self.assertEqual(redact(None), "")

    def test_only_the_number_is_replaced_in_the_original(self):
        self.assertEqual(redact("\uff34\uff41\uff4e\uff0c policy no \uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18 \u674e\u660e"),
                         "\uff34\uff41\uff4e\uff0c policy no [policy no. removed] \u674e\u660e")
        self.assertEqual(redact("\U0001F468\u200d\U0001F469 policy no 1234\u200b5678 ok\u200b"),
                         "\U0001F468\u200d\U0001F469 policy no [policy no. removed] ok\u200b")
        self.assertEqual(redact("\u200bS1234567D\u200b"), "\u200b[ID removed]")
        self.assertEqual(redact("\u200bS1234567D!"), "\u200b[ID removed]!")

    def test_redaction_of_a_long_adversarial_text_is_fast(self):
        import time
        cases = ["policy no " + "1 " * 5000, "policy" + " " * 10000 + "x",
                 "policy no 1" + " " * 10000 + "x", "policy no " + "(" * 10000,
                 "S " * 5000, "policy no 12345678 and " * 450, "policy nos " + "1 & " * 2500,
                 "policy-" * 1400, "policy no " + "1,2," * 2500, "pol " + "no " * 3300,
                 "policy no 1" + ", " * 5000, "policy no 12345678" + " also " * 1600,
                 "policy no " + "9" * 10000, "\u200b" * 10000 + "policy no 12345678"]
        for text in cases:
            start = time.perf_counter()
            redact(text)
            self.assertLess(time.perf_counter() - start, 0.1, text[:30])

    def test_phone_digits_are_ascii_only(self):
        self.assertEqual(normalize_sg("9\u0661\u0662\u0663\u0664\u0665\u0666\u0667")[1], False)
        self.assertEqual(normalize_sg("\uff19\uff11\uff12\uff13\uff14\uff15\uff16\uff17"), ("", False))

if __name__ == "__main__":
    unittest.main()
