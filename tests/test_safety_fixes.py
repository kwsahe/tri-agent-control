"""안전 수정 회귀 테스트 — 승인 우회, 롤백 손실, 체크포인트 덮어쓰기, 중단 처리, 임포트 부작용.

모든 파일 조작은 TemporaryDirectory 안에서만 한다.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isolation  # noqa: F401  (실제 데이터 폴더 격리 — 반드시 roundtable보다 먼저)
import roundtable

SESSION_ID = "20260101_000000_000020"


class SafetyTestBase(unittest.TestCase):
    def setUp(self):
        self.original_state = roundtable.STATE
        self.original_control = dict(roundtable.CONTROL)
        roundtable.STATE = roundtable.new_state()
        roundtable.STATE["id"] = SESSION_ID
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.workspace = root / "project"
        self.memory = root / "memory"
        self.workspace.mkdir()
        self.memory.mkdir()

    def tearDown(self):
        roundtable.STATE = self.original_state
        roundtable.CONTROL.clear()
        roundtable.CONTROL.update(self.original_control)
        self._tmp.cleanup()


# ──────────────────────────────────────────────────────────────
# 중단 처리
# ──────────────────────────────────────────────────────────────
class CancelFlagTests(SafetyTestBase):
    def test_a_new_worker_clears_the_previous_cancel(self):
        """중단 뒤 새 실행에서 진짜 실패가 "사용자 중단"으로 묻히지 않아야 한다."""
        roundtable.CONTROL.update(worker_running=False, user_cancelled=True)
        with patch.object(roundtable, "worker_loop"), patch.object(roundtable, "save_state"):
            roundtable.start_worker_if_needed()
        self.assertFalse(roundtable.CONTROL["user_cancelled"])


# ──────────────────────────────────────────────────────────────
# 롤백 손실 · 체크포인트 덮어쓰기
# ──────────────────────────────────────────────────────────────
@unittest.skipUnless(shutil.which("git"), "git이 없으면 롤백을 검증할 수 없다")
class RollbackTests(SafetyTestBase):
    def write(self, name: str, data: bytes) -> Path:
        path = self.workspace / name
        path.write_bytes(data)
        return path

    def checkpoint(self, before: dict, changed: list[dict]) -> str:
        diff = roundtable.build_turn_diff(self.workspace, before, changed)
        with patch.object(roundtable, "MEMORY_DIR", self.memory):
            return roundtable.save_turn_checkpoint(SESSION_ID, 7, "codex", diff, workspace=self.workspace)

    def rollback(self, checkpoint: str, paths: list[str]) -> dict:
        with patch.object(roundtable, "MEMORY_DIR", self.memory), \
             patch.object(roundtable, "read_configured_project_path", return_value=str(self.workspace)), \
             patch.object(roundtable, "add_runtime_event"):
            return roundtable.rollback_checkpoint_files(checkpoint, paths)

    def test_lf_crlf_and_no_final_newline_files_are_restored_byte_for_byte(self):
        originals = {
            "lf.txt": b"a\nb\nc\n",
            "crlf.txt": b"a\r\nb\r\nc\r\n",
            "nofinal.txt": b"a\nb",
        }
        for name, data in originals.items():
            self.write(name, data)
        before = roundtable.capture_project_texts(self.workspace)
        self.write("lf.txt", b"a\nB\nc\n")
        self.write("crlf.txt", b"a\r\nB\r\nc\r\n")
        self.write("nofinal.txt", b"a\nb\nc")
        checkpoint = self.checkpoint(before, [{"path": n, "change": "수정"} for n in originals])

        result = self.rollback(checkpoint, list(originals))

        self.assertTrue(result.get("success"), result)
        for name, data in originals.items():
            self.assertEqual((self.workspace / name).read_bytes(), data, name)

    def test_created_file_is_removed_and_deleted_file_comes_back(self):
        self.write("gone.txt", b"keep me\n")
        before = roundtable.capture_project_texts(self.workspace)
        (self.workspace / "gone.txt").unlink()
        self.write("new.txt", b"fresh\n")
        checkpoint = self.checkpoint(before, [
            {"path": "gone.txt", "change": "삭제"},
            {"path": "new.txt", "change": "생성"},
        ])

        result = self.rollback(checkpoint, ["gone.txt", "new.txt"])

        self.assertTrue(result.get("success"), result)
        self.assertEqual((self.workspace / "gone.txt").read_bytes(), b"keep me\n")
        self.assertFalse((self.workspace / "new.txt").exists())

    def test_uncaptured_large_file_is_refused_not_emptied(self):
        """512KB를 넘어 변경 전 내용이 없는 파일은 되돌리기를 거부하고 그대로 둔다."""
        big = self.write("big.txt", b"line\n" * 130_000)   # 650KB
        self.write("small.txt", b"x\n")
        before = roundtable.capture_project_texts(self.workspace)
        self.assertNotIn("big.txt", before)
        big.write_bytes(b"line\n" * 129_999 + b"changed\n")
        self.write("small.txt", b"y\n")
        changed_big = big.read_bytes()
        checkpoint = self.checkpoint(before, [
            {"path": "big.txt", "change": "수정"},
            {"path": "small.txt", "change": "수정"},
        ])

        result = self.rollback(checkpoint, ["big.txt"])

        self.assertIn("error", result)
        self.assertIn("big.txt", result["error"])
        self.assertEqual(big.read_bytes(), changed_big)
        # 같은 턴의 다른 파일은 여전히 되돌릴 수 있다.
        self.assertTrue(self.rollback(checkpoint, ["small.txt"]).get("success"))
        self.assertEqual((self.workspace / "small.txt").read_bytes(), b"x\n")

    def test_diff_limit_never_cuts_a_file_in_half(self):
        self.write("a.txt", b"1\n" * 50)
        self.write("b.txt", b"2\n" * 50)
        before = roundtable.capture_project_texts(self.workspace)
        self.write("a.txt", b"3\n" * 50)
        self.write("b.txt", b"4\n" * 50)
        changed = [{"path": "a.txt", "change": "수정"}, {"path": "b.txt", "change": "수정"}]
        full = roundtable.build_turn_diff(self.workspace, before, changed)
        limited = roundtable.build_turn_diff(self.workspace, before, changed, max_chars=len(full) - 10)
        self.assertEqual(roundtable.checkpoint_patch_paths(limited), {"a.txt"})
        self.assertTrue(full.startswith(limited))

    def test_checkpoints_from_the_same_step_do_not_overwrite_each_other(self):
        with patch.object(roundtable, "MEMORY_DIR", self.memory):
            first = roundtable.save_turn_checkpoint(SESSION_ID, 7, "codex", "--- a/x\n", workspace=self.workspace)
            second = roundtable.save_turn_checkpoint(SESSION_ID, 7, "codex", "--- a/y\n", workspace=self.workspace)
        self.assertNotEqual(first, second)
        self.assertEqual(Path(first).read_text(encoding="utf-8"), "--- a/x\n")


# ──────────────────────────────────────────────────────────────
# 승인 우회
# ──────────────────────────────────────────────────────────────
@patch.object(roundtable, "render_html_snapshot")
@patch.object(roundtable, "add_message", return_value=True)
@patch.object(roundtable, "add_runtime_event")
@patch.object(roundtable, "build_memory_context", return_value="")
@patch.object(roundtable, "load_team_prompt", return_value="공통 지침")
@patch.object(roundtable, "save_state")
class PlanApprovalGateTests(SafetyTestBase):
    def setUp(self):
        super().setUp()
        roundtable.STATE.update(
            topic="승인 테스트", mode="coding", workspace_access="write",
            discussion_project_access="write", enabled_agents=["codex"],
        )
        roundtable.CONTROL.update(
            stopped=False, paused=False, approval_requested=False,
            awaiting_approval=False, intervention_pending=False,
        )

    def run_write_turn(self) -> list[tuple[str, str]]:
        calls = []
        with patch.dict(roundtable.ASK_FUNCS, {"codex": lambda p, m: calls.append((p, m)) or "완료"}), \
             patch.object(roundtable, "read_configured_project_path", return_value=str(self.workspace)):
            roundtable.run_step("codex", "작업 수행", "구현해라", "coding", expected_session_id=SESSION_ID)
        return calls

    def test_write_turn_runs_read_only_until_the_plan_is_approved(self, *_):
        roundtable.STATE["plan_approved"] = False
        [(prompt, cli_mode)] = self.run_write_turn()
        self.assertEqual(cli_mode, "discussion")
        self.assertIn("승인하지 않았다", prompt)

        roundtable.STATE["plan_approved"] = True
        [(_prompt, cli_mode)] = self.run_write_turn()
        self.assertEqual(cli_mode, "coding")

    def test_worker_asks_again_instead_of_running_an_unapproved_write_step(self, *_):
        # 계획 단계에는 쓰기 단계가 없다 — 승인 없이 사이클의 구현 단계에 도달한 상황.
        roundtable.STATE.update(enabled_agents=["claude", "codex"], coding_stage="iteration",
                                coding_cycle=1, coding_cycle_step=0, plan_approved=False)
        with patch.object(roundtable, "run_step") as run_step, \
             patch.object(roundtable, "wait_for_user_approval", return_value=False) as wait, \
             patch.object(roundtable, "update_profile"):
            roundtable.worker_loop(SESSION_ID)
        run_step.assert_not_called()
        wait.assert_called_once()
        self.assertTrue(roundtable.CONTROL["approval_requested"])

    def test_approving_saves_the_approval_in_state(self, *_):
        roundtable.STATE["plan_approved"] = False
        roundtable.CONTROL["approval_requested"] = True
        with patch.object(roundtable, "persist_intervention_queue_locked"), \
             patch.object(roundtable, "start_worker_if_needed"):
            roundtable.approve_pending_work()
        self.assertTrue(roundtable.STATE["plan_approved"])

    def test_approve_without_a_pending_request_does_not_approve_the_plan(self, *_):
        roundtable.STATE["plan_approved"] = False
        with patch.object(roundtable, "persist_intervention_queue_locked"), \
             patch.object(roundtable, "start_worker_if_needed"):
            roundtable.approve_pending_work()
        self.assertFalse(roundtable.STATE["plan_approved"])

    def test_approval_survives_a_restart_and_old_sessions_are_migrated(self, *_):
        planning = roundtable.normalize_state({"mode": "coding", "coding_stage": "planning"})
        iteration = roundtable.normalize_state({"mode": "coding", "coding_stage": "iteration"})
        rejected = roundtable.normalize_state(
            {"mode": "coding", "coding_stage": "iteration", "plan_approved": False}
        )
        self.assertFalse(planning["plan_approved"])
        self.assertTrue(iteration["plan_approved"])
        self.assertFalse(rejected["plan_approved"])

    def test_switching_into_coding_mode_requires_a_new_approval(self, *_):
        roundtable.STATE.update(mode="discussion", plan_approved=True, enabled_agents=["claude", "codex"])
        with patch.object(roundtable, "read_configured_project_path", return_value=str(self.workspace)), \
             patch.object(roundtable, "start_worker_if_needed"):
            result = roundtable.switch_mode("coding")
        self.assertTrue(result.get("success"), result)
        self.assertFalse(roundtable.STATE["plan_approved"])


# ──────────────────────────────────────────────────────────────
# 임포트 부작용 (테스트 격리)
# ──────────────────────────────────────────────────────────────
class ImportSideEffectTests(unittest.TestCase):
    def test_importing_roundtable_does_not_touch_user_data(self):
        """임포트만으로 실제 상태·세션·작업 폴더 파일을 열거나 목록을 읽지 않는다.

        예전에는 임포트 때 load_state()가 실제 roundtable_state.json을 읽고, 정규화 결과가
        다르면 다시 저장했다. 감사 훅이 그런 접근을 **막기** 때문에 회귀해도 데이터는 안전하다.
        """
        root = Path(roundtable.__file__).resolve().parent
        guarded = [
            os.path.normcase(str(root / name))
            for name in (
                "roundtable_state.json", "PROJECT_PATH.txt", "roundtable_log.md",
                "roundtable.html", "sessions", "roundtable_memory",
            )
        ]
        code = (
            "import os, sys\n"
            f"guarded = {guarded!r}\n"
            "def hook(event, args):\n"
            "    if event in ('open', 'os.listdir', 'os.scandir') and args and args[0] is not None\\\n"
            "            and not isinstance(args[0], int):\n"
            "        path = os.path.normcase(os.path.abspath(os.fsdecode(os.fspath(args[0]))))\n"
            "        if any(path == g or path.startswith(g + os.sep) for g in guarded):\n"
            "            raise PermissionError('import touched user data: ' + path)\n"
            "sys.addaudithook(hook)\n"
            "import roundtable\n"
            "print('imported, state keys:', len(roundtable.STATE))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=root, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        self.assertIn("imported, state keys: 0", result.stdout)


if __name__ == "__main__":
    unittest.main()
