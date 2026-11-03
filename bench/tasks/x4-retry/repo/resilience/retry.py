"""실패한 호출 다시 시도하기."""

import time


def retry(times=3, exceptions=(Exception,), delay=0.1):
    def decorator(func):
        def wrapper(*args, **kwargs):
            for _attempt in range(times):
                try:
                    return func(*args, **kwargs)
                except exceptions:
                    time.sleep(delay)
            return None
        return wrapper
    return decorator
