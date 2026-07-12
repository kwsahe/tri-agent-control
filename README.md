# 🗣️ Agent Roundtable — Codex · Antigravity · Claude Code

세 개의 AI 코딩 CLI(**Codex**, **Antigravity**, **Claude Code**)를 브라우저 대시보드 하나로
동시에 굴리는 로컬 오케스트레이터입니다. 셋은 서로의 발언을 모두 보고, 반박하고, 역할을
나눈 뒤, 실제로 프로젝트 코드를 함께 작성합니다. 사용자는 터미널이 아니라 **HTML
대시보드만 보면서** 진행 상황을 확인하고 통제합니다.

## 핵심 아이디어

- 셋은 사용자(팀장)가 고용한 AI 개발 직원이라는 공통 지침([`TEAM_PROMPT.md`](TEAM_PROMPT.md))을
  매 턴마다 읽고 답변한다.
- 모든 대화는 서로에게 공개되고, 동의하지 않으면 반박할 수 있다.
- 마지막엔 항상 한 명(Claude Code)이 논의/작업 결과를 정리해서 **최종 보고**를 사용자에게
  전달한다.
- **일반 토론 모드**(읽기 전용, 강점·역할 논의)와 **코딩 모드**(실제로 파일을 쓰는 모드) 두
  가지로 동작한다.
- 코딩 모드가 실제로 작업할 폴더는 [`PROJECT_PATH.txt`](PROJECT_PATH.txt) 한 줄로 지정한다 —
  이 도구 자체를 다른 프로젝트 폴더에 복사할 필요 없이, 경로만 바꾸면 그 프로젝트에서 동작한다.

## 요구 사항

세 CLI가 모두 로컬에 설치·로그인되어 있어야 합니다.

| CLI | 확인 명령 |
|---|---|
| [Codex](https://github.com/openai/codex) | `codex --version` |
| [Antigravity](https://antigravity.google/) (`agy`) | `agy --version` |
| [Claude Code](https://claude.com/claude-code) | `claude --version` |

Python 3.10+ (표준 라이브러리만 사용, 추가 패키지 설치 불필요).

## 시작하기

```bash
python roundtable.py
```

실행하면:

1. 터미널에 세 CLI 연결 확인 로그가 찍힙니다.
2. 브라우저가 자동으로 열립니다 (`http://127.0.0.1:8765/`).
3. 대시보드에서 주제를 입력하고 모드를 고른 뒤 **시작** 버튼을 누르면 바로 진행됩니다.

이후 모든 제어(일시정지/재개, 대화에 메시지 끼어들기, 중단, 새 세션, 연결 재확인)는
브라우저에서만 하면 됩니다.

## 두 가지 모드

### 일반 토론 모드
읽기 전용으로 동작합니다. 순서대로:
1. Codex → Antigravity → Claude Code가 각자 자신 있는 영역을 이야기 (서로 반박 가능)
2. 같은 순서로 역할을 선언 (백엔드 / 프론트엔드 / 기획·아이디어 정리 — 셋이 겹치지 않게)
3. Claude Code가 최종 보고로 결론을 정리

### 코딩 모드
위 토론 6단계에 이어서, 각자 선언한 역할에 맞는 작업을 **`PROJECT_PATH.txt`에 적힌
폴더에 실제로 반영**합니다 (Codex `workspace-write`, Claude Code `acceptEdits`,
Antigravity `accept-edits`). 마지막에 무엇이 어떻게 바뀌었는지 최종 보고가 올라옵니다.

> ⚠️ 코딩 모드는 실제로 파일을 만들고 수정합니다. 처음 써보는 프로젝트라면 먼저
> `git commit`을 해두거나 별도 브랜치를 파서 실행하는 걸 권장합니다. 되돌리기 어려운
> 대량 변경이 생길 수 있습니다.

## 커스터마이징 (전부 텍스트 파일 수정만으로 가능)

| 파일 | 역할 |
|---|---|
| `TEAM_PROMPT.md` | 세 에이전트가 매 턴 읽는 공통 지침. 규칙/말투/우선순위를 자유롭게 수정 |
| `PROJECT_PATH.txt` | 코딩 모드가 실제로 작업할 프로젝트 폴더 경로 (실행 중 변경해도 다음 턴부터 반영) |
| `dashboard_template.html` | 대시보드 화면 자체 (HTML/CSS/JS). 디자인만 바꾸고 싶으면 이 파일만 수정 |

## 생성되는 파일 (커밋 대상 아님)

| 파일 | 내용 |
|---|---|
| `roundtable_state.json` | 현재 세션 상태 (재개용) |
| `roundtable.html` | 마지막 화면 스냅샷 |
| `roundtable_log.md` | 모든 세션의 대화 기록 (append-only) |
| `CODEX_Profile.md` / `ANTIGRAVITY_Profile.md` / `CLAUDE_Profile.md` | 세션마다 쌓이는 각 에이전트의 강점/역할 이력 |

## 환경변수 (선택)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CLAUDE_CMD` / `CODEX_CMD` / `AGY_CMD` | `claude` / `codex` / `agy` | 각 CLI 실행 명령 |
| `CLAUDE_TIMEOUT_SECONDS` / `CODEX_TIMEOUT_SECONDS` / `AGY_TIMEOUT_SECONDS` | 600 / 900 / 600 | 각 CLI 응답 대기 시간 |
| `ROUNDTABLE_PORT` | 8765 | 대시보드 서버 포트 (사용 중이면 자동으로 다음 포트 시도) |

## 파일 구조

```
roundtable.py            메인 오케스트레이터 (연결 확인, 대화 루프, 로컬 HTTP 서버)
dashboard_template.html  대시보드 화면
TEAM_PROMPT.md           공통 지침
PROJECT_PATH.txt         코딩 대상 폴더 경로 (로컬 전용, git에는 커밋 안 됨)
orchestrate.py           (이전 실험용) TODO.md 기반 Codex↔Claude Code 2자 협업 루프
```
