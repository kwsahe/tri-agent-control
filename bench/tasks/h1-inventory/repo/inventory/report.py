"""재고 보고서."""


def total_units(items) -> int:
    return sum(item.qty for item in items)
