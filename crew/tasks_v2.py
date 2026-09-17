"""
tasks_v2.py — Narration-only sequential CrewAI tasks.
"""

from __future__ import annotations

import json
from typing import Any

from crewai import Task

from crew.models_v2 import (
    DomainExpertAnalysisOutput,
    LOW_CONFIDENCE_THRESHOLD,
    MIN_SUMMARY_WORDS,
    QAReviewOutput,
    TranscriptAnalysisOutput,
)
from utils.prompt_utils import load_prompt_v2
from utils.template_guidance import template_writer_rules_json
from utils.schemas import FinalReportOutput, ReportTemplate


def build_v2_tasks(
    agents: dict[str, Any],
    *,
    templates: list[ReportTemplate],
    transcript_json: str,
    video_stem: str,
    video_filename: str,
    report_type: str,
    mark_events_json: str = "[]",
    section_narration_json: str = "[]",
) -> tuple[list[Task], dict[str, Task]]:
    templates_json = json.dumps([t.model_dump() for t in templates], indent=2, ensure_ascii=False)
    writer_rules_json = template_writer_rules_json(templates)

    analyze_task = Task(
        description=load_prompt_v2(
            "task_transcript_analyzer",
            report_templates_json=templates_json,
            transcript_json=transcript_json,
            mark_events_json=mark_events_json,
        ),
        expected_output=(
            "TranscriptAnalysisOutput JSON: observations with context_before/after, "
            "noise_factors, inferred_terms, confidence 0-1, confidence_band, "
            "requires_site_verification when confidence < "
            f"{LOW_CONFIDENCE_THRESHOLD}."
        ),
        output_pydantic=TranscriptAnalysisOutput,
        agent=agents["transcript_analyzer"],
        max_iter=2,
    )

    domain_task = Task(
        description=load_prompt_v2(
            "task_domain_expert",
            report_type=report_type,
        ),
        expected_output=(
            "DomainExpertAnalysisOutput JSON with substantive 4-8 sentence domain_assessment "
            "per observation even when narration is brief; low_confidence_flag when "
            f"confidence < {LOW_CONFIDENCE_THRESHOLD}."
        ),
        output_pydantic=DomainExpertAnalysisOutput,
        agent=agents["domain_expert"],
        context=[analyze_task],
        max_iter=1,
    )

    write_task = Task(
        description=load_prompt_v2(
            "task_report_writer",
            report_templates_json=templates_json,
            template_writer_rules_json=writer_rules_json,
            section_narration_json=section_narration_json,
            video_stem=video_stem,
            video_filename=video_filename,
        ),
        expected_output=(
            f"FinalReportOutput JSON; every summary >= {MIN_SUMMARY_WORDS} words, "
            "senior-inspector structure; expand brief narration via template/domain "
            f"without fabricating; flag low-confidence when < {LOW_CONFIDENCE_THRESHOLD}."
        ),
        output_pydantic=FinalReportOutput,
        agent=agents["report_writer"],
        context=[analyze_task, domain_task],
        max_iter=1,
    )

    qa_task = Task(
        description=load_prompt_v2(
            "task_quality_reviewer",
            report_templates_json=templates_json,
            template_writer_rules_json=writer_rules_json,
        ),
        expected_output=(
            f"QAReviewOutput JSON; requires_revision if any summary < {MIN_SUMMARY_WORDS} "
            "words or vague; supply revised_report when failing."
        ),
        output_pydantic=QAReviewOutput,
        agent=agents["quality_reviewer"],
        context=[analyze_task, domain_task, write_task],
        max_iter=1,
    )

    task_map = {
        "analyze": analyze_task,
        "domain": domain_task,
        "write": write_task,
        "qa": qa_task,
    }
    return [analyze_task, domain_task, write_task, qa_task], task_map
