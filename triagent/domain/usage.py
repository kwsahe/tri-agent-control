"""토큰·비용 집계 — 누적 사용량과 "현재 컨텍스트 점유율"을 분리한다.

화면에 ``Codex 현재 컨텍스트 461.1%``가 뜬 원인:

``latest_context_token_count()``가 마지막 메시지의 ``actual_usage``를 그대로
컨텍스트 한도로 나눴는데, CLI가 주는 ``input_tokens``는 그 턴 *안에서 일어난 모든
내부 모델 호출의 합계*다. 도구를 20번 부른 턴이면 같은 컨텍스트가 20번 더해지므로
컨텍스트 한도를 훌쩍 넘는 게 당연하다. 누적량과 점유율은 애초에 다른 값이다.

이 모듈의 원칙:

* 누적(``cumulative``)과 최근 호출 컨텍스트(``context``)는 절대 섞지 않는다.
* 최근 호출 컨텍스트를 CLI가 알려주지 않으면 ``unknown``이라고 말한다. 누적값으로
  대체하지 않는다.
* 추정치는 ``source="estimated"``로 표시해 실제 측정값과 구분한다.
* 실제 보고된 비용과 추정 비용을 하나의 확정 숫자로 합치지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "TOKEN_KEYS",
    "ContextUsage",
    "CostBreakdown",
    "sum_usage_tokens",
    "cumulative_tokens",
    "last_call_context_tokens",
    "context_usage",
    "cost_breakdown",
]

#: 누적 합계에 쓰는 키. OpenAI의 input_tokens는 cached_input_tokens를 이미 포함하므로
#: cached_input_tokens는 중복 계산하지 않는다. Anthropic의 cache_* 는 별도 항목이다.
TOKEN_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)

#: 컨텍스트 점유율 계산에 쓰는 키. "그 호출이 끝난 시점에 창에 들어 있는 양"이므로
#: 입력에 그 호출의 출력까지 더한다 (출력은 다음 호출의 입력이 된다).
#: 중요한 건 어떤 키를 더하느냐가 아니라, **한 호출**의 usage만 본다는 점이다 —
#: 턴 전체 누적을 여기 쓰면 100%를 넘는 값이 나온다.
_CONTEXT_INPUT_KEYS = TOKEN_KEYS


def _as_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def sum_usage_tokens(usage: object, keys: tuple[str, ...] = TOKEN_KEYS) -> int:
    if not isinstance(usage, dict):
        return 0
    return sum(_as_int(usage.get(key)) for key in keys)


def cumulative_tokens(messages: list[dict], agent: str) -> int:
    """세션 전체에서 ``agent``가 쓴 실제 토큰의 합.

    이 값은 비용/예산용이다. 컨텍스트 한도로 나누면 안 된다.
    """
    total = 0
    for message in messages or []:
        if message.get("agent") != agent:
            continue
        meta = message.get("meta") or {}
        total += sum_usage_tokens(meta.get("actual_usage"))
    return total


def last_call_context_tokens(messages: list[dict], agent: str) -> tuple[int, str]:
    """가장 최근 호출의 컨텍스트 점유 토큰과 그 출처를 돌려준다.

    출처는 셋 중 하나:

    ``measured``
        CLI가 개별 호출 단위 usage를 줬다 (``meta["context_usage"]`` 또는
        Claude의 마지막 assistant 이벤트). 그대로 믿을 수 있다.
    ``estimated``
        개별 호출 usage가 없어 프롬프트 길이 기반 추정치를 쓴다.
    ``unknown``
        아무 근거도 없다. 퍼센트를 계산하지 않는다.
    """
    for message in reversed(messages or []):
        if message.get("agent") != agent:
            continue
        meta = message.get("meta") or {}

        # 1) 호출 단위로 따로 기록해 둔 컨텍스트 스냅샷이 있으면 그게 정답이다.
        snapshot = meta.get("context_usage")
        if isinstance(snapshot, dict):
            tokens = sum_usage_tokens(snapshot, _CONTEXT_INPUT_KEYS)
            if tokens > 0:
                return tokens, "measured"

        # 2) Claude는 assistant 이벤트마다 usage를 주므로 마지막 것이 실제 컨텍스트다.
        usage = meta.get("actual_usage")
        if isinstance(usage, dict):
            iterations = usage.get("iterations")
            if isinstance(iterations, list) and iterations:
                last = iterations[-1]
                tokens = sum_usage_tokens(last, _CONTEXT_INPUT_KEYS)
                if tokens > 0:
                    return tokens, "measured"

        # 3) 남은 건 프롬프트 길이 기반 추정뿐이다.
        estimated = _as_int(meta.get("est_tokens"))
        if estimated > 0:
            return estimated, "estimated"
        return 0, "unknown"
    return 0, "unknown"


@dataclass(frozen=True)
class ContextUsage:
    """한 모델의 "지금 컨텍스트가 얼마나 찼나"."""

    used_tokens: int
    limit_tokens: int
    source: str  # measured | estimated | unknown

    @property
    def known(self) -> bool:
        return self.source in ("measured", "estimated") and self.used_tokens > 0

    @property
    def percent(self) -> float:
        """0~100+ 실수. 100을 넘으면 넘는 대로 돌려준다 (문제를 숨기지 않는다)."""
        if not self.known or self.limit_tokens <= 0:
            return 0.0
        return round((self.used_tokens / self.limit_tokens) * 100, 1)

    @property
    def bar_percent(self) -> float:
        """막대 길이용으로만 0~100으로 자른 값. 표시 숫자는 percent를 쓴다."""
        return min(100.0, max(0.0, self.percent))

    @property
    def overflow(self) -> bool:
        return self.percent > 100.0

    @property
    def label(self) -> str:
        if not self.known:
            return "알 수 없음"
        prefix = "~" if self.source == "estimated" else ""
        return f"{prefix}{self.used_tokens:,} / {self.limit_tokens:,}"

    def as_dict(self) -> dict:
        return {
            "used_tokens": self.used_tokens,
            "limit_tokens": self.limit_tokens,
            "source": self.source,
            "known": self.known,
            "percent": self.percent,
            "bar_percent": self.bar_percent,
            "overflow": self.overflow,
            "label": self.label,
            # 구버전 프런트엔드 호환: estimated 불리언을 유지한다.
            "estimated": self.source != "measured",
        }


def context_usage(messages: list[dict], agent: str, limit_tokens: int) -> ContextUsage:
    used, source = last_call_context_tokens(messages, agent)
    return ContextUsage(
        used_tokens=used,
        limit_tokens=max(0, int(limit_tokens or 0)),
        source=source,
    )


@dataclass(frozen=True)
class CostBreakdown:
    """실제 보고된 비용과 추정 비용을 따로 담는다.

    둘을 더한 단일 숫자를 만들지 않는다 — 근거의 확실성이 다르기 때문이다.
    """

    reported_usd: float
    estimated_usd: float
    reported_turns: int
    estimated_turns: int
    currency: str = "USD"

    @property
    def has_estimate(self) -> bool:
        return self.estimated_turns > 0

    @property
    def basis(self) -> str:
        parts = []
        if self.reported_turns:
            parts.append(f"CLI 보고 {self.reported_turns}턴")
        if self.estimated_turns:
            parts.append(f"토큰 기반 추정 {self.estimated_turns}턴")
        return " · ".join(parts) or "집계된 턴 없음"

    def as_dict(self) -> dict:
        return {
            "reported_usd": round(self.reported_usd, 4),
            "estimated_usd": round(self.estimated_usd, 4),
            "reported_turns": self.reported_turns,
            "estimated_turns": self.estimated_turns,
            "currency": self.currency,
            "has_estimate": self.has_estimate,
            "basis": self.basis,
        }


def cost_breakdown(messages: list[dict], agent: str | None = None) -> CostBreakdown:
    """메시지에서 비용을 모은다.

    ``meta["actual_cost_usd"]``는 CLI가 직접 알려준 값이고,
    ``meta["estimated_cost_usd"]``는 토큰 수로 우리가 계산한 값이다.
    """
    reported = 0.0
    estimated = 0.0
    reported_turns = 0
    estimated_turns = 0

    for message in messages or []:
        if agent is not None and message.get("agent") != agent:
            continue
        meta = message.get("meta") or {}
        try:
            actual = float(meta.get("actual_cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            actual = 0.0
        if actual > 0:
            reported += actual
            reported_turns += 1
            continue
        try:
            guess = float(meta.get("estimated_cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            guess = 0.0
        if guess > 0:
            estimated += guess
            estimated_turns += 1

    return CostBreakdown(
        reported_usd=reported,
        estimated_usd=estimated,
        reported_turns=reported_turns,
        estimated_turns=estimated_turns,
    )
