"""시간 문자열 도구."""


def format_duration(seconds: int) -> str:
    """초를 "1h 2m 3s" 형식으로 바꾼다. 0이면 "0s"."""
    if seconds < 0:
        raise ValueError("seconds must be >= 0")
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{value}{unit}" for value, unit in ((hours, "h"), (minutes, "m"), (secs, "s")) if value]
    return " ".join(parts) or "0s"
