import unittest

from resilience.retry import retry


class RetryTests(unittest.TestCase):
    def test_success_on_first_try(self):
        @retry(times=3)
        def ok():
            return 42

        self.assertEqual(ok(), 42)
