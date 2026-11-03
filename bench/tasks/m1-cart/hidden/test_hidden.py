import unittest

from shop.cart import Cart


def cart_of(*prices):
    cart = Cart()
    for price in prices:
        cart.add("상품", price)
    return cart


class HiddenCartTests(unittest.TestCase):
    def test_half_won_rounds_up(self):
        self.assertEqual(cart_of("15").total(), 17)     # 16.5 → 17
        self.assertEqual(cart_of("35").total(), 39)     # 38.5 → 39

    def test_coupon_then_tax(self):
        self.assertEqual(cart_of("1000").total(coupon_percent=10), 990)
        self.assertEqual(cart_of("150").total(coupon_percent=10), 149)    # 148.5 → 149
        self.assertEqual(cart_of("300").total(coupon_percent=15), 281)    # 280.5 → 281

    def test_tax_rate_is_configurable(self):
        self.assertEqual(cart_of("1000").total(tax_rate="0"), 1000)
        self.assertEqual(cart_of("10").total(tax_rate="0.05"), 11)                    # 10.5 → 11
        self.assertEqual(cart_of("100").total(coupon_percent=10, tax_rate="0.05"), 95)  # 94.5 → 95

    def test_full_coupon_is_free(self):
        self.assertEqual(cart_of("9999").total(coupon_percent=100), 0)

    def test_invalid_coupons_raise(self):
        for coupon in [-1, 101, 10.5, "10", True]:
            with self.subTest(coupon=coupon):
                with self.assertRaises(ValueError):
                    cart_of("1000").total(coupon_percent=coupon)

    def test_returns_int(self):
        self.assertIs(type(cart_of("1234").total()), int)
