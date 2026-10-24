"""리스트 도구."""

from itertools import islice


def flatten(nested):
    """한 단계만 펼친다: [[1, 2], [3]] -> [1, 2, 3]."""
    return [item for group in nested for item in group]


def chunked(iterable, size):
    """size개씩 나눈 리스트들의 리스트. 마지막 묶음은 짧을 수 있다."""
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ValueError("size must be an int >= 1")
    iterator = iter(iterable)
    chunks = []
    while chunk := list(islice(iterator, size)):
        chunks.append(chunk)
    return chunks
