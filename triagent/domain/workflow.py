from __future__ import annotations

import json
from typing import Any

IDLE = "IDLE"
ANALYZING = "ANALYZING"
DEBATING = "DEBATING"
WAITING_FOR_USER_APPROVAL = "WAITING_FOR_USER_APPROVAL"
WAITING_FOR_USER_RESPONSE = "WAITING_FOR_USER_RESPONSE"
CODING = "CODING"
VALIDATING = "VALIDATING"
REVIEWING = "REVIEWING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

WORKFLOW_STATUSES = {
    IDLE,
    ANALYZING,
    DEBATING,
    WAITING_FOR_USER_APPROVAL,
    WAITING_FOR_USER_RESPONSE,
    CODING,
    VALIDATING,
    REVIEWING,
    COMPLETED,
    FAILED,
    CANCELLED,
}


def normalize_workflow_status(value: Any, *, has_topic: bool = False) -> str:
    status = str(value or "").strip().upper()
    if status in WORKFLOW_STATUSES:
        return status
    return ANALYZING if has_topic else IDLE


def normalize_user_question(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if str(value.get("action", "")).strip().upper() != "STOP_AND_ASK_USER":
        return None

    reason = str(value.get("reason", "")).strip()
    question = str(value.get("question", "")).strip()
    if not reason or not question or value.get("blocking") is not True:
        return None

    options: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    raw_options = value.get("options", [])
    if not isinstance(raw_options, list):
        return None
    for raw_option in raw_options:
        if not isinstance(raw_option, dict):
            continue
        option_id = str(raw_option.get("id", "")).strip()[:24]
        label = str(raw_option.get("label", "")).strip()[:160]
        risk = str(raw_option.get("risk", "")).strip()[:240]
        if not option_id or not label or option_id in seen_ids:
            continue
        seen_ids.add(option_id)
        options.append({"id": option_id, "label": label, "risk": risk})

    recommended = str(value.get("recommended_option", "")).strip()
    if recommended and recommended not in seen_ids:
        recommended = ""

    normalized = {
        "action": "STOP_AND_ASK_USER",
        "reason": reason[:1000],
        "question": question[:1000],
        "options": options,
        "recommended_option": recommended,
        "blocking": True,
    }
    source_agent = str(value.get("source_agent", "")).strip()
    source_phase = str(value.get("source_phase", "")).strip()
    asked_at = str(value.get("asked_at", "")).strip()
    if source_agent:
        normalized["source_agent"] = source_agent
    if source_phase:
        normalized["source_phase"] = source_phase[:160]
    if asked_at:
        normalized["asked_at"] = asked_at[:40]
    try:
        normalized["resume_step_index"] = max(0, int(value.get("resume_step_index", 0)))
    except (TypeError, ValueError):
        normalized["resume_step_index"] = 0
    return normalized


def extract_stop_and_ask_user(text: str) -> tuple[str, dict[str, Any] | None]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, consumed = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        question = normalize_user_question(value)
        if question:
            end = index + consumed
            visible = (text[:index] + text[end:]).strip()
            if visible.startswith("```json") and visible.endswith("```"):
                visible = visible[7:-3].strip()
            return visible, question
    return text, None
