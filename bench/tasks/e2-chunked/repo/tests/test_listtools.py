import unittest

from listtools import chunked, flatten


class ListToolsTests(unittest.TestCase):
    def test_chunked_list(self):
        self.assertEqual(chunked([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])

    def test_flatten(self):
        self.assertEqual(flatten([[1, 2], [3]]), [1, 2, 3])
