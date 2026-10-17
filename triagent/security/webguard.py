"""로컬 대시보드용 HTTP 요청 검증.

TriAgent Control은 127.0.0.1에만 바인딩되지만, 그것만으로는 브라우저가 열어둔
다른 웹페이지가 ``fetch("http://127.0.0.1:8765/stop")`` 같은 요청을 보내는 것을
막지 못한다. (CSRF — 브라우저는 localhost로도 cross-origin 요청을 보낸다.)

여기서 막는 것:

* **CSRF 토큰** — 서버 시작 시 생성한 예측 불가능한 토큰이 있어야 상태를 바꾼다.
* **Origin** — 있으면 http(s)://127.0.0.1:<현재포트> 또는 localhost 만 허용.
* **Host** — 로컬 호스트 + 실제 서버 포트여야 한다 (DNS rebinding 차단).
* **Content-Length** — 음수/비정상/과대 요청을 읽기 전에 거부.

CORS 헤더는 일부러 내보내지 않는다. 외부 페이지가 응답을 읽지 못하게 하는 것도
방어의 일부다.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

__all__ = [
    "CSRF_HEADER",
    "CSRF_FORM_FIELD",
    "MAX_BODY_BYTES",
    "LOCAL_HOSTNAMES",
    "RequestRejected",
    "generate_csrf_token",
    "tokens_match",
    "parse_content_length",
    "is_allowed_origin",
    "is_allowed_host",
    "check_state_changing_request",
]

CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"

# 폼/JSON 본문 상한. 대시보드가 보내는 가장 큰 요청은 Profile.md 저장인데,
# 실사용 Profile은 수십 KB 수준이라 1MB면 충분히 여유가 있다.
MAX_BODY_BYTES = 1_048_576

LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


class RequestRejected(Exception):
    """요청을 거부한다. ``status``는 그대로 HTTP 응답 코드로 쓴다."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_match(provided: object, expected: object) -> bool:
    """타이밍 공격에 안전한 토큰 비교."""
    if not isinstance(provided, str) or not isinstance(expected, str):
        return False
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided, expected)


def parse_content_length(raw: object, *, limit: int = MAX_BODY_BYTES) -> int:
    """Content-Length 헤더를 검증해 정수로 반환한다.

    없으면 0. 숫자가 아니거나 음수면 400, 상한 초과면 413.
    """
    if raw is None or raw == "":
        return 0
    text = str(raw).strip()
    if not text.isdigit():  # 음수 부호와 공백, 지수 표기까지 한 번에 거른다
        raise RequestRejected(400, "Content-Length 값이 올바르지 않습니다.")
    try:
        length = int(text)
    except ValueError:
        raise RequestRejected(400, "Content-Length 값이 올바르지 않습니다.") from None
    if length > limit:
        raise RequestRejected(413, f"요청 본문이 너무 큽니다 (최대 {limit:,} 바이트).")
    return length


def _split_host_port(value: str) -> tuple[str, int | None]:
    """``127.0.0.1:8765`` / ``[::1]:8765`` / ``localhost`` 를 분해한다."""
    text = value.strip()
    if not text:
        return "", None
    if text.startswith("["):
        closing = text.find("]")
        if closing == -1:
            return text, None
        host = text[: closing + 1]
        rest = text[closing + 1 :]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, None
    if ":" in text:
        host, _, port_text = text.rpartition(":")
        if port_text.isdigit():
            return host, int(port_text)
        return text, None
    return text, None


def is_allowed_origin(origin: object, port: int) -> bool:
    """Origin 헤더가 이 서버 자신인지 확인한다.

    헤더가 없으면(``None``) 판단하지 않는다 — 호출부에서 결정한다.
    ``"null"``(sandboxed iframe, file://)은 항상 거부한다.
    """
    if not isinstance(origin, str) or not origin.strip():
        return False
    text = origin.strip()
    if text == "null":
        return False
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        return False
    if not parts.netloc:
        return False
    host, origin_port = _split_host_port(parts.netloc)
    if host.lower() not in LOCAL_HOSTNAMES:
        return False
    if origin_port is None:
        return False  # 로컬 대시보드는 항상 명시적 포트로 열린다
    return origin_port == port


def is_allowed_host(host_header: object, port: int) -> bool:
    """Host 헤더가 로컬 호스트 + 실제 서버 포트인지 확인한다."""
    if not isinstance(host_header, str) or not host_header.strip():
        return False
    host, host_port = _split_host_port(host_header)
    if host.lower() not in LOCAL_HOSTNAMES:
        return False
    if host_port is None:
        return False  # 127.0.0.1로 바인딩된 서버는 기본 포트로 접근되지 않는다
    return host_port == port


def check_state_changing_request(
    *,
    origin: object,
    host: object,
    token: object,
    expected_token: str,
    port: int,
) -> None:
    """상태 변경 요청(POST)을 검증한다. 문제가 있으면 :class:`RequestRejected`.

    검사 순서는 저렴한 것부터: Host → Origin → CSRF 토큰.
    """
    if not is_allowed_host(host, port):
        raise RequestRejected(403, "허용되지 않은 Host 헤더입니다.")

    # Origin은 fetch()에서는 항상 붙지만 일부 폼 전송에서는 생략될 수 있다.
    # 있으면 반드시 우리 자신이어야 하고, 없으면 CSRF 토큰으로만 판단한다.
    if origin is not None and str(origin).strip():
        if not is_allowed_origin(origin, port):
            raise RequestRejected(403, "허용되지 않은 Origin에서 온 요청입니다.")

    if not tokens_match(token, expected_token):
        raise RequestRejected(403, "CSRF 토큰이 없거나 일치하지 않습니다.")
