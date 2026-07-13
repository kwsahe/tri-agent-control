import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import roundtable


class RoundtableTests(unittest.TestCase):
    def setUp(self):
        self.original_state = roundtable.STATE
        self.original_control = dict(roundtable.CONTROL)
        roundtable.STATE = roundtable.new_state()

    def tearDown(self):
        roundtable.STATE = self.original_state
        roundtable.CONTROL.clear()
        roundtable.CONTROL.update(self.original_control)

    def test_codex_prompt_is_sent_through_stdin(self):
        result = subprocess.CompletedProcess([], 0, "정상 응답", "")
        with patch.object(roundtable, "run_cli", return_value=result) as run_cli:
            response = roundtable.ask_codex("가" * 12000, "discussion")

        self.assertEqual(response, "정상 응답")
        args = run_cli.call_args.args[2]
        kwargs = run_cli.call_args.kwargs
        self.assertEqual(args[-1], "-")
        self.assertEqual(kwargs["input_text"], "가" * 12000)

    def test_selected_codex_model_and_effort_are_forwarded(self):
        roundtable.STATE["agent_settings"]["codex"] = {
            "model": "gpt-5.6-codex",
            "effort": "medium",
        }
        result = subprocess.CompletedProcess([], 0, "정상 응답", "")
        with patch.object(roundtable, "run_cli", return_value=result) as run_cli:
            roundtable.ask_codex("테스트")
        args = run_cli.call_args.args[2]
        self.assertIn("gpt-5.6-codex", args)
        self.assertIn('model_reasoning_effort="medium"', args)

    def test_selected_claude_model_and_effort_are_forwarded(self):
        roundtable.STATE["agent_settings"]["claude"] = {
            "model": "opus",
            "effort": "high",
        }
        result = subprocess.CompletedProcess([], 0, "정상 응답", "")
        with patch.object(roundtable, "run_cli", return_value=result) as run_cli:
            roundtable.ask_claude("테스트")
        args = run_cli.call_args.args[2]
        self.assertEqual(args[args.index("--model") + 1], "opus")
        self.assertEqual(args[args.index("--effort") + 1], "high")

    def test_selected_antigravity_preset_is_forwarded(self):
        preset = "Gemini 3.5 Flash (High)"
        roundtable.STATE["agent_settings"]["antigravity"] = {
            "model": preset,
            "effort": "",
        }
        result = subprocess.CompletedProcess([], 0, "정상 응답", "")
        with patch.object(roundtable, "run_cli", return_value=result) as run_cli:
            roundtable.ask_antigravity("테스트")
        args = run_cli.call_args.args[2]
        self.assertEqual(args[args.index("--model") + 1], preset)

    def test_unknown_model_settings_fall_back_to_cli_defaults(self):
        settings = roundtable.normalize_agent_settings({
            "codex": {"model": "unknown", "effort": "extreme"},
        })
        self.assertEqual(settings["codex"], {"model": "", "effort": ""})

    def test_cli_output_falls_back_to_windows_korean_encoding(self):
        message = "명령줄이 너무 깁니다."
        self.assertEqual(roundtable.decode_cli_output(message.encode("cp949")), message)

    def test_korean_token_estimate_uses_utf8_size(self):
        self.assertEqual(roundtable.estimate_tokens("가" * 4), 3)

    def test_recent_transcript_respects_character_budget(self):
        messages = [
            {"agent": "codex", "phase": "테스트", "text": "가" * 4000},
            {"agent": "claude", "phase": "테스트", "text": "나" * 4000},
        ]
        with patch.object(roundtable, "TRANSCRIPT_MAX_CHARS", 500):
            transcript = roundtable.build_transcript(messages, window=2)
        self.assertLessEqual(len(transcript), 500)
        self.assertIn("일부 생략", transcript)

    def test_approval_token_is_only_recognized_on_the_final_line(self):
        visible, requested = roundtable.extract_approval_token("변경 전에 확인이 필요합니다.\nAPPROVE")
        self.assertTrue(requested)
        self.assertEqual(visible, "변경 전에 확인이 필요합니다.")

        visible, requested = roundtable.extract_approval_token("APPROVE\n추가 설명")
        self.assertFalse(requested)
        self.assertEqual(visible, "APPROVE\n추가 설명")

    def test_unresolved_approval_is_restored_from_saved_messages(self):
        messages = [
            {
                "agent": "codex",
                "phase": "설계 확인",
                "text": "승인이 필요합니다.",
                "meta": {"approval_requested": True},
            },
            {"agent": "user", "phase": "사용자 개입 · 질문", "text": "범위를 줄여줘."},
        ]
        self.assertEqual(
            roundtable.unresolved_approval_requesters(messages),
            ["Codex · 설계 확인"],
        )
        messages.append({"agent": "user", "phase": "승인", "text": "승인"})
        self.assertEqual(roundtable.unresolved_approval_requesters(messages), [])

    def test_composed_prompt_has_a_hard_character_limit(self):
        prompt = roundtable.compose_agent_prompt(
            "공통 지침" * 1000,
            "주제" * 1000,
            "메모리" * 3000,
            "대화" * 3000,
            "작업" * 1000,
        )
        self.assertLessEqual(len(prompt), roundtable.PROMPT_MAX_CHARS)
        self.assertIn("현재 작업", prompt)

    def test_agent_output_is_clipped(self):
        output, truncated = roundtable.clip_agent_output("가" * 3000, max_chars=2000)
        self.assertTrue(truncated)
        self.assertLessEqual(len(output), 2000)
        self.assertIn("자동 축약", output)

    def test_project_snapshot_detects_create_modify_and_runtime_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            existing = root / "existing.txt"
            existing.write_text("before", encoding="utf-8")
            before, before_truncated = roundtable.snapshot_project_tree(root)

            existing.write_text("after with different size", encoding="utf-8")
            (root / "created.txt").write_text("new", encoding="utf-8")
            (root / ".git").mkdir()
            after, after_truncated = roundtable.snapshot_project_tree(root)

        changes = {
            (item["path"], item["change"])
            for item in roundtable.compare_project_snapshots(before, after)
        }
        self.assertFalse(before_truncated or after_truncated)
        self.assertIn(("existing.txt", "수정"), changes)
        self.assertIn(("created.txt", "생성"), changes)
        self.assertIn((".git/", "생성"), changes)

    def test_disabled_agent_cannot_be_an_intervention_target(self):
        roundtable.STATE["enabled_agents"] = ["codex", "claude"]
        self.assertEqual(
            roundtable.normalize_target_agents(["antigravity", "claude"]),
            ["claude"],
        )
        self.assertEqual(roundtable.normalize_target_agents(["antigravity"]), [])

    @patch.object(roundtable, "start_worker_if_needed")
    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "save_state")
    def test_finished_discussion_switches_to_first_coding_proposal(
        self, _save_state, _event, start_worker
    ):
        roundtable.STATE.update(
            topic="테스트",
            mode="discussion",
            enabled_agents=["codex"],
            step_index=3,
            finished=True,
        )

        payload = roundtable.switch_mode("coding")

        self.assertTrue(payload["success"])
        self.assertEqual(roundtable.STATE["step_index"], 2)
        self.assertFalse(roundtable.STATE["finished"])
        start_worker.assert_called_once_with(force=True)

    @patch.object(roundtable, "start_worker_if_needed")
    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "save_state")
    def test_coding_to_discussion_returns_to_discussion_report(
        self, _save_state, _event, start_worker
    ):
        roundtable.STATE.update(
            topic="테스트",
            mode="coding",
            enabled_agents=["codex"],
            step_index=4,
            finished=False,
        )
        roundtable.CONTROL["awaiting_approval"] = True

        payload = roundtable.switch_mode("discussion")

        self.assertTrue(payload["success"])
        self.assertEqual(roundtable.STATE["step_index"], 2)
        self.assertFalse(roundtable.CONTROL["awaiting_approval"])
        start_worker.assert_called_once_with(force=True)

    @patch.object(roundtable, "save_state")
    def test_mode_switch_waits_for_active_agent(self, _save_state):
        roundtable.STATE.update(topic="테스트", active_agent="codex")
        payload = roundtable.switch_mode("coding")
        self.assertIn("error", payload)

    @patch.object(roundtable, "update_profile")
    @patch.object(roundtable, "render_html_snapshot")
    @patch.object(roundtable, "save_state")
    @patch.object(roundtable, "run_step", return_value=False)
    def test_failed_worker_step_pauses_for_retry(
        self, _run_step, _save_state, _render, _profile
    ):
        roundtable.STATE.update(
            topic="테스트",
            enabled_agents=["codex"],
            mode="discussion",
            step_index=0,
        )
        session_id = roundtable.STATE["id"]

        roundtable.worker_loop(session_id)

        self.assertTrue(roundtable.CONTROL["paused"])
        self.assertEqual(roundtable.STATE["step_index"], 0)
        self.assertFalse(roundtable.STATE["finished"])

    @patch.object(roundtable, "render_html_snapshot")
    @patch.object(roundtable, "add_message", return_value=True)
    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "build_memory_context", return_value="")
    @patch.object(roundtable, "load_team_prompt", return_value="공통 지침")
    @patch.object(roundtable, "save_state")
    def test_failed_cli_step_returns_false_and_clears_thinking(
        self,
        _save_state,
        _team_prompt,
        _memory,
        event,
        add_message,
        _render,
    ):
        roundtable.STATE.update(topic="테스트")
        session_id = roundtable.STATE["id"]
        failure = "(Codex 응답 없음 — 종료 코드 1: 테스트 오류)"
        with patch.dict(roundtable.ASK_FUNCS, {"codex": lambda _p, _m: failure}):
            succeeded = roundtable.run_step(
                "codex",
                "테스트",
                "응답해라",
                "discussion",
                expected_session_id=session_id,
            )

        self.assertFalse(succeeded)
        self.assertIsNone(roundtable.STATE["active_agent"])
        self.assertFalse(add_message.call_args.args[3]["ok"])
        self.assertEqual(event.call_args.kwargs["level"], "error")

    @patch.object(roundtable, "render_html_snapshot")
    @patch.object(roundtable, "add_message", return_value=True)
    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "build_memory_context", return_value="")
    @patch.object(roundtable, "load_team_prompt", return_value="공통 지침")
    @patch.object(roundtable, "save_state")
    def test_agent_can_request_approval_with_final_token(
        self,
        _save_state,
        _team_prompt,
        _memory,
        _event,
        add_message,
        _render,
    ):
        roundtable.STATE.update(topic="테스트")
        roundtable.CONTROL["approval_requested"] = False
        roundtable.CONTROL["approval_requested_by"] = []
        session_id = roundtable.STATE["id"]
        with patch.dict(
            roundtable.ASK_FUNCS,
            {"codex": lambda _prompt, _mode: "파일 변경 전에 승인이 필요합니다.\nAPPROVE"},
        ):
            succeeded = roundtable.run_step(
                "codex",
                "승인 테스트",
                "필요하면 승인을 요청해라",
                "discussion",
                expected_session_id=session_id,
            )

        self.assertTrue(succeeded)
        self.assertTrue(roundtable.CONTROL["approval_requested"])
        self.assertEqual(roundtable.CONTROL["approval_requested_by"], ["Codex · 승인 테스트"])
        self.assertEqual(add_message.call_args.args[2], "파일 변경 전에 승인이 필요합니다.")
        self.assertTrue(add_message.call_args.args[3]["approval_requested"])


if __name__ == "__main__":
    unittest.main()
