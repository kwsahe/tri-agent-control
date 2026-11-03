import unittest

from cachekit import LRUCache


class LRUCacheTests(unittest.TestCase):
    def test_put_and_get(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        self.assertEqual(cache.get("a"), 1)
        self.assertIsNone(cache.get("missing"))

    def test_oldest_is_evicted(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        self.assertIsNone(cache.get("a"))
