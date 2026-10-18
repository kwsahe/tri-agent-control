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

__all__ = ["GitResult", "is_repo", "is_clean", "current_branch", "prepare_work_branch", "commit_paths"]


@dataclass(frozen=True)
class GitResult:
    ok: bool
    detail: str = ""


def _git(root: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    git = shutil.which("git") or "git"
    return subprocess.run(
        [git, "-C", str(root), *args],
        input=input_text, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
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
    """현재 브랜치 이름. 알 수 없으면(분리된 HEAD, git 오류) ''."""
    try:
        result = _git(root, "branch", "--show-current")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def prepare_work_branch(root: Path, branch: str) -> GitResult:
    """깨끗한 저장소에서 작업 브랜치로 옮긴다. 이미 그 브랜치면 그대로 둔다."""
    if not is_repo(root):
        return GitResult(False, "Git 저장소가 아니라 자동 커밋을 하지 않습니다.")
    try:
        if current_branch(root) == branch:
            return GitResult(True, branch)
        if not is_clean(root):
            return GitResult(False, "커밋되지 않은 변경이 있어 자동 커밋을 하지 않습니다. 먼저 커밋하거나 정리해주세요.")
        result = _git(root, "switch", "-c", branch)
        if result.returncode != 0:
            result = _git(root, "switch", branch)   # 이미 있는 브랜치 (세션 재개)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitResult(False, f"git 실행 실패로 자동 커밋을 하지 않습니다: {exc}")
    if result.returncode != 0:
        return GitResult(False, f"작업 브랜치를 만들지 못했습니다: {result.stderr.strip()[:300]}")
    return GitResult(True, branch)


def commit_paths(root: Path, message: str, paths: list[str]) -> GitResult:
    """에이전트가 바꾼 파일만 커밋하고 짧은 SHA를 돌려준다. 바뀐 것이 없으면 ok=True, detail=''.

    ``git add -A``로 전부 담지 않는다 — 사용자가 실행 중에 고친 파일, 이미 스테이지해 둔 것,
    작업 폴더 밖(상위 저장소)의 파일이 섞이면 안 된다. 경로는 작업 폴더 기준 상대 경로다.
    경로 목록은 stdin으로 넘겨 Windows 명령줄 길이 한도에 걸리지 않게 한다.
    """
    try:
        listed = _git(root, "ls-files", "-z")
        tracked = set(listed.stdout.split("\0")) if listed.returncode == 0 else set()
        # 생성 후 삭제된 파일처럼 디스크에도 저장소에도 없는 경로는 git이 오류를 내므로 뺀다.
        keep = [path for path in dict.fromkeys(paths) if path and ((root / path).exists() or path in tracked)]
        if not keep:
            return GitResult(True, "")
        added = _git(root, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul",
                     input_text="\0".join(keep))
        if added.returncode != 0:
            return GitResult(False, added.stderr.strip()[:300])
        staged = _git(root, "diff", "--cached", "--name-only", "-z", "--relative")
        to_commit = [path for path in keep if path in set(staged.stdout.split("\0"))]
        if not to_commit:
            return GitResult(True, "")
        committed = _git(root, "commit", "-q", "-m", message, "--pathspec-from-file=-",
                         "--pathspec-file-nul", input_text="\0".join(to_commit))
        if committed.returncode != 0:
            # 사용자 이름/이메일 미설정, pre-commit 훅 실패 등
            return GitResult(False, (committed.stderr or committed.stdout).strip()[:300])
        return GitResult(True, _git(root, "rev-parse", "--short", "HEAD").stdout.strip())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitResult(False, f"git 실행 실패: {exc}")
