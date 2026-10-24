"""투자 지분 정산."""

from .allocate import allocate


def settle(total: int, shares: dict[str, int]) -> dict[str, int]:
    """지분(shares) 비율로 정산 금액을 나눈다."""
    return dict(zip(shares, allocate(total, list(shares.values()))))
