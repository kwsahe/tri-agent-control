"""작업 폴더의 Git 커밋 — 파이프라인 사이클마다 체크포인트를 남긴다.

원칙:

* 사용자의 기존 변경을 섞지 않는다. 브랜치를 만들 때 작업 트리가 깨끗하지 않으면
  아무것도 바꾸지 않고 이유를 돌려준다.
* main/master에 직접 커밋하지 않는다 — 호출부가 ``triagent/<세션ID>`` 브랜치를 넘긴다.
* 실패는 예외가 아니라 ``GitResult(ok=False, detail=사유)``로 돌려준다. 커밋 실패가
  파이프라인 자체를 멈추면 안 된다.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["GitResult", "is_repo", "is_clean", "current_branch", "prepare_work_branch", "commit_all"]


@dataclass(frozen=True)
class GitResult:
    ok: bool
    detail: str = ""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    git = shutil.which("git") or "git"
    return subprocess.run(
        [git, "-C", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


def is_repo(root: Path) -> bool:
    try:
        result = _git(root, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def is_clean(root: Path) -> bool:
    result = _git(root, "status", "--porcelain")
    return result.returncode == 0 and not result.stdout.strip()


def current_branch(root: Path) -> str:
    result = _git(root, "branch", "--show-current")
    return result.stdout.strip() if result.returncode == 0 else ""


def prepare_work_branch(root: Path, branch: str) -> GitResult:
    """깨끗한 저장소에서 작업 브랜치로 옮긴다. 이미 그 브랜치면 그대로 둔다."""
    if not is_repo(root):
        return GitResult(False, "Git 저장소가 아니라 자동 커밋을 하지 않습니다.")
    if current_branch(root) == branch:
        return GitResult(True, branch)
    if not is_clean(root):
        return GitResult(False, "커밋되지 않은 변경이 있어 자동 커밋을 하지 않습니다. 먼저 커밋하거나 정리해주세요.")
    result = _git(root, "switch", "-c", branch)
    if result.returncode != 0:
        result = _git(root, "switch", branch)   # 이미 있는 브랜치 (세션 재개)
    if result.returncode != 0:
        return GitResult(False, f"작업 브랜치를 만들지 못했습니다: {result.stderr.strip()[:300]}")
    return GitResult(True, branch)


def commit_all(root: Path, message: str) -> GitResult:
    """변경이 있으면 전부 커밋하고 짧은 SHA를 돌려준다. 변경이 없으면 ok=True, detail=''."""
    try:
        added = _git(root, "add", "-A")
        if added.returncode != 0:
            return GitResult(False, added.stderr.strip()[:300])
        if _git(root, "diff", "--cached", "--quiet").returncode == 0:
            return GitResult(True, "")
        committed = _git(root, "commit", "-q", "-m", message)
        if committed.returncode != 0:
            # 사용자 이름/이메일 미설정, pre-commit 훅 실패 등
            return GitResult(False, (committed.stderr or committed.stdout).strip()[:300])
        return GitResult(True, _git(root, "rev-parse", "--short", "HEAD").stdout.strip())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitResult(False, f"git 실행 실패: {exc}")
