"""TriAgent 평가 하니스 — 같은 과제를 "Claude 단독"과 "3명 파이프라인"으로 풀게 하고 숨김 테스트로 채점한다.

실제 모델을 부른다 (요금·사용 한도·시간이 든다). 자동 테스트가 아니다.

    python bench/run_bench.py --check                                  # 과제 자체 점검만 (모델 호출 없음)
    python bench/run_bench.py --tasks e1-duration,m1-cart --repeats 1  # 실행
    python bench/run_bench.py --report bench/results/<파일>.jsonl       # 결과표만 다시 만들기

안전:
* 세션·상태 파일·worktree는 테스트와 같은 격리(tests/isolation.py)로 임시 폴더에 둔다.
  사용자 세션, roundtable_state.json, PROJECT_PATH.txt는 그대로다.
* 과제 저장소는 임시 폴더에 새로 만든다. 숨김 테스트는 그 저장소에 넣지 않고, 끝난 뒤 복사본에서만 돌린다.
* 계획 승인은 하니스가 자동으로 한다 (두 방식 모두 같은 조건).
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import isolation  # noqa: E402,F401  (반드시 roundtable보다 먼저 — 실제 데이터 폴더 격리)
import roundtable  # noqa: E402

TASKS_DIR = BENCH / "tasks"
RESULTS_DIR = BENCH / "results"
ARMS = {"solo": ["claude"], "pipeline": ["claude", "antigravity", "codex"]}
ARM_LABELS = {"solo": "Claude 단독", "pipeline": "3명 파이프라인"}
_COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "__pycache__")

# 숨김 테스트를 새 파이썬 프로세스에서 돌린다. subTest가 여러 번 실패해도 테스트 하나로 센다.
_SCORE_SCRIPT = """
import json, unittest
suite = unittest.defaultTestLoader.discover("_hidden_tests", top_level_dir=".")
result = unittest.TestResult()
suite.run(result)
failed = {getattr(test, "test_case", test).id() for test, _ in result.failures + result.errors}
print(json.dumps({"total": result.testsRun, "failed": len(failed)}))
"""


def task_dirs(selected: str = "") -> list[Path]:
    tasks = sorted(path for path in TASKS_DIR.iterdir() if (path / "task.md").is_file())
    if not selected or selected == "all":
        return tasks
    wanted = [name.strip() for name in selected.split(",") if name.strip()]
    unknown = sorted(set(wanted) - {task.name for task in tasks})
    if unknown:
        raise SystemExit(f"모르는 과제: {', '.join(unknown)}")
    return [task for task in tasks if task.name in wanted]


def overlay(source: Path, target: Path) -> None:
    shutil.copytree(source, target, dirs_exist_ok=True)


def score(workspace: Path, task: Path) -> dict:
    """작업 결과의 복사본에 숨김 테스트를 넣어 돌린다. 작업 폴더 자체는 건드리지 않는다."""
    with tempfile.TemporaryDirectory(prefix="triagent-score-") as tmp:
        copy = Path(tmp) / "work"
        shutil.copytree(workspace, copy, ignore=_COPY_IGNORE)
        hidden = copy / "_hidden_tests"
        shutil.copytree(task / "hidden", hidden, dirs_exist_ok=True)
        (hidden / "__init__.py").write_text("", encoding="utf-8")
        try:
            done = subprocess.run([sys.executable, "-c", _SCORE_SCRIPT], cwd=copy, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=180)
            counts = json.loads(done.stdout.strip().splitlines()[-1])
        except (subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
            return {"total": 0, "passed": 0}
    return {"total": counts["total"], "passed": counts["total"] - counts["failed"]}


def visible_tests_pass(workspace: Path) -> bool:
    done = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=workspace,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    return done.returncode == 0


def check_tasks(tasks: list[Path]) -> tuple[dict[str, int], list[str]]:
    """과제마다 시작 상태는 숨김 테스트에 실패하고, 정답 풀이는 숨김·공개 테스트를 모두 통과하는지 본다.

    돌려주는 값: (과제별 숨김 테스트 수, 문제 목록). 문제가 있으면 모델을 부르기 전에 멈춘다.
    """
    expected, problems = {}, []
    for task in tasks:
        with tempfile.TemporaryDirectory(prefix="triagent-check-") as tmp:
            start = Path(tmp) / "start"
            shutil.copytree(task / "repo", start)
            before = score(start, task)
            if before["total"] and before["passed"] == before["total"]:
                problems.append(f"{task.name}: 시작 상태가 이미 숨김 테스트를 통과합니다")
            solved = Path(tmp) / "solved"
            shutil.copytree(task / "repo", solved)
            overlay(task / "solution", solved)
            after = score(solved, task)
            if not after["total"] or after["passed"] != after["total"]:
                problems.append(f"{task.name}: 정답 풀이가 숨김 테스트 {after['passed']}/{after['total']}만 통과합니다")
            if not visible_tests_pass(solved):
                problems.append(f"{task.name}: 정답 풀이가 공개 테스트를 통과하지 못합니다")
            expected[task.name] = after["total"]
    return expected, problems


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout.strip()


def prepare_repo(task: Path, parent: Path) -> tuple[Path, str]:
    repo = parent / task.name
    shutil.copytree(task / "repo", repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "bench@triagent.local")
    _git(repo, "config", "user.name", "TriAgent Bench")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "start")
    return repo, _git(repo, "rev-parse", "HEAD")


def changed_lines(workspace: Path, base: str) -> int:
    try:
        _git(workspace, "add", "-A", "-N")   # 새 파일도 diff에 잡히게 (이 저장소는 하니스 전용)
        numstat = _git(workspace, "diff", "--numstat", base)
    except subprocess.CalledProcessError:
        return 0
    return sum(int(added) + int(deleted) for added, deleted, _path in
               (line.split("\t", 2) for line in numstat.splitlines()) if added.isdigit() and deleted.isdigit())


def _reset_session() -> None:
    with roundtable.STATE_LOCK:
        roundtable.STATE.clear()
        roundtable.STATE.update(roundtable.new_state())
    roundtable.CONTROL.update(
        paused=False, stopped=False, awaiting_approval=False, approval_requested=False,
        approval_requested_by=[], approval_rejected=False, approval_deferred=False,
        intervention_pending=False, intervention_queue=[], worker_start_pending=False,
    )


def _wait_for_end(timeout: float, poll: float) -> str:
    """워커가 끝날 때까지 기다리며 계획 승인을 대신 누른다. 끝난 이유를 돌려준다."""
    started, paused_since = time.time(), None
    while True:
        if roundtable.CONTROL.get("awaiting_approval"):
            roundtable.approve_pending_work()
        with roundtable.STATE_LOCK:
            finished = roundtable.STATE.get("finished")
            asked = roundtable.waiting_for_user_response(roundtable.STATE)
            active = roundtable.STATE.get("active_agent")
        if not roundtable.CONTROL.get("worker_running"):
            return "finished" if finished else "stopped"
        if asked:
            return "asked_user"
        if roundtable.CONTROL.get("paused") and not active:
            paused_since = paused_since or time.time()
            if time.time() - paused_since > 5:
                return "paused"     # 실패 턴 뒤 사람이 '계속'을 눌러야 하는 상태
        else:
            paused_since = None
        if time.time() - started > timeout:
            return "timeout"
        time.sleep(poll)


def _stop_worker() -> None:
    if not roundtable.CONTROL.get("worker_running"):
        return
    roundtable.CONTROL["stopped"] = True
    roundtable.cancel_active_cli_processes()
    deadline = time.time() + 60
    while roundtable.CONTROL.get("worker_running") and time.time() < deadline:
        time.sleep(0.5)


def run_one(task: Path, arm: str, parent: Path, expected_hidden: int,
            timeout: float = 2400, poll: float = 1.0) -> dict:
    repo, base = prepare_repo(task, parent)
    roundtable.save_project_path(repo)
    _reset_session()
    started = time.time()
    error = roundtable.start_topic((task / "task.md").read_text(encoding="utf-8"), "coding", "write", ARMS[arm])
    outcome = "start_error" if error else _wait_for_end(timeout, poll)
    _stop_worker()
    seconds = round(time.time() - started, 1)
    with roundtable.STATE_LOCK:
        state = json.loads(json.dumps(roundtable.STATE, ensure_ascii=False, default=str))
    git_state = state.get("pipeline_git") or {}
    workspace = Path(git_state.get("workspace") or repo)
    result = score(workspace, task)
    turns = [message for message in state.get("messages", [])
             if message.get("agent") in roundtable.AGENT_ORDER and message.get("meta")]
    calls = Counter(message["agent"] for message in turns)
    # coding_cycle은 마지막 사이클이 끝난 뒤 다음 번호로 넘어가 있으므로 구현 턴 수로 센다.
    implement_phase = roundtable.PIPELINE_ROLE_LABELS["implement"]
    cycles = sum(1 for message in turns if str(message.get("phase", "")).startswith(implement_phase))
    return {
        "task": task.name,
        "arm": arm,
        "session": state.get("id", ""),
        "outcome": outcome,
        "error": error or "",
        "stop_reason": state.get("coding_stop_reason", ""),
        "success": result["passed"] == expected_hidden and result["total"] == expected_hidden,
        "hidden_passed": result["passed"],
        "hidden_total": expected_hidden,
        "seconds": seconds,
        "calls": sum(calls.values()),
        "calls_by_agent": dict(calls),
        "cycles": cycles,
        "commits": len(git_state.get("commits", [])),
        "lines_changed": changed_lines(workspace, base),
        "est_tokens": state.get("total_est_tokens", 0),
        "actual_tokens": state.get("total_actual_tokens", 0),
        "cost_usd": state.get("total_actual_cost_usd", 0.0),
        "workspace": str(workspace),
    }


def report(records: list[dict]) -> str:
    """방식별 요약표와 과제별 성공 횟수표 (Markdown)."""
    arms = [arm for arm in ARMS if any(record["arm"] == arm for record in records)]
    lines = [
        "| 방식 | 실행 | 성공률 | 숨김 테스트 통과율 | 시간 중앙값 | 모델 호출(평균) | 토큰(평균) | 비용(평균) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for arm in arms:
        rows = [record for record in records if record["arm"] == arm]
        passed = sum(record["hidden_passed"] for record in rows)
        total = sum(record["hidden_total"] for record in rows) or 1
        tokens = [record["actual_tokens"] or record["est_tokens"] for record in rows]
        lines.append(
            f"| {ARM_LABELS[arm]} | {len(rows)} | {sum(r['success'] for r in rows) / len(rows):.0%} "
            f"| {passed / total:.0%} | {statistics.median(r['seconds'] for r in rows):.0f}초 "
            f"| {statistics.mean(r['calls'] for r in rows):.1f} | {statistics.mean(tokens):,.0f} "
            f"| ${statistics.mean(r['cost_usd'] for r in rows):.2f} |"
        )
    lines += ["", "| 과제 | " + " | ".join(ARM_LABELS[arm] for arm in arms) + " |",
              "|---|" + "---|" * len(arms)]
    for task in sorted({record["task"] for record in records}):
        cells = []
        for arm in arms:
            rows = [r for r in records if r["task"] == task and r["arm"] == arm]
            cells.append(f"{sum(r['success'] for r in rows)}/{len(rows)}" if rows else "-")
        lines.append(f"| {task} | " + " | ".join(cells) + " |")
    lines += ["", "토큰은 CLI가 알려 준 실측값, 없으면 추정값입니다. 비용은 CLI가 알려 준 경우만 합산합니다 "
                  "(현재 Claude Code만 알려 주므로 파이프라인의 Codex·Antigravity 비용은 빠져 있습니다)."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TriAgent 평가 하니스")
    parser.add_argument("--tasks", default="all", help="쉼표로 구분한 과제 이름 (기본: 전부)")
    parser.add_argument("--arms", default="solo,pipeline", help="solo, pipeline 중 쉼표로 구분")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=2400, help="실행 하나의 시간 제한(초)")
    parser.add_argument("--check", action="store_true", help="과제 자체 점검만 한다 (모델 호출 없음)")
    parser.add_argument("--report", type=Path, help="기존 결과 파일로 표만 만든다")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    if args.report:
        records = [json.loads(line) for line in args.report.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(report(records))
        return 0

    tasks = task_dirs(args.tasks)
    arms = [arm.strip() for arm in args.arms.split(",") if arm.strip()]
    if unknown := sorted(set(arms) - set(ARMS)):
        raise SystemExit(f"모르는 방식: {', '.join(unknown)}")
    expected, problems = check_tasks(tasks)
    for problem in problems:
        print(f"[과제 문제] {problem}")
    if problems:
        return 1
    print(f"과제 점검 통과: {len(tasks)}개 ({', '.join(f'{name} {count}개' for name, count in expected.items())})")
    if args.check:
        return 0

    RESULTS_DIR.mkdir(exist_ok=True)
    results = RESULTS_DIR / f"{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    parent = Path(tempfile.mkdtemp(prefix="triagent-bench-"))
    records = []
    for repeat in range(1, args.repeats + 1):
        for task in tasks:
            for arm in arms:
                print(f"\n▶ {task.name} · {ARM_LABELS[arm]} · {repeat}회차", flush=True)
                record = run_one(task, arm, parent / f"{arm}-{repeat}", expected[task.name], args.timeout)
                record["repeat"] = repeat
                records.append(record)
                with results.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(f"  → {'성공' if record['success'] else '실패'} · 숨김 {record['hidden_passed']}/"
                      f"{record['hidden_total']} · {record['seconds']}초 · 호출 {record['calls']}회 · {record['outcome']}",
                      flush=True)
    table = report(records)
    results.with_suffix(".md").write_text(table + "\n", encoding="utf-8")
    print(f"\n{table}\n\n결과: {results}\n과제 저장소: {parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
