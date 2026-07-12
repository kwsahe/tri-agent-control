#!/usr/bin/env python3
"""
Agent Roundtable — Codex, Antigravity & Claude Code

브라우저에 뜨는 대시보드 하나로 모든 걸 한다 (주제 입력도 그 안의 한 섹션일 뿐,
별도로 안 가로막는다):
  - 연결 확인 상태 (Codex / Antigravity / Claude Code) + 다시 확인 버튼
  - 주제 입력 (미입력 시 대시보드 안에 폼만 보이고, 나머지는 그대로 다 보임)
  - 일시정지 / 재개, 내가 대화에 메시지 보내기, 중단하기, 새 세션
  - 진행 중이던 세션이 있으면 페이지를 열자마자 자동으로 이어서 진행됨
터미널은 시작 시 연결 확인 로그 + 서버 주소 출력, 그리고 Ctrl+C 종료 용도로만 쓴다.

흐름:
  1. 연결 확인 (codex / agy / claude --version) → 대시보드 상단에 표시
  2. 대시보드에서 주제 입력 → 세 에이전트가 순서대로 강점 이야기 → 역할 선언
     (백엔드 / 프론트엔드 / 기획·아이디어 정리)
  3. 매 턴마다 화면이 자동 갱신 (JS 폴링)

파일:
  dashboard_template.html — 대시보드 화면 자체 (HTML/CSS/JS, 여기서 자유롭게 디자인 수정 가능)
  TEAM_PROMPT.md          — 세 에이전트가 매 턴마다 읽는 공통 지침 (자유롭게 수정 가능)
  PROJECT_PATH.txt        — 코딩 모드에서 실제로 작업할 프로젝트 폴더 경로 (한 줄, 자유롭게 변경)
  roundtable_state.json  — 현재 세션 상태 (재개용)
  roundtable.html         — 마지막으로 렌더링된 화면 스냅샷 (참고용, 서버가 실제 화면은 동적으로 서빙)
  roundtable_log.md       — 모든 세션의 대화 기록 (append-only, 사람이 읽는 로그)
  CODEX_Profile.md / ANTIGRAVITY_Profile.md / CLAUDE_Profile.md — 세션마다 쌓이는 강점/역할 프로필

사용법:
    python roundtable.py

환경변수:
    CLAUDE_CMD, CODEX_CMD
    CLAUDE_TIMEOUT_SECONDS (기본 600)
    CODEX_TIMEOUT_SECONDS (기본 900)
    ROUNDTABLE_PORT (기본 8765)
"""

import html
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "roundtable_state.json"
HTML_PATH = ROOT / "roundtable.html"
LOG_PATH = ROOT / "roundtable_log.md"
TEAM_PROMPT_PATH = ROOT / "TEAM_PROMPT.md"
PROFILE_PATHS = {
    "codex": ROOT / "CODEX_Profile.md",
    "antigravity": ROOT / "ANTIGRAVITY_Profile.md",
    "claude": ROOT / "CLAUDE_Profile.md",
}

DEFAULT_TEAM_PROMPT = """# 공통 지침 (Codex / Antigravity / Claude Code 공용)

너희 셋(Codex, Antigravity, Claude Code)은 사용자(팀장)가 고용한 AI 개발 직원이다.
아래 규칙을 답변마다 항상 지켜라.

## 기본 원칙
- 모든 대화는 서로에게 공개된다. 앞서 나온 다른 에이전트의 발언을 반드시 읽고 반영해라.
- 동의하지 않으면 정중하지만 명확하게 반박해라. 근거 없이 무조건 동조하지 마라.
- 역할/의견이 겹치면 반드시 조율해서 겹치지 않게 정리해라.
- 답변은 한국어로, 불필요하게 길게 쓰지 말고 핵심만 간결하게 말해라.
- 너는 실제로 일하는 직원이다. 사용자(팀장)를 위해 실질적인 결론을 내는 것이 목표다.

## 최종 보고
- "최종 보고" 단계를 맡은 사람은 지금까지의 논의(또는 실제로 반영한 작업)를 정리해서
  팀장(사용자)에게 바로 실행 가능한 결론을 보고해야 한다: 누가 뭘 맡았는지 / 무엇이
  어떻게 바뀌었는지, 그리고 다음에 뭘 하면 좋을지.

## 코딩 모드일 때
- 실제로 이 프로젝트 폴더의 파일을 만들거나 수정해도 된다.
- 다른 에이전트가 이미 반영한 변경사항과 충돌하지 않도록 확인하고 작업해라.
- 작업 후에는 어떤 파일을 어떻게 바꿨는지 반드시 요약해서 보고해라.
"""


def ensure_team_prompt() -> None:
    if not TEAM_PROMPT_PATH.exists():
        TEAM_PROMPT_PATH.write_text(DEFAULT_TEAM_PROMPT, encoding="utf-8")


def load_team_prompt() -> str:
    ensure_team_prompt()
    try:
        return TEAM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_TEAM_PROMPT.strip()


# ──────────────────────────────────────────
# 코딩 대상 프로젝트 경로 (이 파일 하나만 고쳐서 다른 프로젝트를 가리키면 됨)
# ──────────────────────────────────────────

PROJECT_PATH_FILE = ROOT / "PROJECT_PATH.txt"

DEFAULT_PROJECT_PATH_CONTENT = f"""# 코딩 모드에서 실제로 작업할 프로젝트 폴더 경로를 한 줄로 적어주세요.
# '#'으로 시작하는 줄은 주석으로 무시됩니다.
# 비워두거나 경로가 존재하지 않으면 이 도구가 있는 폴더를 기본값으로 사용합니다.
# 예: C:\\Users\\me\\projects\\my-app

{ROOT}
"""


def ensure_project_path_file() -> None:
    if not PROJECT_PATH_FILE.exists():
        PROJECT_PATH_FILE.write_text(DEFAULT_PROJECT_PATH_CONTENT, encoding="utf-8")


def load_project_path() -> Path:
    """PROJECT_PATH.txt에 적힌 경로를 매번 새로 읽는다 (실행 중 바꿔도 다음 턴부터 반영)."""
    ensure_project_path_file()
    try:
        lines = PROJECT_PATH_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ROOT
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        candidate = Path(line).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
        print(f"  ⚠️  PROJECT_PATH.txt의 경로를 찾을 수 없습니다: {line} — 기본 폴더({ROOT})를 사용합니다.")
        return ROOT
    return ROOT

CLAUDE_CMD = os.environ.get("CLAUDE_CMD", "claude")
CODEX_CMD = os.environ.get("CODEX_CMD", "codex")
AGY_CMD = os.environ.get("AGY_CMD", "agy")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "600"))
CODEX_TIMEOUT = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "900"))
AGY_TIMEOUT = int(os.environ.get("AGY_TIMEOUT_SECONDS", "600"))
PORT = int(os.environ.get("ROUNDTABLE_PORT", "8765"))

AGENTS = {
    "codex": {"label": "Codex", "color": "#5b8def", "side": "left"},
    "antigravity": {"label": "Antigravity", "color": "#c77dff", "side": "center"},
    "claude": {"label": "Claude Code", "color": "#d97757", "side": "right"},
    "user": {"label": "나 (개입)", "color": "#4caf50", "side": "center"},
}

ROLES = "백엔드(로직/API/데이터), 프론트엔드(UI/사용자 경험), 기획·아이디어 정리(설계/전체 조율)"

# 최종 보고(팀장에게 결론 전달)를 맡을 담당자 — 세 명 중 마지막에 말하는 사람으로 고정
REPORTER_AGENT = "claude"

# 확인 요청 단계의 phase 이름 — worker_loop가 이 문자열로 승인 대기 상태를 감지한다
CONFIRM_PHASE = "확인 요청"

# (agent, phase, instruction, cli_mode) — instruction은 지금까지의 대화 기록 뒤에 붙는다.
# cli_mode는 이 턴에서 실제로 파일을 쓸 수 있게 할지("coding") 읽기 전용으로 할지
# ("discussion")를 결정한다 — 세션 모드(토론/코딩)와는 별개다.
DISCUSSION_STEPS = [
    ("codex", "강점 이야기",
     "지금부터 Antigravity, Claude Code와 함께 셋이서 이 프로젝트를 다룰 거야. 먼저 이 "
     "프로젝트 폴더의 구조와 주요 파일들을 살펴봐 (아직 수정하지 말고 읽기만 해). 그 다음, "
     "그 구조를 근거로 네가 가장 자신 있는 영역이 뭐인지 솔직하게 말해줘 (예: 백엔드 로직, "
     "API 설계, 데이터 처리, 프론트엔드 UI, 기획/아이디어 정리 등). 이유도 함께 3~5문장으로 "
     "간결하게 대답해.", "discussion"),
    ("antigravity", "강점 이야기",
     "너는 Antigravity야. 먼저 이 프로젝트 폴더의 구조와 주요 파일들을 살펴봐 (아직 "
     "수정하지 말고 읽기만 해). 지금까지의 대화도 참고해서, 네가 가장 자신 있는 영역이 "
     "뭐인지 솔직하게 말해줘. Codex 발언에 동의하지 않는 부분이 있으면 반박해도 좋고, "
     "겹치지 않는 부분이 있다면 강조해도 좋아. 이유도 함께 3~5문장으로 간결하게 대답해.",
     "discussion"),
    ("claude", "강점 이야기",
     "너는 Claude Code야. 먼저 이 프로젝트 폴더의 구조와 주요 파일들을 살펴봐 (아직 "
     "수정하지 말고 읽기만 해). 지금까지의 대화도 참고해서, 네가 가장 자신 있는 영역이 "
     "뭐인지 솔직하게 말해줘. Codex나 Antigravity 발언에 동의하지 않으면 반박해도 좋고, "
     "겹치지 않는 부분이 있다면 강조해도 좋아. 이유도 함께 3~5문장으로 간결하게 대답해.",
     "discussion"),
    ("codex", "역할 선언",
     f"지금까지의 대화를 참고해서, 이제 역할을 정하자: 셋이서 {ROLES} 중 서로 겹치지 "
     "않게 하나씩 맡아야 해. 네가 어떤 역할을 맡고 싶은지, 그 이유와 함께 명확하게 "
     "선언해줘. 2~4문장.", "discussion"),
    ("antigravity", "역할 선언",
     f"Codex가 역할을 선언했어. 동의하지 않으면 반박하고 다른 역할을 제안해도 된다. "
     f"지금까지의 대화를 참고해서, 남은 역할({ROLES} 중 Codex가 고르지 않은 것) 중 "
     "하나를 명확하게 선언해줘. Codex와 겹치지 않게. 2~4문장.", "discussion"),
    ("claude", "역할 확정",
     "지금까지의 대화를 참고해서, 너는 Codex와 Antigravity가 고르지 않은 마지막 역할을 "
     "맡거나, 의견이 갈렸다면 반박·조율해서 최종적으로 네 역할을 선언해줘. 겹치지 않게 "
     "명확히 말해. 2~4문장.", "discussion"),
]

REPORT_STEP = (
    REPORTER_AGENT, "최종 보고",
    "지금까지 세 명이 나눈 강점 이야기와 역할 선언을 정리해서, 팀장(사용자)에게 바로 "
    "보고해. 누가 어떤 역할을 맡았는지, 의견이 갈렸다면 어떻게 정리됐는지, 그리고 다음에 "
    "무엇부터 시작하면 좋을지 실행 가능한 결론으로 3~6문장 요약해줘.", "discussion",
)

# 코딩 모드 전용: 역할 확정 뒤 "아직 수정하지 않고" 개선안을 먼저 제안하는 단계.
# 실제 코드 반영은 사용자가 승인한 뒤(/approve)에만 진행된다.
PROPOSAL_STEPS = [
    ("codex", "개선안 제안",
     "네 역할에 맞게, 프로젝트 구조를 다시 확인하고 구체적으로 어떤 파일을 어떻게 "
     "개선/수정할지 제안해. 아직 실제로 파일을 수정하지 마 — 계획만 3~6개 항목으로 "
     "구체적으로 말해.", "discussion"),
    ("antigravity", "개선안 제안",
     "Codex의 제안을 참고해서, 겹치지 않게 네 역할 몫의 개선/수정 계획을 구체적으로 "
     "제안해. 동의하지 않는 부분이 있으면 반박해도 된다. 아직 실제로 파일을 수정하지 "
     "마 — 계획만 3~6개 항목으로 말해.", "discussion"),
    ("claude", CONFIRM_PHASE,
     "Codex와 Antigravity의 제안을 참고해서, 겹치지 않게 네 역할 몫의 개선/수정 계획도 "
     "덧붙이고, 셋의 계획 전체를 하나로 정리해서 팀장(사용자)에게 실제로 진행해도 될지 "
     "확인을 요청하는 메시지를 작성해. 무엇을 어떻게 바꿀지 항목별로 명확하게 정리해. "
     "아직 실제로 파일을 수정하지 마.", "discussion"),
]

# 사용자가 승인한 뒤에만 실행되는 실제 코드 반영 단계
CODING_WORK_STEPS = [
    ("codex", "작업 수행",
     "팀장(사용자)이 방금 승인한 개선안 중 네가 맡은 부분을 이 프로젝트 코드에 실제로 "
     "반영해. 파일을 만들거나 수정해도 좋다. 작업이 끝나면 어떤 파일을 어떻게 바꿨는지 "
     "요약해서 보고해.", "coding"),
    ("antigravity", "작업 수행",
     "팀장(사용자)이 방금 승인한 개선안 중 네가 맡은 부분을, Codex가 방금 한 작업과 "
     "겹치거나 충돌하지 않게 이 프로젝트 코드에 실제로 반영해. 작업이 끝나면 어떤 파일을 "
     "어떻게 바꿨는지 요약해서 보고해.", "coding"),
    ("claude", "작업 수행",
     "팀장(사용자)이 방금 승인한 개선안 중 네가 맡은 부분을, Codex·Antigravity가 방금 "
     "한 작업과 겹치거나 충돌하지 않게 이 프로젝트 코드에 실제로 반영해. 작업이 끝나면 "
     "어떤 파일을 어떻게 바꿨는지 요약해서 보고해.", "coding"),
]

CODING_REPORT_STEP = (
    REPORTER_AGENT, "최종 보고",
    "지금까지 세 명이 각자 실제로 반영한 코드 변경사항을 정리해서, 팀장(사용자)에게 "
    "무엇이 어떻게 바뀌었는지, 다음에 뭘 하면 좋을지 실행 가능한 결론으로 요약해줘.",
    "discussion",
)


def steps_for_mode(mode: str) -> list[tuple[str, str, str, str]]:
    if mode == "coding":
        return DISCUSSION_STEPS + PROPOSAL_STEPS + CODING_WORK_STEPS + [CODING_REPORT_STEP]
    return DISCUSSION_STEPS + [REPORT_STEP]


# ──────────────────────────────────────────
# CLI 실행 유틸
# ──────────────────────────────────────────

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def separator(title: str) -> None:
    print(f"\n{'=' * 55}")
    print(f"  [{ts()}]  {title}")
    print(f"{'=' * 55}")


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

    if os.name == "nt" and executable.lower() == "agy":
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            candidates.append(str(Path(localappdata) / "agy" / "bin" / "agy.exe"))

    resolved = next((c for c in candidates if c and Path(c).exists()), None)
    if resolved:
        return [resolved, *parts[1:]]
    return parts


def print_cli_hint(tool_name: str, command: str, error: str) -> None:
    print(f"\n  ❌ {tool_name} 실행 실패: {error}")
    print(f"     설정된 명령: {command}")
    if tool_name == "Claude Code":
        print("     해결: 터미널에서 `claude auth login`으로 CLI 로그인을 완료하세요.")
    elif tool_name == "Codex":
        print("     해결: Codex CLI가 PATH에 있는지, 로그인이 되어 있는지 확인하세요.")


def run_cli(tool_name: str, command: str, args: list[str], *, timeout: int | None = None, cwd: Path | None = None):
    cmd = resolve_command(command) + args
    try:
        return subprocess.run(
            cmd, cwd=cwd or ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError as exc:
        print_cli_hint(tool_name, command, f"명령을 찾을 수 없음 ({exc})")
    except PermissionError as exc:
        print_cli_hint(tool_name, command, f"권한 거부 ({exc})")
    except subprocess.TimeoutExpired:
        print_cli_hint(tool_name, command, "실행 시간 초과")
    return None


def check_cli(tool_name: str, command: str) -> tuple[bool, str]:
    result = run_cli(tool_name, command, ["--version"], timeout=15)
    if result is None:
        msg = "실행 실패 (명령을 찾을 수 없거나 응답 없음)"
        print_cli_hint(tool_name, command, msg)
        return False, msg
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        msg = output or f"종료 코드 {result.returncode}"
        print_cli_hint(tool_name, command, msg)
        return False, msg
    version_line = (result.stdout or result.stderr or "").strip().splitlines()
    version_text = version_line[0] if version_line else "version 확인됨"
    print(f"  ✅ {tool_name}: {command_label(command)} ({version_text})")
    return True, version_text


# 연결 확인 결과 (대시보드에도 그대로 노출)
CONNECTION_STATUS: dict[str, dict] = {}
CONNECTION_TARGETS = [
    ("codex", "Codex", CODEX_CMD),
    ("antigravity", "Antigravity", AGY_CMD),
    ("claude", "Claude Code", CLAUDE_CMD),
]


def run_connection_checks() -> bool:
    separator("연결 확인")
    all_ok = True
    for key, label, command in CONNECTION_TARGETS:
        ok, detail = check_cli(label, command)
        CONNECTION_STATUS[key] = {"label": label, "ok": ok, "detail": detail}
        all_ok = all_ok and ok
    return all_ok


def preflight() -> None:
    if not run_connection_checks():
        print("\n\U0001f6d1 필요한 CLI를 실행할 수 없어 시작하지 않습니다.")
        sys.exit(1)


def ask_codex(prompt: str, mode: str = "discussion") -> str:
    sandbox = "workspace-write" if mode == "coding" else "read-only"
    result = run_cli(
        "Codex",
        CODEX_CMD,
        ["exec", "--sandbox", sandbox, "--skip-git-repo-check", prompt],
        timeout=CODEX_TIMEOUT,
        cwd=load_project_path(),
    )
    if result is None:
        return "(Codex 응답 없음 — 실행 실패)"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return f"(Codex 응답 없음 — 실행 실패: {detail[:300]})"
    return result.stdout.strip()


def ask_claude(prompt: str, mode: str = "discussion") -> str:
    permission_mode = "acceptEdits" if mode == "coding" else "plan"
    result = run_cli(
        "Claude Code",
        CLAUDE_CMD,
        ["--print", "--permission-mode", permission_mode, prompt],
        timeout=CLAUDE_TIMEOUT,
        cwd=load_project_path(),
    )
    if result is None:
        return "(Claude Code 응답 없음 — 실행 실패)"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return f"(Claude Code 응답 없음 — 실행 실패: {detail[:300]})"
    return result.stdout.strip()


def ask_antigravity(prompt: str, mode: str = "discussion") -> str:
    # agy는 Go 스타일 플래그라 --print 자체가 프롬프트 값을 먹는다. 반드시 마지막에 와야 함.
    if mode == "coding":
        args = ["--mode", "accept-edits", "--print", prompt]
    else:
        args = ["--mode", "plan", "--sandbox", "--print", prompt]
    result = run_cli("Antigravity", AGY_CMD, args, timeout=AGY_TIMEOUT, cwd=load_project_path())
    if result is None:
        return "(Antigravity 응답 없음 — 실행 실패)"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return f"(Antigravity 응답 없음 — 실행 실패: {detail[:300]})"
    return result.stdout.strip()


ASK_FUNCS = {
    "codex": ask_codex,
    "claude": ask_claude,
    "antigravity": ask_antigravity,
}


# ──────────────────────────────────────────
# 상태 / 로그 / 프로필
# ──────────────────────────────────────────

def new_state() -> dict:
    return {"messages": [], "step_index": 0, "finished": False, "topic": "", "mode": "discussion"}


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return new_state()


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_log(agent: str, phase: str, text: str) -> None:
    label = AGENTS[agent]["label"]
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"\n### [{ts()}] {label} — {phase}\n\n{text}\n")


def start_log_session() -> None:
    header = f"\n---\n\n## 세션 시작 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(header)


def update_profile(agent: str, messages: list[dict]) -> None:
    path = PROFILE_PATHS.get(agent)
    if path is None:
        return
    own_msgs = [m for m in messages if m["agent"] == agent]
    if not own_msgs:
        return
    if not path.exists():
        path.write_text(f"# {AGENTS[agent]['label']} Profile\n", encoding="utf-8")
    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"\n## {date}\n"]
    for m in own_msgs:
        lines.append(f"- **{m['phase']}**: {m['text']}\n")
    with path.open("a", encoding="utf-8") as f:
        f.write("".join(lines))


def build_transcript(messages: list[dict]) -> str:
    if not messages:
        return ""
    lines = []
    for m in messages:
        label = AGENTS[m["agent"]]["label"]
        lines.append(f"[{label}] ({m['phase']}): {m['text']}")
    return "\n\n".join(lines)


# ──────────────────────────────────────────
# 공유 상태 (서버 스레드 ↔ 워커 스레드)
# ──────────────────────────────────────────

STATE_LOCK = threading.Lock()
STATE: dict = load_state()
CONTROL = {"paused": False, "stopped": False, "worker_running": False, "awaiting_approval": False}


def bubble_html(m: dict) -> str:
    agent = AGENTS[m["agent"]]
    side = agent["side"]
    phase = m.get("phase", "")
    highlight_class = " report" if phase == "최종 보고" else (" confirm" if phase == CONFIRM_PHASE else "")
    return f"""
    <div class="row {side}{highlight_class}">
      <div class="bubble" style="--accent:{agent['color']}">
        <div class="meta"><span class="name">{html.escape(agent['label'])}</span><span class="phase">{html.escape(m.get('phase', ''))}</span><span class="time">{html.escape(m['time'])}</span></div>
        <div class="text">{html.escape(m['text']).replace(chr(10), '<br>')}</div>
      </div>
    </div>"""


def status_text() -> str:
    if STATE.get("finished"):
        return "완료"
    if CONTROL["stopped"]:
        return "중단됨"
    if CONTROL["awaiting_approval"]:
        return "\U0001f6d1 사용자 승인 대기 중"
    if CONTROL["paused"]:
        return "일시정지"
    return "진행 중..."


def render_html_snapshot() -> None:
    """참고용 정적 스냅샷 파일 (roundtable.html). 실제 화면은 서버가 동적으로 서빙한다."""
    HTML_PATH.write_text(render_dashboard(), encoding="utf-8")


def add_message(agent: str, phase: str, text: str) -> None:
    with STATE_LOCK:
        STATE["messages"].append({"agent": agent, "phase": phase, "time": ts(), "text": text})
        save_state(STATE)
    append_log(agent, phase, text)
    render_html_snapshot()


# ──────────────────────────────────────────
# 워커 (에이전트 호출 루프) — 백그라운드 스레드
# ──────────────────────────────────────────

def run_step(agent: str, phase: str, instruction: str, cli_mode: str) -> None:
    label = AGENTS[agent]["label"]
    separator(f"{label} — {phase}")
    with STATE_LOCK:
        topic = STATE.get("topic", "").strip()
        transcript = build_transcript(STATE["messages"])
    team_prompt = load_team_prompt()
    topic_line = f"오늘 다룰 주제/프로젝트: {topic}\n\n" if topic else ""
    body = f"지금까지의 대화 기록:\n\n{transcript}\n\n---\n\n{instruction}" if transcript else instruction
    prompt = f"{team_prompt}\n\n---\n\n{topic_line}{body}"

    print(f"\U0001f914 {label} 생각 중... (입력 약 {len(prompt)}자, cli_mode={cli_mode})")
    t0 = time.time()
    text = ASK_FUNCS[agent](prompt, cli_mode)
    elapsed = time.time() - t0
    est_tokens = (len(prompt) + len(text)) // 4
    print(f"✅ {label} 응답 완료 — {elapsed:.1f}초, 추정 토큰 ~{est_tokens} "
          f"(입력 {len(prompt)}자 / 출력 {len(text)}자)")
    print(text)
    add_message(agent, phase, text)


def worker_loop() -> None:
    with STATE_LOCK:
        mode = STATE.get("mode", "discussion")
    steps = steps_for_mode(mode)
    try:
        while True:
            with STATE_LOCK:
                step_index = STATE["step_index"]
            if step_index >= len(steps):
                break
            if CONTROL["stopped"]:
                break
            while CONTROL["paused"] and not CONTROL["stopped"]:
                time.sleep(0.3)
            if CONTROL["stopped"]:
                break

            agent, phase, instruction, cli_mode = steps[step_index]
            run_step(agent, phase, instruction, cli_mode)

            with STATE_LOCK:
                STATE["step_index"] += 1
                save_state(STATE)

            if phase == CONFIRM_PHASE and not CONTROL["stopped"]:
                CONTROL["awaiting_approval"] = True
                separator("사용자 승인 대기 중 — 대시보드에서 승인해야 코딩이 진행됩니다")
                while CONTROL["awaiting_approval"] and not CONTROL["stopped"]:
                    time.sleep(0.3)
    finally:
        with STATE_LOCK:
            finished = STATE["step_index"] >= len(steps) and not CONTROL["stopped"]
            STATE["finished"] = finished
            save_state(STATE)
            messages_copy = list(STATE["messages"])
        render_html_snapshot()
        if finished:
            for agent in ("codex", "antigravity", "claude"):
                update_profile(agent, messages_copy)
        CONTROL["worker_running"] = False
        separator("워커 종료" if not finished else "완료")


def start_worker_if_needed() -> None:
    if CONTROL["worker_running"]:
        return
    CONTROL["worker_running"] = True
    CONTROL["stopped"] = False
    thread = threading.Thread(target=worker_loop, daemon=True)
    thread.start()


# ──────────────────────────────────────────
# HTML 대시보드 (항상 이 한 화면 — 주제 입력도 이 안의 한 섹션일 뿐)
# 화면 자체는 dashboard_template.html에 따로 빼두고, 여기서는 값만 채워 넣는다.
# ──────────────────────────────────────────

DASHBOARD_TEMPLATE_PATH = ROOT / "dashboard_template.html"


def load_dashboard_template() -> str:
    return DASHBOARD_TEMPLATE_PATH.read_text(encoding="utf-8")


def connection_status_html() -> str:
    if not CONNECTION_STATUS:
        return '<span class="conn-item">⏳ 연결 확인 대기 중</span>'
    items = []
    for key, label, _cmd in CONNECTION_TARGETS:
        info = CONNECTION_STATUS.get(key)
        if not info:
            items.append(f'<span class="conn-item">⏳ {html.escape(label)}</span>')
            continue
        icon = "✅" if info["ok"] else "❌"
        items.append(
            f'<span class="conn-item">{icon} {html.escape(info["label"])} '
            f'<small>{html.escape(info["detail"][:60])}</small></span>'
        )
    return "".join(items)


MODE_LABELS = {"discussion": "일반 토론 모드", "coding": "코딩 모드"}


def render_dashboard() -> str:
    with STATE_LOCK:
        messages = list(STATE["messages"])
        topic = STATE.get("topic", "").strip()
        finished = STATE.get("finished", False)
        mode = STATE.get("mode", "discussion")
    bubbles = "".join(bubble_html(m) for m in messages) if messages else \
        '<p style="text-align:center;color:#6a6f7c">아직 대화가 없습니다.</p>'
    paused = CONTROL["paused"]
    stopped = CONTROL["stopped"]

    project_path_line = (
        f'<p style="margin:6px 0 0;font-size:12px;color:#7a7f8c">코딩 모드 작업 경로: '
        f'{html.escape(str(load_project_path()))} '
        f'(<code>{html.escape(PROJECT_PATH_FILE.name)}</code>에서 변경)</p>'
    )

    if topic:
        mode_label = MODE_LABELS.get(mode, mode)
        topic_section = (
            f'<div class="topic">\U0001f4cc {html.escape(topic)} '
            f'<span style="opacity:.6">· {html.escape(mode_label)}</span>'
            f'{project_path_line if mode == "coding" else ""}</div>'
        )
    else:
        topic_section = f"""
      <div class="panel">
        <p style="margin:0;font-size:13.5px;color:#c7cad3">토론 주제를 입력하면 바로 시작합니다.</p>
        <textarea id="topicInput" placeholder="예: 네이버 쇼핑 최저가 비교 웹앱을 같이 만들 거야" autofocus></textarea>
        <div class="mode-choice">
          <label><input type="radio" name="mode" value="discussion" checked> 일반 토론 모드 (읽기 전용, 강점/역할 논의)</label>
          <label><input type="radio" name="mode" value="coding"> 코딩 모드 (실제로 이 폴더 코드를 수정)</label>
        </div>
        {project_path_line}
        <button onclick="submitTopic()">시작</button>
      </div>"""

    no_topic = "disabled" if not topic else ""
    pause_disabled = "disabled" if (not topic or finished or stopped or paused) else ""
    resume_disabled = "disabled" if (not topic or finished or stopped or not paused) else ""
    stop_disabled = "disabled" if (not topic or finished or stopped) else ""

    page = load_dashboard_template()
    replacements = {
        "__STATUS__": status_text(),
        "__CONN_HTML__": connection_status_html(),
        "__TOPIC_SECTION__": topic_section,
        "__FEED__": bubbles,
        "__PAUSE_DISABLED__": pause_disabled,
        "__RESUME_DISABLED__": resume_disabled,
        "__STOP_DISABLED__": stop_disabled,
        "__NO_TOPIC__": no_topic,
    }
    for token, value in replacements.items():
        page = page.replace(token, value)
    return page


def state_json_payload() -> dict:
    with STATE_LOCK:
        messages = list(STATE["messages"])
        finished = STATE.get("finished", False)
        topic = STATE.get("topic", "").strip()
        mode = STATE.get("mode", "discussion")
    bubbles = "".join(bubble_html(m) for m in messages) if messages else \
        '<p style="text-align:center;color:#6a6f7c">아직 대화가 없습니다.</p>'
    return {
        "feed_html": bubbles,
        "status": status_text(),
        "finished": finished,
        "paused": CONTROL["paused"],
        "stopped": CONTROL["stopped"],
        "awaiting_approval": CONTROL["awaiting_approval"],
        "topic": html.escape(topic) if topic else "",
        "mode_label": MODE_LABELS.get(mode, mode),
        "conn_html": connection_status_html(),
    }


def maybe_autostart_worker() -> None:
    with STATE_LOCK:
        has_topic = bool(STATE.get("topic"))
        not_finished = not STATE.get("finished", False)
    if has_topic and not_finished and not CONTROL["stopped"]:
        start_worker_if_needed()


# ──────────────────────────────────────────
# HTTP 핸들러
# ──────────────────────────────────────────

class RoundtableHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass  # 콘솔에 HTTP 접근 로그를 찍지 않음

    def _send_html(self, body: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        return parse_qs(body)

    def do_GET(self) -> None:
        if self.path.startswith("/state.json"):
            self._send_json(state_json_payload())
            return
        maybe_autostart_worker()
        self._send_html(render_dashboard())

    def do_POST(self) -> None:
        if self.path == "/topic":
            form = self._read_form()
            topic = form.get("topic", [""])[0].strip()
            mode = form.get("mode", ["discussion"])[0].strip()
            if mode not in MODE_LABELS:
                mode = "discussion"
            if topic:
                with STATE_LOCK:
                    STATE["topic"] = topic
                    STATE["mode"] = mode
                    save_state(STATE)
                start_log_session()
                print(f"  \U0001f4cc 토론 주제: {topic} ({MODE_LABELS[mode]})")
                start_worker_if_needed()
            self._send_html(render_dashboard())
            return

        if self.path == "/preflight":
            run_connection_checks()
            self._send_json(state_json_payload())
            return

        if self.path == "/restart":
            with STATE_LOCK:
                STATE.clear()
                STATE.update(new_state())
                save_state(STATE)
            CONTROL["paused"] = False
            CONTROL["awaiting_approval"] = False
            CONTROL["stopped"] = True  # 혹시 워커가 돌고 있었다면 다음 체크포인트에서 멈추게
            self._send_html(render_dashboard())
            return

        if self.path == "/message":
            form = self._read_form()
            text = form.get("text", [""])[0].strip()
            if text:
                add_message("user", "사용자 개입", text)
            self._send_json(state_json_payload())
            return

        if self.path == "/pause":
            CONTROL["paused"] = True
            self._send_json(state_json_payload())
            return

        if self.path == "/resume":
            CONTROL["paused"] = False
            self._send_json(state_json_payload())
            return

        if self.path == "/stop":
            CONTROL["stopped"] = True
            CONTROL["paused"] = False
            self._send_json(state_json_payload())
            return

        if self.path == "/approve":
            CONTROL["awaiting_approval"] = False
            self._send_json(state_json_payload())
            return

        self.send_response(404)
        self.end_headers()


class RoundtableServer(HTTPServer):
    # HTTPServer의 기본값(allow_reuse_address=1)은 Windows에서 서로 다른 프로세스가
    # 같은 포트에 동시에 바인딩되는 걸 허용해버려서, 이전 테스트/좀비 프로세스가 떠 있으면
    # 요청이 죽은 프로세스로 튀어 빈 화면이 뜨는 원인이 된다. 끄면 포트 충돌이 제대로
    # OSError로 감지되어 아래 start_server()가 다음 포트로 넘어간다.
    allow_reuse_address = False


def start_server() -> tuple[HTTPServer, int]:
    port = PORT
    for candidate in range(port, port + 10):
        try:
            server = RoundtableServer(("127.0.0.1", candidate), RoundtableHandler)
            return server, candidate
        except OSError:
            continue
    print("  ❌ 로컬 서버 포트를 열 수 없습니다 (8765~8774 모두 사용 중). "
          "이전에 열어둔 roundtable.py 창이 남아있는지 확인하세요.")
    sys.exit(1)


# ──────────────────────────────────────────
# 메인
# ──────────────────────────────────────────

def main() -> None:
    print("\U0001f680 Agent Roundtable — Codex, Antigravity & Claude Code")
    print(f"   Codex: {command_label(CODEX_CMD)}")
    print(f"   Antigravity: {command_label(AGY_CMD)}")
    print(f"   Claude Code: {command_label(CLAUDE_CMD)}")
    ensure_team_prompt()
    print(f"   공통 지침: {TEAM_PROMPT_PATH}")
    ensure_project_path_file()
    print(f"   코딩 대상 폴더: {load_project_path()}  (바꾸려면 {PROJECT_PATH_FILE.name} 수정)")
    preflight()

    render_html_snapshot()
    server, port = start_server()
    url = f"http://127.0.0.1:{port}/"
    print(f"\n\U0001f310 브라우저에서 모든 것을 통제하세요: {url}")
    print("   (주제 입력, 일시정지/재개, 메시지 개입, 중단 — 전부 이 페이지에서 합니다)")
    webbrowser.open(url)
    maybe_autostart_worker()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n\n\U0001f6d1 Ctrl+C 감지 — 서버를 종료합니다. 진행 상황은 저장되어 있습니다.")
        CONTROL["stopped"] = True
        server.shutdown()

    separator("종료")
    print(f"대화 상태: {STATE_PATH}")
    print(f"대화 로그: {LOG_PATH}")
    print(f"프로필: {PROFILE_PATHS['codex']}, {PROFILE_PATHS['antigravity']}, {PROFILE_PATHS['claude']}")


if __name__ == "__main__":
    main()
