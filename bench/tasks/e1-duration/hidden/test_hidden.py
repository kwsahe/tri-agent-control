import unittest

from timeparse.core import format_duration, parse_duration


class HiddenParseDurationTests(unittest.TestCase):
    def test_single_units(self):
        self.assertEqual(parse_duration("45s"), 45)
        self.assertEqual(parse_duration("2m"), 120)
        self.assertEqual(parse_duration("3h"), 10800)

    def test_all_units_with_spaces(self):
        self.assertEqual(parse_duration("2h 5m 10s"), 7510)
        self.assertEqual(parse_duration("  1h  30m "), 5400)

    def test_case_insensitive(self):
        self.assertEqual(parse_duration("1H30M"), 5400)

    def test_zero_is_allowed(self):
        self.assertEqual(parse_duration("0s"), 0)

    def test_invalid_inputs_raise(self):
        for text in ["", "   ", "10", "5x", "5s1m", "1m1m", "h", "1.5h", "-1h"]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_duration(text)

    def test_round_trip_with_format(self):
        for seconds in [1, 59, 60, 3599, 3600, 3723, 86399]:
            with self.subTest(seconds=seconds):
                self.assertEqual(parse_duration(format_duration(seconds)), seconds)
