"""시계. 테스트에서 가짜 시계로 바꿔 끼울 수 있게 함수 하나로 둔다."""

import time


def monotonic() -> float:
    return time.monotonic()
