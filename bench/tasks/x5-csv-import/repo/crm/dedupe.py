"""중복 고객 제거."""


def dedupe(customers: list[dict]) -> list[dict]:
    """같은 이메일이면 처음 나온 고객만 남긴다."""
    seen, result = set(), []
    for customer in customers:
        if customer["email"] in seen:
            continue
        seen.add(customer["email"])
        result.append(customer)
    return result
