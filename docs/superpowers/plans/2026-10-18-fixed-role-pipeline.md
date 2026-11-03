# 고정 역할 코딩 파이프라인 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 코딩 모드를 "세 모델 토론 + 셋 다 구현" 구조에서 **Claude Code 구현 → Antigravity 테스트 → 자동 검증 → Codex 검토 → 사이클마다 Git 커밋** 고정 파이프라인으로 바꾼다.

**Architecture:** 워커(`worker_loop`)는 이미 단계 표 `(agent, phase, instruction, cli_mode)`를 순서대로 실행하는 구조라, 코딩 모드의 단계 표만 교체한다. 역할은 기존 `ROLE_CATALOG`의 `qa`(테스트 경로만 쓰기)와 `reviewer`(읽기 전용)에 대응시켜 쓰기 범위 강제를 재사용한다. 검토 판정은 기존 `APPROVE` 토큰과 같은 방식(마지막 줄 토큰, 잘리기 전 원문에서 추출)으로 읽고, Git 커밋은 새 모듈 `triagent/domain/gitops.py`가 맡는다.

**Tech Stack:** Python 3.10+ 표준 라이브러리만(`subprocess`, `difflib` 등), Git CLI, 바닐라 JS(`static/dashboard.js`), `unittest`.

**Spec:** 이 문서의 "Design" 절 (2026-10-17~18 세션 대화에서 합의한 내용을 옮긴 것)

## Design

- **토론 없음.** 코딩 모드는 강점 이야기·반론·역할 선언/확정·개선안 제안 단계를 거치지 않는다. 일반 토론 모드와 빠른 질문 모드는 이번 변경에서 건드리지 않는다.
- **고정 역할.** Claude Code = 구현(유일한 제품 코드 작성자), Codex = 검토(읽기 전용), Antigravity = 테스트(테스트 파일만 작성·실행). 역할 표(`PIPELINE_ROLES`) 한 곳에서 정의한다. 코딩 모드에서는 사용자가 역할을 바꿀 수 없다.
- **흐름.** 계획(Claude, 1회, 읽기 전용, 마지막 줄 `APPROVE`) → 사용자 승인(기존 `plan_approved` 게이트) → 사이클 반복: 구현(Claude) → 테스트(Antigravity) → 자동 검증 → 검토(Codex, 마지막 줄 `REVIEW: PASS` 또는 `REVIEW: CHANGES`) → 사이클 커밋.
- **종료.** 검토가 `PASS`이고 자동 검증이 모두 통과하면 세션을 완료한다. Codex가 꺼져 있으면 검증 통과만으로 완료한다. 판정 줄이 없으면 `CHANGES`로 본다(보수적). 기존 한도(최대 3사이클, 45분, 20만 토큰 등)는 그대로 적용된다.
- **필수 모델.** Claude Code가 꺼져 있으면 코딩 모드를 시작·전환하지 않는다. Antigravity·Codex는 끌 수 있고, 꺼진 역할의 단계는 빠진다.
- **Git.** 구현을 시작하기 직전(사이클 1 진입 시) 작업 폴더가 Git 저장소이고 커밋되지 않은 변경이 없으면 `triagent/<세션ID>` 브랜치로 전환한다. 사이클이 끝날 때마다 변경을 그 브랜치에 커밋한다. 저장소가 아니거나 변경이 남아 있으면 **커밋·브랜치 전환을 하지 않고** 이유를 런타임 로그에 남긴 채 파이프라인은 계속 진행한다. `main`/`master`에 직접 커밋하지 않는다. 푸시·병합은 사용자가 한다.
- **호출 수.** 계획 1회 + 사이클당 최대 3회(현재: 계획 약 13회 + 사이클당 7회).

## Global Constraints

- 실행 시작 조건: 진행 중인 두 수정 브랜치(백엔드 `Fix remaining orchestrator backend bugs`, 화면 `Fix remaining dashboard UI bugs`)가 병합된 뒤의 `main`에서 새 브랜치를 만들어 시작한다. 두 작업이 `roundtable.py`의 `worker_loop`/`run_step`과 `static/dashboard.js`를 고치므로 동시에 진행하면 충돌한다.
- 외부 패키지를 추가하지 않는다 (README: "별도 Python 패키지 없이 표준 라이브러리만으로 실행 가능").
- 테스트는 실제 모델 CLI(`codex`, `claude`, `agy`)를 절대 실행하지 않는다. `tests/live_smoke.py`는 사용자 허락 없이 실행하지 않는다(유료 호출).
- 모든 테스트 파일은 `import isolation`을 `import roundtable`보다 먼저 한다. 실제 `roundtable_state.json`, `sessions/`, `PROJECT_PATH.txt`를 읽거나 쓰지 않는다.
- UI 문구와 코드 주석은 한국어로, 기존 코드 스타일(설명 주석 밀도, 이름)을 따른다.
- 자동 커밋은 `triagent/<세션ID>` 브랜치에만 한다. `main`/`master`에 커밋하지 않는다. 사용자의 기존 미커밋 변경을 커밋에 섞지 않는다.
- 테스트 명령: `python -m unittest discover -s tests` (프로젝트 루트에서). 단일 파일: `$env:PYTHONPATH=".;tests"; python -m unittest test_pipeline -v`.

## Review Focus

1. **Git 저장소가 아닌 작업 폴더** — 파이프라인은 그대로 진행하고 커밋만 건너뛰며, 로그에 "Git 저장소가 아니라 자동 커밋을 하지 않습니다"가 남아야 한다. (Task 3, Task 4 테스트)
2. **시작 시 커밋되지 않은 사용자 변경이 있는 저장소** — 브랜치를 바꾸지 않고, 사용자 파일 내용과 현재 브랜치가 그대로여야 한다. (Task 3 테스트)
3. **검토 답변에 판정 줄이 없거나 마지막 줄이 아님** (중간에 `REVIEW: PASS`를 인용 등) — 통과로 보지 않고 다음 사이클로 간다. (Task 2, Task 4 테스트)
4. **Claude Code가 꺼진 상태로 코딩 모드 시작·전환** — 명확한 오류 문구로 거부한다. (Task 5 테스트)
5. **업그레이드 전에 진행 중이던 코딩 세션을 재개** — 옛 단계 번호로 새 단계 표를 인덱싱해 `IndexError`가 나지 않고, 사이클 처음/계획 위치로 옮겨지며 역할이 고정된다. (Task 6 테스트)

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `roundtable.py` | 단계 표·역할 표, 검토 판정 추출, 사이클 종료 처리, 시작/전환 가드, 상태 마이그레이션, 실행 계획 | 수정 |
| `triagent/domain/gitops.py` | 작업 브랜치 준비, 전체 커밋 (Git CLI 호출만, 상태 모름) | 생성 |
| `static/dashboard.js` | 실행 계획 상자에 역할 줄 표시 | 수정 (`refreshExecutionPlan`) |
| `tests/test_pipeline.py` | 이 계획의 새 회귀 테스트 전부 | 생성 |
| `tests/test_roundtable.py`, `tests/test_stability.py`, `tests/test_safety_fixes.py` | 옛 코딩 흐름을 전제로 한 기대값 갱신 | 수정 |
| `README.md` | 실행 모드·목표 기반 코딩 설명 갱신 | 수정 |

---

### Task 1: 파이프라인 단계 표와 고정 역할

**Files:**
- Modify: `roundtable.py` — `GOAL_CODING_STEPS`/`goal_coding_steps`(현재 526-563행 부근), `steps_for_mode`의 `coding` 분기(905-921행 부근), `allow_agent_calls`(1688행 부근)
- Create: `tests/test_pipeline.py`
- Modify: `tests/test_roundtable.py` (`test_scheduled_coding_step_cannot_delegate_duplicate_work`, `test_goal_coding_cycle_contains_development_review_and_synthesis`), `tests/test_stability.py` (`test_discussion_and_coding_keep_their_multi_step_flows`)

**Interfaces:**
- Produces: `PIPELINE_ROLES: dict[str, str]`, `PIPELINE_ROLE_LABELS: dict[str, str]`, `PIPELINE_CATALOG_ROLES: dict[str, str]`, `PIPELINE_IMPLEMENTER: str`, `REVIEW_PHASE: str`, `pipeline_planning_steps(enabled_agents) -> list[tuple[str, str, str, str]]`, `goal_coding_steps(enabled_agents)` (이름 유지, 내용 교체), `pipeline_agent_roles() -> dict[str, str]`, `pipeline_roles_label(enabled_agents) -> str`

- [ ] **Step 1: Write the failing tests** — `tests/test_pipeline.py` 생성

```python
"""고정 역할 코딩 파이프라인 회귀 테스트.

모든 파일 조작은 TemporaryDirectory 안에서만 한다. 실제 모델 CLI는 부르지 않는다.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isolation  # noqa: F401  (실제 데이터 폴더 격리 — 반드시 roundtable보다 먼저)
import roundtable

SESSION_ID = "20260101_000000_000030"
ALL = list(roundtable.AGENT_ORDER)


class PipelineStepsTests(unittest.TestCase):
    def test_planning_is_a_single_read_only_claude_turn_that_asks_for_approval(self):
        steps = roundtable.steps_for_mode("coding", ALL)
        self.assertEqual(len(steps), 1)
        agent, phase, instruction, cli_mode = steps[0]
        self.assertEqual((agent, phase, cli_mode), ("claude", roundtable.CONFIRM_PHASE, "discussion"))
        self.assertIn(roundtable.APPROVAL_TOKEN, instruction)

    def test_cycle_is_implement_test_review_in_fixed_order(self):
        steps = roundtable.goal_coding_steps(ALL)
        self.assertEqual(
            [(s[0], s[1], s[3]) for s in steps],
            [("claude", "구현", "coding"),
             ("antigravity", "테스트", "coding"),
             ("codex", roundtable.REVIEW_PHASE, "discussion")],
        )
        # 시작 모델 순서를 바꿔도 파이프라인 순서는 고정이다.
        self.assertEqual(roundtable.goal_coding_steps(["codex", "antigravity", "claude"]), steps)

    def test_disabled_agents_drop_their_steps(self):
        steps = roundtable.goal_coding_steps(["claude", "codex"])
        self.assertEqual([s[1] for s in steps], ["구현", roundtable.REVIEW_PHASE])

    def test_fixed_roles_map_onto_the_role_catalog(self):
        roles = roundtable.pipeline_agent_roles()
        self.assertEqual(roles, {"codex": "reviewer", "antigravity": "qa", "claude": ""})
        self.assertFalse(roundtable.ROLE_CATALOG["reviewer"]["can_write"])
        self.assertIn("tests/**", roundtable.ROLE_CATALOG["qa"]["patterns"])
        self.assertEqual(
            roundtable.pipeline_roles_label(ALL),
            "Claude Code=구현, Antigravity=테스트, Codex=검토",
        )

    def test_coding_mode_never_delegates_with_call_agent(self):
        for phase in ("구현", "테스트", roundtable.REVIEW_PHASE, roundtable.CONFIRM_PHASE):
            self.assertFalse(roundtable.allow_agent_calls("coding", phase))
        self.assertTrue(roundtable.allow_agent_calls("discussion", "개입 답변"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail**

Run: `$env:PYTHONPATH=".;tests"; python -m unittest test_pipeline -v`
Expected: FAIL/ERROR — `AttributeError: module 'roundtable' has no attribute 'REVIEW_PHASE'` 등

- [ ] **Step 3: Implement** — `roundtable.py`

`GOAL_CODING_STEPS` 표와 `goal_coding_steps` 함수 전체를 아래로 바꾼다.

```python
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
REVIEW_VERDICT_LINE = "REVIEW: PASS 또는 REVIEW: CHANGES"

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
```

(`AGENTS`, `normalize_agent_roles` 등은 함수 본문에서만 쓰므로 정의 순서와 무관하다. 모듈 수준에서 바로 계산되는 것은 `PIPELINE_IMPLEMENTER`와 `PIPELINE_CYCLE_STEPS`뿐이고, 둘 다 바로 위 상수만 쓴다.)

`steps_for_mode`의 `if mode == "coding":` 분기 전체를 아래 한 줄로 바꾼다.

```python
    if mode == "coding":
        return pipeline_planning_steps(enabled)
```

`allow_agent_calls`를 바꾼다.

```python
def allow_agent_calls(session_mode: str, phase: str) -> bool:
    # 코딩 파이프라인은 역할이 고정이라 모델끼리 일을 넘기지 않는다.
    return session_mode != "coding"
```

이제 안 쓰는 표를 지운다. 이름마다 `git grep -n -E "PROPOSAL_STEPS|CODING_WORK_STEPS|CODING_REPORT_STEP|GOAL_CODING_STEPS" -- roundtable.py tests` 결과가 정의 한 줄뿐인 것만 정의째 지운다. `make_confirm_step`, `make_report_step`은 토론 모드가 쓰므로 남긴다.

- [ ] **Step 4: 옛 흐름을 전제한 기존 테스트 갱신**

`tests/test_roundtable.py`:

```python
    def test_scheduled_coding_step_cannot_delegate_duplicate_work(self):
        self.assertFalse(roundtable.allow_agent_calls("coding", "구현"))
        self.assertFalse(roundtable.allow_agent_calls("coding", roundtable.REVIEW_PHASE))
        self.assertTrue(roundtable.allow_agent_calls("discussion", "개입 답변"))
```

```python
    def test_goal_coding_cycle_contains_development_review_and_synthesis(self):
        steps = roundtable.goal_coding_steps(roundtable.AGENT_ORDER)
        self.assertEqual([step[1] for step in steps], ["구현", "테스트", roundtable.REVIEW_PHASE])
        self.assertEqual([step[3] for step in steps], ["coding", "coding", "discussion"])
```

`tests/test_stability.py`:

```python
    def test_discussion_and_coding_keep_their_multi_step_flows(self):
        discussion = roundtable.steps_for_mode("discussion", list(roundtable.AGENT_ORDER))
        coding = roundtable.steps_for_mode("coding", list(roundtable.AGENT_ORDER))
        self.assertGreater(len(discussion), 3)
        # 코딩은 토론 없이 계획 1회 후 고정 사이클로 간다.
        self.assertEqual(len(coding), 1)
        self.assertEqual(len(roundtable.goal_coding_steps(list(roundtable.AGENT_ORDER))), 3)
```

- [ ] **Step 5: Run tests**

Run: `python -m unittest discover -s tests`
Expected: `test_pipeline` 5개 PASS. 남는 실패는 Task 2·4·6에서 고칠 것만이어야 한다: `test_goal_coding_worker_wraps_into_next_cycle`(옛 phase 이름), `test_coding_planning_transitions_to_goal_iteration`, `test_legacy_continuous_session_migrates_to_goal_coding_position`, `test_execution_plan_shows_call_counts_per_mode`, `test_safety_fixes.PlanApprovalGateTests.test_worker_asks_again_instead_of_running_an_unapproved_write_step`. 그 밖의 실패가 있으면 멈추고 원인을 확인한다.

- [ ] **Step 6: Commit**

```bash
git add roundtable.py tests/test_pipeline.py tests/test_roundtable.py tests/test_stability.py
git commit -m "Replace coding debate steps with fixed-role pipeline tables"
```

---

### Task 2: 검토 판정 읽기와 검증 시점

**Files:**
- Modify: `roundtable.py` — `extract_approval_token` 옆(2516행 부근), `run_step`의 토큰 처리부(3882행 부근)와 `meta` dict, `worker_loop`의 `should_validate`(4176행 부근)
- Test: `tests/test_pipeline.py`, `tests/test_roundtable.py` (`test_goal_coding_worker_wraps_into_next_cycle`)

**Interfaces:**
- Consumes: `REVIEW_PHASE` (Task 1)
- Produces: `extract_review_verdict(text: str) -> tuple[str, str]` — `(판정 줄을 뺀 본문, "pass" | "changes" | "")`; 메시지 `meta["review_verdict"]`

- [ ] **Step 1: Write the failing tests** — `tests/test_pipeline.py`에 추가

```python
class ReviewVerdictTests(unittest.TestCase):
    def test_last_line_verdict_is_extracted_and_hidden(self):
        self.assertEqual(roundtable.extract_review_verdict("문제 없음\nREVIEW: PASS"), ("문제 없음", "pass"))
        self.assertEqual(roundtable.extract_review_verdict("고칠 것\n review: changes \n"), ("고칠 것", "changes"))

    def test_missing_or_quoted_verdict_is_not_a_pass(self):
        for text in ("검토했습니다.", "REVIEW: PASS 라고 쓰면 통과입니다.\n추가 의견", "REVIEW: MAYBE", ""):
            with self.subTest(text=text):
                self.assertEqual(roundtable.extract_review_verdict(text)[1], "")


@patch.object(roundtable, "render_html_snapshot")
@patch.object(roundtable, "add_runtime_event")
@patch.object(roundtable, "build_memory_context", return_value="")
@patch.object(roundtable, "load_team_prompt", return_value="공통 지침")
@patch.object(roundtable, "save_state")
class ReviewTurnTests(unittest.TestCase):
    def setUp(self):
        self.original_state = roundtable.STATE
        self.original_control = dict(roundtable.CONTROL)
        roundtable.STATE = roundtable.new_state()
        roundtable.STATE.update(id=SESSION_ID, topic="검토 테스트", mode="coding", plan_approved=True,
                                coding_stage="iteration", agent_roles=roundtable.pipeline_agent_roles())
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        roundtable.STATE = self.original_state
        roundtable.CONTROL.clear()
        roundtable.CONTROL.update(self.original_control)
        self._tmp.cleanup()

    def run_review(self, reply: str) -> dict:
        with patch.dict(roundtable.ASK_FUNCS, {"codex": lambda _p, _m: reply}), \
             patch.object(roundtable, "read_configured_project_path", return_value=self._tmp.name), \
             patch.object(roundtable, "add_message", return_value=True) as add_message:
            roundtable.run_step("codex", f"{roundtable.REVIEW_PHASE} · 목표 사이클 1", "검토해라",
                                "discussion", expected_session_id=SESSION_ID)
        _agent, _phase, text, meta = add_message.call_args.args[:4]
        return {"text": text, "meta": meta}

    def test_review_turn_records_the_verdict_and_hides_the_token(self, *_):
        result = self.run_review("치명적 결함 없음\nREVIEW: PASS")
        self.assertEqual(result["meta"]["review_verdict"], "pass")
        self.assertNotIn("REVIEW:", result["text"])

    def test_verdict_survives_a_clipped_long_review(self, *_):
        result = self.run_review("가" * (roundtable.OUTPUT_MAX_CHARS * 2) + "\nREVIEW: CHANGES")
        self.assertEqual(result["meta"]["review_verdict"], "changes")
```

(`run_step`은 끝에서 `add_message(agent, phase, text, meta, expected_session_id=...)`로 부르므로 `call_args.args[:4]`가 `(agent, phase, text, meta)`다.)

- [ ] **Step 2: Run to verify they fail**

Run: `$env:PYTHONPATH=".;tests"; python -m unittest test_pipeline.ReviewVerdictTests test_pipeline.ReviewTurnTests -v`
Expected: ERROR — `has no attribute 'extract_review_verdict'`

- [ ] **Step 3: Implement**

`extract_approval_token` 바로 아래에 추가:

```python
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
```

`run_step`에서 `visible_text, approval_requested = extract_approval_token(call_clean_text)` 바로 다음 줄에 추가 (`clip_agent_output`보다 앞이어야 긴 답변에서도 판정이 남는다):

```python
    review_verdict = ""
    if phase.split(" · ", 1)[0] == REVIEW_PHASE:
        visible_text, review_verdict = extract_review_verdict(visible_text)
```

같은 함수의 `meta = {` dict에서 `"approval_requested"` 키 다음 줄에 추가:

```python
        "review_verdict": review_verdict,
```

`worker_loop`의 `should_validate` 식 전체를 아래로 바꾼다 (검토 전에 한 번 검증하고, 검토 단계가 없으면 사이클 끝에 검증한다).

```python
            has_review_step = any(step[1] == REVIEW_PHASE for step in steps)
            should_validate = cli_mode == "coding" and (
                (coding_iteration and not cycle_completed and steps[next_index][1] == REVIEW_PHASE)
                or (coding_iteration and cycle_completed and not has_review_step)
                or (not coding_iteration and (
                    next_index >= len(steps) or steps[next_index][1] == "최종 보고"
                ))
            )
```

- [ ] **Step 4: 옛 phase 이름을 쓰는 워커 테스트 갱신** — `tests/test_roundtable.py`의 `test_goal_coding_worker_wraps_into_next_cycle` 마지막 assert 세 줄을 바꾼다 (cycle은 이제 3단계, 판정이 없으므로 사이클 2로 넘어간다).

```python
        self.assertIn("구현 · 목표 사이클 1", phases[0])
        self.assertIn(f"{roundtable.REVIEW_PHASE} · 목표 사이클 1", phases[len(cycle) - 1])
        self.assertIn("구현 · 목표 사이클 2", phases[len(cycle)])
```

- [ ] **Step 5: Run tests**

Run: `$env:PYTHONPATH=".;tests"; python -m unittest test_pipeline test_roundtable -v`
Expected: `test_pipeline` 전부 PASS, `test_goal_coding_worker_wraps_into_next_cycle` PASS (`validation.assert_called_once()` 포함 — 검토 전에 한 번만 검증)

- [ ] **Step 6: Commit**

```bash
git add roundtable.py tests/test_pipeline.py tests/test_roundtable.py
git commit -m "Read review verdict from the last line and validate before review"
```

---

### Task 3: Git 작업 브랜치와 커밋 모듈

**Files:**
- Create: `triagent/domain/gitops.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `GitResult(ok: bool, detail: str)`, `is_repo(root: Path) -> bool`, `is_clean(root: Path) -> bool`, `current_branch(root: Path) -> str`, `prepare_work_branch(root: Path, branch: str) -> GitResult` (성공 시 `detail` = 브랜치 이름), `commit_all(root: Path, message: str) -> GitResult` (성공 시 `detail` = 짧은 SHA, 변경 없으면 `""`)

- [ ] **Step 1: Write the failing tests** — `tests/test_pipeline.py`에 추가

```python
from triagent.domain import gitops


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(shutil.which("git"), "git이 없으면 커밋을 검증할 수 없다")
class GitOpsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def init_repo(self) -> None:
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "core.autocrlf", "false")
        (self.root / "app.py").write_text("x = 1\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")

    def test_not_a_repo_is_refused_without_side_effects(self):
        result = gitops.prepare_work_branch(self.root, "triagent/s1")
        self.assertFalse(result.ok)
        self.assertIn("Git 저장소가 아니", result.detail)
        self.assertFalse((self.root / ".git").exists())

    def test_clean_repo_switches_to_the_work_branch(self):
        self.init_repo()
        result = gitops.prepare_work_branch(self.root, "triagent/s1")
        self.assertEqual(result, gitops.GitResult(True, "triagent/s1"))
        self.assertEqual(gitops.current_branch(self.root), "triagent/s1")
        # 다시 불러도 그대로 성공한다 (재개 시).
        self.assertTrue(gitops.prepare_work_branch(self.root, "triagent/s1").ok)

    def test_dirty_repo_keeps_the_users_changes_and_branch(self):
        self.init_repo()
        (self.root / "app.py").write_text("x = 2  # 사용자 작업 중\n", encoding="utf-8")
        result = gitops.prepare_work_branch(self.root, "triagent/s1")
        self.assertFalse(result.ok)
        self.assertIn("커밋되지 않은 변경", result.detail)
        self.assertEqual(gitops.current_branch(self.root), "main")
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "x = 2  # 사용자 작업 중\n")

    def test_commit_all_returns_sha_and_skips_when_nothing_changed(self):
        self.init_repo()
        gitops.prepare_work_branch(self.root, "triagent/s1")
        (self.root / "new.py").write_text("y = 1\n", encoding="utf-8")
        result = gitops.commit_all(self.root, "TriAgent cycle 1: test")
        self.assertTrue(result.ok)
        self.assertEqual(result.detail, git(self.root, "rev-parse", "--short", "HEAD"))
        self.assertEqual(git(self.root, "log", "-1", "--format=%s"), "TriAgent cycle 1: test")
        self.assertEqual(git(self.root, "rev-parse", "--short", "main"),
                         git(self.root, "rev-parse", "--short", "HEAD~1"))  # main은 그대로
        self.assertEqual(gitops.commit_all(self.root, "again"), gitops.GitResult(True, ""))
```

- [ ] **Step 2: Run to verify they fail**

Run: `$env:PYTHONPATH=".;tests"; python -m unittest test_pipeline.GitOpsTests -v`
Expected: ERROR — `cannot import name 'gitops'`

- [ ] **Step 3: Implement** — `triagent/domain/gitops.py`

```python
"""작업 폴더의 Git 커밋 — 파이프라인 사이클마다 체크포인트를 남긴다.

원칙:

* 사용자의 기존 변경을 섞지 않는다. 브랜치를 만들 때 작업 트리가 깨끗하지 않으면
  아무것도 바꾸지 않고 이유를 돌려준다.
* main/master에 직접 커밋하지 않는다 — 호출부가 ``triagent/<세션ID>`` 브랜치를 넘긴다.
* 실패는 예외가 아니라 ``GitResult(ok=False, detail=사유)``로 돌려준다. 커밋 실패가
  파이프라인 자체를 멈추면 안 된다.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["GitResult", "is_repo", "is_clean", "current_branch", "prepare_work_branch", "commit_all"]


@dataclass(frozen=True)
class GitResult:
    ok: bool
    detail: str = ""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    git = shutil.which("git") or "git"
    return subprocess.run(
        [git, "-C", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


def is_repo(root: Path) -> bool:
    try:
        result = _git(root, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def is_clean(root: Path) -> bool:
    result = _git(root, "status", "--porcelain")
    return result.returncode == 0 and not result.stdout.strip()


def current_branch(root: Path) -> str:
    result = _git(root, "branch", "--show-current")
    return result.stdout.strip() if result.returncode == 0 else ""


def prepare_work_branch(root: Path, branch: str) -> GitResult:
    """깨끗한 저장소에서 작업 브랜치로 옮긴다. 이미 그 브랜치면 그대로 둔다."""
    if not is_repo(root):
        return GitResult(False, "Git 저장소가 아니라 자동 커밋을 하지 않습니다.")
    if current_branch(root) == branch:
        return GitResult(True, branch)
    if not is_clean(root):
        return GitResult(False, "커밋되지 않은 변경이 있어 자동 커밋을 하지 않습니다. 먼저 커밋하거나 정리해주세요.")
    result = _git(root, "switch", "-c", branch)
    if result.returncode != 0:
        result = _git(root, "switch", branch)   # 이미 있는 브랜치 (세션 재개)
    if result.returncode != 0:
        return GitResult(False, f"작업 브랜치를 만들지 못했습니다: {result.stderr.strip()[:300]}")
    return GitResult(True, branch)


def commit_all(root: Path, message: str) -> GitResult:
    """변경이 있으면 전부 커밋하고 짧은 SHA를 돌려준다. 변경이 없으면 ok=True, detail=''."""
    try:
        added = _git(root, "add", "-A")
        if added.returncode != 0:
            return GitResult(False, added.stderr.strip()[:300])
        if _git(root, "diff", "--cached", "--quiet").returncode == 0:
            return GitResult(True, "")
        committed = _git(root, "commit", "-q", "-m", message)
        if committed.returncode != 0:
            # 사용자 이름/이메일 미설정, pre-commit 훅 실패 등
            return GitResult(False, (committed.stderr or committed.stdout).strip()[:300])
        return GitResult(True, _git(root, "rev-parse", "--short", "HEAD").stdout.strip())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitResult(False, f"git 실행 실패: {exc}")
```

- [ ] **Step 4: Run tests**

Run: `$env:PYTHONPATH=".;tests"; python -m unittest test_pipeline.GitOpsTests -v`
Expected: 4개 PASS

- [ ] **Step 5: Commit**

```bash
git add triagent/domain/gitops.py tests/test_pipeline.py
git commit -m "Add gitops helpers for work-branch cycle commits"
```

---

### Task 4: 사이클 종료 — 커밋, 완료 판정, 워커 연결

**Files:**
- Modify: `roundtable.py` — import 줄(63행 부근), `worker_loop`의 사이클 1 진입 블록(4100행 부근 `"계획·승인 단계 완료 · 목표 기반 코딩 사이클 1 시작"`)과 단계 진행 블록(`if coding_iteration and cycle_completed:`), 새 함수 3개는 `worker_loop` 바로 위
- Test: `tests/test_pipeline.py`, `tests/test_roundtable.py` (`test_coding_planning_transitions_to_goal_iteration`), `tests/test_safety_fixes.py` (`test_worker_asks_again_instead_of_running_an_unapproved_write_step`)

**Interfaces:**
- Consumes: `REVIEW_PHASE`, `goal_coding_steps` (Task 1), `meta["review_verdict"]` (Task 2), `gitops.prepare_work_branch`, `gitops.commit_all`, `gitops.GitResult` (Task 3)
- Produces: `prepare_pipeline_git(session_id: str) -> None`, `commit_cycle(session_id: str, cycle_number: int, verdict: str, validation_ok: bool) -> None`, `finish_pipeline_cycle(session_id: str, cycle_number: int) -> bool`, `STATE["pipeline_git"] = {"enabled": bool, "branch": str, "note": str, "commits": [{"cycle": int, "sha": str}]}`

- [ ] **Step 1: Write the failing tests** — `tests/test_pipeline.py`에 추가

```python
class CycleFinishTests(unittest.TestCase):
    def setUp(self):
        self.original_state = roundtable.STATE
        roundtable.STATE = roundtable.new_state()
        roundtable.STATE.update(id=SESSION_ID, mode="coding", coding_stage="iteration",
                                plan_approved=True, enabled_agents=ALL, messages=[])

    def tearDown(self):
        roundtable.STATE = self.original_state

    def review(self, cycle: int, verdict: str) -> None:
        roundtable.STATE["messages"].append({
            "agent": "codex", "phase": f"{roundtable.REVIEW_PHASE} · 목표 사이클 {cycle}",
            "text": "검토", "meta": {"review_verdict": verdict},
        })

    def finish(self, validation_ok: bool = True) -> bool:
        roundtable.STATE["validation_results"] = [{"label": "unittest", "ok": validation_ok, "elapsed": 1}]
        with patch.object(roundtable, "save_state"), patch.object(roundtable, "add_runtime_event"), \
             patch.object(roundtable, "commit_cycle") as commit:
            done = roundtable.finish_pipeline_cycle(SESSION_ID, 1)
        commit.assert_called_once()
        return done

    def test_pass_and_green_validation_finishes_the_session(self):
        self.review(1, "pass")
        self.assertTrue(self.finish())
        self.assertTrue(roundtable.STATE["finished"])
        self.assertEqual(roundtable.STATE["coding_stop_reason"], "검토·검증 통과")

    def test_changes_or_missing_verdict_or_red_validation_continues(self):
        for verdict, validation_ok in (("changes", True), ("", True), ("pass", False)):
            with self.subTest(verdict=verdict, validation_ok=validation_ok):
                roundtable.STATE.update(messages=[], finished=False)
                if verdict:
                    self.review(1, verdict)
                self.assertFalse(self.finish(validation_ok))
                self.assertFalse(roundtable.STATE["finished"])

    def test_an_older_cycles_pass_does_not_count(self):
        self.review(0, "pass")
        self.assertFalse(self.finish())

    def test_without_a_reviewer_green_validation_is_enough(self):
        roundtable.STATE["enabled_agents"] = ["claude", "antigravity"]
        self.assertTrue(self.finish())


class CycleCommitTests(unittest.TestCase):
    def setUp(self):
        self.original_state = roundtable.STATE
        roundtable.STATE = roundtable.new_state()
        roundtable.STATE.update(id=SESSION_ID, topic="장바구니  기능\n추가", mode="coding")
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        roundtable.STATE = self.original_state
        self._tmp.cleanup()

    def call(self, git_state: dict, commit_result=None):
        roundtable.STATE["pipeline_git"] = git_state
        with patch.object(roundtable, "save_state"), \
             patch.object(roundtable, "add_runtime_event") as event, \
             patch.object(roundtable, "read_configured_project_path", return_value=self._tmp.name), \
             patch.object(roundtable.gitops, "commit_all",
                          return_value=commit_result or roundtable.gitops.GitResult(True, "abc1234")) as commit:
            roundtable.commit_cycle(SESSION_ID, 2, "changes", True)
        return commit, event

    def test_commit_message_and_recorded_sha(self):
        commit, _event = self.call({"enabled": True, "branch": "triagent/x", "note": "", "commits": []})
        message = commit.call_args.args[1]
        self.assertTrue(message.startswith("TriAgent cycle 2: 장바구니 기능 추가"))
        self.assertIn("Review: changes", message)
        self.assertEqual(roundtable.STATE["pipeline_git"]["commits"], [{"cycle": 2, "sha": "abc1234"}])

    def test_disabled_git_never_commits(self):
        commit, _event = self.call({"enabled": False, "branch": "", "note": "Git 저장소가 아니라", "commits": []})
        commit.assert_not_called()

    def test_commit_failure_is_reported_not_raised(self):
        _commit, event = self.call(
            {"enabled": True, "branch": "triagent/x", "note": "", "commits": []},
            roundtable.gitops.GitResult(False, "Please tell me who you are"),
        )
        self.assertIn("Please tell me who you are", event.call_args.args[0])
        self.assertEqual(roundtable.STATE["pipeline_git"]["commits"], [])
```

- [ ] **Step 2: Run to verify they fail**

Run: `$env:PYTHONPATH=".;tests"; python -m unittest test_pipeline.CycleFinishTests test_pipeline.CycleCommitTests -v`
Expected: ERROR — `has no attribute 'finish_pipeline_cycle'`

- [ ] **Step 3: Implement**

import 줄에 `gitops`를 추가한다:

```python
from triagent.domain import clicheck, gitops, runstatus, usage as usage_domain, validation, workflow
```

`worker_loop` 정의 바로 위에 추가:

```python
def prepare_pipeline_git(session_id: str) -> None:
    """사이클 1 진입 시 작업 브랜치를 준비한다. 안 되면 이유만 남기고 커밋 없이 진행한다."""
    resolution = resolve_project_workspace(require_write=True)
    if resolution.blocked or resolution.path is None:
        return
    result = gitops.prepare_work_branch(resolution.path, f"triagent/{session_id}")
    with STATE_LOCK:
        if STATE.get("id") != session_id:
            return
        STATE["pipeline_git"] = {
            "enabled": result.ok,
            "branch": result.detail if result.ok else "",
            "note": "" if result.ok else result.detail,
            "commits": [],
        }
        save_state(STATE)
    if result.ok:
        add_runtime_event(f"사이클 커밋 브랜치: {result.detail}")
    else:
        add_runtime_event(f"자동 커밋 안 함 — {result.detail}", "warn")


def commit_cycle(session_id: str, cycle_number: int, verdict: str, validation_ok: bool) -> None:
    with STATE_LOCK:
        if STATE.get("id") != session_id:
            return
        git_state = dict(STATE.get("pipeline_git") or {})
        topic = re.sub(r"\s+", " ", STATE.get("topic", "")).strip()[:60]
    if not git_state.get("enabled"):
        return
    resolution = resolve_project_workspace(require_write=True)
    if resolution.blocked or resolution.path is None:
        return
    message = (
        f"TriAgent cycle {cycle_number}: {topic}\n\n"
        f"Review: {verdict or 'none'} · Validation: {'pass' if validation_ok else 'fail'}\n"
        f"Session: {session_id}"
    )
    result = gitops.commit_all(resolution.path, message)
    if not result.ok:
        add_runtime_event(f"사이클 {cycle_number} 커밋 실패 — {result.detail}", "warn")
        return
    if not result.detail:
        add_runtime_event(f"사이클 {cycle_number}: 변경이 없어 커밋하지 않았습니다.")
        return
    with STATE_LOCK:
        if STATE.get("id") == session_id:
            commits = list((STATE.get("pipeline_git") or {}).get("commits", []))
            commits.append({"cycle": cycle_number, "sha": result.detail})
            STATE["pipeline_git"] = {**git_state, "commits": commits}
            save_state(STATE)
    add_runtime_event(f"사이클 {cycle_number} 커밋: {result.detail} ({git_state.get('branch', '')})")


def finish_pipeline_cycle(session_id: str, cycle_number: int) -> bool:
    """사이클 끝: 커밋하고, 검토 통과 + 검증 통과면 세션을 완료한다. 완료했으면 True."""
    marker = f"{REVIEW_PHASE} · 목표 사이클 {cycle_number}"
    with STATE_LOCK:
        if STATE.get("id") != session_id:
            return False
        verdict = next(
            ((message.get("meta") or {}).get("review_verdict", "")
             for message in reversed(STATE.get("messages", []))
             if message.get("phase") == marker),
            "",
        )
        validation_ok = all(result.get("ok") for result in STATE.get("validation_results") or [])
        has_reviewer = any(step[1] == REVIEW_PHASE for step in goal_coding_steps(STATE.get("enabled_agents")))
    commit_cycle(session_id, cycle_number, verdict, validation_ok)
    if not validation_ok or (has_reviewer and verdict != "pass"):
        return False
    with STATE_LOCK:
        if STATE.get("id") != session_id:
            return False
        STATE["finished"] = True
        STATE["coding_stopped"] = True
        STATE["coding_stop_reason"] = "검토·검증 통과"
        STATE["workflow_status"] = workflow.COMPLETED
        save_state(STATE)
    add_runtime_event(f"사이클 {cycle_number}: 검토·검증 통과 — 작업 완료")
    return True
```

`worker_loop`의 사이클 1 진입 블록에서 `add_runtime_event("계획·승인 단계 완료 · 목표 기반 코딩 사이클 1 시작")` 다음, `continue` 앞에 한 줄 추가:

```python
                prepare_pipeline_git(session_id)
```

`worker_loop`에서 `should_validate` 블록(`for result in validation_results: ...`가 끝나는 곳) 다음, `if phase == CONFIRM_PHASE and not CONTROL["approval_requested"]:` 앞에 추가 (검토가 없을 때의 사이클 끝 검증이 먼저 끝나야 한다):

```python
            if coding_iteration and cycle_completed and finish_pipeline_cycle(session_id, cycle_number):
                break
```

- [ ] **Step 4: 워커 테스트 두 개 갱신**

`tests/test_roundtable.py`의 `test_coding_planning_transitions_to_goal_iteration`에서 `"codex"`만 켜던 것을 구현 담당이 있는 구성으로 바꾸고, Git 준비는 이 테스트 관심 밖이라 막는다:

```python
        planning_steps = roundtable.steps_for_mode("coding", ["claude", "codex"])
        roundtable.STATE.update(
            topic="목표 코딩",
            mode="coding",
            enabled_agents=["claude", "codex"],
            coding_stage="planning",
            plan_approved=True,   # 계획 단계를 끝까지 지났다 = 사용자가 승인했다
            step_index=len(planning_steps),
            finished=False,
        )
```

같은 테스트의 `with patch.object(roundtable, "run_step", side_effect=fake_run_step):` 줄을 아래로 바꾼다:

```python
        with patch.object(roundtable, "run_step", side_effect=fake_run_step), \
             patch.object(roundtable, "prepare_pipeline_git"):
```

`tests/test_safety_fixes.py`의 `test_worker_asks_again_instead_of_running_an_unapproved_write_step`에서 처음 세 줄(`steps = ...`, `write_index = ...`, `roundtable.STATE.update(...)`)을 아래 한 문장으로 바꾼다. 계획 단계에는 이제 쓰기 단계가 없으므로, 승인 없이 사이클의 구현 단계에 도달한 상황으로 검증한다 (클래스 `setUp`의 `enabled_agents=["codex"]`는 이 `update`가 덮어쓴다):

```python
        roundtable.STATE.update(enabled_agents=["claude", "codex"], coding_stage="iteration",
                                coding_cycle=1, coding_cycle_step=0, plan_approved=False)
```

나머지(`run_step` 미호출, `wait` 1회, `approval_requested` True) 검증은 그대로 둔다.

- [ ] **Step 5: Run tests**

Run: `python -m unittest discover -s tests`
Expected: 남는 실패는 Task 6(`test_legacy_continuous_session_migrates_to_goal_coding_position`)과 Task 7(`test_execution_plan_shows_call_counts_per_mode`)뿐이다.

- [ ] **Step 6: Commit**

```bash
git add roundtable.py tests/test_pipeline.py tests/test_roundtable.py tests/test_safety_fixes.py
git commit -m "Commit each pipeline cycle and finish when review and validation pass"
```

---

### Task 5: 시작·전환 가드와 고정 역할 적용

**Files:**
- Modify: `roundtable.py` — `/topic` 핸들러(5840행 부근), `switch_mode`(5480행 부근), `update_agent_roles`(2833행 부근)
- Test: `tests/test_pipeline.py`, `tests/test_http_security.py`

**Interfaces:**
- Consumes: `PIPELINE_IMPLEMENTER`, `pipeline_agent_roles`, `pipeline_roles_label` (Task 1)

- [ ] **Step 1: Write the failing tests** — `tests/test_pipeline.py`에 추가

```python
class StartGuardTests(unittest.TestCase):
    def setUp(self):
        self.original_state = roundtable.STATE
        self.original_control = dict(roundtable.CONTROL)
        roundtable.STATE = roundtable.new_state()
        roundtable.STATE.update(id=SESSION_ID, topic="가드 테스트", mode="discussion")
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        roundtable.STATE = self.original_state
        roundtable.CONTROL.clear()
        roundtable.CONTROL.update(self.original_control)
        self._tmp.cleanup()

    def switch(self):
        with patch.object(roundtable, "read_configured_project_path", return_value=self._tmp.name), \
             patch.object(roundtable, "save_state"), patch.object(roundtable, "add_runtime_event"), \
             patch.object(roundtable, "start_worker_if_needed"):
            return roundtable.switch_mode("coding")

    def test_switching_to_coding_without_claude_is_refused(self):
        roundtable.STATE["enabled_agents"] = ["codex", "antigravity"]
        result = self.switch()
        self.assertIn("Claude Code", result.get("error", ""))
        self.assertEqual(roundtable.STATE["mode"], "discussion")

    def test_switching_to_coding_fixes_the_roles(self):
        roundtable.STATE.update(enabled_agents=ALL, agent_roles={"codex": "backend", "antigravity": "", "claude": "qa"})
        self.assertTrue(self.switch().get("success"))
        self.assertEqual(roundtable.STATE["agent_roles"], roundtable.pipeline_agent_roles())

    def test_roles_cannot_be_changed_in_coding_mode(self):
        roundtable.STATE.update(mode="coding", agent_roles=roundtable.pipeline_agent_roles())
        result = roundtable.update_agent_roles({"codex": "backend", "antigravity": "qa", "claude": ""})
        self.assertIn("역할이 고정", result.get("error", ""))
        self.assertEqual(roundtable.STATE["agent_roles"], roundtable.pipeline_agent_roles())
```

`/topic` 경로는 HTTP 핸들러 안에 있다. 실제 임시 포트 서버를 띄우는 `HttpServerTestCase`가 있는 `tests/test_http_security.py` 끝에 추가한다 (다른 파일에서 이 클래스를 import하면 그 클래스의 테스트가 두 번 돈다). 이 파일은 이미 `roundtable`, `patch`, `webguard`를 import한다:

```python
class TopicGuardOverHttpTests(HttpServerTestCase):
    def setUp(self):
        self.original_state = roundtable.STATE
        roundtable.STATE = roundtable.new_state()

    def tearDown(self):
        roundtable.STATE = self.original_state

    def test_coding_topic_without_claude_is_rejected_with_400(self):
        body = "topic=x&mode=coding&agent=codex&agent=antigravity"
        with patch.object(roundtable, "start_worker_if_needed") as start:
            status, text = self.request(
                "/topic", method="POST", body=body,
                headers={webguard.CSRF_HEADER: self.token, "Origin": f"http://127.0.0.1:{self.port}",
                         "Content-Type": "application/x-www-form-urlencoded"},
            )
        self.assertEqual(status, 400, text[:200])
        self.assertIn("Claude Code", text)
        start.assert_not_called()
```

(`request(path, *, method, body, headers, host)`는 `(status, body_text)`를 돌려준다.)

- [ ] **Step 2: Run to verify they fail**

Run: `$env:PYTHONPATH=".;tests"; python -m unittest test_pipeline.StartGuardTests test_http_security.TopicGuardOverHttpTests -v`
Expected: FAIL — 오류 없이 전환됨/역할이 바뀜/200 응답

- [ ] **Step 3: Implement**

`/topic` 핸들러에서 `if not topic:` 검사 바로 앞에 추가:

```python
            if mode == "coding" and PIPELINE_IMPLEMENTER not in enabled_agents:
                self._send_error_json(
                    400, "코딩 모드에는 구현 담당 Claude Code가 필요합니다. Claude Code를 켜고 다시 시작해주세요."
                )
                return
```

같은 핸들러의 `STATE["plan_approved"] = False` 다음 줄에 추가:

```python
                    if mode == "coding":
                        STATE["agent_roles"] = pipeline_agent_roles()
                    STATE["pipeline_git"] = {}
```

`switch_mode`에서 `if mode == "coding" and resolve_project_workspace(require_write=True).blocked:` 검사 바로 앞에 추가:

```python
    if mode == "coding":
        with STATE_LOCK:
            implementer_enabled = PIPELINE_IMPLEMENTER in normalize_enabled_agents(STATE.get("enabled_agents"))
        if not implementer_enabled:
            return {"error": "코딩 모드에는 구현 담당 Claude Code가 필요합니다. Claude Code를 켠 세션에서 전환해주세요."}
```

같은 함수의 `if previous_mode != "coding":` 블록 안 `STATE["plan_approved"] = False` 다음 줄에 추가:

```python
                STATE["agent_roles"] = pipeline_agent_roles()
                STATE["pipeline_git"] = {}
```

`update_agent_roles`의 `with STATE_LOCK:` 블록 첫 줄(`if STATE.get("active_agent"):` 앞)에 추가:

```python
        if STATE.get("mode") == "coding":
            return {"error": "코딩 모드는 역할이 고정입니다: "
                             + pipeline_roles_label(STATE.get("enabled_agents"))}
```

- [ ] **Step 4: Run tests**

Run: `$env:PYTHONPATH=".;tests"; python -m unittest test_pipeline test_http_security -v`
Expected: 전부 PASS

- [ ] **Step 5: Commit**

```bash
git add roundtable.py tests/test_pipeline.py tests/test_http_security.py
git commit -m "Require Claude Code for coding mode and lock pipeline roles"
```

---

### Task 6: 진행 중이던 옛 코딩 세션 마이그레이션

**Files:**
- Modify: `roundtable.py` — `new_state`(1831행 부근), `normalize_state`의 `plan_approved` 계산 직후(2000행 부근)
- Test: `tests/test_pipeline.py`, `tests/test_roundtable.py` (`test_legacy_continuous_session_migrates_to_goal_coding_position`)

**Interfaces:**
- Consumes: `pipeline_planning_steps`, `pipeline_agent_roles` (Task 1)
- Produces: `PIPELINE_VERSION = 1`, `STATE["pipeline_version"]`

- [ ] **Step 1: Write the failing tests** — `tests/test_pipeline.py`에 추가

```python
class MigrationTests(unittest.TestCase):
    def test_old_planning_session_restarts_at_the_plan_step(self):
        state = roundtable.normalize_state({"mode": "coding", "coding_stage": "planning", "step_index": 11,
                                            "enabled_agents": ALL})
        self.assertEqual(state["step_index"], 0)
        self.assertEqual(state["pipeline_version"], roundtable.PIPELINE_VERSION)
        self.assertEqual(state["agent_roles"], roundtable.pipeline_agent_roles())
        roundtable.steps_for_mode("coding", ALL)[state["step_index"]]   # IndexError 없어야 함

    def test_old_approved_planning_session_goes_straight_to_cycles(self):
        state = roundtable.normalize_state({"mode": "coding", "coding_stage": "planning", "step_index": 11,
                                            "plan_approved": True, "enabled_agents": ALL})
        self.assertEqual(state["step_index"], len(roundtable.steps_for_mode("coding", ALL)))

    def test_old_iteration_session_restarts_its_current_cycle(self):
        state = roundtable.normalize_state({"mode": "coding", "coding_stage": "iteration", "coding_cycle": 2,
                                            "coding_cycle_step": 5, "enabled_agents": ALL})
        self.assertEqual((state["coding_cycle"], state["coding_cycle_step"]), (2, 0))

    def test_new_and_migrated_sessions_are_left_alone(self):
        fresh = roundtable.new_state()
        fresh.update(mode="coding", coding_stage="iteration", coding_cycle_step=2)
        self.assertEqual(roundtable.normalize_state(fresh)["coding_cycle_step"], 2)

    def test_finished_and_discussion_sessions_are_not_touched(self):
        done = roundtable.normalize_state({"mode": "coding", "coding_stage": "planning", "step_index": 11,
                                           "finished": True, "enabled_agents": ALL})
        self.assertEqual(done["step_index"], 11)
        talk = roundtable.normalize_state({"mode": "discussion", "step_index": 4, "enabled_agents": ALL})
        self.assertEqual(talk["step_index"], 4)
```

- [ ] **Step 2: Run to verify they fail**

Run: `$env:PYTHONPATH=".;tests"; python -m unittest test_pipeline.MigrationTests -v`
Expected: ERROR — `has no attribute 'PIPELINE_VERSION'`

- [ ] **Step 3: Implement**

`REVIEW_PHASE` 정의 근처에 추가:

```python
#: 코딩 단계 표가 바뀌면 올린다. 옛 세션의 단계 번호는 새 표에서 의미가 없다.
PIPELINE_VERSION = 1
```

`new_state()` 반환 dict의 `"plan_approved": False,` 다음 줄에 추가:

```python
        "pipeline_version": PIPELINE_VERSION,
```

`normalize_state`에서 `state["plan_approved"] = bool(...)` 문장 바로 다음에 추가:

```python
    # 파이프라인 전환 전에 진행 중이던 코딩 세션: 옛 단계 번호로 새 표를 가리키지 않게
    # 계획 처음(승인됐으면 사이클 진입 직전) 또는 현재 사이클 처음으로 옮기고 역할을 고정한다.
    if (
        state.get("mode") == "coding"
        and state.get("pipeline_version") != PIPELINE_VERSION
        and not state.get("finished")
    ):
        if state["coding_stage"] == "iteration":
            state["coding_cycle_step"] = 0
        else:
            state["step_index"] = len(pipeline_planning_steps()) if state["plan_approved"] else 0
        state["agent_roles"] = pipeline_agent_roles()
    state["pipeline_version"] = PIPELINE_VERSION
```

이 블록 뒤에 `state["agent_roles"] = normalize_agent_roles(state.get("agent_roles"))` 줄이 있어도 고정 역할은 유지된다(정규화만 한다).

- [ ] **Step 4: 기존 레거시 테스트 기대값 갱신** — `tests/test_roundtable.py`의 `test_legacy_continuous_session_migrates_to_goal_coding_position`에서

```python
        self.assertEqual(normalized["coding_cycle_step"], 2)
```

를 아래로 바꾼다 (옛 사이클 단계 번호는 새 표에서 의미가 없어 사이클 처음으로 간다. 사이클 번호는 유지):

```python
        self.assertEqual(normalized["coding_cycle_step"], 0)
```

- [ ] **Step 5: Run tests**

Run: `python -m unittest discover -s tests`
Expected: 남는 실패는 Task 7의 `test_execution_plan_shows_call_counts_per_mode`뿐

- [ ] **Step 6: Commit**

```bash
git add roundtable.py tests/test_pipeline.py tests/test_roundtable.py
git commit -m "Migrate in-progress coding sessions to the pipeline step tables"
```

---

### Task 7: 실행 계획 화면과 문서

**Files:**
- Modify: `roundtable.py` — `MODE_PLANS["coding"]`(4367행 부근), `mode_execution_plan`(4395행 부근), `topic_section_html`의 코딩 모드 라디오 문구(4553행 부근)
- Modify: `static/dashboard.js` — `refreshExecutionPlan`(1405행 부근)
- Modify: `README.md` — "실행 모드", "코딩 모드", "목표 기반 코딩" 절
- Test: `tests/test_pipeline.py`, `tests/test_stability.py` (`test_execution_plan_shows_call_counts_per_mode`)

**Interfaces:**
- Consumes: `goal_coding_steps`, `pipeline_roles_label`, `DEFAULT_CODING_LIMITS`
- Produces: 실행 계획 dict의 `"pipeline_roles": str` (코딩 모드가 아니면 `""`)

- [ ] **Step 1: Write the failing tests** — `tests/test_pipeline.py`에 추가

```python
class ExecutionPlanTests(unittest.TestCase):
    def test_coding_plan_counts_one_plan_call_plus_three_per_cycle(self):
        plan = roundtable.mode_execution_plan("coding", ALL)
        cycles = roundtable.DEFAULT_CODING_LIMITS["max_cycles"]
        self.assertEqual((plan["min_calls"], plan["max_calls"]), (1 + 3, 1 + 3 * cycles))
        self.assertFalse(plan["role_negotiation"])
        self.assertEqual(plan["pipeline_roles"], "Claude Code=구현, Antigravity=테스트, Codex=검토")

    def test_other_modes_have_no_pipeline_roles(self):
        self.assertEqual(roundtable.mode_execution_plan("quick", ALL)["pipeline_roles"], "")
```

`tests/test_stability.py`의 `test_execution_plan_shows_call_counts_per_mode`에서

```python
        self.assertGreater(coding["max_calls"], discussion["max_calls"])
```

를 아래로 바꾼다 (토론이 사라져 코딩이 더 적게 호출한다):

```python
        self.assertEqual(coding["max_calls"], 1 + 3 * roundtable.DEFAULT_CODING_LIMITS["max_cycles"])
        self.assertFalse(coding["role_negotiation"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `$env:PYTHONPATH=".;tests"; python -m unittest test_pipeline.ExecutionPlanTests test_stability -v`
Expected: FAIL — `KeyError: 'pipeline_roles'`, 호출 수 불일치

- [ ] **Step 3: Implement**

`MODE_PLANS["coding"]`를 바꾼다:

```python
    "coding": {
        "summary": "계획(Claude) → 승인 → [구현 Claude → 테스트 Antigravity → 자동 검증 → 검토 Codex] 반복 → 사이클마다 Git 커밋",
        "single_agent": False,
        "role_negotiation": False,
        "auto_validation": True,
        "default_access": "write",
        "allow_write": True,
        "note": "검토가 PASS이고 자동 검증이 통과하면 끝납니다. Git 저장소이고 커밋되지 않은 변경이 없으면 "
                "사이클 커밋을 triagent/<세션ID> 브랜치에 남깁니다.",
    },
```

`mode_execution_plan`의 호출 수 계산을 바꾼다:

```python
    if mode == "quick":
        min_calls = max_calls = 1
    elif mode == "coding":
        planning = len(steps_for_mode("coding", enabled))
        cycle = len(goal_coding_steps(enabled))
        min_calls = planning + cycle
        max_calls = planning + cycle * DEFAULT_CODING_LIMITS["max_cycles"]
    else:
        step_count = len(steps_for_mode(mode, enabled))
        min_calls = step_count
        # 개입·재시도로 호출이 늘어날 수 있다.
        max_calls = step_count * 2
```

같은 함수 반환 dict에 추가:

```python
        "pipeline_roles": pipeline_roles_label(enabled) if mode == "coding" else "",
```

`topic_section_html`의 코딩 모드 라디오 문구를 바꾼다:

```html
          <label><input type="radio" name="mode" value="coding" onchange="refreshExecutionPlan()"> 코딩 모드 (Claude 구현 · Antigravity 테스트 · Codex 검토 · 사이클마다 Git 커밋)</label>
```

`static/dashboard.js`의 `refreshExecutionPlan`에서 `const rows = [ ... ];` 다음 줄에 추가:

```javascript
    if (plan.pipeline_roles) rows.splice(2, 0, ["역할", plan.pipeline_roles]);
```

`README.md`:
- "실행 모드"의 **코딩 모드** 항목을 위 `MODE_PLANS["coding"]["summary"]` 내용과 같은 흐름으로 바꾼다. "코딩 모드의 승인 전 계획 단계에서는 세 모델 모두 읽기 전용" 문장은 "계획은 Claude Code가 한 번 작성하고, 승인 전에는 모든 모델이 읽기 전용"으로 바꾼다.
- "역할 선택과 강제"에 "코딩 모드는 역할이 고정(Claude Code=구현, Antigravity=테스트 파일만, Codex=읽기 전용 검토)이며 바꿀 수 없다. 역할 선택은 일반 토론 모드에만 적용된다"를 추가한다.
- "기본 실행 흐름 → 코딩 모드"와 "목표 기반 코딩" 절의 1~5단계를 "구현 → 테스트 → 자동 검증 → 검토(`REVIEW: PASS|CHANGES`) → 사이클 커밋, PASS+검증 통과 시 완료"로 바꾼다. 커밋 브랜치 규칙(Git 저장소 + 깨끗한 작업 트리일 때만, `triagent/<세션ID>`, main 미사용, 푸시는 사용자)을 적는다.
- "에이전트 간 호출" 절에 "코딩 모드에서는 쓰지 않는다"를 추가한다.

- [ ] **Step 4: Run tests and syntax check**

Run: `python -m unittest discover -s tests`
Expected: 전부 PASS
Run: `node --check static/dashboard.js`
Expected: 출력 없음, 종료 코드 0

- [ ] **Step 5: Commit**

```bash
git add roundtable.py static/dashboard.js README.md tests/test_pipeline.py tests/test_stability.py
git commit -m "Show pipeline roles and call counts in the execution plan"
```

---

### Task 8: 실제 화면·데이터 안전 검증

**Files:** 없음 (검증만)

- [ ] **Step 1: 실제 사용자 데이터가 그대로인지 확인하며 전체 테스트**

```powershell
$hash = { Get-ChildItem "roundtable_state.json","PROJECT_PATH.txt","sessions\*.json" -ErrorAction SilentlyContinue | ForEach-Object { "{0} {1}" -f (Get-FileHash $_.FullName).Hash, $_.Name } }
$before = & $hash; python -m unittest discover -s tests; $after = & $hash
if (Compare-Object $before $after) { "REAL DATA CHANGED" } else { "real data unchanged" }
```

Expected: 전부 PASS, `real data unchanged`

- [ ] **Step 2: 앱 사본으로 시작 화면 확인** — 짧은 경로(`%TEMP%\triagent-verify`)로 프로젝트를 복사하고(`robocopy . $dst /E /XD .git .claude __pycache__`), 사본의 `PROJECT_PATH.txt`를 빈 임시 폴더로 바꾼 뒤, `ROUNDTABLE_PORT=8790`과 `webbrowser.open = lambda *a, **k: True`를 설정한 래퍼로 `runpy.run_path(사본\roundtable.py, run_name="__main__")` 실행. 브라우저에서:
  - 코딩 모드를 고르면 실행 계획에 `역할: Claude Code=구현, Antigravity=테스트, Codex=검토`, 예상 호출 `4~10회`가 보인다.
  - Claude Code 체크를 끄고 코딩 모드로 시작하면 "구현 담당 Claude Code가 필요합니다" 오류가 보인다. (`/topic` 요청은 400으로 끝나 모델이 실행되지 않는다.)
  - 확인 후 서버를 끄고 사본을 지운다.

- [ ] **Step 3: (사용자 허락 시에만) 실제 모델 1사이클** — 버리는 Git 저장소(임시 폴더에 `git init` + 파일 1개 커밋)를 작업 폴더로 지정하고 코딩 모드로 작은 요청("README에 한 줄 추가")을 실행한다. 확인할 것: 계획 1회 → 승인 → 구현 → 테스트 → 검토 순서, `triagent/<세션ID>` 브랜치 생성, 사이클 커밋 1개, 검토 PASS 시 완료 표시. 요금이 드는 단계이므로 실행 전에 사용자에게 묻는다.

- [ ] **Step 4: 정리** — 결과를 PR 본문에 적는다: 통과한 테스트 수, 실제 데이터 해시 비교 결과, 화면 확인 결과, Step 3 실행 여부.

---

## Not in this plan (의도적으로 뺀 것)

- **사이클 되돌리기 버튼(`git revert`)** — 커밋이 남으므로 지금은 사용자가 직접 `git revert <sha>`로 되돌릴 수 있다. 화면 버튼은 커밋이 실제로 쓰이는 걸 확인한 뒤 추가한다. 기존 턴 단위 패치 롤백은 그대로 동작한다.
- **역할 바꾸기 UI** — `PIPELINE_ROLES` 한 곳만 바꾸면 되도록 해 두었다. 사용자가 실제로 다른 배치를 원할 때 설정 화면을 만든다.
- **일반 토론 모드 제거** — 이번에는 코딩 모드만 바꾼다. 토론 모드 정리는 별도 작업으로 판단한다.
- **IDE 연동(ACP 에이전트로 노출)** — 파이프라인이 자리 잡은 뒤 검토한다.
