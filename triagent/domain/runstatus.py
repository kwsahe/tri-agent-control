"""턴 실행 결과 상태 — 사용자 중단 / 시간 초과 / CLI 실패를 구분한다.

실사용에서 사용자가 Antigravity 작업을 중단했을 때 화면이 이렇게 나왔다.

* 대화: "응답 없음 — 종료 코드 1"
* 배너: "실패 턴 재시도" CTA
* 상단: 중단됨
* 세션 목록: 진행 중

원인은 중단이 그냥 "CLI가 0이 아닌 코드로 끝났다"로만 남아서, 실패와 구분되지
않았기 때문이다. 여기서는 종료 사유를 1급 값으로 만들고, 그 값에서 UI 표시와
재시도 CTA 노출 여부를 일관되게 끌어낸다.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "RunStatus",
    "RUN_STATUSES",
    "TERMINAL_STATUSES",
    "TurnOutcome",
    "classify_turn",
    "status_label",
    "allows_failure_retry",
    "is_active_status",
    "session_list_status",
]


class RunStatus:
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    USER_CANCELLED = "USER_CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    CLI_FAILED = "CLI_FAILED"
    WAITING_FOR_USER_RESPONSE = "WAITING_FOR_USER_RESPONSE"
    PAUSED = "PAUSED"
    IDLE = "IDLE"


RUN_STATUSES = frozenset(
    {
        RunStatus.RUNNING,
        RunStatus.COMPLETED,
        RunStatus.USER_CANCELLED,
        RunStatus.TIMED_OUT,
        RunStatus.CLI_FAILED,
        RunStatus.WAITING_FOR_USER_RESPONSE,
        RunStatus.PAUSED,
        RunStatus.IDLE,
    }
)

#: 더 이상 자동으로 진행되지 않는 상태들.
TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.USER_CANCELLED,
        RunStatus.TIMED_OUT,
        RunStatus.CLI_FAILED,
    }
)

_LABELS = {
    RunStatus.RUNNING: "진행 중",
    RunStatus.COMPLETED: "완료",
    RunStatus.USER_CANCELLED: "사용자 중단",
    RunStatus.TIMED_OUT: "시간 초과",
    RunStatus.CLI_FAILED: "CLI 실패",
    RunStatus.WAITING_FOR_USER_RESPONSE: "사용자 답변 대기",
    RunStatus.PAUSED: "일시정지",
    RunStatus.IDLE: "대기",
}

#: 상태를 색만으로 구분하지 않도록 텍스트 기호도 함께 제공한다.
_GLYPHS = {
    RunStatus.RUNNING: "▶",
    RunStatus.COMPLETED: "✓",
    RunStatus.USER_CANCELLED: "■",
    RunStatus.TIMED_OUT: "⏱",
    RunStatus.CLI_FAILED: "✕",
    RunStatus.WAITING_FOR_USER_RESPONSE: "?",
    RunStatus.PAUSED: "❚❚",
    RunStatus.IDLE: "·",
}


@dataclass(frozen=True)
class TurnOutcome:
    """한 턴의 종료 결과."""

    status: str
    text: str
    detail: str = ""
    #: 중단 직전까지 모델이 내놓은 부분 출력이 있으면 True.
    partial: bool = False

    @property
    def label(self) -> str:
        return status_label(self.status)

    @property
    def is_failure(self) -> bool:
        """모델/CLI 쪽 문제인가? 사용자 중단은 실패가 아니다."""
        return self.status in (RunStatus.CLI_FAILED, RunStatus.TIMED_OUT)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "label": self.label,
            "glyph": status_glyph(self.status),
            "detail": self.detail,
            "partial": self.partial,
        }


def status_label(status: object) -> str:
    return _LABELS.get(str(status or ""), "알 수 없음")


def status_glyph(status: object) -> str:
    return _GLYPHS.get(str(status or ""), "·")


def allows_failure_retry(status: object) -> bool:
    """"실패한 턴 재시도" CTA를 띄워도 되는 상태인가.

    사용자가 스스로 멈춘 것(USER_CANCELLED)에 실패 재시도를 권하면 안 된다 —
    사용자는 새 작업을 시작하거나 그대로 두려고 멈춘 것이다.
    """
    return str(status or "") in (RunStatus.CLI_FAILED, RunStatus.TIMED_OUT)


def is_active_status(status: object) -> bool:
    return str(status or "") == RunStatus.RUNNING


def classify_turn(
    text: str,
    *,
    cancelled: bool = False,
    timed_out: bool = False,
    returncode: int | None = None,
    failure_marker: bool = False,
    had_partial_output: bool = False,
) -> TurnOutcome:
    """턴 결과를 분류한다.

    ``cancelled``가 최우선이다. 사용자가 CLI 프로세스를 죽였기 때문에 뒤따라오는
    "종료 코드 1"은 중단의 *결과*일 뿐 원인이 아니다.
    """
    body = text or ""

    if cancelled:
        detail = "사용자가 실행을 중단했습니다."
        if had_partial_output and body.strip():
            detail += " 중단 직전까지의 부분 출력을 보존했습니다."
        return TurnOutcome(
            status=RunStatus.USER_CANCELLED,
            text=body,
            detail=detail,
            partial=bool(had_partial_output and body.strip()),
        )

    if timed_out:
        return TurnOutcome(
            status=RunStatus.TIMED_OUT,
            text=body,
            detail="CLI 응답이 제한 시간을 넘겼습니다.",
            partial=bool(had_partial_output and body.strip()),
        )

    if failure_marker or (returncode is not None and returncode != 0):
        detail = "CLI가 정상적으로 응답하지 않았습니다."
        if returncode is not None and returncode != 0:
            detail += f" (종료 코드 {returncode})"
        return TurnOutcome(status=RunStatus.CLI_FAILED, text=body, detail=detail)

    return TurnOutcome(status=RunStatus.COMPLETED, text=body)


def session_list_status(
    *,
    is_active_session: bool,
    has_topic: bool,
    finished: bool,
    run_status: object = "",
) -> str:
    """세션 목록에 표시할 상태 — 상단 배너와 같은 값을 쓰도록 한 곳에서 계산한다.

    예전에는 목록이 "활성 세션이면 무조건 진행 중"으로 표시해서, 상단에
    "중단됨"이 떠 있는데 목록은 "진행 중"인 모순이 생겼다.
    """
    if not has_topic:
        return "준비"
    if finished:
        return "완료"

    status = str(run_status or "")
    if status in TERMINAL_STATUSES:
        return status_label(status)
    if status == RunStatus.PAUSED:
        return status_label(status)
    if status == RunStatus.WAITING_FOR_USER_RESPONSE:
        return status_label(status)
    if is_active_session and status == RunStatus.RUNNING:
        return status_label(RunStatus.RUNNING)
    return "보류"
