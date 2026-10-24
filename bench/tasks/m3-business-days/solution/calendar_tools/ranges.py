"""기간 계산."""

from datetime import date, timedelta


def business_days(start: date, end: date, holidays=()) -> int:
    """start부터 end까지(둘 다 포함) 주말과 공휴일을 뺀 영업일 수."""
    if end < start:
        raise ValueError("end must not be before start")
    holidays = set(holidays)   # 제너레이터는 한 번만 읽을 수 있다
    days = 0
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in holidays:
            days += 1
        current += timedelta(days=1)
    return days
