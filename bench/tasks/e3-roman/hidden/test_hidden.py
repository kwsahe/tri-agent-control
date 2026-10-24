import unittest

from numerals import from_roman, to_roman


class HiddenRomanTests(unittest.TestCase):
    def test_known_values(self):
        cases = {1: "I", 4: "IV", 9: "IX", 14: "XIV", 40: "XL", 90: "XC",
                 400: "CD", 1994: "MCMXCIV", 3999: "MMMCMXCIX"}
        for number, text in cases.items():
            with self.subTest(number=number):
                self.assertEqual(to_roman(number), text)
                self.assertEqual(from_roman(text), number)

    def test_round_trip_everywhere(self):
        for number in range(1, 4000):
            self.assertEqual(from_roman(to_roman(number)), number)

    def test_out_of_range_or_wrong_type(self):
        for value in [0, -5, 4000, 2.0, "10", True]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    to_roman(value)

    def test_non_standard_text_is_rejected(self):
        for text in ["", "IIII", "IC", "MMMM", "VX", "iv", "XIIV", "ABC", " X", "IIX"]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    from_roman(text)
