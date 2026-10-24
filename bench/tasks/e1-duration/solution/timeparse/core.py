"""시간 문자열 도구."""

import re

# 각 단위는 한 번씩, h → m → s 순서로만. 전부 생략 가능하지만 최소 하나는 있어야 한다 (아래에서 확인).
_DURATION = re.compile(r"\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*", re.IGNORECASE)


def format_duration(seconds: int) -> str:
    """초를 "1h 2m 3s" 형식으로 바꾼다. 0이면 "0s"."""
    if seconds < 0:
        raise ValueError("seconds must be >= 0")
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{value}{unit}" for value, unit in ((hours, "h"), (minutes, "m"), (secs, "s")) if value]
    return " ".join(parts) or "0s"


def parse_duration(text: str) -> int:
    """"1h30m" 같은 문자열을 초로 바꾼다."""
    match = _DURATION.fullmatch(text)
    if not match or not any(match.groups()):
        raise ValueError(f"invalid duration: {text!r}")
    hours, minutes, seconds = (int(value or 0) for value in match.groups())
    return hours * 3600 + minutes * 60 + seconds
