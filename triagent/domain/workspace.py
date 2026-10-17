"""작업공간(코딩 대상 폴더) 해석 — fail closed.

이전 동작: ``PROJECT_PATH.txt``가 존재하지 않는 폴더를 가리키면 조용히 TriAgent
자신의 폴더(ROOT)로 대체했다. 그래서 세션 목록에는 과거 프로젝트가 표시되는데
실제 모델 실행은 TriAgent 코드 폴더에서 일어날 수 있었다 — 엉뚱한 프로젝트를
읽거나 고칠 수 있는 상태.

지금은 해석에 실패하면 경로를 만들어내지 않고 ``ok=False``인 결과를 돌려주고,
호출부가 읽기·쓰기 실행을 모두 차단한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "WorkspaceStatus",
    "WorkspaceResolution",
    "resolve_workspace",
    "parse_project_path_file",
    "workspace_matches",
]


class WorkspaceStatus:
    OK = "OK"
    MISSING = "MISSING"            # 설정 파일에 경로가 없음
    NOT_FOUND = "NOT_FOUND"        # 경로가 존재하지 않음
    NOT_A_DIRECTORY = "NOT_A_DIRECTORY"  # 파일을 폴더로 지정
    RELATIVE = "RELATIVE"          # 상대 경로 — 기준이 모호해 거부
    UNREADABLE = "UNREADABLE"      # 읽기 권한 없음
    UNWRITABLE = "UNWRITABLE"      # 쓰기 권한 없음 (write 요청 시)


_STATUS_MESSAGES = {
    WorkspaceStatus.MISSING: "작업 폴더가 설정되어 있지 않습니다.",
    WorkspaceStatus.NOT_FOUND: "저장된 작업 폴더를 찾을 수 없습니다.",
    WorkspaceStatus.NOT_A_DIRECTORY: "작업 폴더로 지정된 경로가 폴더가 아닙니다.",
    WorkspaceStatus.RELATIVE: "작업 폴더는 절대 경로여야 합니다.",
    WorkspaceStatus.UNREADABLE: "작업 폴더를 읽을 권한이 없습니다.",
    WorkspaceStatus.UNWRITABLE: "작업 폴더에 쓸 권한이 없습니다.",
}


@dataclass(frozen=True)
class WorkspaceResolution:
    """작업공간 해석 결과.

    ``ok``가 False면 ``path``는 ``None``이다 — 대체 경로를 만들어내지 않는다.
    ``configured``는 사용자가 원래 적어둔 원문이라 UI에서 그대로 보여줄 수 있다.
    """

    ok: bool
    status: str
    path: Path | None
    configured: str
    message: str = ""

    @property
    def blocked(self) -> bool:
        return not self.ok

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "path": str(self.path) if self.path else "",
            "configured": self.configured,
            "message": self.message,
        }


def parse_project_path_file(text: str) -> str:
    """PROJECT_PATH.txt 본문에서 첫 번째 유효 줄을 뽑는다 (주석/빈 줄 무시)."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped
    return ""


def resolve_workspace(configured: str, *, require_write: bool = False) -> WorkspaceResolution:
    """설정된 경로 문자열을 검증한다. 실패하면 대체하지 않고 차단 상태로 돌려준다."""
    raw = (configured or "").strip().strip('"')
    if not raw:
        return WorkspaceResolution(
            ok=False,
            status=WorkspaceStatus.MISSING,
            path=None,
            configured="",
            message=_STATUS_MESSAGES[WorkspaceStatus.MISSING],
        )

    candidate = Path(raw).expanduser()

    def fail(status: str) -> WorkspaceResolution:
        return WorkspaceResolution(
            ok=False,
            status=status,
            path=None,
            configured=raw,
            message=f"{_STATUS_MESSAGES[status]} ({raw})",
        )

    # 상대 경로는 "무엇을 기준으로?"가 프로세스 CWD에 따라 달라져 위험하다.
    if not candidate.is_absolute():
        return fail(WorkspaceStatus.RELATIVE)

    if not candidate.exists():
        return fail(WorkspaceStatus.NOT_FOUND)
    if not candidate.is_dir():
        return fail(WorkspaceStatus.NOT_A_DIRECTORY)

    try:
        resolved = candidate.resolve()
    except OSError:
        return fail(WorkspaceStatus.NOT_FOUND)

    if not os.access(resolved, os.R_OK):
        return fail(WorkspaceStatus.UNREADABLE)
    if require_write and not os.access(resolved, os.W_OK):
        return fail(WorkspaceStatus.UNWRITABLE)

    return WorkspaceResolution(
        ok=True,
        status=WorkspaceStatus.OK,
        path=resolved,
        configured=raw,
        message="",
    )


def workspace_matches(saved: object, current: object) -> bool:
    """세션에 저장된 작업공간과 현재 선택된 폴더가 같은 곳인지 확인한다.

    저장된 값이 없으면(구버전 세션) 판단할 근거가 없으므로 True로 본다 —
    호출부에서 "기록 없음"으로 따로 안내한다.
    """
    saved_text = str(saved or "").strip()
    current_text = str(current or "").strip()
    if not saved_text:
        return True
    if not current_text:
        return False
    try:
        return Path(saved_text).resolve() == Path(current_text).resolve()
    except OSError:
        return os.path.normcase(saved_text) == os.path.normcase(current_text)
