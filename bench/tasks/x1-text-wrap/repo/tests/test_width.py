import unittest

from textfmt.width import display_width


class WidthTests(unittest.TestCase):
    def test_korean_is_two_columns(self):
        self.assertEqual(display_width("ab가"), 4)


class WrapTests(unittest.TestCase):
    def test_simple_words(self):
        from textfmt.wrap import wrap
        self.assertEqual(wrap("hello world foo", 11), ["hello world", "foo"])
