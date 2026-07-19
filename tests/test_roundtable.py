import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_discussion_read_mode_enables_claude_read_tools(self):
        roundtable.STATE["mode"] = "discussion"
        roundtable.STATE["discussion_project_access"] = "read"
        result = subprocess.CompletedProcess([], 0, "정상 응답", "")
        with patch.object(roundtable, "run_cli", return_value=result) as run_cli:
            roundtable.ask_claude("프로젝트 확인")
        args = run_cli.call_args.args[2]
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(run_cli.call_args.kwargs["cwd"], roundtable.load_project_path())

    def test_discussion_without_project_uses_no_tools_and_isolated_directory(self):
        roundtable.STATE["mode"] = "discussion"
        roundtable.STATE["discussion_project_access"] = "none"
        result = subprocess.CompletedProcess([], 0, "정상 응답", "")
        with patch.object(roundtable, "run_cli", return_value=result) as run_cli:
            roundtable.ask_claude("대화만 진행")
        args = run_cli.call_args.args[2]
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(run_cli.call_args.kwargs["cwd"], roundtable.NO_PROJECT_DIR)

        with patch.object(roundtable, "run_cli", return_value=result) as run_cli:
            roundtable.ask_codex("대화만 진행")
        self.assertEqual(run_cli.call_args.kwargs["cwd"], roundtable.NO_PROJECT_DIR)
        self.assertEqual(
            run_cli.call_args.args[2][run_cli.call_args.args[2].index("--sandbox") + 1],
            "read-only",
        )

        with patch.object(roundtable, "run_cli", return_value=result) as run_cli:
            roundtable.ask_antigravity("대화만 진행")
        self.assertEqual(run_cli.call_args.kwargs["cwd"], roundtable.NO_PROJECT_DIR)
        self.assertNotIn("--add-dir", run_cli.call_args.args[2])
        self.assertIn("--sandbox", run_cli.call_args.args[2])
        self.assertNotIn("--dangerously-skip-permissions", run_cli.call_args.args[2])

    def test_all_agents_receive_write_access_for_coding_turns(self):
        roundtable.STATE["mode"] = "coding"
        roundtable.STATE["workspace_access"] = "write"
        result = subprocess.CompletedProcess([], 0, "정상 응답", "")
        for agent, ask_func in roundtable.ASK_FUNCS.items():
            with self.subTest(agent=agent), patch.object(roundtable, "run_cli", return_value=result) as run_cli:
                ask_func("구현", "coding")
                args = run_cli.call_args.args[2]
                if agent == "codex":
                    self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
                    self.assertNotIn("--ignore-user-config", args)
                    self.assertEqual(
                        Path(args[args.index("--cd") + 1]),
                        roundtable.load_project_path(),
                    )
                elif agent == "claude":
                    self.assertIn("Bash,Edit,Read,Write,Glob,Grep", args)
                    self.assertEqual(args[args.index("--permission-mode") + 1], "acceptEdits")
                else:
                    self.assertEqual(args[args.index("--mode") + 1], "accept-edits")
                    self.assertIn("--dangerously-skip-permissions", args)

    def test_all_agents_receive_write_access_in_writable_discussion(self):
        roundtable.STATE["mode"] = "discussion"
        roundtable.STATE["discussion_project_access"] = "write"
        roundtable.STATE["workspace_access"] = "write"
        result = subprocess.CompletedProcess([], 0, "정상 응답", "")
        for agent, ask_func in roundtable.ASK_FUNCS.items():
            with self.subTest(agent=agent), patch.object(roundtable, "run_cli", return_value=result) as run_cli:
                ask_func("쓰기 권한 확인", "discussion")
                args = run_cli.call_args.args[2]
                if agent == "codex":
                    self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
                    self.assertNotIn("--ignore-user-config", args)
                elif agent == "claude":
                    self.assertIn("Bash,Edit,Read,Write,Glob,Grep", args)
                    self.assertEqual(args[args.index("--permission-mode") + 1], "acceptEdits")
                else:
                    self.assertEqual(args[args.index("--mode") + 1], "accept-edits")
                    self.assertIn("--dangerously-skip-permissions", args)

    def test_coding_mode_grants_write_only_to_coding_turns(self):
        state = roundtable.new_state()
        state["mode"] = "coding"
        self.assertEqual(roundtable.turn_project_access("discussion", state), "read")
        self.assertEqual(roundtable.turn_project_access("coding", state), "write")

    def test_continuous_review_turn_is_read_only(self):
        state = roundtable.new_state()
        state["mode"] = "continuous"
        self.assertEqual(roundtable.turn_project_access("discussion", state), "read")
        self.assertEqual(roundtable.turn_project_access("coding", state), "write")

    def test_coding_proposals_wait_for_combined_confirmation(self):
        self.assertFalse(
            roundtable.should_honor_approval_request(True, "coding", "개선안 제안")
        )
        self.assertFalse(
            roundtable.should_honor_approval_request(
                True, "coding", "호출 답변 · Antigravity"
            )
        )
        self.assertFalse(
            roundtable.should_honor_approval_request(True, "coding", "최종 보고")
        )
        self.assertTrue(
            roundtable.should_honor_approval_request(
                True, "coding", roundtable.CONFIRM_PHASE
            )
        )
        self.assertTrue(
            roundtable.should_honor_approval_request(True, "discussion", "개입 답변")
        )

    def test_scheduled_coding_step_cannot_delegate_duplicate_work(self):
        self.assertFalse(roundtable.allow_agent_calls("coding", "작업 수행"))
        self.assertTrue(roundtable.allow_agent_calls("coding", "개선안 제안"))
        self.assertTrue(roundtable.allow_agent_calls("continuous", "개발 진행"))

    def test_codex_read_only_turn_keeps_isolated_user_config(self):
        roundtable.STATE["mode"] = "discussion"
        roundtable.STATE["discussion_project_access"] = "read"
        result = subprocess.CompletedProcess([], 0, "정상 응답", "")

        with patch.object(roundtable, "run_cli", return_value=result) as run_cli:
            roundtable.ask_codex("프로젝트 확인", "discussion")

        args = run_cli.call_args.args[2]
        self.assertIn("--ignore-user-config", args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")

    def test_discussion_mode_can_grant_write_access(self):
        state = roundtable.new_state()
        state["mode"] = "discussion"
        state["discussion_project_access"] = "write"
        self.assertEqual(roundtable.turn_project_access("discussion", state), "write")
        self.assertEqual(roundtable.discussion_project_access_label("write"), "프로젝트 읽기·쓰기")

    def test_continuous_mode_cycle_contains_development_review_and_synthesis(self):
        steps = roundtable.steps_for_mode("continuous", roundtable.AGENT_ORDER)
        self.assertEqual([step[1] for step in steps[:3]], ["개발 진행"] * 3)
        self.assertEqual([step[1] for step in steps[3:6]], ["교차 검토"] * 3)
        self.assertEqual(steps[-1][1], "토론 결과 통합")
        self.assertTrue(all(step[3] == "coding" for step in steps[:3]))
        self.assertTrue(all(step[3] == "discussion" for step in steps[3:]))

    @patch.object(roundtable, "start_worker_if_needed")
    def test_server_startup_requires_manual_resume(self, start_worker):
        roundtable.STATE.update(
            topic="저장된 작업",
            finished=False,
            mode="continuous",
            continuous_stopped=False,
        )
        roundtable.CONTROL.update(paused=False, stopped=False)

        roundtable.prepare_manual_resume_after_startup()

        self.assertTrue(roundtable.CONTROL["paused"])
        self.assertFalse(roundtable.CONTROL["stopped"])
        start_worker.assert_not_called()

    @patch.object(roundtable, "start_worker_if_needed")
    def test_server_startup_does_not_pause_finished_session(self, start_worker):
        roundtable.STATE.update(topic="완료된 작업", finished=True)
        roundtable.CONTROL.update(paused=False, stopped=False)

        roundtable.prepare_manual_resume_after_startup()

        self.assertFalse(roundtable.CONTROL["paused"])
        start_worker.assert_not_called()

    def test_dashboard_get_does_not_reference_autostart_worker(self):
        source = Path(roundtable.__file__).read_text(encoding="utf-8")
        self.assertNotIn("maybe_autostart_worker", source)

    @patch.object(roundtable, "render_html_snapshot")
    @patch.object(roundtable, "run_project_validation", return_value=[])
    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "save_state")
    def test_continuous_worker_wraps_into_next_cycle(
        self, _save_state, _event, validation, _render
    ):
        roundtable.STATE.update(
            topic="반복 개발",
            mode="continuous",
            enabled_agents=list(roundtable.AGENT_ORDER),
            step_index=0,
            finished=False,
        )
        roundtable.CONTROL.update(
            stopped=False,
            paused=False,
            approval_requested=False,
            intervention_pending=False,
            worker_session_id=roundtable.STATE["id"],
        )
        cycle = roundtable.steps_for_mode("continuous", roundtable.AGENT_ORDER)
        phases = []

        def fake_run_step(_agent, phase, _instruction, _cli_mode, **_kwargs):
            phases.append(phase)
            if len(phases) == len(cycle) + 1:
                roundtable.CONTROL["stopped"] = True
            return True

        with patch.object(roundtable, "run_step", side_effect=fake_run_step):
            roundtable.worker_loop(roundtable.STATE["id"])

        self.assertIn("개발 진행 · 사이클 1", phases[0])
        self.assertIn("토론 결과 통합 · 사이클 1", phases[len(cycle) - 1])
        self.assertIn("개발 진행 · 사이클 2", phases[len(cycle)])
        self.assertEqual(roundtable.STATE["step_index"], len(cycle) + 1)
        self.assertFalse(roundtable.STATE["finished"])
        validation.assert_called_once()

    @patch.object(roundtable, "start_worker_if_needed")
    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "save_state")
    def test_switching_to_continuous_mode_starts_fresh_writable_cycle(
        self, _save_state, _event, start_worker
    ):
        roundtable.STATE.update(
            topic="반복 개발",
            mode="discussion",
            step_index=5,
            finished=True,
            workspace_access="read",
            continuous_stopped=True,
        )
        payload = roundtable.switch_mode("continuous")
        self.assertTrue(payload["success"])
        self.assertEqual(roundtable.STATE["mode"], "continuous")
        self.assertEqual(roundtable.STATE["step_index"], 0)
        self.assertEqual(roundtable.STATE["workspace_access"], "write")
        self.assertFalse(roundtable.STATE["finished"])
        self.assertFalse(roundtable.STATE["continuous_stopped"])
        start_worker.assert_called_once_with(force=True)

    @patch.object(roundtable, "state_json_payload", return_value={})
    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "save_state")
    def test_discussion_project_access_can_be_changed(self, save_state, _event, _payload):
        result = roundtable.update_discussion_project_access("none")
        self.assertTrue(result["success"])
        self.assertEqual(roundtable.STATE["discussion_project_access"], "none")
        save_state.assert_called_once()

    @patch.object(roundtable, "state_json_payload", return_value={})
    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "save_state")
    def test_discussion_project_access_can_enable_write(self, save_state, _event, _payload):
        result = roundtable.update_discussion_project_access("write")
        self.assertTrue(result["success"])
        self.assertEqual(roundtable.STATE["discussion_project_access"], "write")
        self.assertEqual(roundtable.STATE["workspace_access"], "write")
        save_state.assert_called_once()

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

    def test_unknown_roles_fall_back_to_unassigned(self):
        roles = roundtable.normalize_agent_roles({
            "codex": "backend",
            "claude": "not-a-role",
        })
        self.assertEqual(roles["codex"], "backend")
        self.assertEqual(roles["claude"], "")
        self.assertEqual(roles["antigravity"], "")

    def test_role_discussion_steps_require_structured_selection(self):
        steps = roundtable.steps_for_mode("discussion", roundtable.AGENT_ORDER)
        role_steps = [step for step in steps if step[1] in {"역할 선언", "역할 확정"}]
        self.assertEqual(len(role_steps), 3)
        self.assertTrue(all("ROLE_SELECT: 역할_ID" in step[2] for step in role_steps))
        self.assertTrue(all("backend=백엔드 개발자" in step[2] for step in role_steps))

    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "write_session_roles")
    @patch.object(roundtable, "save_state")
    def test_role_discussion_auto_selects_and_avoids_duplicates(
        self, _save_state, _write_roles, _event
    ):
        codex_role = roundtable.choose_discussion_role("codex", "backend")
        antigravity_role = roundtable.choose_discussion_role("antigravity", "backend")
        claude_role = roundtable.choose_discussion_role("claude", "frontend")

        self.assertEqual(codex_role, "backend")
        self.assertEqual(antigravity_role, "qa")
        self.assertEqual(claude_role, "frontend")
        self.assertEqual(len(set(roundtable.STATE["agent_roles"].values())), 3)

    def test_role_selection_marker_is_removed_from_visible_answer(self):
        text = "백엔드를 맡겠습니다.\nROLE_SELECT: backend"
        self.assertEqual(roundtable.extract_role_selection(text), "backend")
        self.assertEqual(roundtable.strip_role_selection(text), "백엔드를 맡겠습니다.")

    def test_mode_notice_is_rendered_as_system_chat(self):
        notice = roundtable.mode_notice_html("coding", "read")
        self.assertIn("현재 세션 모드", notice)
        self.assertIn("코딩 모드", notice)
        self.assertIn("프로젝트 읽기·쓰기", notice)
        self.assertIn("row center system", notice)

    @patch.object(roundtable, "add_message", return_value=True)
    @patch.object(roundtable, "save_state")
    def test_complete_roles_are_announced_in_chat(self, _save_state, add_message):
        roundtable.STATE["agent_roles"] = {
            "codex": "backend",
            "antigravity": "qa",
            "claude": "frontend",
        }

        self.assertTrue(roundtable.announce_roles_if_complete())

        args = add_message.call_args.args
        self.assertEqual(args[:2], ("system", "역할 배정 완료"))
        self.assertIn("Codex**: 백엔드 개발자", args[2])
        self.assertIn("Antigravity**: QA·테스트 담당", args[2])
        self.assertIn("Claude Code**: 프론트엔드 개발자", args[2])
        self.assertFalse(roundtable.announce_roles_if_complete())

    def test_legacy_role_block_message_is_removed_on_load(self):
        state = roundtable.new_state()
        state["messages"] = [
            {"agent": "codex", "text": "차단", "meta": {"failure_kind": "role_unassigned"}},
            {"agent": "codex", "text": "정상", "meta": {"ok": True}},
        ]
        normalized = roundtable.normalize_state(state)
        self.assertEqual([message["text"] for message in normalized["messages"]], ["정상"])

    def test_duplicate_roles_are_rejected(self):
        result = roundtable.update_agent_roles({
            "codex": "backend",
            "antigravity": "backend",
            "claude": "frontend",
        })
        self.assertIn("error", result)

    def test_orphaned_intervention_approval_finds_requesting_agent(self):
        messages = [
            {
                "agent": "antigravity",
                "phase": "개입 답변",
                "text": "승인이 필요합니다.",
                "meta": {"approval_requested": True},
            },
            {"agent": "user", "phase": "승인", "text": "승인"},
        ]
        self.assertEqual(roundtable.orphaned_approval_followup_agents(messages), ["antigravity"])

    @patch.object(roundtable, "start_worker_if_needed")
    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "add_message", return_value=True)
    @patch.object(roundtable, "save_state")
    def test_approval_preserves_queue_and_adds_requester_followup(
        self, _save_state, _add_message, _event, start_worker
    ):
        roundtable.STATE.update(
            finished=True,
            messages=[{
                "agent": "antigravity",
                "phase": "개입 답변",
                "text": "승인이 필요합니다.",
                "meta": {"approval_requested": True},
            }],
        )
        existing = {
            "intent": "delegation",
            "targets": ["codex"],
            "cli_mode": "discussion",
            "custom_instruction": "검토",
        }
        roundtable.CONTROL.update(
            awaiting_approval=True,
            approval_requested=True,
            intervention_queue=[existing],
            intervention_pending=True,
        )

        self.assertTrue(roundtable.approve_pending_work())

        self.assertEqual(roundtable.CONTROL["intervention_queue"][0], existing)
        followup = roundtable.CONTROL["intervention_queue"][1]
        self.assertEqual(followup["intent"], "execute")
        self.assertEqual(followup["targets"], ["antigravity"])
        self.assertTrue(followup["approved_followup"])
        self.assertFalse(roundtable.STATE["finished"])
        start_worker.assert_called_once_with(force=True)

    def test_role_scope_detects_files_outside_assignment(self):
        state = roundtable.new_state()
        state["agent_roles"]["codex"] = "backend"
        changed = [
            {"path": "app.py", "change": "modified"},
            {"path": "static/dashboard.js", "change": "modified"},
        ]
        self.assertEqual(
            roundtable.role_scope_violations("codex", changed, state),
            ["static/dashboard.js"],
        )

    def test_role_scope_allows_shared_coordination_files(self):
        state = roundtable.new_state()
        state["agent_roles"]["codex"] = "backend"
        changed = [
            {"path": "collector.py", "change": "modified"},
            {"path": "TODO.md", "change": "modified"},
            {"path": "AGENTS.md", "change": "modified"},
            {"path": ".ruff_cache/0.12/cache", "change": "modified"},
            {"path": ".agents/", "change": "created"},
            {"path": "roundtable_memory/session/full.md", "change": "created"},
        ]
        self.assertEqual(roundtable.role_scope_violations("codex", changed, state), [])

    def test_failed_workflow_step_can_continue_without_repeating(self):
        state = roundtable.new_state()
        state.update(
            mode="coding",
            enabled_agents=list(roundtable.AGENT_ORDER),
            step_index=9,
        )
        messages = [{
            "agent": "codex",
            "phase": "작업 수행",
            "meta": {"ok": False, "failure_kind": "role_scope"},
        }]
        self.assertTrue(roundtable.can_continue_after_failed_step(state, messages))

        messages[-1]["phase"] = "개입 답변"
        self.assertFalse(roundtable.can_continue_after_failed_step(state, messages))

    def test_role_prompt_contains_user_assignment(self):
        state = roundtable.new_state()
        state["agent_roles"]["claude"] = "frontend"
        prompt = roundtable.role_prompt("claude", state)
        self.assertIn("프론트엔드 개발자", prompt)
        self.assertIn("다른 역할의 작업을 대신 수행", prompt)

    def test_unassigned_role_does_not_enforce_file_scope(self):
        state = roundtable.new_state()
        changed = [
            {"path": "app.py", "change": "modified"},
            {"path": "static/dashboard.js", "change": "modified"},
        ]
        self.assertEqual(roundtable.role_scope_violations("codex", changed, state), [])
        self.assertIn("일반 작업자", roundtable.role_prompt("codex", state))

    def test_session_roles_file_tracks_current_assignments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = roundtable.new_state()
            state["name"] = "역할 테스트"
            state["agent_roles"] = {
                "codex": "backend",
                "antigravity": "qa",
                "claude": "frontend",
            }
            with patch.object(roundtable, "MEMORY_DIR", Path(temp_dir)):
                roundtable.write_session_roles(state)
                content = roundtable.session_roles_path(state["id"]).read_text(encoding="utf-8")
        self.assertIn("Codex | 활성 | 백엔드 개발자", content)
        self.assertIn("Antigravity | 활성 | QA·테스트 담당", content)
        self.assertIn("Claude Code | 활성 | 프론트엔드 개발자", content)

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
        self.assertEqual(commands[0][0], "Python pytest")
        self.assertEqual(commands[0][1][-2:], ["pytest", "-q"])

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

    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "save_project_path")
    @patch.object(roundtable, "save_state")
    def test_saved_session_can_be_activated_without_starting_worker(
        self, save_state, save_project_path, _runtime_event
    ):
        source = roundtable.new_state()
        source.update(
            id="saved",
            name="저장 세션",
            topic="계속할 작업",
            mode="coding",
            finished=False,
            workspace_path=str(roundtable.ROOT),
            workspace_access="write",
            messages=[],
        )
        with patch.object(roundtable, "load_session", return_value=source):
            result = roundtable.activate_session("saved")

        self.assertTrue(result["success"])
        self.assertEqual(roundtable.STATE["id"], "saved")
        self.assertTrue(roundtable.CONTROL["paused"])
        self.assertFalse(roundtable.CONTROL["worker_running"])
        save_project_path.assert_called_once()
        save_state.assert_called()

    def test_session_activation_is_blocked_while_model_is_running(self):
        roundtable.STATE["active_agent"] = "codex"

        result = roundtable.activate_session("saved")

        self.assertIn("error", result)

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

    def test_validation_results_are_shared_with_review_turns(self):
        state = roundtable.new_state()
        state["validation_results"] = [{
            "label": "Python unittest",
            "ok": False,
            "output": "FAILED test_checkout",
        }]
        context = roundtable.build_memory_context(state)
        self.assertIn("최근 자동 검증", context)
        self.assertIn("Python unittest: 실패", context)
        self.assertIn("FAILED test_checkout", context)

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

    def test_model_prefix_is_extracted_case_insensitively(self):
        text, target = roundtable.extract_target_prefix("[Antigravity] 진행 시작")
        self.assertEqual(text, "진행 시작")
        self.assertEqual(target, "antigravity")

        text, target = roundtable.extract_target_prefix("[Claude Code] 검증해줘")
        self.assertEqual(text, "검증해줘")
        self.assertEqual(target, "claude")

    def test_agent_call_without_turn_diff_renders_safely(self):
        message = {
            "agent": "claude",
            "phase": "호출 답변 · Antigravity",
            "time": "17:10:36",
            "text": "검토했습니다.",
            "meta": {
                "elapsed": 1.0,
                "est_tokens": 10,
                "prompt_chars": 20,
                "output_chars": 10,
                "cli_mode": "coding",
                "agent_calls": [{
                    "target": "antigravity",
                    "mode": "coding",
                    "task": "재검증",
                }],
            },
        }

        rendered = roundtable.bubble_html(message)

        self.assertIn("후속 에이전트 호출", rendered)
        self.assertNotIn("코드 변경 Diff", rendered)

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

    def test_final_report_does_not_restore_approval_request(self):
        messages = [
            {
                "agent": "claude",
                "phase": "최종 보고",
                "text": "작업과 검증을 마쳤습니다.",
                "meta": {"approval_requested": True},
            }
        ]

        self.assertEqual(roundtable.unresolved_approval_requests(messages), [])
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

    def test_read_only_workspace_downgrades_coding_execution(self):
        roundtable.STATE["workspace_access"] = "read"
        self.assertEqual(roundtable.effective_cli_mode("coding"), "discussion")
        self.assertEqual(roundtable.effective_cli_mode("discussion"), "discussion")
        roundtable.STATE["workspace_access"] = "write"
        self.assertEqual(roundtable.effective_cli_mode("coding"), "coding")

    @patch.object(roundtable, "state_json_payload", return_value={})
    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "save_state")
    def test_workspace_selection_saves_path_and_access(
        self, save_state, _event, _payload
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            path_file = Path(temp_dir) / "PROJECT_PATH.txt"
            with patch.object(roundtable, "PROJECT_PATH_FILE", path_file):
                result = roundtable.update_workspace_selection(temp_dir, "read")

            self.assertTrue(result["success"])
            self.assertEqual(roundtable.STATE["workspace_access"], "read")
            self.assertEqual(roundtable.STATE["workspace_path"], str(Path(temp_dir).resolve()))
            self.assertIn(str(Path(temp_dir).resolve()), path_file.read_text(encoding="utf-8"))
            save_state.assert_called_once()

    @patch.object(roundtable, "save_state")
    def test_execute_intervention_is_queued_in_coding_mode(self, _save_state):
        roundtable.STATE["enabled_agents"] = ["claude"]
        roundtable.CONTROL["intervention_queue"] = []
        roundtable.mark_intervention("execute", ["claude"], "coding")
        queued = roundtable.CONTROL["intervention_queue"][0]
        self.assertEqual(queued["intent"], "execute")
        self.assertEqual(queued["cli_mode"], "coding")

    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "run_step", return_value=True)
    @patch.object(roundtable, "save_state")
    def test_execute_intervention_calls_agent_with_coding_permissions(self, _save_state, run_step, _event):
        roundtable.STATE["enabled_agents"] = ["claude"]
        roundtable.CONTROL["intervention_queue"] = [{"intent": "execute", "targets": ["claude"], "cli_mode": "coding"}]
        roundtable.CONTROL["intervention_pending"] = True
        roundtable.CONTROL["stopped"] = False
        handled = roundtable.process_pending_intervention(roundtable.STATE["id"])
        self.assertTrue(handled)
        self.assertEqual(run_step.call_args.args[3], "coding")

    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "run_step", return_value=False)
    @patch.object(roundtable, "save_state")
    def test_failed_intervention_is_requeued_for_retry(self, _save_state, _run_step, _event):
        roundtable.STATE["enabled_agents"] = ["antigravity"]
        item = {"intent": "execute", "targets": ["antigravity"], "cli_mode": "coding"}
        roundtable.CONTROL["intervention_queue"] = [item]
        roundtable.CONTROL["intervention_pending"] = True
        roundtable.CONTROL["stopped"] = False

        handled = roundtable.process_pending_intervention(roundtable.STATE["id"])

        self.assertTrue(handled)
        self.assertTrue(roundtable.CONTROL["paused"])
        self.assertTrue(roundtable.CONTROL["intervention_pending"])
        queued = roundtable.CONTROL["intervention_queue"][0]
        self.assertEqual(queued["targets"], ["antigravity"])
        self.assertTrue(queued["retry_blocked"])
        self.assertEqual(roundtable.STATE["pending_interventions"], [queued])

        self.assertFalse(
            roundtable.process_pending_intervention(roundtable.STATE["id"])
        )
        self.assertEqual(_run_step.call_count, 1)

    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "run_step", side_effect=[True, False])
    @patch.object(roundtable, "save_state")
    def test_multi_agent_retry_keeps_only_failed_and_unattempted_targets(
        self, _save_state, run_step, _event
    ):
        roundtable.STATE["enabled_agents"] = ["codex", "antigravity", "claude"]
        item = {
            "intent": "execute",
            "targets": ["codex", "antigravity", "claude"],
            "cli_mode": "coding",
        }
        roundtable.CONTROL["intervention_queue"] = [item]
        roundtable.CONTROL["intervention_pending"] = True
        roundtable.CONTROL["stopped"] = False

        handled = roundtable.process_pending_intervention(roundtable.STATE["id"])

        self.assertTrue(handled)
        self.assertEqual(run_step.call_count, 2)
        self.assertEqual(
            roundtable.CONTROL["intervention_queue"][0]["targets"],
            ["antigravity", "claude"],
        )
        self.assertTrue(
            roundtable.CONTROL["intervention_queue"][0]["retry_blocked"]
        )

    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "run_step", return_value=False)
    @patch.object(roundtable, "save_state")
    def test_stopped_intervention_does_not_restore_retry_queue(
        self, _save_state, run_step, _event
    ):
        roundtable.STATE["enabled_agents"] = ["codex"]
        roundtable.CONTROL["intervention_queue"] = [
            {"intent": "execute", "targets": ["codex"], "cli_mode": "coding"}
        ]
        roundtable.CONTROL["intervention_pending"] = True
        roundtable.CONTROL["stopped"] = False

        def stop_during_run(*_args, **_kwargs):
            roundtable.CONTROL["stopped"] = True
            return False

        run_step.side_effect = stop_during_run
        handled = roundtable.process_pending_intervention(roundtable.STATE["id"])

        self.assertTrue(handled)
        self.assertEqual(roundtable.CONTROL["intervention_queue"], [])
        self.assertFalse(roundtable.CONTROL["intervention_pending"])

    @patch.object(roundtable, "terminate_process_tree")
    def test_cancel_active_cli_processes_terminates_process_tree(self, terminate):
        process = Mock()
        process.poll.side_effect = [None, 0]
        roundtable.ACTIVE_PROCESSES["Codex"] = process

        cancelled = roundtable.cancel_active_cli_processes()

        self.assertEqual(cancelled, ["Codex"])
        terminate.assert_called_once_with(process)
        self.assertNotIn("Codex", roundtable.ACTIVE_PROCESSES)

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
        self.assertEqual(roundtable.STATE["workspace_access"], "write")
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
        roundtable.CONTROL.update(
            stopped=False,
            paused=False,
            approval_requested=False,
            intervention_pending=False,
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

    @patch.object(roundtable, "render_html_snapshot")
    @patch.object(roundtable, "add_message", return_value=True)
    @patch.object(roundtable, "add_runtime_event")
    @patch.object(roundtable, "build_memory_context", return_value="")
    @patch.object(roundtable, "load_team_prompt", return_value="공통 지침")
    @patch.object(roundtable, "save_state")
    def test_continuous_mode_ignores_agent_approval_token(
        self, _save_state, _team, _memory, _event, add_message, _render
    ):
        roundtable.STATE.update(topic="반복 개발", mode="continuous")
        roundtable.CONTROL["approval_requested"] = False
        roundtable.CONTROL["approval_requested_by"] = []
        with patch.dict(
            roundtable.ASK_FUNCS,
            {"codex": lambda _prompt, _mode: "계속 진행합니다.\nAPPROVE"},
        ):
            succeeded = roundtable.run_step(
                "codex", "교차 검토 · 사이클 1", "검토해라", "discussion",
                expected_session_id=roundtable.STATE["id"],
            )
        self.assertTrue(succeeded)
        self.assertFalse(roundtable.CONTROL["approval_requested"])
        self.assertFalse(add_message.call_args.args[3]["approval_requested"])


if __name__ == "__main__":
    unittest.main()
