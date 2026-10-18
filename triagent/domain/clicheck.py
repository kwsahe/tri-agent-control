"""CLI 사전 점검 — 단계별 상태와 기능 감지.

이전 동작: ``--version``이 0으로 끝나기만 하면 "연결됨"으로 표시했다. 그래서
Codex가 읽기는 되는데 파일 쓰기는 실패하는 상태에서도 초록불이 떴다.

여기서는 점검을 단계로 쪼개고, 각 단계의 성패를 따로 보고한다.

1. ``command`` — 명령이 PATH에 있는가
2. ``version`` — 실행되고 버전을 알려주는가
3. ``auth``   — 로그인/인증이 되어 있는가
4. ``read``   — 격리된 임시 폴더의 파일을 실제로 읽는가
5. ``write``  — (사용자가 쓰기를 선택했을 때만) 격리된 임시 폴더에 실제로 쓰는가

쓰기 검사는 절대 프로젝트 폴더에서 하지 않는다 — 전용 임시 폴더에서만 한다.
실제 CLI가 없는 CI에서도 회귀 테스트가 가능하도록, 프로세스 실행은 전부
``runner`` 콜러블로 주입된다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Sequence

__all__ = [
    "StepStatus",
    "CheckStep",
    "CliReport",
    "CommandResult",
    "detect_codex_write_flags",
    "summarize_report",
    "redact",
]


class StepStatus:
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


# 로그/화면에 절대 남기면 안 되는 값들. 명령줄 요약과 출력 양쪽에 적용한다.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(?i)\b(gh[pousr]_[A-Za-z0-9]{8,})"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{8,})"),
    # TOKEN(?!S): "max_tokens: 4096", "input_tokens=120" 같은 토큰 개수는 비밀값이 아니다.
    re.compile(r"(?i)\b([A-Z0-9_]*(?:API_KEY|TOKEN(?!S)|SECRET|PASSWORD)[A-Z0-9_]*\s*[=:]\s*)(\S+)"),
    re.compile(r"(?i)(--(?:api-key|token|password)[= ])(\S+)"),
    re.compile(r"(?i)\b(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"),
    re.compile(r"\b(AIza[0-9A-Za-z_\-]{35})"),  # Google(Gemini) API 키
)


def redact(text: object, limit: int | None = 600) -> str:
    """CLI 출력에서 인증 정보로 보이는 부분을 지운다. ``limit=None``이면 자르지 않는다."""
    value = str(text or "")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            value = pattern.sub(lambda m: m.group(1) + "***", value)
        else:
            value = pattern.sub("***", value)
    return value[:limit]


@dataclass(frozen=True)
class CommandResult:
    """주입된 runner가 돌려주는 최소 결과."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    not_found: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.not_found

    @property
    def text(self) -> str:
        return (self.stdout or self.stderr or "").strip()


@dataclass
class CheckStep:
    name: str
    status: str = StepStatus.UNKNOWN
    detail: str = ""
    #: 사용자가 그대로 따라 할 수 있게, 인증정보를 뺀 명령 요약.
    command_summary: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": redact(self.detail),
            "command_summary": redact(self.command_summary, 300),
        }


@dataclass
class CliReport:
    """한 CLI의 점검 결과 전체."""

    key: str
    label: str
    command: str
    version: str = ""
    steps: list[CheckStep] = field(default_factory=list)
    #: 감지된 기능 플래그 (버전마다 다른 옵션 이름을 흡수한다).
    features: dict = field(default_factory=dict)

    def step(self, name: str) -> CheckStep | None:
        for item in self.steps:
            if item.name == name:
                return item
        return None

    def _status(self, name: str) -> str:
        found = self.step(name)
        return found.status if found else StepStatus.UNKNOWN

    @property
    def can_read(self) -> bool:
        return self._status("read") == StepStatus.OK

    @property
    def can_write(self) -> bool:
        return self._status("write") == StepStatus.OK

    @property
    def ok(self) -> bool:
        """"연결됨"으로 표시해도 되는가 — 건너뛴 단계는 실패로 치지 않는다."""
        return all(step.status != StepStatus.FAILED for step in self.steps)

    @property
    def first_failure(self) -> CheckStep | None:
        for item in self.steps:
            if item.status == StepStatus.FAILED:
                return item
        return None

    def as_dict(self) -> dict:
        failure = self.first_failure
        return {
            "key": self.key,
            # label은 대시보드 연결 표시줄이 그대로 쓴다. 빼면 KeyError가 난다.
            "label": self.label,
            "ok": self.ok,
            "version": redact(self.version, 200),
            "can_read": self.can_read,
            "can_write": self.can_write,
            "steps": [step.as_dict() for step in self.steps],
            "features": dict(self.features),
            "failed_step": failure.name if failure else "",
            "failed_detail": redact(failure.detail) if failure else "",
            "action": self.suggested_action(),
            # 구버전 프런트엔드 호환
            "detail": redact(failure.detail) if failure else (self.version or "확인됨"),
        }

    def suggested_action(self) -> str:
        failure = self.first_failure
        if failure is None:
            return ""
        return _ACTIONS.get(
            (self.key, failure.name),
            _ACTIONS.get(("*", failure.name), "CLI 상태를 직접 확인해주세요."),
        )


_ACTIONS = {
    ("*", "command"): "CLI가 PATH에 있는지 확인하고, 설치되어 있지 않다면 설치해주세요.",
    ("*", "version"): "터미널에서 직접 실행해 오류 메시지를 확인해주세요.",
    ("codex", "auth"): "터미널에서 `codex login`을 실행해 로그인해주세요.",
    ("claude", "auth"): "터미널에서 `claude` 를 한 번 실행해 로그인 상태를 확인해주세요.",
    ("antigravity", "auth"): "터미널에서 `agy` 를 실행해 로그인 상태를 확인해주세요.",
    ("*", "auth"): "해당 CLI에 로그인되어 있는지 확인해주세요.",
    ("*", "read"): "CLI가 폴더를 읽을 수 있는지(샌드박스/권한 설정) 확인해주세요.",
    ("codex", "write"): (
        "Codex가 쓰기 승인을 받지 못했습니다. 이 버전에서 지원하는 자동 승인 옵션이 "
        "감지되지 않았다면 `codex exec --help`로 승인 옵션을 확인해주세요."
    ),
    ("*", "write"): "CLI에 파일 쓰기 권한이 있는지 확인해주세요.",
}


# ──────────────────────────────────────────
# 기능 감지 (버전 간 옵션 차이 흡수)
# ──────────────────────────────────────────

def detect_codex_write_flags(help_text: str) -> list[str]:
    """이 Codex 버전에서 "workspace-write + 자동 승인"을 얻는 인자를 고른다.

    옵션 이름이 버전마다 달라졌기 때문에 추측하지 않고 ``codex exec --help``
    출력에서 실제로 존재하는 것만 쓴다.

    * 0.148.x — ``--approve-for-me``. 이 버전에서는 ``--sandbox``와 **함께 쓸 수 없다**
      (``error: the argument '--sandbox <SANDBOX_MODE>' cannot be used with
      '--approve-for-me'``). 플래그 자체가 workspace-write 샌드박스를 의미한다.
    * 그 이전 — ``--full-auto`` 또는 ``--ask-for-approval never`` + ``--sandbox``
    """
    text = help_text or ""

    if "--approve-for-me" in text:
        # 배타적 플래그다. --sandbox를 같이 넘기면 codex가 인자 오류로 즉시 죽는다.
        return ["--approve-for-me"]
    if "--full-auto" in text:
        # --full-auto도 그 자체로 workspace-write를 의미한다.
        return ["--full-auto"]
    if "--ask-for-approval" in text:
        return ["--sandbox", "workspace-write", "--ask-for-approval", "never"]
    # 아무 자동 승인 옵션도 없으면 샌드박스 지정만 남긴다 — 승인 프롬프트에서 막힐 수
    # 있고, 그 사실은 write 단계 실패로 정직하게 드러난다.
    return ["--sandbox", "workspace-write"]


def summarize_report(reports: Sequence[CliReport]) -> dict:
    return {
        "all_ok": all(report.ok for report in reports),
        "readable": [report.key for report in reports if report.can_read],
        "writable": [report.key for report in reports if report.can_write],
        "reports": {report.key: report.as_dict() for report in reports},
    }


def run_staged_check(
    *,
    key: str,
    label: str,
    command: str,
    runner: Callable[[list[str]], CommandResult],
    version_args: Sequence[str] = ("--version",),
    auth_probe: Callable[[], CommandResult] | None = None,
    read_probe: Callable[[], tuple[bool, str]] | None = None,
    write_probe: Callable[[], tuple[bool, str]] | None = None,
    help_probe: Callable[[], str] | None = None,
    check_write: bool = False,
) -> CliReport:
    """단계별 점검을 수행한다. 앞 단계가 실패하면 뒤 단계는 ``skipped``."""
    report = CliReport(key=key, label=label, command=command)

    command_step = CheckStep("command", command_summary=f"{command} --version")
    version_step = CheckStep("version", command_summary=f"{command} --version")
    auth_step = CheckStep("auth")
    read_step = CheckStep("read")
    write_step = CheckStep("write")
    report.steps = [command_step, version_step, auth_step, read_step, write_step]

    result = runner(list(version_args))
    if result.not_found:
        command_step.status = StepStatus.FAILED
        command_step.detail = "명령을 찾을 수 없습니다 (PATH 확인 필요)."
        for step in (version_step, auth_step, read_step, write_step):
            step.status = StepStatus.SKIPPED
        return report

    command_step.status = StepStatus.OK
    command_step.detail = "명령을 찾았습니다."

    if not result.ok:
        version_step.status = StepStatus.FAILED
        version_step.detail = (
            "실행 시간 초과" if result.timed_out
            else f"종료 코드 {result.returncode}: {redact(result.text, 200)}"
        )
        for step in (auth_step, read_step, write_step):
            step.status = StepStatus.SKIPPED
        return report

    lines = result.text.splitlines()
    report.version = lines[0].strip() if lines else "버전 확인됨"
    version_step.status = StepStatus.OK
    version_step.detail = report.version

    if help_probe is not None:
        try:
            report.features["help"] = True
            report.features["write_args"] = detect_codex_write_flags(help_probe()) if key == "codex" else []
        except Exception:  # pragma: no cover - help 조회는 실패해도 치명적이지 않다
            report.features["help"] = False

    if auth_probe is None:
        auth_step.status = StepStatus.SKIPPED
        auth_step.detail = "이 CLI는 별도 인증 확인을 제공하지 않습니다."
    else:
        auth_result = auth_probe()
        if auth_result.ok:
            auth_step.status = StepStatus.OK
            auth_step.detail = "인증 확인됨"
        else:
            auth_step.status = StepStatus.FAILED
            auth_step.detail = redact(auth_result.text, 200) or "인증되지 않았습니다."
            for step in (read_step, write_step):
                step.status = StepStatus.SKIPPED
            return report

    if read_probe is None:
        read_step.status = StepStatus.SKIPPED
        read_step.detail = "읽기 검사를 건너뛰었습니다."
    else:
        ok, detail = read_probe()
        read_step.status = StepStatus.OK if ok else StepStatus.FAILED
        read_step.detail = redact(detail, 300)
        if not ok:
            write_step.status = StepStatus.SKIPPED
            return report

    if not check_write or write_probe is None:
        write_step.status = StepStatus.SKIPPED
        write_step.detail = (
            "쓰기 권한을 선택하지 않아 검사하지 않았습니다."
            if not check_write else "쓰기 검사를 건너뛰었습니다."
        )
        return report

    ok, detail = write_probe()
    write_step.status = StepStatus.OK if ok else StepStatus.FAILED
    write_step.detail = redact(detail, 300)
    return report
