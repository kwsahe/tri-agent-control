# TriAgent Control

Codex, Antigravity, Claude Code를 하나의 웹 대시보드에서 실행하고 관제하는 로컬 멀티 에이전트 오케스트레이터입니다. 세 모델이 같은 대화 문맥을 공유하며 토론, 역할 분담, 실제 코딩, 검증과 최종 보고를 수행합니다. 사용자는 실행 중에도 대상 모델을 선택해 질문하거나 방향을 바꾸고, 승인·보류·중단을 직접 결정할 수 있습니다.

## 주요 기능

### 세 모델 통합 관제

- Codex, Antigravity, Claude Code 연결 상태와 CLI 버전을 확인합니다.
- 세 모델 중 필요한 모델만 활성화해 세션을 시작할 수 있습니다.
- 모델별 세부 모델과 추론 강도 또는 프리셋을 선택할 수 있습니다.
- 비활성 모델 정보도 나머지 모델의 프롬프트에 전달됩니다.
- 각 모델의 공식 아이콘, 현재 상태, 선택 모델을 대시보드에 표시합니다.

### 토론과 코딩

- **토론 모드**: 프로젝트 분석, 강점 공유, 반론, 역할 분담과 최종 정리를 수행합니다.
- **코딩 모드**: `PROJECT_PATH.txt`가 가리키는 실제 프로젝트를 읽고 수정합니다.
- 실행 중에도 토론 모드와 코딩 모드를 전환할 수 있습니다.
- 코딩 지시는 계획 보고로 끝내지 않고 파일 수정과 가능한 검증까지 수행하도록 별도 실행 의도로 전달됩니다.
- 코딩 턴 전후의 파일 변경을 감지하고 unified diff 체크포인트를 저장합니다.
- 체크포인트에서 선택한 파일만 해당 턴 이전 상태로 되돌릴 수 있습니다.

### 사용자 개입과 승인

- 질문, 방향 수정, 코딩 실행, 멈추고 답변, 참고 메모 중 개입 유형을 선택할 수 있습니다.
- Codex, Antigravity, Claude Code 중 답변할 모델만 지정할 수 있습니다.
- 개입 요청은 정규 진행 단계보다 먼저 처리됩니다.
- 모델이 응답 마지막에 `APPROVE`를 출력하면 토큰은 채팅에서 숨겨지고 승인 창이 열립니다.
- 승인, 거절, 보류를 선택할 수 있으며 보류 상태에서 추가 질문이나 수정 지시를 보낼 수 있습니다.
- 중단 시 실행 중인 CLI 하위 프로세스도 함께 종료합니다.
- 실패한 턴과 보류된 개입 요청은 재시도할 수 있습니다.

### 에이전트 간 호출

모델이 자신의 담당 범위를 벗어난 검토나 후속 작업이 필요하다고 판단하면 다른 모델을 직접 호출할 수 있습니다.

```text
CALL_AGENT: codex|antigravity|claude | discussion|coding | 구체적인 요청
```

- 호출은 공유 개입 큐에 등록되고 대상 모델이 이어서 처리합니다.
- 자기 자신, 비활성 모델, 중복 호출은 무시합니다.
- 토론 턴에서 코딩 권한을 임의로 위임할 수 없습니다.
- 무한 왕복을 막기 위해 호출 깊이는 2단계, 세션당 호출은 12회로 제한합니다.
- 호출자, 대상, 모드와 요청 내용은 `Agent Calls` 패널에 기록됩니다.

### 실시간 작업 로그

- 모델 실행 중 `Thinking` 카드와 경과 시간을 표시합니다.
- CLI의 추론, 도구 호출, 명령 실행, 파일 읽기·수정, 사용량 이벤트를 실시간으로 보여줍니다.
- 완료된 답변에서 해당 턴의 작업 로그와 변경 파일을 펼쳐볼 수 있습니다.
- 최근 어떤 모델의 대화가 프롬프트에 전달됐는지 표시합니다.
- Python/Node 프로젝트의 테스트·린트·빌드 명령을 감지해 자동 실행하고 `Validation` 패널에 결과를 남깁니다.

### 토큰 사용량 관제

- 전체 세션의 실제 토큰, 경과 시간과 비용을 표시합니다.
- 모델별 세션 누적 점유율의 합계를 100%로 계산해 진행 막대로 보여줍니다.
- 각 모델은 최신 턴의 컨텍스트 사용량을 모델별 한도로 나눈 현재 컨텍스트 사용률도 별도로 표시합니다.
- `모델별 토큰 추정량` 버튼을 누르면 모델별 상세 카드를 열 수 있습니다.
- 상세 카드에는 점유율, 턴 수, 추정 토큰, 입력, 캐시, 출력과 비용이 표시됩니다.
- CLI가 실제 사용량을 제공하면 실제 값을 사용하고, 제공하지 않는 모델은 `~`가 붙은 문자 기반 추정값을 사용합니다.
- 상세 창을 열어둔 상태에서도 폴링 데이터에 맞춰 값이 갱신됩니다.
- 자동 토큰·비용 제한은 기본적으로 비활성화되어 있습니다.

### 세션과 메모리

- 세션 이름 수정, 검색, 태그, 즐겨찾기, 보관, 삭제와 분기를 지원합니다.
- 분기한 세션은 기존 대화와 설정을 유지한 채 별도 세션으로 이어집니다.
- 모든 대화는 세션별 Markdown 원문과 압축 메모리에 저장됩니다.
- 세션마다 `Profile.md`와 모델별 역할 프로필을 생성합니다.
- 대시보드에서 `Profile.md`를 열고 수정해 저장할 수 있습니다.
- 전체 대화를 매번 프롬프트에 넣지 않고 최근 대화와 압축 요약만 전달해 입력 크기를 제한합니다.
- 최종 프롬프트, 공유 문맥과 예상 토큰을 실행 전에 미리 볼 수 있습니다.

## 요구 사항

- Python 3.10 이상
- 별도 Python 패키지 없이 표준 라이브러리만으로 실행 가능
- 사용할 모델의 CLI 설치와 로그인

| 모델 | 기본 명령 | 확인 명령 |
|---|---|---|
| [Codex](https://github.com/openai/codex) | `codex` | `codex --version` |
| [Antigravity](https://antigravity.google/) | `agy` | `agy --version` |
| [Claude Code](https://claude.com/claude-code) | `claude` | `claude --version` |

모든 CLI를 설치할 필요는 없습니다. 세션 시작 시 연결된 모델만 선택할 수 있습니다.

## 시작하기

1. 코딩 대상 프로젝트 경로를 `PROJECT_PATH.txt` 한 줄에 입력합니다.
2. 서버를 실행합니다.

```powershell
python roundtable.py
```

3. 자동으로 열린 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)에서 주제, 모드, 참여 모델과 세부 모델을 선택합니다.
4. **시작**을 누르고 대화와 작업 로그를 확인합니다.

기본 포트가 사용 중이면 다음 포트를 순서대로 시도합니다. 실제 URL은 터미널에 출력됩니다.

## 기본 실행 흐름

### 토론 모드

1. 활성 모델이 순서대로 프로젝트와 자신의 강점을 분석합니다.
2. 서로의 분석을 확인하고 반론 또는 보완 의견을 냅니다.
3. 담당 영역과 파일 범위를 합의해 역할을 선언합니다.
4. 역할을 세션 `Profile.md`에 저장합니다.
5. 보고 담당 모델이 논의 결과와 다음 행동을 정리합니다.

### 코딩 모드

토론과 역할 합의 후 각 모델이 담당 범위의 실제 파일을 수정합니다. 파일 변경, 작업 로그와 체크포인트를 기록하고 자동 검증을 실행한 뒤 최종 결과를 보고합니다.

> 코딩 모드는 실제 프로젝트 파일을 변경합니다. 실행 전에 대상 프로젝트를 커밋하거나 별도 브랜치에서 시작하는 것을 권장합니다.

## 프롬프트와 메모리 절약

각 CLI 호출은 독립 실행이므로 모델이 이전 턴을 영구 학습한 상태로 유지되지는 않습니다. TriAgent Control은 다음 정보만 압축해 전달합니다.

- `TEAM_PROMPT.md` 최대 1,400자
- `brief.md` 최근 6줄
- 활성 모델별 최신 발언과 최근 사용자 개입
- 최근 대화 최대 2개, 1,600자
- 메모리 문맥 최대 1,800자
- 전체 프롬프트 기본 최대 5,000자
- 저장·표시하는 모델 답변 기본 최대 2,000자

`full.md`와 `Profile.md` 전문은 매번 넣지 않고 경로만 알려줍니다. 과거 근거가 필요한 코딩 작업에서만 모델이 직접 읽습니다.

## 테스트

```powershell
python -m py_compile roundtable.py
node --check static/dashboard.js
python -m unittest discover -s tests -v
```

현재 회귀 테스트는 상태 정규화, 모델 설정 전달, CLI 스트림 파싱, 개입과 승인, 모드 전환, 토큰 집계, 예산 차단 복구, 에이전트 호출, 작업 실패와 재시도를 검증합니다.

실제 CLI 읽기·쓰기 확인은 별도 스모크 테스트로 실행할 수 있습니다.

```powershell
$env:PYTHONPATH='.'
python tests\live_smoke.py
```

스모크 테스트는 `.roundtable-smoke` 폴더를 사용하고 생성물을 정리합니다.

## 설정 파일

| 파일 | 역할 |
|---|---|
| `TEAM_PROMPT.md` | 세 모델이 공유하는 공통 규칙과 우선순위 |
| `PROJECT_PATH.txt` | 코딩 모드가 실제로 작업할 프로젝트 경로 |
| `dashboard_template.html` | 대시보드 HTML 구조 |
| `static/dashboard.css` | 대시보드 스타일과 반응형 레이아웃 |
| `static/dashboard.js` | 폴링, 제어, 세션과 상세 UI 동작 |
| `static/agents/` | 모델과 사용자 아이콘 |

## 생성 데이터

| 경로 | 내용 |
|---|---|
| `roundtable_state.json` | 현재 세션 상태와 재개 정보 |
| `roundtable.html` | 마지막으로 렌더링한 정적 화면 스냅샷 |
| `roundtable_log.md` | 전체 세션의 append-only 대화 로그 |
| `sessions/<세션ID>.json` | 세션 상태 저장본 |
| `sessions/<세션ID>.md` | 세션별 대화 기록 |
| `roundtable_memory/<세션ID>/full.md` | 전체 대화 원문 |
| `roundtable_memory/<세션ID>/brief.md` | 프롬프트용 압축 요약 |
| `roundtable_memory/<세션ID>/Profile.md` | 세션 역할 분담 프로필 |
| `roundtable_memory/<세션ID>/profiles/*_Profile.md` | 모델별 역할 프로필 |
| `roundtable_memory/<세션ID>/checkpoints/` | 코딩 턴별 diff 체크포인트 |
| `CODEX_Profile.md`, `ANTIGRAVITY_Profile.md`, `CLAUDE_Profile.md` | 모델별 누적 역할 이력 |

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---:|---|
| `CODEX_CMD` / `AGY_CMD` / `CLAUDE_CMD` | `codex` / `agy` / `claude` | CLI 실행 명령 |
| `CODEX_TIMEOUT_SECONDS` | 900 | Codex 응답 제한 시간 |
| `AGY_TIMEOUT_SECONDS` / `CLAUDE_TIMEOUT_SECONDS` | 600 | Antigravity와 Claude 응답 제한 시간 |
| `ROUNDTABLE_PORT` | 8765 | 로컬 대시보드 시작 포트 |
| `ROUNDTABLE_TRANSCRIPT_WINDOW` | 2 | 직접 전달하는 최근 메시지 수 |
| `ROUNDTABLE_MEMORY_BRIEF_LINES` | 6 | 압축 메모리 줄 수 |
| `ROUNDTABLE_TRANSCRIPT_MAX_CHARS` | 1600 | 최근 대화 최대 문자 수 |
| `ROUNDTABLE_MEMORY_CONTEXT_MAX_CHARS` | 1800 | 메모리 문맥 최대 문자 수 |
| `ROUNDTABLE_TEAM_PROMPT_MAX_CHARS` | 1400 | 공통 지침 최대 문자 수 |
| `ROUNDTABLE_PROMPT_MAX_CHARS` | 5000 | 최종 프롬프트 최대 문자 수 |
| `ROUNDTABLE_OUTPUT_MAX_CHARS` | 2000 | 저장할 모델 답변 최대 문자 수 |
| `ROUNDTABLE_SNAPSHOT_MAX_ENTRIES` | 20000 | 파일 변경 감지 최대 항목 수 |
| `CODEX_CONTEXT_TOKENS` | 258400 | Codex 컨텍스트 사용률 계산 기준 |
| `CLAUDE_CONTEXT_TOKENS` | 128000 | Claude 컨텍스트 사용률 계산 기준 |
| `AGY_CONTEXT_TOKENS` | 1048576 | Antigravity Gemini 계열 컨텍스트 사용률 계산 기준 |

## 프로젝트 구조

```text
roundtable.py            CLI 실행, 상태 관리, 작업 큐와 로컬 HTTP 서버
dashboard_template.html  대시보드 문서 구조
static/dashboard.css     대시보드 디자인과 반응형 레이아웃
static/dashboard.js      실시간 상태 갱신과 사용자 제어
static/agents/            모델 아이콘
tests/                    회귀 및 실제 CLI 스모크 테스트
TEAM_PROMPT.md            세 모델 공통 지침
PROJECT_PATH.txt          코딩 대상 프로젝트 경로
orchestrate.py            이전 2모델 협업 실험 스크립트
```

## 보안 범위

서버는 기본적으로 `127.0.0.1`에만 바인딩됩니다. 코딩 모드는 로컬 CLI 권한으로 대상 프로젝트를 수정하므로, 각 CLI의 권한 설정과 `PROJECT_PATH.txt` 경로를 실행 전에 확인하세요.
