"""장바구니 금액 계산. 가격은 원 단위 문자열로 받는다 (예: "1200")."""

from decimal import ROUND_HALF_UP, Decimal


class Cart:
    def __init__(self):
        self.items = []

    def add(self, name: str, unit_price: str, qty: int = 1) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        self.items.append((name, Decimal(unit_price), qty))

    def subtotal(self) -> Decimal:
        return sum((price * qty for _name, price, qty in self.items), Decimal(0))

    def total(self, coupon_percent: int = 0, tax_rate: str = "0.1") -> int:
        """쿠폰 할인과 부가세를 적용한 최종 금액(원)."""
        if isinstance(coupon_percent, bool) or not isinstance(coupon_percent, int) or not 0 <= coupon_percent <= 100:
            raise ValueError("coupon_percent must be an int in 0..100")
        discounted = self.subtotal() * (100 - coupon_percent) / 100
        amount = discounted * (1 + Decimal(tax_rate))
        return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))
