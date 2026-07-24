# TriAgent Control 현재 구조

## 기준선

- 검증일: 2026-07-24
- 단위 테스트: `118 passed, 6 subtests passed`
- 정적 검사: Ruff, `py_compile`, `node --check` 통과
- 브라우저 검사: 데스크톱과 390x844에서 수평 오버플로 및 콘솔 오류 없음

## 현재 책임

| 영역 | 현재 담당 |
|---|---|
| CLI 프로세스 어댑터와 스트림 파싱 | `roundtable.py` |
| 워크플로 단계와 워커 루프 | `roundtable.py` |
| 워크스페이스 스냅샷, diff, 검증, 롤백 | `roundtable.py` |
| 세션 저장과 메모리 | `roundtable.py` |
| HTTP 라우트와 JSON 응답 | `roundtable.py` |
| 워크플로 상태와 구조화된 사용자 질문 | `triagent/domain/workflow.py` |
| 대시보드 구조 | `dashboard_template.html` |
| 대시보드 상태와 상호작용 | `static/dashboard.js` |
| 대시보드 표현 | `static/dashboard.css` |

가장 큰 기술 부채는 서로 다른 책임이 `roundtable.py`에 집중되어 있고 상태가
영속 `STATE`와 런타임 `CONTROL`로 분리되어 있다는 점이다.

## 수동 재현

1. `python roundtable.py`로 서버를 시작한다.
2. `http://127.0.0.1:8765`를 연다.
3. 토론을 시작하고 Agent가 유효한 `STOP_AND_ASK_USER` JSON 객체를 반환하게 한다.
4. 질문 영역이 나타나고 상태가 `WAITING_FOR_USER_RESPONSE`가 되는지 확인한다.
5. 재개와 승인 버튼으로 다른 Agent가 시작되지 않는지 확인한다.
6. 서버를 재시작해도 같은 질문이 유지되는지 확인한다.
7. 잘못된 선택값을 보내면 질문이 계속 차단 상태인지 확인한다.
8. 유효한 선택값을 보내면 답변이 채팅에 기록되고 저장된 다음 단계가 바뀌지
   않은 채 실행이 재개되는지 확인한다.

## 다음 분리 순서

1. CLI 어댑터
2. 워크스페이스 스냅샷과 diff
3. 역할과 권한 정책
4. 세션 저장소
5. 워크플로 상태 전이
6. HTTP 서버와 대시보드 API
