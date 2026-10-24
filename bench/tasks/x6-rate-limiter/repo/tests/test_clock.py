import unittest

from throttle.clock import monotonic


class ClockTests(unittest.TestCase):
    def test_monotonic_moves_forward(self):
        self.assertLessEqual(monotonic(), monotonic())


class BucketTests(unittest.TestCase):
    def test_first_request_is_allowed(self):
        from throttle.bucket import TokenBucketLimiter
        self.assertTrue(TokenBucketLimiter(1, 1).allow("user"))
