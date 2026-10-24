"""재고 보고서."""

import csv
import io


def total_units(items) -> int:
    return sum(item.qty for item in items)


def low_stock(items):
    """qty가 reorder_level 이하인 품목. 부족분이 큰 순, 같으면 sku 순."""
    short = [item for item in items if item.qty <= item.reorder_level]
    return sorted(short, key=lambda item: (item.qty - item.reorder_level, item.sku))


def to_csv(items) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["sku", "name", "qty", "reorder_level"])
    for item in items:
        writer.writerow([item.sku, item.name, item.qty, item.reorder_level])
    return buffer.getvalue()
