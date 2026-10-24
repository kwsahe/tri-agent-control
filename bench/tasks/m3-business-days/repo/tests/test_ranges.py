import unittest
from datetime import date

from calendar_tools.ranges import business_days


class BusinessDaysTests(unittest.TestCase):
    def test_full_week(self):
        # 2026-10-19(월) ~ 2026-10-23(금)
        self.assertEqual(business_days(date(2026, 10, 19), date(2026, 10, 23)), 5)
