"""
tasks.py — CrewAI ``Task`` graph for jobdoc-demo.

Sequential chain
----------------
    Workflow → Transcription → Matching (“Mark this.”) → Report Writing → QA

Prompt files
------------
    Long-form instructions live under ``prompts/``. The ``reference_*`` JSON strings
    are produced by the Python pipeline (real ASR + ``structured_pipeline``) and are
    embedded so agents see the same facts that drive PDF generation.
"""

from __future__ import annotations

import json
from typing import Any

from crewai import Task

from crew.agents import IDENTIFICATION_PHRASE
from utils.prompt_utils import load_prompt
from utils.schemas import (
    FinalReportOutput,
    MatchedSection,
    MatchedSectionsOutput,
    QAReport,
    TranscriptOutput,
    WorkflowBundle,
)


def build_pipeline_tasks(
    agents: dict[str, Any],
    *,
    report_templates_json: str,
    selected_report_types: list[str],
    video_path: str,
    job_metadata_json: str,
    reference_transcript_json: str,
    reference_matched_json: str,
    reference_final_report_json: str,
    reference_qa_json: str,
) -> list[Task]:
    # Fail fast if deterministic reference payloads drift from our typed contracts.
    TranscriptOutput.model_validate_json(reference_transcript_json)
    FinalReportOutput.model_validate_json(reference_final_report_json)
    QAReport.model_validate_json(reference_qa_json)
    _ = [MatchedSection.model_validate(row) for row in json.loads(reference_matched_json or "[]")]

    workflow_planner = agents["workflow_planner"]
    transcriber = agents["transcriber"]
    matcher = agents["matcher"]
    report_writer = agents["report_writer"]
    qa = agents["qa"]

    workflow_description = load_prompt(
        "workflow_generator",
        report_templates_json=report_templates_json,
        selected_report_types=", ".join(selected_report_types),
        job_metadata=job_metadata_json,
        output_language="English",
    )
    workflow_generation_task = Task(
        description=workflow_description,
        expected_output="JSON workflow bundle with ordered steps referencing template section ids.",
        output_pydantic=WorkflowBundle,
        agent=workflow_planner,
        max_iter=1,
    )

    transcription_description = load_prompt(
        "transcription_task",
        video_path=video_path,
        identification_phrase=IDENTIFICATION_PHRASE,
        reference_transcript_json=reference_transcript_json,
    )
    transcription_task = Task(
        description=transcription_description,
        expected_output="JSON transcript bundle with segments[] (timestamps + text).",
        output_pydantic=TranscriptOutput,
        agent=transcriber,
        context=[workflow_generation_task],
        max_iter=1,
    )

    matcher_description = load_prompt(
        "matcher_prompt",
        report_templates_json=report_templates_json,
        transcript_json=reference_transcript_json,
        identification_phrase=IDENTIFICATION_PHRASE,
        reference_matched_json=reference_matched_json,
        output_language="English",
        matcher_hints=(
            "Use transcript content from transcription task context as the factual source, "
            "extract narration from each cue through the next cue, and preserve canonical keys "
            "``timestamp_sec`` and ``image_timestamp_sec``."
        ),
    )
    matching_task = Task(
        description=matcher_description,
        expected_output=(
            "JSON array of MatchedSection objects with section_id, timestamp_sec, "
            "image_timestamp_sec, summary (45–80 words)."
        ),
        output_pydantic=MatchedSectionsOutput,
        agent=matcher,
        context=[workflow_generation_task, transcription_task],
        max_iter=1,
    )

    report_writer_description = load_prompt(
        "report_writer_prompt",
        matched_sections_json=(
            "Primary source: MatchedSectionsOutput from matching_task context.\n"
            "Reference shape example:\n"
            f"{reference_matched_json}"
        ),
        report_templates_json=report_templates_json,
        tone="neutral technical field report",
        audience="supervisors and compliance reviewers",
        reference_final_report_json=reference_final_report_json,
    )
    report_writing_task = Task(
        description=report_writer_description,
        expected_output="Single JSON object: FinalReportOutput with reports{} map.",
        output_pydantic=FinalReportOutput,
        agent=report_writer,
        context=[workflow_generation_task, transcription_task, matching_task],
        max_iter=1,
    )

    qa_description = load_prompt(
        "qa_prompt",
        final_report_json=reference_final_report_json,
        report_templates_json=report_templates_json,
        severity_scale="info < warning < error",
        reference_qa_json=reference_qa_json,
    )
    qa_task = Task(
        description=qa_description,
        expected_output="JSON object QAReport with passed, findings[], summary.",
        output_pydantic=QAReport,
        agent=qa,
        context=[workflow_generation_task, transcription_task, matching_task, report_writing_task],
        max_iter=1,
    )

    return [
        workflow_generation_task,
        transcription_task,
        matching_task,
        report_writing_task,
        qa_task,
    ]
