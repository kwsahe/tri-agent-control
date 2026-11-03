"""금액 분배."""


def allocate(total: int, weights: list[int]) -> list[int]:
    """total(원)을 weights 비율로 나눈다. 합은 항상 total (최대 잔여 방식, 정수 계산)."""
    if total < 0:
        raise ValueError("total must be >= 0")
    if any(weight < 0 for weight in weights):
        raise ValueError("weights must be >= 0")
    weight_sum = sum(weights)
    if weight_sum == 0:
        raise ValueError("weights must not all be zero")
    parts = [divmod(total * weight, weight_sum) for weight in weights]
    shares = [quotient for quotient, _ in parts]
    leftover = total - sum(shares)
    # 나머지가 큰 순, 같으면 앞쪽부터 1원씩
    order = sorted(range(len(weights)), key=lambda index: (-parts[index][1], index))
    for index in order[:leftover]:
        shares[index] += 1
    return shares
