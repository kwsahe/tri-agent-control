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
from typing import Callable

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


def _has_python_tests(root: Path, exists: Callable[[Path], bool] | None = None) -> bool:
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return False
    try:
        return any(tests_dir.glob("test*.py"))
    except OSError:
        return False


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
            commands.append(("npm test", [environment.npm_executable, "test", "--", "--runInBand"]))
        if scripts.get("build"):
            commands.append(("npm run build", [environment.npm_executable, "run", "build"]))

    if _has_python_tests(root):
        if environment.has_pytest:
            commands.append(
                ("Python pytest", [environment.python_executable, "-m", "pytest", "-q"])
            )
        else:
            # pytest가 없어도 tests/test*.py는 표준 unittest로 실행할 수 있다.
            commands.append(
                (
                    "Python unittest",
                    [
                        environment.python_executable,
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        "tests",
                    ],
                )
            )

    return commands[:MAX_VALIDATION_COMMANDS]
