"""숫자 표기 변환."""

# 큰 값부터. 빼기 표기(CM, XC 등)도 포함한다.
ROMAN_SYMBOLS = [
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
]


def to_roman(n):
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 3999:
        raise ValueError("n must be an int in 1..3999")
    parts = []
    for value, symbol in ROMAN_SYMBOLS:
        count, n = divmod(n, value)
        parts.append(symbol * count)
    return "".join(parts)


def from_roman(text):
    total, index = 0, 0
    for value, symbol in ROMAN_SYMBOLS:
        while text.startswith(symbol, index):
            total += value
            index += len(symbol)
    # 다시 표준 표기로 바꿔 같은지 보면 "IIII"·"IC" 같은 비표준 표기가 걸러진다.
    if index != len(text) or not 1 <= total <= 3999 or to_roman(total) != text:
        raise ValueError(f"not a standard roman numeral: {text!r}")
    return total
