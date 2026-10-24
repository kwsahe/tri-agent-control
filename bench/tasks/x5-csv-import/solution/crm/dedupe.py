"""중복 고객 제거."""


def normalize_email(email: str) -> str:
    return email.strip().lower()


def dedupe(customers: list[dict]) -> list[dict]:
    """같은 이메일(공백·대소문자 무시)이면 처음 나온 고객만 남긴다."""
    seen, result = set(), []
    for customer in customers:
        key = normalize_email(customer["email"])
        if key in seen:
            continue
        seen.add(key)
        result.append(customer)
    return result
