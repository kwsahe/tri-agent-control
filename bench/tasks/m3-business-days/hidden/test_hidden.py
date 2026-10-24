import unittest
from datetime import date

from calendar_tools.ranges import business_days

MON = date(2026, 10, 19)
SAT = date(2026, 10, 24)


class HiddenBusinessDaysTests(unittest.TestCase):
    def test_single_day(self):
        self.assertEqual(business_days(MON, MON), 1)
        self.assertEqual(business_days(SAT, SAT), 0)

    def test_end_is_inclusive_across_weeks(self):
        self.assertEqual(business_days(MON, date(2026, 10, 30)), 10)

    def test_holidays_from_a_generator(self):
        holidays = (day for day in [date(2026, 10, 21), date(2026, 10, 22)])
        self.assertEqual(business_days(MON, date(2026, 10, 23), holidays), 3)

    def test_weekend_holiday_is_not_subtracted_twice(self):
        self.assertEqual(business_days(MON, date(2026, 10, 25), [SAT]), 5)

    def test_end_before_start_raises(self):
        with self.assertRaises(ValueError):
            business_days(date(2026, 10, 23), MON)
