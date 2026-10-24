import unittest

from timeparse.core import format_duration, parse_duration


class ParseDurationTests(unittest.TestCase):
    def test_hours_and_minutes(self):
        self.assertEqual(parse_duration("1h30m"), 5400)


class FormatDurationTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_duration(3723), "1h 2m 3s")
