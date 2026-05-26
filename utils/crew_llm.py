"""
crew_llm.py — Shared CrewAI LLM wiring for production Anthropic usage.
"""

from __future__ import annotations

from typing import Any

from utils.runtime_config import require_crew_llm_credentials

CLAUDE_OPUS_46_LITELLM = "anthropic/claude-opus-4-6"


def get_shared_agent_llm() -> Any:
    """
    One production LLM identifier for workflow, matcher, writer, and QA agents.

    Raises:
        RuntimeError: Missing required Anthropic credentials.
    """
    require_crew_llm_credentials()
    return CLAUDE_OPUS_46_LITELLM
