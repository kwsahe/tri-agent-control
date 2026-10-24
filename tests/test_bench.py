"""평가 하니스(bench/) 회귀 테스트. 실제 모델 CLI는 부르지 않고 가짜 응답으로 워커를 끝까지 돌린다."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isolation  # noqa: F401  (실제 데이터 폴더 격리 — 반드시 roundtable보다 먼저)
import roundtable
from bench import run_bench

TASK = run_bench.TASKS_DIR / "e2-chunked"


class TaskSetTests(unittest.TestCase):
    def test_every_task_fails_at_start_and_its_solution_passes(self):
        expected, problems = run_bench.check_tasks(run_bench.task_dirs())
        self.assertEqual(problems, [])
        self.assertEqual(len(expected), 8)


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.task, self.workspace = base / "task", base / "work"
        (self.task / "hidden").mkdir(parents=True)
        self.workspace.mkdir()
        (self.workspace / "mod.py").write_text("def double(x):\n    return x * 2\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def hidden(self, body: str) -> None:
        (self.task / "hidden" / "test_hidden.py").write_text(body, encoding="utf-8")

    def test_failed_subtests_count_as_one_failed_test(self):
        self.hidden(
            "import unittest\nfrom mod import double\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertEqual(double(2), 4)\n"
            "    def test_many(self):\n"
            "        for x in (1, 2, 3):\n"
            "            with self.subTest(x=x):\n                self.assertEqual(double(x), 0)\n"
        )
        self.assertEqual(run_bench.score(self.workspace, self.task), {"total": 2, "passed": 1})
        self.assertFalse((self.workspace / "_hidden_tests").exists())   # 작업 폴더는 그대로

    def test_a_missing_function_scores_zero(self):
        self.hidden("import unittest\nfrom mod import triple\n\nclass T(unittest.TestCase):\n"
                    "    def test_x(self):\n        self.assertEqual(triple(1), 3)\n")
        self.assertEqual(run_bench.score(self.workspace, self.task)["passed"], 0)


def fake_agents(solution: Path):
    """계획 → 승인 → 구현(정답 풀이 복사) → 테스트 → 검토 PASS로 흘러가는 가짜 응답."""
    def claude(prompt, mode, event_callback=None):
        if mode == "coding":
            shutil.copytree(solution, roundtable.cli_working_directory("coding"), dirs_exist_ok=True)
            return "변경: listtools/__init__.py\n결과: chunked 추가"
        return f"파일: listtools/__init__.py\n변경: chunked 추가\n테스트: tests/\n{roundtable.APPROVAL_TOKEN}"

    def antigravity(prompt, mode, event_callback=None):
        return "테스트: 추가 없음\n결과: 공개 테스트 통과"

    def codex(prompt, mode, event_callback=None):
        return "지적 없음\nREVIEW: PASS"

    return {"claude": claude, "antigravity": antigravity, "codex": codex}


@unittest.skipUnless(shutil.which("git"), "git이 없으면 세션 worktree를 만들 수 없다")
class RunOneTests(unittest.TestCase):
    def setUp(self):
        self.original_state = dict(roundtable.STATE)
        self.original_control = dict(roundtable.CONTROL)
        self._tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self._tmp.name)

    def tearDown(self):
        roundtable.CONTROL["stopped"] = True
        roundtable.STATE.clear()
        roundtable.STATE.update(self.original_state)
        roundtable.CONTROL.clear()
        roundtable.CONTROL.update(self.original_control)
        self._tmp.cleanup()

    def run_arm(self, arm: str) -> dict:
        with patch.dict(roundtable.ASK_FUNCS, fake_agents(TASK / "solution")), \
             patch.object(roundtable, "add_runtime_event"):
            return run_bench.run_one(TASK, arm, self.parent / arm, expected_hidden=6, timeout=60, poll=0.1)

    def test_pipeline_run_is_approved_committed_and_scored(self):
        record = self.run_arm("pipeline")
        self.assertEqual(record["outcome"], "finished", record)
        self.assertTrue(record["success"], record)
        self.assertEqual(record["calls_by_agent"], {"claude": 2, "antigravity": 1, "codex": 1})
        self.assertEqual(record["commits"], 1)
        self.assertEqual(record["cycles"], 1)
        self.assertGreater(record["lines_changed"], 0)
        self.assertIn("worktrees", record["workspace"])   # 사용자 폴더가 아니라 세션 worktree

    def test_solo_run_only_calls_claude(self):
        record = self.run_arm("solo")
        self.assertEqual(record["outcome"], "finished", record)
        self.assertTrue(record["success"], record)
        self.assertEqual(set(record["calls_by_agent"]), {"claude"})

    def test_report_summarizes_each_arm_and_task(self):
        records = [
            {"task": "e2-chunked", "arm": "solo", "success": False, "hidden_passed": 3, "hidden_total": 6,
             "seconds": 30.0, "calls": 2, "est_tokens": 1000, "actual_tokens": 0, "cost_usd": 0.0},
            {"task": "e2-chunked", "arm": "pipeline", "success": True, "hidden_passed": 6, "hidden_total": 6,
             "seconds": 90.0, "calls": 4, "est_tokens": 3000, "actual_tokens": 2500, "cost_usd": 0.12},
        ]
        table = run_bench.report(records)
        self.assertIn("| Claude 단독 | 1 | 0% | 50% | 30초 | 2.0 | 1,000 | $0.00 |", table)
        self.assertIn("| 3명 파이프라인 | 1 | 100% | 100% | 90초 | 4.0 | 2,500 | $0.12 |", table)
        self.assertIn("| e2-chunked | 0/1 | 1/1 |", table)
