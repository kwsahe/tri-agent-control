class PublishError(Exception):
    """핸들러 하나 이상이 예외를 냈다. errors에 순서대로 담는다."""

    def __init__(self, errors):
        super().__init__(f"{len(errors)}개 핸들러가 실패했습니다")
        self.errors = list(errors)
