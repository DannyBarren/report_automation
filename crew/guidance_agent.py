"""
guidance_agent.py — Optional agentic polish for guided capture scripts.

The JSON template remains the single source of truth for section order, ids,
voice_prompt content, and writer fields. The agent may only refine transition
wording and intro flow for natural spoken delivery.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from utils.template_schema import GuidancePlan, ReportTemplate

logger = logging.getLogger(__name__)


def _agent_enabled() -> bool:
    return os.environ.get("JOBDOC_GUIDANCE_AGENT", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def maybe_enhance_guidance_plan(
    plan: GuidancePlan,
    templates: list[ReportTemplate],
) -> GuidancePlan:
    """
    If ``JOBDOC_GUIDANCE_AGENT=1`` and LLM credentials exist, polish intro/transition text.

    On failure or disabled, returns the template-built plan unchanged.
    """
    if not _agent_enabled() or not plan.steps:
        return plan

    try:
        from utils.runtime_config import require_crew_llm_credentials

        require_crew_llm_credentials()
    except RuntimeError:
        logger.info("Guidance agent skipped — LLM credentials not configured.")
        return plan

    try:
        polished = _polish_with_llm(plan, templates)
        return polished.model_copy(update={"source": "template+agent"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Guidance agent polish failed, using template plan: %s", exc)
        return plan


def _polish_with_llm(plan: GuidancePlan, templates: list[ReportTemplate]) -> GuidancePlan:
    from utils.crew_llm import get_shared_agent_llm

    llm = get_shared_agent_llm()
    template_digest = [
        {
            "report_type": t.report_type,
            "title": t.title,
            "sections": [
                {
                    "section_id": g.section_id,
                    "title": g.title,
                    "voice_prompt": g.voice_prompt,
                }
                for g in t.sorted_guidance_sections()
            ],
        }
        for t in templates
    ]
    steps_in = [
        {
            "step_index": s.step_index,
            "section_id": s.section_id,
            "title": s.title,
            "transition_line": s.transition_line,
            "spoken_intro": s.spoken_intro,
        }
        for s in plan.steps
    ]
    prompt = f"""You polish field-inspection spoken guidance. Rules:
- Do NOT add, remove, or reorder sections.
- Do NOT change section_id values.
- Keep the cue phrase "{plan.identification_phrase}" in every spoken_intro.
- Improve transition_line and spoken_intro to sound natural for text-to-speech.
- Keep intro_script under 120 words; professional and encouraging.

Template (authoritative structure):
{json.dumps(template_digest, indent=2)}

Current plan:
{json.dumps({"intro_script": plan.intro_script, "outro_script": plan.outro_script, "steps": steps_in}, indent=2)}

Return JSON only:
{{
  "intro_script": "...",
  "outro_script": "...",
  "steps": [{{"step_index": 1, "transition_line": "...", "spoken_intro": "..."}}]
}}
"""
    raw = llm.invoke(prompt)
    text = raw.content if hasattr(raw, "content") else str(raw)
    text = text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)

    step_by_index = {s["step_index"]: s for s in data.get("steps") or [] if isinstance(s, dict)}
    new_steps = []
    for step in plan.steps:
        patch = step_by_index.get(step.step_index) or {}
        new_steps.append(
            step.model_copy(
                update={
                    "transition_line": patch.get("transition_line") or step.transition_line,
                    "spoken_intro": patch.get("spoken_intro") or step.spoken_intro,
                }
            )
        )
    return plan.model_copy(
        update={
            "intro_script": data.get("intro_script") or plan.intro_script,
            "outro_script": data.get("outro_script") or plan.outro_script,
            "steps": new_steps,
        }
    )
