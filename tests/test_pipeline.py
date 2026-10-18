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
from triagent.domain import gitops

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

    def test_commit_paths_returns_sha_and_skips_when_nothing_changed(self):
        self.init_repo()
        gitops.prepare_work_branch(self.root, "triagent/s1")
        (self.root / "new.py").write_text("y = 1\n", encoding="utf-8")
        result = gitops.commit_paths(self.root, "TriAgent cycle 1: test", ["new.py"])
        self.assertTrue(result.ok)
        self.assertEqual(result.detail, git(self.root, "rev-parse", "--short", "HEAD"))
        self.assertEqual(git(self.root, "log", "-1", "--format=%s"), "TriAgent cycle 1: test")
        self.assertEqual(git(self.root, "rev-parse", "--short", "main"),
                         git(self.root, "rev-parse", "--short", "HEAD~1"))  # main은 그대로
        self.assertEqual(gitops.commit_paths(self.root, "again", ["new.py"]), gitops.GitResult(True, ""))

    def test_commit_paths_leaves_the_users_other_work_alone(self):
        """에이전트가 바꾼 파일만 커밋한다 — 사용자가 고친 파일, 미리 스테이지한 것, 사라진 경로는 제외."""
        self.init_repo()
        (self.root / "notes.py").write_text("n = 0\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "notes")
        gitops.prepare_work_branch(self.root, "triagent/s1")
        (self.root / "agent.py").write_text("a = 1\n", encoding="utf-8")       # 에이전트 변경
        (self.root / "app.py").write_text("x = 99  # 사용자 작업\n", encoding="utf-8")  # 사용자 변경
        (self.root / "notes.py").write_text("n = 1\n", encoding="utf-8")
        git(self.root, "add", "notes.py")                                     # 사용자가 스테이지
        result = gitops.commit_paths(self.root, "cycle", ["agent.py", "made_then_deleted.py"])
        self.assertTrue(result.ok, result)
        self.assertEqual(git(self.root, "show", "--name-only", "--format=", "HEAD"), "agent.py")
        # git() 헬퍼는 앞 공백을 지우므로 porcelain 출력은 원문 그대로 본다.
        status = subprocess.run(["git", "-C", str(self.root), "status", "--porcelain"],
                                capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertIn(" M app.py", status)      # 사용자 변경은 커밋되지 않고 남음
        self.assertIn("M  notes.py", status)    # 사용자가 스테이지한 것도 그대로

    def test_commit_paths_in_a_subfolder_never_touches_the_parent_repo(self):
        self.init_repo()
        workspace = self.root / "app"
        workspace.mkdir()
        (workspace / "keep.py").write_text("k = 1\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "sub")
        (self.root / "outside.txt").write_text("사용자 파일\n", encoding="utf-8")
        (workspace / "keep.py").write_text("k = 2\n", encoding="utf-8")
        result = gitops.commit_paths(workspace, "cycle", ["keep.py"])
        self.assertTrue(result.ok, result)
        self.assertEqual(git(self.root, "show", "--name-only", "--format=", "HEAD"), "app/keep.py")
        self.assertIn("?? outside.txt", git(self.root, "status", "--porcelain"))

    def test_git_errors_while_preparing_are_reported_not_raised(self):
        with patch.object(gitops, "is_repo", return_value=True), \
             patch.object(gitops, "current_branch", side_effect=subprocess.TimeoutExpired("git", 60)):
            result = gitops.prepare_work_branch(self.root, "triagent/s1")
        self.assertFalse(result.ok)
        self.assertIn("git 실행 실패", result.detail)


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
        roundtable.STATE["validation_cycle"] = 1
        with patch.object(roundtable, "save_state"), patch.object(roundtable, "add_runtime_event"), \
             patch.object(roundtable, "commit_cycle") as commit:
            done = roundtable.finish_pipeline_cycle(SESSION_ID, 1)
        commit.assert_called_once()
        return done

    def test_stale_or_missing_validation_is_rerun_before_deciding(self):
        """실패 턴을 건너뛰어 이번 사이클 검증이 빠졌으면 이전 결과로 완료하지 않는다."""
        self.review(1, "pass")
        roundtable.STATE.update(validation_results=[{"label": "old", "ok": True, "elapsed": 1}], validation_cycle=0)
        failing = [{"label": "unittest", "ok": False, "elapsed": 1}]
        with patch.object(roundtable, "save_state"), patch.object(roundtable, "add_runtime_event"), \
             patch.object(roundtable, "commit_cycle"), \
             patch.object(roundtable, "run_validation_for_current_workspace", return_value=failing) as validate:
            done = roundtable.finish_pipeline_cycle(SESSION_ID, 1)
        validate.assert_called_once()
        self.assertFalse(done)
        self.assertEqual(roundtable.STATE["validation_cycle"], 1)

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

    def git_state(self, **overrides) -> dict:
        state = {"enabled": True, "branch": "triagent/x", "workspace": str(Path(self._tmp.name).resolve()),
                 "note": "", "commits": [], "committed_upto": 0}
        state.update(overrides)
        return state

    def call(self, git_state: dict, commit_result=None, on_branch: str = "triagent/x"):
        roundtable.STATE["pipeline_git"] = git_state
        with patch.object(roundtable, "save_state"), \
             patch.object(roundtable, "add_runtime_event") as event, \
             patch.object(roundtable, "read_configured_project_path", return_value=self._tmp.name), \
             patch.object(roundtable.gitops, "current_branch", return_value=on_branch), \
             patch.object(roundtable.gitops, "commit_paths",
                          return_value=commit_result or roundtable.gitops.GitResult(True, "abc1234")) as commit:
            roundtable.commit_cycle(SESSION_ID, 2, "changes", True)
        return commit, event

    def test_commit_message_and_recorded_sha(self):
        commit, _event = self.call(self.git_state())
        message = commit.call_args.args[1]
        self.assertTrue(message.startswith("TriAgent cycle 2: 장바구니 기능 추가"))
        self.assertIn("Review: changes", message)
        self.assertEqual(roundtable.STATE["pipeline_git"]["commits"], [{"cycle": 2, "sha": "abc1234"}])

    def test_only_files_changed_by_agent_turns_since_the_last_commit_are_committed(self):
        def turn(*paths):
            return {"agent": "claude", "meta": {"changed_paths": [{"path": p, "change": "수정"} for p in paths]}}

        roundtable.STATE["messages"] = [turn("old.py"), turn("src/a.py", "src/new_dir/"), turn("tests/test_a.py")]
        commit, _event = self.call(self.git_state(committed_upto=1))
        self.assertEqual(commit.call_args.args[2], ["src/a.py", "tests/test_a.py"])
        self.assertEqual(roundtable.STATE["pipeline_git"]["committed_upto"], 3)

    def test_wrong_branch_or_folder_never_commits(self):
        """사용자가 main으로 돌아갔거나 작업 폴더를 바꿨으면 그 브랜치에 섞어 커밋하지 않는다."""
        for git_state, on_branch in (
            (self.git_state(), "main"),
            (self.git_state(workspace="C:\\other\\project"), "triagent/x"),
        ):
            with self.subTest(on_branch=on_branch, workspace=git_state["workspace"]):
                commit, event = self.call(git_state, on_branch=on_branch)
                commit.assert_not_called()
                self.assertIn("커밋 안 함", event.call_args.args[0])

    def test_disabled_git_never_commits(self):
        commit, _event = self.call(self.git_state(enabled=False, branch="", note="Git 저장소가 아니라"))
        commit.assert_not_called()

    def test_commit_failure_is_reported_not_raised(self):
        _commit, event = self.call(
            self.git_state(), roundtable.gitops.GitResult(False, "Please tell me who you are"),
        )
        self.assertIn("Please tell me who you are", event.call_args.args[0])
        self.assertEqual(roundtable.STATE["pipeline_git"]["commits"], [])


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


@patch.object(roundtable, "update_profile")
@patch.object(roundtable, "render_html_snapshot")
@patch.object(roundtable, "add_runtime_event")
@patch.object(roundtable, "save_state")
class WorkerFlowTests(unittest.TestCase):
    """리뷰에서 나온 worker_loop 흐름 결함의 회귀 테스트 (모델은 부르지 않는다)."""

    def setUp(self):
        self.original_state = roundtable.STATE
        self.original_control = dict(roundtable.CONTROL)
        roundtable.STATE = roundtable.new_state()
        roundtable.STATE.update(id=SESSION_ID, topic="흐름", mode="coding", enabled_agents=ALL,
                                coding_stage="iteration", coding_cycle=1, coding_cycle_step=0,
                                plan_approved=True, finished=False)
        roundtable.CONTROL.update(stopped=False, paused=False, approval_requested=False,
                                  awaiting_approval=False, intervention_pending=False, intervention_queue=[],
                                  worker_session_id=SESSION_ID)

    def tearDown(self):
        roundtable.STATE = self.original_state
        roundtable.CONTROL.clear()
        roundtable.CONTROL.update(self.original_control)

    def test_a_finished_session_does_not_start_new_cycles(self, *_):
        roundtable.STATE.update(finished=True, coding_stop_reason="검토·검증 통과")
        with patch.object(roundtable, "run_step") as run_step:
            roundtable.worker_loop(SESSION_ID)
        run_step.assert_not_called()

    def test_entering_cycles_without_approval_asks_again_before_touching_git(self, *_):
        roundtable.STATE.update(coding_stage="planning", step_index=1, plan_approved=False)
        with patch.object(roundtable, "prepare_pipeline_git") as prepare, \
             patch.object(roundtable, "wait_for_user_approval", return_value=False) as wait:
            roundtable.worker_loop(SESSION_ID)
        prepare.assert_not_called()
        wait.assert_called_once()
        self.assertEqual(roundtable.STATE["coding_stage"], "planning")
        self.assertIsNone(roundtable.STATE["coding_started_at"])   # 승인 전에는 시간 측정도 시작하지 않음

    def test_skipping_past_the_cycle_end_still_commits_and_judges(self, *_):
        roundtable.STATE["coding_cycle_step"] = len(roundtable.goal_coding_steps(ALL))
        with patch.object(roundtable, "finish_pipeline_cycle", return_value=True) as finish, \
             patch.object(roundtable, "run_step") as run_step:
            roundtable.worker_loop(SESSION_ID)
        finish.assert_called_once_with(SESSION_ID, 1)
        run_step.assert_not_called()

    def test_coding_session_without_claude_never_runs(self, *_):
        roundtable.STATE["enabled_agents"] = ["codex", "antigravity"]
        with patch.object(roundtable, "run_step") as run_step:
            roundtable.worker_loop(SESSION_ID)
        run_step.assert_not_called()
        self.assertTrue(roundtable.CONTROL["paused"])


class ReviewFollowUpTests(unittest.TestCase):
    def test_tester_scope_accepts_common_test_layouts_but_not_product_code(self):
        state = {"agent_roles": roundtable.pipeline_agent_roles()}
        allowed = ["src/__tests__/cart.js", "shop/tests.py", "pkg/tests/test_x.py", "src/pkg/test_x.py",
                   "conftest.py", "pkg/conftest.py", ".coverage", "test-results/report.xml",
                   "htmlcov/index.html", "web/cart.test.ts"]
        changed = [{"path": p, "change": "수정"} for p in allowed + ["src/app.py", "templates/cart.html"]]
        self.assertEqual(
            roundtable.role_scope_violations("antigravity", changed, state),
            ["src/app.py", "templates/cart.html"],
        )

    def test_a_failed_planning_turn_cannot_be_skipped(self):
        state = roundtable.new_state()
        state.update(mode="coding", coding_stage="planning", step_index=0, enabled_agents=ALL)
        failed_plan = [{"agent": "claude", "phase": roundtable.CONFIRM_PHASE,
                        "meta": {"ok": False, "failure_kind": "cli"}}]
        self.assertFalse(roundtable.can_continue_after_failed_step(state, failed_plan))

    def test_finished_old_sessions_still_get_the_fixed_roles(self):
        state = roundtable.normalize_state({"mode": "coding", "coding_stage": "iteration", "finished": True,
                                            "agent_roles": {"claude": "frontend"}, "enabled_agents": ALL})
        self.assertEqual(state["agent_roles"], roundtable.pipeline_agent_roles())

    def test_markdown_wrapped_verdicts_are_read(self):
        self.assertEqual(roundtable.extract_review_verdict("좋음\n**REVIEW: PASS**")[1], "pass")
        self.assertEqual(roundtable.extract_review_verdict("고칠 것\n`REVIEW: CHANGES`")[1], "changes")


class ExecutionPlanTests(unittest.TestCase):
    def test_coding_plan_counts_one_plan_call_plus_three_per_cycle(self):
        plan = roundtable.mode_execution_plan("coding", ALL)
        cycles = roundtable.DEFAULT_CODING_LIMITS["max_cycles"]
        self.assertEqual((plan["min_calls"], plan["max_calls"]), (1 + 3, 1 + 3 * cycles))
        self.assertFalse(plan["role_negotiation"])
        self.assertEqual(plan["pipeline_roles"], "Claude Code=구현, Antigravity=테스트, Codex=검토")

    def test_other_modes_have_no_pipeline_roles(self):
        self.assertEqual(roundtable.mode_execution_plan("quick", ALL)["pipeline_roles"], "")


if __name__ == "__main__":
    unittest.main()
