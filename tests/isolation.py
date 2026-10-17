"""테스트 전역 격리 — 실제 사용자 데이터 폴더를 절대 건드리지 않는다.

`unittest discover`가 이 패키지를 임포트할 때 자동으로 적용된다.

왜 필요한가:

`save_state`만 막아서는 부족하다. `append_session_transcript`,
`write_session_transcript_header`, `append_memory`, `append_log`, `update_profile`
같은 함수들은 `SESSIONS_DIR` / `MEMORY_DIR` / `LOG_PATH`에 직접 쓴다. 격리가 없으면
테스트 한 번에 실제 `sessions/` 폴더에 세션 파일 수백 개가 쌓인다.

여기서는 모듈 전역 경로를 임시 폴더로 바꿔 두고, 프로세스가 끝날 때 지운다.
"""

from __future__ import annotations

import atexit
import tempfile
from pathlib import Path

import roundtable

_TEMP = tempfile.TemporaryDirectory(prefix="triagent-tests-")
_ROOT = Path(_TEMP.name).resolve()

SESSIONS_DIR = _ROOT / "sessions"
MEMORY_DIR = _ROOT / "roundtable_memory"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_DIR.mkdir(parents=True, exist_ok=True)

#: 원래 값 (필요하면 개별 테스트가 되돌릴 수 있도록 남겨둔다)
ORIGINAL = {
    "SESSIONS_DIR": roundtable.SESSIONS_DIR,
    "MEMORY_DIR": roundtable.MEMORY_DIR,
    "STATE_PATH": roundtable.STATE_PATH,
    "LOG_PATH": roundtable.LOG_PATH,
    "HTML_PATH": roundtable.HTML_PATH,
    "PROJECT_PATH_FILE": roundtable.PROJECT_PATH_FILE,
    "PROFILE_PATHS": dict(roundtable.PROFILE_PATHS),
}

roundtable.SESSIONS_DIR = SESSIONS_DIR
roundtable.MEMORY_DIR = MEMORY_DIR
roundtable.STATE_PATH = _ROOT / "roundtable_state.json"
roundtable.LOG_PATH = _ROOT / "roundtable_log.md"
roundtable.HTML_PATH = _ROOT / "roundtable.html"
roundtable.PROJECT_PATH_FILE = _ROOT / "PROJECT_PATH.txt"
for _agent in roundtable.PROFILE_PATHS:
    roundtable.PROFILE_PATHS[_agent] = _ROOT / f"{_agent.upper()}_Profile.md"

# roundtable은 임포트 때 상태를 불러오지 않는다(빈 dict). 경로를 바꾼 뒤에 새 세션을 만든다.
roundtable.STATE = roundtable.new_state()


@atexit.register
def _cleanup() -> None:
    _TEMP.cleanup()
