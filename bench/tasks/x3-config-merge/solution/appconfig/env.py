"""환경변수(APP__로 시작)로 설정 덮어쓰기."""

import re

from .merge import merge

PREFIX = "APP__"
_INT = re.compile(r"[+-]?\d+")


def _convert(value: str):
    if value == "":
        return None          # merge에서 키를 지운다
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    if _INT.fullmatch(value):
        return int(value)
    return value


def load(base: dict, environ: dict) -> dict:
    override: dict = {}
    for key, value in environ.items():
        if not key.startswith(PREFIX):
            continue
        parts = key[len(PREFIX):].split("__")
        if not all(parts):
            continue
        node = override
        for part in parts[:-1]:
            node = node.setdefault(part.lower(), {})
        node[parts[-1].lower()] = _convert(value)
    return merge(base, override)
