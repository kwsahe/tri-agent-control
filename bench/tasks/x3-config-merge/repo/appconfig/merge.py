"""설정 합치기."""


def merge(base: dict, override: dict) -> dict:
    """설정 두 개를 합친다. override가 이긴다."""
    result = base
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result
