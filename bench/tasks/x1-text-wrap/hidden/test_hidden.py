import unittest

from textfmt.width import display_width
from textfmt.wrap import wrap


class HiddenWrapTests(unittest.TestCase):
    def test_korean_counts_two_columns(self):
        self.assertEqual(wrap("가나다 라마", 6), ["가나다", "라마"])
        self.assertEqual(wrap("가 나 다", 5), ["가 나", "다"])

    def test_long_words_are_split_without_halving_wide_chars(self):
        self.assertEqual(wrap("abcdefgh", 3), ["abc", "def", "gh"])
        self.assertEqual(wrap("가나다라마", 5), ["가나", "다라", "마"])
        self.assertEqual(wrap("ab가c", 3), ["ab", "가c"])

    def test_long_word_starts_a_new_line_and_the_next_word_may_follow(self):
        self.assertEqual(wrap("a abcdef g", 4), ["a", "abcd", "ef g"])

    def test_whitespace_is_collapsed(self):
        self.assertEqual(wrap("  a   b\tc  ", 10), ["a b c"])

    def test_paragraphs_and_blank_lines_are_kept(self):
        self.assertEqual(wrap("a\n\nb c", 3), ["a", "", "b c"])
        self.assertEqual(wrap("", 5), [""])
        self.assertEqual(wrap("   ", 5), [""])
        self.assertEqual(wrap("a\n", 5), ["a", ""])

    def test_invalid_width(self):
        for width in [1, 0, -3, 2.5, "4"]:
            with self.subTest(width=width):
                with self.assertRaises(ValueError):
                    wrap("abc", width)

    def test_no_line_is_ever_too_wide(self):
        text = "TriAgent는 세 모델이 함께 코딩하는 도구다 supercalifragilistic 한글과English가섞인단어"
        for width in range(2, 20):
            with self.subTest(width=width):
                lines = wrap(text, width)
                self.assertTrue(all(display_width(line) <= width for line in lines), lines)
                self.assertEqual("".join(lines).replace(" ", ""), text.replace(" ", ""))
