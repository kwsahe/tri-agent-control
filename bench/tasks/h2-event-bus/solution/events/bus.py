"""아주 작은 동기식 이벤트 버스."""

from .errors import PublishError


def _matches(pattern: str, event: str) -> bool:
    if pattern.endswith(".*"):
        prefix = pattern[:-1]   # "order.*" -> "order."
        return event.startswith(prefix) and len(event) > len(prefix)
    return pattern == event


class EventBus:
    def __init__(self):
        self._subscriptions = []   # (패턴, 핸들러, 토큰) — 구독 순서대로

    def subscribe(self, event: str, handler):
        token = object()
        self._subscriptions.append((event, handler, token))

        def unsubscribe():
            self._subscriptions = [entry for entry in self._subscriptions if entry[2] is not token]

        return unsubscribe

    def publish(self, event: str, payload=None) -> int:
        called, errors = 0, []
        for pattern, handler, _token in list(self._subscriptions):   # 시작 시점의 목록
            if not _matches(pattern, event):
                continue
            called += 1
            try:
                handler(payload)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise PublishError(errors)
        return called
