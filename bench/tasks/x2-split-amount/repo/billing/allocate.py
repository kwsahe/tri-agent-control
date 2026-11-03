"""금액 분배."""


def allocate(total: int, weights: list[int]) -> list[int]:
    """total(원)을 weights 비율로 나눈다."""
    weight_sum = sum(weights)
    return [round(total * weight / weight_sum) for weight in weights]
