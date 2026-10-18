"""4. CLI 사전 점검 (실제 CLI 없이 mock으로) · 9. 대용량 세션 성능.

실제 CLI가 설치되지 않은 환경에서도 회귀를 잡을 수 있도록 프로세스 실행은 전부
주입한다.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import isolation  # noqa: F401  (실제 데이터 폴더 격리 — 반드시 roundtable보다 먼저)
import roundtable
from triagent.domain import clicheck
from triagent.domain.clicheck import CommandResult, StepStatus


def ok(stdout="ok"):
    return CommandResult(returncode=0, stdout=stdout)


class CodexFlagDetectionTests(unittest.TestCase):
    """옵션은 추측하지 않고 `codex exec --help` 출력에서 고른다."""

    # 이 기계에 설치된 0.148.0의 실제 help 발췌.
    HELP_0_148 = """
      -s, --sandbox <SANDBOX_MODE>
              [possible values: read-only, workspace-write, danger-full-access]
          --approve-for-me
              Route approval requests through automatic review using the workspace-write sandbox
          --dangerously-bypass-approvals-and-sandbox
      -C, --cd <DIR>
    """

    HELP_OLDER = """
      -s, --sandbox <SANDBOX_MODE>
          --full-auto
      -a, --ask-for-approval <APPROVAL_POLICY>
    """

    def test_0_148_uses_approve_for_me_alone(self):
        args = clicheck.detect_codex_write_flags(self.HELP_0_148)
        self.assertEqual(args, ["--approve-for-me"])
        # 실제 CLI가 거부하는 조합이므로 절대 같이 넘기면 안 된다:
        # "error: the argument '--sandbox <SANDBOX_MODE>' cannot be used with '--approve-for-me'"
        self.assertNotIn("--sandbox", args)

    def test_older_versions_use_full_auto(self):
        self.assertEqual(clicheck.detect_codex_write_flags(self.HELP_OLDER), ["--full-auto"])

    def test_ask_for_approval_fallback(self):
        help_text = "-s, --sandbox <SANDBOX_MODE>\n-a, --ask-for-approval <POLICY>"
        self.assertEqual(
            clicheck.detect_codex_write_flags(help_text),
            ["--sandbox", "workspace-write", "--ask-for-approval", "never"],
        )

    def test_unknown_version_falls_back_to_sandbox_only(self):
        self.assertEqual(
            clicheck.detect_codex_write_flags("no useful flags here"),
            ["--sandbox", "workspace-write"],
        )

    def test_empty_help_does_not_crash(self):
        self.assertTrue(clicheck.detect_codex_write_flags(""))


class StagedCheckTests(unittest.TestCase):
    def run_check(self, **kwargs):
        defaults = dict(
            key="codex", label="Codex", command="codex",
            runner=lambda args: ok("codex-cli 0.148.0"),
        )
        defaults.update(kwargs)
        return clicheck.run_staged_check(**defaults)

    def test_missing_command_fails_at_the_first_step(self):
        report = self.run_check(runner=lambda args: CommandResult(returncode=127, not_found=True))
        self.assertFalse(report.ok)
        self.assertEqual(report.first_failure.name, "command")
        # 뒤 단계는 실행하지 않고 skipped.
        self.assertEqual(report.step("version").status, StepStatus.SKIPPED)
        self.assertIn("PATH", report.suggested_action())

    def test_version_failure_is_reported_with_the_step_name(self):
        report = self.run_check(runner=lambda args: CommandResult(returncode=1, stderr="boom"))
        self.assertEqual(report.first_failure.name, "version")
        self.assertIn("boom", report.first_failure.detail)
        self.assertFalse(report.can_read)

    def test_timeout_is_reported_as_such(self):
        report = self.run_check(runner=lambda args: CommandResult(returncode=124, timed_out=True))
        self.assertIn("시간 초과", report.first_failure.detail)

    def test_read_success_and_write_failure_are_separate_states(self):
        """Codex 실사용에서 관찰된 상태: 읽기는 되는데 쓰기는 실패."""
        report = self.run_check(
            read_probe=lambda: (True, "읽었습니다"),
            write_probe=lambda: (False, "파일이 생성되지 않았습니다"),
            check_write=True,
        )
        self.assertTrue(report.can_read)
        self.assertFalse(report.can_write)
        # 쓰기가 실패했으면 "연결됨"으로 표시하지 않는다.
        self.assertFalse(report.ok)
        self.assertEqual(report.first_failure.name, "write")
        self.assertTrue(report.suggested_action())

    def test_write_is_skipped_when_the_user_did_not_ask_for_write(self):
        report = self.run_check(
            read_probe=lambda: (True, "읽었습니다"),
            write_probe=lambda: (False, "이건 호출되면 안 된다"),
            check_write=False,
        )
        self.assertTrue(report.ok)          # 건너뛴 단계는 실패가 아니다
        self.assertTrue(report.can_read)
        self.assertFalse(report.can_write)
        self.assertEqual(report.step("write").status, StepStatus.SKIPPED)

    def test_auth_failure_short_circuits_read_and_write(self):
        report = self.run_check(
            auth_probe=lambda: CommandResult(returncode=1, stderr="not logged in"),
            read_probe=lambda: (True, "호출되면 안 된다"),
            check_write=True,
        )
        self.assertEqual(report.first_failure.name, "auth")
        self.assertEqual(report.step("read").status, StepStatus.SKIPPED)
        self.assertIn("login", report.suggested_action().lower())

    def test_all_green_reports_ok_with_both_capabilities(self):
        report = self.run_check(
            read_probe=lambda: (True, "읽음"),
            write_probe=lambda: (True, "씀"),
            check_write=True,
        )
        self.assertTrue(report.ok)
        self.assertTrue(report.can_read)
        self.assertTrue(report.can_write)
        self.assertEqual(report.version, "codex-cli 0.148.0")

    def test_report_dict_is_json_serialisable_for_the_dashboard(self):
        report = self.run_check(read_probe=lambda: (True, "읽음"))
        json.dumps(report.as_dict(), ensure_ascii=False)


class SecretRedactionTests(unittest.TestCase):
    """인증 토큰이 화면이나 로그에 남지 않아야 한다."""

    SECRETS = [
        ("sk-abcdef1234567890abcdef", "sk-"),
        ("ghp_ABCDEFGHIJKLMNOPQRSTUV1234", "ghp_"),
        ("Authorization: Bearer eyJhbGciOi.someMiddlePart.andSignature", "Bearer"),
        ("OPENAI_API_KEY=super-secret-value", "API_KEY"),
        ("ANTHROPIC_AUTH_TOKEN: tok-live-9876543210", "AUTH_TOKEN"),
        ("--api-key hunter2hunter2", "--api-key"),
    ]

    def test_secrets_are_masked(self):
        for raw, _marker in self.SECRETS:
            with self.subTest(raw=raw[:24]):
                cleaned = clicheck.redact(raw)
                self.assertIn("***", cleaned)
                # 값 자체는 남지 않는다.
                self.assertNotIn("super-secret-value", cleaned)
                self.assertNotIn("hunter2hunter2", cleaned)
                self.assertNotIn("abcdef1234567890abcdef", cleaned)

    def test_redaction_is_applied_to_report_output(self):
        report = clicheck.CliReport(key="codex", label="Codex", command="codex")
        report.steps = [clicheck.CheckStep(
            "version", StepStatus.FAILED, detail="failed with OPENAI_API_KEY=leaked-value"
        )]
        payload = report.as_dict()
        self.assertNotIn("leaked-value", json.dumps(payload))

    def test_normal_output_survives_redaction(self):
        self.assertEqual(clicheck.redact("codex-cli 0.148.0"), "codex-cli 0.148.0")


class ProbeIsolationTests(unittest.TestCase):
    """읽기/쓰기 검사는 프로젝트 폴더가 아니라 전용 임시 폴더에서만."""

    def test_probe_workspace_is_under_the_temp_preflight_dir(self):
        directory = roundtable._make_probe_workspace("unit-test")
        try:
            self.assertTrue(directory.is_dir())
            self.assertTrue(
                directory.resolve().is_relative_to(roundtable.PREFLIGHT_DIR.resolve())
            )
            self.assertFalse(
                directory.resolve().is_relative_to(roundtable.ROOT.resolve())
            )
        finally:
            import shutil
            shutil.rmtree(directory, ignore_errors=True)

    def test_probe_directory_is_reset_between_runs(self):
        first = roundtable._make_probe_workspace("unit-test-reset")
        (first / "left-over.txt").write_text("x", encoding="utf-8")
        second = roundtable._make_probe_workspace("unit-test-reset")
        try:
            self.assertEqual(list(second.iterdir()), [])
        finally:
            import shutil
            shutil.rmtree(second, ignore_errors=True)

    def test_write_probe_reports_failure_when_no_file_is_created(self):
        """Codex 0.148.0이 이 기계에서 실제로 보인 동작 (fs sandbox helper 실패)."""
        with patch.object(roundtable, "_run_probe_cli", return_value="apply_patch verification failed"):
            ok_flag, detail = roundtable._cli_write_probe("codex", "codex")
        self.assertFalse(ok_flag)
        self.assertIn("생성되지 않았습니다", detail)

    def test_write_probe_verifies_file_contents_not_just_existence(self):
        def fake_probe(key, command, prompt, directory, write):
            (Path(directory) / "probe_out.txt").write_text("WRONG", encoding="utf-8")
            return "DONE"

        with patch.object(roundtable, "_run_probe_cli", side_effect=fake_probe):
            ok_flag, detail = roundtable._cli_write_probe("codex", "codex")
        self.assertFalse(ok_flag)
        self.assertIn("내용이 다릅니다", detail)

    def test_write_probe_passes_when_content_matches(self):
        def fake_probe(key, command, prompt, directory, write):
            (Path(directory) / "probe_out.txt").write_text(
                "TRIAGENT_WRITE_PROBE_9C21", encoding="utf-8"
            )
            return "DONE"

        with patch.object(roundtable, "_run_probe_cli", side_effect=fake_probe):
            ok_flag, _detail = roundtable._cli_write_probe("codex", "codex")
        self.assertTrue(ok_flag)

    def test_read_probe_requires_the_marker_in_the_response(self):
        with patch.object(roundtable, "_run_probe_cli", return_value="I cannot read files."):
            ok_flag, detail = roundtable._cli_read_probe("codex", "codex")
        self.assertFalse(ok_flag)
        with patch.object(roundtable, "_run_probe_cli",
                          return_value="TRIAGENT_READ_PROBE_7F3A"):
            ok_flag, _ = roundtable._cli_read_probe("codex", "codex")
        self.assertTrue(ok_flag)

    def test_connection_checks_run_without_real_clis(self):
        with patch.object(roundtable, "_probe_runner",
                          return_value=lambda args: CommandResult(returncode=127, not_found=True)), \
             patch("builtins.print"):
            all_ok = roundtable.run_connection_checks(check_write=False, deep=False)
        self.assertFalse(all_ok)
        for key in ("codex", "antigravity", "claude"):
            self.assertFalse(roundtable.CONNECTION_STATUS[key]["ok"])
            self.assertEqual(roundtable.CONNECTION_STATUS[key]["failed_step"], "command")


# ──────────────────────────────────────────────────────────────
# 9. 대용량 세션
# ──────────────────────────────────────────────────────────────
def synthetic_messages(count):
    return [
        {
            "agent": ["codex", "antigravity", "claude"][index % 3],
            "phase": "합성 턴",
            "time": "12:00:00",
            "text": f"합성 메시지 {index} " + ("내용 " * 20),
            "meta": {"est_tokens": 100, "ok": True},
        }
        for index in range(count)
    ]


class LargeSessionTests(unittest.TestCase):
    MESSAGE_COUNT = 1500

    def setUp(self):
        self.original_state = roundtable.STATE
        roundtable.STATE = roundtable.new_state()
        roundtable.STATE["topic"] = "대용량 세션"
        roundtable.STATE["messages"] = synthetic_messages(self.MESSAGE_COUNT)

    def tearDown(self):
        roundtable.STATE = self.original_state

    def test_polling_payload_does_not_carry_every_message(self):
        payload = roundtable.state_json_payload()
        self.assertNotIn("feed_html", payload)
        self.assertEqual(payload["message_count"], self.MESSAGE_COUNT)
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        # 예전에는 이 응답 하나가 361KB였다.
        self.assertLess(size, 120_000, f"폴링 응답이 너무 큽니다: {size:,} bytes")

    def test_full_feed_is_available_on_demand(self):
        payload = roundtable.state_json_payload(include_feed=True)
        self.assertIn("feed_html", payload)
        self.assertIn("합성 메시지 1499", payload["feed_html"])

    def test_initial_render_only_includes_the_recent_window(self):
        html = roundtable.render_dashboard()
        self.assertIn("합성 메시지 1499", html)
        # 첫 메시지는 초기 렌더에 없다 — "이전 메시지 불러오기"로 가져온다.
        self.assertNotIn("합성 메시지 0 ", html)
        self.assertIn("이전 메시지", html)

    def test_incremental_fetch_returns_only_new_messages(self):
        page = roundtable.messages_payload(after="1497", limit="80")
        self.assertEqual(page["count"], 2)
        self.assertEqual(page["from_index"], 1498)
        self.assertEqual(page["to_index"], 1499)
        self.assertIn("합성 메시지 1498", page["messages_html"])
        self.assertNotIn("합성 메시지 1000", page["messages_html"])

    def test_first_load_returns_the_most_recent_page(self):
        page = roundtable.messages_payload(after="-1", limit="50")
        self.assertEqual(page["count"], 50)
        self.assertEqual(page["to_index"], 1499)
        self.assertTrue(page["has_older"])
        self.assertFalse(page["has_newer"])

    def test_older_page_walks_backwards(self):
        page = roundtable.older_messages_payload("", before=1440, limit=40)
        self.assertEqual(page["from_index"], 1400)
        self.assertEqual(page["to_index"], 1439)
        self.assertEqual(page["count"], 40)
        self.assertTrue(page["has_older"])

    def test_page_size_is_capped(self):
        page = roundtable.messages_payload(after="-1", limit="99999")
        self.assertLessEqual(page["count"], roundtable.MAX_MESSAGE_PAGE)

    def test_bad_cursor_values_do_not_crash(self):
        for after in ("abc", "", "-9999", "999999"):
            with self.subTest(after=after):
                page = roundtable.messages_payload(after=after)
                self.assertIn("messages_html", page)

    def test_polling_payload_is_fast_enough_for_a_1_8s_loop(self):
        start = time.perf_counter()
        for _ in range(3):
            roundtable.state_json_payload()
        elapsed = (time.perf_counter() - start) / 3
        self.assertLess(elapsed, 0.5, f"상태 응답이 느립니다: {elapsed:.3f}s")


class ThreadSafetyTests(unittest.TestCase):
    def test_server_is_threading_so_one_slow_request_cannot_block_stop(self):
        import socketserver

        self.assertTrue(issubclass(roundtable.RoundtableServer, socketserver.ThreadingMixIn))

    def test_state_writes_are_atomic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "state.json"
            roundtable.atomic_write_json(target, {"a": 1})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"a": 1})
            # 임시 파일이 남지 않는다.
            leftovers = [p.name for p in Path(temp_dir).iterdir() if p.name != "state.json"]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()


class ConnectionBarTests(unittest.TestCase):
    """대시보드 연결 표시줄이 읽기/쓰기를 따로 보여주는지."""

    def make_report(self, **step_status):
        report = clicheck.CliReport(key="codex", label="Codex", command="codex")
        report.version = "codex-cli 0.148.0"
        report.steps = [
            clicheck.CheckStep(name, status, detail)
            for name, (status, detail) in step_status.items()
        ]
        return report

    def render(self, report):
        original = dict(roundtable.CONNECTION_STATUS)
        try:
            roundtable.CONNECTION_STATUS["codex"] = report.as_dict()
            return roundtable.connection_status_html()
        finally:
            roundtable.CONNECTION_STATUS.clear()
            roundtable.CONNECTION_STATUS.update(original)

    def test_report_dict_keeps_the_label_the_dashboard_needs(self):
        # label이 빠지면 connection_status_html()이 KeyError로 죽는다.
        self.assertIn("label", self.make_report().as_dict())

    def test_read_ok_but_write_failed_is_not_shown_as_connected(self):
        report = self.make_report(
            command=(StepStatus.OK, ""),
            version=(StepStatus.OK, "codex-cli 0.148.0"),
            read=(StepStatus.OK, "읽음"),
            write=(StepStatus.FAILED, "파일이 생성되지 않았습니다"),
        )
        html = self.render(report)
        self.assertIn("읽기 ✓", html)
        self.assertIn("쓰기 ✕", html)
        self.assertIn("❌", html)          # 초록불로 위장하지 않는다
        self.assertIn("[write]", html)     # 실패 단계를 밝힌다

    def test_all_ok_shows_both_capabilities(self):
        report = self.make_report(
            command=(StepStatus.OK, ""),
            version=(StepStatus.OK, "codex-cli 0.148.0"),
            read=(StepStatus.OK, "읽음"),
            write=(StepStatus.OK, "씀"),
        )
        html = self.render(report)
        self.assertIn("✅", html)
        self.assertIn("읽기 ✓", html)
        self.assertIn("쓰기 ✓", html)

    def test_failure_detail_never_leaks_credentials(self):
        report = self.make_report(
            command=(StepStatus.OK, ""),
            version=(StepStatus.FAILED, "auth error OPENAI_API_KEY=leaked-secret-value"),
        )
        html = self.render(report)
        self.assertNotIn("leaked-secret-value", html)
