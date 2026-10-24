"""리스트 도구."""


def flatten(nested):
    """한 단계만 펼친다: [[1, 2], [3]] -> [1, 2, 3]."""
    return [item for group in nested for item in group]
