"""
crew_llm.py — Shared CrewAI LLM wiring for production OpenAI usage.
"""

from __future__ import annotations

import os
from typing import Any

from utils.runtime_config import require_crew_llm_credentials

# Default reasoning model. GPT-4o is a strong, cost-effective general model with reliable
# JSON/structured output and broad availability — a good default for report writing and
# summarization. Override per-deployment with the OPENAI_MODEL env var.
DEFAULT_OPENAI_MODEL = "gpt-4o"


def get_openai_model() -> str:
    """Return the litellm-style OpenAI model id (e.g. ``openai/gpt-4o``).

    Configurable via ``OPENAI_MODEL`` (with or without the ``openai/`` provider prefix).
    """
    model = (os.getenv("OPENAI_MODEL", "") or "").strip() or DEFAULT_OPENAI_MODEL
    # litellm/CrewAI route by a ``provider/model`` id; add the prefix when omitted.
    if "/" not in model:
        model = f"openai/{model}"
    return model


def get_shared_agent_llm() -> Any:
    """
    One production LLM identifier for workflow, matcher, writer, and QA agents.

    Raises:
        RuntimeError: Missing required OpenAI credentials.
    """
    require_crew_llm_credentials()
    return get_openai_model()
