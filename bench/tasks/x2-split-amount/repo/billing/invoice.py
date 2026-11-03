"""청구서."""

from .allocate import allocate


class Invoice:
    def __init__(self, total: int):
        self.total = total

    def split_by_people(self, people: int) -> list[int]:
        """여러 사람이 똑같이 나눠 낸다."""
        return allocate(self.total, [1] * people)
