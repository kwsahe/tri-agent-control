"""프로젝트 검증(테스트/빌드) 명령 탐지 — 순수 함수.

원래 :func:`roundtable.detect_validation_commands`는 ``importlib``/``shutil.which``/
``sys.executable``을 직접 들여다봐서 (1) 실행 환경에 따라 결과가 달라지고
(2) 단위 테스트에서 결과를 고정할 수 없었다. 그래서
``test_validation_command_detection_for_python_tests``는 pytest가 설치된 기계에서만
통과했고, worker 테스트는 진짜로 ``python -m unittest discover -s tests``를 자식
프로세스로 띄워 자기 자신을 재귀 실행할 수 있었다.

여기서는 환경 조회를 전부 인자로 뽑아내 순수 함수로 만든다. 테스트는 환경을
명시적으로 주입하고, 실제 실행은 :func:`roundtable.detect_validation_commands`가
현재 환경을 채워 넣는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ToolEnvironment",
    "detect_validation_commands",
    "MAX_VALIDATION_COMMANDS",
]

MAX_VALIDATION_COMMANDS = 2

# npm test 스크립트가 이 문구를 담고 있으면 `npm init`의 자리표시자라 실행 의미가 없다.
_PLACEHOLDER_TEST_SCRIPT = "no test specified"


@dataclass(frozen=True)
class ToolEnvironment:
    """검증 명령을 만들 때 필요한 바깥 환경.

    테스트에서 그대로 만들어 주입할 수 있도록 값 객체로 둔다.
    """

    python_executable: str = "python"
    npm_executable: str = "npm"
    has_pytest: bool = False
    has_npm: bool = True
    #: 재귀 방지용. TriAgent 자신의 폴더를 검증 대상으로 잡으면 unittest가
    #: 자기 자신을 다시 실행하므로, 이 목록에 있는 폴더는 명령을 만들지 않는다.
    excluded_roots: tuple[str, ...] = field(default=())


def _is_excluded(root: Path, environment: ToolEnvironment) -> bool:
    if not environment.excluded_roots:
        return False
    try:
        resolved = root.resolve()
    except OSError:
        return False
    for excluded in environment.excluded_roots:
        try:
            if resolved == Path(excluded).resolve():
                return True
        except OSError:
            continue
    return False


def _npm_scripts(root: Path) -> dict:
    package_json = root / "package.json"
    if not package_json.is_file():
        return {}
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    scripts = data.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def _project_venv(root: Path) -> tuple[str, str, bool] | None:
    """프로젝트 가상환경 ``(.venv|venv, Python 경로, pytest 설치 여부)``. 없으면 None.

    프로젝트 의존성과 pytest는 보통 이 가상환경에만 있다. TriAgent 자신의 Python으로
    돌리면 import가 실패하거나 pytest가 없어 unittest로 떨어져 테스트를 하나도 못 찾는다.
    """
    for name in (".venv", "venv"):
        venv = root / name
        for python in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
            if python.is_file():
                has_pytest = (venv / "Lib" / "site-packages" / "pytest").is_dir() or any(
                    venv.glob("lib/python*/site-packages/pytest")
                )
                return name, str(python), has_pytest
    return None


def _python_test_start(root: Path) -> str:
    """unittest가 시작할 폴더 — ``tests/``에 테스트가 있으면 "tests", 루트에만 있으면 ".", 없으면 "".

    코딩 파이프라인의 테스트 담당은 루트에 ``test_*.py``를 만들기도 한다. 예전에는 ``tests/``만
    봐서 그런 테스트가 있어도 자동 검증을 아예 건너뛰었다.
    """
    try:
        tests_dir = root / "tests"
        if tests_dir.is_dir() and any(tests_dir.glob("test*.py")):
            return "tests"
        if any(path.is_file() for path in root.glob("test*.py")):
            return "."
    except OSError:
        pass
    return ""


def detect_validation_commands(
    root: Path,
    environment: ToolEnvironment | None = None,
) -> list[tuple[str, list[str]]]:
    """``root``에서 실행할 만한 검증 명령을 (라벨, argv) 목록으로 돌려준다.

    환경 의존성은 전부 ``environment``로 주입되므로 결과가 결정적이다.
    """
    environment = environment or ToolEnvironment()
    root = Path(root)
    commands: list[tuple[str, list[str]]] = []

    if _is_excluded(root, environment):
        # TriAgent 자신을 검증 대상으로 잡으면 무한 재귀가 된다.
        return []

    scripts = _npm_scripts(root)
    if scripts and environment.has_npm:
        test_script = str(scripts.get("test") or "")
        if test_script and _PLACEHOLDER_TEST_SCRIPT not in test_script:
            # --runInBand는 Jest 전용이다. vitest 등은 모르는 옵션으로 거부해 검증이 매번 실패한다.
            extra = ["--", "--runInBand"] if "jest" in test_script else []
            commands.append(("npm test", [environment.npm_executable, "test", *extra]))
        if scripts.get("build"):
            commands.append(("npm run build", [environment.npm_executable, "run", "build"]))

    test_start = _python_test_start(root)
    if test_start:
        venv = _project_venv(root)
        python = venv[1] if venv else environment.python_executable
        has_pytest = venv[2] if venv else environment.has_pytest
        suffix = f" · {venv[0]}" if venv else ""
        if has_pytest:
            # pytest는 루트와 tests/ 양쪽의 test_*.py를 스스로 찾는다.
            commands.append((f"Python pytest{suffix}", [python, "-m", "pytest", "-q"]))
        else:
            # pytest가 없어도 test*.py는 표준 unittest로 실행할 수 있다.
            commands.append(
                (f"Python unittest{suffix}", [python, "-m", "unittest", "discover", "-s", test_start])
            )

    return commands[:MAX_VALIDATION_COMMANDS]
