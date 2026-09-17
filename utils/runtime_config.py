"""
runtime_config.py — Fail-fast checks that required API credentials exist.

jobdoc-demo runs only against real speech-to-text and real LLM providers.
"""

from __future__ import annotations

import os


def _normalize_keys() -> None:
    """Fold any alias-spelled secrets (e.g. OPEN_AI_API_KEY) into canonical names."""
    try:
        from utils.credentials import normalize_provider_env

        normalize_provider_env()
    except Exception:  # noqa: BLE001 — never let normalization break the check itself
        pass


def require_asr_credentials() -> None:
    """
    Deepgram is required for transcription.

    Raises:
        RuntimeError: With README-oriented guidance when neither is available.
    """
    _normalize_keys()
    if os.getenv("DEEPGRAM_API_KEY", "").strip():
        return
    raise RuntimeError(
        "Speech-to-text requires DEEPGRAM_API_KEY. "
        "This project is configured for Deepgram Nova-3 only."
    )


def require_crew_llm_credentials() -> None:
    """
    CrewAI agents require OpenAI credentials for the reasoning model.

    Raises:
        RuntimeError: When the OpenAI key is not set (under any accepted alias).
    """
    _normalize_keys()
    if os.getenv("OPENAI_API_KEY", "").strip():
        return
    raise RuntimeError(
        "CrewAI agents require OPENAI_API_KEY for the report-writing model. "
        "On Modal, add it to the 'jobdoc-secrets' Secret (OPENAI_API_KEY=sk-…)."
    )
