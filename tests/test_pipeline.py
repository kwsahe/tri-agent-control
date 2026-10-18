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
