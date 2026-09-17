"""
pipeline_config.py — JobDoc pipeline version and runtime limits.
"""

from __future__ import annotations

import os

PIPELINE_V1 = "v1"
PIPELINE_V2 = "v2"


def get_jobdoc_pipeline() -> str:
    """Return ``v2`` (default) or ``v1`` from ``JOBDOC_PIPELINE``.

    Unset or blank env → v2 so a forgotten variable cannot drop MARK taps.
    Explicit ``JOBDOC_PIPELINE=v1`` (or ``1``) is still honored.
    """
    raw = (os.getenv("JOBDOC_PIPELINE") or "").strip().lower()
    if raw in (PIPELINE_V1, "1"):
        return PIPELINE_V1
    return PIPELINE_V2


def is_v2_pipeline() -> bool:
    return get_jobdoc_pipeline() == PIPELINE_V2


def crew_verbose() -> bool:
    return os.getenv("CREW_VERBOSE", "0").strip() in ("1", "true", "yes")


def crew_timeout_seconds() -> int:
    """Per-stage Crew kickoff timeout (seconds)."""
    try:
        return max(60, int(os.getenv("JOBDOC_CREW_TIMEOUT_SEC", "480")))
    except ValueError:
        return 480


def transcription_timeout_seconds() -> int:
    try:
        return max(30, int(os.getenv("JOBDOC_TRANSCRIPTION_TIMEOUT_SEC", "180")))
    except ValueError:
        return 180


def crew_max_retries() -> int:
    try:
        return max(0, min(3, int(os.getenv("JOBDOC_CREW_MAX_RETRIES", "1"))))
    except ValueError:
        return 1


def transcription_max_retries() -> int:
    try:
        return max(0, min(3, int(os.getenv("JOBDOC_TRANSCRIPTION_MAX_RETRIES", "2"))))
    except ValueError:
        return 2


def max_frames_per_section() -> int:
    """Max still frames stored/rendered per report section.

    Effectively unlimited for real inspections — an inspector can mark 50–100 items in one
    section. Override with ``JOBDOC_MAX_FRAMES_PER_SECTION`` (default 80). Near-duplicate marks
    within a short gap are still merged upstream, so this is a safety ceiling, not a target.
    """
    try:
        return max(1, int(os.getenv("JOBDOC_MAX_FRAMES_PER_SECTION", "80")))
    except ValueError:
        return 80


def frame_context_window_seconds() -> tuple[float, float]:
    """Narration context captured around each mark: ``(before, after)`` seconds.

    Defaults to 5s before / 10s after so every photo carries the surrounding description, not
    just the single nearest sentence. Override with ``JOBDOC_FRAME_CONTEXT_BEFORE_SEC`` /
    ``JOBDOC_FRAME_CONTEXT_AFTER_SEC``.
    """
    def _read(name: str, default: float) -> float:
        try:
            return max(0.0, float(os.getenv(name, str(default))))
        except ValueError:
            return default

    return _read("JOBDOC_FRAME_CONTEXT_BEFORE_SEC", 5.0), _read("JOBDOC_FRAME_CONTEXT_AFTER_SEC", 10.0)
