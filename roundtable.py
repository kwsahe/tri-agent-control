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
import traceback
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from triagent.domain import clicheck, runstatus, usage as usage_domain, validation, workflow
from triagent.domain.workspace import (
    WorkspaceStatus,
    parse_project_path_file,
    resolve_workspace,
    workspace_matches,
)
from triagent.security import webguard
from triagent.security.paths import (
    UnsafePathError,
    UnsafeSessionIdError,
    is_valid_session_id,
    safe_child,
    validate_session_id,
)

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


def run_process_tree(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
    active_name: str | None = None,
) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output=True)과 같지만 시간 초과 시 프로세스 트리 전체를 끝낸다.

    subprocess.run은 직계 자식만 죽인 뒤 communicate()로 파이프가 닫히길 끝없이 기다린다.
    npm.cmd(cmd.exe) → node처럼 손자가 파이프를 물고 있으면 워커가 영원히 멈춘다.
    ``active_name``을 주면 실행 중에 /stop이 이 프로세스 트리를 끝낼 수 있다.
    """
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE if input_bytes is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if active_name:
        with ACTIVE_PROCESS_LOCK:
            ACTIVE_PROCESSES[active_name] = process
    try:
        stdout, stderr = process.communicate(input_bytes, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        terminate_process_tree(process)
        try:
            exc.output, exc.stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # 트리 밖으로 빠져나간 손자가 아직 파이프를 쥐고 있다 — 출력 없이 포기한다.
        raise exc
    finally:
        if active_name:
            with ACTIVE_PROCESS_LOCK:
                if ACTIVE_PROCESSES.get(active_name) is process:
                    ACTIVE_PROCESSES.pop(active_name, None)
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)

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


class WorkspaceUnavailable(RuntimeError):
    """작업 폴더가 유효하지 않아 실행을 진행할 수 없다."""

    def __init__(self, resolution) -> None:
        super().__init__(resolution.message)
        self.resolution = resolution
        self.status = resolution.status
        self.configured = resolution.configured


# 작업공간 경고를 상태 조회마다 반복 출력하지 않기 위한 기억. 상태가 바뀔 때만 찍는다.
_WORKSPACE_WARNING_STATE: dict[str, object] = {"signature": "", "seen": set()}


def read_configured_project_path() -> str:
    """PROJECT_PATH.txt에 적힌 경로 원문을 돌려준다 (검증하지 않음)."""
    ensure_project_path_file()
    try:
        return parse_project_path_file(PROJECT_PATH_FILE.read_text(encoding="utf-8"))
    except OSError:
        return ""


def resolve_project_workspace(require_write: bool = False):
    """현재 설정된 작업공간을 해석한다. 실패하면 대체하지 않고 차단 상태를 돌려준다.

    예전에는 경로가 없으면 조용히 ROOT(=TriAgent 코드 폴더)로 바꿔서, 세션에는 옛
    프로젝트가 적혀 있는데 실제 실행은 이 도구 폴더에서 일어날 수 있었다.
    이제는 fail closed — 유효한 폴더를 다시 고를 때까지 읽기도 쓰기도 막는다.
    """
    resolution = resolve_workspace(read_configured_project_path(), require_write=require_write)

    # 경고는 상태가 바뀔 때 한 번만 남긴다 (폴링 때문에 무한 반복되던 문제).
    # require_write는 호출부마다 달라서 서명에 넣으면 두 값이 번갈아 찍힌다.
    # 상태(status)와 설정 경로가 바뀔 때만 한 번 남긴다.
    signature = f"{resolution.status}:{resolution.configured}"
    seen = _WORKSPACE_WARNING_STATE["seen"]
    if resolution.blocked:
        if signature not in seen:
            seen.add(signature)
            _WORKSPACE_WARNING_STATE["signature"] = signature
            print(f"  ⚠️  작업 폴더를 사용할 수 없습니다 — {resolution.message}")
            print("      대시보드에서 '작업 폴더 선택'으로 유효한 폴더를 지정할 때까지 모델 실행이 차단됩니다.")
    elif _WORKSPACE_WARNING_STATE["signature"]:
        # 정상으로 돌아왔으면 다음 차단 때 다시 알릴 수 있도록 기억을 비운다.
        _WORKSPACE_WARNING_STATE["signature"] = ""
        seen.clear()
    return resolution


def load_project_path() -> Path:
    """유효한 작업 폴더를 돌려준다. 없으면 :class:`WorkspaceUnavailable`.

    호출부가 예외를 잡지 않고 그냥 쓰던 자리를 위해, 읽기 전용 맥락에서는
    :func:`project_path_or_none`을 쓰는 편이 안전하다.
    """
    resolution = resolve_project_workspace()
    if resolution.blocked or resolution.path is None:
        raise WorkspaceUnavailable(resolution)
    return resolution.path


def project_path_or_none() -> Path | None:
    resolution = resolve_project_workspace()
    return resolution.path if resolution.ok else None


def workspace_block_message(resolution) -> str:
    """차단 사유 + 원래 저장된 경로 + 다음 행동을 한 문단으로 정리한다."""
    configured = resolution.configured or "(설정 없음)"
    return (
        f"{resolution.message}\n"
        f"저장된 경로: {configured}\n"
        "대시보드의 '작업 폴더 선택'에서 유효한 폴더를 지정하기 전까지 "
        "읽기와 쓰기 모두 실행하지 않습니다."
    )


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
#: CLI가 끝난 뒤 남은 출력을 기다리는 시간. 그 뒤에도 파이프가 열려 있으면 CLI가 띄운
#: 자식(개발 서버 등)이 물려받은 것이므로 더 기다리지 않는다.
CLI_PIPE_DRAIN_SECONDS = 2.0
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

# ──────────────────────────────────────────
# 코딩 파이프라인 — 토론 없이 고정 역할로 구현 → 테스트 → 검증 → 검토를 반복한다.
# ──────────────────────────────────────────

#: 역할은 토론으로 정하지 않는다. 이 표 하나만 바꾸면 역할이 바뀐다.
PIPELINE_ROLES = {"claude": "implement", "antigravity": "test", "codex": "review"}
PIPELINE_ROLE_LABELS = {"implement": "구현", "test": "테스트", "review": "검토"}
#: 기존 역할 카탈로그에 대응시켜 쓰기 범위 강제를 재사용한다
#: (qa = 테스트 경로만 쓰기, reviewer = 읽기 전용, "" = 범위 제한 없음).
PIPELINE_CATALOG_ROLES = {"implement": "", "test": "qa", "review": "reviewer"}
PIPELINE_IMPLEMENTER = next(agent for agent, role in PIPELINE_ROLES.items() if role == "implement")
REVIEW_PHASE = "검토"

PIPELINE_CYCLE_STEPS = [
    ("claude", "구현",
     "승인된 계획과 직전 사이클의 Codex 검토·자동 검증 결과(있다면)를 먼저 확인해. 아직 끝나지 않은 항목을 "
     "계획 보고로 끝내지 말고 실제 코드로 구현해. 검토에서 지적된 결함이 있으면 그것부터 고쳐. "
     "변경 파일과 결과만 간결하게 보고해.", "coding"),
    ("antigravity", "테스트",
     "Claude Code가 이번 사이클에 바꾼 기능을 확인하는 테스트를 테스트 파일(tests/, test_*.py, *.test.* 등)에만 "
     "추가·수정하고 실행해. 제품 코드는 고치지 마. 실패하면 실패한 테스트, 원인과 재현 방법을 보고해.", "coding"),
    ("codex", REVIEW_PHASE,
     "이번 사이클의 변경과 자동 검증 결과를 승인된 계획에 대조해 읽기 전용으로 검토해. 결함, 누락된 요구사항, "
     "회귀 위험, 빠진 테스트를 우선순위로 정리해. 파일은 수정하지 마. 고칠 것이 없으면 마지막 줄에 "
     "REVIEW: PASS, 고칠 것이 있으면 마지막 줄에 REVIEW: CHANGES만 써라.", "discussion"),
]


def pipeline_planning_steps(enabled_agents: list[str] | None = None) -> list[tuple[str, str, str, str]]:
    """계획 단계 — Claude 한 번. 이 턴이 곧 승인 요청이다."""
    return [(
        PIPELINE_IMPLEMENTER, CONFIRM_PHASE,
        "사용자 요청과 프로젝트 파일을 읽고 이번 작업의 구현 계획을 3~7개 항목으로 정리해. 각 항목에 수정할 파일과 "
        "완료 기준(어떤 테스트·검증으로 확인할지)을 적어. 담당은 고정이다: Claude Code가 구현, Antigravity가 테스트, "
        "Codex가 검토. 아직 파일은 수정하지 마. "
        f"마지막 줄에 {APPROVAL_TOKEN}만 써라.",
        "discussion",
    )]


def goal_coding_steps(enabled_agents: list[str]) -> list[tuple[str, str, str, str]]:
    """한 사이클 = 구현 → 테스트 → 검토. 순서는 고정이고 꺼진 모델의 단계만 빠진다."""
    enabled = set(normalize_enabled_agents(enabled_agents))
    return [step for step in PIPELINE_CYCLE_STEPS if step[0] in enabled]


def pipeline_agent_roles() -> dict:
    return normalize_agent_roles({
        agent: PIPELINE_CATALOG_ROLES[role] for agent, role in PIPELINE_ROLES.items()
    })


def pipeline_roles_label(enabled_agents: list[str] | None) -> str:
    enabled = set(normalize_enabled_agents(enabled_agents))
    return ", ".join(
        f'{AGENTS[agent]["label"]}={PIPELINE_ROLE_LABELS[role]}'
        for agent, role in PIPELINE_ROLES.items()
        if agent in enabled
    )


DEFAULT_CODING_LIMITS = {
    "max_cycles": 3,
    "max_minutes": 45,
    "max_tokens": 200_000,
    "max_changed_files": 50,
    "max_diff_lines": 2_000,
    "max_consecutive_failures": 2,
    "max_same_error": 2,
}


def normalize_coding_limits(value: dict | None) -> dict:
    raw = value if isinstance(value, dict) else {}
    return {
        key: max(1, int(raw.get(key, default) or default))
        for key, default in DEFAULT_CODING_LIMITS.items()
    }


def normalize_coding_progress(value: dict | None) -> dict:
    raw = value if isinstance(value, dict) else {}
    changed_paths = [
        str(path) for path in raw.get("changed_paths", [])
        if isinstance(path, str) and path
    ][:500]
    return {
        "changed_paths": list(dict.fromkeys(changed_paths)),
        "diff_lines": max(0, int(raw.get("diff_lines", 0) or 0)),
        "consecutive_failures": max(0, int(raw.get("consecutive_failures", 0) or 0)),
        "same_error_count": max(0, int(raw.get("same_error_count", 0) or 0)),
        "last_error_signature": str(raw.get("last_error_signature", ""))[:500],
    }


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
        "patterns": ["tests/**", "test/**", "spec/**", "test_*.py", "*_test.*", "*.spec.*", "*.test.*", "pytest.ini", "tox.ini", "pyproject.toml", "playwright.config.*", "vitest.config.*", "jest.config.*"],
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
    if current.get("mode") == "quick":
        # 빠른 질문은 기본 읽기 전용이고, 쓰기는 어떤 경로로도 허용하지 않는다.
        access = normalize_discussion_project_access(current.get("discussion_project_access"))
        return "read" if access == "read" else "none"
    if cli_mode == "coding":
        return "write"
    if current.get("mode") == "coding":
        return "read"
    return normalize_discussion_project_access(current.get("discussion_project_access"))


def cli_working_directory(cli_mode: str) -> Path:
    access = turn_project_access(cli_mode)
    if access != "none":
        # read/write 어느 쪽이든 유효한 폴더가 없으면 실행 자체를 막는다.
        resolution = resolve_project_workspace(require_write=access == "write")
        if resolution.blocked or resolution.path is None:
            raise WorkspaceUnavailable(resolution)
        return resolution.path
    NO_PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    return NO_PROJECT_DIR


def normalize_enabled_agents(enabled_agents: list[str] | None) -> list[str]:
    enabled = [a for a in (enabled_agents or AGENT_ORDER) if a in AGENT_ORDER]
    return enabled or list(AGENT_ORDER)


def prioritize_start_agent(enabled_agents: list[str] | None, start_agent: str = "") -> list[str]:
    enabled = normalize_enabled_agents(enabled_agents)
    if start_agent not in enabled:
        return enabled
    return [start_agent, *[agent for agent in enabled if agent != start_agent]]


def order_steps_by_agents(
    steps: list[tuple[str, str, str, str]],
    enabled_agents: list[str],
) -> list[tuple[str, str, str, str]]:
    """Keep phase order stable while honoring the selected agent order within each phase."""
    enabled = normalize_enabled_agents(enabled_agents)
    phases = list(dict.fromkeys(step[1] for step in steps))
    return [
        step
        for phase in phases
        for agent in enabled
        for step in steps
        if step[1] == phase and step[0] == agent
    ]


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


def steps_for_mode(mode: str, enabled_agents: list[str] | None = None) -> list[tuple[str, str, str, str]]:
    enabled = normalize_enabled_agents(enabled_agents)
    if mode == "quick":
        return quick_steps(enabled)
    discussion_steps = order_steps_by_agents([
        (agent, phase, instruction + (role_choice_instruction() if phase in {"역할 선언", "역할 확정"} else ""), cli_mode)
        for agent, phase, instruction, cli_mode in DISCUSSION_STEPS
        if agent in enabled
    ], enabled)
    if mode == "coding":
        return pipeline_planning_steps(enabled)
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
        if os.name != "nt":
            return shlex.split(command)
        # POSIX 규칙은 백슬래시를 이스케이프로 먹는다 (C:\tools\codex.cmd → C:toolscodex.cmd).
        # Windows 규칙으로 나누고 감싼 따옴표만 벗긴다.
        return [
            part[1:-1] if len(part) > 1 and part[0] == part[-1] and part[0] in "\"'" else part
            for part in shlex.split(command, posix=False)
        ]
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
        dirs[:] = [
            name
            for name in dirs
            if name not in skipped_dirs
            and not name.startswith((".pytest_", ".test_tmp", ".test_work"))
        ]
        for name in dirs:
            rel = (relative_dir / name).as_posix() + "/"
            entries[rel] = ("dir",)
            if len(entries) >= PROJECT_SNAPSHOT_MAX_ENTRIES:
                truncated = True
                break
        if truncated:
            break
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
                # read_text는 \r\n을 \n으로 바꾼다. 롤백 패치가 원래 줄 끝과 맞도록 그대로 읽는다.
                text = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            contents[path.relative_to(root).as_posix()] = text
            total += size
    return contents


def _split_lines_keep_ends(text: str) -> list[str]:
    """\\n에서만 나누고 줄 끝(\\r\\n 포함)은 그대로 둔다 — git apply가 줄을 보는 방식과 같다."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def build_turn_diff(
    root: Path, before: dict[str, str], changed_paths: list[dict], max_chars: int = 100_000
) -> str:
    """`git apply --reverse`로 정확히 되돌릴 수 있는 패치를 만든다.

    * 줄 끝을 바꾸지 않는다 — LF·CRLF 파일 모두 바이트 그대로 복원된다.
    * 변경 전 내용을 저장하지 못한 파일(크기·예산 초과)은 패치에서 뺀다. 예전에는 빈
      내용과 비교해서, 되돌리면 파일이 통째로 비워졌다.
    * 한도는 파일 단위로 적용한다. 헝크 중간에서 자르면 패치 전체가 깨진다.
    """
    chunks = []
    total = 0
    for item in changed_paths[:50]:
        relative = item["path"].rstrip("/")
        if not relative or item["path"].endswith("/"):
            continue
        created = item.get("change") == "생성"
        if not created and relative not in before:
            continue
        path = root / relative
        try:
            deleted = not path.exists()
            new_text = "" if deleted else path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = []
        for line in difflib.unified_diff(
            _split_lines_keep_ends("" if created else before[relative]),
            _split_lines_keep_ends(new_text),
            fromfile="/dev/null" if created else f"a/{relative}",
            tofile="/dev/null" if deleted else f"b/{relative}",
        ):
            if not line.endswith("\n"):
                line += "\n\\ No newline at end of file\n"
            lines.append(line)
        chunk = "".join(lines)
        if not chunk or total + len(chunk) > max_chars:
            continue
        chunks.append(chunk)
        total += len(chunk)
    return "".join(chunks)


def checkpoint_patch_paths(patch_text: str) -> set[str]:
    """패치에 실제로 들어 있는 파일 경로 — 여기 없는 파일은 되돌릴 수 없다."""
    return {
        match.group(1)
        for match in re.finditer(r"^(?:---|\+\+\+) [ab]/(.+?)\r?$", patch_text, re.MULTILINE)
    }


def checkpoint_dir(session_id: str) -> Path:
    return safe_child(session_memory_dir(session_id), "checkpoints")


def checkpoint_meta_path(patch_path: Path) -> Path:
    return patch_path.with_suffix(".json")


def save_turn_checkpoint(
    session_id: str,
    step_index: int,
    agent: str,
    turn_diff: str,
    workspace: Path | None = None,
) -> str:
    """턴 diff를 체크포인트로 저장한다.

    소유권 정보(세션 ID + 작업공간)를 옆에 같이 남긴다. 이게 없으면 "MEMORY_DIR
    아래에 있다"는 이유만으로 다른 세션·다른 프로젝트의 패치가 적용될 수 있다.
    """
    if not turn_diff:
        return ""
    directory = checkpoint_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    # step_index는 목표 사이클·개입·재시도 동안 그대로라 이름이 겹친다. 시각을 붙여 덮어쓰지 않는다.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = safe_child(directory, f"{step_index:03d}_{agent}_{stamp}.patch")
    # newline=""로 써야 Windows에서 \n이 \r\n으로 바뀌지 않는다 (원래 줄 끝 보존).
    path.write_text(turn_diff, encoding="utf-8", newline="")
    atomic_write_json(checkpoint_meta_path(path), {
        "session_id": validate_session_id(session_id),
        "workspace_path": str(workspace) if workspace else "",
        "agent": agent,
        "step_index": step_index,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    return str(path)


def current_tool_environment() -> validation.ToolEnvironment:
    """검증 명령 탐지에 필요한 바깥 환경을 지금 값으로 채운다.

    ``excluded_roots``에 TriAgent 자신의 폴더를 넣는 게 핵심이다. 이게 없으면
    TriAgent를 대상으로 코딩 모드를 돌릴 때 자동 검증이
    ``python -m unittest discover -s tests``를 자식 프로세스로 띄우고, 그 테스트가
    다시 워커를 돌려 자기 자신을 무한히 재귀 실행한다.
    """
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    return validation.ToolEnvironment(
        python_executable=sys.executable,
        npm_executable=npm or "npm",
        has_pytest=importlib.util.find_spec("pytest") is not None,
        has_npm=npm is not None,
        excluded_roots=(str(ROOT),),
    )


def detect_validation_commands(
    root: Path, environment: validation.ToolEnvironment | None = None
) -> list[tuple[str, list[str]]]:
    """순수 탐지 로직은 triagent.domain.validation에 있다 (환경 주입 가능)."""
    return validation.detect_validation_commands(
        root, environment or current_tool_environment()
    )


def run_project_validation(
    root: Path, environment: validation.ToolEnvironment | None = None
) -> list[dict]:
    results = []
    for label, command in detect_validation_commands(root, environment):
        started = time.time()
        try:
            result = run_process_tree(command, cwd=root, timeout=180, active_name="자동 검증")
            if CONTROL["stopped"]:
                break  # /stop이 검증을 끊었다 — 끊긴 결과를 실패로 기록하지 않는다.
            # unittest는 결과를 stderr에, pytest는 stdout에 쓴다. 둘 다 남긴다.
            output = "\n".join(
                part for part in (decode_cli_output(result.stdout).strip(),
                                  decode_cli_output(result.stderr).strip()) if part
            )
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


def run_validation_for_current_workspace() -> list[dict]:
    """현재 작업공간에서 자동 검증을 실행한다.

    작업 폴더가 막혀 있거나 TriAgent 자신을 가리키면 실행하지 않는다 — 후자는
    unittest가 자기 자신을 자식 프로세스로 다시 띄우는 무한 재귀가 된다.
    """
    resolution = resolve_project_workspace()
    if resolution.blocked or resolution.path is None:
        add_runtime_event(f"자동 검증 건너뜀 — {resolution.message}", "warn")
        return []
    environment = current_tool_environment()
    commands = detect_validation_commands(resolution.path, environment)
    if not commands:
        try:
            same_as_self = resolution.path.resolve() == ROOT.resolve()
        except OSError:
            same_as_self = False
        if same_as_self:
            add_runtime_event(
                "자동 검증 건너뜀 — 작업 폴더가 TriAgent 자신이라 테스트 재귀 실행을 막았습니다.",
                "warn",
            )
        return []
    return run_project_validation(resolution.path, environment)


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

            output_queue: queue.Queue = queue.Queue()

            def read_stream(name: str, stream) -> None:
                for raw_line in iter(stream.readline, b""):
                    output_queue.put((name, raw_line))
                output_queue.put((name, None))

            def write_stdin() -> None:
                # 읽기 스레드와 함께 돌려야 큰 프롬프트를 쓰는 동안 출력 파이프가 차서 서로
                # 기다리지 않는다. CLI가 먼저 끝나 파이프가 닫히면(BrokenPipeError) 진짜 원인은
                # stderr와 종료 코드에 남으므로 여기서는 삼킨다.
                try:
                    process.stdin.write(input_text.encode("utf-8"))
                    process.stdin.close()
                except OSError:
                    pass

            threads = [
                threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
                threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
            ]
            if input_text is not None and process.stdin is not None:
                threads.append(threading.Thread(target=write_stdin, daemon=True))
            for thread in threads:
                thread.start()

            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            final_messages: list[str] = []
            closed_streams = 0
            deadline = time.monotonic() + timeout if timeout else None
            exited_at = None
            while closed_streams < 2:
                now = time.monotonic()
                if deadline and now >= deadline:
                    terminate_process_tree(process)
                    raise subprocess.TimeoutExpired(
                        cmd, timeout,
                        output="\n".join(final_messages or stdout_lines),
                        stderr="\n".join(stderr_lines),
                    )
                if exited_at is None:
                    if process.poll() is not None:
                        exited_at = now
                elif now - exited_at >= CLI_PIPE_DRAIN_SECONDS and output_queue.empty():
                    # CLI는 끝났는데 CLI가 띄운 자식(개발 서버 등)이 출력 파이프를 물려받아
                    # 붙잡고 있다. 기다리면 시간 초과까지 멈추고 끝난 답변을 버리게 된다.
                    break
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

        result = run_process_tree(
            cmd,
            cwd=cwd or ROOT,
            input_bytes=input_text.encode("utf-8") if input_text is not None else None,
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
    except subprocess.TimeoutExpired as exc:
        with ACTIVE_PROCESS_LOCK:
            active = ACTIVE_PROCESSES.get(tool_name)
            if active is not None and active.poll() is not None:
                ACTIVE_PROCESSES.pop(tool_name, None)
        print_cli_hint(tool_name, command, "실행 시간 초과")
        # "실행하지 못함"과 구분하고, 시간 초과 전까지 받은 부분 출력을 보존한다.
        result = subprocess.CompletedProcess(
            cmd, -1, decode_cli_output(exc.output), decode_cli_output(exc.stderr)
        )
        result.timed_out = timeout
        return result
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


#: 격리된 사전 점검 전용 폴더. 절대 프로젝트 폴더에서 읽기/쓰기 검사를 하지 않는다.
PREFLIGHT_DIR = Path(tempfile.gettempdir()) / "triagent-preflight"

CLI_PROBE_TIMEOUT = int(os.environ.get("ROUNDTABLE_PROBE_TIMEOUT", "120"))


def _probe_runner(command: str):
    """clicheck에 주입할 프로세스 실행기. 실제 CLI가 없는 테스트에서는 교체된다."""

    def run(args: list[str]) -> clicheck.CommandResult:
        try:
            result = run_process_tree(resolve_command(command) + args, timeout=30)
        except FileNotFoundError:
            return clicheck.CommandResult(returncode=127, not_found=True)
        except PermissionError as exc:
            return clicheck.CommandResult(returncode=126, stderr=str(exc))
        except subprocess.TimeoutExpired:
            return clicheck.CommandResult(returncode=124, timed_out=True)
        return clicheck.CommandResult(
            returncode=result.returncode,
            stdout=decode_cli_output(result.stdout),
            stderr=decode_cli_output(result.stderr),
        )

    return run


def _make_probe_workspace(name: str) -> Path:
    """점검용 임시 폴더를 깨끗하게 만든다 (프로젝트 폴더와 완전히 분리)."""
    directory = PREFLIGHT_DIR / name
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _codex_help_text() -> str:
    try:
        result = run_process_tree(resolve_command(CODEX_CMD) + ["exec", "--help"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return decode_cli_output(result.stdout) + decode_cli_output(result.stderr)


def codex_write_args() -> list[str]:
    """이 Codex 버전에서 쓰기 실행에 쓸 인자.

    옵션 이름을 추측하지 않고 `codex exec --help` 출력에서 실제로 존재하는 것만
    고른다. 0.148.0에서는 --approve-for-me가 --sandbox와 배타적이라, 둘을 같이
    넘기면 인자 오류로 즉시 죽는다.
    """
    cached = (CONNECTION_STATUS.get("codex") or {}).get("features", {}).get("write_args")
    if cached:
        return list(cached)
    return clicheck.detect_codex_write_flags(_codex_help_text())


def _cli_read_probe(key: str, command: str) -> tuple[bool, str]:
    """격리된 폴더의 파일을 CLI가 실제로 읽는지 확인한다."""
    directory = _make_probe_workspace(f"{key}-read")
    marker = "TRIAGENT_READ_PROBE_7F3A"
    (directory / "probe.txt").write_text(marker + "\n", encoding="utf-8")
    prompt = (
        "Read the file probe.txt in the current working directory and reply with "
        "its exact contents and nothing else."
    )
    try:
        response = _run_probe_cli(key, command, prompt, directory, write=False)
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 점검 실패로 보고한다
        return False, f"읽기 검사 실행 실패: {type(exc).__name__}"
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if response is None:
        return False, "읽기 검사에서 CLI가 응답하지 않았습니다."
    if marker in response:
        return True, "격리된 폴더의 파일을 읽었습니다."
    return False, f"파일 내용을 읽지 못했습니다. 응답 일부: {clicheck.redact(response, 200)}"


def _cli_write_probe(key: str, command: str) -> tuple[bool, str]:
    """격리된 폴더에 CLI가 실제로 파일을 쓰는지 확인하고 내용까지 검증한다."""
    directory = _make_probe_workspace(f"{key}-write")
    marker = "TRIAGENT_WRITE_PROBE_9C21"
    prompt = (
        "Create a file named probe_out.txt in the current working directory whose "
        f"entire content is exactly {marker}. Then reply DONE."
    )
    target = directory / "probe_out.txt"
    try:
        response = _run_probe_cli(key, command, prompt, directory, write=True)
        if not target.is_file():
            return False, (
                "파일이 생성되지 않았습니다. 응답 일부: "
                f"{clicheck.redact(response or '', 240)}"
            )
        written = target.read_text(encoding="utf-8", errors="replace").strip()
        if marker not in written:
            return False, f"파일은 만들어졌지만 내용이 다릅니다: {written[:120]!r}"
        return True, "격리된 폴더에 파일을 쓰고 내용까지 확인했습니다."
    except Exception as exc:  # noqa: BLE001
        return False, f"쓰기 검사 실행 실패: {type(exc).__name__}"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _run_probe_cli(key: str, command: str, prompt: str, directory: Path, write: bool) -> str | None:
    """점검용 최소 인자로 CLI를 한 번 호출한다. 인증정보는 인자에 넣지 않는다."""
    if key == "codex":
        args = ["exec", "--ephemeral", "--skip-git-repo-check"]
        args += codex_write_args() if write else ["--sandbox", "read-only"]
        args += ["--cd", str(directory), "-"]
        result = run_cli("Codex", command, args, timeout=CLI_PROBE_TIMEOUT,
                         cwd=directory, input_text=prompt)
    elif key == "claude":
        args = ["--print", "--safe-mode", "--no-session-persistence"]
        args += (["--tools", "Bash,Edit,Read,Write,Glob,Grep", "--permission-mode", "acceptEdits"]
                 if write else ["--tools", "Read,Glob,Grep", "--permission-mode", "dontAsk"])
        args.append(prompt)
        result = run_cli("Claude Code", command, args, timeout=CLI_PROBE_TIMEOUT, cwd=directory)
    else:
        args = ["--add-dir", str(directory)]
        args += (["--mode", "accept-edits", "--dangerously-skip-permissions"]
                 if write else ["--mode", "plan", "--sandbox", "--dangerously-skip-permissions"])
        args += ["--print", prompt]
        result = run_cli("Antigravity", command, args, timeout=CLI_PROBE_TIMEOUT, cwd=directory)
    if result is None:
        return None
    return (result.stdout or "") + "\n" + (result.stderr or "")


def run_connection_checks(check_write: bool | None = None, deep: bool = False) -> bool:
    """CLI별 사전 점검을 단계별로 수행한다.

    ``deep``이 False면 명령 존재 + 버전까지만 본다 (시작이 느려지지 않도록).
    ``deep``이 True면 격리된 임시 폴더에서 실제 읽기 검사를, 사용자가 쓰기 권한을
    선택했을 때만 쓰기 검사까지 한다. 검사는 절대 프로젝트 폴더에서 하지 않는다.
    """
    separator("연결 확인")
    if check_write is None:
        check_write = workspace_access_mode() == "write"

    codex_help = _codex_help_text() if deep else ""
    reports = []
    for key, label, command in CONNECTION_TARGETS:
        report = clicheck.run_staged_check(
            key=key,
            label=label,
            command=command,
            runner=_probe_runner(command),
            read_probe=(lambda k=key, c=command: _cli_read_probe(k, c)) if deep else None,
            write_probe=(lambda k=key, c=command: _cli_write_probe(k, c)) if deep else None,
            check_write=check_write and deep,
        )
        if key == "codex" and deep:
            report.features["write_args"] = clicheck.detect_codex_write_flags(codex_help)
        CONNECTION_STATUS[key] = report.as_dict()
        reports.append(report)

        if report.ok:
            extra = []
            if report.can_read:
                extra.append("읽기 OK")
            if report.can_write:
                extra.append("쓰기 OK")
            suffix = f" · {' · '.join(extra)}" if extra else ""
            print(f"  ✅ {label}: {command_label(command)} ({report.version}){suffix}")
        else:
            failure = report.first_failure
            print(f"  ❌ {label}: [{failure.name}] {clicheck.redact(failure.detail, 200)}")
            if report.suggested_action():
                print(f"      → {report.suggested_action()}")
    return all(report.ok for report in reports)


def preflight() -> None:
    if run_connection_checks():
        return
    usable = [key for key, status in CONNECTION_STATUS.items() if status.get("ok")]
    if not usable:
        print("\n\U0001f6d1 사용할 수 있는 CLI가 하나도 없어 시작하지 않습니다.")
        print("   위의 실패 단계와 조치 안내를 확인해주세요.")
        sys.exit(1)
    print("\n\u26a0\ufe0f  일부 CLI 점검이 실패했습니다. 대시보드에서 실패 단계를 확인하세요.")


def _finalize_cli_result(tool_name: str, result) -> str:
    """subprocess 결과를 응답 문자열로 정리한다. 실패/빈 응답을 구분해서 원인을 남긴다."""
    if result is None:
        return f"({tool_name} 응답 없음 — 실행 실패: 프로세스를 실행하지 못함)"
    if getattr(result, "timed_out", None):
        partial = (result.stdout or "").strip()
        notice = f"({tool_name} 응답 없음 — 실행 시간 초과: {result.timed_out}초)"
        return f"{notice}\n\n시간 초과 전 부분 출력:\n{partial}" if partial else notice
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
    if not requested or "목표 사이클" in phase:
        return False
    if session_mode == "coding" and (
        phase in {"개선안 제안", "최종 보고"} or phase.startswith("호출 답변")
    ):
        return False
    return True


def allow_agent_calls(session_mode: str, phase: str) -> bool:
    # 코딩 파이프라인은 역할이 고정이라 모델끼리 일을 넘기지 않는다.
    return session_mode != "coding"


def ask_codex(prompt: str, mode: str = "discussion", event_callback=None) -> str:
    project_access = turn_project_access(mode)
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
    # 승인·샌드박스 옵션은 버전마다 다르다. 추측하지 말고 `codex exec --help`에서
    # 실제로 지원하는 조합만 쓴다 (0.148.0: --approve-for-me는 --sandbox와 배타적).
    args.extend(codex_write_args() if project_access == "write" else ["--sandbox", "read-only"])
    args.extend([
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
        ]
        if project_access == "read":
            args.append("--dangerously-skip-permissions")
        args.extend(["--print", prompt])
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
        "start_agent": AGENT_ORDER[0],
        "agent_settings": normalize_agent_settings(None),
        "agent_roles": normalize_agent_roles(None),
        "role_history": [],
        "roles_announced_signature": "",
        "workspace_path": str(project_path_or_none() or ""),
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
        "coding_stage": "planning",
        # 코딩 모드의 쓰기 턴은 사용자가 계획을 승인한 뒤에만 허용된다. 메모리(CONTROL)가 아니라
        # 상태에 저장해야 재시작·모드 전환·거절 뒤 메시지로 승인을 건너뛸 수 없다.
        "plan_approved": False,
        "coding_cycle": 0,
        "coding_cycle_step": 0,
        "coding_started_at": None,
        "coding_limits": normalize_coding_limits(None),
        "coding_progress": normalize_coding_progress(None),
        "coding_stopped": False,
        "coding_stop_reason": "",
        "workflow_status": workflow.IDLE,
        "pending_user_question": None,
        "user_question_history": [],
    }


def normalize_state(state: dict) -> dict:
    legacy_continuous = state.get("mode") == "continuous"
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
    if state["mode"] == "continuous":
        state["mode"] = "coding"
    state["discussion_project_access"] = normalize_discussion_project_access(
        state.get("discussion_project_access")
    )
    state.setdefault("workspace_path", str(project_path_or_none() or ""))
    state.setdefault("workspace_access", "write")
    state["workspace_access"] = "write" if state.get("workspace_access") == "write" else "read"
    if state.get("mode") == "coding":
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
    saved_workflow_status = state.get("workflow_status")
    if not saved_workflow_status and state.get("finished"):
        saved_workflow_status = workflow.COMPLETED
    elif not saved_workflow_status and state.get("continuous_stopped"):
        saved_workflow_status = workflow.CANCELLED
    state["workflow_status"] = workflow.normalize_workflow_status(
        saved_workflow_status, has_topic=bool(state.get("topic"))
    )
    state["pending_user_question"] = workflow.normalize_user_question(
        state.get("pending_user_question")
    )
    if state["pending_user_question"]:
        state["workflow_status"] = workflow.WAITING_FOR_USER_RESPONSE

    # 실행 결과 상태(run_status)는 workflow_status와 별개로 보관한다.
    # 서버가 재시작돼도 "사용자 중단"이 "진행 중"으로 되살아나면 안 된다.
    saved_run_status = str(state.get("run_status", "") or "")
    if saved_run_status not in runstatus.RUN_STATUSES:
        if state.get("finished"):
            saved_run_status = runstatus.RunStatus.COMPLETED
        elif state["workflow_status"] == workflow.CANCELLED:
            saved_run_status = runstatus.RunStatus.USER_CANCELLED
        elif state["workflow_status"] == workflow.WAITING_FOR_USER_RESPONSE:
            saved_run_status = runstatus.RunStatus.WAITING_FOR_USER_RESPONSE
        elif state.get("topic"):
            saved_run_status = runstatus.RunStatus.PAUSED
        else:
            saved_run_status = runstatus.RunStatus.IDLE
    elif saved_run_status == runstatus.RunStatus.RUNNING:
        # 프로세스가 새로 떴다면 이전 프로세스의 RUNNING은 더 이상 유효하지 않다.
        saved_run_status = runstatus.RunStatus.PAUSED
    state["run_status"] = saved_run_status
    state.setdefault("user_question_history", [])
    if not isinstance(state["user_question_history"], list):
        state["user_question_history"] = []
    state["enabled_agents"] = prioritize_start_agent(
        state.get("enabled_agents"), state.get("start_agent", "")
    )
    state["start_agent"] = state["enabled_agents"][0]
    state["coding_limits"] = normalize_coding_limits(state.get("coding_limits"))
    state["coding_progress"] = normalize_coding_progress(state.get("coding_progress"))
    state["coding_stopped"] = bool(
        state.get("coding_stopped", False) or state.get("continuous_stopped", False)
    )
    state.setdefault("coding_stop_reason", "")
    state.setdefault("coding_started_at", None)
    if legacy_continuous:
        legacy_index = max(0, int(state.get("step_index", 0)))
        cycle_steps = goal_coding_steps(state["enabled_agents"])
        state["coding_stage"] = "iteration"
        state["coding_cycle"] = (legacy_index // max(1, len(cycle_steps))) + 1
        state["coding_cycle_step"] = legacy_index % max(1, len(cycle_steps))
        state["step_index"] = len(steps_for_mode("coding", state["enabled_agents"]))
    else:
        state["coding_stage"] = (
            state.get("coding_stage")
            if state.get("coding_stage") in {"planning", "iteration"}
            else "planning"
        )
        state["coding_cycle"] = max(0, int(state.get("coding_cycle", 0) or 0))
        state["coding_cycle_step"] = max(0, int(state.get("coding_cycle_step", 0) or 0))
    # 이 필드가 생기기 전 세션: 목표 사이클까지 간 세션은 이미 승인된 것으로 본다.
    state["plan_approved"] = bool(
        state.get("plan_approved", state["coding_stage"] == "iteration")
    )
    if state["coding_stage"] == "iteration" and not state.get("coding_started_at"):
        state["coding_started_at"] = time.time()
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
            raw_state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            state = normalize_state(
                json.loads(json.dumps(raw_state, ensure_ascii=False))
            )
            if state != raw_state:
                save_state(state)
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


class StateSaveError(RuntimeError):
    """상태를 디스크에 쓰지 못했다 (디스크 부족, 권한, 잠김 등)."""


def atomic_write_json(path: Path, state: dict) -> None:
    """임시 파일에 쓰고 os.replace로 바꿔치기한다 (부분 저장 방지).

    실패하면 조용히 넘어가지 않는다 — 디스크가 가득 찼는데 계속 진행하면
    사용자는 저장됐다고 믿고 작업을 잃는다.
    """
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, path)
    except OSError as exc:
        raise StateSaveError(f"{path.name} 저장 실패: {exc.strerror or exc}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


#: 마지막 저장 실패 메시지. 상태 응답에 실어 대시보드에 띄운다.
LAST_SAVE_ERROR: dict[str, str] = {"message": ""}


def save_state(state: dict) -> None:
    # 세션 파일 저장 실패(목록 조회와 겹친 WinError 5 등)도 여기서 멈춰야 한다. 예외가 새면
    # 워커 스레드가 죽고 finally의 save_state가 다시 실패해 worker_running이 True로 남는다.
    try:
        atomic_write_json(STATE_PATH, state)
        session_id = state.get("id")
        if session_id:
            try:
                session_path = session_json_path(session_id)
            except UnsafePathError as exc:
                # 세션 ID가 오염된 상태로는 어디에도 쓰지 않는다.
                print(f"  ⚠️  세션 파일을 저장하지 않았습니다 — {exc}")
            else:
                atomic_write_json(session_path, state)
    except StateSaveError as exc:
        # 진행은 계속하되(메모리 상태는 살아 있다) 사용자에게 복구 가능한 오류로 알린다.
        LAST_SAVE_ERROR["message"] = (
            f"{exc} — 디스크 여유 공간과 폴더 권한을 확인한 뒤 다시 시도해주세요. "
            "지금까지의 대화는 메모리에 남아 있으며, 저장이 복구되면 이어서 기록됩니다."
        )
        print(f"  ❌ {LAST_SAVE_ERROR['message']}")
        return
    if LAST_SAVE_ERROR["message"]:
        LAST_SAVE_ERROR["message"] = ""
        print("  ✅ 상태 저장이 복구되었습니다.")


_SKIPPED_SESSION_WARNINGS: set[str] = set()


def list_sessions() -> list[dict]:
    SESSIONS_DIR.mkdir(exist_ok=True)
    summaries = []
    for path in SESSIONS_DIR.glob("*.json"):
        # 디스크에 비정상 이름이 있어도 자동으로 지우거나 옮기지 않는다.
        # 목록에서만 빼고, 파일마다 한 번씩만 경고한다.
        if not is_valid_session_id(path.stem):
            if path.stem not in _SKIPPED_SESSION_WARNINGS:
                _SKIPPED_SESSION_WARNINGS.add(path.stem)
                print(f"  ⚠️  세션 ID 형식이 아니어서 목록에서 제외합니다 (파일은 그대로 둡니다): {path.name}")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 손상된 세션 하나 때문에 전체 목록이 실패하면 안 된다.
            if path.stem not in _SKIPPED_SESSION_WARNINGS:
                _SKIPPED_SESSION_WARNINGS.add(path.stem)
                print(f"  ⚠️  세션 파일을 읽지 못해 목록에서 제외합니다: {path.name} ({type(exc).__name__})")
            summaries.append({
                "id": path.stem,
                "topic": "",
                "name": f"(손상된 세션 파일) {path.name}",
                "tags": [],
                "favorite": False,
                "archived": False,
                "mode": "discussion",
                "discussion_project_access": "read",
                "enabled_agents": list(AGENT_ORDER),
                "agent_settings": normalize_agent_settings(None),
                "finished": False,
                "created_at": "",
                "message_count": 0,
                "corrupt": True,
            })
            continue
        if not isinstance(data, dict):
            continue
        summaries.append({
            "id": data.get("id", path.stem),
            "topic": data.get("topic", ""),
            "name": data.get("name") or data.get("topic") or "새 세션",
            "tags": data.get("tags", []),
            "favorite": bool(data.get("favorite", False)),
            "archived": bool(data.get("archived", False)),
            # 구버전 "continuous" 세션은 코딩 모드로 읽는다 (normalize_state와 동일).
            # 이게 없으면 목록에 원문 "continuous"가 그대로 보인다.
            "mode": "coding" if data.get("mode") == "continuous" else data.get("mode", "discussion"),
            "discussion_project_access": normalize_discussion_project_access(
                data.get("discussion_project_access")
            ),
            "enabled_agents": normalize_enabled_agents(data.get("enabled_agents")),
            "agent_settings": normalize_agent_settings(data.get("agent_settings")),
            "finished": data.get("finished", False),
            "created_at": data.get("created_at", ""),
            "message_count": len(data.get("messages", [])),
            "run_status": str(data.get("run_status", "") or ""),
        })
    summaries.sort(key=lambda s: s["created_at"], reverse=True)
    return summaries


def load_session(session_id: str) -> dict | None:
    try:
        path = session_json_path(session_id)
    except UnsafePathError:
        return None
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


def session_json_path(session_id: str) -> Path:
    """sessions/<id>.json — ID 검증 + 폴더 봉쇄 확인을 거친 경로."""
    return safe_child(SESSIONS_DIR, f"{validate_session_id(session_id)}.json")


def session_transcript_path(session_id: str) -> Path:
    return safe_child(SESSIONS_DIR, f"{validate_session_id(session_id)}.md")


def session_memory_dir(session_id: str) -> Path:
    return safe_child(MEMORY_DIR, validate_session_id(session_id))


def session_memory_paths(session_id: str) -> tuple[Path, Path]:
    memory_dir = session_memory_dir(session_id)
    return memory_dir / "full.md", memory_dir / "brief.md"


def session_profile_path(session_id: str) -> Path:
    return safe_child(session_memory_dir(session_id), "Profile.md")


def session_roles_path(session_id: str) -> Path:
    return safe_child(session_memory_dir(session_id), "Roles.md")


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
    return safe_child(session_memory_dir(session_id), "profiles")


def session_agent_profile_path(session_id: str, agent: str) -> Path:
    if agent not in AGENTS:
        raise UnsafePathError(f"알 수 없는 모델입니다: {agent!r}")
    return safe_child(session_agent_profile_dir(session_id), f"{agent.upper()}_Profile.md")


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


_REVIEW_VERDICT_RE = re.compile(r"^\s*REVIEW\s*:\s*(PASS|CHANGES)\s*$", re.IGNORECASE)


def extract_review_verdict(text: str) -> tuple[str, str]:
    """검토 답변 마지막 줄의 'REVIEW: PASS|CHANGES'를 떼어 낸다. 없으면 판정은 ''.

    본문 중간에 인용된 토큰은 판정으로 보지 않는다 — 마지막 줄만 본다.
    """
    lines = (text or "").rstrip().splitlines()
    if lines:
        match = _REVIEW_VERDICT_RE.match(lines[-1])
        if match:
            return "\n".join(lines[:-1]).rstrip(), match.group(1).lower()
    return text, ""


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
        "사용자 판단 없이는 안전하게 진행할 수 없으면 추측하지 말고 답변 마지막에 JSON 객체를 쓴다. "
        '형식: {"action":"STOP_AND_ASK_USER","reason":"중단 이유","question":"질문",'
        '"options":[{"id":"A","label":"선택지","risk":"위험"}],'
        '"recommended_option":"A","blocking":true}. '
        "blocking은 반드시 true이며, 이 신호를 쓴 뒤 추가 작업을 진행하지 않는다.\n\n"
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

# 서버 실행마다 새로 만드는 예측 불가능한 토큰. 상태를 바꾸는 모든 요청이 이 값을
# 제시해야 한다. 다른 웹페이지는 대시보드 HTML을 읽을 수 없으므로 이 값을 알 수 없다.
CSRF_TOKEN = webguard.generate_csrf_token()

STATE_LOCK = threading.Lock()
# 저장된 상태는 main()이 restore_saved_state()로 불러온다. 임포트만으로는 사용자 데이터를
# 읽거나 다시 쓰지 않는다 — 테스트와 스모크 스크립트가 이 모듈을 임포트한다.
STATE: dict = {}
CONTROL = {
    # /stop으로 사용자가 직접 멈췄는지. "stopped"는 예산 초과·승인 거절 등으로도
    # 켜지기 때문에, 사용자 중단만 따로 구분해야 실패로 오분류하지 않는다.
    "user_cancelled": False,
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


def waiting_for_user_response(state: dict | None = None) -> bool:
    # 대기 여부는 질문 자체로만 판단한다 (normalize_state와 같은 규칙). workflow_status는
    # 승인 대기·중단·거절이 덮어쓰기 때문에, 그 값을 보면 /approve·/resume·채팅이
    # 답하지 않은 질문을 지나쳐 버린다. 질문은 답변이나 새 주제로만 지워진다.
    current = state if state is not None else STATE
    return bool(current.get("pending_user_question"))


def register_user_question(agent: str, phase: str, question_data: dict) -> bool:
    question = workflow.normalize_user_question(question_data)
    if not question:
        return False
    with STATE_LOCK:
        if waiting_for_user_response(STATE):
            return False
        question.update({
            "source_agent": agent,
            "source_phase": phase,
            "asked_at": datetime.now().isoformat(timespec="seconds"),
            "resume_step_index": int(STATE.get("step_index", 0)) + 1,
        })
        STATE["pending_user_question"] = question
        STATE["workflow_status"] = workflow.WAITING_FOR_USER_RESPONSE
        save_state(STATE)
    add_runtime_event(
        f"{AGENTS.get(agent, {}).get('label', agent)}의 사용자 질문으로 자동 진행을 중단했습니다."
    )
    return True


def answer_pending_user_question(option_id: str = "", answer: str = "") -> dict:
    option_id = option_id.strip()
    answer = answer.strip()
    with STATE_LOCK:
        question = workflow.normalize_user_question(STATE.get("pending_user_question"))
        if not question or not waiting_for_user_response(STATE):
            return {"error": "현재 답변을 기다리는 질문이 없습니다."}

        options = {item["id"]: item for item in question["options"]}
        if options and option_id not in options:
            return {"error": "제시된 선택지 중 하나를 선택해주세요.", "question": question}
        if not options and not answer:
            return {"error": "질문에 대한 답변을 입력해주세요.", "question": question}

        selected = options.get(option_id)
        response_text = selected["label"] if selected else answer
        if selected and answer:
            response_text = f"{response_text}\n\n추가 답변: {answer}"
        history_item = {
            **question,
            "selected_option": option_id,
            "answer": answer,
            "answered_at": datetime.now().isoformat(timespec="seconds"),
        }
        session_id = STATE.get("id")

    if not add_message(
        "user", "질문 답변", response_text, expected_session_id=session_id
    ):
        return {"error": "답변을 세션에 기록하지 못했습니다. 다시 시도해주세요."}

    with STATE_LOCK:
        if STATE.get("id") != session_id or not waiting_for_user_response(STATE):
            return {"error": "답변을 기록하는 동안 활성 세션이 변경되었습니다."}
        STATE["user_question_history"].append(history_item)
        STATE["pending_user_question"] = None
        STATE["workflow_status"] = (
            workflow.CODING
            if STATE.get("mode") == "coding"
            else workflow.DEBATING
        )
        CONTROL["paused"] = False
        CONTROL["stopped"] = False
        save_state(STATE)

    add_runtime_event("사용자 답변이 기록되어 중단된 워크플로를 재개합니다.")
    start_worker_if_needed(force=True)
    payload = state_json_payload()
    payload["success"] = True
    return payload


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
    allowed_patterns = ", ".join(role.get("patterns", [])) or "읽기 전용"
    return (
        f"확정 역할: {role['label']}\n"
        f"책임: {role['summary']}\n"
        f"수정 허용 범위: {role['scope']}\n"
        f"허용 파일 패턴: {allowed_patterns}\n"
        "강제 규칙: 사용자가 역할 선택 UI에서 바꾸기 전까지 이 역할을 유지한다. "
        "다른 역할의 작업을 대신 수행하거나 담당 밖 파일을 수정하지 않는다. "
        "호출한 모델이 담당 밖 파일 수정까지 요청하더라도 거절하고 필요한 작업을 담당 모델에게 넘긴다. "
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
        if path.endswith("/"):
            continue  # 폴더 항목(스냅샷의 "x/")은 파일 변경이 아니다 — 안의 파일은 따로 검사된다.
        top_level = path.split("/", 1)[0]
        if top_level in {
            ".pytest_cache", ".ruff_cache", ".codex_pytest_tmp", ".agents",
            ".venv", "venv", "__pycache__", "roundtable_memory", "sessions",
        }:
            continue
        if top_level.startswith((".pytest_", ".test_tmp", ".test_work")):
            continue
        if top_level in {"data", "instance"} and Path(path).suffix.lower() in {
            ".db", ".sqlite", ".sqlite3", ".db-journal", ".db-wal", ".db-shm",
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
    enabled = normalize_enabled_agents(state.get("enabled_agents"))
    # 파이프라인의 쓰기 단계는 모두 목표 사이클 안에 있다 — 그 단계 표와 위치로 판단한다.
    if coding_iteration_state(state):
        steps = goal_coding_steps(enabled)
        step_index = int(state.get("coding_cycle_step", 0))
    else:
        steps = steps_for_mode(state.get("mode", "discussion"), enabled)
        step_index = int(state.get("step_index", 0))
    if step_index >= len(steps):
        return False
    expected_agent, expected_phase, _instruction, _cli_mode = steps[step_index]
    actual_phase = str(last.get("phase", "")).split(" · ", 1)[0]
    return last.get("agent") == expected_agent and actual_phase == expected_phase


def coding_iteration_state(state: dict) -> bool:
    return state.get("mode") == "coding" and state.get("coding_stage") == "iteration"


def continue_after_failed_step() -> dict:
    with STATE_LOCK:
        if not can_continue_after_failed_step(STATE, STATE.get("messages", [])):
            return {"error": "다음 단계로 넘길 수 있는 실패 턴이 없습니다."}
        skipped = STATE["messages"][-1]
        position = "coding_cycle_step" if coding_iteration_state(STATE) else "step_index"
        STATE[position] = int(STATE.get(position, 0)) + 1
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
    if normalized in ("read", "write"):
        resolution = resolve_project_workspace(require_write=normalized == "write")
        if resolution.blocked:
            return {"error": workspace_block_message(resolution)}
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
    candidate = Path(path_text.strip()).expanduser()
    if not path_text.strip() or not candidate.is_absolute():
        # Path("")·상대 경로는 서버 실행 폴더로 풀려 그대로 저장된다 (fail closed 우회).
        return {"error": "작업 폴더가 지정되지 않았습니다. '작업 폴더 선택'으로 폴더를 골라주세요."}
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return {"error": "선택한 작업 폴더를 찾을 수 없습니다."}
    if not candidate.is_dir():
        return {"error": "작업영역은 폴더만 선택할 수 있습니다."}
    if not os.access(candidate, os.R_OK):
        return {"error": "선택한 폴더에 읽기 권한이 없습니다."}
    with STATE_LOCK:
        coding_session = STATE.get("mode") == "coding"
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


def restore_saved_state() -> None:
    """디스크의 마지막 세션 상태를 STATE/CONTROL에 복원한다. 서버 시작 시(main) 한 번만 부른다.

    예전에는 모듈 임포트 때 실행돼서, 테스트가 임포트만 해도 실제 roundtable_state.json을
    읽고 정규화 결과가 다르면 다시 써버렸다.
    """
    loaded = load_state()
    restored_interventions = list(loaded.get("pending_interventions", []))
    with STATE_LOCK:
        STATE.clear()
        STATE.update(loaded)
        CONTROL["stopped"] = bool(STATE.get("mode") == "coding" and STATE.get("coding_stopped"))
        CONTROL["intervention_queue"] = restored_interventions
        CONTROL["intervention_pending"] = bool(restored_interventions)
        CONTROL["intervention_intent"] = (
            restored_interventions[0].get("intent", "") if restored_interventions else ""
        )

    restored_approval_requesters = unresolved_approval_requesters(STATE.get("messages", []))
    if restored_approval_requesters and not STATE.get("finished", False):
        CONTROL["approval_requested"] = True
        CONTROL["approval_requested_by"] = restored_approval_requesters
        CONTROL["awaiting_approval"] = True
        if not waiting_for_user_response(STATE):
            STATE["workflow_status"] = workflow.WAITING_FOR_USER_APPROVAL

    orphaned_approval_agents = orphaned_approval_followup_agents(STATE.get("messages", []))
    if orphaned_approval_agents and not any(
        item.get("approved_followup") for item in CONTROL["intervention_queue"]
    ):
        CONTROL["intervention_queue"].append({
            "intent": "execute",
            "targets": orphaned_approval_agents,
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
            "text": clicheck.redact(event.get("text", "작업 중"), 300),
        }
        if event.get("paths"):
            entry["paths"] = [str(path)[:240] for path in event["paths"][:20]]
        if event.get("cost_usd") is not None:
            entry["cost_usd"] = event["cost_usd"]
            # CLI 실행마다 한 번 오는 최종 비용이다. Claude 자동 교정 재시도처럼 한 턴에
            # 두 번 실행되면 둘 다 더한다 (마지막 값만 쓰면 첫 실행 비용이 빠진다).
            STATE["active_cost_usd"] = STATE.get("active_cost_usd", 0.0) + float(event["cost_usd"] or 0.0)
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
        STATE["workflow_status"] = workflow.WAITING_FOR_USER_APPROVAL
        save_state(STATE)


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
        # meta는 세션 파일에서 그대로 오므로 키가 빠져 있을 수 있다. 예전에는
        # meta["elapsed"] 하나가 없으면 KeyError로 대화 전체 렌더링이 죽었다.
        run_status = str(meta.get("run_status", "") or "")
        status_html = ""
        if run_status and run_status != runstatus.RunStatus.COMPLETED:
            status_html = (
                f' · <span class="turn-status" data-status="{html.escape(run_status)}">'
                f'{html.escape(runstatus.status_glyph(run_status))} '
                f'{html.escape(runstatus.status_label(run_status))}</span>'
            )
        truncated_html = (
            ' · <span class="turn-truncated" title="화면 표시만 줄였습니다. 전체 원문은 '
            'roundtable_memory의 full.md에 그대로 보관됩니다.">표시 축약됨</span>'
            if meta.get("output_truncated") else ""
        )
        partial_html = (
            ' · <span class="turn-partial">중단 전 부분 출력</span>'
            if meta.get("partial_output") else ""
        )
        stats_html = (
            f'<div class="stats">⏱ {meta.get("elapsed", "-")}초 · '
            f'추정 토큰 ~{meta.get("est_tokens", 0)} · '
            f'입력 {meta.get("prompt_chars", 0)}자/출력 {meta.get("output_chars", 0)}자 · '
            f'{html.escape(str(meta.get("cli_mode", "-")))}{changes_html}'
            f'{status_html}{partial_html}{truncated_html}</div>'
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
    if waiting_for_user_response(STATE):
        return "사용자 답변 대기 중"
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
    if STATE.get("mode") == "coding" and STATE.get("coding_stage") == "iteration":
        return f"목표 기반 코딩 · 사이클 {max(1, int(STATE.get('coding_cycle', 1) or 1))}"
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


def coding_limit_reason(state: dict) -> str | None:
    if state.get("mode") != "coding" or state.get("coding_stage") != "iteration":
        return None
    limits = normalize_coding_limits(state.get("coding_limits"))
    progress = normalize_coding_progress(state.get("coding_progress"))
    cycle = max(1, int(state.get("coding_cycle", 1) or 1))
    if cycle > limits["max_cycles"]:
        return f"최대 목표 사이클 {limits['max_cycles']}회 완료"
    started_at = float(state.get("coding_started_at", 0) or 0)
    if started_at and time.time() - started_at >= limits["max_minutes"] * 60:
        return f"최대 실행 시간 {limits['max_minutes']}분 도달"
    used_tokens = max(
        int(state.get("total_actual_tokens", 0) or 0) - int(state.get("coding_start_actual_tokens", 0) or 0),
        int(state.get("total_est_tokens", 0) or 0) - int(state.get("coding_start_est_tokens", 0) or 0),
    )
    if used_tokens >= limits["max_tokens"]:
        return f"최대 토큰 {limits['max_tokens']:,} 도달"
    if len(progress["changed_paths"]) >= limits["max_changed_files"]:
        return f"최대 변경 파일 {limits['max_changed_files']}개 도달"
    if progress["diff_lines"] >= limits["max_diff_lines"]:
        return f"최대 diff {limits['max_diff_lines']:,}줄 도달"
    if progress["consecutive_failures"] >= limits["max_consecutive_failures"]:
        return f"연속 실패 {limits['max_consecutive_failures']}회 도달"
    if progress["same_error_count"] >= limits["max_same_error"]:
        return f"동일 오류 {limits['max_same_error']}회 반복"
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


def plan_approval_missing(state: dict) -> bool:
    """코딩 모드인데 사용자가 아직 계획을 승인하지 않았는가 — 그동안 쓰기 턴은 허용하지 않는다."""
    return state.get("mode") == "coding" and not state.get("plan_approved")


def approve_pending_work() -> bool:
    was_waiting = CONTROL["awaiting_approval"] or CONTROL["approval_requested"]
    with STATE_LOCK:
        approval_requests = unresolved_approval_requests(STATE.get("messages", []))
        if was_waiting and STATE.get("mode") == "coding":
            STATE["plan_approved"] = True
            save_state(STATE)
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
        if not waiting_for_user_response(STATE):
            STATE["workflow_status"] = (
                workflow.CODING
                if STATE.get("mode") == "coding"
                else workflow.DEBATING
            )
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
        STATE["workflow_status"] = workflow.WAITING_FOR_USER_APPROVAL
        save_state(STATE)
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
        if waiting_for_user_response(STATE):
            return False
        budget_reason = budget_exceeded_reason(STATE)
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
    with STATE_LOCK:
        approval_missing = plan_approval_missing(STATE)
    if cli_mode == "coding" and approval_missing:
        # 모든 쓰기 턴(정규 단계·개입·에이전트 호출)이 여기를 지난다. 승인 전에는 읽기 전용으로만 실행한다.
        cli_mode = "discussion"
        instruction = (
            f"{instruction}\n\n"
            "사용자가 아직 계획을 승인하지 않았다. 파일을 생성·수정·삭제하지 말고, "
            "분석 결과와 변경 제안만 보고해라."
        )
        add_runtime_event(f"{AGENTS[agent]['label']} 코딩 요청을 승인 전이라 읽기 전용으로 전환")
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
        STATE["active_cost_usd"] = 0.0
        STATE["workflow_status"] = (
            workflow.CODING if cli_mode == "coding" else workflow.DEBATING
        )
        STATE["run_status"] = runstatus.RunStatus.RUNNING
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
    try:
        project_path = cli_working_directory(cli_mode)
    except WorkspaceUnavailable as exc:
        message = workspace_block_message(exc.resolution)
        add_runtime_event(f"작업 폴더 차단으로 실행하지 않음 — {exc.resolution.message}", "error")
        add_message(agent, phase, message, meta={
            "run_status": runstatus.RunStatus.CLI_FAILED,
            "workspace_blocked": True,
        })
        # 실패로 돌려줘야 워커가 단계를 넘기지 않고, 개입 요청도 재시도 큐에 남는다
        # (CONTROL["stopped"]를 켜면 process_pending_intervention이 요청을 버린다).
        with STATE_LOCK:
            STATE["workflow_status"] = workflow.FAILED
            save_state(STATE)
        return False
    before_snapshot, before_truncated = (
        snapshot_project_tree(project_path) if cli_mode == "coding" else ({}, False)
    )
    before_texts = capture_project_texts(project_path) if cli_mode == "coding" else {}
    if CONTROL["stopped"]:
        # 프롬프트 작성·스냅샷 중에 /stop이 눌렸다. 아직 띄운 CLI가 없으니 여기서 멈춘다.
        with STATE_LOCK:
            if STATE.get("id") == session_id:
                STATE["active_agent"] = None
                STATE["active_phase"] = None
                STATE["active_started_at"] = None
                STATE["active_cli_mode"] = None
                STATE["active_prompt_chars"] = 0
                save_state(STATE)
        return False
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
    # 사용자가 /stop을 눌러 CLI 프로세스를 죽였다면, 뒤따라오는 "종료 코드 1"은
    # 중단의 결과일 뿐 모델 실패가 아니다. 반드시 먼저 판단한다.
    turn_cancelled = bool(CONTROL.get("user_cancelled"))
    turn_timed_out = "실행 시간 초과" in raw_text or "TimeoutExpired" in raw_text
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
        workspace=project_path,
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
        actual_cost_usd = round(float(STATE.get("active_cost_usd", 0.0) or 0.0), 6)
    question_clean_text, pending_question = workflow.extract_stop_and_ask_user(raw_text)
    call_clean_text, agent_calls = extract_agent_calls(question_clean_text, agent, cli_mode)
    if not allow_agent_calls(state_snapshot.get("mode", "discussion"), phase):
        agent_calls = []
    visible_text, approval_requested = extract_approval_token(call_clean_text)
    # 긴 답변은 아래에서 잘리므로 판정은 자르기 전에 읽는다.
    review_verdict = ""
    if phase.split(" · ", 1)[0] == REVIEW_PHASE:
        visible_text, review_verdict = extract_review_verdict(visible_text)
    if pending_question:
        agent_calls = []
        approval_requested = False
    requested_role = extract_role_selection(visible_text) if phase in {"역할 선언", "역할 확정"} else ""
    if phase in {"역할 선언", "역할 확정"}:
        visible_text = strip_role_selection(visible_text)
    approval_requested = should_honor_approval_request(
        approval_requested,
        state_snapshot.get("mode", "discussion"),
        phase,
    )
    text, output_truncated = clip_agent_output(visible_text)
    # 대화·로그·메모리·전사본에 남기 전에 API 키·토큰으로 보이는 값을 가린다.
    text = clicheck.redact(text, None)
    if pending_question and not text:
        text = pending_question["question"]
    if approval_requested and not text:
        text = "사용자 승인을 요청했습니다."
    est_tokens = estimate_tokens(prompt, raw_text)
    outcome = runstatus.classify_turn(
        text,
        cancelled=turn_cancelled,
        timed_out=turn_timed_out,
        failure_marker=is_cli_failure(raw_text),
        had_partial_output=bool(text.strip()),
    )
    if outcome.status == runstatus.RunStatus.USER_CANCELLED:
        # 중단은 실패가 아니다 — 실패 재시도 CTA를 띄우지 않는다.
        failed = False
    else:
        failed = outcome.is_failure or bool(scope_violations)
    if scope_violations and outcome.status == runstatus.RunStatus.COMPLETED:
        outcome = runstatus.TurnOutcome(
            status=runstatus.RunStatus.CLI_FAILED,
            text=text,
            detail="역할 범위를 벗어난 파일을 변경했습니다.",
        )
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
        "review_verdict": review_verdict,
        "ok": not failed,
        "run_status": outcome.status,
        "run_status_label": outcome.label,
        "run_status_detail": outcome.detail,
        "partial_output": outcome.partial,
        "failure_kind": (
            "role_scope" if scope_violations
            else ("user_cancelled" if outcome.status == runstatus.RunStatus.USER_CANCELLED
                  else ("cli" if failed else ""))
        ),
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
        "user_question_requested": bool(pending_question),
        "delegation_depth": delegation_depth,
        "coding_cycle": (
            max(1, int(state_snapshot.get("coding_cycle", 1) or 1))
            if state_snapshot.get("coding_stage") == "iteration" else 0
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
            if state_snapshot.get("mode") == "coding":
                progress = normalize_coding_progress(STATE.get("coding_progress"))
                progress["changed_paths"] = list(dict.fromkeys([
                    *progress["changed_paths"],
                    *(item["path"] for item in changed_paths),
                ]))[:500]
                progress["diff_lines"] += len(turn_diff.splitlines())
                if failed:
                    signature = (
                        "role_scope:" + ",".join(scope_violations)
                        if scope_violations else raw_text.strip()[:500]
                    )
                    progress["consecutive_failures"] += 1
                    progress["same_error_count"] = (
                        progress["same_error_count"] + 1
                        if signature == progress["last_error_signature"] else 1
                    )
                    progress["last_error_signature"] = signature
                else:
                    progress["consecutive_failures"] = 0
                STATE["coding_progress"] = progress
            STATE["active_agent"] = None
            STATE["active_phase"] = None
            STATE["active_started_at"] = None
            STATE["active_cli_mode"] = None
            STATE["active_prompt_chars"] = 0
            STATE["active_work_log"] = []
            STATE["active_usage"] = {}
            # 상단 배너·세션 목록·인스펙터가 모두 이 한 값을 본다.
            STATE["run_status"] = outcome.status
            if outcome.status == runstatus.RunStatus.USER_CANCELLED:
                STATE["workflow_status"] = workflow.CANCELLED
            save_state(STATE)
    if not same_session:
        print(f"  \u26a0\ufe0f {label} 응답은 새 세션이 시작되어 폐기했습니다.")
        return False
    if outcome.status == runstatus.RunStatus.USER_CANCELLED:
        level, result_label = "warn", "사용자 중단"
    elif failed:
        level, result_label = "error", "실패"
    else:
        level, result_label = "info", "완료"
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
    if added and not failed and pending_question:
        register_user_question(agent, phase, pending_question)
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
                if waiting_for_user_response(STATE):
                    break
                mode = STATE.get("mode", "discussion")
                enabled_agents = normalize_enabled_agents(STATE.get("enabled_agents"))
                coding_iteration = (
                    mode == "coding" and STATE.get("coding_stage") == "iteration"
                )
                steps = (
                    goal_coding_steps(enabled_agents)
                    if coding_iteration else steps_for_mode(mode, enabled_agents)
                )
                step_index = (
                    int(STATE.get("coding_cycle_step", 0))
                    if coding_iteration else int(STATE.get("step_index", 0))
                )
                cycle_number = (
                    max(1, int(STATE.get("coding_cycle", 1) or 1))
                    if coding_iteration else 0
                )
                limit_reason = coding_limit_reason(STATE) if coding_iteration else None
            if CONTROL["approval_requested"]:
                if not wait_for_user_approval(session_id):
                    break
                continue
            if process_pending_intervention(session_id):
                continue
            if limit_reason:
                with STATE_LOCK:
                    if STATE.get("id") == session_id:
                        STATE["finished"] = True
                        STATE["coding_stopped"] = True
                        STATE["coding_stop_reason"] = limit_reason
                        STATE["workflow_status"] = workflow.COMPLETED
                        save_state(STATE)
                add_runtime_event(f"목표 기반 코딩 종료: {limit_reason}")
                break
            if mode == "coding" and not coding_iteration and step_index >= len(steps):
                with STATE_LOCK:
                    if STATE.get("id") != session_id:
                        break
                    STATE["coding_stage"] = "iteration"
                    STATE["coding_cycle"] = 1
                    STATE["coding_cycle_step"] = 0
                    # 시간·토큰 한도는 목표 사이클부터 잰다. 계획·승인 단계(사용자 대기 포함)가
                    # 한도를 먼저 써버리면 사이클 1이 시작하자마자 끝난다.
                    STATE["coding_started_at"] = time.time()
                    STATE["coding_start_actual_tokens"] = int(STATE.get("total_actual_tokens", 0) or 0)
                    STATE["coding_start_est_tokens"] = int(STATE.get("total_est_tokens", 0) or 0)
                    STATE["coding_stopped"] = False
                    STATE["coding_stop_reason"] = ""
                    STATE["finished"] = False
                    save_state(STATE)
                add_runtime_event("계획·승인 단계 완료 · 목표 기반 코딩 사이클 1 시작")
                continue
            if coding_iteration and step_index >= len(steps):
                with STATE_LOCK:
                    STATE["coding_cycle"] = cycle_number + 1
                    STATE["coding_cycle_step"] = 0
                    save_state(STATE)
                continue
            if mode != "coding" and step_index >= len(steps):
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

            cycle_index = step_index
            agent, phase, instruction, cli_mode = steps[cycle_index]
            with STATE_LOCK:
                approval_missing = plan_approval_missing(STATE)
            if cli_mode == "coding" and approval_missing:
                # 승인 없이 쓰기 단계에 도달했다 (거절 뒤 메시지, 서버 재시작, 모드 전환 등).
                # 읽기 전용으로 헛돌리지 말고 승인을 다시 요청한다 — 루프 맨 위에서 대기한다.
                mark_approval_requested(agent, CONFIRM_PHASE)
                continue
            display_phase = f"{phase} · 목표 사이클 {cycle_number}" if coding_iteration else phase
            if coding_iteration and cycle_index == 0:
                add_runtime_event(f"목표 기반 코딩 사이클 {cycle_number} 시작")
            succeeded = run_step(
                agent,
                display_phase,
                instruction,
                cli_mode,
                expected_session_id=session_id,
            )
            if not succeeded:
                if CONTROL["stopped"]:
                    break  # 사용자 중단 — 실패로 기록하지 않는다 (/stop이 이미 상태를 정했다).
                with STATE_LOCK:
                    if STATE.get("id") == session_id:
                        CONTROL["paused"] = True
                        if not waiting_for_user_response(STATE):
                            STATE["workflow_status"] = workflow.FAILED
                        save_state(STATE)
                break

            with STATE_LOCK:
                if STATE.get("id") != session_id:
                    break
                cycle_completed = False
                if coding_iteration:
                    STATE["coding_cycle_step"] += 1
                    if STATE["coding_cycle_step"] >= len(steps):
                        STATE["coding_cycle"] = cycle_number + 1
                        STATE["coding_cycle_step"] = 0
                        cycle_completed = True
                    next_index = STATE["coding_cycle_step"]
                else:
                    STATE["step_index"] += 1
                    next_index = STATE["step_index"]
                save_state(STATE)

            if coding_iteration and cycle_completed:
                add_runtime_event(f"목표 기반 코딩 사이클 {cycle_number} 완료")
            # 검토 전에 한 번 검증한다. 검토 단계가 없으면(Codex 꺼짐) 사이클 끝에 검증한다.
            has_review_step = any(step[1] == REVIEW_PHASE for step in steps)
            should_validate = cli_mode == "coding" and (
                (coding_iteration and not cycle_completed and steps[next_index][1] == REVIEW_PHASE)
                or (coding_iteration and cycle_completed and not has_review_step)
                or (not coding_iteration and (
                    next_index >= len(steps) or steps[next_index][1] == "최종 보고"
                ))
            )
            if should_validate:
                add_runtime_event("자동 검증 시작")
                with STATE_LOCK:
                    if STATE.get("id") == session_id:
                        STATE["workflow_status"] = workflow.VALIDATING
                        save_state(STATE)
                validation_results = run_validation_for_current_workspace()
                with STATE_LOCK:
                    if STATE.get("id") == session_id:
                        STATE["validation_results"] = validation_results
                        failed_validations = [
                            result for result in validation_results if not result["ok"]
                        ]
                        if failed_validations and mode == "coding":
                            progress = normalize_coding_progress(STATE.get("coding_progress"))
                            signature = "validation:" + "|".join(
                                result["label"] for result in failed_validations
                            )
                            progress["consecutive_failures"] += 1
                            progress["same_error_count"] = (
                                progress["same_error_count"] + 1
                                if signature == progress["last_error_signature"] else 1
                            )
                            progress["last_error_signature"] = signature
                            STATE["coding_progress"] = progress
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
                    mode in ("discussion", "quick")
                    and STATE["step_index"] >= len(steps)
                    and not CONTROL["stopped"]
                ) or bool(STATE.get("finished", False))
                STATE["finished"] = finished
                if finished:
                    STATE["workflow_status"] = workflow.COMPLETED
                STATE["active_agent"] = None
                STATE["active_phase"] = None
                STATE["active_started_at"] = None
                STATE["active_cli_mode"] = None
                STATE["active_prompt_chars"] = 0
                messages_copy = list(STATE["messages"])
                # 완료 뒤 개입으로 워커가 다시 끝날 때마다 전체 대화를 프로필에 또 붙이지 않는다.
                profiled_count = int(STATE.get("profiled_message_count", 0) or 0)
                if finished:
                    STATE["profiled_message_count"] = len(messages_copy)
                save_state(STATE)
            if CONTROL.get("worker_session_id") == session_id:
                CONTROL["worker_running"] = False
                CONTROL["worker_session_id"] = None
                restart_pending = CONTROL.get("worker_start_pending", False)
                CONTROL["worker_start_pending"] = False
        if same_session:
            render_html_snapshot()
        if same_session and finished:
            for agent in ("codex", "antigravity", "claude"):
                update_profile(agent, messages_copy[profiled_count:])
        separator("워커 종료" if not finished else "완료")
        if restart_pending:
            start_worker_if_needed(force=True)


def start_worker_if_needed(force: bool = False) -> None:
    with STATE_LOCK:
        session_id = STATE.get("id")
        if waiting_for_user_response(STATE):
            return
        if CONTROL["worker_running"]:
            if CONTROL.get("worker_session_id") != session_id:
                CONTROL["worker_start_pending"] = True
            return
        CONTROL["worker_running"] = True
        CONTROL["worker_session_id"] = session_id
        CONTROL["worker_start_pending"] = False
        CONTROL["stopped"] = False
        # 이전 실행의 중단 표시가 남아 있으면 새 실행의 진짜 실패까지 "사용자 중단"으로 분류된다.
        CONTROL["user_cancelled"] = False
        if STATE.get("coding_stopped"):
            STATE["coding_stopped"] = False
            STATE["coding_stop_reason"] = ""
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
        icon = "✅" if info.get("ok") else "❌"
        # 읽기와 쓰기를 별도 상태로 보여준다. "버전 확인됨 = 연결됨"으로 뭉뚱그리면
        # Codex처럼 읽기는 되고 쓰기는 실패하는 상태가 초록불로 보인다.
        capability = []
        if info.get("can_read"):
            capability.append("읽기 ✓")
        if info.get("can_write"):
            capability.append("쓰기 ✓")
        elif info.get("failed_step") == "write":
            capability.append("쓰기 ✕")
        capability_html = (
            f' <small class="conn-caps">{html.escape(" · ".join(capability))}</small>'
            if capability else ""
        )

        if info.get("ok"):
            detail = info.get("version") or info.get("detail", "")
            title = detail
        else:
            step = info.get("failed_step", "")
            detail = f'[{step}] {info.get("failed_detail", "")}' if step else info.get("detail", "")
            # 사용자가 취할 조치를 툴팁으로 붙인다.
            title = f'{detail}\n{info.get("action", "")}'.strip()

        items.append(
            f'<span class="conn-item" title="{html.escape(title[:300])}">'
            f'{icon} {html.escape(info["label"])}{capability_html} '
            f'<small>{html.escape(str(detail)[:70])}</small></span>'
        )
    return "".join(items)


MODE_LABELS = {
    "quick": "빠른 질문",
    "discussion": "일반 토론 모드",
    "coding": "코딩 모드",
}

#: 각 모드의 실행 계획 — 시작 전에 화면에 그대로 보여준다.
MODE_PLANS = {
    "quick": {
        "summary": "선택한 모델 1명을 한 번만 호출합니다.",
        "min_calls": 1,
        "max_calls": 1,
        "single_agent": True,
        "role_negotiation": False,
        "auto_validation": False,
        "default_access": "read",
        "allow_write": False,
        "note": "강점 분석·역할 협의·보고 단계를 생략하고, 길이와 형식은 요청한 대로 따릅니다.",
    },
    "discussion": {
        "summary": "활성 모델들이 순서대로 발언한 뒤 한 모델이 정리 보고를 합니다.",
        "single_agent": False,
        "role_negotiation": True,
        "auto_validation": False,
        "default_access": "read",
        "allow_write": True,
        "note": "모델 수와 단계 수에 따라 시간이 길어질 수 있습니다.",
    },
    "coding": {
        "summary": "계획 → 사용자 승인 → 구현 → 자동 검증 순으로 진행합니다.",
        "single_agent": False,
        "role_negotiation": True,
        "auto_validation": True,
        "default_access": "write",
        "allow_write": True,
        "note": "쓰기 권한과 유효한 작업 폴더가 확인되어야 시작합니다.",
    },
}


def quick_steps(enabled_agents: list[str] | None, project_access: str = "read") -> list[tuple[str, str, str, str]]:
    """빠른 질문: 모델 1명을 한 번만 부른다.

    역할 협의·강점 분석·보고 단계를 만들지 않는다. 한 문장 요청에 최소 3회
    모델을 호출하던 게 이 모드를 만든 이유다.
    """
    enabled = normalize_enabled_agents(enabled_agents)
    agent = enabled[0]
    instruction = (
        "사용자의 요청에 곧바로 답해라. 역할 선언·강점 분석·다른 모델과의 협의는 하지 마라. "
        "사용자가 길이나 형식을 지정했다면 그것을 다른 어떤 기본 형식보다 우선해라. "
        "지정이 없으면 간결하게 답해라."
    )
    return [(agent, "빠른 답변", instruction, "discussion")]


def mode_execution_plan(
    mode: str,
    enabled_agents: list[str] | None,
    discussion_project_access: str = "read",
    workspace_access: str = "read",
) -> dict:
    """실행 전에 보여줄 계획 — 모델 수, 예상 호출 횟수, 권한, 작업공간."""
    mode = "coding" if mode == "continuous" else mode
    if mode not in MODE_LABELS:
        mode = "discussion"
    plan = dict(MODE_PLANS.get(mode, MODE_PLANS["discussion"]))
    enabled = normalize_enabled_agents(enabled_agents)
    if plan.get("single_agent"):
        enabled = enabled[:1]

    if mode == "quick":
        min_calls = max_calls = 1
    else:
        step_count = len(steps_for_mode(mode, enabled))
        min_calls = step_count
        # 개입·재시도·목표 사이클로 호출이 늘어날 수 있다.
        max_calls = step_count * (3 if mode == "coding" else 2)

    workspace = resolve_project_workspace()
    access = "write" if mode == "coding" else normalize_discussion_project_access(
        discussion_project_access
    )
    if not plan.get("allow_write", True):
        access = normalize_discussion_project_access(discussion_project_access)
        if access == "write":
            access = "read"
    return {
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "summary": plan["summary"],
        "note": plan.get("note", ""),
        "agents": enabled,
        "agent_labels": [AGENTS[a]["label"] for a in enabled],
        "agent_count": len(enabled),
        "min_calls": min_calls,
        "max_calls": max_calls,
        "long_running_warning": max_calls >= 6,
        "project_access": access,
        "project_access_label": discussion_project_access_label(access),
        "can_write": access == "write",
        "auto_validation": bool(plan.get("auto_validation")),
        "role_negotiation": bool(plan.get("role_negotiation")),
        "workspace_path": str(workspace.path) if workspace.ok else "",
        "workspace_ok": workspace.ok,
        "workspace_message": workspace.message,
        "workspace_configured": workspace.configured,
    }


def mode_notice_html(mode: str, discussion_project_access: str = "read") -> str:
    mode_label = MODE_LABELS.get(mode, mode)
    if mode == "coding":
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
            f'<label class="agent-toggle"><input type="checkbox" name="agent" value="{agent}" checked '
            f'onchange="syncDirectAgentButtons()">'
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
    workspace_resolution = resolve_project_workspace()
    if workspace_resolution.ok:
        project_path_line = (
            f'<p>코딩 모드 작업 경로: '
            f'{html.escape(str(workspace_resolution.path))} '
            f'· 권한: {access_label}</p>'
        )
    else:
        project_path_line = (
            '<p class="workspace-blocked" role="status">'
            '<strong>작업 폴더 차단됨</strong> — '
            f'{html.escape(workspace_resolution.message)}<br>'
            f'저장된 경로: <code>{html.escape(workspace_resolution.configured or "(설정 없음)")}</code><br>'
            '유효한 폴더를 다시 선택하기 전까지 모델 실행이 차단됩니다.</p>'
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
            f'<p>첫 실행 모델: {html.escape(AGENTS[enabled_agents[0]]["label"])}</p>'
            f'<p>모델: {html.escape(settings_label)}</p>'
            f'<p>프로젝트 접근: {"읽기·쓰기 (코딩 실행)" if mode == "coding" else html.escape(discussion_access_label)}</p>'
            f'<p>저장 폴더: {html.escape(str(session_memory_dir(active_id)))}</p>'
            f'{project_path_line if mode == "coding" else ""}</div>'
        )

    return f"""
      <div class="panel">
        <p style="margin:0;color:var(--muted);font-size:13px">토론 주제를 입력하면 바로 시작합니다.</p>
        <label class="visually-hidden" for="topicInput">토론 주제 또는 질문</label>
        <textarea id="topicInput" placeholder="예: 네이버 쇼핑 최저가 비교 웹앱을 같이 만들 거야" autofocus aria-describedby="topicError"></textarea>
        <fieldset class="mode-choice">
          <legend>실행 모드</legend>
          <label><input type="radio" name="mode" value="quick" onchange="refreshExecutionPlan()"> 빠른 질문 (모델 1명 · 1회 호출 · 읽기 전용)</label>
          <label><input type="radio" name="mode" value="discussion" checked onchange="refreshExecutionPlan()"> 일반 토론 모드</label>
          <label><input type="radio" name="mode" value="coding" onchange="refreshExecutionPlan()"> 코딩 모드 (계획·승인 후 목표 기반 구현·검토 반복)</label>
        </fieldset>
        <p class="inline-error" id="topicError" role="alert" aria-live="assertive" hidden></p>
        <div class="primary-start">
          <button type="button" class="start-button" onclick="submitTopic()">
            시작하기
          </button>
          <button type="button" class="ghost compact" onclick="toggleStartOptions()"
                  id="startOptionsToggle" aria-expanded="false" aria-controls="startOptions">
            세부 설정
          </button>
        </div>
        <div class="execution-plan" id="executionPlan" role="status" aria-live="polite"></div>

        <div class="start-options" id="startOptions" hidden>
          <fieldset class="discussion-access-choice">
            <legend>프로젝트 접근</legend>
            <label><input type="radio" name="discussion_project_access" value="read" checked onchange="refreshExecutionPlan()"> 프로젝트 읽기</label>
            <label><input type="radio" name="discussion_project_access" value="write" onchange="refreshExecutionPlan()"> 프로젝트 읽기·쓰기</label>
            <label><input type="radio" name="discussion_project_access" value="none" onchange="refreshExecutionPlan()"> 프로젝트 읽지 않고 토론</label>
          </fieldset>
          {agent_model_cards_html(agent_settings)}
          {project_path_line}
          <div class="direct-agent-start">
            <strong>Agent 직접 호출</strong>
            <span>선택한 모델이 첫 턴을 시작합니다.</span>
            <div class="direct-agent-buttons">
              {''.join(
                  f'<button type="button" class="direct-agent-button" data-direct-agent="{agent}" '
                  f'onclick="submitTopic(\'{agent}\')" style="--agent-color:{AGENTS[agent]["color"]}">'
                  f'<img src="{html.escape(AGENTS[agent]["avatar"])}" alt="">'
                  f'<span>{html.escape(AGENTS[agent]["label"])}부터 시작</span></button>'
                  for agent in AGENT_ORDER
              )}
            </div>
          </div>
        </div>
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
    # 최초 페인트에는 최근 메시지만 넣는다. 1,500개짜리 세션에서 전체를 HTML로
    # 굽던 게 첫 화면이 1MB를 넘던 원인이다. 나머지는 "이전 메시지 불러오기"로.
    total_messages = len(messages)
    recent = messages[-INITIAL_MESSAGE_WINDOW:] if total_messages else []
    first_rendered_index = total_messages - len(recent)
    if recent:
        message_bubbles = "".join(bubble_html(m) for m in recent)
    else:
        message_bubbles = '<div class="empty-state">아직 대화가 없습니다.</div>'
    if first_rendered_index > 0:
        message_bubbles = (
            '<div class="load-older" id="loadOlderWrap">'
            '<button type="button" class="ghost" onclick="loadOlderMessages()">'
            f'이전 메시지 {first_rendered_index:,}개 불러오기</button></div>'
        ) + message_bubbles
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
        # 주제가 없으면(=아직 시작 전) composer를 화면 하단에 고정하지 않는다.
        # 고정하면 모바일에서 주제 패널과 시작 버튼을 덮는다.
        "__COMPOSER_STATE__": "no-topic" if not topic else "has-topic",
        # 상태를 바꾸는 모든 요청이 제시해야 하는 토큰. 외부 페이지는 이 HTML을
        # 읽을 수 없으므로 값을 알 수 없다.
        "__CSRF_TOKEN__": html.escape(CSRF_TOKEN),
        "__FEED_FIRST_INDEX__": str(first_rendered_index),
        "__FEED_LAST_INDEX__": str(total_messages - 1),
        "__MESSAGE_TOTAL__": str(total_messages),
    }
    # 한 번에 치환한다. 순서대로 replace하면 앞에서 넣은 주제·메시지 안의
    # "__CSRF_TOKEN__" 같은 글자가 뒤 단계에서 진짜 값으로 바뀐다.
    pattern = re.compile("|".join(map(re.escape, replacements)))
    return pattern.sub(lambda match: replacements[match.group(0)], page)


#: 최초 진입 시 내려보낼 최근 메시지 개수. 나머지는 "이전 메시지 불러오기"로 가져온다.
INITIAL_MESSAGE_WINDOW = int(os.environ.get("ROUNDTABLE_INITIAL_MESSAGES", "60"))
MAX_MESSAGE_PAGE = 200


def _messages_for_session(session_id: str) -> tuple[list[dict], str] | None:
    clean_id = (session_id or "").strip()
    with STATE_LOCK:
        active_id = STATE.get("id", "")
        if not clean_id or clean_id == active_id:
            return list(STATE["messages"]), active_id
    data = load_session(clean_id)
    if data is None:
        return None
    return list(data.get("messages", [])), clean_id


def messages_payload(session_id: str = "", after: str = "-1", limit: str = "80") -> dict:
    """메시지를 증분으로 돌려준다.

    ``after``는 마지막으로 받은 메시지의 인덱스. -1이면 "최근 ``limit``개".
    상태 폴링(active_tick 등)이 1,500개 메시지를 매번 다시 실어 나르지 않게 하는
    것이 목적이다.
    """
    try:
        after_index = int(after)
    except (TypeError, ValueError):
        after_index = -1
    try:
        page_size = max(1, min(MAX_MESSAGE_PAGE, int(limit)))
    except (TypeError, ValueError):
        page_size = 80

    found = _messages_for_session(session_id)
    if found is None:
        return {"error": "세션을 찾을 수 없습니다.", "messages_html": "", "total": 0}
    messages, resolved_id = found

    total = len(messages)
    if after_index < 0:
        start, end = max(0, total - page_size), total
    else:
        start = min(total, after_index + 1)
        end = min(total, start + page_size)

    chunk = messages[start:end]
    return {
        "session_id": resolved_id,
        "total": total,
        "from_index": start,
        "to_index": end - 1 if end > start else start - 1,
        "count": len(chunk),
        "has_older": start > 0,
        "has_newer": end < total,
        "messages_html": "".join(bubble_html(m) for m in chunk),
    }


def older_messages_payload(session_id: str, before: int, limit: int = 60) -> dict:
    """"이전 메시지 불러오기"용 역방향 페이지."""
    found = _messages_for_session(session_id)
    messages = found[0] if found else []
    end = max(0, min(len(messages), before))
    start = max(0, end - max(1, min(MAX_MESSAGE_PAGE, limit)))
    chunk = messages[start:end]
    return {
        "total": len(messages),
        "from_index": start,
        "to_index": end - 1,
        "count": len(chunk),
        "has_older": start > 0,
        "messages_html": "".join(bubble_html(m) for m in chunk),
    }


def state_json_payload(include_feed: bool = False) -> dict:
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
        coding_stage = STATE.get("coding_stage", "planning")
        coding_cycle = max(0, int(STATE.get("coding_cycle", 0) or 0))
        coding_limits = normalize_coding_limits(STATE.get("coding_limits"))
        coding_progress = normalize_coding_progress(STATE.get("coding_progress"))
        coding_stop_reason = str(STATE.get("coding_stop_reason", ""))
        workflow_status = workflow.normalize_workflow_status(
            STATE.get("workflow_status"), has_topic=bool(topic)
        )
        pending_user_question = workflow.normalize_user_question(
            STATE.get("pending_user_question")
        )
        if pending_user_question:
            # 승인 대기·중단이 상태 값을 덮어써도 화면은 계속 질문을 보여준다.
            workflow_status = workflow.WAITING_FOR_USER_RESPONSE
        run_status = str(STATE.get("run_status", "") or runstatus.RunStatus.IDLE)
        state_snapshot = dict(STATE)
    if CONTROL["paused"] and run_status == runstatus.RunStatus.RUNNING:
        run_status = runstatus.RunStatus.PAUSED
    workspace_state = resolve_project_workspace()
    if active_id and not session_roles_path(active_id).exists():
        ensure_session_memory(state_snapshot)
    active_elapsed = max(0.0, round(time.time() - active_started_at, 1)) if active_started_at else 0.0
    if include_feed:
        message_bubbles = "".join(bubble_html(m) for m in messages) if messages else \
            '<div class="empty-state">아직 대화가 없습니다.</div>'
        bubbles = (mode_notice_html(mode, discussion_project_access) if topic else "") + message_bubbles
    else:
        # 폴링 응답에는 메시지 본문을 싣지 않는다. 클라이언트가 /messages.json으로
        # 새 메시지만 증분 조회한다 (1.2MB 세션을 1.8초마다 다시 보내던 비용 제거).
        bubbles = None
    disabled_agents = [a for a in AGENT_ORDER if a not in enabled_agents]
    last_model_message = next((message for message in reversed(messages) if message.get("agent") in AGENT_ORDER), None)
    last_meta = (last_model_message.get("meta") or {}) if last_model_message else {}
    can_retry = bool(
        CONTROL["paused"]
        and last_model_message
        and not last_meta.get("ok", True)
        # 사용자가 스스로 멈춘 턴에는 "실패 턴 재시도"를 권하지 않는다.
        and runstatus.allows_failure_retry(last_meta.get("run_status", runstatus.RunStatus.CLI_FAILED))
    )
    can_continue_next = bool(
        CONTROL["paused"]
        and can_continue_after_failed_step(state_snapshot, messages)
        and not budget_exceeded_reason(state_snapshot)
    )
    payload = {
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
        "workflow_status": workflow_status,
        "pending_user_question": pending_user_question,
        "topic": html.escape(topic) if topic else "",
        "session_name": session_name,
        "tags": list(STATE.get("tags", [])),
        "favorite": bool(STATE.get("favorite", False)),
        "archived": bool(STATE.get("archived", False)),
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "discussion_project_access": discussion_project_access,
        "discussion_project_access_label": discussion_project_access_label(discussion_project_access),
        "enabled_agents": enabled_agents,
        "start_agent": enabled_agents[0],
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
        "memory_dir": str(session_memory_dir(active_id)) if active_id else "",
        "profile_path": str(session_profile_path(active_id)) if active_id else "",
        "roles_path": str(session_roles_path(active_id)) if active_id else "",
        "workspace_path": str(project_path_or_none() or ""),
        "workspace_access": workspace_access,
        "workspace_access_label": "읽기·쓰기" if workspace_access == "write" else "읽기 전용",
        "total_est_tokens": total_est_tokens,
        "total_actual_tokens": total_actual_tokens,
        "total_actual_cost_usd": total_actual_cost_usd,
        "budget": budget,
        "budget_exceeded": budget_exceeded_reason({
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
        "coding_stage": coding_stage,
        "coding_cycle": coding_cycle,
        "coding_limits": coding_limits,
        "coding_progress": coding_progress,
        "coding_stop_reason": coding_stop_reason,
        "coding_limit_reason": coding_limit_reason(state_snapshot),
        "agent_usage": agent_usage_summary(messages),
        "context_usage": agent_context_summary(messages, agent_settings),
        "cost_breakdown": usage_domain.cost_breakdown(messages).as_dict(),
        "run_status": run_status,
        "run_status_label": runstatus.status_label(run_status),
        "run_status_glyph": runstatus.status_glyph(run_status),
        "mode_notice_html": mode_notice_html(mode, discussion_project_access) if topic else "",
        "workspace": workspace_state.as_dict(),
        "workspace_blocked": workspace_state.blocked,
        "csrf_token": CSRF_TOKEN,
        "save_error": LAST_SAVE_ERROR["message"],
    }
    if include_feed:
        payload["feed_html"] = bubbles
    return payload


def sessions_list_payload() -> dict:
    with STATE_LOCK:
        active_id = STATE.get("id", "")
        active_run_status = str(STATE.get("run_status", "") or "")
    if CONTROL["paused"] and active_run_status == runstatus.RunStatus.RUNNING:
        active_run_status = runstatus.RunStatus.PAUSED
    items = []
    for s in list_sessions():
        is_active = s["id"] == active_id
        # 상단 배너와 같은 계산을 쓴다. 예전에는 목록이 "활성 세션 = 진행 중"으로
        # 단정해서, 상단에 "중단됨"이 떠 있는데 목록은 "진행 중"인 모순이 났다.
        run_status = active_run_status if is_active else s.get("run_status", "")
        items.append({
            **s,
            "topic": s["topic"] or "(주제 없음)",
            "name": s["name"],
            "mode_label": MODE_LABELS.get(s["mode"], s["mode"]),
            "is_active": is_active,
            "run_status": run_status,
            "status_label": runstatus.session_list_status(
                is_active_session=is_active,
                has_topic=bool(s["topic"]),
                finished=bool(s["finished"]),
                run_status=run_status,
            ),
            "status_glyph": runstatus.status_glyph(run_status),
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
    """(토큰, 추정치 여부). 하위 호환용 얇은 래퍼."""
    used, source = usage_domain.last_call_context_tokens(messages, agent)
    return used, source != "measured"


def agent_context_summary(
    messages: list[dict], agent_settings: dict | None = None
) -> dict[str, dict]:
    """모델별 "현재 컨텍스트 점유율".

    CLI가 주는 turn 단위 usage는 그 턴 안의 모든 내부 호출 합계라서, 그대로
    컨텍스트 한도로 나누면 100%를 훌쩍 넘는다(실제로 461.1%가 표시됐다).
    이제는 호출 단위 스냅샷이 있을 때만 measured로 쓰고, 없으면 추정치임을
    명시하거나 "알 수 없음"으로 둔다. 세션 누적 토큰은 여기 쓰지 않는다.
    """
    summary = {}
    for agent in AGENT_ORDER:
        limit = max(1, context_limit_for_agent(agent, agent_settings))
        context = usage_domain.context_usage(messages, agent, limit)
        row = context.as_dict()
        # 누적 사용량은 별도 항목으로 — 절대 컨텍스트 퍼센트와 섞지 않는다.
        row["session_cumulative_tokens"] = usage_domain.cumulative_tokens(messages, agent)
        summary[agent] = row
    return summary


def session_detail_payload(session_id: str) -> dict | None:
    session_id = validate_session_id(session_id)
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
        "session_name": data.get("name") or data.get("topic") or "새 세션",
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
        "memory_dir": str(session_memory_dir(session_id)),
        "profile_path": str(session_profile_path(session_id)),
        "roles_path": str(session_roles_path(session_id)),
        "workspace_path": data.get("workspace_path") or str(project_path_or_none() or ""),
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
        "coding_stage": data.get("coding_stage", "planning"),
        "coding_cycle": max(0, int(data.get("coding_cycle", 0) or 0)),
        "coding_limits": normalize_coding_limits(data.get("coding_limits")),
        "coding_progress": normalize_coding_progress(data.get("coding_progress")),
        "coding_stop_reason": data.get("coding_stop_reason", ""),
    }


def profile_payload(session_id: str | None = None) -> dict | None:
    if session_id is not None:
        session_id = validate_session_id(session_id)
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
    session_id = validate_session_id(session_id)
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
    atomic_write_json(session_json_path(session_id), data)
    return {"success": True, "id": session_id, "name": clean_name, "is_active": False}


def update_session_metadata(session_id: str, tags: str, favorite: bool, archived: bool) -> dict:
    session_id = validate_session_id(session_id)
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
    atomic_write_json(session_json_path(session_id), data)
    return {"success": True, "id": session_id, "tags": clean_tags, "favorite": favorite, "archived": archived}


def delete_session(session_id: str) -> dict:
    session_id = validate_session_id(session_id)
    with STATE_LOCK:
        if STATE.get("id") == session_id:
            return {"error": "현재 세션은 삭제할 수 없습니다. 새 세션을 만든 뒤 삭제해주세요."}
    removed = False
    # 삭제는 되돌릴 수 없으므로, 각 경로가 정말 허용 폴더 안인지 다시 확인한다.
    for path in (session_json_path(session_id), session_transcript_path(session_id)):
        if path.is_file():
            path.unlink()
            removed = True
    memory_path = session_memory_dir(session_id)
    if memory_path.is_dir():
        shutil.rmtree(memory_path)
        removed = True
    return {"success": removed, "id": session_id} if removed else {"error": "세션을 찾을 수 없습니다."}


def clone_session(session_id: str) -> dict:
    session_id = validate_session_id(session_id)
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


def activate_session(session_id: str, confirm_workspace_switch: bool = False) -> dict:
    session_id = validate_session_id(session_id)
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

    access = "write" if activated.get("mode") == "coding" else activated.get("workspace_access", "read")
    saved_workspace = str(activated.get("workspace_path") or "").strip()
    current_configured = read_configured_project_path()

    if not saved_workspace:
        # 구버전 세션 — 어떤 폴더에서 돌았는지 기록이 없다. 현재 선택 폴더를 쓰되
        # 그 사실을 사용자에게 남긴다.
        resolution = resolve_project_workspace(require_write=access == "write")
        if resolution.blocked:
            return {"error": workspace_block_message(resolution)}
        activated["workspace_path"] = str(resolution.path)
        add_runtime_event(
            "이 세션에는 작업 폴더 기록이 없어 현재 선택된 폴더를 사용합니다: "
            f"{resolution.path}",
            "warn",
        )
    else:
        session_resolution = resolve_workspace(saved_workspace, require_write=access == "write")
        if session_resolution.status == WorkspaceStatus.NOT_FOUND:
            # 세션 폴더가 옮겨졌거나 지워졌다. 현재 선택 폴더가 유효하면 사용자 확인 후
            # 그 폴더에서 이어간다 — 확인 없이 다른 폴더로 바꾸지는 않는다.
            current_resolution = resolve_project_workspace(require_write=access == "write")
            if current_resolution.blocked:
                return {"error": workspace_block_message(current_resolution)}
            if not confirm_workspace_switch:
                return {
                    "error": (
                        "이 세션의 작업 폴더를 찾을 수 없습니다 (옮겨졌거나 삭제됨).\n"
                        f"세션 폴더: {saved_workspace}\n"
                        f"현재 폴더: {current_resolution.path}\n"
                        "현재 폴더에서 이어서 진행하려면 다시 확인해주세요."
                    ),
                    "needs_workspace_confirm": True,
                    "session_workspace": saved_workspace,
                    "current_workspace": str(current_resolution.path),
                }
            activated["workspace_path"] = str(current_resolution.path)
            add_runtime_event(
                f"세션 폴더({saved_workspace})를 찾을 수 없어 현재 폴더에서 이어갑니다: "
                f"{current_resolution.path}",
                "warn",
            )
        elif session_resolution.blocked:
            return {"error": workspace_block_message(session_resolution)}
        elif not workspace_matches(saved_workspace, current_configured) and not confirm_workspace_switch:
            return {
                "error": (
                    "이 세션은 다른 작업 폴더에서 진행됐습니다.\n"
                    f"세션 폴더: {session_resolution.path}\n"
                    f"현재 폴더: {current_configured or '(설정 없음)'}\n"
                    "세션 폴더로 전환해 계속하려면 다시 확인해주세요."
                ),
                "needs_workspace_confirm": True,
                "session_workspace": str(session_resolution.path),
                "current_workspace": current_configured,
            }
        else:
            save_project_path(session_resolution.path)
            activated["workspace_path"] = str(session_resolution.path)
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
    if state.get("mode") == "coding" and state.get("coding_stage") == "iteration":
        steps = goal_coding_steps(enabled)
        preview_index = int(state.get("coding_cycle_step", 0))
    else:
        preview_index = step_index
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


def load_checkpoint_owner(checkpoint_path: Path) -> dict | None:
    """체크포인트 옆에 저장된 소유권 정보를 읽는다. 없으면 None (레거시)."""
    meta_path = checkpoint_meta_path(checkpoint_path)
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def verify_checkpoint_ownership(
    checkpoint_path: Path, session_id: str, workspace: Path
) -> str:
    """이 체크포인트를 지금 세션·지금 작업공간에 적용해도 되는지 확인한다.

    빈 문자열이면 통과, 아니면 거부 사유. "MEMORY_DIR 아래에 있다"는 것만으로는
    허용하지 않는다 — 다른 세션이 다른 프로젝트에서 만든 패치일 수 있다.
    """
    owner = load_checkpoint_owner(checkpoint_path)
    if owner is None:
        return (
            "소유권 정보가 없는 레거시 체크포인트입니다. "
            "어느 세션·어느 프로젝트에서 만들어졌는지 확인할 수 없어 자동으로 적용하지 않습니다."
        )
    owner_session = str(owner.get("session_id", "")).strip()
    if not owner_session or owner_session != session_id:
        return (
            "다른 세션에서 만든 체크포인트입니다 "
            f"(체크포인트 세션: {owner_session or '알 수 없음'} / 현재 세션: {session_id})."
        )
    owner_workspace = str(owner.get("workspace_path", "")).strip()
    if not owner_workspace:
        return "체크포인트에 작업공간 정보가 없어 적용하지 않습니다."
    if not workspace_matches(owner_workspace, workspace):
        return (
            "다른 작업공간에서 만든 체크포인트입니다 "
            f"(체크포인트: {owner_workspace} / 현재: {workspace})."
        )
    return ""


def rollback_checkpoint_files(checkpoint: str, paths: list[str]) -> dict:
    try:
        checkpoint_path = Path(checkpoint).resolve()
        memory_root = MEMORY_DIR.resolve()
        if not checkpoint_path.is_relative_to(memory_root) or not checkpoint_path.is_file():
            return {"error": "유효한 체크포인트가 아닙니다."}
    except (OSError, ValueError):
        return {"error": "체크포인트 경로를 확인할 수 없습니다."}
    workspace_resolution = resolve_project_workspace(require_write=True)
    if workspace_resolution.blocked or workspace_resolution.path is None:
        return {"error": workspace_block_message(workspace_resolution)}
    workspace_root = workspace_resolution.path

    clean_paths = []
    for path in paths:
        if not path:
            continue
        candidate = Path(path)
        # 체크포인트 내부 파일 경로의 ..·절대경로·심볼릭 링크 우회를 모두 막는다.
        if candidate.is_absolute() or ".." in candidate.parts or candidate.drive:
            continue
        try:
            resolved = (workspace_root / candidate).resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(workspace_root):
            continue
        clean_paths.append(path)
    clean_paths = clean_paths[:50]
    if not clean_paths:
        return {"error": "되돌릴 파일을 선택해주세요."}

    with STATE_LOCK:
        current_session_id = str(STATE.get("id", ""))
    ownership_error = verify_checkpoint_ownership(
        checkpoint_path, current_session_id, workspace_root
    )
    if ownership_error:
        return {"error": ownership_error, "ownership_blocked": True}

    # 패치에 없는 파일을 고르면 git apply는 아무것도 안 하고 성공(0)을 돌려준다.
    in_patch = checkpoint_patch_paths(checkpoint_path.read_text(encoding="utf-8", errors="replace"))
    not_restorable = [path for path in clean_paths if path not in in_patch]
    if not_restorable:
        return {"error": (
            "이 체크포인트에는 다음 파일의 변경 전 내용이 없어 되돌릴 수 없습니다 "
            f"(크기·용량 한도 초과 또는 텍스트가 아닌 파일): {', '.join(not_restorable[:6])}"
        )}

    git = shutil.which("git") or "git"
    # core.autocrlf가 켜져 있으면 git apply가 LF 파일을 CRLF로 바꿔 써서 원래대로 돌아가지 않는다.
    command = [git, "-c", "core.autocrlf=false", "apply", "--reverse", "--whitespace=nowarn"]
    for path in clean_paths:
        command.append(f"--include={path}")
    command.append(str(checkpoint_path))
    result = subprocess.run(command, cwd=workspace_root, capture_output=True)
    if result.returncode != 0:
        detail = decode_cli_output(result.stderr or result.stdout).strip()
        return {"error": f"변경을 되돌리지 못했습니다: {detail[:500]}"}
    add_runtime_event(f"체크포인트에서 {len(clean_paths)}개 파일 되돌림: {', '.join(clean_paths[:6])}")
    return {"success": True, "paths": clean_paths}


def save_profile_content(content: str, session_id: str | None = None) -> dict:
    if session_id is not None:
        session_id = validate_session_id(session_id)
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
    if mode == "continuous":
        mode = "coding"
    if mode not in MODE_LABELS:
        return {"error": "알 수 없는 모드입니다."}
    if mode == "coding" and resolve_project_workspace(require_write=True).blocked:
        return {"error": "코딩 모드는 작업 폴더의 쓰기 권한이 필요합니다."}
    with STATE_LOCK:
        if waiting_for_user_response(STATE):
            return {"error": "사용자 질문에 답한 뒤 모드를 전환해주세요."}
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
        if mode == "quick":
            # 빠른 질문에서는 쓰기 권한을 끈다.
            STATE["workspace_access"] = "read"
            if normalize_discussion_project_access(
                STATE.get("discussion_project_access")
            ) == "write":
                STATE["discussion_project_access"] = "read"
        if mode == "coding":
            STATE["workspace_access"] = "write"
            STATE["discussion_project_access"] = "write"
            if previous_mode != "coding":
                STATE["coding_stage"] = "planning"
                STATE["plan_approved"] = False
                STATE["coding_cycle"] = 0
                STATE["coding_cycle_step"] = 0
                STATE["coding_started_at"] = None  # 목표 사이클 시작 때 정한다
                STATE["coding_progress"] = normalize_coding_progress(None)
                STATE["coding_stopped"] = False
                STATE["coding_stop_reason"] = ""
        step_count = len(steps_for_mode(mode, enabled))
        STATE["finished"] = (
            mode == "discussion"
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
    #: 서버가 실제로 바인딩한 포트. start_server()가 채운다. Origin/Host 검증의 기준.
    server_port_number = PORT

    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass  # 콘솔에 HTTP 접근 로그를 찍지 않음

    # ── 응답 헬퍼 ────────────────────────────────────────────
    def _write_body(self, body: bytes, status: int, content_type: str, *, no_store: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            # 상태 API는 절대 캐시되면 안 된다 (뒤로 가기 시 옛 상태가 보임).
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_html(self, body: str, status: int = 200) -> None:
        self._write_body(body.encode("utf-8"), status, "text/html; charset=utf-8", no_store=True)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._write_body(body, status, "application/json; charset=utf-8", no_store=True)

    def _send_error_json(self, status: int, message: str) -> None:
        """오류를 구조화해서 돌려준다. 내부 경로·스택은 절대 넣지 않는다."""
        self._send_json({"error": message, "status": status}, status=status)

    def _send_not_found(self) -> None:
        self._send_error_json(404, "요청한 경로를 찾을 수 없습니다.")

    # ── 요청 검증 ────────────────────────────────────────────
    def _read_body_bytes(self) -> bytes:
        length = webguard.parse_content_length(self.headers.get("Content-Length"))
        if not length:
            return b""
        body = self.rfile.read(length)
        if len(body) < length:
            raise webguard.RequestRejected(400, "요청 본문이 예고된 길이보다 짧습니다.")
        return body

    #: 요청 본문은 소켓에서 한 번만 읽을 수 있다. 기존 분기들이 _read_form()을
    #: 여러 번 부르므로 요청 단위로 캐시한다.
    _form_cache: dict | None = None

    def _read_form(self) -> dict:
        if self._form_cache is not None:
            return self._form_cache
        body = self._read_body_bytes()
        if not body:
            self._form_cache = {}
            return self._form_cache
        try:
            self._form_cache = parse_qs(body.decode("utf-8"))
        except UnicodeDecodeError:
            raise webguard.RequestRejected(400, "요청 본문을 UTF-8로 해석할 수 없습니다.") from None
        return self._form_cache

    def _guard_state_change(self, form: dict | None = None) -> None:
        """상태 변경 요청을 검증한다. 실패하면 RequestRejected."""
        token = self.headers.get(webguard.CSRF_HEADER)
        if not token and form is not None:
            token = (form.get(webguard.CSRF_FORM_FIELD) or [""])[0]
        webguard.check_state_changing_request(
            origin=self.headers.get("Origin"),
            host=self.headers.get("Host"),
            token=token,
            expected_token=CSRF_TOKEN,
            port=self.server_port_number,
        )

    def _guard_get(self) -> None:
        """GET도 Host는 확인한다 (DNS rebinding으로 로컬 데이터를 읽는 걸 막는다)."""
        if not webguard.is_allowed_host(self.headers.get("Host"), self.server_port_number):
            raise webguard.RequestRejected(403, "허용되지 않은 Host 헤더입니다.")

    def do_GET(self) -> None:
        try:
            self._guard_get()
            self._handle_get()
        except webguard.RequestRejected as rejected:
            self._send_error_json(rejected.status, rejected.message)
        except (UnsafePathError, ValueError) as exc:
            self._send_error_json(400, str(exc))
        except BrokenPipeError:
            pass
        except Exception:
            # 내부 경로·스택을 브라우저에 그대로 노출하지 않는다. 서버 콘솔에만 남긴다.
            traceback.print_exc()
            self._send_error_json(500, "서버 내부 오류가 발생했습니다. 콘솔 로그를 확인해주세요.")

    def do_HEAD(self) -> None:
        self.do_GET()

    def _handle_get(self) -> None:
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
                    self._write_body(content, 200, content_type, no_store=False)
                    return
                except OSError:
                    pass
            self._send_not_found()
            return

        if parsed.path == "/state.json":
            include_feed = parse_qs(parsed.query).get("feed", ["0"])[0] == "1"
            self._send_json(state_json_payload(include_feed=include_feed))
            return
        if parsed.path == "/messages-older.json":
            query = parse_qs(parsed.query)
            try:
                before = int(query.get("before", ["0"])[0])
            except ValueError:
                before = 0
            self._send_json(older_messages_payload(query.get("id", [""])[0], before))
            return
        if parsed.path == "/sessions.json":
            self._send_json(sessions_list_payload())
            return
        if parsed.path == "/session.json":
            session_id = parse_qs(parsed.query).get("id", [""])[0]
            try:
                payload = session_detail_payload(session_id)
            except UnsafeSessionIdError as exc:
                self._send_error_json(400, f"세션 ID를 사용할 수 없습니다: {exc}")
                return
            if payload is None:
                self._send_not_found()
                return
            self._send_json(payload)
            return
        if parsed.path == "/profile.json":
            session_id = parse_qs(parsed.query).get("id", [""])[0] or None
            try:
                payload = profile_payload(session_id)
            except UnsafeSessionIdError as exc:
                self._send_error_json(400, f"세션 ID를 사용할 수 없습니다: {exc}")
                return
            if payload is None:
                self._send_not_found()
                return
            self._send_json(payload)
            return
        if parsed.path == "/messages.json":
            # 대용량 세션용 증분 조회 — active_tick 때문에 1,500개 메시지를
            # 매번 다시 보내지 않도록 상태 조회와 분리했다.
            query = parse_qs(parsed.query)
            self._send_json(messages_payload(
                session_id=query.get("id", [""])[0],
                after=query.get("after", ["-1"])[0],
                limit=query.get("limit", ["80"])[0],
            ))
            return
        if parsed.path == "/plan.json":
            query = parse_qs(parsed.query)
            with STATE_LOCK:
                current_enabled = normalize_enabled_agents(STATE.get("enabled_agents"))
                current_access = normalize_discussion_project_access(
                    STATE.get("discussion_project_access")
                )
                current_workspace_access = STATE.get("workspace_access", "read")
            self._send_json(mode_execution_plan(
                query.get("mode", ["discussion"])[0],
                query.get("agent") or current_enabled,
                query.get("access", [current_access])[0],
                current_workspace_access,
            ))
            return
        if parsed.path == "/prompt-preview.json":
            agent = parse_qs(parsed.query).get("agent", ["codex"])[0]
            self._send_json(prompt_preview_payload(agent))
            return
        if parsed.path in ("/", "/index.html"):
            self._send_html(render_dashboard())
            return
        # 존재하지 않는 API·정적 경로에 대시보드 HTML을 돌려주면, 오타난 fetch가
        # HTML을 JSON으로 파싱하려다 엉뚱한 곳에서 실패한다.
        self._send_not_found()

    def do_POST(self) -> None:
        self._form_cache = None
        try:
            self._handle_post()
        except webguard.RequestRejected as rejected:
            self._send_error_json(rejected.status, rejected.message)
        except UnsafeSessionIdError as exc:
            self._send_error_json(400, f"세션 ID를 사용할 수 없습니다: {exc}")
        except UnsafePathError as exc:
            self._send_error_json(400, str(exc))
        except WorkspaceUnavailable as exc:
            self._send_error_json(409, workspace_block_message(exc.resolution))
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self._send_error_json(500, "서버 내부 오류가 발생했습니다. 콘솔 로그를 확인해주세요.")

    def _handle_post(self) -> None:
        # 모든 POST는 상태를 바꾼다. 본문을 읽기 전에 Host/Origin부터 확인하고,
        # 폼 본문에 담겨 오는 토큰도 받을 수 있도록 폼을 먼저 읽는다.
        form = self._read_form()
        self._guard_state_change(form)

        if self.path == "/workspace/select":
            form = self._read_form()
            access = form.get("access", ["read"])[0]
            selected = choose_workspace_folder(project_path_or_none() or ROOT)
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
            self._send_json(update_workspace_selection(read_configured_project_path(), access))
            return

        if self.path == "/topic":
            form = self._read_form()
            topic = form.get("topic", [""])[0].strip()
            mode = form.get("mode", ["discussion"])[0].strip()
            if mode == "continuous":
                mode = "coding"
            discussion_project_access = normalize_discussion_project_access(
                form.get("discussion_project_access", ["read"])[0].strip()
            )
            requested_start_agent = form.get("start_agent", [""])[0].strip()
            enabled_agents = prioritize_start_agent(form.get("agent", []), requested_start_agent)
            agent_settings = normalize_agent_settings({
                agent: {
                    "model": form.get(f"model_{agent}", [""])[0].strip(),
                    "effort": form.get(f"effort_{agent}", [""])[0].strip(),
                }
                for agent in AGENT_ORDER
            })
            if mode not in MODE_LABELS:
                mode = "discussion"
            if mode == "quick":
                # 빠른 질문은 모델 1명, 읽기 전용.
                enabled_agents = enabled_agents[:1]
                if discussion_project_access == "write":
                    discussion_project_access = "read"
            if not topic:
                self._send_error_json(400, "주제를 입력해주세요.")
                return
            if topic:
                with STATE_LOCK:
                    STATE["topic"] = topic
                    if not STATE.get("name") or STATE.get("name") == "새 세션":
                        STATE["name"] = re.sub(r"\s+", " ", topic).strip()[:80]
                    STATE["mode"] = mode
                    STATE["discussion_project_access"] = (
                        "write" if mode == "coding" else discussion_project_access
                    )
                    if mode == "coding" or discussion_project_access == "write":
                        STATE["workspace_access"] = "write"
                    elif mode == "quick":
                        STATE["workspace_access"] = "read"
                    STATE["enabled_agents"] = enabled_agents
                    STATE["start_agent"] = enabled_agents[0]
                    STATE["step_index"] = 0
                    STATE["coding_stage"] = "planning"
                    STATE["plan_approved"] = False
                    STATE["coding_cycle"] = 0
                    STATE["coding_cycle_step"] = 0
                    STATE["coding_started_at"] = None  # 목표 사이클 시작 때 정한다
                    STATE["coding_progress"] = normalize_coding_progress(None)
                    STATE["coding_stopped"] = False
                    STATE["coding_stop_reason"] = ""
                    STATE["workflow_status"] = workflow.ANALYZING
                    STATE["pending_user_question"] = None
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
            # deep=1이면 격리된 임시 폴더에서 실제 읽기(및 쓰기 권한 선택 시 쓰기)까지 검사
            deep = (form.get("deep", ["0"])[0] or "0") == "1"
            run_connection_checks(deep=deep)
            payload = state_json_payload()
            payload["success"] = True
            self._send_json(payload)
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
            self._send_json(activate_session(
                form.get("id", [""])[0].strip(),
                form.get("confirm_workspace", ["false"])[0].lower() == "true",
            ))
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
            if intent == "redirect" and source == "composer" and current_mode == "coding":
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
            with STATE_LOCK:
                waiting_for_answer = waiting_for_user_response(STATE)
            if waiting_for_answer:
                self._send_json({"error": "대기 중인 질문에 먼저 답해주세요."})
                return
            CONTROL["paused"] = False
            CONTROL["user_cancelled"] = False
            start_worker_if_needed(force=True)
            self._send_json(state_json_payload())
            return

        if self.path == "/stop":
            # 프로세스를 죽이기 전에 표시부터 켠다. 순서가 반대면 워커가 "종료 코드 1"을
            # 먼저 보고 중단을 실패로 기록한다.
            CONTROL["user_cancelled"] = True
            CONTROL["stopped"] = True
            cancelled = cancel_active_cli_processes()
            CONTROL["paused"] = False
            CONTROL["awaiting_approval"] = False
            CONTROL["approval_deferred"] = False
            CONTROL["approval_requested"] = False
            CONTROL["approval_requested_by"] = []
            CONTROL["intervention_pending"] = False
            CONTROL["intervention_intent"] = ""
            CONTROL["intervention_queue"] = []
            with STATE_LOCK:
                if STATE.get("mode") == "coding":
                    STATE["coding_stopped"] = True
                    STATE["coding_stop_reason"] = "사용자 중단"
                STATE["workflow_status"] = workflow.CANCELLED
                STATE["run_status"] = runstatus.RunStatus.USER_CANCELLED
                # 중단 시 실행 중 표시를 즉시 지운다 — 남아 있으면 세션 목록이
                # 계속 "진행 중"으로 보인다.
                STATE["active_agent"] = None
                STATE["active_phase"] = None
                STATE["active_started_at"] = None
                persist_intervention_queue_locked()
                save_state(STATE)
            if cancelled:
                add_runtime_event(f"실행 중 CLI 강제 종료: {', '.join(cancelled)}", "warn")
            add_runtime_event("사용자가 실행을 중단했습니다.", "warn")
            self._send_json(state_json_payload())
            return

        if self.path == "/retry":
            if budget_exceeded_reason(STATE):
                self._send_json({"error": "예산을 먼저 늘린 뒤 재시도해주세요."})
                return
            CONTROL["paused"] = False
            CONTROL["stopped"] = False
            CONTROL["user_cancelled"] = False
            with STATE_LOCK:
                STATE["coding_stopped"] = False
                STATE["coding_stop_reason"] = ""
                STATE["run_status"] = runstatus.RunStatus.RUNNING
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
            with STATE_LOCK:
                waiting_for_answer = waiting_for_user_response(STATE)
            if waiting_for_answer:
                self._send_json({"error": "승인 요청이 아니라 사용자 답변이 필요한 상태입니다."})
                return
            approve_pending_work()
            self._send_json(state_json_payload())
            return

        if self.path == "/answer-question":
            form = self._read_form()
            option_id = form.get("option_id", [""])[0]
            answer = form.get("answer", [""])[0]
            self._send_json(answer_pending_user_question(option_id, answer))
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
                STATE["workflow_status"] = workflow.CANCELLED
                STATE["plan_approved"] = False
                persist_intervention_queue_locked()
                save_state(STATE)
            self._send_json(state_json_payload())
            return

        if self.path == "/defer":
            if CONTROL["awaiting_approval"]:
                CONTROL["approval_deferred"] = True
            self._send_json(state_json_payload())
            return

        self._send_not_found()


class RoundtableServer(ThreadingHTTPServer):
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
            # 8765~8774 중 어느 포트로 열리든 Origin/Host 검증이 그 포트를 기준으로
            # 동작해야 한다.
            RoundtableHandler.server_port_number = candidate
            return server, candidate
        except OSError:
            continue
    print("  ❌ 로컬 서버 포트를 열 수 없습니다 (8765~8774 모두 사용 중). "
          "이전에 열어둔 roundtable.py 창이 남아있는지 확인하세요.")
    sys.exit(1)


# ──────────────────────────────────────────
# 메인
# ──────────────────────────────────────────

def shutdown_cleanly(server) -> None:
    """종료 시 실행 중인 CLI 자식 프로세스를 죽이고 상태를 마지막으로 저장한다.

    예전에는 Ctrl+C가 서버만 내리고 자식 CLI를 그대로 남겨서, 다음 실행 때
    좀비 프로세스가 포트와 파일을 붙잡고 있었다.
    """
    CONTROL["stopped"] = True
    CONTROL["user_cancelled"] = True
    cancelled = cancel_active_cli_processes()
    if cancelled:
        print(f"   실행 중이던 CLI 종료: {', '.join(cancelled)}")
    try:
        with STATE_LOCK:
            # 진행 중 표시를 남긴 채 죽으면 다음 실행에서 "진행 중"으로 복원된다.
            if STATE.get("active_agent"):
                STATE["run_status"] = runstatus.RunStatus.USER_CANCELLED
            STATE["active_agent"] = None
            STATE["active_phase"] = None
            STATE["active_started_at"] = None
            STATE["active_cli_mode"] = None
            STATE["active_work_log"] = []
            save_state(STATE)
    except Exception as exc:  # noqa: BLE001 - 종료 경로에서 예외로 죽지 않는다
        print(f"   ⚠️  종료 중 상태 저장 실패: {exc}")
    try:
        server.shutdown()
    except Exception:  # noqa: BLE001
        pass
    print("   진행 상황을 저장했습니다.")


def main() -> None:
    restore_saved_state()
    print("\U0001f680 TriAgent Control — Codex, Antigravity & Claude Code")
    print(f"   Codex: {command_label(CODEX_CMD)}")
    print(f"   Antigravity: {command_label(AGY_CMD)}")
    print(f"   Claude Code: {command_label(CLAUDE_CMD)}")
    ensure_team_prompt()
    print(f"   공통 지침: {TEAM_PROMPT_PATH}")
    ensure_project_path_file()
    startup_workspace = resolve_project_workspace()
    if startup_workspace.ok:
        print(f"   코딩 대상 폴더: {startup_workspace.path}  (바꾸려면 {PROJECT_PATH_FILE.name} 수정)")
    else:
        print(f"   ⚠️  코딩 대상 폴더 차단됨: {startup_workspace.message}")
        print(f"      대시보드에서 폴더를 다시 선택하거나 {PROJECT_PATH_FILE.name}을 고쳐주세요.")
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
        print("\n\n\U0001f6d1 Ctrl+C 감지 — 서버를 종료합니다.")
    finally:
        shutdown_cleanly(server)

    separator("종료")
    print(f"대화 상태: {STATE_PATH}")
    print(f"대화 로그: {LOG_PATH}")
    print(f"프로필: {PROFILE_PATHS['codex']}, {PROFILE_PATHS['antigravity']}, {PROFILE_PATHS['claude']}")


if __name__ == "__main__":
    main()
