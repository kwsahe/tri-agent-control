"""화면 폭 계산."""

import unicodedata


def char_width(ch: str) -> int:
    """한글·한자 같은 전각 문자는 2칸, 나머지는 1칸."""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)
