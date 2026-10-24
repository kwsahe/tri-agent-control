import unittest

from numerals import from_roman, to_roman


class RomanTests(unittest.TestCase):
    def test_to_roman(self):
        self.assertEqual(to_roman(2024), "MMXXIV")

    def test_from_roman(self):
        self.assertEqual(from_roman("XLII"), 42)
