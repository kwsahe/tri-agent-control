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
from triagent.domain import gitops

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

# 기본 작업 폴더도 임시 폴더로 둔다. PROJECT_PATH.txt가 없으면 앱이 기본값(= 이 프로젝트 폴더,
# 즉 실제 Git 저장소)으로 만들어서, 작업 폴더를 따로 정하지 않은 테스트가 실제 저장소에서
# 파이프라인 브랜치를 만든 적이 있다.
WORKSPACE = _ROOT / "workspace"
WORKSPACE.mkdir(parents=True, exist_ok=True)
roundtable.PROJECT_PATH_FILE.write_text(f"# 테스트용 작업 폴더\n\n{WORKSPACE}\n", encoding="utf-8")

# 안전망: 테스트 중에는 실제 저장소(이 프로젝트와 그 하위 폴더)에서 git을 실행하지 않는다.
# gitops는 OSError를 "git 사용 불가"로 처리하므로 브랜치 전환·커밋 없이 넘어간다.
_REAL_REPO = roundtable.ROOT.resolve()
_real_git = gitops._git


def _git_outside_real_repo(root, *args, **kwargs):
    target = Path(root).resolve()
    if target == _REAL_REPO or _REAL_REPO in target.parents:
        raise OSError(f"테스트에서 실제 저장소의 git 실행을 막았습니다: {target}")
    return _real_git(root, *args, **kwargs)


gitops._git = _git_outside_real_repo

# roundtable은 임포트 때 상태를 불러오지 않는다(빈 dict). 경로를 바꾼 뒤에 새 세션을 만든다.
roundtable.STATE = roundtable.new_state()


@atexit.register
def _cleanup() -> None:
    _TEMP.cleanup()
