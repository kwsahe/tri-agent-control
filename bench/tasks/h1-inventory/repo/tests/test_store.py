import tempfile
import unittest
from pathlib import Path

from inventory.models import Item
from inventory.report import total_units
from inventory.store import load_items, save_items


class StoreTests(unittest.TestCase):
    def test_round_trip(self):
        items = [Item("A-1", "볼펜", 10, 5), Item("B-2", "노트", 0, 3)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "items.json"
            save_items(path, items)
            self.assertEqual(load_items(path), items)
        self.assertEqual(total_units(items), 10)
