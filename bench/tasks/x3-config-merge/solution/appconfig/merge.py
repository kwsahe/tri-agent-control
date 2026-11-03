"""설정 합치기."""

import copy


def merge(base: dict, override: dict) -> dict:
    """설정 두 개를 합친다. override가 이긴다. 입력은 바꾸지 않고 새 객체를 돌려준다."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result
