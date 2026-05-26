"""
guidance_session.py — In-memory guided capture session state (user-driven step progression).

Template JSON remains authoritative; sessions only track which step index the user is viewing.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from utils.template_schema import GuidancePlan, GuidedCaptureStep

logger = logging.getLogger("jobdoc.guidance")

# session_id -> GuidanceSession
_SESSIONS: dict[str, GuidanceSession] = {}
_SESSION_TTL_SEC = 60 * 60 * 8  # 8 hours


@dataclass
class GuidanceSession:
    session_id: str
    report_types: list[str]
    plan: GuidancePlan
    current_index: int = 0  # 0-based index into plan.steps
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def steps(self) -> list[GuidedCaptureStep]:
        return list(self.plan.steps)

    @property
    def total_steps(self) -> int:
        return len(self.plan.steps)


def _purge_stale_sessions() -> None:
    now = time.time()
    stale = [sid for sid, s in _SESSIONS.items() if now - s.updated_at > _SESSION_TTL_SEC]
    for sid in stale:
        del _SESSIONS[sid]
        logger.info("guidance_session expired session_id=%s", sid)


def create_session(plan: GuidancePlan, report_types: list[str]) -> GuidanceSession:
    _purge_stale_sessions()
    session_id = uuid.uuid4().hex
    session = GuidanceSession(
        session_id=session_id,
        report_types=list(report_types),
        plan=plan,
        current_index=0,
    )
    _SESSIONS[session_id] = session
    logger.info(
        "guidance_session created session_id=%s report_types=%s total_steps=%s",
        session_id,
        report_types,
        session.total_steps,
    )
    return session


def get_session(session_id: str) -> GuidanceSession | None:
    _purge_stale_sessions()
    return _SESSIONS.get(session_id)


def step_payload(step: GuidedCaptureStep, *, current_index: int, total: int) -> dict[str, Any]:
    """API-safe dict for one guidance step (from template, not mutated)."""
    is_last = current_index >= total - 1
    is_first = current_index <= 0
    return {
        "step_index": step.step_index,
        "total_steps": step.total_steps,
        "section_id": step.section_id,
        "report_type": step.report_type,
        "report_title": step.report_title,
        "title": step.title,
        "voice_prompt": step.voice_prompt,
        "on_screen_text": step.on_screen_text,
        "spoken_intro": step.spoken_intro,
        "transition_line": step.transition_line,
        "required": step.required,
        "min_marks": step.min_marks,
        "suggested_phrases": list(step.suggested_phrases),
        "required_fields": list(step.required_fields),
        "writer_instructions": step.writer_instructions,
        "identification_phrase": step.identification_phrase,
        "progress_label": f"Step {step.step_index} of {step.total_steps} — {step.title}",
        "current_index": current_index,
        "is_first": is_first,
        "is_last": is_last,
        "can_advance": not is_last,
    }


def session_state_payload(session: GuidanceSession) -> dict[str, Any]:
    total = session.total_steps
    if total == 0:
        return {
            "session_id": session.session_id,
            "report_types": session.report_types,
            "total_steps": 0,
            "current_index": 0,
            "identification_phrase": session.plan.identification_phrase,
            "intro_script": session.plan.intro_script,
            "outro_script": session.plan.outro_script,
            "current_step": None,
            "complete": True,
        }
    idx = min(max(0, session.current_index), total - 1)
    step = session.steps[idx]
    return {
        "session_id": session.session_id,
        "report_types": session.report_types,
        "total_steps": total,
        "current_index": idx,
        "identification_phrase": session.plan.identification_phrase,
        "intro_script": session.plan.intro_script,
        "outro_script": session.plan.outro_script,
        "source": session.plan.source,
        "current_step": step_payload(step, current_index=idx, total=total),
        "complete": False,
    }


def get_current_step(session_id: str) -> dict[str, Any] | None:
    session = get_session(session_id)
    if not session:
        return None
    payload = session_state_payload(session)
    logger.info(
        "guidance_session current session_id=%s index=%s section_id=%s",
        session_id,
        payload.get("current_index"),
        (payload.get("current_step") or {}).get("section_id"),
    )
    return payload


def advance_step(session_id: str) -> dict[str, Any] | None:
    session = get_session(session_id)
    if not session or not session.steps:
        return None
    total = session.total_steps
    if session.current_index >= total - 1:
        logger.info("guidance_session advance blocked (already on last step) session_id=%s", session_id)
        return session_state_payload(session)
    session.current_index += 1
    session.updated_at = time.time()
    payload = session_state_payload(session)
    logger.info(
        "guidance_session advanced session_id=%s -> index=%s section_id=%s",
        session_id,
        session.current_index,
        (payload.get("current_step") or {}).get("section_id"),
    )
    return payload


def go_to_step(session_id: str, step_index: int) -> dict[str, Any] | None:
    """Jump to 1-based step_index from checklist (user review only)."""
    session = get_session(session_id)
    if not session or not session.steps:
        return None
    target = max(1, min(step_index, session.total_steps)) - 1
    session.current_index = target
    session.updated_at = time.time()
    logger.info("guidance_session goto session_id=%s index=%s", session_id, target)
    return session_state_payload(session)
