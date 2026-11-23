import unittest

from src.device_labels import display_titles, strip_ordinal


class StripOrdinalTests(unittest.TestCase):
    def test_numbered_endpoint_title_loses_number_and_no_leading_space(self):
        self.assertEqual(strip_ordinal("2- MPOW Wireless Gaming Headset"), "MPOW Wireless Gaming Headset")

    def test_number_without_space_after_dash(self):
        self.assertEqual(strip_ordinal("2-MPOW"), "MPOW")

    def test_spaces_around_number_are_removed(self):
        self.assertEqual(strip_ordinal("  12 -  MPOW "), "MPOW")

    def test_title_that_is_only_a_number_is_unchanged(self):
        self.assertEqual(strip_ordinal("12"), "12")
        self.assertEqual(strip_ordinal(" 2- "), "2-")

    def test_plain_title_is_unchanged(self):
        self.assertEqual(strip_ordinal("MPOW"), "MPOW")

    def test_cyrillic_title_is_unchanged(self):
        self.assertEqual(strip_ordinal("Jabra EVOLVE 20 MS"), "Jabra EVOLVE 20 MS")
        self.assertEqual(strip_ordinal("ÐœÐ¸ÐºÑ€Ð¾Ñ„Ð¾Ð½"), "ÐœÐ¸ÐºÑ€Ð¾Ñ„Ð¾Ð½")

    def test_title_never_starts_or_ends_with_space(self):
        for raw in ("2- MPOW ", "  3 -MPOW Headset  ", "4-  Name"):
            shown = strip_ordinal(raw)
            self.assertEqual(shown, shown.strip(), raw)


class DisplayTitlesTests(unittest.TestCase):
    def test_single_headset_drops_the_number(self):
        titles = display_titles(["Microphone (2- MPOW Wireless Gaming Headset)"])
        self.assertEqual(titles["Microphone (2- MPOW Wireless Gaming Headset)"], "MPOW Wireless Gaming Headset")

    def test_two_identical_connected_headsets_keep_their_numbers(self):
        first = "Microphone (1- MPOW Wireless Gaming Headset)"
        second = "Microphone (2- MPOW Wireless Gaming Headset)"
        titles = display_titles([first, second])
        self.assertEqual(titles[first], "1- MPOW Wireless Gaming Headset")
        self.assertEqual(titles[second], "2- MPOW Wireless Gaming Headset")

    def test_numbered_and_plain_name_that_would_clash_keep_the_number(self):
        numbered = "Microphone (2- MPOW)"
        plain = "Headset (MPOW)"
        titles = display_titles([numbered, plain])
        self.assertEqual(titles[numbered], "2- MPOW")
        self.assertEqual(titles[plain], "MPOW")

    def test_full_names_are_not_changed(self):
        name = "Microphone (2- MPOW Wireless Gaming Headset)"
        self.assertIn(name, display_titles([name]))


if __name__ == "__main__":
    unittest.main()
