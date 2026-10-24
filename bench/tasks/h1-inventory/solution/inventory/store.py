"""재고 파일(JSON) 읽기·쓰기."""

import json
from pathlib import Path

from .models import Item


def load_items(path) -> list[Item]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = []
    for row in data:
        row = {"reorder_level": 0, **row}   # 예전 파일에는 reorder_level이 없다
        for field in ("qty", "reorder_level"):
            value = row[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{row.get('sku', '?')}: {field}는 0 이상의 정수여야 합니다 (값: {value!r})")
        items.append(Item(**row))
    return items


def save_items(path, items) -> None:
    rows = [item.__dict__ for item in items]
    Path(path).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
