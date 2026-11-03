"""재고 파일(JSON) 읽기·쓰기."""

import json
from pathlib import Path

from .models import Item


def load_items(path) -> list[Item]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Item(**row) for row in data]


def save_items(path, items) -> None:
    rows = [item.__dict__ for item in items]
    Path(path).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
