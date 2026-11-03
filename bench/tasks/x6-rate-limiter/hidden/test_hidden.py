import unittest

from throttle.bucket import TokenBucketLimiter


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class HiddenBucketTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(100.0)

    def limiter(self, capacity=3, rate=2):
        return TokenBucketLimiter(capacity, rate, clock=self.clock)

    def test_new_key_starts_full(self):
        limiter = self.limiter()
        self.assertEqual([limiter.allow("a") for _ in range(4)], [True, True, True, False])

    def test_refill_and_cap(self):
        limiter = self.limiter()
        for _ in range(3):
            limiter.allow("a")
        self.clock.now += 0.5                       # 2/초 × 0.5초 = 1개
        self.assertEqual([limiter.allow("a"), limiter.allow("a")], [True, False])
        self.clock.now += 1000
        self.assertEqual(sum(limiter.allow("a") for _ in range(5)), 3)

    def test_keys_are_independent(self):
        limiter = self.limiter(capacity=1)
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))

    def test_rejected_request_takes_nothing(self):
        limiter = self.limiter(capacity=3)
        self.assertTrue(limiter.allow("a", cost=2))
        self.assertFalse(limiter.allow("a", cost=2))
        self.assertTrue(limiter.allow("a", cost=1))

    def test_float_error_does_not_reject_a_full_token(self):
        limiter = TokenBucketLimiter(1, 1, clock=self.clock)
        self.assertTrue(limiter.allow("a"))
        for _ in range(10):
            self.clock.now += 0.1
            limiter.retry_after("a")                # 0.1초마다 조금씩 채운다
        self.assertTrue(limiter.allow("a"))

    def test_retry_after(self):
        limiter = self.limiter(capacity=1, rate=2)
        self.assertEqual(limiter.retry_after("a"), 0.0)
        limiter.allow("a")
        self.assertAlmostEqual(limiter.retry_after("a"), 0.5)
        self.clock.now += 0.25
        self.assertAlmostEqual(limiter.retry_after("a"), 0.25)
        self.assertFalse(limiter.allow("a"))      # retry_after는 토큰을 쓰지 않는다
        self.clock.now += 0.25
        self.assertEqual(limiter.retry_after("a"), 0.0)
        self.assertTrue(limiter.allow("a"))

    def test_clock_going_backwards(self):
        limiter = self.limiter(capacity=1, rate=2)
        limiter.allow("a")                          # t=100, 토큰 0
        self.clock.now = 95.0
        self.assertFalse(limiter.allow("a"))        # 흐른 시간 0, 기준 시각은 95로
        self.clock.now = 95.5
        self.assertTrue(limiter.allow("a"))         # 95 → 95.5 = 1개

    def test_invalid_arguments(self):
        for capacity, rate in [(0, 1), (-1, 1), (1, 0), (True, 1), ("3", 1), (1, None)]:
            with self.subTest(capacity=capacity, rate=rate):
                with self.assertRaises(ValueError):
                    TokenBucketLimiter(capacity, rate, clock=self.clock)
        limiter = self.limiter(capacity=2)
        for cost in [0, -1, 3]:
            with self.subTest(cost=cost):
                with self.assertRaises(ValueError):
                    limiter.allow("a", cost=cost)
                with self.assertRaises(ValueError):
                    limiter.retry_after("a", cost=cost)
