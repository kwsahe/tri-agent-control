# 실사용 기록 — 회의실 예약 시스템

TriAgent Control로 실제 풀스택 프로젝트(`meeting-room-booking`: FastAPI + SQLite, React + Vite)를 기능 단위로 만들면서 생긴 문제와 수정을 적는다. 큰 구현은 TriAgent의 Claude Code(구현 담당)가 맡고, 사람(운영자)은 계획 승인·결과 확인·TriAgent 수정을 맡는다.

## 준비 (2026-11-01)

- 운영자가 만든 것: 폴더, git, `.venv`(fastapi·uvicorn·pytest·httpx), Vite React 템플릿 + Vitest, `/api/health` 하나와 테스트 하나, 최상위 `package.json`의 `test`(화면 Vitest + 서버 pytest)·`build` 스크립트.
- 최상위 스크립트를 둔 이유: TriAgent 자동 검증은 작업 폴더 최상위의 `package.json`과 `tests/`만 찾는다. `backend/`·`frontend/`로 나눈 프로젝트는 최상위 스크립트가 없으면 "자동 검증 없음"이 된다.

## 문제 기록

### 1. 세션 worktree에서 화면 테스트·빌드가 항상 실패 (수정함)

- **증상**: 모델을 부르기 전 점검에서, 세션 worktree의 `npm test`·`npm run build`가 `'vitest'은(는) 내부 또는 외부 명령... 아닙니다`로 실패.
- **원인**: worktree에는 Git이 추적하지 않는 의존성 폴더가 없어서 사용자 폴더의 것을 연결하는데, 최상위(`.venv`, `node_modules`)만 연결했다. 이 프로젝트의 `node_modules`는 `frontend/` 아래에 있다.
- **영향**: 모노레포 구성에서는 자동 검증이 매번 실패해, 파이프라인이 통과할 수 없었다.
- **수정**: `triagent/domain/gitops.py` — 한 단계 아래의 의존성 폴더(`frontend/node_modules`, `backend/.venv` 등)도 연결하고, 정리할 때도 그 연결만 끊는다. 회귀 테스트 추가.
- **확인**: 같은 점검을 다시 돌려 worktree에서 `npm test`(화면 1 + 서버 1)·`npm run build` 통과, 원본 `frontend/node_modules`와 사용자 브랜치·파일은 그대로.

### 2. 기능을 이어서 만들 때 이전 기능이 다음 세션에 없음 (운영 방식으로 해결)

- **증상(예상)**: 세션마다 사용자 폴더의 마지막 커밋에서 worktree를 만든다. 기능 1의 결과는 `triagent/<세션ID>` 브랜치에만 있으므로, 기능 2 세션은 기능 1 없이 시작한다.
- **운영 방식**: 기능 하나가 끝나면 운영자가 결과를 확인하고 세션 브랜치를 `main`에 병합(fast-forward)한 뒤 다음 기능을 시작한다.
- **개선 후보**: 대시보드에 "결과를 내 브랜치에 반영" 단추 (테스트 통과·충돌 없음일 때만).
