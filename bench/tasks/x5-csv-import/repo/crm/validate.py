"""행 검사."""

import re

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_row(row: dict) -> str | None:
    if not _EMAIL.match(row.get("email") or ""):
        return "이메일 형식이 아닙니다"
    return None
