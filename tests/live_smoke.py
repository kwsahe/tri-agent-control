"""실제 세 CLI의 읽기/쓰기 동작을 격리된 임시 작업공간에서 확인한다.

이 스크립트는 진짜 모델을 호출한다 (요금과 시간이 든다). 자동 테스트가 아니라
수동 검증용이다.

안전 규칙:

* 작업공간은 ``TemporaryDirectory``다. 원본 프로젝트 폴더가 아니다.
  (예전 버전은 프로젝트 안의 ``.roundtable-smoke``에서 돌아 원본 트리를 건드릴
   위험이 있었다.)
* 실행 전후로 원본 프로젝트 전체의 SHA-256을 비교해, 단 한 파일이라도 바뀌면
  실패로 보고한다.
* 읽기 검사와 쓰기 검사를 따로 판정한다.
* 출력에 인증 정보가 섞이지 않는지 확인한다.
* CLI가 없거나 로그인되지 않았으면 성공으로 위장하지 않고 "미실행/실패"로 남긴다.

실행:

    $env:PYTHONPATH='.'
    python tests\\live_smoke.py            # 세 CLI 모두
    python tests\\live_smoke.py codex      # 하나만
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import roundtable
from triagent.domain import clicheck

#: 스모크 테스트용 짧은 타임아웃. 하나가 멈춰도 나머지 CLI를 확인할 수 있어야 한다.
SMOKE_TIMEOUT = int(os.environ.get("TRIAGENT_SMOKE_TIMEOUT", "240"))

READ_MARKER = "TRIAGENT_LIVE_READ_5E7B"

READ_PROMPT = (
    "Read the file NOTES.md in the current working directory and reply with the "
    "single token it contains, and nothing else."
)

#: 원본 트리 해시에서 제외할 항목 (실행 중 정상적으로 바뀌는 사용자 데이터/캐시).
HASH_EXCLUDE_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache",
    "sessions", "roundtable_memory", ".roundtable-smoke",
}
HASH_EXCLUDE_FILES = {
    "roundtable_state.json", "roundtable.html", "roundtable_log.md",
    "CODEX_Profile.md", "ANTIGRAVITY_Profile.md", "CLAUDE_Profile.md",
}


def hash_tree(root: Path) -> dict[str, str]:
    """원본 프로젝트의 파일별 SHA-256. 사용자 데이터/캐시는 제외한다."""
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if set(relative.parts) & HASH_EXCLUDE_DIRS:
            continue
        if relative.name in HASH_EXCLUDE_FILES:
            continue
        try:
            digests[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digests[str(relative)] = "<unreadable>"
    return digests


def diff_hashes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changes = []
    for name in sorted(set(before) | set(after)):
        if name not in after:
            changes.append(f"삭제됨: {name}")
        elif name not in before:
            changes.append(f"추가됨: {name}")
        elif before[name] != after[name]:
            changes.append(f"수정됨: {name}")
    return changes


def looks_like_a_secret(text: str) -> bool:
    """출력에 인증 정보가 그대로 들어 있는지 (redact 전후 비교)."""
    return clicheck.redact(text, limit=len(text) + 1) != text


@contextmanager
def isolated_workspace():
    """임시 작업공간을 만들고, 그동안 작업 폴더 해석을 그쪽으로 돌린다.

    Windows에서는 CLI가 종료 직후에도 폴더 핸들을 잠깐 붙잡고 있어 정리가 실패할 수
    있다. 그 때문에 리포트 전체가 날아가면 안 되므로 정리 오류는 무시한다.
    """
    temp = tempfile.TemporaryDirectory(
        prefix="triagent-live-", ignore_cleanup_errors=True
    )
    try:
        workspace = Path(temp.name).resolve()
        (workspace / "NOTES.md").write_text(READ_MARKER + "\n", encoding="utf-8")
        with patch.object(
            roundtable, "read_configured_project_path", return_value=str(workspace)
        ):
            # 임포트만으로는 상태가 비어 있다. 사용자 세션 대신 임시 작업공간의 새 세션을 쓴다.
            roundtable.STATE = roundtable.new_state()
            yield workspace
    finally:
        try:
            temp.cleanup()
        except OSError as exc:
            print(f"[안내] 임시 작업공간 정리 실패(무시): {exc}", file=sys.stderr)


@contextmanager
def short_cli_timeouts():
    """CLI별 기본 타임아웃(최대 900초)을 스모크용으로 줄인다."""
    originals = {
        name: getattr(roundtable, name)
        for name in ("CODEX_TIMEOUT", "CLAUDE_TIMEOUT", "AGY_TIMEOUT")
    }
    for name in originals:
        setattr(roundtable, name, SMOKE_TIMEOUT)
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(roundtable, name, value)


def probe_agent(agent: str, workspace: Path) -> dict:
    label = roundtable.AGENTS[agent]["label"]
    result: dict = {
        "agent": label,
        "read": "미실행",
        "write": "미실행",
        "read_detail": "",
        "write_detail": "",
        "secrets_in_output": False,
    }

    # ── 읽기 ────────────────────────────────────────────────
    roundtable.STATE["mode"] = "discussion"
    roundtable.STATE["discussion_project_access"] = "read"
    roundtable.STATE["workspace_access"] = "read"
    try:
        read_response = roundtable.ASK_FUNCS[agent](READ_PROMPT, "discussion")
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 그대로 보고한다
        read_response = f"(예외: {type(exc).__name__}: {exc})"

    if roundtable.is_cli_failure(read_response):
        result["read"] = "실패"
    elif READ_MARKER in read_response:
        result["read"] = "성공"
    else:
        result["read"] = "실패"
    result["read_detail"] = clicheck.redact(read_response.strip(), 400)
    if looks_like_a_secret(read_response):
        result["secrets_in_output"] = True

    # ── 쓰기 ────────────────────────────────────────────────
    marker_name = f"smoke_{agent}.txt"
    marker_path = workspace / marker_name
    marker_path.unlink(missing_ok=True)
    expected = f"{label} READY"
    write_prompt = (
        f"Create a file named {marker_name} in the current working directory whose "
        f"entire content is exactly the single line: {expected}\n"
        "Do not modify any other file. Then reply DONE."
    )
    roundtable.STATE["mode"] = "coding"
    roundtable.STATE["discussion_project_access"] = "write"
    roundtable.STATE["workspace_access"] = "write"
    try:
        write_response = roundtable.ASK_FUNCS[agent](write_prompt, "coding")
    except Exception as exc:  # noqa: BLE001
        write_response = f"(예외: {type(exc).__name__}: {exc})"

    if marker_path.is_file():
        written = marker_path.read_text(encoding="utf-8", errors="replace").strip()
        if written == expected:
            result["write"] = "성공"
            result["write_detail"] = "파일 생성 및 내용 일치"
        else:
            result["write"] = "실패"
            result["write_detail"] = f"내용 불일치: {written[:120]!r}"
    else:
        result["write"] = "실패"
        result["write_detail"] = (
            "파일이 생성되지 않았습니다 · 응답: "
            + clicheck.redact(write_response.strip(), 300)
        )
    marker_path.unlink(missing_ok=True)
    if looks_like_a_secret(write_response):
        result["secrets_in_output"] = True

    return result


def run() -> int:
    requested = [a for a in sys.argv[1:] if a in roundtable.AGENT_ORDER]
    agents = requested or list(roundtable.AGENT_ORDER)

    project_root = roundtable.ROOT
    before = hash_tree(project_root)

    results = []
    with isolated_workspace() as workspace, short_cli_timeouts():
        for agent in agents:
            print(f"[진행] {roundtable.AGENTS[agent]['label']} 확인 중...",
                  file=sys.stderr, flush=True)
            results.append(probe_agent(agent, workspace))

    after = hash_tree(project_root)
    changes = diff_hashes(before, after)

    report = {
        "workspace": "임시 폴더 (TemporaryDirectory) — 원본 프로젝트 아님",
        "original_project_unchanged": not changes,
        "original_project_changes": changes,
        "files_hashed": len(before),
        "agents": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if changes:
        print("\n[실패] 스모크 테스트가 원본 프로젝트 파일을 변경했습니다.", file=sys.stderr)
        return 2
    if any(item["secrets_in_output"] for item in results):
        print("\n[경고] CLI 출력에 인증 정보로 보이는 값이 있었습니다.", file=sys.stderr)
        return 3
    all_ok = all(item["read"] == "성공" and item["write"] == "성공" for item in results)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
