import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from inventory.models import Item
from inventory.report import low_stock, to_csv
from inventory.store import load_items


class HiddenLowStockTests(unittest.TestCase):
    def test_filters_and_sorts_by_shortage_then_sku(self):
        items = [
            Item("C-3", "자", 2, 5),     # 부족 3
            Item("A-1", "볼펜", 10, 5),  # 충분
            Item("B-2", "노트", 0, 3),   # 부족 3
            Item("D-4", "풀", 4, 4),     # 부족 0 (이하이므로 포함)
            Item("E-5", "칼", 0, 10),    # 부족 10
        ]
        self.assertEqual([item.sku for item in low_stock(items)], ["E-5", "B-2", "C-3", "D-4"])

    def test_empty(self):
        self.assertEqual(low_stock([]), [])


class HiddenCsvTests(unittest.TestCase):
    def test_header_rows_and_newlines(self):
        text = to_csv([Item("A-1", "볼펜", 10, 5)])
        self.assertEqual(text, "sku,name,qty,reorder_level\nA-1,볼펜,10,5\n")

    def test_tricky_names_survive_a_round_trip(self):
        name = '큰 "특가", 2줄\n이름'
        rows = list(csv.reader(io.StringIO(to_csv([Item("X-9", name, 1, 2)]))))
        self.assertEqual(rows, [["sku", "name", "qty", "reorder_level"], ["X-9", name, "1", "2"]])

    def test_empty_list_has_only_the_header(self):
        self.assertEqual(to_csv([]), "sku,name,qty,reorder_level\n")


class HiddenLoadTests(unittest.TestCase):
    def write(self, rows) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "items.json"
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return path

    def test_missing_reorder_level_defaults_to_zero(self):
        items = load_items(self.write([{"sku": "A-1", "name": "볼펜", "qty": 3}]))
        self.assertEqual(items, [Item("A-1", "볼펜", 3, 0)])

    def test_negative_or_non_int_values_name_the_sku(self):
        for row in [{"sku": "Z-1", "name": "x", "qty": -1, "reorder_level": 0},
                    {"sku": "Z-1", "name": "x", "qty": 1, "reorder_level": -2},
                    {"sku": "Z-1", "name": "x", "qty": "3", "reorder_level": 0}]:
            with self.subTest(row=row):
                with self.assertRaises(ValueError) as caught:
                    load_items(self.write([row]))
                self.assertIn("Z-1", str(caught.exception))
