"""
template_guidance.py — Build guided capture plans from rich templates (JSON = source of truth).
"""

from __future__ import annotations

import json
import re
from typing import Any

from utils.template_schema import (
    GuidedCaptureStep,
    GuidancePlan,
    GuidanceSection,
    IDENTIFICATION_PHRASE,
    ReportTemplate,
)

# Natural transitions between common inspection areas (template ids unchanged).
_SECTION_TRANSITIONS: dict[str, str] = {
    "roof_structure": "Next, move to the roof and attic area.",
    "electrical_system": "Now document the electrical system.",
    "plumbing_system": "Continue with plumbing and the water heater.",
    "hvac_system": "Next, inspect the heating and cooling equipment.",
    "interior_rooms": "Walk through the main interior rooms.",
    "basement_structure": "If safe, enter the basement or crawl space.",
    "inspection_summary": "Finally, give your overall summary and client priorities.",
    "roof_exterior": "Next, examine the roof and exterior envelope.",
    "electrical_panel": "Now document the electrical service panel.",
    "plumbing_supply": "Continue with plumbing supply and fixtures.",
    "hvac_system": "Next, film the HVAC equipment.",
    "interior_general": "Walk the interior living areas.",
    "attic_crawl": "Check the attic or crawl space if accessible.",
    "summary_recommendations": "Close with your top findings and recommendations.",
}


def _transition_for_section(section_id: str, title: str, step_index: int) -> str:
    if step_index <= 1:
        return f"We will start with {title}."
    if section_id in _SECTION_TRANSITIONS:
        return _SECTION_TRANSITIONS[section_id]
    return f"Next, document {title}."


def _build_flowing_intro(templates: list[ReportTemplate], phrase: str, total: int) -> str:
    parts: list[str] = []
    for tpl in templates:
        if tpl.guidance and tpl.guidance.intro_script:
            parts.append(tpl.guidance.intro_script.strip())

    if parts:
        base = " ".join(parts)
    else:
        names = []
        for tpl in templates:
            for g in tpl.sorted_guidance_sections():
                names.append(g.title)
        overview = ", ".join(names[:6])
        if len(names) > 6:
            overview += f", and {len(names) - 6} more"
        base = (
            f"Welcome. You will complete {total} documentation stops for "
            f"{templates[0].title if templates else 'this inspection'}. "
            f"Areas include {overview}. "
            f"At each stop, say '{phrase}' before you describe what you see."
        )

    if "Mark this" not in base and phrase not in base:
        base += f" Remember to say '{phrase}' before each section."
    return re.sub(r"\s+", " ", base).strip()


def _spoken_intro_for_step(
    transition: str,
    guidance: GuidanceSection,
    phrase: str,
) -> str:
    """Combine transition + template voice_prompt (cue phrase preserved)."""
    prompt = guidance.voice_prompt.strip()
    if phrase.lower() not in prompt.lower():
        prompt = f"Say '{phrase}'. {prompt}"
    parts = [p for p in (transition.strip(), prompt) if p]
    return " ".join(parts)


def ordered_steps_from_templates(templates: list[ReportTemplate]) -> list[dict[str, Any]]:
    """
    Return a clean ordered list of capture steps for API / session use.

    Each dict mirrors ``GuidedCaptureStep`` plus ``progress_label``. Order comes
    **only** from ``guidance.sections`` (``capture_order``) in the ReportTemplate JSON.
    """
    plan = build_guidance_plan(templates)
    from utils.guidance_session import step_payload

    total = len(plan.steps)
    return [
        step_payload(s, current_index=i, total=total)
        for i, s in enumerate(plan.steps)
    ]


def build_guidance_plan(templates: list[ReportTemplate]) -> GuidancePlan:
    """
    Flatten selected templates into ordered guided capture steps.

    Section order, ids, fields, and writer rules come **only** from the JSON template.
    """
    steps: list[GuidedCaptureStep] = []
    phrase = IDENTIFICATION_PHRASE
    outro = ""

    for tpl in templates:
        if tpl.guidance:
            phrase = tpl.guidance.identification_phrase or phrase
            if tpl.guidance.outro_script:
                outro = tpl.guidance.outro_script.strip()

        for g in tpl.sorted_guidance_sections():
            content = tpl.content_for_section(g.section_id)
            steps.append(
                GuidedCaptureStep(
                    step_index=0,
                    total_steps=0,
                    section_id=g.section_id,
                    report_type=tpl.report_type,
                    document_class=tpl.document_class,
                    report_title=tpl.title,
                    title=g.title,
                    voice_prompt=g.voice_prompt,
                    on_screen_text=g.on_screen_text or g.title,
                    worker_instructions=g.worker_instructions,
                    success_criteria=list(g.success_criteria),
                    required=g.required,
                    min_marks=g.min_marks,
                    estimated_seconds=g.estimated_seconds,
                    capture_order=g.capture_order,
                    suggested_phrases=list(g.suggested_phrases),
                    required_fields=list(content.fields) if content else [],
                    writer_instructions=content.writer_instructions if content else "",
                    tone=content.tone if content else "professional neutral",
                    min_summary_words=content.min_summary_words if content else 45,
                    identification_phrase=g.identification_phrase or phrase,
                )
            )

    total = len(steps)
    total_minutes = sum(
        (tpl.guidance.estimated_total_minutes or 0)
        for tpl in templates
        if tpl.guidance
    )
    intro = _build_flowing_intro(templates, phrase, total)

    numbered: list[GuidedCaptureStep] = []
    for i, s in enumerate(steps):
        transition = _transition_for_section(s.section_id, s.title, i + 1)
        g_sec = None
        for tpl in templates:
            if tpl.report_type != s.report_type:
                continue
            for g in tpl.sorted_guidance_sections():
                if g.section_id == s.section_id:
                    g_sec = g
                    break
        spoken = _spoken_intro_for_step(transition, g_sec, phrase) if g_sec else s.voice_prompt
        on_screen = s.on_screen_text
        numbered.append(
            s.model_copy(
                update={
                    "step_index": i + 1,
                    "total_steps": total,
                    "transition_line": transition,
                    "spoken_intro": spoken,
                    "on_screen_text": on_screen,
                }
            )
        )

    return GuidancePlan(
        identification_phrase=phrase,
        intro_script=intro,
        outro_script=outro or "All sections complete. Stop recording and upload.",
        estimated_total_minutes=total_minutes,
        steps=numbered,
        templates=[t.report_type for t in templates],
        source="template",
    )


def template_writer_rules_json(templates: list[ReportTemplate]) -> str:
    """
    Per-section writer contract for CrewAI — mirrors ``content_structure`` exactly.
    """
    rules: list[dict[str, Any]] = []
    for tpl in templates:
        cs = tpl.content_structure
        if not cs:
            continue
        # Authoritative section order (guidance capture order) for this template.
        order = {sid: i for i, sid in enumerate(tpl.section_ids_in_order())}
        for sec in cs.sections:
            specs = sec.field_specs or []
            guide = tpl.guidance_for_section(sec.section_id)
            rules.append(
                {
                    "report_type": tpl.report_type,
                    "document_class": tpl.document_class,
                    "report_title": tpl.title,
                    "section_id": sec.section_id,
                    "capture_order": order.get(sec.section_id, 999),
                    "section_title": sec.title,
                    "writer_instructions": sec.writer_instructions,
                    "tone": sec.tone,
                    "min_summary_words": sec.min_summary_words,
                    "default_text": sec.default_text,
                    "worker_instructions": guide.worker_instructions if guide else "",
                    "show_photo": sec.show_photo,
                    "show_severity_badge": sec.show_severity_badge,
                    "image_placement": sec.image_placement.model_dump(),
                    "layout_hints": sec.layout_hints.model_dump(),
                    "fields": list(sec.fields),
                    "field_specs": [fs.model_dump() for fs in specs],
                    "field_values_contract": (
                        "Populate field_values with EVERY key listed in fields (no missing keys, "
                        "no extra keys). Use labels from field_specs. severity must be one of: "
                        "no_issue, minor, moderate, major, safety_critical, informational. "
                        "If narration reports no problems, use no_issue or minor with explicit "
                        "'no issues' wording. When narration is missing, base the summary on "
                        "default_text and clearly state the limitation — never invent site facts."
                    ),
                }
            )
    return json.dumps(rules, indent=2, ensure_ascii=False)


def enrich_guidance_plan_with_agent(plan: GuidancePlan, templates: list[ReportTemplate]) -> GuidancePlan:
    """Optional LLM polish — never changes section order, ids, or required fields."""
    from crew.guidance_agent import maybe_enhance_guidance_plan

    return maybe_enhance_guidance_plan(plan, templates)
