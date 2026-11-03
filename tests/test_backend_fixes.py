"""백엔드 결함 회귀 테스트 — 질문 대기 우회, 남은 자식 프로세스, 시간 초과, 저장 실패 등.

실제 모델 CLI는 부르지 않는다. 프로세스 동작은 파이썬 자식 프로세스로 흉내 낸다.
모든 파일 조작은 TemporaryDirectory(또는 isolation의 임시 폴더) 안에서만 한다.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import isolation  # noqa: F401  (실제 데이터 폴더 격리 — 반드시 roundtable보다 먼저)
import roundtable
from test_http_security import HttpServerTestCase
from triagent.domain import clicheck, workflow
from triagent.security import webguard

SESSION_ID = "20260101_000000_000030"
#: 공백이 있는 경로에서도 깨지지 않게 따옴표로 감싼 파이썬 실행 파일
PYTHON_CMD = f'"{sys.executable}"'
QUESTION = {
    "action": "STOP_AND_ASK_USER",
    "reason": "범위 결정 필요",
    "question": "A안과 B안 중 무엇으로 진행할까요?",
    "options": [{"id": "A", "label": "A안"}, {"id": "B", "label": "B안"}],
    "recommended_option": "A",
    "blocking": True,
}


def kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


class BackendTestBase(unittest.TestCase):
    def setUp(self):
        self.original_state = roundtable.STATE
        self.original_control = dict(roundtable.CONTROL)
        roundtable.STATE = roundtable.new_state()
        roundtable.STATE.update(id=SESSION_ID, topic="백엔드 테스트", enabled_agents=["codex"])
        roundtable.CONTROL.update(
            stopped=False, paused=False, approval_requested=False, awaiting_approval=False,
            intervention_pending=False, intervention_queue=[], user_cancelled=False,
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name).resolve()

    def tearDown(self):
        roundtable.STATE = self.original_state
        roundtable.CONTROL.clear()
        roundtable.CONTROL.update(self.original_control)
        self._tmp.cleanup()


# ──────────────────────────────────────────────────────────────
# 1. 질문 대기는 승인·중단·거절로 풀리지 않는다
# ──────────────────────────────────────────────────────────────
class QuestionWaitTests(HttpServerTestCase):
    def setUp(self):
        self.original_state = roundtable.STATE
        self.original_control = dict(roundtable.CONTROL)
        roundtable.STATE = roundtable.new_state()
        roundtable.STATE.update(topic="질문 테스트", mode="coding", plan_approved=False)

    def tearDown(self):
        roundtable.STATE = self.original_state
        roundtable.CONTROL.clear()
        roundtable.CONTROL.update(self.original_control)

    def post(self, path):
        return self.request(
            path, method="POST", body="",
            headers={webguard.CSRF_HEADER: self.token,
                     "Origin": f"http://127.0.0.1:{self.port}"},
        )

    def test_question_from_the_confirm_step_survives_approve_stop_reject_and_resume(self):
        with patch.object(roundtable, "save_state"), \
             patch.object(roundtable, "add_runtime_event"), \
             patch.object(roundtable, "add_message", return_value=True), \
             patch.object(roundtable, "cancel_active_cli_processes", return_value=[]), \
             patch.object(roundtable, "start_worker_if_needed") as start_worker:
            self.assertTrue(roundtable.register_user_question("claude", roundtable.CONFIRM_PHASE, QUESTION))
            # 확인 요청 단계 뒤 워커가 승인을 요청한다 — 예전에는 여기서 질문 대기가 덮어써졌다.
            roundtable.mark_approval_requested("claude", roundtable.CONFIRM_PHASE)
            _, approve_body = self.post("/approve")
            self.post("/stop")
            self.post("/reject")
            _, resume_body = self.post("/resume")

        self.assertIn("error", json.loads(approve_body))
        self.assertIn("error", json.loads(resume_body))
        self.assertFalse(roundtable.STATE["plan_approved"])
        self.assertTrue(roundtable.waiting_for_user_response())
        start_worker.assert_not_called()
        payload = json.loads(self.request("/state.json")[1])
        self.assertEqual(payload["workflow_status"], workflow.WAITING_FOR_USER_RESPONSE)


# ──────────────────────────────────────────────────────────────
# 2·4·12. CLI 실행: 남은 손자 프로세스, 시간 초과, 프롬프트 쓰기 실패
# ──────────────────────────────────────────────────────────────
class RunCliProcessTests(unittest.TestCase):
    def test_finished_answer_is_kept_when_a_grandchild_holds_the_pipes(self):
        """CLI가 개발 서버를 띄우고 끝나도 시간 초과까지 기다리거나 답변을 버리지 않는다."""
        parent = (
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
            " stdout=sys.stdout, stderr=sys.stderr)\n"
            "print(child.pid, flush=True)\n"
            "print('answer done', flush=True)\n"
        )
        started = time.monotonic()
        with patch.object(roundtable, "CLI_PIPE_DRAIN_SECONDS", 0.5):
            result = roundtable.run_cli(
                "Test CLI", PYTHON_CMD, ["-c", parent], timeout=25, stream_events=True
            )
        elapsed = time.monotonic() - started
        self.assertIsNotNone(result)
        self.addCleanup(kill_quietly, int(result.stdout.splitlines()[0]))
        self.assertLess(elapsed, 15)
        self.assertEqual(result.returncode, 0)
        self.assertIn("answer done", result.stdout)

    def test_timeout_is_reported_as_timeout_with_partial_output(self):
        code = "import time; print('half way', flush=True); time.sleep(30)"
        result = roundtable.run_cli(
            "Test CLI", PYTHON_CMD, ["-c", code], timeout=3, stream_events=True
        )
        text = roundtable._finalize_cli_result("Test CLI", result)
        self.assertIn("실행 시간 초과", text)   # run_step이 TIMED_OUT으로 분류하는 표시
        self.assertIn("half way", text)
        self.assertNotIn("실행하지 못함", text)

    def test_cli_that_exits_before_reading_the_prompt_reports_its_own_error(self):
        code = "import sys; sys.stderr.write('real failure reason'); sys.exit(3)"
        result = roundtable.run_cli(
            "Test CLI", PYTHON_CMD, ["-c", code], timeout=30,
            input_text="x" * 5_000_000, stream_events=True,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("real failure reason", result.stderr)


# ──────────────────────────────────────────────────────────────
# 5. 검증·점검 프로세스의 시간 초과는 손자까지 끝낸다
# ──────────────────────────────────────────────────────────────
class ProcessTreeTimeoutTests(unittest.TestCase):
    def test_timeout_ends_grandchildren_that_hold_the_output_pipes(self):
        """npm.cmd → node처럼 손자가 파이프를 물고 있어도 워커가 멈추지 않는다."""
        parent = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
            " stdout=sys.stdout, stderr=sys.stderr)\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(30)\n"
        )
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            roundtable.run_process_tree([sys.executable, "-c", parent], timeout=3)
        self.assertLess(time.monotonic() - started, 15)
        output = roundtable.decode_cli_output(caught.exception.output).split()
        self.assertTrue(output, "손자 PID를 읽지 못했다 — 트리가 끝나지 않아 출력이 남지 않았다")
        grandchild = int(output[0])
        self.addCleanup(kill_quietly, grandchild)
        if os.name == "nt":
            listing = subprocess.run(
                ["tasklist", "/FI", f"PID eq {grandchild}", "/NH"],
                capture_output=True, text=True, errors="replace",
            ).stdout
            self.assertNotIn(str(grandchild), listing)

    def test_validation_output_keeps_stdout_and_stderr(self):
        """unittest는 실패 내용을 stderr에 쓴다 — stdout이 있다고 버리면 안 된다."""
        code = "import sys; print('collected 3 items'); sys.stderr.write('FAILED test_x'); sys.exit(1)"
        original = roundtable.CONTROL["stopped"]
        roundtable.CONTROL["stopped"] = False
        self.addCleanup(roundtable.CONTROL.__setitem__, "stopped", original)
        with tempfile.TemporaryDirectory() as root, patch.object(
            roundtable, "detect_validation_commands",
            return_value=[("unit", [sys.executable, "-c", code])],
        ):
            [result] = roundtable.run_project_validation(Path(root))
        self.assertFalse(result["ok"])
        self.assertIn("collected 3 items", result["output"])
        self.assertIn("FAILED test_x", result["output"])


# ──────────────────────────────────────────────────────────────
# 3·8 + 마스킹·비용: run_step
# ──────────────────────────────────────────────────────────────
@patch.object(roundtable, "render_html_snapshot")
@patch.object(roundtable, "add_runtime_event")
@patch.object(roundtable, "build_memory_context", return_value="")
@patch.object(roundtable, "load_team_prompt", return_value="공통 지침")
@patch.object(roundtable, "save_state")
class RunStepTests(BackendTestBase):
    def setUp(self):
        super().setUp()
        roundtable.STATE.update(mode="discussion", discussion_project_access="read")

    def run_discussion_step(self, agent="codex"):
        with patch.object(roundtable, "read_configured_project_path", return_value=str(self.workspace)):
            return roundtable.run_step(agent, "분석", "분석해라", "discussion", expected_session_id=SESSION_ID)

    def test_stop_while_preparing_the_prompt_does_not_launch_the_cli(self, *_):
        calls = []

        def stop_during_prompt_build():
            roundtable.CONTROL["stopped"] = True   # /stop이 프롬프트 작성 중에 들어온다
            return "공통 지침"

        with patch.object(roundtable, "load_team_prompt", side_effect=stop_during_prompt_build), \
             patch.object(roundtable, "add_message", return_value=True), \
             patch.dict(roundtable.ASK_FUNCS, {"codex": lambda p, m: calls.append(m) or "완료"}):
            succeeded = self.run_discussion_step()
        self.assertFalse(succeeded)
        self.assertEqual(calls, [])
        self.assertIsNone(roundtable.STATE["active_agent"])

    def test_blocked_workspace_keeps_the_step_and_the_pending_intervention(self, *_):
        roundtable.CONTROL["intervention_queue"] = [
            {"intent": "question", "targets": ["codex"], "cli_mode": "discussion"}
        ]
        roundtable.CONTROL["intervention_pending"] = True
        with patch.object(roundtable, "read_configured_project_path",
                          return_value="C:\\definitely\\missing\\folder"), \
             patch.object(roundtable, "add_message", return_value=True), \
             patch.dict(roundtable.ASK_FUNCS, {"codex": lambda p, m: self.fail("CLI가 실행됐다")}):
            # 예전에는 차단 메시지 문자열(참)을 돌려줘 워커가 단계를 넘겼다.
            self.assertIs(roundtable.run_step("codex", "분석", "분석해라", "discussion"), False)
            roundtable.process_pending_intervention(SESSION_ID)
        queue = roundtable.CONTROL["intervention_queue"]
        self.assertEqual([item["intent"] for item in queue], ["question"])
        self.assertTrue(queue[0]["retry_blocked"])

    def test_agent_output_is_masked_but_token_counts_survive(self, *_):
        google_key = "AIza" + "B" * 35
        answer = f"설정: OPENAI_API_KEY=sk-live-abcdef1234567890 / {google_key} / max_tokens: 4096"
        with patch.object(roundtable, "add_message", return_value=True) as add_message, \
             patch.dict(roundtable.ASK_FUNCS, {"codex": lambda p, m: answer}):
            self.assertTrue(self.run_discussion_step())
        text = add_message.call_args.args[2]
        self.assertNotIn("sk-live-abcdef1234567890", text)
        self.assertNotIn(google_key, text)
        self.assertIn("max_tokens: 4096", text)

    def test_claude_retry_cost_is_added_to_the_first_run(self, *_):
        roundtable.STATE["enabled_agents"] = ["claude"]

        def ask_claude_with_retry(prompt, mode, event_callback):
            event_callback({"kind": "usage", "text": "Claude 응답 및 사용량 집계 완료", "cost_usd": 0.01})
            event_callback({"kind": "status", "text": "불완전한 도구 요청 응답 감지 · 자동 교정 재시도"})
            event_callback({"kind": "usage", "text": "Claude 응답 및 사용량 집계 완료", "cost_usd": 0.02})
            return "완결된 답변"

        with patch.object(roundtable, "add_message", return_value=True) as add_message, \
             patch.dict(roundtable.ASK_FUNCS, {"claude": ask_claude_with_retry}):
            self.assertTrue(self.run_discussion_step("claude"))
        self.assertAlmostEqual(add_message.call_args.args[3]["actual_cost_usd"], 0.03)


# ──────────────────────────────────────────────────────────────
# 7 + 빠른 질문 완료 + 프로필 중복: worker_loop
# ──────────────────────────────────────────────────────────────
@patch.object(roundtable, "render_html_snapshot")
@patch.object(roundtable, "add_runtime_event")
@patch.object(roundtable, "save_state")
class WorkerLoopTests(BackendTestBase):
    def test_goal_limits_are_measured_from_the_first_cycle(self, *_):
        roundtable.STATE.update(
            mode="coding", coding_stage="planning", plan_approved=True,
            enabled_agents=["claude", "codex"],   # 코딩 파이프라인은 구현 담당 Claude Code가 필요하다
            step_index=len(roundtable.steps_for_mode("coding", ["claude", "codex"])),
            coding_started_at=time.time() - 3 * 3600,   # 계획·승인 단계에서 3시간을 보냈다
            total_est_tokens=500_000,                    # 계획 단계에서 쓴 토큰
        )

        def stop_after_first_turn(*_args, **_kwargs):
            roundtable.CONTROL["stopped"] = True
            return False

        with patch.object(roundtable, "run_step", side_effect=stop_after_first_turn) as run_step, \
             patch.object(roundtable, "update_profile"):
            roundtable.worker_loop(SESSION_ID)
        run_step.assert_called_once()   # 한도 때문에 사이클 1이 시작하자마자 끝나지 않는다
        self.assertEqual(roundtable.STATE["coding_stage"], "iteration")
        self.assertIsNone(roundtable.coding_limit_reason(roundtable.STATE))

    def test_quick_mode_session_is_marked_finished(self, *_):
        roundtable.STATE.update(mode="quick")
        with patch.object(roundtable, "run_step", return_value=True), \
             patch.object(roundtable, "update_profile"):
            roundtable.worker_loop(SESSION_ID)
        self.assertTrue(roundtable.STATE["finished"])
        self.assertEqual(roundtable.STATE["workflow_status"], workflow.COMPLETED)

    def test_profile_is_not_appended_again_when_a_finished_session_ends_again(self, *_):
        roundtable.STATE.update(
            mode="discussion",
            step_index=len(roundtable.steps_for_mode("discussion", ["codex"])),
            messages=[{"agent": "codex", "phase": "최종 보고", "text": "고유한 보고 내용"}],
        )
        profiles = {agent: self.workspace / f"{agent}.md" for agent in roundtable.PROFILE_PATHS}
        with patch.dict(roundtable.PROFILE_PATHS, profiles):
            roundtable.worker_loop(SESSION_ID)   # 완료
            roundtable.worker_loop(SESSION_ID)   # 완료 뒤 개입으로 다시 끝남
        self.assertEqual(profiles["codex"].read_text(encoding="utf-8").count("고유한 보고 내용"), 1)


# ──────────────────────────────────────────────────────────────
# 6. 세션 파일 저장 실패가 save_state 밖으로 새지 않는다
# ──────────────────────────────────────────────────────────────
class SaveStateTests(unittest.TestCase):
    def test_session_file_write_failure_does_not_escape_save_state(self):
        real_write = roundtable.atomic_write_json

        def session_file_locked(path, state):
            if path != roundtable.STATE_PATH:
                raise roundtable.StateSaveError(f"{path.name} 저장 실패: [WinError 5] 액세스가 거부되었습니다")
            real_write(path, state)

        self.addCleanup(roundtable.LAST_SAVE_ERROR.__setitem__, "message", "")
        with patch.object(roundtable, "atomic_write_json", side_effect=session_file_locked):
            roundtable.save_state(roundtable.new_state())   # 예전에는 여기서 예외가 새서 워커가 죽었다
        self.assertIn("저장 실패", roundtable.LAST_SAVE_ERROR["message"])


# ──────────────────────────────────────────────────────────────
# 9·10·11. 역할 범위의 폴더 항목, 작업 폴더 경로, Windows 명령 분해
# ──────────────────────────────────────────────────────────────
class PathHandlingTests(unittest.TestCase):
    def test_new_folders_are_not_role_scope_violations(self):
        state = {"agent_roles": {"codex": "backend"}}
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            before, _ = roundtable.snapshot_project_tree(root)
            (root / "services").mkdir()
            (root / "services" / "api.py").write_text("x = 1\n", encoding="utf-8")
            (root / "dist").mkdir()
            (root / "styles").mkdir()
            (root / "styles" / "site.css").write_text("a{}\n", encoding="utf-8")
            after, _ = roundtable.snapshot_project_tree(root)
        changed = roundtable.compare_project_snapshots(before, after)
        self.assertIn({"path": "services/", "change": "생성"}, changed)
        # 폴더는 위반이 아니고, 담당 밖 파일은 여전히 위반이다.
        self.assertEqual(roundtable.role_scope_violations("codex", changed, state), ["styles/site.css"])

    def test_workspace_access_needs_an_absolute_folder_path(self):
        with patch.object(roundtable, "save_project_path") as save_project_path:
            for value in ("", "   ", ".", "relative\\folder"):
                with self.subTest(value=value):
                    self.assertIn("error", roundtable.update_workspace_selection(value, "write"))
        save_project_path.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows 경로 분해 규칙")
    def test_windows_command_paths_keep_their_backslashes(self):
        self.assertEqual(
            roundtable.command_parts(r"C:\tools\codex.cmd --json"),
            [r"C:\tools\codex.cmd", "--json"],
        )
        self.assertEqual(
            roundtable.command_parts(r'"C:\Program Files\Codex\codex.cmd" --profile work'),
            [r"C:\Program Files\Codex\codex.cmd", "--profile", "work"],
        )


class RedactionPatternTests(unittest.TestCase):
    def test_google_keys_are_masked_and_token_counts_are_not(self):
        self.assertNotIn("AIza" + "C" * 35, clicheck.redact("key " + "AIza" + "C" * 35))
        for count in ("input_tokens: 1200", "max_tokens=4096", "Tokens: 12,000"):
            with self.subTest(count=count):
                self.assertEqual(clicheck.redact(count), count)


if __name__ == "__main__":
    unittest.main()
