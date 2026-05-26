"""
runtime_config.py — Fail-fast checks that required API credentials exist.

jobdoc-demo runs only against real speech-to-text and real LLM providers.
"""

from __future__ import annotations

import os


def require_asr_credentials() -> None:
    """
    Deepgram is required for transcription.

    Raises:
        RuntimeError: With README-oriented guidance when neither is available.
    """
    if os.getenv("DEEPGRAM_API_KEY", "").strip():
        return
    raise RuntimeError(
        "Speech-to-text requires DEEPGRAM_API_KEY. "
        "This project is configured for Deepgram Nova-3 only."
    )


def require_crew_llm_credentials() -> None:
    """
    CrewAI agents require Anthropic credentials for Claude Opus 4.6.

    Raises:
        RuntimeError: When neither provider key is set.
    """
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        return
    raise RuntimeError(
        "CrewAI agents require ANTHROPIC_API_KEY for Claude Opus 4.6."
    )
