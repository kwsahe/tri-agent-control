"""11. 안정성·개인정보: 저장 실패, 원자적 쓰기, 출력 잘림 표시, 종료 정리."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import isolation  # noqa: F401  (실제 데이터 폴더 격리 — 반드시 roundtable보다 먼저)
import roundtable
from triagent.domain import runstatus


class AtomicWriteTests(unittest.TestCase):
    def test_write_is_atomic_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "state.json"
            roundtable.atomic_write_json(target, {"messages": [1, 2, 3]})
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")), {"messages": [1, 2, 3]}
            )
            self.assertEqual(
                [p.name for p in Path(temp_dir).iterdir()], ["state.json"]
            )

    def test_existing_file_is_untouched_when_the_write_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "state.json"
            target.write_text('{"keep": true}', encoding="utf-8")
            with patch.object(Path, "write_text", side_effect=OSError(28, "No space left on device")):
                with self.assertRaises(roundtable.StateSaveError):
                    roundtable.atomic_write_json(target, {"new": 1})
            # 원본이 잘리거나 지워지지 않았다.
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"keep": True})

    def test_disk_full_is_reported_to_the_user_not_swallowed(self):
        roundtable.LAST_SAVE_ERROR["message"] = ""
        state = roundtable.new_state()
        with patch.object(
            roundtable, "atomic_write_json",
            side_effect=roundtable.StateSaveError("roundtable_state.json 저장 실패: No space left on device"),
        ), patch("builtins.print"):
            roundtable.save_state(state)
        message = roundtable.LAST_SAVE_ERROR["message"]
        self.assertIn("저장 실패", message)
        self.assertIn("디스크", message)
        roundtable.LAST_SAVE_ERROR["message"] = ""

    def test_save_error_appears_in_the_state_payload(self):
        roundtable.LAST_SAVE_ERROR["message"] = "테스트 저장 실패"
        try:
            payload = roundtable.state_json_payload()
            self.assertEqual(payload["save_error"], "테스트 저장 실패")
        finally:
            roundtable.LAST_SAVE_ERROR["message"] = ""

    def test_recovery_clears_the_error(self):
        roundtable.LAST_SAVE_ERROR["message"] = "이전 실패"
        with patch.object(roundtable, "atomic_write_json"), patch("builtins.print"):
            roundtable.save_state(roundtable.new_state())
        self.assertEqual(roundtable.LAST_SAVE_ERROR["message"], "")


class TruncationDisclosureTests(unittest.TestCase):
    def test_clipping_reports_that_it_clipped(self):
        text, truncated = roundtable.clip_agent_output("가" * 5000, max_chars=100)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text), 200)

    def test_short_output_is_not_marked_truncated(self):
        _text, truncated = roundtable.clip_agent_output("짧은 답변", max_chars=2000)
        self.assertFalse(truncated)

    def test_bubble_shows_a_truncation_notice(self):
        message = {
            "agent": "codex", "phase": "검토", "time": "12:00:00", "text": "요약된 답변",
            "meta": {"elapsed": 1.2, "est_tokens": 10, "prompt_chars": 5,
                     "output_chars": 5, "cli_mode": "discussion",
                     "output_truncated": True},
        }
        html = roundtable.bubble_html(message)
        self.assertIn("표시 축약됨", html)
        # 전체 원문 보관 위치를 안내한다.
        self.assertIn("full.md", html)

    def test_bubble_survives_missing_meta_keys(self):
        """세션 파일이 오래되어 meta 키가 빠져 있어도 렌더링이 죽으면 안 된다."""
        message = {"agent": "codex", "phase": "검토", "time": "12:00:00",
                   "text": "내용", "meta": {"cli_mode": "discussion"}}
        html = roundtable.bubble_html(message)
        self.assertIn("내용", html)

    def test_bubble_shows_cancel_and_partial_markers(self):
        message = {
            "agent": "antigravity", "phase": "구현", "time": "12:00:00",
            "text": "중단 전까지 작성한 내용",
            "meta": {"elapsed": 3.0, "est_tokens": 10, "prompt_chars": 5,
                     "output_chars": 5, "cli_mode": "coding",
                     "run_status": runstatus.RunStatus.USER_CANCELLED,
                     "partial_output": True},
        }
        html = roundtable.bubble_html(message)
        self.assertIn("사용자 중단", html)
        self.assertIn("부분 출력", html)
        # 실패로 표시하지 않는다.
        self.assertNotIn("CLI 실패", html)


class ShutdownTests(unittest.TestCase):
    def test_shutdown_kills_child_processes_and_clears_running_state(self):
        original = roundtable.STATE
        roundtable.STATE = roundtable.new_state()
        roundtable.STATE["active_agent"] = "codex"
        roundtable.STATE["active_phase"] = "구현"
        server = MagicMock()
        try:
            with patch.object(roundtable, "cancel_active_cli_processes",
                              return_value=["Codex"]) as cancel, \
                 patch.object(roundtable, "save_state"), \
                 patch("builtins.print"):
                roundtable.shutdown_cleanly(server)
            cancel.assert_called_once()
            server.shutdown.assert_called_once()
            self.assertIsNone(roundtable.STATE["active_agent"])
            # 다음 실행에서 "진행 중"으로 되살아나지 않는다.
            self.assertEqual(
                roundtable.STATE["run_status"], runstatus.RunStatus.USER_CANCELLED
            )
        finally:
            roundtable.STATE = original
            roundtable.CONTROL["stopped"] = False
            roundtable.CONTROL["user_cancelled"] = False

    def test_shutdown_does_not_raise_when_saving_fails(self):
        original = roundtable.STATE
        roundtable.STATE = roundtable.new_state()
        try:
            with patch.object(roundtable, "cancel_active_cli_processes", return_value=[]), \
                 patch.object(roundtable, "save_state", side_effect=OSError("disk gone")), \
                 patch("builtins.print"):
                roundtable.shutdown_cleanly(MagicMock())   # 예외가 새어 나오면 실패
        finally:
            roundtable.STATE = original
            roundtable.CONTROL["stopped"] = False
            roundtable.CONTROL["user_cancelled"] = False


class PrivacyTests(unittest.TestCase):
    def test_dashboard_makes_no_external_requests(self):
        """로컬 대시보드가 인터넷(구글 폰트 등)으로 나가지 않아야 한다."""
        for name in ("static/dashboard.css", "dashboard_template.html", "static/dashboard.js"):
            content = (roundtable.ROOT / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertNotIn("fonts.googleapis.com", content)
                self.assertNotIn("fonts.gstatic.com", content)

    def test_no_cors_wildcard_is_emitted(self):
        source = (roundtable.ROOT / "roundtable.py").read_text(encoding="utf-8")
        self.assertNotIn("Access-Control-Allow-Origin", source)


class QuickModeTests(unittest.TestCase):
    """7. 빠른 질문이 고정 다단계 토론을 실행하지 않는다."""

    def test_quick_mode_calls_one_model_once(self):
        steps = roundtable.steps_for_mode("quick", ["codex", "claude", "antigravity"])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0][0], "codex")
        # 역할 선언·강점 분석 단계가 없다.
        phases = [step[1] for step in steps]
        self.assertNotIn("역할 선언", phases)
        self.assertNotIn("강점", " ".join(phases))

    def test_discussion_and_coding_keep_their_multi_step_flows(self):
        discussion = roundtable.steps_for_mode("discussion", list(roundtable.AGENT_ORDER))
        coding = roundtable.steps_for_mode("coding", list(roundtable.AGENT_ORDER))
        self.assertGreater(len(discussion), 3)
        # 코딩은 토론 없이 계획 1회 후 고정 사이클로 간다.
        self.assertEqual(len(coding), 1)
        self.assertEqual(len(roundtable.goal_coding_steps(list(roundtable.AGENT_ORDER))), 3)

    def test_execution_plan_shows_call_counts_per_mode(self):
        quick = roundtable.mode_execution_plan("quick", list(roundtable.AGENT_ORDER))
        discussion = roundtable.mode_execution_plan("discussion", list(roundtable.AGENT_ORDER))
        coding = roundtable.mode_execution_plan("coding", list(roundtable.AGENT_ORDER))

        self.assertEqual((quick["min_calls"], quick["max_calls"]), (1, 1))
        self.assertEqual(quick["agent_count"], 1)
        self.assertFalse(quick["can_write"])
        self.assertFalse(quick["auto_validation"])
        self.assertFalse(quick["role_negotiation"])

        self.assertGreater(discussion["min_calls"], quick["min_calls"])
        # 토론이 없어져 코딩은 계획 1회 + 사이클당 3회다.
        self.assertEqual(coding["max_calls"], 1 + 3 * roundtable.DEFAULT_CODING_LIMITS["max_cycles"])
        self.assertFalse(coding["role_negotiation"])
        self.assertTrue(coding["auto_validation"])
        self.assertTrue(coding["long_running_warning"])

    def test_plan_reports_a_blocked_workspace(self):
        with patch.object(roundtable, "read_configured_project_path",
                          return_value="C:\\nope\\missing"):
            plan = roundtable.mode_execution_plan("coding", ["codex"])
        self.assertFalse(plan["workspace_ok"])
        self.assertTrue(plan["workspace_message"])

    def test_quick_mode_never_grants_write_access(self):
        original = roundtable.STATE
        roundtable.STATE = roundtable.new_state()
        try:
            roundtable.STATE["mode"] = "quick"
            roundtable.STATE["discussion_project_access"] = "write"
            roundtable.STATE["workspace_access"] = "write"
            # coding cli_mode로 요청해도 쓰기를 주지 않는다.
            self.assertNotEqual(roundtable.turn_project_access("coding"), "write")
        finally:
            roundtable.STATE = original


if __name__ == "__main__":
    unittest.main()
