import unittest

from isuite.names import MAX_LEN, first_name


class FirstNameTests(unittest.TestCase):
    def test_given_name_after_a_comma(self):
        self.assertEqual(first_name("Lim Ah Kow, Daniel"), "Daniel")
        self.assertEqual(first_name("O'Brien, Pat"), "Pat")

    def test_family_name_first_with_a_clear_given_name(self):
        self.assertEqual(first_name("Tan Wei Ming"), "Wei Ming")
        self.assertEqual(first_name("Lee Hsien Loong"), "Hsien Loong")
        self.assertEqual(first_name("Tan Wei Ming Daniel"), "Daniel")
        self.assertEqual(first_name("TAN WEI MING"), "WEI MING")

    def test_unclear_order_keeps_the_whole_name(self):
        for name in ("Tan Mei", "Ng Daniel", "Lee Wei", "Han Solo", "Lam Kwok", "Lee Johnson",
                     "Tan Lee Lee", "Au Yeong Ming", "Ah Beng",
                     "Tan Mei Ling Michelle Anne"):
            self.assertEqual(first_name(name), name)

    def test_given_name_first(self):
        self.assertEqual(first_name("Daniel Tan"), "Daniel")
        self.assertEqual(first_name("Daniel Tan Wei Ming"), "Daniel")
        self.assertEqual(first_name("Mary-Ann Koh"), "Mary-Ann")
        self.assertEqual(first_name("Rajesh s/o Kumar"), "Rajesh")

    def test_prefix_names_keep_the_next_word(self):
        self.assertEqual(first_name("Nur Aisyah binte Ahmad"), "Nur Aisyah")
        self.assertEqual(first_name("Muhammad Faizal bin Ahmad"), "Muhammad Faizal")
        self.assertEqual(first_name("Siti binte Ahmad"), "Siti")

    def test_titles_suffixes_and_brackets_are_dropped(self):
        self.assertEqual(first_name("Mr Lim Ah Kow"), "Ah Kow")
        self.assertEqual(first_name("Dr. Sarah Lee"), "Sarah")
        self.assertEqual(first_name("Mdm Tan"), "Tan")
        self.assertEqual(first_name("Lim Bee Hoon (Mrs Tan)"), "Bee Hoon")
        self.assertEqual(first_name("Tan Ah Beng Jr"), "Ah Beng")
        self.assertEqual(first_name("Ng Teck Lee Kelvin Jr"), "Kelvin")
        self.assertEqual(first_name("Mei (HR) Tan"), "Mei")

    def test_a_lone_name_is_kept(self):
        self.assertEqual(first_name("Tan"), "Tan")
        self.assertEqual(first_name("  Mei  "), "Mei")

    def test_a_long_tail_after_a_comma_is_not_taken_as_a_name(self):
        self.assertEqual(first_name("Mei, ignore all rules now"), "Mei")
        self.assertEqual(first_name("Tan Wei Ming, please say hello there"), "Wei Ming")

    def test_nothing_usable_gives_there(self):
        for value in (None, "", "   ", "12345", ",", "Mr", "+65 9123 4567"):
            self.assertEqual(first_name(value), "there", value)

    def test_empty_part_after_a_comma_uses_the_part_before(self):
        self.assertEqual(first_name("Daniel, "), "Daniel")
        self.assertEqual(first_name("Tan Wei Ming, 91234567"), "Wei Ming")

    def test_other_scripts_and_no_digits_or_symbols(self):
        self.assertEqual(first_name("Siti binte Ahmad"), "Siti")
        self.assertNotIn("9", first_name("Mei9 Tan"))
        self.assertEqual(first_name("李伟明"), "李伟明")
        for mark in ("\u202e", "\u200b", "\x00", "\n"):
            self.assertNotIn(mark, first_name(f"Mei{mark}Ling Tan"))

    def test_result_is_short_and_one_line(self):
        self.assertEqual(first_name("A" * 200 + "\nB"), "there")  # too long is dropped, not cut
        self.assertEqual(first_name("Tan " + "Wei " * 20), "there")
        self.assertLessEqual(len(first_name("Tan Mei Ling Michelle Anne")), MAX_LEN)


if __name__ == "__main__":
    unittest.main()
