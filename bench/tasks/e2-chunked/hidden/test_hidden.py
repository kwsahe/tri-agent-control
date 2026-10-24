import unittest

from listtools import chunked, flatten


class HiddenChunkedTests(unittest.TestCase):
    def test_exact_and_uneven_splits(self):
        self.assertEqual(chunked([1, 2, 3, 4], 2), [[1, 2], [3, 4]])
        self.assertEqual(chunked([1, 2, 3], 5), [[1, 2, 3]])

    def test_generator_and_string_inputs(self):
        self.assertEqual(chunked((n for n in range(5)), 2), [[0, 1], [2, 3], [4]])
        self.assertEqual(chunked("abcde", 2), [["a", "b"], ["c", "d"], ["e"]])

    def test_empty_input(self):
        self.assertEqual(chunked([], 3), [])
        self.assertEqual(chunked(iter(()), 1), [])

    def test_invalid_sizes_raise(self):
        for size in [0, -1, 2.5, "2", True, None]:
            with self.subTest(size=size):
                with self.assertRaises(ValueError):
                    chunked([1, 2, 3], size)

    def test_chunks_are_independent_lists(self):
        chunks = chunked([1, 2, 3, 4], 2)
        chunks[0].append(99)
        self.assertEqual(chunks[1], [3, 4])

    def test_flatten_reverses_chunked(self):
        data = list(range(11))
        self.assertEqual(flatten(chunked(data, 3)), data)
