import unittest

from billing.allocate import allocate
from billing.invoice import Invoice
from billing.settlement import settle


class HiddenAllocateTests(unittest.TestCase):
    def test_largest_remainder_with_earlier_first_on_ties(self):
        self.assertEqual(allocate(100, [1, 1, 1]), [34, 33, 33])
        self.assertEqual(allocate(7, [1, 1, 1, 1]), [2, 2, 2, 1])
        self.assertEqual(allocate(5, [0, 1, 1]), [0, 3, 2])
        self.assertEqual(allocate(10, [3, 1]), [8, 2])       # 7.5 / 2.5 → 소수 같음 → 앞쪽

    def test_bigger_fraction_gets_the_extra_won(self):
        self.assertEqual(allocate(10, [1, 2]), [3, 7])       # 3.33 / 6.67 → 0.67 쪽이 1원 더

    def test_sum_always_matches(self):
        for total in [0, 1, 99, 1001, 10**15 + 7]:
            for weights in ([1, 1, 1], [3, 7, 11, 13], [1, 0, 2], [10**9, 1, 1]):
                with self.subTest(total=total, weights=weights):
                    self.assertEqual(sum(allocate(total, weights)), total)

    def test_no_float_error_on_huge_amounts(self):
        self.assertEqual(allocate(10**17 + 1, [1, 1]), [5 * 10**16 + 1, 5 * 10**16])

    def test_invalid_inputs(self):
        for total, weights in [(10, [1, -1, 2]), (10, [0, 0]), (10, []), (-5, [1, 1])]:
            with self.subTest(total=total, weights=weights):
                with self.assertRaises(ValueError):
                    allocate(total, weights)


class HiddenCallerTests(unittest.TestCase):
    def test_invoice_follows_the_same_rule(self):
        self.assertEqual(Invoice(100).split_by_people(3), [34, 33, 33])
        for people in [0, -1]:
            with self.subTest(people=people):
                with self.assertRaises(ValueError):
                    Invoice(100).split_by_people(people)

    def test_settlement_follows_the_same_rule(self):
        self.assertEqual(settle(100, {"a": 1, "b": 1, "c": 1}), {"a": 34, "b": 33, "c": 33})
        self.assertEqual(settle(1, {"x": 1, "y": 1}), {"x": 1, "y": 0})
        result = settle(1001, {"kim": 3, "lee": 7, "park": 11})
        self.assertEqual(sum(result.values()), 1001)
        self.assertEqual(list(result), ["kim", "lee", "park"])

    def test_settlement_rejects_bad_shares(self):
        with self.assertRaises(ValueError):
            settle(100, {"a": 0, "b": 0})
