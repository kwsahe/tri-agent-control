"""토큰 버킷 요청 제한."""

from .clock import monotonic

_EPSILON = 1e-9


def _positive_number(name: str, value) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be a positive number")


class TokenBucketLimiter:
    def __init__(self, capacity, refill_per_second, clock=None):
        _positive_number("capacity", capacity)
        _positive_number("refill_per_second", refill_per_second)
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._clock = clock or monotonic
        self._buckets = {}   # key -> [토큰, 기준 시각]

    def _check_cost(self, cost) -> None:
        _positive_number("cost", cost)
        if cost > self.capacity:
            raise ValueError("cost must not exceed capacity")

    def _refill(self, key) -> list:
        now = self._clock()
        bucket = self._buckets.setdefault(key, [self.capacity, now])
        elapsed = max(0.0, now - bucket[1])   # 시계가 뒤로 가면 0초
        bucket[0] = min(self.capacity, bucket[0] + elapsed * self.refill_per_second)
        bucket[1] = now
        return bucket

    def allow(self, key, cost=1) -> bool:
        self._check_cost(cost)
        bucket = self._refill(key)
        if bucket[0] + _EPSILON < cost:
            return False
        bucket[0] = max(0.0, bucket[0] - cost)
        return True

    def retry_after(self, key, cost=1) -> float:
        self._check_cost(cost)
        missing = cost - self._refill(key)[0]
        return 0.0 if missing <= _EPSILON else missing / self.refill_per_second
