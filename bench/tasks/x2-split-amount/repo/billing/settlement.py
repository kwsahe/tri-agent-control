"""투자 지분 정산."""


def settle(total: int, shares: dict[str, int]) -> dict[str, int]:
    """지분(shares) 비율로 정산 금액을 나눈다."""
    weight_sum = sum(shares.values())
    return {name: int(total * share / weight_sum) for name, share in shares.items()}
