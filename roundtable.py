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
import locale
import math
import os
import re
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
PROFILE_PATHS = {
    "codex": ROOT / "CODEX_Profile.md",
    "antigravity": ROOT / "ANTIGRAVITY_Profile.md",
    "claude": ROOT / "CLAUDE_Profile.md",
}

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

APPROVAL_TOKEN = "APPROVE"

AGENTS = {
    "codex": {"label": "Codex", "color": "#4f8cff", "side": "left", "avatar": "/static/agents/codex.png"},
    "antigravity": {"label": "Antigravity", "color": "#a66cff", "side": "center", "avatar": "/static/agents/antigravity.png"},
    "claude": {"label": "Claude Code", "color": "#ff8a3d", "side": "right", "avatar": "/static/agents/claude.svg"},
    "user": {"label": "나 (개입)", "color": "#42c991", "side": "center", "avatar": "/static/agents/user.svg"},
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


AGENT_ORDER = ["codex", "antigravity", "claude"]


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
    discussion_steps = [step for step in DISCUSSION_STEPS if step[0] in enabled]
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
    skipped_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__"}
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


def run_cli(
    tool_name: str,
    command: str,
    args: list[str],
    *,
    timeout: int | None = None,
    cwd: Path | None = None,
    input_text: str | None = None,
):
    cmd = resolve_command(command) + args
    try:
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


def ask_codex(prompt: str, mode: str = "discussion") -> str:
    sandbox = "workspace-write" if mode == "coding" else "read-only"
    setting = selected_agent_setting("codex")
    args = ["exec"]
    if setting["model"]:
        args.extend(["--model", setting["model"]])
    if setting["effort"]:
        args.extend(["--config", f'model_reasoning_effort="{setting["effort"]}"'])
    args.extend(["--sandbox", sandbox, "--skip-git-repo-check", "-"])
    result = run_cli(
        "Codex",
        CODEX_CMD,
        args,
        timeout=CODEX_TIMEOUT,
        cwd=load_project_path(),
        input_text=prompt,
    )
    return _finalize_cli_result("Codex", result)


def ask_claude(prompt: str, mode: str = "discussion") -> str:
    permission_mode = "acceptEdits" if mode == "coding" else "plan"
    setting = selected_agent_setting("claude")
    args = ["--print"]
    if setting["model"]:
        args.extend(["--model", setting["model"]])
    if setting["effort"]:
        args.extend(["--effort", setting["effort"]])
    args.extend(["--permission-mode", permission_mode, prompt])
    result = run_cli(
        "Claude Code",
        CLAUDE_CMD,
        args,
        timeout=CLAUDE_TIMEOUT,
        cwd=load_project_path(),
    )
    return _finalize_cli_result("Claude Code", result)


def ask_antigravity(prompt: str, mode: str = "discussion") -> str:
    # agy는 Go 스타일 플래그라 --print 자체가 프롬프트 값을 먹는다. 반드시 마지막에 와야 함.
    # subprocess의 cwd만으로는 agy가 프로젝트 폴더를 인식하지 못하고 자기 내부의 기본
    # scratch 워크스페이스를 본다 — --add-dir로 명시적으로 작업 폴더를 알려줘야 한다.
    project_path = load_project_path()
    base_args = ["--add-dir", str(project_path)]
    setting = selected_agent_setting("antigravity")
    if setting["model"]:
        base_args.extend(["--model", setting["model"]])
    if mode == "coding":
        args = base_args + ["--mode", "accept-edits", "--print", prompt]
    else:
        args = base_args + ["--mode", "plan", "--sandbox", "--print", prompt]
    result = run_cli("Antigravity", AGY_CMD, args, timeout=AGY_TIMEOUT, cwd=project_path)
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
        "messages": [],
        "step_index": 0,
        "finished": False,
        "topic": "",
        "mode": "discussion",
        "enabled_agents": list(AGENT_ORDER),
        "agent_settings": normalize_agent_settings(None),
        "total_est_tokens": 0,
        "total_elapsed_time": 0.0,
        "active_agent": None,
        "active_phase": None,
        "active_started_at": None,
        "active_cli_mode": None,
        "active_prompt_chars": 0,
        "runtime_events": [],
    }


def normalize_state(state: dict) -> dict:
    state.setdefault("id", new_session_id())
    state.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    state.setdefault("messages", [])
    state.setdefault("step_index", 0)
    state.setdefault("finished", False)
    state.setdefault("topic", "")
    state.setdefault("mode", "discussion")
    state.setdefault("total_est_tokens", 0)
    state.setdefault("total_elapsed_time", 0.0)
    state.setdefault("active_agent", None)
    state.setdefault("active_phase", None)
    state.setdefault("active_started_at", None)
    state.setdefault("active_cli_mode", None)
    state.setdefault("active_prompt_chars", 0)
    state.setdefault("runtime_events", [])
    state["enabled_agents"] = normalize_enabled_agents(state.get("enabled_agents"))
    state["agent_settings"] = normalize_agent_settings(state.get("agent_settings"))
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
            "mode": data.get("mode", "discussion"),
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
    path.write_text(
        f"# 세션 기록 — {state.get('topic', '')}\n\n"
        f"- 세션 ID: {state['id']}\n"
        f"- 모드: {mode_label}\n"
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
    enabled = normalize_enabled_agents(state.get("enabled_agents"))
    disabled = [a for a in AGENT_ORDER if a not in enabled]
    header = (
        f"# Roundtable Memory — {state.get('topic', '')}\n\n"
        f"- 세션 ID: {session_id}\n"
        f"- 모드: {mode_label}\n"
        f"- 활성 에이전트: {agent_names(enabled)}\n"
        f"- 비활성 에이전트: {agent_names(disabled) if disabled else '없음'}\n"
        f"- 시작: {state.get('created_at', '')}\n"
    )
    if not full_path.exists():
        full_path.write_text(header, encoding="utf-8")
    if not brief_path.exists():
        brief_path.write_text(header + "\n## 압축 요약\n", encoding="utf-8")
    profile_path = session_profile_path(session_id)
    if not profile_path.exists():
        profile_path.write_text(SESSION_PROFILE_TEMPLATE, encoding="utf-8")
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


def build_memory_context(state: dict) -> str:
    session_id = state.get("id", "")
    if not session_id:
        return ""
    ensure_session_memory(state)
    full_path, brief_path = session_memory_paths(session_id)
    profile_path = session_profile_path(session_id)
    enabled = normalize_enabled_agents(state.get("enabled_agents"))
    disabled = [a for a in AGENT_ORDER if a not in enabled]
    brief_tail = read_brief_tail(session_id)
    lines = [
        "세션 외부 메모리 (필요할 때만 파일을 직접 읽기):",
        f"- 전체 기록 파일: {full_path}",
        f"- 압축 요약 파일: {brief_path}",
        f"- 세션 역할 프로필: {profile_path}",
        f"- 활성 에이전트: {agent_names(enabled)}",
        f"- 비활성 에이전트: {agent_names(disabled) if disabled else '없음'}",
        "",
        "규칙:",
        "- 전체 기록과 Profile.md 전문은 현재 프롬프트에 포함되지 않았다.",
        "- 역할 결정이나 과거 근거가 꼭 필요할 때만 해당 파일을 직접 읽는다.",
        "- 역할이 바뀌면 응답에 명확히 남겨라. 시스템이 이 세션 프로필에 기록한다.",
        "- 비활성 에이전트는 이번 세션에서 발언/작업하지 않는다는 점을 고려한다.",
    ]
    if brief_tail:
        lines.extend(["", "짧은 세션 요약:", brief_tail])
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


def compose_agent_prompt(
    team_prompt: str,
    topic: str,
    memory_context: str,
    transcript: str,
    instruction: str,
) -> str:
    team_prompt = team_prompt[:TEAM_PROMPT_MAX_CHARS]
    topic = topic[:500]
    instruction = instruction[:1200]
    header = (
        "모든 사용자 노출 문장은 한국어로 쓴다. 파일명·명령어·코드 식별자만 원문을 유지한다.\n"
        "도구 실행 계획과 탐색 과정을 출력하지 말고 결론만 최대 6개 항목, 1,200자 이내로 답한다.\n"
        f"사용자 승인이 꼭 필요하면 설명을 마친 뒤 마지막 줄에 {APPROVAL_TOKEN}만 쓴다. "
        f"승인이 필요하지 않으면 {APPROVAL_TOKEN}를 쓰지 않는다.\n\n"
    )
    current_task = f"\n\n현재 주제: {topic}\n현재 작업: {instruction}\n"
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
CONTROL = {
    "paused": False,
    "stopped": False,
    "worker_running": False,
    "worker_session_id": None,
    "worker_start_pending": False,
    "awaiting_approval": False,
    "approval_deferred": False,
    "approval_requested": False,
    "approval_requested_by": [],
    "approval_rejected": False,
    "approval_seen_messages": 0,
    "intervention_pending": False,
    "intervention_seen_messages": 0,
    "intervention_intent": "",
    "intervention_queue": [],
}


def unresolved_approval_requesters(messages: list[dict]) -> list[str]:
    requesters: list[str] = []
    for message in reversed(messages):
        if message.get("agent") == "user" and message.get("phase") in {"승인", "승인 거절"}:
            break
        if not message.get("meta", {}).get("approval_requested"):
            continue
        agent = message.get("agent", "")
        label = AGENTS.get(agent, {}).get("label", agent or "에이전트")
        requester = f"{label} · {message.get('phase', '승인 요청')}"
        if requester not in requesters:
            requesters.append(requester)
    requesters.reverse()
    return requesters


_restored_approval_requesters = unresolved_approval_requesters(STATE.get("messages", []))
if _restored_approval_requesters:
    CONTROL["approval_requested"] = True
    CONTROL["approval_requested_by"] = _restored_approval_requesters
    CONTROL["awaiting_approval"] = True


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
    highlight_class = " report" if phase == "최종 보고" else (" confirm" if phase == CONFIRM_PHASE else "")

    meta = m.get("meta")
    stats_html = ""
    if meta:
        changed_paths = meta.get("changed_paths", [])
        changes_html = f' · 파일 변경 {len(changed_paths)}개' if meta.get("cli_mode") == "coding" else ""
        stats_html = (
            f'<div class="stats">⏱ {meta["elapsed"]}초 · '
            f'추정 토큰 ~{meta["est_tokens"]} · '
            f'입력 {meta["prompt_chars"]}자/출력 {meta["output_chars"]}자 · '
            f'{html.escape(meta["cli_mode"])}{changes_html}</div>'
        )

    return f"""
    <div class="row {side}{highlight_class}">
      <div class="bubble" style="--accent:{agent['color']}">
        <div class="meta">
          <img class="avatar" src="{html.escape(agent.get('avatar', ''))}" alt="">
          <span class="name">{html.escape(agent['label'])}</span>
          <span class="phase">{html.escape(m.get('phase', ''))}</span>
          <span class="time">{html.escape(m['time'])}</span>
        </div>
        <div class="text">{render_text_html(m['text'])}</div>
        {stats_html}
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
    return "진행 중..."


def render_html_snapshot() -> None:
    """참고용 정적 스냅샷 파일 (roundtable.html). 실제 화면은 서버가 동적으로 서빙한다."""
    HTML_PATH.write_text(render_dashboard(), encoding="utf-8")


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
        STATE["messages"].append(message)
        save_state(STATE)
        session_id = STATE["id"]
    append_log(agent, phase, text)
    append_session_transcript(session_id, agent, phase, text, meta)
    append_memory(session_id, agent, phase, text, meta)
    append_session_role_profile(session_id, agent, phase, text)
    render_html_snapshot()
    return True


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


def mark_intervention(intent: str, targets: list[str] | None = None) -> None:
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
        })


def process_pending_intervention(expected_session_id: str | None = None) -> bool:
    with STATE_LOCK:
        if expected_session_id and STATE.get("id") != expected_session_id:
            return False
    if not CONTROL["intervention_pending"]:
        return False
    with STATE_LOCK:
        item = CONTROL["intervention_queue"].pop(0) if CONTROL["intervention_queue"] else None
        CONTROL["intervention_pending"] = bool(CONTROL["intervention_queue"])
        CONTROL["intervention_intent"] = CONTROL["intervention_queue"][0]["intent"] if CONTROL["intervention_queue"] else ""
    if not item:
        return False
    intent = item["intent"] if item["intent"] in INTERVENTION_INTENTS else "question"
    spec = INTERVENTION_INTENTS[intent]
    targets = normalize_target_agents(item.get("targets"))
    add_runtime_event(f"사용자 개입 처리 시작: {spec['label']} → {agent_names(targets)}")
    for responder in targets:
        target_instruction = (
            f"{spec['instruction']}\n\n"
            f"이번 개입의 직접 대상 모델: {agent_names(targets)}.\n"
            f"너({AGENTS[responder]['label']})에게 직접 질문/지시가 왔다고 보고 답해라."
        )
        if not run_step(
            responder,
            "개입 답변",
            target_instruction,
            "discussion",
            expected_session_id=expected_session_id,
        ):
            CONTROL["paused"] = True
            return True
    with STATE_LOCK:
        CONTROL["intervention_seen_messages"] = len(STATE["messages"])
    if spec["pause_after"]:
        CONTROL["paused"] = True
        separator("사용자 개입 처리 완료 — 재개 버튼을 누르면 이어서 진행됩니다")
    return True


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
    return "응답 없음" in text or "빈 응답" in text


def run_step(
    agent: str,
    phase: str,
    instruction: str,
    cli_mode: str,
    *,
    expected_session_id: str | None = None,
) -> bool:
    label = AGENTS[agent]["label"]
    separator(f"{label} — {phase}")
    with STATE_LOCK:
        if expected_session_id and STATE.get("id") != expected_session_id:
            return False
        session_id = STATE.get("id")
        state_snapshot = dict(STATE)
        topic = STATE.get("topic", "").strip()
        transcript = build_transcript(STATE["messages"], window=TRANSCRIPT_WINDOW)
    memory_context = build_memory_context(state_snapshot)
    team_prompt = load_team_prompt()
    prompt = compose_agent_prompt(team_prompt, topic, memory_context, transcript, instruction)

    with STATE_LOCK:
        if expected_session_id and STATE.get("id") != expected_session_id:
            return False
        STATE["active_agent"] = agent
        STATE["active_phase"] = phase
        STATE["active_started_at"] = time.time()
        STATE["active_cli_mode"] = cli_mode
        STATE["active_prompt_chars"] = len(prompt)
        save_state(STATE)
    add_runtime_event(f"{label} — {phase} 시작 (cli_mode={cli_mode}, 입력 {len(prompt)}자)")
    render_html_snapshot()
    print(f"\U0001f914 {label} 생각 중... (입력 약 {len(prompt)}자, cli_mode={cli_mode})")
    project_path = load_project_path()
    before_snapshot, before_truncated = (
        snapshot_project_tree(project_path) if cli_mode == "coding" else ({}, False)
    )
    t0 = time.time()
    try:
        raw_text = ASK_FUNCS[agent](prompt, cli_mode)
    except Exception as exc:
        raw_text = f"({label} 응답 없음 — 실행 중 예외: {exc})"
    elapsed = time.time() - t0
    after_snapshot, after_truncated = (
        snapshot_project_tree(project_path) if cli_mode == "coding" else ({}, False)
    )
    changed_paths = compare_project_snapshots(before_snapshot, after_snapshot) if cli_mode == "coding" else []
    visible_text, approval_requested = extract_approval_token(raw_text)
    text, output_truncated = clip_agent_output(visible_text)
    if approval_requested and not text:
        text = "사용자 승인을 요청했습니다."
    est_tokens = estimate_tokens(prompt, raw_text)
    failed = is_cli_failure(raw_text)
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
        "changed_paths": changed_paths[:100],
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
    if approval_requested:
        mark_approval_requested(agent, phase)
        add_runtime_event(f"승인 요청 감지: {label} · {phase}")
    if cli_mode == "coding":
        if changed_paths:
            preview = ", ".join(f"{item['change']} {item['path']}" for item in changed_paths[:6])
            suffix = " ..." if len(changed_paths) > 6 else ""
            add_runtime_event(f"파일 변경 감지 {len(changed_paths)}개: {preview}{suffix}")
        else:
            add_runtime_event("파일 변경 감지: 없음")
    added = add_message(agent, phase, text, meta, expected_session_id=session_id)
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
            if CONTROL["approval_requested"]:
                if not wait_for_user_approval(session_id):
                    break
                continue
            if process_pending_intervention(session_id):
                continue
            if step_index >= len(steps):
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

            agent, phase, instruction, cli_mode = steps[step_index]
            succeeded = run_step(
                agent,
                phase,
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
                save_state(STATE)

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
                finished = STATE["step_index"] >= len(steps) and not CONTROL["stopped"]
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


MODE_LABELS = {"discussion": "일반 토론 모드", "coding": "코딩 모드"}


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
) -> str:
    project_path_line = (
        f'<p>코딩 모드 작업 경로: '
        f'{html.escape(str(load_project_path()))} '
        f'(<code>{html.escape(PROJECT_PATH_FILE.name)}</code>에서 변경)</p>'
    )

    if topic:
        mode_label = MODE_LABELS.get(mode, mode)
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
            f'<p>저장 폴더: {html.escape(str(session_memory_dir(active_id)))}</p>'
            f'{project_path_line if mode == "coding" else ""}</div>'
        )

    return f"""
      <div class="panel">
        <p style="margin:0;color:var(--muted);font-size:13px">토론 주제를 입력하면 바로 시작합니다.</p>
        <textarea id="topicInput" placeholder="예: 네이버 쇼핑 최저가 비교 웹앱을 같이 만들 거야" autofocus></textarea>
        <div class="mode-choice">
          <label><input type="radio" name="mode" value="discussion" checked> 일반 토론 모드 (읽기 전용, 강점/역할 논의)</label>
          <label><input type="radio" name="mode" value="coding"> 코딩 모드 (실제로 이 폴더 코드를 수정)</label>
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
        active_id = STATE.get("id", "")
    bubbles = "".join(bubble_html(m) for m in messages) if messages else \
        '<div class="empty-state">아직 대화가 없습니다.</div>'
    paused = CONTROL["paused"]
    stopped = CONTROL["stopped"]
    topic_section = topic_section_html(topic, mode, enabled_agents, active_id, agent_settings)

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
        active_id = STATE.get("id", "")
        enabled_agents = normalize_enabled_agents(STATE.get("enabled_agents"))
        agent_settings = normalize_agent_settings(STATE.get("agent_settings"))
        total_est_tokens = STATE.get("total_est_tokens", 0)
        total_elapsed_time = STATE.get("total_elapsed_time", 0.0)
        active_agent = STATE.get("active_agent")
        active_phase = STATE.get("active_phase")
        active_started_at = STATE.get("active_started_at")
        active_cli_mode = STATE.get("active_cli_mode")
        active_prompt_chars = STATE.get("active_prompt_chars", 0)
        runtime_events = list(STATE.get("runtime_events", []))[-20:]
    active_elapsed = max(0.0, round(time.time() - active_started_at, 1)) if active_started_at else 0.0
    bubbles = "".join(bubble_html(m) for m in messages) if messages else \
        '<div class="empty-state">아직 대화가 없습니다.</div>'
    disabled_agents = [a for a in AGENT_ORDER if a not in enabled_agents]
    return {
        "feed_html": bubbles,
        "topic_section_html": topic_section_html(topic, mode, enabled_agents, active_id, agent_settings),
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
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "enabled_agents": enabled_agents,
        "agent_settings": agent_settings,
        "agent_setting_labels": {
            agent: agent_setting_label(agent, agent_settings) for agent in AGENT_ORDER
        },
        "enabled_agents_label": html.escape(agent_names(enabled_agents)),
        "disabled_agents_label": html.escape(agent_names(disabled_agents) if disabled_agents else "없음"),
        "conn_html": connection_status_html(),
        "active_id": active_id,
        "memory_dir": html.escape(str(session_memory_dir(active_id))) if active_id else "",
        "profile_path": html.escape(str(session_profile_path(active_id))) if active_id else "",
        "total_est_tokens": total_est_tokens,
        "total_elapsed_time": round(total_elapsed_time, 1),
        "message_count": len(messages),
        "active_agent": active_agent,
        "active_phase": active_phase,
        "active_elapsed": active_elapsed,
        "active_cli_mode": active_cli_mode,
        "active_prompt_chars": active_prompt_chars,
        "runtime_events": runtime_events,
    }


def sessions_list_payload() -> dict:
    with STATE_LOCK:
        active_id = STATE.get("id", "")
    items = []
    for s in list_sessions():
        items.append({
            **s,
            "topic": s["topic"] or "(주제 없음)",
            "mode_label": MODE_LABELS.get(s["mode"], s["mode"]),
            "is_active": s["id"] == active_id,
        })
    return {"sessions": items, "active_id": active_id}


def session_detail_payload(session_id: str) -> dict | None:
    data = load_session(session_id)
    if data is None:
        return None
    messages = data.get("messages", [])
    bubbles = "".join(bubble_html(m) for m in messages) if messages else \
        '<div class="empty-state">아직 대화가 없습니다.</div>'
    with STATE_LOCK:
        is_active = STATE.get("id") == session_id
    mode = data.get("mode", "discussion")
    enabled_agents = normalize_enabled_agents(data.get("enabled_agents"))
    agent_settings = normalize_agent_settings(data.get("agent_settings"))
    disabled_agents = [a for a in AGENT_ORDER if a not in enabled_agents]
    return {
        "id": session_id,
        "feed_html": bubbles,
        "topic_section_html": topic_section_html(
            data.get("topic", ""), mode, enabled_agents, session_id, agent_settings
        ),
        "topic": html.escape(data.get("topic", "")),
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "enabled_agents": enabled_agents,
        "agent_settings": agent_settings,
        "agent_setting_labels": {
            agent: agent_setting_label(agent, agent_settings) for agent in AGENT_ORDER
        },
        "enabled_agents_label": html.escape(agent_names(enabled_agents)),
        "disabled_agents_label": html.escape(agent_names(disabled_agents) if disabled_agents else "없음"),
        "memory_dir": html.escape(str(session_memory_dir(session_id))),
        "profile_path": html.escape(str(session_profile_path(session_id))),
        "finished": data.get("finished", False),
        "is_active": is_active,
        "status": "완료" if data.get("finished", False) else "저장됨",
        "message_count": len(messages),
        "total_est_tokens": data.get("total_est_tokens", 0),
        "total_elapsed_time": round(data.get("total_elapsed_time", 0.0), 1),
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
    with STATE_LOCK:
        if STATE.get("active_agent"):
            return {"error": "현재 모델 응답이 끝난 뒤 모드를 전환해주세요."}
        previous_mode = STATE.get("mode", "discussion")
        enabled = normalize_enabled_agents(STATE.get("enabled_agents"))
        common_step_count = len([step for step in DISCUSSION_STEPS if step[0] in enabled])
        if previous_mode != mode:
            if mode == "coding" and STATE.get("step_index", 0) >= common_step_count:
                STATE["step_index"] = common_step_count
            elif mode == "discussion" and STATE.get("step_index", 0) > common_step_count:
                STATE["step_index"] = common_step_count
        STATE["mode"] = mode
        step_count = len(steps_for_mode(mode, enabled))
        STATE["finished"] = bool(STATE.get("topic")) and STATE.get("step_index", 0) >= step_count
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


def maybe_autostart_worker() -> None:
    with STATE_LOCK:
        has_topic = bool(STATE.get("topic"))
        not_finished = not STATE.get("finished", False)
    if CONTROL["intervention_pending"] and has_topic:
        start_worker_if_needed(force=True)
    elif has_topic and not_finished and not CONTROL["stopped"]:
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
                f"{active_agent}_{active_phase}_{active_started_at}_{topic}_{mode}_{status}_{paused}_{stopped}_"
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
        maybe_autostart_worker()
        self._send_html(render_dashboard())

    def do_POST(self) -> None:
        if self.path == "/topic":
            form = self._read_form()
            topic = form.get("topic", [""])[0].strip()
            mode = form.get("mode", ["discussion"])[0].strip()
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
                    STATE["mode"] = mode
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

        if self.path == "/profile":
            form = self._read_form()
            session_id = form.get("id", [""])[0].strip() or None
            content = form.get("content", [""])[0]
            self._send_json(save_profile_content(content, session_id))
            return

        if self.path == "/restart":
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
            requested_targets = form.get("target")
            targets = normalize_target_agents(requested_targets)
            spec = INTERVENTION_INTENTS.get(intent, INTERVENTION_INTENTS["question"])
            if not text:
                self._send_json({"error": "메시지 내용이 비어 있습니다."})
                return
            if intent != "note" and not targets:
                self._send_json({"error": "활성 상태인 대상 모델을 하나 이상 선택해주세요."})
                return

            try:
                target_suffix = f" → {agent_names(targets)}" if intent != "note" else ""
                with STATE_LOCK:
                    initial_msg_count = len(STATE["messages"])

                add_message("user", spec["phase"] + target_suffix, text)

                with STATE_LOCK:
                    current_msg_count = len(STATE["messages"])
                    has_topic = bool(STATE.get("topic"))

                if current_msg_count <= initial_msg_count:
                    raise RuntimeError("대화 이력(STATE)에 메시지가 기록되지 않았습니다.")

                mark_intervention(intent, targets)
                if intent != "note" and has_topic:
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
            CONTROL["stopped"] = True
            CONTROL["paused"] = False
            CONTROL["awaiting_approval"] = False
            CONTROL["approval_deferred"] = False
            CONTROL["approval_requested"] = False
            CONTROL["approval_requested_by"] = []
            CONTROL["intervention_pending"] = False
            CONTROL["intervention_intent"] = ""
            CONTROL["intervention_queue"] = []
            self._send_json(state_json_payload())
            return

        if self.path == "/approve":
            was_waiting = CONTROL["awaiting_approval"] or CONTROL["approval_requested"]
            if was_waiting:
                add_message("user", "승인", "승인")
                add_runtime_event("사용자가 진행을 승인했습니다.")
            CONTROL["awaiting_approval"] = False
            CONTROL["approval_deferred"] = False
            CONTROL["approval_requested"] = False
            CONTROL["approval_requested_by"] = []
            CONTROL["approval_rejected"] = False
            CONTROL["intervention_pending"] = False
            CONTROL["intervention_intent"] = ""
            CONTROL["intervention_queue"] = []
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
