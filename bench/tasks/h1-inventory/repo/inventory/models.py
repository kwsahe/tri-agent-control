from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    name: str
    qty: int
    reorder_level: int
