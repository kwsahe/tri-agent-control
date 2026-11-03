"""화면 폭에 맞춘 줄 바꿈."""

from .width import char_width, display_width


def _split_word(word: str, width: int) -> list[str]:
    pieces, current = [], ""
    for ch in word:
        if display_width(current) + char_width(ch) > width:
            pieces.append(current)
            current = ""
        current += ch
    return pieces + [current]


def _wrap_paragraph(words: list[str], width: int) -> list[str]:
    lines, current = [], ""
    for word in words:
        if display_width(word) > width:
            if current:
                lines.append(current)
            *full, current = _split_word(word, width)
            lines.extend(full)
            continue
        candidate = f"{current} {word}" if current else word
        if display_width(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def wrap(text: str, width: int) -> list[str]:
    if isinstance(width, bool) or not isinstance(width, int) or width < 2:
        raise ValueError("width must be an int >= 2")
    lines = []
    for paragraph in text.split("\n"):
        lines.extend(_wrap_paragraph(paragraph.split(), width))
    return lines
