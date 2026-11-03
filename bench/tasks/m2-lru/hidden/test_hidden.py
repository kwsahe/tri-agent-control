import unittest

from cachekit import LRUCache


class HiddenLRUTests(unittest.TestCase):
    def test_get_marks_as_recently_used(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.get("a")
        cache.put("c", 3)
        self.assertEqual(cache.get("a"), 1)
        self.assertIsNone(cache.get("b"))

    def test_update_marks_as_recently_used(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("a", 10)
        cache.put("c", 3)
        self.assertEqual(cache.get("a"), 10)
        self.assertNotIn("b", cache)

    def test_missing_get_does_not_change_order(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        self.assertEqual(cache.get("zzz", "기본값"), "기본값")
        cache.put("c", 3)
        self.assertNotIn("a", cache)
        self.assertIn("b", cache)

    def test_contains_does_not_change_order(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        self.assertIn("a", cache)
        cache.put("c", 3)
        self.assertNotIn("a", cache)

    def test_none_values_are_real_entries(self):
        cache = LRUCache(2)
        cache.put("a", None)
        cache.put("b", 2)
        self.assertIsNone(cache.get("a", "없음"))   # 있는 항목이라 default가 아니라 None
        cache.put("c", 3)
        self.assertIn("a", cache)                   # 방금 읽었으니 b가 지워진다
        self.assertNotIn("b", cache)

    def test_capacity_one_and_len(self):
        cache = LRUCache(1)
        cache.put("a", 1)
        cache.put("b", 2)
        self.assertEqual(len(cache), 1)
        self.assertEqual(cache.get("b"), 2)

    def test_invalid_capacity(self):
        for capacity in [0, -1, 1.5, "2"]:
            with self.subTest(capacity=capacity):
                with self.assertRaises(ValueError):
                    LRUCache(capacity)
