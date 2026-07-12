#!/usr/bin/env python3
"""
Codex <-> Claude Code 자동 개발 루프
Ctrl+C로 중단

사용법:
    python orchestrate.py [레포경로]

환경변수:
    CLAUDE_CMD=claude
    CODEX_CMD=codex
    CLAUDE_TIMEOUT_SECONDS=600
    CODEX_TIMEOUT_SECONDS=900
    CLAUDE_PERMISSION_MODE=acceptEdits

기본값: 현재 디렉토리
"""

import subprocess
import sys
import time
import hashlib
import os
import shlex
import shutil
from datetime import datetime
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ──────────────────────────────────────────
# 설정
# ──────────────────────────────────────────

REPO_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()

ROUND_DELAY = 3  # 라운드 사이 대기 시간 (초)
CLAUDE_CMD = os.environ.get("CLAUDE_CMD", "claude")
CODEX_CMD = os.environ.get("CODEX_CMD", "codex")
CLAUDE_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "600"))
CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "900"))
CLAUDE_PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")

# Claude Code에게 보낼 프롬프트 — 1단계: UX 검토 + 백엔드 요청 작성
CLAUDE_REVIEW_PROMPT = """
CLAUDE.md, AGENTS.md, TODO.md를 순서대로 읽어라.

너의 역할은 UX 에이전트 (Claude Code) 다.
이번 단계의 목적은 사용자 관점에서 앱을 점검하고, 필요한 백엔드 작업을 Codex에게 정확히 요청하는 것이다.

작업 순서:
1. CLAUDE.md를 읽고 이 프로젝트의 아키텍처, 개발 규칙, 금지 사항을 파악한다
2. 앱을 사용자 관점에서 검토해 UX 문제나 개선 아이디어 발굴
3. 백엔드 수정이 필요한 항목을 [CODEX 요청] 섹션에 오늘 날짜와 함께 추가
   — 요청 시 CLAUDE.md의 개발 규칙을 준수하는 방향으로 작성할 것
4. 프론트만으로 해결 가능한 작은 UX 수정은 templates/, static/ 에 반영
5. 이번 검토 내용을 [검증 결과] 섹션에 요약

주의:
- app.py, analysis.py, db.py 등 백엔드 파일은 절대 수정하지 않는다
- CLAUDE.md에 명시된 금지 사항을 어기는 요청은 작성하지 않는다
- 할 일이 없으면 [검증 결과]에 "대기 중 — 추가 요청 없음" 기록
""".strip()

# Claude Code에게 보낼 프롬프트 — 3단계: Codex 구현 결과 반영 + 사용자 관점 검증
CLAUDE_APPLY_PROMPT = """
CLAUDE.md, AGENTS.md, TODO.md를 순서대로 읽어라.

너의 역할은 UX 에이전트 (Claude Code) 다.
이번 단계의 목적은 Codex가 구현한 백엔드 변경사항을 사용자 화면에 반영하고 실제 사용자 관점에서 검증하는 것이다.

작업 순서:
1. TODO.md의 [CODEX 완료] 섹션에서 이번 라운드 구현 내용을 확인한다
2. API 응답 구조나 백엔드 동작 변경이 있으면 templates/, static/ 에 반영한다
3. 앱을 사용자 관점에서 검토해 화면 흐름, 문구, 로딩/빈 상태, 오류 상태를 개선한다
4. 추가 백엔드 수정이 필요하면 [CODEX 요청] 섹션에 새 항목으로 작성한다
5. 이번 프론트 반영 및 검증 결과를 [검증 결과] 섹션에 오늘 날짜와 함께 요약한다

주의:
- app.py, analysis.py, db.py 등 백엔드 파일은 절대 수정하지 않는다
- CLAUDE.md에 명시된 금지 사항을 어기는 요청은 작성하지 않는다
- 할 일이 없으면 [검증 결과]에 "대기 중 — 추가 요청 없음" 기록
""".strip()

# Codex에게 보낼 프롬프트
CODEX_PROMPT = """
AGENTS.md와 TODO.md를 읽어라.

너의 역할은 기능 에이전트 (Codex) 다.

작업 순서:
1. TODO.md의 [CODEX 요청] 섹션에서 미완료 항목 확인
2. 요청된 기능을 백엔드 파일에 구현 (app.py, analysis.py, db.py 등)
3. 완료된 항목에 [완료] 표시
4. 구현 내용을 [CODEX 완료] 섹션에 오늘 날짜와 함께 요약

주의:
- templates/, static/ 파일은 수정하지 않는다
- API 응답 구조가 바뀌면 [CODEX 완료]에 반드시 명시
- 할 일이 없으면 [CODEX 완료]에 "대기 중 — 구현할 요청 없음" 기록
""".strip()

CLAUDE_ALLOWED_PREFIXES = {
    "templates/",
    "static/",
}
CLAUDE_ALLOWED_FILES = {
    "TODO.md",
    "AGENTS.md",
    "CLAUDE.md",
}

CODEX_BLOCKED_PREFIXES = {
    "templates/",
    "static/",
}

CODEX_ALLOWED_FILES = {
    "TODO.md",
}


# ──────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────

def ts():
    return datetime.now().strftime("%H:%M:%S")

def separator(title: str):
    print(f"\n{'='*55}")
    print(f"  [{ts()}]  {title}")
    print(f"{'='*55}")

def _git_lines(args: list[str]) -> set[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_PATH,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return set()
    return {line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()}

def command_parts(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return [command]

def command_label(command: str) -> str:
    return " ".join(command_parts(command))

def resolve_command(command: str) -> list[str]:
    parts = command_parts(command)
    if not parts:
        return parts

    executable = parts[0]
    if Path(executable).is_absolute() or "\\" in executable or "/" in executable:
        return parts

    candidates = [
        shutil.which(executable),
        shutil.which(f"{executable}.cmd"),
        shutil.which(f"{executable}.exe"),
    ]

    if os.name == "nt" and executable.lower() == "codex":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(str(Path(appdata) / "npm" / "codex.cmd"))

    resolved = next((candidate for candidate in candidates if candidate and Path(candidate).exists()), None)
    if resolved:
        return [resolved, *parts[1:]]

    return parts

def print_cli_hint(tool_name: str, command: str, error: str) -> None:
    print(f"\n  ❌ {tool_name} 실행 실패: {error}")
    print(f"     설정된 명령: {command}")

    if tool_name == "Claude Code":
        print("     해결: 터미널에서 `claude auth login`으로 CLI 로그인을 완료하세요.")
        print("     자동화용 장기 토큰이 필요하면 `claude setup-token`도 실행하세요.")
        print("     다른 실행 파일을 써야 하면 `$env:CLAUDE_CMD='경로 또는 명령'`로 지정하세요.")
    elif tool_name == "Codex":
        print("     해결: Python에서 실행 가능한 Codex CLI를 PATH에 추가하세요.")
        print("     Codex Desktop 번들 실행 파일은 Windows 권한 문제로 외부 Python에서 막힐 수 있습니다.")
        print("     다른 실행 파일을 써야 하면 `$env:CODEX_CMD='경로 또는 명령'`로 지정하세요.")

def run_cli(tool_name: str, command: str, args: list[str], *, capture: bool = False, timeout: int | None = None):
    cmd = resolve_command(command) + args
    try:
        return subprocess.run(
            cmd,
            cwd=REPO_PATH,
            capture_output=capture,
            text=capture,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        print_cli_hint(tool_name, command, f"명령을 찾을 수 없음 ({exc})")
    except PermissionError as exc:
        print_cli_hint(tool_name, command, f"권한 거부 ({exc})")
    except subprocess.TimeoutExpired:
        print_cli_hint(tool_name, command, "실행 시간 초과")
    return None

def check_cli(tool_name: str, command: str) -> bool:
    result = run_cli(tool_name, command, ["--version"], capture=True, timeout=15)
    if result is None:
        return False

    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        print_cli_hint(tool_name, command, output or f"종료 코드 {result.returncode}")
        return False

    version = (result.stdout or result.stderr or "").strip().splitlines()
    version_text = version[0] if version else "version 확인됨"
    print(f"  ✅ {tool_name}: {command_label(command)} ({version_text})")
    return True

def preflight() -> None:
    separator("도구 확인")
    ok = True
    ok = check_cli("Claude Code", CLAUDE_CMD) and ok
    ok = check_cli("Codex", CODEX_CMD) and ok

    if not ok:
        print("\n🛑 필요한 CLI를 실행할 수 없어 루프를 시작하지 않습니다.")
        print("   위 안내대로 로그인/PATH/CODEX_CMD 설정을 마친 뒤 다시 실행하세요.")
        sys.exit(1)

def repo_files() -> set[str]:
    tracked = _git_lines(["ls-files"])
    untracked = _git_lines(["ls-files", "--others", "--exclude-standard"])
    return tracked | untracked

def file_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for rel_path in repo_files():
        path = REPO_PATH / rel_path
        if not path.is_file():
            snapshot[rel_path] = "<missing>"
            continue
        snapshot[rel_path] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot

def step_delta(before: dict[str, str]) -> set[str]:
    after = file_snapshot()
    paths = set(before) | set(after)
    return {path for path in paths if before.get(path) != after.get(path)}

def allowed_for_claude(path: str) -> bool:
    return (
        path in CLAUDE_ALLOWED_FILES
        or any(path.startswith(prefix) for prefix in CLAUDE_ALLOWED_PREFIXES)
    )

def allowed_for_codex(path: str) -> bool:
    if path in CODEX_ALLOWED_FILES:
        return True
    return not any(path.startswith(prefix) for prefix in CODEX_BLOCKED_PREFIXES)

def warn_boundary_violations(agent_name: str, files: set[str], checker) -> None:
    violations = sorted(path for path in files if not checker(path))
    if not violations:
        print(f"  ✅ {agent_name} 파일 경계 확인 완료")
        return

    print(f"  ⚠️  {agent_name} 파일 경계 위반 가능성:")
    for path in violations:
        print(f"     - {path}")
    print("     변경사항은 자동 되돌리지 않습니다. 다음 라운드 전 확인하세요.")

def run_claude_code(round_num: int, phase: str, prompt: str) -> bool:
    separator(f"라운드 {round_num} — Claude Code ({phase})")
    print(f"\n📋 프롬프트:\n{prompt}\n")

    result = run_cli(
        "Claude Code",
        CLAUDE_CMD,
        ["--print", "--permission-mode", CLAUDE_PERMISSION_MODE, prompt],
        timeout=CLAUDE_TIMEOUT_SECONDS,
    )
    if result is None:
        return False
    if result.returncode != 0:
        print("\n  ❌ Claude Code 단계 실패")
        print("     로그인 문제라면 터미널에서 `claude auth login`을 실행하세요.")
        print("     `claude --print \"ping\"`이 성공해야 orchestrate.py도 동작합니다.")
        return False
    return True

def run_codex(round_num: int) -> bool:
    separator(f"라운드 {round_num} — Codex (Feature)")
    print(f"\n📋 프롬프트:\n{CODEX_PROMPT}\n")

    result = run_cli(
        "Codex",
        CODEX_CMD,
        [
            "exec",
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            CODEX_PROMPT,
        ],
        timeout=CODEX_TIMEOUT_SECONDS,
    )
    if result is None:
        return False
    if result.returncode != 0:
        print("\n  ❌ Codex 단계 실패")
        return False
    return True

def stop_after_failed_step(step_name: str) -> None:
    print(f"\n🛑 {step_name} 실패로 루프를 중단합니다.")
    print("   문제를 해결한 뒤 `python orchestrate.py`를 다시 실행하세요.")
    sys.exit(1)

def git_commit(round_num: int):
    """라운드 완료 후 자동 커밋 (선택)"""
    msg = f"chore: auto loop round {round_num} [{datetime.now().strftime('%Y-%m-%d %H:%M')}]"
    subprocess.run(["git", "add", "-A"], cwd=REPO_PATH, capture_output=True)
    result = subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=REPO_PATH,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"  ✅ 커밋: {msg}")
    else:
        print(f"  ⚠️  커밋 없음 (변경사항 없거나 실패)")


# ──────────────────────────────────────────
# 메인 루프
# ──────────────────────────────────────────

def main():
    print("🚀 자동 개발 루프 시작")
    print(f"   레포: {REPO_PATH}")
    print(f"   Claude: {command_label(CLAUDE_CMD)}")
    print(f"   Codex: {command_label(CODEX_CMD)}")
    print(f"   Timeout: Claude {CLAUDE_TIMEOUT_SECONDS}s / Codex {CODEX_TIMEOUT_SECONDS}s")
    print("   Ctrl+C 로 중단\n")
    preflight()

    round_num = 0

    try:
        while True:
            round_num += 1

            # 1. Claude Code: UX 검토 + 백엔드 요청 작성
            before = file_snapshot()
            ok = run_claude_code(round_num, "UX 검토", CLAUDE_REVIEW_PROMPT)
            warn_boundary_violations("Claude Code (UX 검토)", step_delta(before), allowed_for_claude)
            if not ok:
                stop_after_failed_step("Claude Code (UX 검토)")

            time.sleep(ROUND_DELAY)

            # 2. Codex: 백엔드 구현
            before = file_snapshot()
            ok = run_codex(round_num)
            warn_boundary_violations("Codex (Feature)", step_delta(before), allowed_for_codex)
            if not ok:
                stop_after_failed_step("Codex (Feature)")

            time.sleep(ROUND_DELAY)

            # 3. Claude Code: Codex 구현 결과를 프론트에 반영 + 검증
            before = file_snapshot()
            ok = run_claude_code(round_num, "프론트 반영/검증", CLAUDE_APPLY_PROMPT)
            warn_boundary_violations("Claude Code (프론트 반영)", step_delta(before), allowed_for_claude)
            if not ok:
                stop_after_failed_step("Claude Code (프론트 반영)")

            # 4. 자동 커밋 (히스토리 추적용)
            print()
            git_commit(round_num)

            print(f"\n⏳ {ROUND_DELAY}초 후 다음 라운드...")
            time.sleep(ROUND_DELAY)

    except KeyboardInterrupt:
        print(f"\n\n🛑 루프 중단. 총 {round_num}라운드 완료.")
        print(f"   TODO.md에서 진행 히스토리를 확인하세요.")
        sys.exit(0)


if __name__ == "__main__":
    main()
