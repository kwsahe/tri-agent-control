"""실패한 호출 다시 시도하기."""

import functools
import time


def retry(times=3, exceptions=(Exception,), delay=0.1, backoff=2.0, max_delay=10.0, sleep=None, on_retry=None):
    if isinstance(times, bool) or not isinstance(times, int) or times < 1:
        raise ValueError("times must be an int >= 1")

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, times + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt == times:
                        raise
                    if on_retry is not None:
                        on_retry(attempt, exc)
                    (sleep or time.sleep)(min(delay * backoff ** (attempt - 1), max_delay))
        return wrapper
    return decorator
