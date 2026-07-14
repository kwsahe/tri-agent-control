import subprocess
import sys
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
        self.assertIn("--safe-mode", args)
        self.assertIn("--no-session-persistence", args)

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

    def test_codex_stream_event_exposes_command_and_final_message(self):
        event, final = roundtable.parse_cli_stream_event(
            "Codex",
            '{"type":"item.completed","item":{"type":"command_execution","command":"rg -n TODO","status":"completed"}}',
        )
        self.assertEqual(event["kind"], "command")
        self.assertIn("rg -n TODO", event["text"])
        self.assertIsNone(final)

        event, final = roundtable.parse_cli_stream_event(
            "Codex",
            '{"type":"item.completed","item":{"type":"agent_message","text":"완료"}}',
        )
        self.assertEqual(event["kind"], "message")
        self.assertEqual(final, "완료")

    def test_claude_stream_result_exposes_usage(self):
        event, final = roundtable.parse_cli_stream_event(
            "Claude Code",
            '{"type":"result","result":"확인","usage":{"input_tokens":120,"output_tokens":8},"total_cost_usd":0.01}',
        )
        self.assertEqual(event["kind"], "usage")
        self.assertEqual(event["usage"]["input_tokens"], 120)
        self.assertEqual(final, "확인")

    def test_incomplete_tool_marker_is_a_failure(self):
        self.assertTrue(roundtable.is_incomplete_tool_response("파일을 확인하겠습니다.\n\n**Tool: read**"))
        self.assertTrue(roundtable.is_cli_failure("**Tool: read**"))
        self.assertFalse(roundtable.is_incomplete_tool_response("read 도구 없이 최종 답변을 작성했습니다."))

    def test_claude_discussion_retries_incomplete_tool_response_once(self):
        first = subprocess.CompletedProcess([], 0, "확인하겠습니다.\n\n**Tool: read**", "")
        second = subprocess.CompletedProcess([], 0, "완결된 답변", "")
        events = []
        with patch.object(roundtable, "run_cli", side_effect=[first, second]) as run_cli:
            response = roundtable.ask_claude("질문", "discussion", events.append)
        self.assertEqual(response, "완결된 답변")
        self.assertEqual(run_cli.call_count, 2)
        self.assertTrue(any("자동 교정" in event["text"] for event in events))
        first_args = run_cli.call_args_list[0].args[2]
        self.assertIn("--system-prompt", first_args)

    @patch.object(roundtable, "save_state")
    def test_active_session_can_be_renamed(self, save_state):
        result = roundtable.rename_session(roundtable.STATE["id"], "  UI 점검 세션  ")
        self.assertTrue(result["success"])
        self.assertEqual(roundtable.STATE["name"], "UI 점검 세션")
        save_state.assert_called_once()

    def test_usage_summary_is_split_by_agent(self):
        messages = [
            {"agent": "claude", "meta": {"est_tokens": 30, "prompt_chars": 100, "output_chars": 20,
                "actual_usage": {"input_tokens": 80, "cache_read_input_tokens": 10, "output_tokens": 5}}},
            {"agent": "codex", "meta": {"est_tokens": 12, "prompt_chars": 40, "output_chars": 8}},
        ]
        summary = roundtable.agent_usage_summary(messages)
        self.assertEqual(summary["claude"]["turns"], 1)
        self.assertEqual(summary["claude"]["input_tokens"], 80)
        self.assertEqual(summary["codex"]["estimated_tokens"], 12)

    def test_actual_token_count_does_not_double_count_openai_cache(self):
        usage = {"input_tokens": 12000, "cached_input_tokens": 9000, "output_tokens": 50}
        self.assertEqual(roundtable.actual_token_count(usage), 12050)

    def test_actual_token_count_includes_anthropic_cache_fields(self):
        usage = {
            "input_tokens": 2,
            "cache_creation_input_tokens": 3300,
            "cache_read_input_tokens": 3200,
            "output_tokens": 4,
        }
        self.assertEqual(roundtable.actual_token_count(usage), 6506)

    def test_claude_context_uses_latest_iteration_instead_of_cumulative_usage(self):
        messages = [{
            "agent": "claude",
            "meta": {
                "est_tokens": 2381,
                "actual_usage": {
                    "input_tokens": 49,
                    "cache_creation_input_tokens": 59920,
                    "cache_read_input_tokens": 1015972,
                    "output_tokens": 15860,
                    "iterations": [{
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 1949,
                        "cache_read_input_tokens": 57971,
                        "output_tokens": 702,
                    }],
                },
            },
        }]

        context = roundtable.agent_context_summary(messages)["claude"]

        self.assertEqual(context["used_tokens"], 60624)
        self.assertEqual(context["limit_tokens"], 128000)
        self.assertEqual(context["percent"], 47.4)
        self.assertFalse(context["estimated"])

    def test_context_usage_falls_back_to_latest_estimate(self):
        messages = [
            {"agent": "antigravity", "meta": {"est_tokens": 2100}},
            {"agent": "antigravity", "meta": {"est_tokens": 2500}},
        ]

        context = roundtable.agent_context_summary(messages)["antigravity"]

        self.assertEqual(context["used_tokens"], 2500)
        self.assertTrue(context["estimated"])

    def test_budget_limit_reports_token_and_cost_overruns(self):
        state = {"budget": {"token_limit": 100, "cost_limit_usd": 1.0}, "total_actual_tokens": 100, "total_actual_cost_usd": 0.2}
        self.assertIn("토큰 예산", roundtable.budget_exceeded_reason(state))
        state["total_actual_tokens"] = 50
        state["total_actual_cost_usd"] = 1.0
        self.assertIn("비용 예산", roundtable.budget_exceeded_reason(state))

    def test_budget_block_adds_visible_agent_message(self):
        roundtable.STATE.update(
            topic="테스트",
            budget={"token_limit": 100, "cost_limit_usd": 0.0},
            total_actual_tokens=100,
        )
        with (
            patch.object(roundtable, "add_runtime_event"),
            patch.object(roundtable, "add_message", return_value=True) as add_message,
        ):
            succeeded = roundtable.run_step(
                "antigravity",
                "개입 답변",
                "검증 시작",
                "coding",
                expected_session_id=roundtable.STATE["id"],
            )

        self.assertFalse(succeeded)
        self.assertTrue(roundtable.CONTROL["paused"])
        self.assertEqual(add_message.call_args.args[1], "실행 차단 · 예산 한도")
        self.assertIn("자동으로 재개", add_message.call_args.args[2])

    def test_validation_command_detection_for_python_tests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_sample.py").write_text("", encoding="utf-8")
            commands = roundtable.detect_validation_commands(root)
        self.assertEqual(commands[0][0], "Python unittest")

    def test_turn_diff_contains_only_changed_text_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "sample.py"
            path.write_text("value = 1\n", encoding="utf-8")
            before = roundtable.capture_project_texts(root)
            path.write_text("value = 2\n", encoding="utf-8")
            diff = roundtable.build_turn_diff(root, before, [{"path": "sample.py", "change": "수정"}])
        self.assertIn("-value = 1", diff)
        self.assertIn("+value = 2", diff)

    def test_active_cli_process_can_be_cancelled(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        roundtable.ACTIVE_PROCESSES["Test CLI"] = process
        cancelled = roundtable.cancel_active_cli_processes()
        self.assertIn("Test CLI", cancelled)
        self.assertIsNotNone(process.poll())

    def test_checkpoint_can_rollback_selected_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            memory = root / "memory"
            memory.mkdir()
            target = root / "target"
            target.mkdir()
            source = target / "sample.txt"
            source.write_text("before\n", encoding="utf-8")
            before = roundtable.capture_project_texts(target)
            source.write_text("after\n", encoding="utf-8")
            diff = roundtable.build_turn_diff(target, before, [{"path": "sample.txt", "change": "수정"}])
            checkpoint = memory / "turn.patch"
            checkpoint.write_text(diff, encoding="utf-8")
            with patch.object(roundtable, "MEMORY_DIR", memory), patch.object(
                roundtable, "load_project_path", return_value=target
            ), patch.object(roundtable, "add_runtime_event"):
                result = roundtable.rollback_checkpoint_files(str(checkpoint), ["sample.txt"])
            self.assertTrue(result["success"])
            self.assertEqual(source.read_text(encoding="utf-8"), "before\n")

    @patch.object(roundtable, "save_state")
    def test_active_session_metadata_can_be_updated(self, save_state):
        result = roundtable.update_session_metadata(
            roundtable.STATE["id"], "backend, urgent", True, False
        )
        self.assertTrue(result["success"])
        self.assertEqual(roundtable.STATE["tags"], ["backend", "urgent"])
        self.assertTrue(roundtable.STATE["favorite"])
        save_state.assert_called_once()

    def test_current_session_cannot_be_deleted(self):
        result = roundtable.delete_session(roundtable.STATE["id"])
        self.assertIn("error", result)

    @patch.object(roundtable, "build_memory_context", return_value="메모리")
    @patch.object(roundtable, "load_team_prompt", return_value="공통 지침")
    def test_prompt_preview_contains_final_prompt_and_size(self, _team, _memory):
        roundtable.STATE.update(topic="미리보기 테스트", enabled_agents=["codex"])
        payload = roundtable.prompt_preview_payload("codex")
        self.assertIn("현재 주제: 미리보기 테스트", payload["prompt"])
        self.assertGreater(payload["characters"], 0)
        self.assertGreater(payload["estimated_tokens"], 0)

    @patch.object(roundtable, "save_state")
    @patch.object(roundtable, "cancel_active_cli_processes")
    def test_clone_session_creates_new_active_branch(self, _cancel, save_state):
        source = roundtable.new_state()
        source.update(id="source", name="원본", topic="주제", messages=[{"agent": "codex", "phase": "검토", "time": "00:00:00", "text": "내용"}])
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            roundtable, "load_session", return_value=source
        ), patch.object(
            roundtable, "session_memory_dir", side_effect=lambda session_id: Path(temp_dir) / session_id
        ):
            result = roundtable.clone_session("source")
        self.assertTrue(result["success"])
        self.assertNotEqual(roundtable.STATE["id"], "source")
        self.assertIn("분기", roundtable.STATE["name"])
        self.assertEqual(len(roundtable.STATE["messages"]), 1)
        save_state.assert_called_once()

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

    def test_each_agent_receives_the_other_agents_recent_messages(self):
        messages = [
            {"agent": "codex", "phase": "검토", "text": "Codex의 판단"},
            {"agent": "antigravity", "phase": "검토", "text": "Antigravity의 판단"},
            {"agent": "claude", "phase": "검토", "text": "Claude의 판단"},
        ]
        transcript, selected = roundtable.build_shared_transcript(messages, roundtable.AGENT_ORDER)
        self.assertIn("[Codex]", transcript)
        self.assertIn("Codex의 판단", transcript)
        self.assertIn("[Antigravity]", transcript)
        self.assertIn("Antigravity의 판단", transcript)
        self.assertIn("[Claude Code]", transcript)
        self.assertIn("Claude의 판단", transcript)
        self.assertEqual(len(selected), 3)

    def test_approval_token_is_only_recognized_on_the_final_line(self):
        visible, requested = roundtable.extract_approval_token("변경 전에 확인이 필요합니다.\nAPPROVE")
        self.assertTrue(requested)
        self.assertEqual(visible, "변경 전에 확인이 필요합니다.")

        visible, requested = roundtable.extract_approval_token("APPROVE\n추가 설명")
        self.assertFalse(requested)
        self.assertEqual(visible, "APPROVE\n추가 설명")

    def test_agent_call_is_extracted_and_removed_from_visible_answer(self):
        text = "검토를 마쳤습니다.\nCALL_AGENT: claude | discussion | 접근성을 확인해줘"
        visible, calls = roundtable.extract_agent_calls(text, "codex", "discussion")
        self.assertEqual(visible, "검토를 마쳤습니다.")
        self.assertEqual(calls, [{"target": "claude", "mode": "discussion", "task": "접근성을 확인해줘"}])

    def test_discussion_turn_cannot_delegate_coding_permission(self):
        _visible, calls = roundtable.extract_agent_calls(
            "CALL_AGENT: antigravity | coding | 테스트를 수정해줘", "codex", "discussion"
        )
        self.assertEqual(calls[0]["mode"], "discussion")

    def test_agent_cannot_call_itself(self):
        visible, calls = roundtable.extract_agent_calls(
            "답변\nCALL_AGENT: codex | discussion | 다시 확인", "codex", "discussion"
        )
        self.assertEqual(visible, "답변")
        self.assertEqual(calls, [])

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

    def test_execute_intervention_is_queued_in_coding_mode(self):
        roundtable.STATE["enabled_agents"] = ["claude"]
        roundtable.CONTROL["intervention_queue"] = []
        roundtable.mark_intervention("execute", ["claude"], "coding")
        queued = roundtable.CONTROL["intervention_queue"][0]
        self.assertEqual(queued["intent"], "execute")
        self.assertEqual(queued["cli_mode"], "coding")

    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "run_step", return_value=True)
    def test_execute_intervention_calls_agent_with_coding_permissions(self, run_step, _event):
        roundtable.STATE["enabled_agents"] = ["claude"]
        roundtable.CONTROL["intervention_queue"] = [{"intent": "execute", "targets": ["claude"], "cli_mode": "coding"}]
        roundtable.CONTROL["intervention_pending"] = True
        handled = roundtable.process_pending_intervention(roundtable.STATE["id"])
        self.assertTrue(handled)
        self.assertEqual(run_step.call_args.args[3], "coding")

    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "run_step", return_value=False)
    def test_failed_intervention_is_requeued_for_retry(self, _run_step, _event):
        roundtable.STATE["enabled_agents"] = ["antigravity"]
        item = {"intent": "execute", "targets": ["antigravity"], "cli_mode": "coding"}
        roundtable.CONTROL["intervention_queue"] = [item]
        roundtable.CONTROL["intervention_pending"] = True

        handled = roundtable.process_pending_intervention(roundtable.STATE["id"])

        self.assertTrue(handled)
        self.assertTrue(roundtable.CONTROL["paused"])
        self.assertTrue(roundtable.CONTROL["intervention_pending"])
        self.assertEqual(roundtable.CONTROL["intervention_queue"], [item])

    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "save_state")
    def test_agent_call_is_added_to_shared_queue(self, save_state, _event):
        roundtable.STATE["enabled_agents"] = ["codex", "claude"]
        roundtable.STATE["delegation_count"] = 0
        roundtable.CONTROL["intervention_queue"] = []
        queued = roundtable.queue_agent_calls(
            "codex", [{"target": "claude", "mode": "discussion", "task": "UI를 검토해줘"}], 0
        )
        self.assertEqual(len(queued), 1)
        self.assertEqual(roundtable.CONTROL["intervention_queue"][0]["intent"], "delegation")
        self.assertEqual(roundtable.STATE["delegation_count"], 1)
        save_state.assert_called_once()

    def test_agent_call_depth_limit_blocks_recursive_queue(self):
        roundtable.STATE["enabled_agents"] = ["codex", "claude"]
        roundtable.CONTROL["intervention_queue"] = []
        queued = roundtable.queue_agent_calls(
            "codex", [{"target": "claude", "mode": "discussion", "task": "검토"}],
            roundtable.MAX_DELEGATION_DEPTH,
        )
        self.assertEqual(queued, [])
        self.assertEqual(roundtable.CONTROL["intervention_queue"], [])

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
