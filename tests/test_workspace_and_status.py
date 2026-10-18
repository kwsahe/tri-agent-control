"""작업공간 fail-closed, 체크포인트 소유권, 실행 상태 전이, 토큰/비용 계산.

모든 파일 조작은 TemporaryDirectory 안에서만 한다.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isolation  # noqa: F401  (실제 데이터 폴더 격리 — 반드시 roundtable보다 먼저)
import roundtable
from triagent.domain import runstatus, usage as usage_domain, workflow
from triagent.domain.workspace import (
    WorkspaceStatus,
    parse_project_path_file,
    resolve_workspace,
    workspace_matches,
)


# ──────────────────────────────────────────────────────────────
# 3. 작업공간 오작동 방지
# ──────────────────────────────────────────────────────────────
class WorkspaceResolutionTests(unittest.TestCase):
    def test_valid_directory_resolves(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = resolve_workspace(temp_dir)
            self.assertTrue(result.ok)
            self.assertEqual(result.path, Path(temp_dir).resolve())

    def test_missing_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = str(Path(temp_dir) / "nope" / "gone")
            result = resolve_workspace(missing)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, WorkspaceStatus.NOT_FOUND)
        # 대체 경로를 만들어내지 않는다.
        self.assertIsNone(result.path)
        # 원래 저장된 경로를 UI에 그대로 보여줄 수 있어야 한다.
        self.assertEqual(result.configured, missing)

    def test_file_given_as_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "not-a-dir.txt"
            target.write_text("x", encoding="utf-8")
            result = resolve_workspace(str(target))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, WorkspaceStatus.NOT_A_DIRECTORY)
        self.assertIsNone(result.path)

    def test_relative_path_is_rejected(self):
        result = resolve_workspace("./some/relative/path")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, WorkspaceStatus.RELATIVE)

    def test_empty_configuration_fails_closed(self):
        result = resolve_workspace("")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, WorkspaceStatus.MISSING)

    def test_project_path_file_parsing_ignores_comments(self):
        self.assertEqual(
            parse_project_path_file("# comment\n\n  C:\\work\\app  \nsecond"),
            "C:\\work\\app",
        )
        self.assertEqual(parse_project_path_file("# only comments\n"), "")

    def test_workspace_matches_compares_resolved_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            same = Path(temp_dir)
            self.assertTrue(workspace_matches(str(same), str(same)))
            self.assertTrue(workspace_matches(str(same), str(same) + "\\."))
            self.assertFalse(workspace_matches(str(same), str(same / "sub")))
        # 기록이 없는 구버전 세션은 판단하지 않는다.
        self.assertTrue(workspace_matches("", "anything"))


class WorkspaceFailClosedIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.original_state = roundtable.STATE
        self.original_control = dict(roundtable.CONTROL)
        roundtable.STATE = roundtable.new_state()
        roundtable._WORKSPACE_WARNING_STATE["signature"] = ""
        roundtable._WORKSPACE_WARNING_STATE["seen"] = set()

    def tearDown(self):
        roundtable.STATE = self.original_state
        roundtable.CONTROL.clear()
        roundtable.CONTROL.update(self.original_control)

    def test_blocked_workspace_stops_reads_and_writes(self):
        with patch.object(roundtable, "read_configured_project_path",
                          return_value="C:\\definitely\\not\\here\\at\\all"):
            for cli_mode in ("discussion", "coding"):
                with self.subTest(cli_mode=cli_mode):
                    with self.assertRaises(roundtable.WorkspaceUnavailable):
                        roundtable.cli_working_directory(cli_mode)

    def test_chat_only_mode_still_works_when_workspace_is_blocked(self):
        """읽지 않고 토론하는 모드는 작업 폴더가 없어도 막히지 않아야 한다."""
        roundtable.STATE["discussion_project_access"] = "none"
        with patch.object(roundtable, "read_configured_project_path", return_value=""):
            self.assertEqual(
                roundtable.cli_working_directory("discussion"),
                roundtable.NO_PROJECT_DIR,
            )

    def test_coding_mode_switch_is_blocked_without_valid_workspace(self):
        with patch.object(roundtable, "read_configured_project_path",
                          return_value="C:\\definitely\\not\\here"):
            result = roundtable.switch_mode("coding")
        self.assertIn("error", result)

    def test_coding_mode_switch_works_after_choosing_a_valid_folder(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            roundtable, "read_configured_project_path", return_value=temp_dir
        ), patch.object(roundtable, "save_state"), patch.object(
            roundtable, "add_runtime_event"
        ), patch.object(roundtable, "start_worker_if_needed"):
            result = roundtable.switch_mode("coding")
        self.assertTrue(result.get("success"), result)

    def test_warning_is_logged_once_not_on_every_poll(self):
        printed = []
        with patch.object(roundtable, "read_configured_project_path",
                          return_value="C:\\missing\\folder"), patch(
            "builtins.print", side_effect=lambda *a, **k: printed.append(a)
        ):
            for _ in range(25):
                roundtable.resolve_project_workspace()
                roundtable.resolve_project_workspace(require_write=True)
        blocked_lines = [args for args in printed if args and "작업 폴더를 사용할 수 없습니다" in str(args[0])]
        self.assertEqual(len(blocked_lines), 1, printed)

    def test_activating_a_session_from_another_workspace_needs_confirmation(self):
        with tempfile.TemporaryDirectory() as session_dir, \
             tempfile.TemporaryDirectory() as current_dir:
            source = roundtable.new_state()
            source.update(
                id="20260101_000000_000001",
                topic="이어서 작업",
                mode="discussion",
                workspace_path=str(Path(session_dir).resolve()),
                workspace_access="read",
                finished=False,
                messages=[],
            )
            with patch.object(roundtable, "load_session", return_value=source), \
                 patch.object(roundtable, "read_configured_project_path", return_value=current_dir), \
                 patch.object(roundtable, "save_state"), \
                 patch.object(roundtable, "save_project_path") as save_project_path, \
                 patch.object(roundtable, "add_runtime_event"):
                blocked = roundtable.activate_session("20260101_000000_000001")

                self.assertIn("error", blocked)
                self.assertTrue(blocked["needs_workspace_confirm"])
                save_project_path.assert_not_called()

                # 사용자가 확인하면 진행된다.
                allowed = roundtable.activate_session(
                    "20260101_000000_000001", confirm_workspace_switch=True
                )
                self.assertTrue(allowed.get("success"), allowed)
                save_project_path.assert_called_once()

    def test_activation_is_blocked_when_the_session_folder_is_gone(self):
        source = roundtable.new_state()
        source.update(
            id="20260101_000000_000002",
            topic="사라진 폴더",
            workspace_path="C:\\gone\\forever",
            workspace_access="read",
            messages=[],
        )
        with patch.object(roundtable, "load_session", return_value=source), \
             patch.object(roundtable, "read_configured_project_path", return_value="C:\\gone\\forever"):
            result = roundtable.activate_session("20260101_000000_000002", True)
        self.assertIn("error", result)
        self.assertIn("찾을 수 없습니다", result["error"])

    def test_moved_session_folder_continues_in_current_folder_after_confirmation(self):
        """프로젝트 폴더를 옮긴 뒤에도 옛 세션을 확인 후 현재 폴더에서 이어갈 수 있다."""
        with tempfile.TemporaryDirectory() as current_dir:
            source = roundtable.new_state()
            source.update(
                id="20260101_000000_000003",
                topic="옮겨진 프로젝트",
                mode="discussion",
                workspace_path="C:\\gone\\forever",
                workspace_access="read",
                finished=False,
                messages=[],
            )
            with patch.object(roundtable, "load_session", return_value=source), \
                 patch.object(roundtable, "read_configured_project_path", return_value=current_dir), \
                 patch.object(roundtable, "save_state"), \
                 patch.object(roundtable, "save_project_path") as save_project_path, \
                 patch.object(roundtable, "add_runtime_event"):
                blocked = roundtable.activate_session("20260101_000000_000003")
                self.assertTrue(blocked.get("needs_workspace_confirm"), blocked)
                self.assertNotEqual(roundtable.STATE.get("id"), "20260101_000000_000003")

                allowed = roundtable.activate_session(
                    "20260101_000000_000003", confirm_workspace_switch=True
                )
                self.assertTrue(allowed.get("success"), allowed)
                self.assertEqual(
                    roundtable.STATE["workspace_path"], str(Path(current_dir).resolve())
                )
                # 현재 선택 폴더를 그대로 쓰므로 PROJECT_PATH.txt를 바꾸지 않는다.
                save_project_path.assert_not_called()


# ──────────────────────────────────────────────────────────────
# 2D. 체크포인트 소유권
# ──────────────────────────────────────────────────────────────
class CheckpointOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.original_state = roundtable.STATE
        roundtable.STATE = roundtable.new_state()
        roundtable.STATE["id"] = "20260101_000000_000010"
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.memory = root / "memory"
        self.workspace = root / "project"
        self.other_workspace = root / "other-project"
        self.memory.mkdir()
        self.workspace.mkdir()
        self.other_workspace.mkdir()
        self.target = self.workspace / "sample.txt"
        self.target.write_text("before\n", encoding="utf-8")
        before = roundtable.capture_project_texts(self.workspace)
        self.target.write_text("after\n", encoding="utf-8")
        self.diff = roundtable.build_turn_diff(
            self.workspace, before, [{"path": "sample.txt", "change": "수정"}]
        )
        self.checkpoint = self.memory / "turn.patch"
        self.checkpoint.write_text(self.diff, encoding="utf-8", newline="")

    def tearDown(self):
        roundtable.STATE = self.original_state
        self._tmp.cleanup()

    def write_owner(self, session_id, workspace):
        (self.memory / "turn.json").write_text(
            json.dumps({"session_id": session_id, "workspace_path": str(workspace)}),
            encoding="utf-8",
        )

    def rollback(self):
        with patch.object(roundtable, "MEMORY_DIR", self.memory), \
             patch.object(roundtable, "read_configured_project_path", return_value=str(self.workspace)), \
             patch.object(roundtable, "add_runtime_event"):
            return roundtable.rollback_checkpoint_files(str(self.checkpoint), ["sample.txt"])

    def test_own_checkpoint_applies(self):
        self.write_owner(roundtable.STATE["id"], self.workspace.resolve())
        result = self.rollback()
        self.assertTrue(result.get("success"), result)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "before\n")

    def test_checkpoint_from_another_session_is_blocked(self):
        self.write_owner("20260101_000000_000099", self.workspace.resolve())
        result = self.rollback()
        self.assertIn("error", result)
        self.assertTrue(result["ownership_blocked"])
        self.assertIn("다른 세션", result["error"])
        # 파일이 되돌려지지 않았다.
        self.assertEqual(self.target.read_text(encoding="utf-8"), "after\n")

    def test_checkpoint_from_another_project_is_blocked(self):
        self.write_owner(roundtable.STATE["id"], self.other_workspace.resolve())
        result = self.rollback()
        self.assertIn("error", result)
        self.assertIn("다른 작업공간", result["error"])
        self.assertEqual(self.target.read_text(encoding="utf-8"), "after\n")

    def test_legacy_checkpoint_without_ownership_is_not_applied(self):
        # 소유권 파일 없음 = 레거시.
        result = self.rollback()
        self.assertIn("error", result)
        self.assertIn("레거시", result["error"])
        self.assertEqual(self.target.read_text(encoding="utf-8"), "after\n")

    def test_paths_outside_the_workspace_are_dropped(self):
        self.write_owner(roundtable.STATE["id"], self.workspace.resolve())
        with patch.object(roundtable, "MEMORY_DIR", self.memory), \
             patch.object(roundtable, "read_configured_project_path", return_value=str(self.workspace)), \
             patch.object(roundtable, "add_runtime_event"):
            for bad in ("../escape.txt", "..\\escape.txt", "C:\\Windows\\win.ini", "/etc/passwd"):
                with self.subTest(bad=bad):
                    result = roundtable.rollback_checkpoint_files(str(self.checkpoint), [bad])
                    self.assertIn("error", result)

    def test_checkpoint_outside_memory_dir_is_rejected(self):
        outside = Path(self._tmp.name) / "outside.patch"
        outside.write_text(self.diff, encoding="utf-8", newline="")
        with patch.object(roundtable, "MEMORY_DIR", self.memory), \
             patch.object(roundtable, "read_configured_project_path", return_value=str(self.workspace)):
            result = roundtable.rollback_checkpoint_files(str(outside), ["sample.txt"])
        self.assertIn("error", result)

    def test_saving_a_checkpoint_records_owner_metadata(self):
        with patch.object(roundtable, "MEMORY_DIR", self.memory):
            path = roundtable.save_turn_checkpoint(
                "20260101_000000_000010", 3, "codex", self.diff, workspace=self.workspace
            )
        meta = json.loads(Path(path).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(meta["session_id"], "20260101_000000_000010")
        self.assertEqual(meta["workspace_path"], str(self.workspace))


# ──────────────────────────────────────────────────────────────
# 6. 중단 / 실패 / 시간초과 분리
# ──────────────────────────────────────────────────────────────
class RunStatusTests(unittest.TestCase):
    def test_user_cancel_is_not_a_model_failure(self):
        outcome = runstatus.classify_turn(
            "(Antigravity 응답 없음 — 종료 코드 1)",
            cancelled=True,
            returncode=1,
            failure_marker=True,
        )
        self.assertEqual(outcome.status, runstatus.RunStatus.USER_CANCELLED)
        self.assertFalse(outcome.is_failure)
        self.assertEqual(outcome.label, "사용자 중단")

    def test_cancel_wins_over_timeout_and_failure(self):
        outcome = runstatus.classify_turn(
            "부분 출력", cancelled=True, timed_out=True, returncode=1
        )
        self.assertEqual(outcome.status, runstatus.RunStatus.USER_CANCELLED)

    def test_partial_output_before_cancel_is_preserved_and_flagged(self):
        outcome = runstatus.classify_turn(
            "여기까지 작성했습니다", cancelled=True, had_partial_output=True
        )
        self.assertTrue(outcome.partial)
        self.assertIn("부분 출력", outcome.detail)
        self.assertEqual(outcome.text, "여기까지 작성했습니다")

    def test_timeout_and_cli_failure_are_distinct(self):
        self.assertEqual(
            runstatus.classify_turn("", timed_out=True).status,
            runstatus.RunStatus.TIMED_OUT,
        )
        self.assertEqual(
            runstatus.classify_turn("", returncode=2).status,
            runstatus.RunStatus.CLI_FAILED,
        )
        self.assertEqual(
            runstatus.classify_turn("정상 답변", returncode=0).status,
            runstatus.RunStatus.COMPLETED,
        )

    def test_retry_cta_is_only_offered_for_real_failures(self):
        self.assertTrue(runstatus.allows_failure_retry(runstatus.RunStatus.CLI_FAILED))
        self.assertTrue(runstatus.allows_failure_retry(runstatus.RunStatus.TIMED_OUT))
        self.assertFalse(runstatus.allows_failure_retry(runstatus.RunStatus.USER_CANCELLED))
        self.assertFalse(runstatus.allows_failure_retry(runstatus.RunStatus.COMPLETED))

    def test_session_list_status_matches_the_header(self):
        cases = [
            (dict(is_active_session=True, has_topic=False, finished=False), "준비"),
            (dict(is_active_session=True, has_topic=True, finished=True), "완료"),
            (dict(is_active_session=True, has_topic=True, finished=False,
                  run_status=runstatus.RunStatus.USER_CANCELLED), "사용자 중단"),
            (dict(is_active_session=True, has_topic=True, finished=False,
                  run_status=runstatus.RunStatus.RUNNING), "진행 중"),
            (dict(is_active_session=True, has_topic=True, finished=False,
                  run_status=runstatus.RunStatus.PAUSED), "일시정지"),
            (dict(is_active_session=False, has_topic=True, finished=False,
                  run_status=runstatus.RunStatus.RUNNING), "보류"),
            (dict(is_active_session=True, has_topic=True, finished=False,
                  run_status=runstatus.RunStatus.CLI_FAILED), "CLI 실패"),
        ]
        for kwargs, expected in cases:
            with self.subTest(**kwargs):
                self.assertEqual(runstatus.session_list_status(**kwargs), expected)

    def test_status_is_not_conveyed_by_colour_alone(self):
        for status in runstatus.RUN_STATUSES:
            self.assertTrue(runstatus.status_label(status))
            self.assertTrue(runstatus.status_glyph(status))


class RunStatusPersistenceTests(unittest.TestCase):
    def test_cancelled_state_does_not_come_back_as_running(self):
        state = roundtable.new_state()
        state["topic"] = "작업"
        state["run_status"] = runstatus.RunStatus.USER_CANCELLED
        state["workflow_status"] = workflow.CANCELLED
        restored = roundtable.normalize_state(json.loads(json.dumps(state)))
        self.assertEqual(restored["run_status"], runstatus.RunStatus.USER_CANCELLED)

    def test_stale_running_status_becomes_paused_after_restart(self):
        state = roundtable.new_state()
        state["topic"] = "작업"
        state["run_status"] = runstatus.RunStatus.RUNNING
        restored = roundtable.normalize_state(json.loads(json.dumps(state)))
        self.assertEqual(restored["run_status"], runstatus.RunStatus.PAUSED)

    def test_missing_run_status_is_derived_from_workflow_status(self):
        state = roundtable.new_state()
        state["topic"] = "작업"
        state["workflow_status"] = workflow.CANCELLED
        state.pop("run_status", None)
        restored = roundtable.normalize_state(json.loads(json.dumps(state)))
        self.assertEqual(restored["run_status"], runstatus.RunStatus.USER_CANCELLED)


# ──────────────────────────────────────────────────────────────
# 10. 토큰 / 비용
# ──────────────────────────────────────────────────────────────
class TokenAccountingTests(unittest.TestCase):
    def test_cumulative_tokens_are_not_treated_as_context(self):
        """Codex처럼 턴 누적 usage만 주는 경우 461% 같은 값이 나오면 안 된다."""
        messages = [
            {"agent": "codex", "meta": {"actual_usage": {"input_tokens": 400_000,
                                                         "output_tokens": 8_000}}},
        ]
        context = usage_domain.context_usage(messages, "codex", 258_400)
        # 근거가 호출 단위가 아니므로 measured가 아니다.
        self.assertNotEqual(context.source, "measured")
        cumulative = usage_domain.cumulative_tokens(messages, "codex")
        self.assertEqual(cumulative, 408_000)

    def test_claude_iterations_give_a_measured_last_call_context(self):
        messages = [
            {"agent": "claude", "meta": {"actual_usage": {
                "input_tokens": 999_999,
                "iterations": [
                    {"input_tokens": 100, "output_tokens": 10},
                    {"input_tokens": 5_000, "cache_read_input_tokens": 1_000,
                     "output_tokens": 200},
                ],
            }}},
        ]
        context = usage_domain.context_usage(messages, "claude", 128_000)
        self.assertEqual(context.source, "measured")
        self.assertEqual(context.used_tokens, 6_200)
        self.assertAlmostEqual(context.percent, round(6_200 / 128_000 * 100, 1))

    def test_explicit_context_snapshot_wins(self):
        messages = [
            {"agent": "antigravity", "meta": {
                "context_usage": {"input_tokens": 2_000},
                "actual_usage": {"input_tokens": 900_000},
            }},
        ]
        context = usage_domain.context_usage(messages, "antigravity", 1_048_576)
        self.assertEqual(context.source, "measured")
        self.assertEqual(context.used_tokens, 2_000)

    def test_unknown_context_reports_unknown_not_zero_percent_of_something(self):
        messages = [{"agent": "codex", "meta": {}}]
        context = usage_domain.context_usage(messages, "codex", 258_400)
        self.assertEqual(context.source, "unknown")
        self.assertFalse(context.known)
        self.assertEqual(context.label, "알 수 없음")
        self.assertEqual(context.percent, 0.0)

    def test_estimates_are_labelled(self):
        messages = [{"agent": "codex", "meta": {"est_tokens": 1_200}}]
        context = usage_domain.context_usage(messages, "codex", 258_400)
        self.assertEqual(context.source, "estimated")
        self.assertTrue(context.label.startswith("~"))
        self.assertTrue(context.as_dict()["estimated"])

    def test_overflow_is_reported_not_hidden(self):
        messages = [{"agent": "claude", "meta": {"actual_usage": {
            "iterations": [{"input_tokens": 600_000}],
        }}}]
        context = usage_domain.context_usage(messages, "claude", 128_000)
        self.assertTrue(context.overflow)
        self.assertGreater(context.percent, 100)   # 실제 값은 그대로 보여준다
        self.assertEqual(context.bar_percent, 100) # 막대만 잘린다

    def test_ten_million_tokens_does_not_break_formatting(self):
        messages = [{"agent": "codex", "meta": {"actual_usage": {
            "input_tokens": 10_000_000, "output_tokens": 500_000,
        }}}]
        self.assertEqual(
            usage_domain.cumulative_tokens(messages, "codex"), 10_500_000
        )
        context = usage_domain.context_usage(messages, "codex", 258_400)
        self.assertGreaterEqual(context.percent, 0.0)
        self.assertLessEqual(context.bar_percent, 100.0)

    def test_each_cli_shape_is_handled(self):
        shapes = {
            # Codex: turn 누적, cached_input_tokens는 input_tokens에 포함
            "codex": {"input_tokens": 85_781, "cached_input_tokens": 50_176,
                      "output_tokens": 1_045},
            # Claude: cache_* 가 별도 항목
            "claude": {"input_tokens": 12, "cache_creation_input_tokens": 4_000,
                       "cache_read_input_tokens": 20_000, "output_tokens": 300},
            # Antigravity: 최소 정보
            "antigravity": {"input_tokens": 3_000, "output_tokens": 120},
        }
        expected = {"codex": 86_826, "claude": 24_312, "antigravity": 3_120}
        for agent, usage in shapes.items():
            with self.subTest(agent=agent):
                messages = [{"agent": agent, "meta": {"actual_usage": usage}}]
                self.assertEqual(
                    usage_domain.cumulative_tokens(messages, agent), expected[agent]
                )

    def test_reported_and_estimated_costs_are_not_summed(self):
        messages = [
            {"agent": "claude", "meta": {"actual_cost_usd": 0.12}},
            {"agent": "codex", "meta": {"estimated_cost_usd": 0.34}},
            {"agent": "codex", "meta": {"estimated_cost_usd": 0.06}},
        ]
        cost = usage_domain.cost_breakdown(messages)
        self.assertAlmostEqual(cost.reported_usd, 0.12)
        self.assertAlmostEqual(cost.estimated_usd, 0.40)
        self.assertEqual(cost.reported_turns, 1)
        self.assertEqual(cost.estimated_turns, 2)
        self.assertIn("추정", cost.basis)
        self.assertEqual(cost.as_dict()["currency"], "USD")

    def test_agent_context_summary_exposes_source_and_cumulative(self):
        messages = [{"agent": "codex", "meta": {"actual_usage": {"input_tokens": 500_000}}}]
        summary = roundtable.agent_context_summary(messages)
        row = summary["codex"]
        self.assertIn("source", row)
        self.assertIn("session_cumulative_tokens", row)
        self.assertEqual(row["session_cumulative_tokens"], 500_000)


if __name__ == "__main__":
    unittest.main()
