"""
workflow_stub.py — Deterministic workflow generation helpers.

Templates under ``reports/*.json`` remain the single source of truth. This module
expands them into an ordered list of steps for:

    - The recording UI (human-readable plan + same ids the crew will use later).
    - Crew kickoff context payloads so planner prompts share the same section ordering.
"""

from __future__ import annotations

import json
from typing import Any

from utils.schemas import ReportTemplate
from utils.template_guidance import build_guidance_plan


def build_workflow_steps_from_templates(templates: list[ReportTemplate]) -> list[dict[str, Any]]:
    """Ordered capture steps — prefers rich ``guidance`` when present."""
    plan = build_guidance_plan(templates)
    if plan.steps:
        return [
            {
                "section_id": s.section_id,
                "report_type": s.report_type,
                "document_class": s.document_class,
                "report_title": s.report_title,
                "section_name": s.title,
                "capture_instructions": s.voice_prompt,
                "on_screen_text": s.on_screen_text,
                "voice_prompt": s.voice_prompt,
                "worker_instructions": s.worker_instructions,
                "success_criteria": s.success_criteria,
                "required": s.required,
                "min_marks": s.min_marks,
                "estimated_seconds": s.estimated_seconds,
                "suggested_phrases": s.suggested_phrases,
                "required_fields": s.required_fields,
                "step_index": s.step_index,
                "total_steps": s.total_steps,
                "identification_phrase": s.identification_phrase,
            }
            for s in plan.steps
        ]
    steps: list[dict[str, Any]] = []
    for tpl in templates:
        for sec in tpl.sections:
            steps.append(
                {
                    "section_id": sec.id,
                    "report_type": tpl.report_type,
                    "report_title": tpl.title,
                    "section_name": sec.name,
                    "capture_instructions": sec.capture_instructions,
                    "required_fields": list(sec.required_fields),
                    "identification_phrase": "Mark this.",
                }
            )
    return steps


def build_workflow_json_bundle(
    *,
    templates: list[ReportTemplate],
    selected_report_types: list[str],
    generator_label: str = "deterministic_v1",
) -> dict[str, Any]:
    """
    Structured workflow document shared by UI summaries and Crew outputs.

    ``selected_report_types`` is echoed for traceability / logging.
    """
    steps = build_workflow_steps_from_templates(templates)
    return {
        "generator": generator_label,
        "selected_report_types": list(selected_report_types),
        "identification_phrase": "Mark this.",
        "steps": steps,
    }


def workflow_bundle_to_pretty_json(bundle: dict[str, Any]) -> str:
    """Serialize workflow bundle for prompts and Crew task descriptions."""
    return json.dumps(bundle, indent=2, ensure_ascii=False)
