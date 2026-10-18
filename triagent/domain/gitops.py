"""세션 전용 Git worktree와 사이클 커밋.

원칙:

* 사용자 폴더의 브랜치와 파일은 건드리지 않는다. 파이프라인은 사용자 저장소의 HEAD에서
  ``triagent/<세션ID>`` 브랜치를 만들어 별도 폴더(worktree)에 체크아웃하고 거기서 일한다.
* Git이 추적하지 않는 가상환경·node_modules는 worktree에 없으므로 사용자 폴더의 것을
  연결(Windows junction, 그 밖은 symlink)해서 함께 쓴다. 정리할 때는 연결만 끊는다.
* 실패는 예외가 아니라 ``GitResult(ok=False, detail=사유)``로 돌려준다. git 문제가
  파이프라인 자체를 멈추면 안 된다.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "GitResult", "SHARED_DIRS", "is_repo", "is_clean", "current_branch", "repo_root",
    "create_worktree", "link_shared_dirs", "remove_worktree", "commit_paths",
]

#: worktree에 연결해 함께 쓰는 사용자 폴더의 무거운 의존성 폴더 (Git이 추적하지 않는다).
SHARED_DIRS = (".venv", "venv", "node_modules")


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


def repo_root(path: Path) -> Path | None:
    """``path``가 속한 저장소의 최상위 폴더. 저장소가 아니거나 git을 못 쓰면 None."""
    try:
        result = _git(path, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.TimeoutExpired):
        return None
    top = result.stdout.strip()
    return Path(top) if result.returncode == 0 and top else None


def create_worktree(repo: Path, worktree_dir: Path, branch: str, start: str = "HEAD") -> GitResult:
    """``repo``의 ``start``(기본 HEAD)에서 ``branch``를 만들어 ``worktree_dir``에 체크아웃한다. 사용자 폴더는 그대로다."""
    try:
        if worktree_dir.exists():
            if current_branch(worktree_dir) == branch:
                return GitResult(True, branch)   # 세션 재개
            return GitResult(False, f"worktree 폴더가 이미 있지만 {branch} 브랜치가 아닙니다: {worktree_dir}")
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        result = _git(repo, "worktree", "add", "-b", branch, str(worktree_dir), start)
        if result.returncode != 0:
            result = _git(repo, "worktree", "add", str(worktree_dir), branch)   # 브랜치만 남아 있던 경우
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitResult(False, f"git 실행 실패로 worktree를 만들지 못했습니다: {exc}")
    if result.returncode != 0:
        return GitResult(False, f"worktree를 만들지 못했습니다: {result.stderr.strip()[:300]}")
    return GitResult(True, branch)


def _is_link(path: Path) -> bool:
    """symlink 또는 Windows junction인가 (os.path.isjunction은 3.12부터라 직접 본다)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def link_shared_dirs(source: Path, target: Path) -> list[str]:
    """사용자 폴더의 가상환경·node_modules를 worktree에 연결한다(복사하지 않는다). 연결한 이름 목록."""
    linked = []
    for name in SHARED_DIRS:
        src, dst = Path(source) / name, Path(target) / name
        if not src.is_dir() or dst.exists() or _is_link(dst):
            continue
        try:
            if os.name == "nt":
                import _winapi   # CPython이 제공하는 junction 생성 (관리자 권한 불필요)
                _winapi.CreateJunction(str(src.resolve()), str(dst))
            else:
                dst.symlink_to(src.resolve(), target_is_directory=True)
        except (OSError, ImportError, AttributeError):
            continue
        linked.append(name)
    return linked


def remove_worktree(repo: Path, worktree_dir: Path, workspace: Path | None = None) -> GitResult:
    """연결부터 끊고 worktree를 지운다. 커밋 안 된 변경이 있으면 지우지 않는다 (브랜치는 남긴다)."""
    base = Path(workspace or worktree_dir)
    for name in SHARED_DIRS:
        link = base / name
        if _is_link(link):
            try:
                # 연결만 끊는다 — 재귀 삭제하면 연결된 원본(사용자 .venv 등)까지 지워진다.
                os.rmdir(link) if os.name == "nt" else link.unlink()
            except OSError as exc:
                return GitResult(False, f"연결 폴더를 끊지 못해 worktree를 그대로 둡니다: {link} ({exc})")
    try:
        if not Path(worktree_dir).exists():
            _git(repo, "worktree", "prune")
            return GitResult(True, "")
        result = _git(repo, "worktree", "remove", str(worktree_dir))
        if result.returncode != 0:
            return GitResult(False, f"worktree를 지우지 않았습니다 (커밋 안 된 변경이 있을 수 있음): {result.stderr.strip()[:300]}")
        _git(repo, "worktree", "prune")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitResult(False, f"git 실행 실패로 worktree를 지우지 못했습니다: {exc}")
    return GitResult(True, "")


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
