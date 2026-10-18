"""세션 ID 검증과 안전한 경로 결합.

roundtable.py는 세션 ID를 그대로 파일 경로에 붙여 쓰기 때문에
``..\\victim`` 같은 값이 들어오면 허용 폴더 밖을 읽고/쓰고/삭제할 수 있었다.
이 모듈은 그 진입점 전체가 공유하는 단일 검증 지점이다.

두 겹으로 막는다.

1. 문자열 검증 (:func:`validate_session_id`) — 구분자/드라이브 문자/traversal/
   URL 인코딩 우회를 모두 거부한다.
2. 경로 검증 (:func:`safe_child`) — resolve된 최종 경로가 실제로 허용된 부모
   디렉터리 안인지 확인한다. 문자열 검사를 통과해도 심볼릭 링크나 OS별 경로
   정규화 때문에 밖으로 나갈 수 있으므로 이 단계가 반드시 필요하다.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

__all__ = [
    "UnsafePathError",
    "UnsafeSessionIdError",
    "CANONICAL_SESSION_ID_RE",
    "SESSION_ID_RE",
    "is_canonical_session_id",
    "is_valid_session_id",
    "validate_session_id",
    "safe_child",
    "safe_session_child",
]


class UnsafePathError(ValueError):
    """허용 폴더 밖을 가리키는 경로."""


class UnsafeSessionIdError(UnsafePathError):
    """파일 경로로 쓸 수 없는 세션 ID."""


# 서버가 새로 만드는 형식: 20260712_173325_471441 (new_session_id()와 동일)
CANONICAL_SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}_\d{6}$")

# 디스크에 남아 있는 기존 세션 호환용. 실제 sessions/ 폴더를 조사한 결과 전부
# canonical 형식이었지만, 다른 설치본에서 만들어진 이름도 읽을 수 있도록
# "경로 구분자·상대경로·드라이브 문자가 전혀 없는 단순 이름"까지는 허용한다.
# 점(.)을 아예 금지하므로 ".", "..", "..." 류는 구조적으로 통과할 수 없다.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# 명시적으로 거부하는 문자들 (정규식으로 이미 걸러지지만, 거부 사유를 사용자에게
# 정확히 알려주기 위해 따로 확인한다).
_SEPARATORS = ("/", "\\")
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _decode_repeatedly(value: str, rounds: int = 3) -> str:
    """URL 인코딩(%2e%2e%2f 등)을 이용한 우회를 막기 위해 반복 디코딩한다."""
    current = value
    for _ in range(rounds):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return current


def _reject(reason: str) -> None:
    raise UnsafeSessionIdError(reason)


def validate_session_id(value: object, *, canonical_only: bool = False) -> str:
    """세션 ID를 검증해 반환한다. 잘못된 값이면 :class:`UnsafeSessionIdError`.

    ``canonical_only=True``면 서버가 생성하는 형식만 허용한다 (신규 생성 경로용).
    """
    if not isinstance(value, str):
        _reject("세션 ID가 문자열이 아닙니다.")

    raw = value.strip()
    if not raw:
        _reject("세션 ID가 비어 있습니다.")
    if len(raw) > 64:
        _reject("세션 ID가 너무 깁니다.")

    # URL 인코딩 우회 차단: 디코딩 전후가 다르면 인코딩된 구분자가 숨어 있다는 뜻.
    decoded = _decode_repeatedly(raw)
    if decoded != raw:
        _reject("세션 ID에 URL 인코딩된 문자가 있습니다.")

    # 널 바이트 / 제어문자
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        _reject("세션 ID에 제어 문자가 있습니다.")

    for separator in _SEPARATORS:
        if separator in raw:
            _reject("세션 ID에 경로 구분자를 쓸 수 없습니다.")
    if ":" in raw:
        _reject("세션 ID에 드라이브 문자나 콜론을 쓸 수 없습니다.")
    if ".." in raw:
        _reject("세션 ID에 상위 폴더 참조(..)를 쓸 수 없습니다.")
    if raw.startswith("~"):
        _reject("세션 ID에 홈 디렉터리 참조를 쓸 수 없습니다.")

    if raw.lower() in _RESERVED_NAMES:
        _reject("Windows 예약 장치 이름은 세션 ID로 쓸 수 없습니다.")

    if canonical_only:
        if not CANONICAL_SESSION_ID_RE.match(raw):
            _reject("서버가 생성한 세션 ID 형식이 아닙니다.")
        return raw

    if not SESSION_ID_RE.match(raw):
        _reject("세션 ID에 허용되지 않는 문자가 있습니다.")
    return raw


def is_valid_session_id(value: object) -> bool:
    """예외 없이 검증 결과만 알고 싶을 때 (목록 필터링 등)."""
    try:
        validate_session_id(value)
    except UnsafeSessionIdError:
        return False
    return True


def is_canonical_session_id(value: object) -> bool:
    try:
        validate_session_id(value, canonical_only=True)
    except UnsafeSessionIdError:
        return False
    return True


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return child == parent or child.is_relative_to(parent)
    except AttributeError:  # pragma: no cover - Python 3.9 이하 방어
        return str(child) == str(parent) or str(child).startswith(str(parent) + "\\") \
            or str(child).startswith(str(parent) + "/")


def safe_child(parent: Path, *parts: str) -> Path:
    """``parent`` 안에 있다고 보장되는 경로를 만든다.

    각 조각은 단일 이름이어야 하며, resolve된 결과가 ``parent`` 밖이면
    :class:`UnsafePathError`를 던진다. (심볼릭 링크 우회 차단)
    """
    if not parts:
        raise UnsafePathError("경로 조각이 없습니다.")

    for part in parts:
        if not isinstance(part, str) or not part:
            raise UnsafePathError("빈 경로 조각은 사용할 수 없습니다.")
        if any(separator in part for separator in _SEPARATORS):
            raise UnsafePathError(f"경로 조각에 구분자를 쓸 수 없습니다: {part!r}")
        if part in (".", "..") or ":" in part:
            raise UnsafePathError(f"허용되지 않는 경로 조각입니다: {part!r}")

    parent_resolved = Path(parent).resolve()
    candidate = parent_resolved.joinpath(*parts)

    # strict=False: 아직 존재하지 않는 파일도 만들 수 있어야 한다.
    resolved = candidate.resolve()
    if not _is_within(resolved, parent_resolved):
        raise UnsafePathError(f"허용된 폴더 밖의 경로입니다: {candidate}")
    return resolved


def safe_session_child(parent: Path, session_id: object, *parts: str) -> Path:
    """세션 ID를 검증한 뒤 ``parent`` 아래 경로를 만든다."""
    clean_id = validate_session_id(session_id)
    return safe_child(parent, clean_id, *parts)
