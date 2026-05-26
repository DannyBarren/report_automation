"""
pipeline_config.py — JobDoc pipeline version and runtime limits.
"""

from __future__ import annotations

import os

PIPELINE_V1 = "v1"
PIPELINE_V2 = "v2"


def get_jobdoc_pipeline() -> str:
    """Return ``v1`` (default) or ``v2`` from ``JOBDOC_PIPELINE``."""
    raw = os.getenv("JOBDOC_PIPELINE", PIPELINE_V1).strip().lower()
    if raw in (PIPELINE_V2, "2", "narration", "narration_v2"):
        return PIPELINE_V2
    return PIPELINE_V1


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
