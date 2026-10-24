import unittest

from billing.allocate import allocate
from billing.invoice import Invoice
from billing.settlement import settle


class BillingTests(unittest.TestCase):
    def test_even_split(self):
        self.assertEqual(allocate(100, [1, 1]), [50, 50])
        self.assertEqual(Invoice(100).split_by_people(2), [50, 50])
        self.assertEqual(settle(100, {"a": 1, "b": 1}), {"a": 50, "b": 50})
