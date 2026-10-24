import unittest

from shop.cart import Cart


class CartTests(unittest.TestCase):
    def test_total_without_coupon(self):
        cart = Cart()
        cart.add("펜", "1000", 2)
        self.assertEqual(cart.total(), 2200)

    def test_subtotal(self):
        cart = Cart()
        cart.add("펜", "1000", 2)
        cart.add("노트", "2500")
        self.assertEqual(cart.subtotal(), 4500)
