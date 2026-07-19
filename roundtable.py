#!/usr/bin/env python3
"""
TriAgent Control — Codex, Antigravity & Claude Code

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
import importlib.util
import inspect
import difflib
import fnmatch
import json
import locale
import math
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "roundtable_state.json"
HTML_PATH = ROOT / "roundtable.html"
LOG_PATH = ROOT / "roundtable_log.md"
SESSIONS_DIR = ROOT / "sessions"
MEMORY_DIR = ROOT / "roundtable_memory"
TEAM_PROMPT_PATH = ROOT / "TEAM_PROMPT.md"
ROLE_POLICY_PATH = ROOT / "ROLE_POLICY.md"
PROFILE_PATHS = {
    "codex": ROOT / "CODEX_Profile.md",
    "antigravity": ROOT / "ANTIGRAVITY_Profile.md",
    "claude": ROOT / "CLAUDE_Profile.md",
}
NO_PROJECT_DIR = Path(tempfile.gettempdir()) / "triagent-control-chat-only"

ACTIVE_PROCESS_LOCK = threading.Lock()
ACTIVE_PROCESSES: dict[str, subprocess.Popen] = {}


def terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def cancel_active_cli_processes() -> list[str]:
    cancelled = []
    with ACTIVE_PROCESS_LOCK:
        processes = list(ACTIVE_PROCESSES.items())
    for tool_name, process in processes:
        if process.poll() is not None:
            continue
        try:
            terminate_process_tree(process)
            cancelled.append(tool_name)
        except (OSError, subprocess.SubprocessError):
            continue
    with ACTIVE_PROCESS_LOCK:
        for tool_name, process in list(ACTIVE_PROCESSES.items()):
            if process.poll() is not None:
                ACTIVE_PROCESSES.pop(tool_name, None)
    return cancelled

SESSION_PROFILE_TEMPLATE = """# Session Role Profile

이 파일은 이 채팅 세션의 역할/강점/분담을 관리하는 프로필이다.
매 세션은 이 기본값에서 시작하고, 에이전트들이 토의한 뒤 나온 역할 선언과 조율 내용을 여기에 누적한다.

## 운영 규칙
- 각 에이전트는 이 세션에서 맡은 역할과 책임 범위를 우선 따른다.
- 역할이 충돌하면 대화에서 조율하고, 조율 결과를 다음 응답에 명확히 남긴다.
- 이 프로필은 세션별 기록이다. 다른 세션의 역할 분담과 섞지 않는다.

## 현재 역할
- Codex: 미정
- Antigravity: 미정
- Claude Code: 미정

## 변경 기록
"""

AGENT_PROFILE_TEMPLATES = {
    "codex": """# Codex Profile

## 기본 성향
- 코드 구조, 백엔드 로직, 상태 관리, 실행 검증을 우선적으로 점검한다.
- 다른 에이전트와 역할이 겹치면 구현 책임과 검증 책임을 분리한다.

## 이번 세션 역할
- 미정

## 세션 기록
""",
    "antigravity": """# Antigravity Profile

## 기본 성향
- UI 흐름, 프론트엔드 구조, 상호작용, 사용자 경험을 우선적으로 점검한다.
- 시각적 개선을 제안할 때 실제 구현 파일과 사용자 동선을 함께 본다.

## 이번 세션 역할
- 미정

## 세션 기록
""",
    "claude": """# Claude Code Profile

## 기본 성향
- 기획 정리, 역할 조율, 최종 보고, 사용자 실행 관점의 결론을 우선적으로 점검한다.
- 의견이 갈리면 결론과 다음 행동을 명확히 정리한다.

## 이번 세션 역할
- 미정

## 세션 기록
""",
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


def save_project_path(path: Path) -> None:
    PROJECT_PATH_FILE.write_text(
        "# TriAgent Control 작업 폴더입니다. 대시보드에서 다시 선택할 수 있습니다.\n\n"
        f"{path}\n",
        encoding="utf-8",
    )


def choose_workspace_folder(initial_dir: Path) -> str | None:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            parent=root,
            title="TriAgent Control 작업 폴더 선택",
            initialdir=str(initial_dir),
            mustexist=True,
        )
        root.destroy()
        return selected or None
    except Exception as exc:
        print(f"  ⚠️  작업 폴더 선택 창을 열지 못했습니다: {exc}")
        return None

CLAUDE_CMD = os.environ.get("CLAUDE_CMD", "claude")
CODEX_CMD = os.environ.get("CODEX_CMD", "codex")
AGY_CMD = os.environ.get("AGY_CMD", "agy")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "600"))
CODEX_TIMEOUT = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "900"))
AGY_TIMEOUT = int(os.environ.get("AGY_TIMEOUT_SECONDS", "600"))
PORT = int(os.environ.get("ROUNDTABLE_PORT", "8765"))
# 프롬프트에 매번 재전송할 최근 대화 개수 — 전체 기록은 roundtable_memory/<id>/full.md에
# 남기고, 프롬프트에는 요약 + 최근 메시지만 넣어 토큰 낭비를 줄인다.
TRANSCRIPT_WINDOW = int(os.environ.get("ROUNDTABLE_TRANSCRIPT_WINDOW", "2"))
MEMORY_BRIEF_LINES = int(os.environ.get("ROUNDTABLE_MEMORY_BRIEF_LINES", "6"))
TRANSCRIPT_MAX_CHARS = int(os.environ.get("ROUNDTABLE_TRANSCRIPT_MAX_CHARS", "1600"))
MEMORY_CONTEXT_MAX_CHARS = int(os.environ.get("ROUNDTABLE_MEMORY_CONTEXT_MAX_CHARS", "1800"))
TEAM_PROMPT_MAX_CHARS = int(os.environ.get("ROUNDTABLE_TEAM_PROMPT_MAX_CHARS", "1400"))
PROMPT_MAX_CHARS = int(os.environ.get("ROUNDTABLE_PROMPT_MAX_CHARS", "5000"))
OUTPUT_MAX_CHARS = int(os.environ.get("ROUNDTABLE_OUTPUT_MAX_CHARS", "2000"))
PROJECT_SNAPSHOT_MAX_ENTRIES = int(os.environ.get("ROUNDTABLE_SNAPSHOT_MAX_ENTRIES", "20000"))
CODEX_CONTEXT_TOKENS = int(os.environ.get("CODEX_CONTEXT_TOKENS", "258400"))
CLAUDE_CONTEXT_TOKENS = int(os.environ.get("CLAUDE_CONTEXT_TOKENS", "128000"))
AGY_CONTEXT_TOKENS = int(os.environ.get("AGY_CONTEXT_TOKENS", "1048576"))

APPROVAL_TOKEN = "APPROVE"
MAX_DELEGATION_DEPTH = 2
MAX_SESSION_DELEGATIONS = 12
_AGENT_CALL_RE = re.compile(
    r"^\s*CALL_AGENT:\s*(codex|antigravity|claude)\s*\|\s*(discussion|coding)\s*\|\s*(.+?)\s*$",
    re.IGNORECASE,
)
_TARGET_PREFIX_RE = re.compile(
    r"^\s*\[(codex|antigravity|claude(?:\s+code)?)\]\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)

AGENTS = {
    "codex": {"label": "Codex", "color": "#4f8cff", "side": "left", "avatar": "/static/agents/codex.png"},
    "antigravity": {"label": "Antigravity", "color": "#a66cff", "side": "center", "avatar": "/static/agents/antigravity.png"},
    "claude": {"label": "Claude Code", "color": "#ff8a3d", "side": "right", "avatar": "/static/agents/claude.svg"},
    "user": {"label": "나 (개입)", "color": "#42c991", "side": "center", "avatar": "/static/agents/user.svg"},
    "system": {"label": "TriAgent Control", "color": "#8b95a7", "side": "center", "avatar": ""},
}

MODEL_CATALOG = {
    "codex": {
        "models": [
            ("", "CLI 기본값"),
            ("gpt-5.6-codex", "GPT-5.6 Codex"),
            ("gpt-5.5-codex", "GPT-5.5 Codex"),
            ("gpt-5.4", "GPT-5.4"),
        ],
        "efforts": [("", "기본"), ("low", "낮음"), ("medium", "중간"), ("high", "높음"), ("xhigh", "매우 높음")],
    },
    "claude": {
        "models": [("", "CLI 기본값"), ("sonnet", "Sonnet"), ("opus", "Opus"), ("fable", "Fable")],
        "efforts": [("", "기본"), ("low", "낮음"), ("medium", "중간"), ("high", "높음"), ("xhigh", "매우 높음"), ("max", "최대")],
    },
    "antigravity": {
        "models": [
            ("", "CLI 기본값"),
            ("Gemini 3.5 Flash (Low)", "Gemini 3.5 Flash · 낮음"),
            ("Gemini 3.5 Flash (Medium)", "Gemini 3.5 Flash · 중간"),
            ("Gemini 3.5 Flash (High)", "Gemini 3.5 Flash · 높음"),
            ("Gemini 3.1 Pro (Low)", "Gemini 3.1 Pro · 낮음"),
            ("Gemini 3.1 Pro (High)", "Gemini 3.1 Pro · 높음"),
            ("Claude Sonnet 4.6 (Thinking)", "Claude Sonnet 4.6 · Thinking"),
            ("Claude Opus 4.6 (Thinking)", "Claude Opus 4.6 · Thinking"),
            ("GPT-OSS 120B (Medium)", "GPT-OSS 120B · 중간"),
        ],
        "efforts": [],
    },
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
     "반영해. 직전 확인 요청에서 네 모델에 배정된 대상 파일과 기능만 수정하고 다른 모델을 호출하지 마. "
     "작업이 끝나면 어떤 파일을 어떻게 바꿨는지 요약해서 보고해.", "coding"),
    ("antigravity", "작업 수행",
     "팀장(사용자)이 방금 승인한 개선안 중 네가 맡은 부분을, Codex가 방금 한 작업과 "
     "겹치거나 충돌하지 않게 이 프로젝트 코드에 실제로 반영해. 직전 확인 요청에서 네 모델에 "
     "배정된 대상 파일과 기능만 수정하고 다른 모델을 호출하지 마. 작업이 끝나면 어떤 파일을 "
     "어떻게 바꿨는지 요약해서 보고해.", "coding"),
    ("claude", "작업 수행",
     "팀장(사용자)이 방금 승인한 개선안 중 네가 맡은 부분을, Codex·Antigravity가 방금 "
     "한 작업과 겹치거나 충돌하지 않게 이 프로젝트 코드에 실제로 반영해. 직전 확인 요청에서 "
     "네 모델에 배정된 대상 파일과 기능만 수정하고 다른 모델을 호출하지 마. 작업이 끝나면 "
     "어떤 파일을 어떻게 바꿨는지 요약해서 보고해.", "coding"),
]

CODING_REPORT_STEP = (
    REPORTER_AGENT, "최종 보고",
    "지금까지 세 명이 각자 실제로 반영한 코드 변경사항을 정리해서, 팀장(사용자)에게 "
    "무엇이 어떻게 바뀌었는지, 다음에 뭘 하면 좋을지 실행 가능한 결론으로 요약해줘.",
    "discussion",
)

CONTINUOUS_CODING_STEPS = [
    ("codex", "개발 진행",
     "현재 프로젝트 상태, 최근 교차 검토와 통합 결론을 먼저 확인해. 아직 완성되지 않은 항목 중 가장 중요한 담당 작업을 "
     "계획 보고로 끝내지 말고 실제 코드로 구현해. 기존 변경과 충돌하지 않게 수정하고 가능한 검증을 실행한 뒤 변경 파일과 결과만 간결하게 보고해.",
     "coding"),
    ("antigravity", "개발 진행",
     "Codex가 방금 반영한 내용과 최근 교차 검토를 확인해. 중복을 피하면서 프로젝트 완성도를 가장 크게 높일 담당 작업을 "
     "실제 코드로 구현하고 검증해. 변경 파일, 구현 결과와 남은 위험만 간결하게 보고해.", "coding"),
    ("claude", "개발 진행",
     "Codex와 Antigravity가 이번 사이클에 반영한 변경과 최근 통합 결론을 확인해. 빠진 구현이나 연결 작업을 실제 코드로 "
     "완료하고 검증해. 변경 파일, 구현 결과와 남은 위험만 간결하게 보고해.", "coding"),
    ("codex", "교차 검토",
     "이번 사이클의 세 모델 변경과 자동 검증 결과를 읽기 전용으로 검토해. 실제 사용자 요구 대비 결함, 미완성 기능, 회귀 위험, "
     "누락 테스트를 우선순위로 정리하고 다음 사이클에서 반드시 고칠 항목을 제안해. 파일은 수정하지 마.", "discussion"),
    ("antigravity", "교차 검토",
     "이번 사이클 변경과 Codex 검토 의견을 프로젝트 파일에 대조해. 동의하지 않는 부분은 반박하고, 놓친 결함과 다음 구현 우선순위를 "
     "구체적으로 제안해. 파일은 수정하지 마.", "discussion"),
    ("claude", "교차 검토",
     "이번 사이클 변경, 자동 검증, 두 모델의 검토 의견을 프로젝트 파일에 대조해. 사용자 경험과 전체 연결 관점에서 남은 결함과 "
     "다음 구현 우선순위를 제안해. 파일은 수정하지 마.", "discussion"),
]


def continuous_coding_steps(enabled_agents: list[str]) -> list[tuple[str, str, str, str]]:
    enabled = normalize_enabled_agents(enabled_agents)
    steps = [step for step in CONTINUOUS_CODING_STEPS if step[0] in enabled]
    reporter = reporter_for(enabled)
    steps.append((
        reporter,
        "토론 결과 통합",
        "이번 사이클의 구현, 자동 검증과 세 모델 검토 의견을 하나의 결론으로 통합해. 의견 충돌을 정리하고 다음 사이클에서 "
        "각 모델이 실제로 구현할 우선순위를 명확히 지정해. 프로젝트가 충분히 완성됐다고 판단해도 사용자가 중단하기 전까지 "
        "품질, 테스트, 접근성, 성능과 운영 완성도를 다시 점검할 다음 작업을 선정해. 파일은 수정하지 마.",
        "discussion",
    ))
    return steps


AGENT_ORDER = ["codex", "antigravity", "claude"]

ROLE_CATALOG = {
    "architect": {
        "label": "시스템 설계자",
        "summary": "구조, 경계, 인터페이스와 기술 결정을 설계합니다.",
        "scope": "설계 문서와 ADR",
        "can_write": True,
        "patterns": ["*.md", "docs/**", "adr/**"],
    },
    "backend": {
        "label": "백엔드 개발자",
        "summary": "서버 로직, API, 데이터 저장소와 마이그레이션을 담당합니다.",
        "scope": "백엔드 코드와 설정",
        "can_write": True,
        "patterns": ["*.py", "*.go", "*.java", "*.cs", "*.rb", "*.php", "backend/**", "server/**", "api/**", "db/**", "migrations/**", "config/**"],
    },
    "frontend": {
        "label": "프론트엔드 개발자",
        "summary": "화면, 컴포넌트, 스타일과 사용자 상호작용을 구현합니다.",
        "scope": "템플릿과 프론트엔드 코드",
        "can_write": True,
        "patterns": ["*.html", "*.css", "*.js", "*.ts", "*.jsx", "*.tsx", "*.vue", "*.svelte", "templates/**", "static/**", "frontend/**", "client/**", "web/**"],
    },
    "ux": {
        "label": "UX·접근성 담당",
        "summary": "사용 흐름, 반응형 화면, 키보드와 스크린리더 접근성을 개선합니다.",
        "scope": "UI와 접근성 관련 파일",
        "can_write": True,
        "patterns": ["*.html", "*.css", "*.js", "*.ts", "*.jsx", "*.tsx", "templates/**", "static/**", "frontend/**", "client/**", "web/**"],
    },
    "qa": {
        "label": "QA·테스트 담당",
        "summary": "테스트, 재현 절차, 회귀 검증과 품질 기준을 담당합니다.",
        "scope": "테스트 코드와 테스트 설정",
        "can_write": True,
        "patterns": ["tests/**", "test/**", "spec/**", "test_*.py", "*_test.*", "*.spec.*", "*.test.*", "pytest.ini", "tox.ini", "playwright.config.*", "vitest.config.*", "jest.config.*"],
    },
    "reviewer": {
        "label": "코드 리뷰어",
        "summary": "변경 사항의 결함, 회귀 위험과 누락된 테스트를 검토합니다.",
        "scope": "읽기 전용 검토",
        "can_write": False,
        "patterns": [],
    },
    "security": {
        "label": "보안 담당",
        "summary": "인증, 권한, 입력 검증, 비밀정보와 취약점을 점검합니다.",
        "scope": "보안·인증 코드와 보안 테스트",
        "can_write": True,
        "patterns": ["**/auth*", "**/security*", "**/permission*", "**/secret*", "tests/security/**", "tests/auth/**", "config/**", ".env.example"],
    },
    "ai_data": {
        "label": "AI·데이터 담당",
        "summary": "모델 연동, 프롬프트, 데이터 처리와 분석 로직을 담당합니다.",
        "scope": "AI·데이터·분석 코드",
        "can_write": True,
        "patterns": ["ai/**", "ml/**", "data/**", "prompts/**", "**/llm*", "**/model*", "**/analysis*", "*.ipynb"],
    },
    "devops": {
        "label": "DevOps 담당",
        "summary": "빌드, 배포, CI, 컨테이너와 운영 설정을 담당합니다.",
        "scope": "인프라와 자동화 설정",
        "can_write": True,
        "patterns": [".github/**", ".gitlab-ci.yml", "Dockerfile*", "docker-compose*", "compose*.yml", "infra/**", "deploy/**", "scripts/**", "Makefile", "*.toml", "*.yaml", "*.yml"],
    },
    "performance": {
        "label": "성능 담당",
        "summary": "병목 측정, 캐시, 쿼리와 렌더링 성능을 개선합니다.",
        "scope": "성능 관련 코드와 벤치마크",
        "can_write": True,
        "patterns": ["benchmarks/**", "benchmark/**", "perf/**", "*.py", "*.go", "*.java", "*.js", "*.ts", "*.css", "*.html", "backend/**", "server/**", "frontend/**", "static/**", "templates/**"],
    },
    "product": {
        "label": "제품 기획자",
        "summary": "요구사항, 우선순위, 수용 기준과 사용자 흐름을 정리합니다.",
        "scope": "기획 문서",
        "can_write": True,
        "patterns": ["*.md", "docs/**", "product/**", "requirements/**"],
    },
    "docs": {
        "label": "문서 담당",
        "summary": "README, 사용법, 운영 문서와 변경 기록을 정리합니다.",
        "scope": "문서 파일",
        "can_write": True,
        "patterns": ["*.md", "docs/**", "CHANGELOG*", "LICENSE*"],
    },
    "coordinator": {
        "label": "통합 조정자",
        "summary": "모델 간 작업 분배, 충돌 조정과 최종 결과 통합을 담당합니다.",
        "scope": "읽기 전용 조정",
        "can_write": False,
        "patterns": [],
    },
}


def normalize_agent_roles(roles: dict | None) -> dict:
    source = roles if isinstance(roles, dict) else {}
    return {
        agent: source.get(agent, "") if source.get(agent, "") in ROLE_CATALOG else ""
        for agent in AGENT_ORDER
    }


def role_label(role_id: str) -> str:
    return ROLE_CATALOG.get(role_id, {}).get("label", "미지정")


ROLE_SELECTION_RE = re.compile(r"(?im)^\s*ROLE_SELECT\s*:\s*([a-z_]+)\s*$")
ROLE_PREFERENCES = {
    "codex": ["backend", "architect", "ai_data", "performance", "security"],
    "antigravity": ["qa", "reviewer", "ux", "security", "devops"],
    "claude": ["frontend", "product", "architect", "docs", "coordinator"],
}


def role_choice_instruction() -> str:
    choices = ", ".join(f"{role_id}={role['label']}" for role_id, role in ROLE_CATALOG.items())
    return (
        "\n\n대화로 역할을 조율한 뒤 다음 선택지에서 네 역할 하나를 직접 선택해. "
        f"선택지: {choices}. 이미 다른 모델이 선택한 역할은 고르지 마. "
        "답변 마지막 줄에는 반드시 `ROLE_SELECT: 역할_ID` 형식만 적어. "
        "이 값은 대시보드 역할 선택지와 Roles.md에 자동 저장된다."
    )


def extract_role_selection(text: str) -> str:
    match = ROLE_SELECTION_RE.search(text or "")
    if match and match.group(1) in ROLE_CATALOG:
        return match.group(1)
    return ""


def strip_role_selection(text: str) -> str:
    return ROLE_SELECTION_RE.sub("", text or "").strip()


def choose_discussion_role(agent: str, requested_role: str = "") -> str:
    with STATE_LOCK:
        roles = normalize_agent_roles(STATE.get("agent_roles"))
        used = {role for owner, role in roles.items() if owner != agent and role}
        candidates = [requested_role] if requested_role in ROLE_CATALOG else []
        candidates.extend(ROLE_PREFERENCES.get(agent, []))
        candidates.extend(ROLE_CATALOG)
        selected = next((role for role in candidates if role and role not in used), "")
        previous = roles.get(agent, "")
        if not selected or selected == previous:
            return selected
        roles[agent] = selected
        STATE["agent_roles"] = roles
        STATE.setdefault("role_history", []).append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "agent": agent,
            "from": previous,
            "to": selected,
            "source": "discussion_auto",
        })
        del STATE["role_history"][:-100]
        save_state(STATE)
        snapshot = dict(STATE)
    write_session_roles(snapshot)
    add_runtime_event(
        f"역할 토론 자동 선택: {AGENTS[agent]['label']}={role_label(selected)}"
        + ("" if requested_role == selected else " (남은 선택지로 자동 보정)")
    )
    return selected


def normalize_agent_settings(settings: dict | None) -> dict:
    source = settings if isinstance(settings, dict) else {}
    normalized = {}
    for agent in AGENT_ORDER:
        catalog = MODEL_CATALOG[agent]
        raw = source.get(agent, {}) if isinstance(source.get(agent, {}), dict) else {}
        valid_models = {value for value, _label in catalog["models"]}
        valid_efforts = {value for value, _label in catalog["efforts"]}
        model = raw.get("model", "")
        effort = raw.get("effort", "")
        normalized[agent] = {
            "model": model if model in valid_models else "",
            "effort": effort if effort in valid_efforts else "",
        }
    return normalized


def agent_setting_label(agent: str, settings: dict | None) -> str:
    setting = normalize_agent_settings(settings).get(agent, {})
    catalog = MODEL_CATALOG[agent]
    model_labels = dict(catalog["models"])
    effort_labels = dict(catalog["efforts"])
    model_label = model_labels.get(setting.get("model", ""), "CLI 기본값")
    effort = setting.get("effort", "")
    if effort and catalog["efforts"]:
        return f"{model_label} · {effort_labels.get(effort, effort)}"
    return model_label


def selected_agent_setting(agent: str) -> dict:
    state = globals().get("STATE", {})
    return normalize_agent_settings(state.get("agent_settings", {}))[agent]


def normalize_discussion_project_access(value: str | None) -> str:
    return value if value in {"none", "read", "write"} else "read"


def discussion_project_access_label(value: str | None) -> str:
    return {
        "write": "프로젝트 읽기·쓰기",
        "read": "프로젝트 읽기",
        "none": "읽지 않고 토론",
    }[normalize_discussion_project_access(value)]


def turn_project_access(cli_mode: str, state: dict | None = None) -> str:
    current = state if isinstance(state, dict) else globals().get("STATE", {})
    if cli_mode == "coding":
        return "write"
    if current.get("mode") in {"coding", "continuous"}:
        return "read"
    return normalize_discussion_project_access(current.get("discussion_project_access"))


def cli_working_directory(cli_mode: str) -> Path:
    if turn_project_access(cli_mode) != "none":
        return load_project_path()
    NO_PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    return NO_PROJECT_DIR


def normalize_enabled_agents(enabled_agents: list[str] | None) -> list[str]:
    enabled = [a for a in (enabled_agents or AGENT_ORDER) if a in AGENT_ORDER]
    return enabled or list(AGENT_ORDER)


def reporter_for(enabled_agents: list[str]) -> str:
    enabled = normalize_enabled_agents(enabled_agents)
    for candidate in ("claude", "antigravity", "codex"):
        if candidate in enabled:
            return candidate
    return "claude"


def make_report_step(enabled_agents: list[str], coding: bool = False) -> tuple[str, str, str, str]:
    reporter = reporter_for(enabled_agents)
    if coding:
        instruction = (
            "지금까지 활성화된 에이전트들이 실제로 반영한 코드 변경사항을 정리해서, "
            "팀장(사용자)에게 무엇이 어떻게 바뀌었는지, 비활성화된 에이전트가 누구였는지, "
            "다음에 뭘 하면 좋을지 실행 가능한 결론으로 요약해줘."
        )
    else:
        instruction = (
            "지금까지 활성화된 에이전트들이 나눈 강점 이야기와 역할 선언을 정리해서, "
            "팀장(사용자)에게 바로 보고해. 누가 어떤 역할을 맡았는지, 비활성화된 "
            "에이전트가 누구였는지, 그리고 다음에 무엇부터 시작하면 좋을지 3~6문장으로 요약해줘."
        )
    return (reporter, "최종 보고", instruction, "discussion")


def make_confirm_step(enabled_agents: list[str]) -> tuple[str, str, str, str]:
    reporter = reporter_for(enabled_agents)
    return (
        reporter, CONFIRM_PHASE,
        "지금까지 활성화된 에이전트들의 제안을 참고해서, 각자의 개선/수정 계획 전체를 "
        "하나로 정리하고 팀장(사용자)에게 실제로 진행해도 될지 확인을 요청하는 메시지를 "
        "작성해. 비활성화된 에이전트가 누구인지도 명시해. 아직 실제로 파일을 수정하지 말고, "
        f"마지막 줄에 {APPROVAL_TOKEN}만 써라.",
        "discussion",
    )


def steps_for_mode(mode: str, enabled_agents: list[str] | None = None) -> list[tuple[str, str, str, str]]:
    enabled = normalize_enabled_agents(enabled_agents)
    if mode == "continuous":
        return continuous_coding_steps(enabled)
    discussion_steps = [
        (agent, phase, instruction + (role_choice_instruction() if phase in {"역할 선언", "역할 확정"} else ""), cli_mode)
        for agent, phase, instruction, cli_mode in DISCUSSION_STEPS
        if agent in enabled
    ]
    if mode == "coding":
        proposal_steps = [step for step in PROPOSAL_STEPS if step[0] in enabled and step[1] != CONFIRM_PHASE]
        coding_steps = [step for step in CODING_WORK_STEPS if step[0] in enabled]
        return discussion_steps + proposal_steps + [make_confirm_step(enabled)] + coding_steps + [make_report_step(enabled, coding=True)]
    return discussion_steps + [make_report_step(enabled)]


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


def decode_cli_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    encodings = ["utf-8", locale.getpreferredencoding(False), "cp949"]
    for encoding in dict.fromkeys(encodings):
        try:
            return value.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return value.decode("utf-8", errors="replace")


def snapshot_project_tree(root: Path) -> tuple[dict[str, tuple], bool]:
    """코딩 턴 전후 비교용 경량 파일 트리 스냅샷."""
    entries: dict[str, tuple] = {}
    skipped_dirs = {
        ".git", "node_modules", ".venv", "venv", "__pycache__",
        ".pytest_cache", ".ruff_cache", ".codex_pytest_tmp", ".agents",
        "roundtable_memory", "sessions",
    }
    if not root.is_dir():
        return entries, False
    truncated = False
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        relative_dir = current_path.relative_to(root)
        for name in dirs:
            rel = (relative_dir / name).as_posix() + "/"
            entries[rel] = ("dir",)
            if len(entries) >= PROJECT_SNAPSHOT_MAX_ENTRIES:
                truncated = True
                break
        if truncated:
            break
        dirs[:] = [name for name in dirs if name not in skipped_dirs]
        for name in files:
            path = current_path / name
            rel = (relative_dir / name).as_posix()
            try:
                stat = path.stat()
            except OSError:
                continue
            entries[rel] = ("file", stat.st_size, stat.st_mtime_ns)
            if len(entries) >= PROJECT_SNAPSHOT_MAX_ENTRIES:
                truncated = True
                break
        if truncated:
            break
    return entries, truncated


def compare_project_snapshots(before: dict[str, tuple], after: dict[str, tuple]) -> list[dict[str, str]]:
    changes = []
    for path in sorted(before.keys() | after.keys()):
        if path not in before:
            change = "생성"
        elif path not in after:
            change = "삭제"
        elif before[path] != after[path]:
            change = "수정"
        else:
            continue
        changes.append({"path": path, "change": change})
    return changes


def capture_project_texts(root: Path, max_total_bytes: int = 5_000_000) -> dict[str, str]:
    contents: dict[str, str] = {}
    total = 0
    skipped_dirs = {
        ".git", "node_modules", ".venv", "venv", "__pycache__",
        ".pytest_cache", ".ruff_cache", ".codex_pytest_tmp", ".agents",
        "roundtable_memory", "sessions",
    }
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in skipped_dirs]
        for name in files:
            path = Path(current) / name
            try:
                size = path.stat().st_size
                if size > 512_000 or total + size > max_total_bytes:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            contents[path.relative_to(root).as_posix()] = text
            total += size
    return contents


def build_turn_diff(root: Path, before: dict[str, str], changed_paths: list[dict]) -> str:
    chunks = []
    for item in changed_paths[:50]:
        relative = item["path"].rstrip("/")
        if not relative or item["path"].endswith("/"):
            continue
        path = root / relative
        old_text = before.get(relative, "")
        try:
            new_text = path.read_text(encoding="utf-8") if path.exists() else ""
        except (OSError, UnicodeDecodeError):
            continue
        diff = difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile=f"a/{relative}", tofile=f"b/{relative}", lineterm="",
        )
        chunk = "\n".join(diff)
        if chunk:
            chunks.append(chunk)
    combined = "\n\n".join(chunks)
    return (combined + "\n")[:100_000] if combined else ""


def save_turn_checkpoint(session_id: str, step_index: int, agent: str, turn_diff: str) -> str:
    if not turn_diff:
        return ""
    directory = session_memory_dir(session_id) / "checkpoints"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{step_index:03d}_{agent}.patch"
    path.write_text(turn_diff, encoding="utf-8")
    return str(path)


def detect_validation_commands(root: Path) -> list[tuple[str, list[str]]]:
    commands: list[tuple[str, list[str]]] = []
    package_json = root / "package.json"
    if package_json.exists():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        npm = shutil.which("npm.cmd") or shutil.which("npm") or "npm"
        if scripts.get("test") and "no test specified" not in scripts["test"]:
            commands.append(("npm test", [npm, "test", "--", "--runInBand"]))
        if scripts.get("build"):
            commands.append(("npm run build", [npm, "run", "build"]))
    if (root / "tests").is_dir() and any((root / "tests").glob("test*.py")):
        if importlib.util.find_spec("pytest") is not None:
            commands.append(("Python pytest", [sys.executable, "-m", "pytest", "-q"]))
        else:
            commands.append(("Python unittest", [sys.executable, "-m", "unittest", "discover", "-s", "tests"]))
    return commands[:2]


def run_project_validation(root: Path) -> list[dict]:
    results = []
    for label, command in detect_validation_commands(root):
        started = time.time()
        try:
            result = subprocess.run(command, cwd=root, capture_output=True, timeout=180)
            output = decode_cli_output(result.stdout or result.stderr).strip()
            results.append({
                "label": label,
                "ok": result.returncode == 0,
                "returncode": result.returncode,
                "elapsed": round(time.time() - started, 1),
                "output": output[-2000:],
            })
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append({"label": label, "ok": False, "returncode": -1, "elapsed": round(time.time() - started, 1), "output": str(exc)})
    return results


def parse_cli_stream_event(tool_name: str, line: str) -> tuple[dict | None, str | None]:
    """CLI JSONL 한 줄을 대시보드용 진행 이벤트와 최종 답변으로 변환한다."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        if tool_name == "Antigravity" and line.strip():
            return {"kind": "log", "text": line.strip()[:240]}, None
        return None, None

    if tool_name == "Codex":
        event_type = data.get("type", "")
        item = data.get("item") or {}
        item_type = item.get("type", "")
        if event_type == "thread.started":
            return {"kind": "status", "text": "Codex 세션 시작"}, None
        if event_type == "turn.started":
            return {"kind": "thinking", "text": "요청 분석 및 작업 계획 수립 중"}, None
        if item_type == "reasoning":
            return {"kind": "thinking", "text": "추론 중"}, None
        if item_type == "command_execution":
            command = item.get("command") or item.get("text") or "명령 실행"
            status = item.get("status", "started")
            return {"kind": "command", "text": f"{status}: {command}"[:300]}, None
        if item_type == "file_change":
            changes = item.get("changes") or []
            paths = [change.get("path", "") for change in changes if isinstance(change, dict)]
            return {"kind": "file", "text": "파일 변경", "paths": paths[:20]}, None
        if item_type in {"mcp_tool_call", "web_search"}:
            name = item.get("server") or item.get("name") or item_type
            return {"kind": "tool", "text": f"도구 실행: {name}"}, None
        if item_type == "agent_message":
            text = item.get("text", "")
            return {"kind": "message", "text": "최종 답변 작성 완료"}, text or None
        if event_type == "turn.completed" and data.get("usage"):
            return {"kind": "usage", "text": "사용량 집계", "usage": data["usage"]}, None

    if tool_name == "Claude Code":
        event_type = data.get("type", "")
        if event_type == "system" and data.get("subtype") == "init":
            return {"kind": "status", "text": "Claude 세션 및 도구 초기화"}, None
        if event_type == "assistant":
            message = data.get("message") or {}
            usage = message.get("usage")
            for block in message.get("content") or []:
                block_type = block.get("type")
                if block_type == "tool_use":
                    name = block.get("name", "도구")
                    tool_input = block.get("input") or {}
                    path = tool_input.get("file_path") or tool_input.get("path")
                    detail = f": {path}" if path else ""
                    kind = "file" if name in {"Edit", "Write", "NotebookEdit"} else "tool"
                    event = {"kind": kind, "text": f"{name} 실행{detail}"[:300], "usage": usage}
                    if path:
                        event["paths"] = [path]
                    return event, None
                if block_type == "thinking":
                    return {"kind": "thinking", "text": "추론 중", "usage": usage}, None
            if usage:
                return {"kind": "usage", "text": "Claude 사용량 갱신", "usage": usage}, None
        if event_type == "user":
            return {"kind": "tool", "text": "도구 실행 결과 확인"}, None
        if event_type == "result":
            usage = data.get("usage") or {}
            event = {
                "kind": "usage",
                "text": "Claude 응답 및 사용량 집계 완료",
                "usage": usage,
                "cost_usd": data.get("total_cost_usd"),
            }
            return event, data.get("result") or None
    return None, None


def run_cli(
    tool_name: str,
    command: str,
    args: list[str],
    *,
    timeout: int | None = None,
    cwd: Path | None = None,
    input_text: str | None = None,
    stream_events: bool = False,
    event_callback=None,
):
    cmd = resolve_command(command) + args
    try:
        if stream_events:
            process = subprocess.Popen(
                cmd,
                cwd=cwd or ROOT,
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with ACTIVE_PROCESS_LOCK:
                ACTIVE_PROCESSES[tool_name] = process
            if input_text is not None and process.stdin is not None:
                process.stdin.write(input_text.encode("utf-8"))
                process.stdin.close()

            output_queue: queue.Queue = queue.Queue()

            def read_stream(name: str, stream) -> None:
                for raw_line in iter(stream.readline, b""):
                    output_queue.put((name, raw_line))
                output_queue.put((name, None))

            threads = [
                threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
                threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
            ]
            for thread in threads:
                thread.start()

            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            final_messages: list[str] = []
            closed_streams = 0
            deadline = time.monotonic() + timeout if timeout else None
            while closed_streams < 2:
                if deadline and time.monotonic() >= deadline:
                    terminate_process_tree(process)
                    raise subprocess.TimeoutExpired(cmd, timeout)
                try:
                    stream_name, raw_line = output_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if raw_line is None:
                    closed_streams += 1
                    continue
                line = decode_cli_output(raw_line).rstrip("\r\n")
                target = stdout_lines if stream_name == "stdout" else stderr_lines
                target.append(line)
                if stream_name == "stdout":
                    event, final_text = parse_cli_stream_event(tool_name, line)
                    if event and event_callback:
                        event_callback(event)
                    if final_text:
                        final_messages.append(final_text)
            returncode = process.wait()
            with ACTIVE_PROCESS_LOCK:
                if ACTIVE_PROCESSES.get(tool_name) is process:
                    ACTIVE_PROCESSES.pop(tool_name, None)
            stdout = "\n".join(final_messages) if final_messages else "\n".join(stdout_lines)
            return subprocess.CompletedProcess(cmd, returncode, stdout, "\n".join(stderr_lines))

        result = subprocess.run(
            cmd,
            cwd=cwd or ROOT,
            capture_output=True,
            input=input_text.encode("utf-8") if input_text is not None else None,
            timeout=timeout,
        )
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            decode_cli_output(result.stdout),
            decode_cli_output(result.stderr),
        )
    except FileNotFoundError as exc:
        print_cli_hint(tool_name, command, f"명령을 찾을 수 없음 ({exc})")
    except PermissionError as exc:
        print_cli_hint(tool_name, command, f"권한 거부 ({exc})")
    except subprocess.TimeoutExpired:
        with ACTIVE_PROCESS_LOCK:
            active = ACTIVE_PROCESSES.get(tool_name)
            if active is not None and active.poll() is not None:
                ACTIVE_PROCESSES.pop(tool_name, None)
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


def _finalize_cli_result(tool_name: str, result) -> str:
    """subprocess 결과를 응답 문자열로 정리한다. 실패/빈 응답을 구분해서 원인을 남긴다."""
    if result is None:
        return f"({tool_name} 응답 없음 — 실행 실패: 프로세스를 실행하지 못함)"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return f"({tool_name} 응답 없음 — 종료 코드 {result.returncode}: {detail[:300]})"
    text = result.stdout.strip()
    if not text:
        stderr = (result.stderr or "").strip()
        return f"({tool_name}가 빈 응답을 반환함" + (f" — stderr: {stderr[:300]}" if stderr else "") + ")"
    return text


_INCOMPLETE_TOOL_RESPONSE_RE = re.compile(
    r"(?:\*\*)?Tool:\s*[\w.-]+(?:\*\*)?\s*$|<tool(?:_use)?[\s>]",
    re.IGNORECASE,
)


def is_incomplete_tool_response(text: str) -> bool:
    return bool(_INCOMPLETE_TOOL_RESPONSE_RE.search(text.strip()))


def should_honor_approval_request(
    requested: bool, session_mode: str, phase: str
) -> bool:
    if not requested or session_mode == "continuous":
        return False
    if session_mode == "coding" and (
        phase in {"개선안 제안", "최종 보고"} or phase.startswith("호출 답변")
    ):
        return False
    return True


def allow_agent_calls(session_mode: str, phase: str) -> bool:
    return not (session_mode == "coding" and phase == "작업 수행")


def ask_codex(prompt: str, mode: str = "discussion", event_callback=None) -> str:
    project_access = turn_project_access(mode)
    sandbox = "workspace-write" if project_access == "write" else "read-only"
    working_directory = cli_working_directory(mode)
    setting = selected_agent_setting("codex")
    args = ["exec", "--ephemeral"]
    # Codex CLI 0.144+ treats --ignore-user-config as read-only in managed desktop
    # sessions even when --sandbox workspace-write is supplied.
    if project_access != "write":
        args.append("--ignore-user-config")
    if mode == "discussion":
        args.append("--ignore-rules")
    if setting["model"]:
        args.extend(["--model", setting["model"]])
    if setting["effort"]:
        args.extend(["--config", f'model_reasoning_effort="{setting["effort"]}"'])
    args.extend([
        "--sandbox", sandbox,
        "--cd", str(working_directory),
        "--skip-git-repo-check", "--json", "-",
    ])
    result = run_cli(
        "Codex",
        CODEX_CMD,
        args,
        timeout=CODEX_TIMEOUT,
        cwd=working_directory,
        input_text=prompt,
        stream_events=True,
        event_callback=event_callback,
    )
    return _finalize_cli_result("Codex", result)


def ask_claude(prompt: str, mode: str = "discussion", event_callback=None) -> str:
    project_access = turn_project_access(mode)
    permission_mode = "acceptEdits" if project_access == "write" else "dontAsk"
    setting = selected_agent_setting("claude")
    args = ["--print", "--safe-mode", "--no-session-persistence"]
    if setting["model"]:
        args.extend(["--model", setting["model"]])
    if setting["effort"]:
        args.extend(["--effort", setting["effort"]])
    state = globals().get("STATE", {})
    budget = state.get("budget") or {}
    cost_limit = float(budget.get("cost_limit_usd", 0.0) or 0.0)
    if cost_limit:
        remaining = max(0.01, cost_limit - float(state.get("total_actual_cost_usd", 0.0) or 0.0))
        args.extend(["--max-budget-usd", f"{remaining:.4f}"])
    if project_access != "write":
        tools = "Read,Glob,Grep" if project_access == "read" else ""
        args.extend(["--tools", tools])
        access_instruction = (
            "Inspect only the relevant project files with Read, Glob, and Grep before answering. "
            if project_access == "read" else
            "Do not inspect files or call tools. Use only the conversation context in the prompt. "
        )
        args.extend([
            "--system-prompt",
            "You are the Claude participant in a controlled roundtable. " + access_instruction +
            "Do not enter plan mode, mention plan files, or request ExitPlanMode. "
            "Return only a complete final answer in Korean, with no Tool: markers.",
        ])
    else:
        args.extend(["--tools", "Bash,Edit,Read,Write,Glob,Grep"])
    args.extend(["--permission-mode", permission_mode, "--verbose", "--output-format", "stream-json", prompt])
    result = run_cli(
        "Claude Code",
        CLAUDE_CMD,
        args,
        timeout=CLAUDE_TIMEOUT,
        cwd=cli_working_directory(mode),
        stream_events=True,
        event_callback=event_callback,
    )
    text = _finalize_cli_result("Claude Code", result)
    if project_access != "write" and is_incomplete_tool_response(text):
        if event_callback:
            event_callback({"kind": "status", "text": "불완전한 도구 요청 응답 감지 · 자동 교정 재시도"})
        retry_args = list(args)
        retry_args[-1] = (
            "이전 응답이 Tool 요청에서 중단되었다. 도구를 절대 호출하거나 언급하지 말고, "
            "이미 제공된 프롬프트 정보만으로 완결된 최종 답변을 한국어로 작성해라.\n\n" + prompt
        )
        retry_result = run_cli(
            "Claude Code", CLAUDE_CMD, retry_args, timeout=CLAUDE_TIMEOUT,
            cwd=cli_working_directory(mode), stream_events=True, event_callback=event_callback,
        )
        text = _finalize_cli_result("Claude Code", retry_result)
    return text


def ask_antigravity(prompt: str, mode: str = "discussion", event_callback=None) -> str:
    # agy는 Go 스타일 플래그라 --print 자체가 프롬프트 값을 먹는다. 반드시 마지막에 와야 함.
    # subprocess의 cwd만으로는 agy가 프로젝트 폴더를 인식하지 못하고 자기 내부의 기본
    # scratch 워크스페이스를 본다 — --add-dir로 명시적으로 작업 폴더를 알려줘야 한다.
    project_access = turn_project_access(mode)
    project_path = cli_working_directory(mode)
    base_args = ["--add-dir", str(project_path)] if project_access != "none" else []
    setting = selected_agent_setting("antigravity")
    if setting["model"]:
        base_args.extend(["--model", setting["model"]])
    if project_access == "write":
        args = base_args + [
            "--mode", "accept-edits",
            "--dangerously-skip-permissions",
            "--print", prompt,
        ]
    else:
        args = base_args + [
            "--mode", "plan",
            "--sandbox",
            "--print", prompt,
        ]
    result = run_cli(
        "Antigravity", AGY_CMD, args, timeout=AGY_TIMEOUT, cwd=project_path,
        stream_events=True, event_callback=event_callback,
    )
    return _finalize_cli_result("Antigravity", result)


ASK_FUNCS = {
    "codex": ask_codex,
    "claude": ask_claude,
    "antigravity": ask_antigravity,
}


# ──────────────────────────────────────────
# 상태 / 로그 / 프로필
# ──────────────────────────────────────────

def new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def new_state() -> dict:
    return {
        "id": new_session_id(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "name": "새 세션",
        "tags": [],
        "favorite": False,
        "archived": False,
        "messages": [],
        "step_index": 0,
        "finished": False,
        "topic": "",
        "mode": "discussion",
        "discussion_project_access": "read",
        "enabled_agents": list(AGENT_ORDER),
        "agent_settings": normalize_agent_settings(None),
        "agent_roles": normalize_agent_roles(None),
        "role_history": [],
        "roles_announced_signature": "",
        "workspace_path": str(load_project_path()),
        "workspace_access": "write",
        "total_est_tokens": 0,
        "total_actual_tokens": 0,
        "total_actual_cost_usd": 0.0,
        "budget": {"token_limit": 0, "cost_limit_usd": 0.0},
        "total_elapsed_time": 0.0,
        "active_agent": None,
        "active_phase": None,
        "active_started_at": None,
        "active_cli_mode": None,
        "active_prompt_chars": 0,
        "active_work_log": [],
        "active_usage": {},
        "runtime_events": [],
        "validation_results": [],
        "delegation_count": 0,
        "delegation_history": [],
        "pending_interventions": [],
        "continuous_stopped": False,
    }


def normalize_state(state: dict) -> dict:
    state.setdefault("id", new_session_id())
    state.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    state.setdefault("name", state.get("topic") or "새 세션")
    state.setdefault("tags", [])
    state.setdefault("favorite", False)
    state.setdefault("archived", False)
    state["tags"] = [str(tag).strip()[:24] for tag in state["tags"] if str(tag).strip()][:8]
    state.setdefault("messages", [])
    state["messages"] = [
        message for message in state["messages"]
        if (message.get("meta") or {}).get("failure_kind") != "role_unassigned"
    ]
    state.setdefault("step_index", 0)
    state.setdefault("finished", False)
    state.setdefault("topic", "")
    state.setdefault("mode", "discussion")
    state["discussion_project_access"] = normalize_discussion_project_access(
        state.get("discussion_project_access")
    )
    state.setdefault("workspace_path", str(load_project_path()))
    state.setdefault("workspace_access", "write")
    state["workspace_access"] = "write" if state.get("workspace_access") == "write" else "read"
    if state.get("mode") in {"coding", "continuous"}:
        state["discussion_project_access"] = "write"
        state["workspace_access"] = "write"
    state.setdefault("total_est_tokens", 0)
    state.setdefault("total_actual_tokens", 0)
    state.setdefault("total_actual_cost_usd", 0.0)
    state.setdefault("budget", {"token_limit": 0, "cost_limit_usd": 0.0})
    budget = state["budget"] if isinstance(state["budget"], dict) else {}
    state["budget"] = {
        "token_limit": max(0, int(budget.get("token_limit", 0) or 0)),
        "cost_limit_usd": max(0.0, float(budget.get("cost_limit_usd", 0.0) or 0.0)),
    }
    state.setdefault("total_elapsed_time", 0.0)
    state.setdefault("active_agent", None)
    state.setdefault("active_phase", None)
    state.setdefault("active_started_at", None)
    state.setdefault("active_cli_mode", None)
    state.setdefault("active_prompt_chars", 0)
    state.setdefault("active_work_log", [])
    state.setdefault("active_usage", {})
    state.setdefault("runtime_events", [])
    state.setdefault("validation_results", [])
    state.setdefault("delegation_count", 0)
    state.setdefault("delegation_history", [])
    state.setdefault("pending_interventions", [])
    state["continuous_stopped"] = bool(state.get("continuous_stopped", False))
    state["enabled_agents"] = normalize_enabled_agents(state.get("enabled_agents"))
    state["agent_settings"] = normalize_agent_settings(state.get("agent_settings"))
    state["agent_roles"] = normalize_agent_roles(state.get("agent_roles"))
    state.setdefault("role_history", [])
    if not isinstance(state["role_history"], list):
        state["role_history"] = []
    state.setdefault("roles_announced_signature", "")
    return state


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            state = normalize_state(json.loads(STATE_PATH.read_text(encoding="utf-8")))
            # 프로세스가 새로 시작됐다면 이전 프로세스의 실행 중 표시는 더 이상 유효하지 않다.
            state["active_agent"] = None
            state["active_phase"] = None
            state["active_started_at"] = None
            state["active_cli_mode"] = None
            state["active_prompt_chars"] = 0
            state["active_work_log"] = []
            state["active_usage"] = {}
            return state
        except json.JSONDecodeError:
            pass
    return new_state()


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    SESSIONS_DIR.mkdir(exist_ok=True)
    session_id = state.get("id")
    if session_id:
        (SESSIONS_DIR / f"{session_id}.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def list_sessions() -> list[dict]:
    SESSIONS_DIR.mkdir(exist_ok=True)
    summaries = []
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        summaries.append({
            "id": data.get("id", path.stem),
            "topic": data.get("topic", ""),
            "name": data.get("name") or data.get("topic") or "새 세션",
            "tags": data.get("tags", []),
            "favorite": bool(data.get("favorite", False)),
            "archived": bool(data.get("archived", False)),
            "mode": data.get("mode", "discussion"),
            "discussion_project_access": normalize_discussion_project_access(
                data.get("discussion_project_access")
            ),
            "enabled_agents": normalize_enabled_agents(data.get("enabled_agents")),
            "agent_settings": normalize_agent_settings(data.get("agent_settings")),
            "finished": data.get("finished", False),
            "created_at": data.get("created_at", ""),
            "message_count": len(data.get("messages", [])),
        })
    summaries.sort(key=lambda s: s["created_at"], reverse=True)
    return summaries


def load_session(session_id: str) -> dict | None:
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def append_log(agent: str, phase: str, text: str) -> None:
    label = AGENTS[agent]["label"]
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"\n### [{ts()}] {label} — {phase}\n\n{text}\n")


def start_log_session() -> None:
    header = f"\n---\n\n## 세션 시작 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(header)


def session_transcript_path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.md"


def session_memory_dir(session_id: str) -> Path:
    return MEMORY_DIR / session_id


def session_memory_paths(session_id: str) -> tuple[Path, Path]:
    memory_dir = session_memory_dir(session_id)
    return memory_dir / "full.md", memory_dir / "brief.md"


def session_profile_path(session_id: str) -> Path:
    return session_memory_dir(session_id) / "Profile.md"


def session_roles_path(session_id: str) -> Path:
    return session_memory_dir(session_id) / "Roles.md"


def write_session_roles(state: dict) -> None:
    session_id = state.get("id", "")
    if not session_id:
        return
    memory_dir = session_memory_dir(session_id)
    memory_dir.mkdir(parents=True, exist_ok=True)
    roles = normalize_agent_roles(state.get("agent_roles"))
    enabled = set(normalize_enabled_agents(state.get("enabled_agents")))
    lines = [
        f"# 세션 역할 - {state.get('name') or state.get('topic') or session_id}",
        "",
        f"- 세션 ID: {session_id}",
        f"- 전역 정책: {ROLE_POLICY_PATH}",
        "",
        "## 현재 배정",
        "",
        "| 모델 | 상태 | 역할 | 담당 범위 |",
        "| --- | --- | --- | --- |",
    ]
    for agent in AGENT_ORDER:
        role = ROLE_CATALOG.get(roles[agent], {})
        lines.append(
            f"| {AGENTS[agent]['label']} | {'활성' if agent in enabled else '꺼짐'} | "
            f"{role.get('label', '미지정')} | {role.get('scope', '-')} |"
        )
    lines.extend(["", "## 변경 이력", ""])
    history = state.get("role_history", [])[-100:]
    if history:
        for entry in history:
            lines.append(
                f"- [{entry.get('time', '')}] {AGENTS.get(entry.get('agent'), {}).get('label', entry.get('agent', ''))}: "
                f"{role_label(entry.get('from', ''))} -> {role_label(entry.get('to', ''))}"
            )
    else:
        lines.append("- 아직 사용자가 확정한 역할이 없습니다.")
    session_roles_path(session_id).write_text("\n".join(lines) + "\n", encoding="utf-8")


def session_agent_profile_dir(session_id: str) -> Path:
    return session_memory_dir(session_id) / "profiles"


def session_agent_profile_path(session_id: str, agent: str) -> Path:
    filename = f"{agent.upper()}_Profile.md"
    return session_agent_profile_dir(session_id) / filename


def agent_names(agent_keys: list[str]) -> str:
    return ", ".join(AGENTS[a]["label"] for a in agent_keys)


def write_session_transcript_header(state: dict) -> None:
    """세션별 전체 기록 .md — 매 턴 프롬프트에 재전송하는 대신 여기 따로 보관한다."""
    SESSIONS_DIR.mkdir(exist_ok=True)
    path = session_transcript_path(state["id"])
    if path.exists():
        return
    mode_label = MODE_LABELS.get(state.get("mode", "discussion"), state.get("mode", ""))
    discussion_access_label = discussion_project_access_label(state.get("discussion_project_access"))
    path.write_text(
        f"# 세션 기록 — {state.get('topic', '')}\n\n"
        f"- 세션 ID: {state['id']}\n"
        f"- 모드: {mode_label}\n"
        f"- 일반 토론 프로젝트 접근: {discussion_access_label}\n"
        f"- 시작: {state.get('created_at', '')}\n",
        encoding="utf-8",
    )


def ensure_session_memory(state: dict) -> None:
    session_id = state["id"]
    memory_dir = session_memory_dir(session_id)
    memory_dir.mkdir(parents=True, exist_ok=True)
    full_path, brief_path = session_memory_paths(session_id)
    profile_dir = session_agent_profile_dir(session_id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    mode_label = MODE_LABELS.get(state.get("mode", "discussion"), state.get("mode", ""))
    discussion_access_label = discussion_project_access_label(state.get("discussion_project_access"))
    enabled = normalize_enabled_agents(state.get("enabled_agents"))
    disabled = [a for a in AGENT_ORDER if a not in enabled]
    header = (
        f"# Roundtable Memory — {state.get('topic', '')}\n\n"
        f"- 세션 ID: {session_id}\n"
        f"- 모드: {mode_label}\n"
        f"- 일반 토론 프로젝트 접근: {discussion_access_label}\n"
        f"- 활성 에이전트: {agent_names(enabled)}\n"
        f"- 비활성 에이전트: {agent_names(disabled) if disabled else '없음'}\n"
        f"- 시작: {state.get('created_at', '')}\n"
    )
    if not full_path.exists():
        full_path.write_text(header, encoding="utf-8")
    if not brief_path.exists():
        brief_path.write_text(header + "\n## 압축 요약\n", encoding="utf-8")
    profile_path = session_profile_path(session_id)
    roles_path = session_roles_path(session_id)
    if not profile_path.exists():
        profile_path.write_text(SESSION_PROFILE_TEMPLATE, encoding="utf-8")
    write_session_roles(state)
    for agent in AGENT_ORDER:
        path = session_agent_profile_path(session_id, agent)
        if not path.exists():
            path.write_text(AGENT_PROFILE_TEMPLATES[agent], encoding="utf-8")


def append_memory(session_id: str, agent: str, phase: str, text: str, meta: dict | None = None) -> None:
    with STATE_LOCK:
        state_snapshot = dict(STATE)
    ensure_session_memory(state_snapshot)
    full_path, brief_path = session_memory_paths(session_id)
    label = AGENTS[agent]["label"]
    stats = ""
    if meta:
        stats = f" _(⏱ {meta['elapsed']}초 · 추정 토큰 ~{meta['est_tokens']} · {meta['cli_mode']})_"
    with full_path.open("a", encoding="utf-8") as f:
        f.write(f"\n## [{ts()}] {label} — {phase}{stats}\n\n{text}\n")
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) > 360:
        compact = compact[:357] + "..."
    with brief_path.open("a", encoding="utf-8") as f:
        f.write(f"\n- [{ts()}] **{label} / {phase}**: {compact}\n")


def should_record_role_profile(phase: str) -> bool:
    role_keywords = ("강점", "역할", "확인 요청", "개선안", "최종 보고", "개입 답변")
    return any(keyword in phase for keyword in role_keywords)


def append_session_role_profile(session_id: str, agent: str, phase: str, text: str) -> None:
    if agent not in AGENT_ORDER or not should_record_role_profile(phase):
        return
    with STATE_LOCK:
        state_snapshot = dict(STATE)
    ensure_session_memory(state_snapshot)
    label = AGENTS[agent]["label"]
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) > 900:
        compact = compact[:897] + "..."
    entry = f"\n### [{ts()}] {label} — {phase}\n\n{compact}\n"
    with session_profile_path(session_id).open("a", encoding="utf-8") as f:
        f.write(entry)
    with session_agent_profile_path(session_id, agent).open("a", encoding="utf-8") as f:
        f.write(entry)


def read_profile_tail(path: Path, max_lines: int = 28) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def append_session_transcript(session_id: str, agent: str, phase: str, text: str, meta: dict | None = None) -> None:
    label = AGENTS[agent]["label"]
    stats = ""
    if meta:
        stats = f" _(⏱ {meta['elapsed']}초 · 추정 토큰 ~{meta['est_tokens']} · {meta['cli_mode']})_"
    path = session_transcript_path(session_id)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n## [{ts()}] {label} — {phase}{stats}\n\n{text}\n")


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


def build_transcript(messages: list[dict], window: int | None = None) -> str:
    """
    프롬프트에 실어 보낼 최근 대화 기록을 만든다. 전체 기록은 memory full.md에 저장하고,
    여기서는 최근 window개만 넣는다.
    """
    if not messages:
        return ""
    windowed = messages[-window:] if window else messages
    omitted = len(messages) - len(windowed)
    lines = []
    if omitted > 0:
        lines.append(f"(이전 {omitted}개 메시지는 생략됨 — 최근 {len(windowed)}개만 표시)")
    for m in windowed:
        label = AGENTS[m["agent"]]["label"]
        message_text = m["text"]
        if len(message_text) > 2200:
            message_text = message_text[:1100] + "\n... (메시지 중간 생략) ...\n" + message_text[-1100:]
        lines.append(f"[{label}] ({m['phase']}): {message_text}")
    transcript = "\n\n".join(lines)
    if len(transcript) > TRANSCRIPT_MAX_CHARS:
        notice = "(최근 대화 일부 생략)\n\n"
        remaining = max(0, TRANSCRIPT_MAX_CHARS - len(notice))
        transcript = (notice + (transcript[-remaining:] if remaining else ""))[:TRANSCRIPT_MAX_CHARS]
    return transcript


def select_shared_messages(messages: list[dict], enabled_agents: list[str]) -> list[dict]:
    """각 활성 모델의 최신 발언과 사용자의 최신 개입을 한 번씩 고른다."""
    wanted = set(normalize_enabled_agents(enabled_agents)) | {"user"}
    selected: list[tuple[int, dict]] = []
    seen: set[str] = set()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        agent = message.get("agent")
        if agent not in wanted or agent in seen:
            continue
        selected.append((index, message))
        seen.add(agent)
        if seen == wanted:
            break
    selected.sort(key=lambda item: item[0])
    return [message for _index, message in selected]


def build_shared_transcript(messages: list[dict], enabled_agents: list[str]) -> tuple[str, list[dict]]:
    selected = select_shared_messages(messages, enabled_agents)
    compact = []
    for message in selected:
        text = message.get("text", "")
        if len(text) > 320:
            text = text[:155] + "\n... (발언 축약) ...\n" + text[-145:]
        compact.append({**message, "text": text})
    return build_transcript(compact), selected


def read_brief_tail(session_id: str, max_lines: int = MEMORY_BRIEF_LINES) -> str:
    _full_path, brief_path = session_memory_paths(session_id)
    if not brief_path.exists():
        return ""
    try:
        lines = brief_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    bullet_lines = [line for line in lines if line.startswith("- [")]
    return "\n".join(bullet_lines[-max_lines:])


def build_memory_context(state: dict, expose_file_paths: bool = True) -> str:
    session_id = state.get("id", "")
    if not session_id:
        return ""
    ensure_session_memory(state)
    full_path, brief_path = session_memory_paths(session_id)
    profile_path = session_profile_path(session_id)
    roles_path = session_roles_path(session_id)
    enabled = normalize_enabled_agents(state.get("enabled_agents"))
    disabled = [a for a in AGENT_ORDER if a not in enabled]
    brief_tail = read_brief_tail(session_id)
    if expose_file_paths:
        lines = [
            "세션 외부 메모리 (필요할 때만 파일을 직접 읽기):",
            f"- 전체 기록 파일: {full_path}",
            f"- 압축 요약 파일: {brief_path}",
            f"- 세션 역할 프로필: {profile_path}",
            f"- 사용자 확정 역할: {roles_path}",
            f"- 전역 역할 강제 규칙: {ROLE_POLICY_PATH}",
            f"- 활성 에이전트: {agent_names(enabled)}",
            f"- 비활성 에이전트: {agent_names(disabled) if disabled else '없음'}",
            "",
            "규칙:",
            "- 전체 기록과 Profile.md 전문은 현재 프롬프트에 포함되지 않았다.",
            "- 역할 결정이나 과거 근거가 꼭 필요할 때만 해당 파일을 직접 읽는다.",
            "- 역할은 모델이 스스로 바꾸지 않는다. 사용자가 대시보드에서 지정한 Roles.md의 현재 배정을 따른다.",
            "- 비활성 에이전트는 이번 세션에서 발언/작업하지 않는다는 점을 고려한다.",
        ]
    else:
        lines = [
            "대화 전용 세션 문맥:",
            f"- 활성 에이전트: {agent_names(enabled)}",
            f"- 비활성 에이전트: {agent_names(disabled) if disabled else '없음'}",
            "- 프로젝트 및 외부 메모리 파일 경로는 제공하지 않는다.",
            "- 아래 인라인 요약과 최근 대화만 사용한다.",
        ]
    if brief_tail:
        lines.extend(["", "짧은 세션 요약:", brief_tail])
    validation_results = state.get("validation_results", [])
    if validation_results:
        lines.extend(["", "최근 자동 검증:"])
        for result in validation_results[-6:]:
            status = "통과" if result.get("ok") else "실패"
            output = re.sub(r"\s+", " ", str(result.get("output", ""))).strip()[:180]
            detail = f" - {output}" if output else ""
            lines.append(f"- {result.get('label', '검증')}: {status}{detail}")
    context = "\n".join(lines)
    if len(context) > MEMORY_CONTEXT_MAX_CHARS:
        fixed = "\n".join(lines[:16])
        notice = "\n\n(이전 메모리 일부 생략)\n"
        remaining = max(0, MEMORY_CONTEXT_MAX_CHARS - len(fixed) - len(notice))
        tail = context[-remaining:] if remaining else ""
        context = (fixed + notice + tail)[:MEMORY_CONTEXT_MAX_CHARS]
    return context


def clip_agent_output(text: str, max_chars: int = OUTPUT_MAX_CHARS) -> tuple[str, bool]:
    text = text.strip()
    if len(text) <= max_chars:
        return text, False
    notice = "(앞부분 자동 축약)\n\n"
    remaining = max(0, max_chars - len(notice))
    return (notice + (text[-remaining:] if remaining else ""))[:max_chars], True


def extract_approval_token(text: str) -> tuple[str, bool]:
    lines = text.rstrip().splitlines()
    if lines and lines[-1].strip().upper() == APPROVAL_TOKEN:
        return "\n".join(lines[:-1]).rstrip(), True
    return text, False


def extract_agent_calls(text: str, source_agent: str, current_cli_mode: str) -> tuple[str, list[dict]]:
    calls = []
    kept_lines = []
    seen = set()
    for line in text.splitlines():
        match = _AGENT_CALL_RE.match(line)
        if not match:
            kept_lines.append(line)
            continue
        target, requested_mode, task = match.groups()
        target = target.lower()
        task = task.strip()[:600]
        if target == source_agent or not task or target in seen or len(calls) >= 2:
            continue
        mode = "coding" if requested_mode.lower() == "coding" and current_cli_mode == "coding" else "discussion"
        calls.append({"target": target, "mode": mode, "task": task})
        seen.add(target)
    return "\n".join(kept_lines).rstrip(), calls


def extract_target_prefix(text: str) -> tuple[str, str | None]:
    match = _TARGET_PREFIX_RE.match(text or "")
    if not match:
        return (text or "").strip(), None
    target = match.group(1).lower().replace(" ", "")
    if target == "claudecode":
        target = "claude"
    return match.group(2).strip(), target


def compose_agent_prompt(
    team_prompt: str,
    topic: str,
    memory_context: str,
    transcript: str,
    instruction: str,
    role_context: str = "",
    project_context: str = "",
) -> str:
    team_prompt = team_prompt[:TEAM_PROMPT_MAX_CHARS]
    topic = topic[:500]
    instruction = instruction[:1200]
    header = (
        "모든 사용자 노출 문장은 한국어로 쓴다. 파일명·명령어·코드 식별자만 원문을 유지한다.\n"
        "도구 실행 계획과 탐색 과정을 출력하지 말고 결론만 최대 6개 항목, 1,200자 이내로 답한다.\n"
        "다른 에이전트의 전문성이 꼭 필요하면 답변 마지막에 최대 2개까지 "
        "CALL_AGENT: codex|antigravity|claude | discussion|coding | 구체적인 요청 형식으로 쓴다. "
        "직접 해결할 수 있으면 호출하지 않는다. 자신은 호출하지 않는다.\n"
        f"사용자 승인이 꼭 필요하면 설명을 마친 뒤 마지막 줄에 {APPROVAL_TOKEN}만 쓴다. "
        f"승인이 필요하지 않으면 {APPROVAL_TOKEN}를 쓰지 않는다.\n\n"
    )
    policy_blocks = [block for block in (project_context, role_context) if block]
    policy_block = "\n\n" + "\n\n".join(policy_blocks) if policy_blocks else ""
    current_task = f"{policy_block}\n\n현재 주제: {topic}\n현재 작업: {instruction}\n"
    base = f"{header}{team_prompt}{current_task}"
    context_parts = []
    if memory_context:
        context_parts.append(memory_context)
    if transcript:
        context_parts.append(f"최근 대화:\n{transcript}")
    context = "\n\n".join(context_parts)
    available = max(0, PROMPT_MAX_CHARS - len(base) - 24)
    if len(context) > available:
        notice = "\n... 문맥 축약 ...\n"
        remaining = max(0, available - len(notice))
        head_size = remaining // 2
        tail_size = remaining - head_size
        context = context[:head_size] + notice + (context[-tail_size:] if tail_size else "")
    prompt = f"{header}{team_prompt}\n\n{context}{current_task}"
    return prompt[:PROMPT_MAX_CHARS]


# ──────────────────────────────────────────
# 공유 상태 (서버 스레드 ↔ 워커 스레드)
# ──────────────────────────────────────────

STATE_LOCK = threading.Lock()
STATE: dict = load_state()
_RESTORED_INTERVENTIONS = list(STATE.get("pending_interventions", []))
CONTROL = {
    "paused": False,
    "stopped": bool(STATE.get("mode") == "continuous" and STATE.get("continuous_stopped")),
    "worker_running": False,
    "worker_session_id": None,
    "worker_start_pending": False,
    "awaiting_approval": False,
    "approval_deferred": False,
    "approval_requested": False,
    "approval_requested_by": [],
    "approval_rejected": False,
    "approval_seen_messages": 0,
    "intervention_pending": bool(_RESTORED_INTERVENTIONS),
    "intervention_seen_messages": 0,
    "intervention_intent": _RESTORED_INTERVENTIONS[0].get("intent", "") if _RESTORED_INTERVENTIONS else "",
    "intervention_queue": _RESTORED_INTERVENTIONS,
}


def workspace_access_mode() -> str:
    with STATE_LOCK:
        return "write" if STATE.get("workspace_access") == "write" else "read"


def effective_cli_mode(requested_mode: str) -> str:
    if requested_mode == "coding" and workspace_access_mode() != "write":
        return "discussion"
    return "coding" if requested_mode == "coding" else "discussion"


def role_prompt(agent: str, state: dict) -> str:
    role_id = normalize_agent_roles(state.get("agent_roles")).get(agent, "")
    role = ROLE_CATALOG.get(role_id)
    if not role:
        return (
            "확정 역할: 미지정. 사용자가 아직 역할 강제를 요청하지 않은 상태다. "
            "현재 사용자 지시를 일반 작업자로서 수행하되, 다른 활성 모델과 변경 파일이 충돌하지 않도록 최근 대화를 확인한다."
        )
    return (
        f"확정 역할: {role['label']}\n"
        f"책임: {role['summary']}\n"
        f"수정 허용 범위: {role['scope']}\n"
        "강제 규칙: 사용자가 역할 선택 UI에서 바꾸기 전까지 이 역할을 유지한다. "
        "다른 역할의 작업을 대신 수행하거나 담당 밖 파일을 수정하지 않는다. "
        "범위 밖 작업은 담당 역할의 모델을 CALL_AGENT로 호출하고, 담당 모델이 없으면 사용자에게 배정 변경을 요청한다."
    )


def project_access_prompt(project_access: str) -> str:
    if project_access == "write":
        return (
            "프로젝트 접근 권한: 읽기·쓰기. 현재 작업이 구현이나 수정을 명시하면 관련 파일을 먼저 확인한 뒤 "
            "실제 구현과 검증까지 완료한다. 현재 작업이 토론·분석·계획만 요구하면 파일을 수정하지 않는다. "
            "승인된 작업 범위와 사용자 지시를 벗어나지 않는다."
        )
    if project_access == "read":
        return (
            "프로젝트 접근: 읽기 전용. 답변 전에 프로젝트 구조와 관련 파일을 필요한 만큼 직접 확인한다. "
            "파일을 생성·수정·삭제하지 않는다."
        )
    return (
        "프로젝트 접근: 사용 안 함. 프로젝트 폴더와 파일을 열거나 검색하지 않는다. "
        "현재 프롬프트에 포함된 사용자 메시지, 최근 대화와 인라인 요약만으로 토론한다. "
        "현재 작업 지시의 파일 확인 문구보다 이 규칙을 우선한다."
    )


def role_scope_violations(agent: str, changed_paths: list[dict], state: dict) -> list[str]:
    role_id = normalize_agent_roles(state.get("agent_roles")).get(agent, "")
    role = ROLE_CATALOG.get(role_id)
    if not role:
        return []
    if not role.get("can_write"):
        return [item.get("path", "") for item in changed_paths if item.get("path")]
    patterns = role.get("patterns", [])
    violations = []
    for item in changed_paths:
        path = str(item.get("path", "")).replace("\\", "/")
        top_level = path.rstrip("/").split("/", 1)[0]
        if top_level in {
            ".pytest_cache", ".ruff_cache", ".codex_pytest_tmp", ".agents",
            "__pycache__", "roundtable_memory", "sessions",
        }:
            continue
        if path in {"TODO.md", "AGENTS.md"}:
            continue
        if path and not any(fnmatch.fnmatch(path.lower(), pattern.lower()) for pattern in patterns):
            violations.append(path)
    return violations


def can_continue_after_failed_step(state: dict, messages: list[dict]) -> bool:
    if not messages or state.get("active_agent"):
        return False
    last = messages[-1]
    if (last.get("meta") or {}).get("failure_kind") not in {"role_scope", "cli"}:
        return False
    steps = steps_for_mode(
        state.get("mode", "discussion"),
        normalize_enabled_agents(state.get("enabled_agents")),
    )
    step_index = int(state.get("step_index", 0))
    if step_index >= len(steps):
        return False
    expected_agent, expected_phase, _instruction, _cli_mode = steps[step_index]
    actual_phase = str(last.get("phase", "")).split(" · 사이클 ", 1)[0]
    return last.get("agent") == expected_agent and actual_phase == expected_phase


def continue_after_failed_step() -> dict:
    with STATE_LOCK:
        if not can_continue_after_failed_step(STATE, STATE.get("messages", [])):
            return {"error": "다음 단계로 넘길 수 있는 실패 턴이 없습니다."}
        skipped = STATE["messages"][-1]
        STATE["step_index"] = int(STATE.get("step_index", 0)) + 1
        CONTROL["paused"] = False
        CONTROL["stopped"] = False
        save_state(STATE)
    add_runtime_event(
        f"{AGENTS[skipped['agent']]['label']} 실패 턴의 변경을 유지하고 다음 모델로 진행"
    )
    start_worker_if_needed(force=True)
    payload = state_json_payload()
    payload["success"] = True
    return payload


def update_agent_roles(roles: dict) -> dict:
    normalized = normalize_agent_roles(roles)
    selected = [role for role in normalized.values() if role]
    if len(selected) != len(set(selected)):
        return {"error": "세 모델에 같은 역할을 중복 배정할 수 없습니다."}
    with STATE_LOCK:
        if STATE.get("active_agent"):
            return {"error": "모델이 실행 중일 때는 역할을 변경할 수 없습니다."}
        previous = normalize_agent_roles(STATE.get("agent_roles"))
        changes = []
        for agent in AGENT_ORDER:
            if previous[agent] != normalized[agent]:
                changes.append({
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "agent": agent,
                    "from": previous[agent],
                    "to": normalized[agent],
                })
        STATE["agent_roles"] = normalized
        STATE.setdefault("role_history", []).extend(changes)
        del STATE["role_history"][:-100]
        save_state(STATE)
        snapshot = dict(STATE)
    write_session_roles(snapshot)
    if changes:
        summary = ", ".join(
            f"{AGENTS[item['agent']]['label']}={role_label(item['to'])}" for item in changes
        )
        add_runtime_event(f"사용자 역할 배정 변경: {summary}")
    announce_roles_if_complete()
    payload = state_json_payload()
    payload["success"] = True
    return payload


def update_discussion_project_access(access: str) -> dict:
    normalized = normalize_discussion_project_access(access)
    if normalized == "write" and not os.access(load_project_path(), os.W_OK):
        return {"error": "현재 프로젝트 폴더에 쓰기 권한이 없습니다."}
    with STATE_LOCK:
        if STATE.get("active_agent"):
            return {"error": "현재 모델 응답이 끝난 뒤 토론 접근 방식을 변경해주세요."}
        STATE["discussion_project_access"] = normalized
        if normalized == "write":
            STATE["workspace_access"] = "write"
        save_state(STATE)
    label = {
        "write": "프로젝트 읽기·쓰기",
        "read": "프로젝트 읽기",
        "none": "읽지 않고 토론",
    }[normalized]
    add_runtime_event(f"일반 토론 프로젝트 접근 변경: {label}")
    payload = state_json_payload()
    payload["success"] = True
    return payload


def update_workspace_selection(path_text: str, access: str) -> dict:
    candidate = Path(path_text).expanduser()
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return {"error": "선택한 작업 폴더를 찾을 수 없습니다."}
    if not candidate.is_dir():
        return {"error": "작업영역은 폴더만 선택할 수 있습니다."}
    if not os.access(candidate, os.R_OK):
        return {"error": "선택한 폴더에 읽기 권한이 없습니다."}
    with STATE_LOCK:
        coding_session = STATE.get("mode") in {"coding", "continuous"}
    access = "write" if coding_session or access == "write" else "read"
    if access == "write" and not os.access(candidate, os.W_OK):
        return {"error": "선택한 폴더에 쓰기 권한이 없습니다."}
    try:
        save_project_path(candidate)
    except OSError as exc:
        return {"error": f"PROJECT_PATH.txt 저장 실패: {exc}"}
    with STATE_LOCK:
        STATE["workspace_path"] = str(candidate)
        STATE["workspace_access"] = access
        save_state(STATE)
    access_label = "읽기·쓰기" if access == "write" else "읽기 전용"
    add_runtime_event(f"작업 폴더 변경: {candidate} ({access_label})")
    payload = state_json_payload()
    payload["success"] = True
    return payload


def unresolved_approval_requests(messages: list[dict]) -> list[dict]:
    requests: list[dict] = []
    for message in reversed(messages):
        if message.get("agent") == "user" and message.get("phase") in {"승인", "승인 거절"}:
            break
        if not message.get("meta", {}).get("approval_requested"):
            continue
        if message.get("phase") == "최종 보고":
            continue
        agent = message.get("agent", "")
        if agent not in AGENTS:
            continue
        label = AGENTS.get(agent, {}).get("label", agent or "에이전트")
        request = {
            "agent": agent,
            "phase": message.get("phase", "승인 요청"),
            "label": f"{label} · {message.get('phase', '승인 요청')}",
        }
        if request not in requests:
            requests.append(request)
    requests.reverse()
    return requests


def unresolved_approval_requesters(messages: list[dict]) -> list[str]:
    return [request["label"] for request in unresolved_approval_requests(messages)]


def orphaned_approval_followup_agents(messages: list[dict]) -> list[str]:
    approval_index = next(
        (
            index for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("agent") == "user" and messages[index].get("phase") == "승인"
        ),
        -1,
    )
    if approval_index < 0 or any(message.get("agent") in AGENTS for message in messages[approval_index + 1:]):
        return []
    prior_requests = unresolved_approval_requests(messages[:approval_index])
    return list(dict.fromkeys(
        request["agent"] for request in prior_requests if request["phase"] != CONFIRM_PHASE
    ))


_restored_approval_requesters = unresolved_approval_requesters(STATE.get("messages", []))
if _restored_approval_requesters and not STATE.get("finished", False):
    CONTROL["approval_requested"] = True
    CONTROL["approval_requested_by"] = _restored_approval_requesters
    CONTROL["awaiting_approval"] = True

_orphaned_approval_agents = orphaned_approval_followup_agents(STATE.get("messages", []))
if _orphaned_approval_agents and not any(
    item.get("approved_followup") for item in CONTROL["intervention_queue"]
):
    CONTROL["intervention_queue"].append({
        "intent": "execute",
        "targets": _orphaned_approval_agents,
        "cli_mode": "coding",
        "custom_instruction": "사용자가 직전 승인 요청을 승인했다. 승인받은 작업을 지금 실제로 수행하고 결과를 보고해라.",
        "approved_followup": True,
    })
    CONTROL["intervention_pending"] = True
    CONTROL["intervention_intent"] = "execute"
    STATE["pending_interventions"] = json.loads(json.dumps(CONTROL["intervention_queue"], ensure_ascii=False))
    STATE["finished"] = False
    save_state(STATE)


def add_runtime_event(text: str, level: str = "info") -> None:
    event = {
        "time": ts(),
        "level": level,
        "text": text,
    }
    print(f"  [{event['time']}] {text}")
    with STATE_LOCK:
        events = STATE.setdefault("runtime_events", [])
        events.append(event)
        del events[:-80]
        save_state(STATE)


def add_active_work_event(agent: str, event: dict) -> None:
    """실행 중인 CLI 이벤트를 짧게 정리해 상태에 보관한다."""
    with STATE_LOCK:
        if STATE.get("active_agent") != agent:
            return
        usage = event.get("usage")
        if isinstance(usage, dict) and usage:
            STATE["active_usage"] = usage
        entry = {
            "time": ts(),
            "kind": event.get("kind", "log"),
            "text": str(event.get("text", "작업 중"))[:300],
        }
        if event.get("paths"):
            entry["paths"] = [str(path)[:240] for path in event["paths"][:20]]
        if event.get("cost_usd") is not None:
            entry["cost_usd"] = event["cost_usd"]
        logs = STATE.setdefault("active_work_log", [])
        if logs and logs[-1].get("kind") == entry["kind"] and logs[-1].get("text") == entry["text"]:
            logs[-1].update(entry)
        else:
            logs.append(entry)
            del logs[:-40]


def mark_approval_requested(agent: str, phase: str) -> None:
    requester = f"{AGENTS[agent]['label']} · {phase}"
    with STATE_LOCK:
        CONTROL["approval_requested"] = True
        CONTROL["approval_rejected"] = False
        requesters = CONTROL.setdefault("approval_requested_by", [])
        if requester not in requesters:
            requesters.append(requester)


# ──────────────────────────────────────────
# 아주 가벼운 마크다운 → HTML 변환 (외부 의존성 없이 굵게/목록/링크만 처리)
# ──────────────────────────────────────────

_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(((?:https?|file)://[^\s)]+)\)")
_MD_NUM_LIST_RE = re.compile(r"^(\d+)\.\s+(.*)$")
_MD_BULLET_LIST_RE = re.compile(r"^[-*]\s+(.*)$")
_MD_HEADER_RE = re.compile(r"^#{1,6}\s+(.*)$")


def _md_inline(escaped_line: str) -> str:
    """이미 html.escape된 한 줄 안에서 인라인 서식만 치환한다."""
    escaped_line = _MD_LINK_RE.sub(r'<a href="\2" target="_blank" rel="noopener">\1</a>', escaped_line)
    escaped_line = _MD_BOLD_RE.sub(r"<strong>\1</strong>", escaped_line)
    escaped_line = _MD_INLINE_CODE_RE.sub(r"<code>\1</code>", escaped_line)
    return escaped_line


def render_text_html(raw: str) -> str:
    """LLM이 흔히 쓰는 마크다운(굵게/번호목록/불릿목록/링크/헤더)을 안전하게 HTML로 렌더링."""
    escaped = html.escape(raw)
    parts: list[str] = []
    list_buffer: list[tuple[str, str | None]] = []  # (내용, 번호목록이면 원래 번호)
    list_tag: str | None = None

    def flush_list() -> None:
        nonlocal list_buffer, list_tag
        if list_buffer:
            items = "".join(
                f'<li value="{num}">{_md_inline(text)}</li>' if num else f"<li>{_md_inline(text)}</li>"
                for text, num in list_buffer
            )
            parts.append(f"<{list_tag}>{items}</{list_tag}>")
            list_buffer = []
            list_tag = None

    for line in escaped.split("\n"):
        stripped = line.strip()
        num_match = _MD_NUM_LIST_RE.match(stripped)
        bullet_match = _MD_BULLET_LIST_RE.match(stripped)
        header_match = _MD_HEADER_RE.match(stripped)

        if num_match:
            if list_tag != "ol":
                flush_list()
                list_tag = "ol"
            list_buffer.append((num_match.group(2), num_match.group(1)))
        elif bullet_match:
            if list_tag != "ul":
                flush_list()
                list_tag = "ul"
            list_buffer.append((bullet_match.group(1), None))
        else:
            flush_list()
            if header_match:
                parts.append(f"<strong>{_md_inline(header_match.group(1))}</strong><br>")
            elif stripped == "":
                parts.append("<br>")
            else:
                parts.append(_md_inline(line) + "<br>")
    flush_list()
    return "".join(parts)


def bubble_html(m: dict) -> str:
    agent = AGENTS[m["agent"]]
    side = agent["side"]
    phase = m.get("phase", "")
    highlight_class = " system" if m["agent"] == "system" else (
        " report" if phase == "최종 보고" else (" confirm" if phase == CONFIRM_PHASE else "")
    )
    avatar_html = (
        f'<img class="avatar" src="{html.escape(agent["avatar"])}" alt="">'
        if agent.get("avatar") else '<span class="system-avatar" aria-hidden="true">TC</span>'
    )

    meta = m.get("meta")
    stats_html = ""
    work_html = ""
    if meta:
        changed_paths = meta.get("changed_paths", [])
        changes_html = f' · 파일 변경 {len(changed_paths)}개' if meta.get("cli_mode") == "coding" else ""
        stats_html = (
            f'<div class="stats">⏱ {meta["elapsed"]}초 · '
            f'추정 토큰 ~{meta["est_tokens"]} · '
            f'입력 {meta["prompt_chars"]}자/출력 {meta["output_chars"]}자 · '
            f'{html.escape(meta["cli_mode"])}{changes_html}</div>'
        )
        work_log = meta.get("work_log", [])
        if work_log:
            entries = []
            for event in work_log[-20:]:
                paths = event.get("paths") or []
                path_html = "".join(f'<code class="log-path">{html.escape(path)}</code>' for path in paths[:10])
                entries.append(
                    f'<li class="work-log-item work-{html.escape(event.get("kind", "log"))}">'
                    f'<span class="log-time">{html.escape(event.get("time", ""))}</span>'
                    f'<p class="log-text">{html.escape(event.get("text", ""))}</p>{path_html}</li>'
                )
            work_html = (
                f'<details class="turn-work-log collapsible-section"><summary class="section-summary">작업 로그 <span class="badge">{len(work_log)}</span></summary>'
                f'<ol class="work-log-list">{"".join(entries)}</ol></details>'
            )
        shared_context = meta.get("shared_context", [])
        if shared_context:
            labels = ", ".join(
                f'{item.get("label", "모델")} · {item.get("phase", "대화")}' for item in shared_context
            )
            work_html += f'<div class="context-proof">전달받은 최근 대화: {html.escape(labels)}</div>'
        if meta.get("turn_diff"):
            checkpoint = html.escape(meta.get("checkpoint_path", ""))
            file_choices = "".join(
                f'<label class="file-choice-label"><input type="checkbox" value="{html.escape(item["path"])}">'
                f'<span class="change-type">{html.escape(item["change"])}</span> <span class="file-path">{html.escape(item["path"])}</span></label>'
                for item in meta.get("changed_paths", []) if not item["path"].endswith("/")
            )
            work_html += (
                f'<details class="turn-diff collapsible-section"><summary class="section-summary">코드 변경 Diff</summary>'
                f'<div class="diff-container">'
                f'<div class="checkpoint-info">체크포인트: <code>{checkpoint}</code></div>'
                f'<pre class="diff-content"><code>{html.escape(meta["turn_diff"])}</code></pre>'
                f'<div class="file-review" data-checkpoint="{checkpoint}">{file_choices}'
                f'<button class="danger compact rollback-btn" onclick="rollbackSelectedFiles(this)">선택 변경 되돌리기</button>'
                f'</div></div></details>'
            )
        if meta.get("agent_calls"):
            calls_html = "".join(
                f'<li class="agent-call-item"><strong class="agent-label">{html.escape(AGENTS.get(call["target"], {}).get("label", call["target"]))}</strong>'
                f' · <span class="call-mode">{html.escape(call["mode"])}</span> · <span class="call-task">{html.escape(call["task"])}</span></li>'
                for call in meta["agent_calls"]
            )
            work_html += (
                f'<details class="turn-agent-calls collapsible-section">'
                f'<summary class="section-summary">후속 에이전트 호출 <span class="badge">{len(meta["agent_calls"])}</span></summary>'
                f'<ul class="agent-calls-list">{calls_html}</ul>'
                f'</details>'
            )

    return f"""
    <div class="row {side}{highlight_class}">
      <div class="bubble" style="--accent:{agent['color']}">
        <div class="meta">
          {avatar_html}
          <span class="name">{html.escape(agent['label'])}</span>
          <span class="phase">{html.escape(m.get('phase', ''))}</span>
          <span class="time">{html.escape(m['time'])}</span>
        </div>
        <div class="text">{render_text_html(m['text'])}</div>
        {stats_html}
        {work_html}
      </div>
    </div>"""


def status_text() -> str:
    if not STATE.get("topic"):
        return "주제 입력 대기"
    active_agent = STATE.get("active_agent")
    if active_agent:
        label = AGENTS.get(active_agent, {}).get("label", active_agent)
        phase = STATE.get("active_phase") or "생각 중"
        return f"{label} · {phase} 중"
    if CONTROL["intervention_pending"]:
        return "사용자 개입 처리 대기 중"
    if STATE.get("finished"):
        return "완료"
    if CONTROL["approval_rejected"]:
        return "승인 거절됨"
    if CONTROL["stopped"]:
        return "중단됨"
    if CONTROL["awaiting_approval"]:
        if CONTROL["approval_deferred"]:
            return "\U0001f6d1 승인 보류 중 — 질문/수정 요청 가능"
        return "\U0001f6d1 사용자 승인 대기 중"
    if CONTROL["paused"]:
        return "일시정지"
    if STATE.get("mode") == "continuous":
        return "무제한 코딩 진행 중"
    return "진행 중..."


def render_html_snapshot() -> None:
    """참고용 정적 스냅샷 파일 (roundtable.html). 실제 화면은 서버가 동적으로 서빙한다."""
    HTML_PATH.write_text(render_dashboard(), encoding="utf-8")


def actual_token_count(usage: dict | None) -> int:
    usage = usage or {}
    # OpenAI input_tokens already includes cached_input_tokens; Anthropic cache fields are separate.
    return sum(int(usage.get(key, 0) or 0) for key in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"
    ))


def budget_exceeded_reason(state: dict) -> str | None:
    budget = state.get("budget") or {}
    token_limit = int(budget.get("token_limit", 0) or 0)
    cost_limit = float(budget.get("cost_limit_usd", 0.0) or 0.0)
    if token_limit and int(state.get("total_actual_tokens", 0) or 0) >= token_limit:
        return f"실제 토큰 예산 {token_limit:,}개에 도달했습니다."
    if cost_limit and float(state.get("total_actual_cost_usd", 0.0) or 0.0) >= cost_limit:
        return f"비용 예산 ${cost_limit:.2f}에 도달했습니다."
    return None


def budget_block_message(reason: str) -> str:
    return (
        f"요청을 실행하지 않았습니다. {reason}\n\n"
        "오른쪽 Usage에서 Token limit을 현재 누적 사용량보다 크게 설정하거나, "
        "0으로 설정해 제한을 해제해주세요. 보류된 요청은 저장 후 자동으로 재개되며, "
        "전송 전에 차단된 요청은 입력창에서 다시 보내면 됩니다."
    )


def add_message(
    agent: str,
    phase: str,
    text: str,
    meta: dict | None = None,
    expected_session_id: str | None = None,
) -> bool:
    with STATE_LOCK:
        if expected_session_id and STATE.get("id") != expected_session_id:
            return False
        message = {"agent": agent, "phase": phase, "time": ts(), "text": text}
        if meta:
            message["meta"] = meta
            STATE["total_est_tokens"] = STATE.get("total_est_tokens", 0) + meta.get("est_tokens", 0)
            STATE["total_elapsed_time"] = STATE.get("total_elapsed_time", 0.0) + meta.get("elapsed", 0.0)
            STATE["total_actual_tokens"] = STATE.get("total_actual_tokens", 0) + actual_token_count(
                meta.get("actual_usage")
            )
            STATE["total_actual_cost_usd"] = round(
                STATE.get("total_actual_cost_usd", 0.0) + float(meta.get("actual_cost_usd", 0.0) or 0.0),
                6,
            )
        STATE["messages"].append(message)
        save_state(STATE)
        session_id = STATE["id"]
    append_log(agent, phase, text)
    append_session_transcript(session_id, agent, phase, text, meta)
    append_memory(session_id, agent, phase, text, meta)
    append_session_role_profile(session_id, agent, phase, text)
    render_html_snapshot()
    return True


def announce_roles_if_complete(expected_session_id: str | None = None) -> bool:
    with STATE_LOCK:
        if expected_session_id and STATE.get("id") != expected_session_id:
            return False
        enabled = normalize_enabled_agents(STATE.get("enabled_agents"))
        roles = normalize_agent_roles(STATE.get("agent_roles"))
        if not enabled or any(not roles.get(agent) for agent in enabled):
            return False
        signature = "|".join(f"{agent}:{roles[agent]}" for agent in enabled)
        if STATE.get("roles_announced_signature") == signature:
            return False
        STATE["roles_announced_signature"] = signature
        save_state(STATE)
        session_id = STATE.get("id")
    lines = ["역할 배정이 확정되었습니다."]
    lines.extend(
        f"- **{AGENTS[agent]['label']}**: {role_label(roles[agent])}"
        for agent in enabled
    )
    return add_message(
        "system", "역할 배정 완료", "\n".join(lines),
        expected_session_id=expected_session_id or session_id,
    )


INTERVENTION_INTENTS = {
    "question": {
        "label": "질문",
        "phase": "사용자 개입 · 질문",
        "instruction": (
            "팀장(사용자)이 진행 중인 대화에 질문으로 개입했다. 최근 대화와 공유 메모리를 "
            "확인해서 질문에 먼저 답하고, 기존 진행 계획에 영향이 있는지 짧게 정리해라. "
            "아직 실제로 파일을 수정하지 마."
        ),
        "pause_after": False,
    },
    "redirect": {
        "label": "방향 수정",
        "phase": "사용자 개입 · 방향 수정",
        "instruction": (
            "팀장(사용자)이 진행 방향을 수정했다. 이 지시를 기존 계획보다 우선해서 반영하고, "
            "앞으로 어떤 단계/역할/작업이 달라지는지 정리해라. 아직 실제로 파일을 수정하지 마."
        ),
        "pause_after": False,
    },
    "execute": {
        "label": "코딩 실행",
        "phase": "사용자 개입 · 코딩 실행",
        "instruction": (
            "팀장(사용자)이 실제 구현을 명시적으로 지시했다. 계획이나 착수 보고만 작성하지 말고, "
            "현재 프로젝트 파일을 직접 확인하고 담당 범위의 코드를 실제로 수정해라. 변경 후 가능한 검증을 실행하고, "
            "완료된 파일과 검증 결과만 간결하게 보고해라. 다른 에이전트 담당 파일은 수정하지 마."
        ),
        "pause_after": False,
    },
    "delegation": {
        "label": "에이전트 호출",
        "phase": "에이전트 호출 답변",
        "instruction": "다른 에이전트가 전문 검토나 후속 작업을 요청했다. 요청 범위만 실제로 처리하고 결과를 호출한 에이전트와 팀장에게 간결하게 보고해라.",
        "pause_after": False,
    },
    "hold": {
        "label": "멈추고 답변",
        "phase": "사용자 개입 · 멈추고 답변",
        "instruction": (
            "팀장(사용자)이 진행을 잠시 멈추고 답변을 요구했다. 질문이나 우려에 답하고, "
            "계속 진행하려면 무엇을 승인하거나 수정해야 하는지 명확히 정리해라. 아직 실제로 파일을 수정하지 마."
        ),
        "pause_after": True,
    },
    "note": {
        "label": "참고",
        "phase": "사용자 메모",
        "instruction": "",
        "pause_after": False,
    },
}


def intervention_responder() -> str:
    with STATE_LOCK:
        enabled = normalize_enabled_agents(STATE.get("enabled_agents"))
        active = STATE.get("active_agent")
    if active in enabled:
        return active
    return reporter_for(enabled)


def normalize_target_agents(targets: list[str] | None) -> list[str]:
    with STATE_LOCK:
        enabled = normalize_enabled_agents(STATE.get("enabled_agents"))
    if targets is None:
        return enabled
    return [agent for agent in targets if agent in enabled]


def persist_intervention_queue_locked() -> None:
    STATE["pending_interventions"] = json.loads(json.dumps(
        CONTROL["intervention_queue"], ensure_ascii=False
    ))
    save_state(STATE)


def mark_intervention(intent: str, targets: list[str] | None = None, cli_mode: str = "discussion") -> None:
    intent = intent if intent in INTERVENTION_INTENTS else "question"
    if intent == "note":
        return
    target_agents = normalize_target_agents(targets)
    with STATE_LOCK:
        CONTROL["intervention_pending"] = True
        CONTROL["intervention_intent"] = intent
        CONTROL["intervention_seen_messages"] = len(STATE["messages"])
        CONTROL["intervention_queue"].append({
            "intent": intent,
            "targets": target_agents,
            "cli_mode": "coding" if cli_mode == "coding" else "discussion",
        })
        persist_intervention_queue_locked()


def queue_agent_calls(source_agent: str, calls: list[dict], depth: int) -> list[dict]:
    queued = []
    if depth >= MAX_DELEGATION_DEPTH:
        return queued
    with STATE_LOCK:
        enabled = normalize_enabled_agents(STATE.get("enabled_agents"))
        count = int(STATE.get("delegation_count", 0))
        for call in calls:
            target = call.get("target")
            if target not in enabled or target == source_agent or count >= MAX_SESSION_DELEGATIONS:
                continue
            entry = {
                "source": source_agent,
                "target": target,
                "mode": call.get("mode", "discussion"),
                "task": str(call.get("task", ""))[:600],
                "depth": depth + 1,
                "time": ts(),
            }
            CONTROL["intervention_queue"].append({
                "intent": "delegation",
                "targets": [target],
                "cli_mode": entry["mode"],
                "custom_instruction": entry["task"],
                "source_agent": source_agent,
                "delegation_depth": depth + 1,
            })
            STATE.setdefault("delegation_history", []).append(entry)
            del STATE["delegation_history"][:-40]
            count += 1
            queued.append(entry)
        STATE["delegation_count"] = count
        CONTROL["intervention_pending"] = bool(CONTROL["intervention_queue"])
        if queued:
            persist_intervention_queue_locked()
    for entry in queued:
        add_runtime_event(
            f'{AGENTS[source_agent]["label"]} → {AGENTS[entry["target"]]["label"]} 호출 '
            f'({entry["mode"]}): {entry["task"][:160]}'
        )
    return queued


def process_pending_intervention(expected_session_id: str | None = None) -> bool:
    with STATE_LOCK:
        if expected_session_id and STATE.get("id") != expected_session_id:
            return False
    if not CONTROL["intervention_pending"]:
        return False
    with STATE_LOCK:
        first_item = (
            CONTROL["intervention_queue"][0]
            if CONTROL["intervention_queue"] else None
        )
        if (
            first_item
            and first_item.get("retry_blocked")
            and CONTROL["paused"]
        ):
            return False
        item = CONTROL["intervention_queue"].pop(0) if CONTROL["intervention_queue"] else None
        CONTROL["intervention_pending"] = bool(CONTROL["intervention_queue"])
        CONTROL["intervention_intent"] = CONTROL["intervention_queue"][0]["intent"] if CONTROL["intervention_queue"] else ""
        persist_intervention_queue_locked()
    if not item:
        return False
    intent = item["intent"] if item["intent"] in INTERVENTION_INTENTS else "question"
    cli_mode = "coding" if item.get("cli_mode") == "coding" else "discussion"
    spec = INTERVENTION_INTENTS[intent]
    targets = normalize_target_agents(item.get("targets"))
    source_agent = item.get("source_agent")
    source_label = AGENTS.get(source_agent, {}).get("label", "사용자")
    event_prefix = f"{source_label} 호출" if intent == "delegation" else "사용자 개입"
    add_runtime_event(f"{event_prefix} 처리 시작: {spec['label']} → {agent_names(targets)}")
    for target_index, responder in enumerate(targets):
        if CONTROL["stopped"]:
            return True
        custom_instruction = item.get("custom_instruction", "")
        target_instruction = (
            f"{spec['instruction']}\n\n"
            f'{f"호출한 에이전트: {source_label}. 요청 내용: {custom_instruction}" if custom_instruction else ""}\n'
            f"이번 개입의 직접 대상 모델: {agent_names(targets)}.\n"
            f"너({AGENTS[responder]['label']})에게 직접 질문/지시가 왔다고 보고 "
            f'{"실제 파일을 수정하고 결과를 보고해라." if cli_mode == "coding" else "답해라."}'
        )
        if not run_step(
            responder,
            f"호출 답변 · {source_label}" if intent == "delegation" else "개입 답변",
            target_instruction,
            cli_mode,
            expected_session_id=expected_session_id,
            delegation_depth=int(item.get("delegation_depth", 0)),
        ):
            retry_item = dict(item)
            retry_item["targets"] = targets[target_index:]
            retry_item["retry_blocked"] = True
            with STATE_LOCK:
                if not CONTROL["stopped"]:
                    CONTROL["intervention_queue"].insert(0, retry_item)
                    CONTROL["intervention_pending"] = True
                    CONTROL["intervention_intent"] = retry_item["intent"]
                    CONTROL["paused"] = True
                persist_intervention_queue_locked()
            return True
    with STATE_LOCK:
        CONTROL["intervention_seen_messages"] = len(STATE["messages"])
    if spec["pause_after"]:
        CONTROL["paused"] = True
        separator("사용자 개입 처리 완료 — 재개 버튼을 누르면 이어서 진행됩니다")
    return True


def approve_pending_work() -> bool:
    was_waiting = CONTROL["awaiting_approval"] or CONTROL["approval_requested"]
    with STATE_LOCK:
        approval_requests = unresolved_approval_requests(STATE.get("messages", []))
    if was_waiting:
        add_message("user", "승인", "승인")
        add_runtime_event("사용자가 진행을 승인했습니다.")
    CONTROL["awaiting_approval"] = False
    CONTROL["approval_deferred"] = False
    CONTROL["approval_requested"] = False
    CONTROL["approval_requested_by"] = []
    CONTROL["approval_rejected"] = False
    followup_agents = list(dict.fromkeys(
        request["agent"] for request in approval_requests if request["phase"] != CONFIRM_PHASE
    ))
    with STATE_LOCK:
        if followup_agents:
            CONTROL["intervention_queue"].append({
                "intent": "execute",
                "targets": followup_agents,
                "cli_mode": "coding",
                "custom_instruction": (
                    "사용자가 방금 승인했다. 네가 승인을 요청했던 작업을 계획 보고로 끝내지 말고 "
                    "지금 실제로 수행한 뒤 변경 내용과 검증 결과를 답해라."
                ),
                "approved_followup": True,
            })
            STATE["finished"] = False
        CONTROL["intervention_pending"] = bool(CONTROL["intervention_queue"])
        CONTROL["intervention_intent"] = (
            CONTROL["intervention_queue"][0].get("intent", "")
            if CONTROL["intervention_queue"] else ""
        )
        persist_intervention_queue_locked()
    if CONTROL["intervention_pending"]:
        add_runtime_event("승인된 후속 작업을 실행 큐에 등록했습니다.")
        start_worker_if_needed(force=True)
    return was_waiting


def wait_for_user_approval(session_id: str) -> bool:
    with STATE_LOCK:
        if STATE.get("id") != session_id:
            return False
        CONTROL["awaiting_approval"] = True
        CONTROL["approval_deferred"] = False
        CONTROL["approval_rejected"] = False
        CONTROL["approval_seen_messages"] = len(STATE["messages"])
    separator("사용자 승인 대기 중 — 승인해야 다음 단계가 진행됩니다")
    while CONTROL["awaiting_approval"] and not CONTROL["stopped"]:
        with STATE_LOCK:
            if STATE.get("id") != session_id:
                return False
            message_count = len(STATE["messages"])
            last_message = STATE["messages"][-1] if STATE["messages"] else None
        if message_count > CONTROL["approval_seen_messages"]:
            CONTROL["approval_seen_messages"] = message_count
            if last_message and last_message.get("agent") == "user":
                CONTROL["approval_deferred"] = True
                process_pending_intervention(session_id)
                with STATE_LOCK:
                    CONTROL["approval_seen_messages"] = len(STATE["messages"])
        time.sleep(0.3)
    return not CONTROL["approval_rejected"] and not CONTROL["stopped"]


# ──────────────────────────────────────────
# 워커 (에이전트 호출 루프) — 백그라운드 스레드
# ──────────────────────────────────────────

def estimate_tokens(*texts: str) -> int:
    """한국어도 과소 계산하지 않도록 UTF-8 바이트를 이용한 거친 토큰 추정치."""
    return max(1, math.ceil(sum(len(text.encode("utf-8")) for text in texts) / 4))


def is_cli_failure(text: str) -> bool:
    return "응답 없음" in text or "빈 응답" in text or is_incomplete_tool_response(text)


def run_step(
    agent: str,
    phase: str,
    instruction: str,
    cli_mode: str,
    *,
    expected_session_id: str | None = None,
    delegation_depth: int = 0,
) -> bool:
    requested_cli_mode = cli_mode
    with STATE_LOCK:
        budget_reason = None if STATE.get("mode") == "continuous" else budget_exceeded_reason(STATE)
    if budget_reason:
        CONTROL["paused"] = True
        add_runtime_event(f"예산 초과로 자동 일시정지: {budget_reason}", level="error")
        add_message(
            agent,
            "실행 차단 · 예산 한도",
            budget_block_message(budget_reason),
            {
                "elapsed": 0.0,
                "est_tokens": 0,
                "cli_mode": effective_cli_mode(cli_mode),
                "prompt_chars": 0,
                "output_chars": 0,
                "raw_output_chars": 0,
                "output_truncated": False,
                "approval_requested": False,
                "ok": False,
                "failure_kind": "budget",
                "changed_paths": [],
                "snapshot_truncated": False,
            },
            expected_session_id=expected_session_id,
        )
        return False
    if requested_cli_mode == "coding":
        with STATE_LOCK:
            current_roles = normalize_agent_roles(STATE.get("agent_roles"))
            role_id = current_roles.get(agent, "")
            same_session = not expected_session_id or STATE.get("id") == expected_session_id
        if not same_session:
            return False
        if role_id and not ROLE_CATALOG[role_id]["can_write"]:
            cli_mode = "discussion"
            instruction = (
                f"{instruction}\n\n현재 역할은 {ROLE_CATALOG[role_id]['label']}이며 읽기 전용 역할이다. "
                "파일을 수정하지 말고 검토 결과와 담당 모델에게 넘길 작업만 보고해라."
            )
            add_runtime_event(f"{AGENTS[agent]['label']} 코딩 요청을 역할 규칙에 따라 읽기 전용으로 전환")
        else:
            cli_mode = effective_cli_mode(cli_mode)
    else:
        cli_mode = effective_cli_mode(cli_mode)
    cli_mode = effective_cli_mode(cli_mode)
    if requested_cli_mode == "coding" and cli_mode != "coding" and workspace_access_mode() != "write":
        instruction = (
            f"{instruction}\n\n"
            "현재 작업영역 권한은 읽기 전용이다. 파일을 생성·수정·삭제하지 말고, "
            "분석 결과와 변경 제안만 보고해라."
        )
        add_runtime_event(f"{AGENTS[agent]['label']} 코딩 요청을 읽기 전용 분석으로 전환")
    label = AGENTS[agent]["label"]
    separator(f"{label} — {phase}")
    with STATE_LOCK:
        if expected_session_id and STATE.get("id") != expected_session_id:
            return False
        session_id = STATE.get("id")
        state_snapshot = dict(STATE)
        topic = STATE.get("topic", "").strip()
        transcript, shared_messages = build_shared_transcript(
            STATE["messages"],
            normalize_enabled_agents(STATE.get("enabled_agents")),
        )
    project_access = turn_project_access(cli_mode, state_snapshot)
    memory_context = build_memory_context(
        state_snapshot, expose_file_paths=project_access != "none"
    )
    team_prompt = load_team_prompt()
    prompt = compose_agent_prompt(
        team_prompt, topic, memory_context, transcript, instruction,
        role_prompt(agent, state_snapshot), project_access_prompt(project_access),
    )

    with STATE_LOCK:
        if expected_session_id and STATE.get("id") != expected_session_id:
            return False
        STATE["active_agent"] = agent
        STATE["active_phase"] = phase
        STATE["active_started_at"] = time.time()
        STATE["active_cli_mode"] = cli_mode
        STATE["active_prompt_chars"] = len(prompt)
        STATE["active_work_log"] = []
        STATE["active_usage"] = {}
        save_state(STATE)
    add_active_work_event(agent, {"kind": "status", "text": "CLI 프로세스 시작"})
    if shared_messages:
        shared_labels = [AGENTS.get(message.get("agent"), {}).get("label", "사용자") for message in shared_messages]
        add_active_work_event(
            agent,
            {"kind": "context", "text": f"최근 대화 {len(shared_messages)}개 공유: {', '.join(shared_labels)}"},
        )
    add_runtime_event(f"{label} — {phase} 시작 (cli_mode={cli_mode}, 입력 {len(prompt)}자)")
    render_html_snapshot()
    print(f"\U0001f914 {label} 생각 중... (입력 약 {len(prompt)}자, cli_mode={cli_mode})")
    project_path = load_project_path()
    before_snapshot, before_truncated = (
        snapshot_project_tree(project_path) if cli_mode == "coding" else ({}, False)
    )
    before_texts = capture_project_texts(project_path) if cli_mode == "coding" else {}
    t0 = time.time()
    try:
        ask_func = ASK_FUNCS[agent]
        parameters = list(inspect.signature(ask_func).parameters.values())
        supports_callback = len(parameters) >= 3 or any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters
        )
        if supports_callback:
            raw_text = ask_func(
                prompt,
                cli_mode,
                lambda event: add_active_work_event(agent, event),
            )
        else:
            raw_text = ask_func(prompt, cli_mode)
    except Exception as exc:
        raw_text = f"({label} 응답 없음 — 실행 중 예외: {exc})"
    elapsed = time.time() - t0
    after_snapshot, after_truncated = (
        snapshot_project_tree(project_path) if cli_mode == "coding" else ({}, False)
    )
    changed_paths = compare_project_snapshots(before_snapshot, after_snapshot) if cli_mode == "coding" else []
    scope_violations = role_scope_violations(agent, changed_paths, state_snapshot) if cli_mode == "coding" else []
    turn_diff = build_turn_diff(project_path, before_texts, changed_paths) if cli_mode == "coding" else ""
    checkpoint_path = save_turn_checkpoint(
        session_id,
        int(state_snapshot.get("step_index", 0)),
        agent,
        turn_diff,
    ) if cli_mode == "coding" else ""
    if changed_paths:
        add_active_work_event(
            agent,
            {
                "kind": "file",
                "text": f"작업 폴더 변경 {len(changed_paths)}개 감지",
                "paths": [item["path"] for item in changed_paths[:20]],
            },
        )
    with STATE_LOCK:
        work_log = list(STATE.get("active_work_log", []))
        actual_usage = dict(STATE.get("active_usage", {}))
    actual_cost_usd = next(
        (event.get("cost_usd") for event in reversed(work_log) if event.get("cost_usd") is not None),
        0.0,
    )
    call_clean_text, agent_calls = extract_agent_calls(raw_text, agent, cli_mode)
    if not allow_agent_calls(state_snapshot.get("mode", "discussion"), phase):
        agent_calls = []
    visible_text, approval_requested = extract_approval_token(call_clean_text)
    requested_role = extract_role_selection(visible_text) if phase in {"역할 선언", "역할 확정"} else ""
    if phase in {"역할 선언", "역할 확정"}:
        visible_text = strip_role_selection(visible_text)
    approval_requested = should_honor_approval_request(
        approval_requested,
        state_snapshot.get("mode", "discussion"),
        phase,
    )
    text, output_truncated = clip_agent_output(visible_text)
    if approval_requested and not text:
        text = "사용자 승인을 요청했습니다."
    est_tokens = estimate_tokens(prompt, raw_text)
    failed = is_cli_failure(raw_text) or bool(scope_violations)
    if scope_violations:
        violation_list = ", ".join(scope_violations[:8])
        suffix = " ..." if len(scope_violations) > 8 else ""
        text = (
            f"{text}\n\n[역할 범위 위반] 담당 밖 파일 변경 {len(scope_violations)}개: "
            f"{violation_list}{suffix}\n세션을 일시정지했습니다. 체크포인트에서 변경을 검토하거나 되돌려주세요."
        ).strip()
        CONTROL["paused"] = True
    approval_requested = approval_requested and not failed
    print(f"✅ {label} 응답 완료 — {elapsed:.1f}초, 추정 토큰 ~{est_tokens} "
          f"(입력 {len(prompt)}자 / 출력 {len(text)}자)")
    print(text)
    meta = {
        "elapsed": round(elapsed, 1),
        "est_tokens": est_tokens,
        "cli_mode": cli_mode,
        "prompt_chars": len(prompt),
        "output_chars": len(text),
        "raw_output_chars": len(raw_text),
        "output_truncated": output_truncated,
        "approval_requested": approval_requested,
        "ok": not failed,
        "failure_kind": "role_scope" if scope_violations else ("cli" if failed else ""),
        "role_id": normalize_agent_roles(state_snapshot.get("agent_roles")).get(agent, ""),
        "role_label": role_label(normalize_agent_roles(state_snapshot.get("agent_roles")).get(agent, "")),
        "role_scope_violations": scope_violations,
        "changed_paths": changed_paths[:100],
        "turn_diff": turn_diff,
        "checkpoint_path": checkpoint_path,
        "work_log": work_log,
        "actual_usage": actual_usage,
        "actual_cost_usd": actual_cost_usd,
        "agent_calls": agent_calls,
        "delegation_depth": delegation_depth,
        "continuous_cycle": (
            (int(state_snapshot.get("step_index", 0)) // max(1, len(steps_for_mode(
                "continuous", normalize_enabled_agents(state_snapshot.get("enabled_agents"))
            )))) + 1
            if state_snapshot.get("mode") == "continuous" else 0
        ),
        "shared_context": [
            {
                "agent": message.get("agent", ""),
                "label": AGENTS.get(message.get("agent"), {}).get("label", "사용자"),
                "phase": message.get("phase", ""),
            }
            for message in shared_messages
        ],
        "snapshot_truncated": before_truncated or after_truncated or len(changed_paths) > 100,
    }
    with STATE_LOCK:
        same_session = STATE.get("id") == session_id
        if same_session:
            STATE["active_agent"] = None
            STATE["active_phase"] = None
            STATE["active_started_at"] = None
            STATE["active_cli_mode"] = None
            STATE["active_prompt_chars"] = 0
            STATE["active_work_log"] = []
            STATE["active_usage"] = {}
            save_state(STATE)
    if not same_session:
        print(f"  \u26a0\ufe0f {label} 응답은 새 세션이 시작되어 폐기했습니다.")
        return False
    level = "error" if failed else "info"
    result_label = "실패" if failed else "완료"
    add_runtime_event(
        f"{label} — {phase} {result_label} ({elapsed:.1f}초, 추정 토큰 ~{est_tokens})",
        level=level,
    )
    if scope_violations:
        add_runtime_event(
            f"역할 범위 위반으로 일시정지: {', '.join(scope_violations[:6])}", level="error"
        )
    if approval_requested:
        mark_approval_requested(agent, phase)
        add_runtime_event(f"승인 요청 감지: {label} · {phase}")
    if cli_mode == "coding":
        if changed_paths:
            preview = ", ".join(f"{item['change']} {item['path']}" for item in changed_paths[:6])
            suffix = " ..." if len(changed_paths) > 6 else ""
            add_runtime_event(f"파일 변경 감지 {len(changed_paths)}개: {preview}{suffix}")
            if checkpoint_path:
                add_runtime_event(f"턴 체크포인트 저장: {checkpoint_path}")
        else:
            add_runtime_event("파일 변경 감지: 없음")
    selected_role = ""
    if not failed and phase in {"역할 선언", "역할 확정"}:
        selected_role = choose_discussion_role(agent, requested_role)
        meta["role_id"] = selected_role
        meta["role_label"] = role_label(selected_role)
        meta["role_auto_selected"] = True
    added = add_message(agent, phase, text, meta, expected_session_id=session_id)
    if added and not failed and phase in {"역할 선언", "역할 확정"}:
        announce_roles_if_complete(expected_session_id=session_id)
    if added and not failed and agent_calls:
        queue_agent_calls(agent, agent_calls, delegation_depth)
    return added and not failed


def worker_loop(session_id: str) -> None:
    restart_pending = False
    try:
        while True:
            with STATE_LOCK:
                if STATE.get("id") != session_id:
                    break
                mode = STATE.get("mode", "discussion")
                enabled_agents = normalize_enabled_agents(STATE.get("enabled_agents"))
                steps = steps_for_mode(mode, enabled_agents)
                step_index = STATE["step_index"]
                continuous = mode == "continuous"
            if CONTROL["approval_requested"]:
                if not wait_for_user_approval(session_id):
                    break
                continue
            if process_pending_intervention(session_id):
                continue
            if not continuous and step_index >= len(steps):
                break
            if CONTROL["stopped"]:
                break
            while CONTROL["paused"] and not CONTROL["stopped"]:
                if process_pending_intervention(session_id):
                    continue
                time.sleep(0.3)
            if CONTROL["stopped"]:
                break
            if process_pending_intervention(session_id):
                continue

            cycle_index = step_index % len(steps) if continuous else step_index
            cycle_number = (step_index // len(steps)) + 1 if continuous else 0
            agent, phase, instruction, cli_mode = steps[cycle_index]
            display_phase = f"{phase} · 사이클 {cycle_number}" if continuous else phase
            if continuous and cycle_index == 0:
                add_runtime_event(f"무제한 코딩 사이클 {cycle_number} 시작")
            succeeded = run_step(
                agent,
                display_phase,
                instruction,
                cli_mode,
                expected_session_id=session_id,
            )
            if not succeeded:
                with STATE_LOCK:
                    if STATE.get("id") == session_id:
                        CONTROL["paused"] = True
                break

            with STATE_LOCK:
                if STATE.get("id") != session_id:
                    break
                STATE["step_index"] += 1
                next_index = STATE["step_index"]
                save_state(STATE)

            next_cycle_index = next_index % len(steps) if continuous else next_index
            if continuous and next_cycle_index == 0:
                add_runtime_event(f"무제한 코딩 사이클 {cycle_number} 완료 · 다음 사이클 계속")
            should_validate = cli_mode == "coding" and (
                (continuous and steps[next_cycle_index][1] == "교차 검토")
                or (not continuous and (
                    next_index >= len(steps) or steps[next_index][1] == "최종 보고"
                ))
            )
            if should_validate:
                add_runtime_event("자동 검증 시작")
                validation_results = run_project_validation(load_project_path())
                with STATE_LOCK:
                    if STATE.get("id") == session_id:
                        STATE["validation_results"] = validation_results
                        save_state(STATE)
                if not validation_results:
                    add_runtime_event("자동 검증 명령을 찾지 못했습니다.")
                for result in validation_results:
                    level = "info" if result["ok"] else "error"
                    add_runtime_event(
                        f'{result["label"]}: {"통과" if result["ok"] else "실패"} '
                        f'({result["elapsed"]}초)',
                        level=level,
                    )

            if phase == CONFIRM_PHASE and not CONTROL["approval_requested"]:
                mark_approval_requested(agent, phase)
            if CONTROL["approval_requested"] and not wait_for_user_approval(session_id):
                break
    finally:
        with STATE_LOCK:
            same_session = STATE.get("id") == session_id
            finished = False
            messages_copy = []
            if same_session:
                mode = STATE.get("mode", "discussion")
                enabled_agents = normalize_enabled_agents(STATE.get("enabled_agents"))
                steps = steps_for_mode(mode, enabled_agents)
                finished = (
                    mode != "continuous"
                    and STATE["step_index"] >= len(steps)
                    and not CONTROL["stopped"]
                )
                STATE["finished"] = finished
                STATE["active_agent"] = None
                STATE["active_phase"] = None
                STATE["active_started_at"] = None
                STATE["active_cli_mode"] = None
                STATE["active_prompt_chars"] = 0
                save_state(STATE)
                messages_copy = list(STATE["messages"])
            if CONTROL.get("worker_session_id") == session_id:
                CONTROL["worker_running"] = False
                CONTROL["worker_session_id"] = None
                restart_pending = CONTROL.get("worker_start_pending", False)
                CONTROL["worker_start_pending"] = False
        if same_session:
            render_html_snapshot()
        if same_session and finished:
            for agent in ("codex", "antigravity", "claude"):
                update_profile(agent, messages_copy)
        separator("워커 종료" if not finished else "완료")
        if restart_pending:
            start_worker_if_needed(force=True)


def start_worker_if_needed(force: bool = False) -> None:
    with STATE_LOCK:
        session_id = STATE.get("id")
        if CONTROL["worker_running"]:
            if CONTROL.get("worker_session_id") != session_id:
                CONTROL["worker_start_pending"] = True
            return
        CONTROL["worker_running"] = True
        CONTROL["worker_session_id"] = session_id
        CONTROL["worker_start_pending"] = False
        CONTROL["stopped"] = False
        if STATE.get("continuous_stopped"):
            STATE["continuous_stopped"] = False
            save_state(STATE)
    thread = threading.Thread(target=worker_loop, args=(session_id,), daemon=True)
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


MODE_LABELS = {
    "discussion": "일반 토론 모드",
    "coding": "코딩 모드",
    "continuous": "무제한 코딩",
}


def mode_notice_html(mode: str, discussion_project_access: str = "read") -> str:
    mode_label = MODE_LABELS.get(mode, mode)
    if mode in {"coding", "continuous"}:
        access_label = "프로젝트 읽기·쓰기"
    else:
        access_label = discussion_project_access_label(discussion_project_access)
    return bubble_html({
        "agent": "system",
        "phase": "현재 세션 모드",
        "time": "",
        "text": f"현재 세션은 **{mode_label}**입니다.\n- 접근 범위: {access_label}",
    })


def select_options_html(options: list[tuple[str, str]], selected: str = "") -> str:
    return "".join(
        f'<option value="{html.escape(value)}"{" selected" if value == selected else ""}>'
        f'{html.escape(label)}</option>'
        for value, label in options
    )


def agent_model_cards_html(settings: dict | None = None) -> str:
    normalized = normalize_agent_settings(settings)
    cards = []
    for agent in AGENT_ORDER:
        info = AGENTS[agent]
        catalog = MODEL_CATALOG[agent]
        setting = normalized[agent]
        effort_field = ""
        if catalog["efforts"]:
            effort_field = (
                '<label class="model-field"><span>추론 수준</span>'
                f'<select name="effort_{agent}">'
                f'{select_options_html(catalog["efforts"], setting["effort"])}</select></label>'
            )
        cards.append(
            f'<div class="agent-model-card" data-agent-card="{agent}">'
            f'<label class="agent-toggle"><input type="checkbox" name="agent" value="{agent}" checked>'
            f'<img src="{html.escape(info["avatar"])}" alt="">'
            f'<span><strong>{html.escape(info["label"])}</strong><small>사용</small></span></label>'
            f'<label class="model-field"><span>모델</span><select name="model_{agent}">'
            f'{select_options_html(catalog["models"], setting["model"])}</select></label>'
            f'{effort_field}</div>'
        )
    return '<div class="agent-model-grid">' + "".join(cards) + "</div>"


def topic_section_html(
    topic: str,
    mode: str,
    enabled_agents: list[str],
    active_id: str,
    agent_settings: dict | None = None,
    discussion_project_access: str = "read",
) -> str:
    access_label = "읽기·쓰기" if workspace_access_mode() == "write" else "읽기 전용"
    project_path_line = (
        f'<p>코딩 모드 작업 경로: '
        f'{html.escape(str(load_project_path()))} '
        f'· 권한: {access_label}</p>'
    )

    if topic:
        mode_label = MODE_LABELS.get(mode, mode)
        access_mode = normalize_discussion_project_access(discussion_project_access)
        discussion_access_label = discussion_project_access_label(access_mode)
        disabled_agents = [a for a in AGENT_ORDER if a not in enabled_agents]
        settings_label = " · ".join(
            f'{AGENTS[agent]["label"]}: {agent_setting_label(agent, agent_settings)}'
            for agent in enabled_agents
        )
        return (
            f'<div class="topic"><h3>{html.escape(topic)} '
            f'<span style="color:var(--faint);font-weight:500">· {html.escape(mode_label)}</span></h3>'
            f'<p>활성: {html.escape(agent_names(enabled_agents))} · 비활성: '
            f'{html.escape(agent_names(disabled_agents) if disabled_agents else "없음")}</p>'
            f'<p>모델: {html.escape(settings_label)}</p>'
            f'<p>프로젝트 접근: {"읽기·쓰기 (코딩 실행)" if mode in {"coding", "continuous"} else html.escape(discussion_access_label)}</p>'
            f'<p>저장 폴더: {html.escape(str(session_memory_dir(active_id)))}</p>'
            f'{project_path_line if mode in {"coding", "continuous"} else ""}</div>'
        )

    return f"""
      <div class="panel">
        <p style="margin:0;color:var(--muted);font-size:13px">토론 주제를 입력하면 바로 시작합니다.</p>
        <textarea id="topicInput" placeholder="예: 네이버 쇼핑 최저가 비교 웹앱을 같이 만들 거야" autofocus></textarea>
        <div class="mode-choice">
          <label><input type="radio" name="mode" value="discussion" checked> 일반 토론 모드</label>
          <label><input type="radio" name="mode" value="coding"> 코딩 모드 (실제로 이 폴더 코드를 수정)</label>
          <label><input type="radio" name="mode" value="continuous"> 무제한 코딩 (중단할 때까지 구현·검토 반복)</label>
        </div>
        <div class="discussion-access-choice">
          <strong>일반 토론의 프로젝트 접근</strong>
          <label><input type="radio" name="discussion_project_access" value="read" checked> 프로젝트 읽기</label>
          <label><input type="radio" name="discussion_project_access" value="write"> 프로젝트 읽기·쓰기</label>
          <label><input type="radio" name="discussion_project_access" value="none"> 프로젝트 읽지 않고 토론</label>
        </div>
        {agent_model_cards_html(agent_settings)}
        {project_path_line}
        <button onclick="submitTopic()">시작</button>
      </div>"""


def render_dashboard() -> str:
    with STATE_LOCK:
        messages = list(STATE["messages"])
        topic = STATE.get("topic", "").strip()
        finished = STATE.get("finished", False)
        mode = STATE.get("mode", "discussion")
        enabled_agents = normalize_enabled_agents(STATE.get("enabled_agents"))
        agent_settings = normalize_agent_settings(STATE.get("agent_settings"))
        discussion_project_access = normalize_discussion_project_access(
            STATE.get("discussion_project_access")
        )
        active_id = STATE.get("id", "")
    message_bubbles = "".join(bubble_html(m) for m in messages) if messages else \
        '<div class="empty-state">아직 대화가 없습니다.</div>'
    bubbles = (mode_notice_html(mode, discussion_project_access) if topic else "") + message_bubbles
    paused = CONTROL["paused"]
    stopped = CONTROL["stopped"]
    topic_section = topic_section_html(
        topic, mode, enabled_agents, active_id, agent_settings,
        discussion_project_access,
    )

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
        session_name = STATE.get("name") or topic or "새 세션"
        mode = STATE.get("mode", "discussion")
        active_id = STATE.get("id", "")
        enabled_agents = normalize_enabled_agents(STATE.get("enabled_agents"))
        agent_settings = normalize_agent_settings(STATE.get("agent_settings"))
        agent_roles = normalize_agent_roles(STATE.get("agent_roles"))
        discussion_project_access = normalize_discussion_project_access(
            STATE.get("discussion_project_access")
        )
        total_est_tokens = STATE.get("total_est_tokens", 0)
        total_actual_tokens = STATE.get("total_actual_tokens", 0)
        total_actual_cost_usd = STATE.get("total_actual_cost_usd", 0.0)
        budget = dict(STATE.get("budget") or {})
        total_elapsed_time = STATE.get("total_elapsed_time", 0.0)
        active_agent = STATE.get("active_agent")
        active_phase = STATE.get("active_phase")
        active_started_at = STATE.get("active_started_at")
        active_cli_mode = STATE.get("active_cli_mode")
        active_prompt_chars = STATE.get("active_prompt_chars", 0)
        runtime_events = list(STATE.get("runtime_events", []))[-20:]
        active_work_log = list(STATE.get("active_work_log", []))[-40:]
        active_usage = dict(STATE.get("active_usage", {}))
        validation_results = list(STATE.get("validation_results", []))
        delegation_history = list(STATE.get("delegation_history", []))[-20:]
        workspace_access = STATE.get("workspace_access", "write")
        step_index = int(STATE.get("step_index", 0))
        state_snapshot = dict(STATE)
    if active_id and not session_roles_path(active_id).exists():
        ensure_session_memory(state_snapshot)
    active_elapsed = max(0.0, round(time.time() - active_started_at, 1)) if active_started_at else 0.0
    message_bubbles = "".join(bubble_html(m) for m in messages) if messages else \
        '<div class="empty-state">아직 대화가 없습니다.</div>'
    bubbles = (mode_notice_html(mode, discussion_project_access) if topic else "") + message_bubbles
    disabled_agents = [a for a in AGENT_ORDER if a not in enabled_agents]
    last_model_message = next((message for message in reversed(messages) if message.get("agent") in AGENT_ORDER), None)
    can_retry = bool(
        CONTROL["paused"] and last_model_message and not (last_model_message.get("meta") or {}).get("ok", True)
    )
    can_continue_next = bool(
        CONTROL["paused"]
        and can_continue_after_failed_step(state_snapshot, messages)
        and not budget_exceeded_reason(state_snapshot)
    )
    return {
        "feed_html": bubbles,
        "topic_section_html": topic_section_html(
            topic, mode, enabled_agents, active_id, agent_settings, discussion_project_access
        ),
        "status": status_text(),
        "finished": finished,
        "paused": CONTROL["paused"],
        "stopped": CONTROL["stopped"],
        "awaiting_approval": CONTROL["awaiting_approval"],
        "approval_deferred": CONTROL["approval_deferred"],
        "approval_requested": CONTROL["approval_requested"],
        "approval_requested_by": list(CONTROL["approval_requested_by"]),
        "approval_rejected": CONTROL["approval_rejected"],
        "intervention_pending": CONTROL["intervention_pending"],
        "intervention_intent": CONTROL["intervention_intent"],
        "topic": html.escape(topic) if topic else "",
        "session_name": html.escape(session_name),
        "tags": list(STATE.get("tags", [])),
        "favorite": bool(STATE.get("favorite", False)),
        "archived": bool(STATE.get("archived", False)),
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "discussion_project_access": discussion_project_access,
        "discussion_project_access_label": discussion_project_access_label(discussion_project_access),
        "enabled_agents": enabled_agents,
        "agent_settings": agent_settings,
        "agent_roles": agent_roles,
        "role_labels": {agent: role_label(agent_roles[agent]) for agent in AGENT_ORDER},
        "role_catalog": [
            {"id": role_id, "label": role["label"], "summary": role["summary"], "scope": role["scope"], "can_write": role["can_write"]}
            for role_id, role in ROLE_CATALOG.items()
        ],
        "agent_setting_labels": {
            agent: agent_setting_label(agent, agent_settings) for agent in AGENT_ORDER
        },
        "enabled_agents_label": html.escape(agent_names(enabled_agents)),
        "disabled_agents_label": html.escape(agent_names(disabled_agents) if disabled_agents else "없음"),
        "conn_html": connection_status_html(),
        "active_id": active_id,
        "memory_dir": html.escape(str(session_memory_dir(active_id))) if active_id else "",
        "profile_path": html.escape(str(session_profile_path(active_id))) if active_id else "",
        "roles_path": html.escape(str(session_roles_path(active_id))) if active_id else "",
        "workspace_path": str(load_project_path()),
        "workspace_access": workspace_access,
        "workspace_access_label": "읽기·쓰기" if workspace_access == "write" else "읽기 전용",
        "total_est_tokens": total_est_tokens,
        "total_actual_tokens": total_actual_tokens,
        "total_actual_cost_usd": total_actual_cost_usd,
        "budget": budget,
        "budget_exceeded": "" if mode == "continuous" else budget_exceeded_reason({
            "budget": budget,
            "total_actual_tokens": total_actual_tokens,
            "total_actual_cost_usd": total_actual_cost_usd,
        }),
        "total_elapsed_time": round(total_elapsed_time, 1),
        "message_count": len(messages),
        "active_agent": active_agent,
        "active_phase": active_phase,
        "active_elapsed": active_elapsed,
        "active_cli_mode": active_cli_mode,
        "active_prompt_chars": active_prompt_chars,
        "runtime_events": runtime_events,
        "can_retry": can_retry,
        "can_continue_next": can_continue_next,
        "active_work_log": active_work_log,
        "active_usage": active_usage,
        "validation_results": validation_results,
        "delegation_history": delegation_history,
        "delegation_count": STATE.get("delegation_count", 0),
        "continuous_cycle": (
            (step_index // max(1, len(steps_for_mode("continuous", enabled_agents)))) + 1
            if mode == "continuous" else 0
        ),
        "agent_usage": agent_usage_summary(messages),
        "context_usage": agent_context_summary(messages, agent_settings),
    }


def sessions_list_payload() -> dict:
    with STATE_LOCK:
        active_id = STATE.get("id", "")
    items = []
    for s in list_sessions():
        items.append({
            **s,
            "topic": s["topic"] or "(주제 없음)",
            "name": s["name"],
            "mode_label": MODE_LABELS.get(s["mode"], s["mode"]),
            "is_active": s["id"] == active_id,
        })
    return {"sessions": items, "active_id": active_id}


def agent_usage_summary(messages: list[dict]) -> dict:
    summary = {
        agent: {
            "turns": 0,
            "estimated_tokens": 0,
            "prompt_chars": 0,
            "output_chars": 0,
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
        }
        for agent in AGENT_ORDER
    }
    for message in messages:
        agent = message.get("agent")
        if agent not in summary:
            continue
        meta = message.get("meta") or {}
        row = summary[agent]
        row["turns"] += 1
        row["estimated_tokens"] += int(meta.get("est_tokens", 0) or 0)
        row["prompt_chars"] += int(meta.get("prompt_chars", 0) or 0)
        row["output_chars"] += int(meta.get("output_chars", 0) or 0)
        usage = meta.get("actual_usage") or {}
        for key in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
            "cached_input_tokens", "output_tokens",
        ):
            row[key] += int(usage.get(key, 0) or 0)
        row["cost_usd"] += float(meta.get("actual_cost_usd", 0.0) or 0.0)
    return summary


def context_limit_for_agent(agent: str, agent_settings: dict | None = None) -> int:
    if agent == "codex":
        return CODEX_CONTEXT_TOKENS
    if agent == "claude":
        return CLAUDE_CONTEXT_TOKENS
    setting = normalize_agent_settings(agent_settings).get("antigravity", {})
    model = setting.get("model", "")
    if model.startswith("Claude ") or model.startswith("GPT-OSS"):
        return CLAUDE_CONTEXT_TOKENS
    return AGY_CONTEXT_TOKENS


def latest_context_token_count(agent: str, messages: list[dict]) -> tuple[int, bool]:
    for message in reversed(messages):
        if message.get("agent") != agent:
            continue
        meta = message.get("meta") or {}
        usage = meta.get("actual_usage") or {}
        if usage:
            if agent == "claude" and usage.get("iterations"):
                usage = usage["iterations"][-1]
            tokens = actual_token_count(usage)
            if tokens > 0:
                return tokens, False
        estimated = int(meta.get("est_tokens", 0) or 0)
        if estimated > 0:
            return estimated, True
    return 0, True


def agent_context_summary(
    messages: list[dict], agent_settings: dict | None = None
) -> dict[str, dict]:
    summary = {}
    for agent in AGENT_ORDER:
        used, estimated = latest_context_token_count(agent, messages)
        limit = max(1, context_limit_for_agent(agent, agent_settings))
        summary[agent] = {
            "used_tokens": used,
            "limit_tokens": limit,
            "percent": round((used / limit) * 100, 1),
            "estimated": estimated,
        }
    return summary


def session_detail_payload(session_id: str) -> dict | None:
    data = load_session(session_id)
    if data is None:
        return None
    messages = data.get("messages", [])
    with STATE_LOCK:
        is_active = STATE.get("id") == session_id
    mode = data.get("mode", "discussion")
    enabled_agents = normalize_enabled_agents(data.get("enabled_agents"))
    agent_settings = normalize_agent_settings(data.get("agent_settings"))
    agent_roles = normalize_agent_roles(data.get("agent_roles"))
    discussion_project_access = normalize_discussion_project_access(
        data.get("discussion_project_access")
    )
    message_bubbles = "".join(bubble_html(m) for m in messages) if messages else \
        '<div class="empty-state">아직 대화가 없습니다.</div>'
    bubbles = (
        mode_notice_html(mode, discussion_project_access) if data.get("topic") else ""
    ) + message_bubbles
    disabled_agents = [a for a in AGENT_ORDER if a not in enabled_agents]
    return {
        "id": session_id,
        "feed_html": bubbles,
        "topic_section_html": topic_section_html(
            data.get("topic", ""), mode, enabled_agents, session_id, agent_settings,
            discussion_project_access,
        ),
        "topic": html.escape(data.get("topic", "")),
        "session_name": html.escape(data.get("name") or data.get("topic") or "새 세션"),
        "tags": data.get("tags", []),
        "favorite": bool(data.get("favorite", False)),
        "archived": bool(data.get("archived", False)),
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "discussion_project_access": discussion_project_access,
        "discussion_project_access_label": discussion_project_access_label(discussion_project_access),
        "enabled_agents": enabled_agents,
        "agent_settings": agent_settings,
        "agent_roles": agent_roles,
        "role_labels": {agent: role_label(agent_roles[agent]) for agent in AGENT_ORDER},
        "role_catalog": [
            {"id": role_id, "label": role["label"], "summary": role["summary"], "scope": role["scope"], "can_write": role["can_write"]}
            for role_id, role in ROLE_CATALOG.items()
        ],
        "agent_setting_labels": {
            agent: agent_setting_label(agent, agent_settings) for agent in AGENT_ORDER
        },
        "enabled_agents_label": html.escape(agent_names(enabled_agents)),
        "disabled_agents_label": html.escape(agent_names(disabled_agents) if disabled_agents else "없음"),
        "memory_dir": html.escape(str(session_memory_dir(session_id))),
        "profile_path": html.escape(str(session_profile_path(session_id))),
        "roles_path": html.escape(str(session_roles_path(session_id))),
        "workspace_path": data.get("workspace_path") or str(load_project_path()),
        "workspace_access": data.get("workspace_access", "write"),
        "workspace_access_label": "읽기·쓰기" if data.get("workspace_access", "write") == "write" else "읽기 전용",
        "finished": data.get("finished", False),
        "is_active": is_active,
        "status": "완료" if data.get("finished", False) else "저장됨",
        "message_count": len(messages),
        "total_est_tokens": data.get("total_est_tokens", 0),
        "total_actual_tokens": data.get("total_actual_tokens", 0),
        "total_actual_cost_usd": data.get("total_actual_cost_usd", 0.0),
        "budget": data.get("budget", {"token_limit": 0, "cost_limit_usd": 0.0}),
        "total_elapsed_time": round(data.get("total_elapsed_time", 0.0), 1),
        "agent_usage": agent_usage_summary(messages),
        "context_usage": agent_context_summary(messages, agent_settings),
        "active_work_log": [],
        "active_usage": {},
        "validation_results": data.get("validation_results", []),
        "delegation_history": data.get("delegation_history", []),
        "delegation_count": data.get("delegation_count", 0),
        "continuous_cycle": (
            (int(data.get("step_index", 0)) // max(1, len(steps_for_mode("continuous", enabled_agents)))) + 1
            if mode == "continuous" else 0
        ),
    }


def profile_payload(session_id: str | None = None) -> dict | None:
    with STATE_LOCK:
        active_id = STATE.get("id", "")
        state_snapshot = dict(STATE)
    target_id = session_id or active_id
    if not target_id:
        return None
    if target_id == active_id:
        ensure_session_memory(state_snapshot)
    path = session_profile_path(target_id)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return {
        "session_id": target_id,
        "path": str(path),
        "content": content,
        "is_active": target_id == active_id,
    }


def rename_session(session_id: str, name: str) -> dict:
    clean_name = re.sub(r"\s+", " ", name).strip()[:80]
    if not clean_name:
        return {"error": "세션 이름을 입력해주세요."}
    with STATE_LOCK:
        if STATE.get("id") == session_id:
            STATE["name"] = clean_name
            save_state(STATE)
            return {"success": True, "id": session_id, "name": clean_name, "is_active": True}
    data = load_session(session_id)
    if data is None:
        return {"error": "세션을 찾을 수 없습니다."}
    data["name"] = clean_name
    path = SESSIONS_DIR / f"{session_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"success": True, "id": session_id, "name": clean_name, "is_active": False}


def update_session_metadata(session_id: str, tags: str, favorite: bool, archived: bool) -> dict:
    clean_tags = [item.strip()[:24] for item in tags.split(",") if item.strip()][:8]
    with STATE_LOCK:
        if STATE.get("id") == session_id:
            STATE.update(tags=clean_tags, favorite=favorite, archived=archived)
            save_state(STATE)
            return {"success": True, "id": session_id, "tags": clean_tags, "favorite": favorite, "archived": archived}
    data = load_session(session_id)
    if data is None:
        return {"error": "세션을 찾을 수 없습니다."}
    data.update(tags=clean_tags, favorite=favorite, archived=archived)
    (SESSIONS_DIR / f"{session_id}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"success": True, "id": session_id, "tags": clean_tags, "favorite": favorite, "archived": archived}


def delete_session(session_id: str) -> dict:
    with STATE_LOCK:
        if STATE.get("id") == session_id:
            return {"error": "현재 세션은 삭제할 수 없습니다. 새 세션을 만든 뒤 삭제해주세요."}
    removed = False
    for path in (SESSIONS_DIR / f"{session_id}.json", SESSIONS_DIR / f"{session_id}.md"):
        if path.exists():
            path.unlink()
            removed = True
    memory_path = session_memory_dir(session_id)
    if memory_path.exists():
        shutil.rmtree(memory_path)
        removed = True
    return {"success": removed, "id": session_id} if removed else {"error": "세션을 찾을 수 없습니다."}


def clone_session(session_id: str) -> dict:
    source = load_session(session_id)
    if source is None:
        return {"error": "세션을 찾을 수 없습니다."}
    cancel_active_cli_processes()
    clone = normalize_state(json.loads(json.dumps(source, ensure_ascii=False)))
    old_id = clone.get("id", session_id)
    clone["id"] = new_session_id()
    clone["created_at"] = datetime.now().isoformat(timespec="seconds")
    clone["name"] = f'{clone.get("name") or clone.get("topic") or "세션"} · 분기'[:80]
    clone["favorite"] = False
    clone["archived"] = False
    clone["active_agent"] = None
    clone["active_phase"] = None
    clone["active_started_at"] = None
    clone["active_cli_mode"] = None
    clone["active_prompt_chars"] = 0
    clone["active_work_log"] = []
    clone["active_usage"] = {}
    with STATE_LOCK:
        STATE.clear()
        STATE.update(clone)
        save_state(STATE)
    source_memory = session_memory_dir(old_id)
    target_memory = session_memory_dir(clone["id"])
    if source_memory.exists():
        shutil.copytree(source_memory, target_memory, dirs_exist_ok=True)
    CONTROL["paused"] = True
    CONTROL["stopped"] = False
    CONTROL["awaiting_approval"] = False
    CONTROL["approval_requested"] = False
    CONTROL["approval_requested_by"] = []
    return {"success": True, "id": clone["id"], "name": clone["name"]}


def activate_session(session_id: str) -> dict:
    with STATE_LOCK:
        if STATE.get("active_agent") or CONTROL["worker_running"]:
            return {"error": "모델이 실행 중일 때는 세션을 전환할 수 없습니다. 먼저 중단해주세요."}
        already_active = STATE.get("id") == session_id
    if already_active:
        payload = state_json_payload()
        payload["success"] = True
        payload["already_active"] = True
        return payload

    source = load_session(session_id)
    if source is None:
        return {"error": "세션을 찾을 수 없습니다."}

    activated = normalize_state(json.loads(json.dumps(source, ensure_ascii=False)))
    activated["active_agent"] = None
    activated["active_phase"] = None
    activated["active_started_at"] = None
    activated["active_cli_mode"] = None
    activated["active_prompt_chars"] = 0
    activated["active_work_log"] = []
    activated["active_usage"] = {}

    workspace = Path(activated.get("workspace_path") or load_project_path()).expanduser()
    if not workspace.is_dir() or not os.access(workspace, os.R_OK):
        return {"error": f"세션의 작업 폴더를 사용할 수 없습니다: {workspace}"}
    access = "write" if activated.get("mode") in {"coding", "continuous"} else activated.get("workspace_access", "read")
    if access == "write" and not os.access(workspace, os.W_OK):
        return {"error": f"세션의 작업 폴더에 쓰기 권한이 없습니다: {workspace}"}
    save_project_path(workspace.resolve())
    activated["workspace_path"] = str(workspace.resolve())
    activated["workspace_access"] = access

    pending_interventions = list(activated.get("pending_interventions", []))
    approval_requesters = (
        unresolved_approval_requesters(activated.get("messages", []))
        if not activated.get("finished", False)
        else []
    )
    with STATE_LOCK:
        STATE.clear()
        STATE.update(activated)
        save_state(STATE)
        CONTROL["paused"] = not activated.get("finished", False)
        CONTROL["stopped"] = False
        CONTROL["worker_running"] = False
        CONTROL["worker_session_id"] = None
        CONTROL["worker_start_pending"] = False
        CONTROL["awaiting_approval"] = bool(approval_requesters)
        CONTROL["approval_deferred"] = False
        CONTROL["approval_requested"] = bool(approval_requesters)
        CONTROL["approval_requested_by"] = approval_requesters
        CONTROL["approval_rejected"] = False
        CONTROL["approval_seen_messages"] = len(activated.get("messages", []))
        CONTROL["intervention_queue"] = pending_interventions
        CONTROL["intervention_pending"] = bool(pending_interventions)
        CONTROL["intervention_intent"] = pending_interventions[0].get("intent", "") if pending_interventions else ""
        CONTROL["intervention_seen_messages"] = len(activated.get("messages", []))

    add_runtime_event(f"세션 활성화: {activated.get('name') or activated.get('topic') or session_id}")
    payload = state_json_payload()
    payload["success"] = True
    return payload


def prompt_preview_payload(agent: str) -> dict:
    if agent not in AGENT_ORDER:
        return {"error": "모델을 선택해주세요."}
    with STATE_LOCK:
        state = dict(STATE)
        messages = list(STATE.get("messages", []))
        enabled = normalize_enabled_agents(STATE.get("enabled_agents"))
        steps = steps_for_mode(STATE.get("mode", "discussion"), enabled)
        step_index = int(STATE.get("step_index", 0))
    instruction = "현재 주제와 공유된 최신 발언을 검토하고, 자신의 역할에 맞는 다음 의견을 간결하게 정리해라."
    phase = "사용자 미리보기"
    preview_index = step_index % len(steps) if state.get("mode") == "continuous" and steps else step_index
    if preview_index < len(steps) and steps[preview_index][0] == agent:
        _agent, phase, instruction, cli_mode = steps[preview_index]
    else:
        cli_mode = "discussion"
    project_access = turn_project_access(cli_mode, state)
    transcript, shared = build_shared_transcript(messages, enabled)
    prompt = compose_agent_prompt(
        load_team_prompt(), state.get("topic", ""),
        build_memory_context(state, expose_file_paths=project_access != "none"), transcript, instruction,
        role_prompt(agent, state), project_access_prompt(project_access),
    )
    return {
        "agent": agent,
        "label": AGENTS[agent]["label"],
        "phase": phase,
        "prompt": prompt,
        "characters": len(prompt),
        "estimated_tokens": estimate_tokens(prompt),
        "shared_context": [AGENTS.get(message.get("agent"), {}).get("label", "사용자") for message in shared],
    }


def rollback_checkpoint_files(checkpoint: str, paths: list[str]) -> dict:
    try:
        checkpoint_path = Path(checkpoint).resolve()
        memory_root = MEMORY_DIR.resolve()
        if not checkpoint_path.is_relative_to(memory_root) or not checkpoint_path.is_file():
            return {"error": "유효한 체크포인트가 아닙니다."}
    except (OSError, ValueError):
        return {"error": "체크포인트 경로를 확인할 수 없습니다."}
    clean_paths = [path for path in paths if path and ".." not in Path(path).parts][:50]
    if not clean_paths:
        return {"error": "되돌릴 파일을 선택해주세요."}
    git = shutil.which("git") or "git"
    command = [git, "apply", "--reverse", "--whitespace=nowarn"]
    for path in clean_paths:
        command.append(f"--include={path}")
    command.append(str(checkpoint_path))
    result = subprocess.run(command, cwd=load_project_path(), capture_output=True)
    if result.returncode != 0:
        detail = decode_cli_output(result.stderr or result.stdout).strip()
        return {"error": f"변경을 되돌리지 못했습니다: {detail[:500]}"}
    add_runtime_event(f"체크포인트에서 {len(clean_paths)}개 파일 되돌림: {', '.join(clean_paths[:6])}")
    return {"success": True, "paths": clean_paths}


def save_profile_content(content: str, session_id: str | None = None) -> dict:
    with STATE_LOCK:
        active_id = STATE.get("id", "")
        state_snapshot = dict(STATE)
    target_id = session_id or active_id
    if not target_id:
        return {"error": "세션이 없습니다."}
    if target_id == active_id:
        ensure_session_memory(state_snapshot)
    path = session_profile_path(target_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    add_runtime_event(f"Profile.md 저장: {path}")
    return {"success": True, "session_id": target_id, "path": str(path), "content": content}


def switch_mode(mode: str) -> dict:
    if mode not in MODE_LABELS:
        return {"error": "알 수 없는 모드입니다."}
    if mode in {"coding", "continuous"} and not os.access(load_project_path(), os.W_OK):
        return {"error": "코딩 모드는 작업 폴더의 쓰기 권한이 필요합니다."}
    with STATE_LOCK:
        if STATE.get("active_agent"):
            return {"error": "현재 모델 응답이 끝난 뒤 모드를 전환해주세요."}
        previous_mode = STATE.get("mode", "discussion")
        enabled = normalize_enabled_agents(STATE.get("enabled_agents"))
        common_step_count = len([step for step in DISCUSSION_STEPS if step[0] in enabled])
        if previous_mode != mode:
            if mode == "continuous":
                STATE["step_index"] = 0
            elif previous_mode == "continuous":
                STATE["step_index"] = common_step_count if mode == "coding" else 0
            elif mode == "coding" and STATE.get("step_index", 0) >= common_step_count:
                STATE["step_index"] = common_step_count
            elif mode == "discussion" and STATE.get("step_index", 0) > common_step_count:
                STATE["step_index"] = common_step_count
        STATE["mode"] = mode
        STATE["continuous_stopped"] = False
        if mode in {"coding", "continuous"}:
            STATE["workspace_access"] = "write"
            STATE["discussion_project_access"] = "write"
        step_count = len(steps_for_mode(mode, enabled))
        STATE["finished"] = (
            mode != "continuous"
            and bool(STATE.get("topic"))
            and STATE.get("step_index", 0) >= step_count
        )
        if not STATE["finished"]:
            CONTROL["stopped"] = False
        CONTROL["awaiting_approval"] = False
        CONTROL["approval_deferred"] = False
        CONTROL["approval_requested"] = False
        CONTROL["approval_requested_by"] = []
        CONTROL["approval_rejected"] = False
        save_state(STATE)
        topic_exists = bool(STATE.get("topic"))
        should_start = topic_exists and not STATE.get("finished", False)
    add_runtime_event(f"세션 모드 전환: {MODE_LABELS[mode]}")
    if should_start:
        start_worker_if_needed(force=True)
    payload = state_json_payload()
    payload["success"] = True
    return payload


def prepare_manual_resume_after_startup() -> None:
    with STATE_LOCK:
        has_topic = bool(STATE.get("topic"))
        not_finished = not STATE.get("finished", False)
        if has_topic and not_finished:
            CONTROL["paused"] = True
            CONTROL["stopped"] = False


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
        parsed = urlparse(self.path)
        if parsed.path == "/version.json":
            with STATE_LOCK:
                messages_count = len(STATE["messages"])
                finished = STATE.get("finished", False)
                total_est_tokens = STATE.get("total_est_tokens", 0)
                total_elapsed_time = STATE.get("total_elapsed_time", 0.0)
                active_agent = STATE.get("active_agent")
                active_phase = STATE.get("active_phase")
                active_started_at = STATE.get("active_started_at")
                topic = STATE.get("topic", "")
                session_name = STATE.get("name", "")
                mode = STATE.get("mode", "")
            status = status_text()
            paused = CONTROL["paused"]
            stopped = CONTROL["stopped"]
            awaiting_approval = CONTROL["awaiting_approval"]
            approval_deferred = CONTROL["approval_deferred"]
            approval_requested = CONTROL["approval_requested"]
            approval_requested_by = tuple(CONTROL["approval_requested_by"])
            approval_rejected = CONTROL["approval_rejected"]
            intervention_pending = CONTROL["intervention_pending"]
            intervention_intent = CONTROL["intervention_intent"]
            active_tick = int(time.time()) if active_agent else 0
            version_str = (
                f"{messages_count}_{finished}_{total_est_tokens}_{total_elapsed_time}_"
                f"{active_agent}_{active_phase}_{active_started_at}_{topic}_{session_name}_{mode}_{status}_{paused}_{stopped}_"
                f"{awaiting_approval}_{approval_deferred}_{approval_requested}_{approval_requested_by}_"
                f"{approval_rejected}_{intervention_pending}_{intervention_intent}_"
                f"{len(CONNECTION_STATUS)}_{active_tick}"
            )
            self._send_json({"version": version_str})
            return

        if parsed.path.startswith("/static/"):
            rel_path = parsed.path.lstrip("/")
            static_file = (ROOT / rel_path).resolve()
            static_root = (ROOT / "static").resolve()
            try:
                in_static_root = static_file.is_relative_to(static_root)
            except AttributeError:
                in_static_root = str(static_file).startswith(str(static_root))
            if static_file.is_file() and in_static_root:
                ext = static_file.suffix.lower()
                content_type = "application/octet-stream"
                if ext == ".css":
                    content_type = "text/css; charset=utf-8"
                elif ext == ".js":
                    content_type = "application/javascript; charset=utf-8"
                elif ext == ".png":
                    content_type = "image/png"
                elif ext == ".jpg" or ext == ".jpeg":
                    content_type = "image/jpeg"
                elif ext == ".svg":
                    content_type = "image/svg+xml"

                try:
                    content = static_file.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                except OSError:
                    pass
            self.send_response(404)
            self.end_headers()
            return

        if parsed.path == "/state.json":
            self._send_json(state_json_payload())
            return
        if parsed.path == "/sessions.json":
            self._send_json(sessions_list_payload())
            return
        if parsed.path == "/session.json":
            session_id = parse_qs(parsed.query).get("id", [""])[0]
            payload = session_detail_payload(session_id)
            if payload is None:
                self.send_response(404)
                self.end_headers()
                return
            self._send_json(payload)
            return
        if parsed.path == "/profile.json":
            session_id = parse_qs(parsed.query).get("id", [""])[0] or None
            payload = profile_payload(session_id)
            if payload is None:
                self.send_response(404)
                self.end_headers()
                return
            self._send_json(payload)
            return
        if parsed.path == "/prompt-preview.json":
            agent = parse_qs(parsed.query).get("agent", ["codex"])[0]
            self._send_json(prompt_preview_payload(agent))
            return
        self._send_html(render_dashboard())

    def do_POST(self) -> None:
        if self.path == "/workspace/select":
            form = self._read_form()
            access = form.get("access", ["read"])[0]
            selected = choose_workspace_folder(load_project_path())
            if not selected:
                payload = state_json_payload()
                payload["cancelled"] = True
                self._send_json(payload)
                return
            self._send_json(update_workspace_selection(selected, access))
            return

        if self.path == "/workspace/access":
            form = self._read_form()
            access = form.get("access", ["read"])[0]
            self._send_json(update_workspace_selection(str(load_project_path()), access))
            return

        if self.path == "/topic":
            form = self._read_form()
            topic = form.get("topic", [""])[0].strip()
            mode = form.get("mode", ["discussion"])[0].strip()
            discussion_project_access = normalize_discussion_project_access(
                form.get("discussion_project_access", ["read"])[0].strip()
            )
            enabled_agents = normalize_enabled_agents(form.get("agent", []))
            agent_settings = normalize_agent_settings({
                agent: {
                    "model": form.get(f"model_{agent}", [""])[0].strip(),
                    "effort": form.get(f"effort_{agent}", [""])[0].strip(),
                }
                for agent in AGENT_ORDER
            })
            if mode not in MODE_LABELS:
                mode = "discussion"
            if topic:
                with STATE_LOCK:
                    STATE["topic"] = topic
                    if not STATE.get("name") or STATE.get("name") == "새 세션":
                        STATE["name"] = topic[:80]
                    STATE["mode"] = mode
                    STATE["continuous_stopped"] = False
                    STATE["discussion_project_access"] = (
                        "write" if mode in {"coding", "continuous"} else discussion_project_access
                    )
                    if mode in {"coding", "continuous"} or discussion_project_access == "write":
                        STATE["workspace_access"] = "write"
                    STATE["enabled_agents"] = enabled_agents
                    STATE["agent_settings"] = agent_settings
                    STATE["finished"] = False
                    CONTROL["approval_requested"] = False
                    CONTROL["approval_requested_by"] = []
                    CONTROL["approval_rejected"] = False
                    save_state(STATE)
                    state_snapshot = dict(STATE)
                start_log_session()
                write_session_transcript_header(state_snapshot)
                ensure_session_memory(state_snapshot)
                print(f"  \U0001f4cc 토론 주제: {topic} ({MODE_LABELS[mode]}, 활성: {agent_names(enabled_agents)})")
                start_worker_if_needed()
            self._send_html(render_dashboard())
            return

        if self.path == "/preflight":
            run_connection_checks()
            self._send_json(state_json_payload())
            return

        if self.path == "/mode":
            form = self._read_form()
            mode = form.get("mode", ["discussion"])[0].strip()
            self._send_json(switch_mode(mode))
            return

        if self.path == "/discussion-access":
            form = self._read_form()
            access = form.get("access", ["read"])[0].strip()
            self._send_json(update_discussion_project_access(access))
            return

        if self.path == "/roles":
            form = self._read_form()
            self._send_json(update_agent_roles({
                agent: form.get(f"role_{agent}", [""])[0].strip()
                for agent in AGENT_ORDER
            }))
            return

        if self.path == "/profile":
            form = self._read_form()
            session_id = form.get("id", [""])[0].strip() or None
            content = form.get("content", [""])[0]
            self._send_json(save_profile_content(content, session_id))
            return

        if self.path == "/session/name":
            form = self._read_form()
            session_id = form.get("id", [""])[0].strip()
            name = form.get("name", [""])[0]
            self._send_json(rename_session(session_id, name))
            return

        if self.path == "/session/meta":
            form = self._read_form()
            self._send_json(update_session_metadata(
                form.get("id", [""])[0].strip(),
                form.get("tags", [""])[0],
                form.get("favorite", ["false"])[0].lower() == "true",
                form.get("archived", ["false"])[0].lower() == "true",
            ))
            return

        if self.path == "/session/delete":
            form = self._read_form()
            self._send_json(delete_session(form.get("id", [""])[0].strip()))
            return

        if self.path == "/session/clone":
            form = self._read_form()
            self._send_json(clone_session(form.get("id", [""])[0].strip()))
            return

        if self.path == "/session/activate":
            form = self._read_form()
            self._send_json(activate_session(form.get("id", [""])[0].strip()))
            return

        if self.path == "/checkpoint/rollback":
            form = self._read_form()
            checkpoint = form.get("checkpoint", [""])[0]
            paths = form.get("path", [])
            self._send_json(rollback_checkpoint_files(checkpoint, paths))
            return

        if self.path == "/restart":
            cancel_active_cli_processes()
            with STATE_LOCK:
                STATE.clear()
                STATE.update(new_state())
                save_state(STATE)
            CONTROL["paused"] = False
            CONTROL["awaiting_approval"] = False
            CONTROL["approval_deferred"] = False
            CONTROL["approval_requested"] = False
            CONTROL["approval_requested_by"] = []
            CONTROL["approval_rejected"] = False
            CONTROL["approval_seen_messages"] = 0
            CONTROL["intervention_pending"] = False
            CONTROL["intervention_intent"] = ""
            CONTROL["intervention_seen_messages"] = 0
            CONTROL["intervention_queue"] = []
            CONTROL["worker_start_pending"] = False
            CONTROL["stopped"] = True  # 혹시 워커가 돌고 있었다면 다음 체크포인트에서 멈추게
            self._send_html(render_dashboard())
            return

        if self.path == "/message":
            form = self._read_form()
            text = form.get("text", [""])[0].strip()
            intent = form.get("intent", ["question"])[0].strip()
            source = form.get("source", ["composer"])[0].strip()
            with STATE_LOCK:
                current_mode = STATE.get("mode", "discussion")
            effective_intent = intent
            if intent == "redirect" and source == "composer" and current_mode in {"coding", "continuous"}:
                effective_intent = "execute"
            requested_targets = form.get("target")
            targets = normalize_target_agents(requested_targets)
            if effective_intent != "note":
                text, preferred_target = extract_target_prefix(text)
                if preferred_target:
                    preferred = normalize_target_agents([preferred_target])
                    if not preferred:
                        self._send_json({"error": f"[{preferred_target}] 모델이 현재 비활성 상태입니다."})
                        return
                    targets = preferred + [target for target in targets if target != preferred_target]
            spec = INTERVENTION_INTENTS.get(effective_intent, INTERVENTION_INTENTS["question"])
            if not text:
                self._send_json({"error": "메시지 내용이 비어 있습니다."})
                return
            if effective_intent != "note" and not targets:
                self._send_json({"error": "활성 상태인 대상 모델을 하나 이상 선택해주세요."})
                return

            with STATE_LOCK:
                budget_reason = budget_exceeded_reason(STATE)
            if effective_intent != "note" and budget_reason:
                self._send_json({"error": budget_block_message(budget_reason)})
                return

            try:
                target_suffix = f" → {agent_names(targets)}" if effective_intent != "note" else ""
                with STATE_LOCK:
                    initial_msg_count = len(STATE["messages"])

                add_message("user", spec["phase"] + target_suffix, text)

                with STATE_LOCK:
                    current_msg_count = len(STATE["messages"])
                    has_topic = bool(STATE.get("topic"))

                if current_msg_count <= initial_msg_count:
                    raise RuntimeError("대화 이력(STATE)에 메시지가 기록되지 않았습니다.")

                cli_mode = "coding" if effective_intent == "execute" else "discussion"
                mark_intervention(effective_intent, targets, cli_mode)
                if effective_intent != "note" and has_topic:
                    start_worker_if_needed(force=True)

                payload = state_json_payload()
                payload["success"] = True
                self._send_json(payload)
            except Exception as e:
                self._send_json({"error": f"백엔드 큐 등록 오류: {str(e)}"})
            return

        if self.path == "/pause":
            CONTROL["paused"] = True
            self._send_json(state_json_payload())
            return

        if self.path == "/resume":
            CONTROL["paused"] = False
            start_worker_if_needed(force=True)
            self._send_json(state_json_payload())
            return

        if self.path == "/stop":
            cancelled = cancel_active_cli_processes()
            CONTROL["stopped"] = True
            CONTROL["paused"] = False
            CONTROL["awaiting_approval"] = False
            CONTROL["approval_deferred"] = False
            CONTROL["approval_requested"] = False
            CONTROL["approval_requested_by"] = []
            CONTROL["intervention_pending"] = False
            CONTROL["intervention_intent"] = ""
            CONTROL["intervention_queue"] = []
            with STATE_LOCK:
                if STATE.get("mode") == "continuous":
                    STATE["continuous_stopped"] = True
                persist_intervention_queue_locked()
                save_state(STATE)
            if cancelled:
                add_runtime_event(f"실행 중 CLI 강제 종료: {', '.join(cancelled)}")
            self._send_json(state_json_payload())
            return

        if self.path == "/retry":
            if budget_exceeded_reason(STATE):
                self._send_json({"error": "예산을 먼저 늘린 뒤 재시도해주세요."})
                return
            CONTROL["paused"] = False
            CONTROL["stopped"] = False
            with STATE_LOCK:
                STATE["continuous_stopped"] = False
                save_state(STATE)
            add_runtime_event("실패한 턴을 다시 실행합니다.")
            start_worker_if_needed(force=True)
            self._send_json(state_json_payload())
            return

        if self.path == "/continue-next":
            self._send_json(continue_after_failed_step())
            return

        if self.path == "/budget":
            form = self._read_form()
            try:
                token_limit = max(0, int(form.get("token_limit", ["0"])[0] or 0))
                cost_limit = max(0.0, float(form.get("cost_limit_usd", ["0"])[0] or 0))
            except ValueError:
                self._send_json({"error": "예산 값을 숫자로 입력해주세요."})
                return
            with STATE_LOCK:
                was_exceeded = bool(budget_exceeded_reason(STATE))
                STATE["budget"] = {"token_limit": token_limit, "cost_limit_usd": cost_limit}
                should_resume = bool(
                    was_exceeded
                    and not budget_exceeded_reason(STATE)
                    and CONTROL["intervention_queue"]
                )
                if should_resume:
                    CONTROL["paused"] = False
                    CONTROL["stopped"] = False
                save_state(STATE)
            add_runtime_event(f"세션 예산 변경: 토큰 {token_limit:,}, 비용 ${cost_limit:.2f}")
            if should_resume:
                add_runtime_event("예산 차단이 해제되어 보류된 요청을 자동 재개합니다.")
                start_worker_if_needed(force=True)
            self._send_json(state_json_payload())
            return

        if self.path == "/approve":
            approve_pending_work()
            self._send_json(state_json_payload())
            return

        if self.path == "/reject":
            was_waiting = CONTROL["awaiting_approval"] or CONTROL["approval_requested"]
            if was_waiting:
                add_message("user", "승인 거절", "승인하지 않음")
                add_runtime_event("사용자가 승인을 거절해 진행을 중단했습니다.")
            CONTROL["awaiting_approval"] = False
            CONTROL["approval_deferred"] = False
            CONTROL["approval_requested"] = False
            CONTROL["approval_requested_by"] = []
            CONTROL["approval_rejected"] = True
            CONTROL["stopped"] = True
            CONTROL["paused"] = False
            CONTROL["intervention_pending"] = False
            CONTROL["intervention_intent"] = ""
            CONTROL["intervention_queue"] = []
            with STATE_LOCK:
                persist_intervention_queue_locked()
            self._send_json(state_json_payload())
            return

        if self.path == "/defer":
            if CONTROL["awaiting_approval"]:
                CONTROL["approval_deferred"] = True
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
    print("\U0001f680 TriAgent Control — Codex, Antigravity & Claude Code")
    print(f"   Codex: {command_label(CODEX_CMD)}")
    print(f"   Antigravity: {command_label(AGY_CMD)}")
    print(f"   Claude Code: {command_label(CLAUDE_CMD)}")
    ensure_team_prompt()
    print(f"   공통 지침: {TEAM_PROMPT_PATH}")
    ensure_project_path_file()
    print(f"   코딩 대상 폴더: {load_project_path()}  (바꾸려면 {PROJECT_PATH_FILE.name} 수정)")
    preflight()

    prepare_manual_resume_after_startup()
    render_html_snapshot()
    server, port = start_server()
    url = f"http://127.0.0.1:{port}/"
    print(f"\n\U0001f310 브라우저에서 모든 것을 통제하세요: {url}")
    print("   (주제 입력, 일시정지/재개, 메시지 개입, 중단 — 전부 이 페이지에서 합니다)")
    webbrowser.open(url)

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
