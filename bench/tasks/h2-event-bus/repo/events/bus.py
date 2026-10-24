"""아주 작은 동기식 이벤트 버스."""


class EventBus:
    def __init__(self):
        self._handlers = {}

    def subscribe(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def publish(self, event: str, payload=None) -> None:
        for handler in self._handlers.get(event, []):
            handler(payload)
