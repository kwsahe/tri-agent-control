import unittest

from appconfig.merge import merge


class MergeTests(unittest.TestCase):
    def test_flat(self):
        self.assertEqual(merge({"a": 1}, {"b": 2}), {"a": 1, "b": 2})

    def test_nested(self):
        self.assertEqual(merge({"db": {"host": "x", "port": 1}}, {"db": {"port": 2}}),
                         {"db": {"host": "x", "port": 2}})
