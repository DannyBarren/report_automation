"""
flask_pipeline.py — Bridge v1/v2 pipelines to Flask ``generate_report`` route.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from crew.jobdoc_crew_v2 import (
    build_low_confidence_user_messages,
    collect_v2_warnings,
    generate_report_from_video,
)
from crew.models_v2 import PipelineResultV2
from crew.report_resilience import build_review_items, merge_review_notes
from utils.pipeline_config import PIPELINE_V2, crew_verbose, get_jobdoc_pipeline
from utils.pipeline_errors import friendly_error_message
from utils.pipeline_logging import job_step_timer, log_job_step
from utils.schemas import FinalReportOutput

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, str], None]


@dataclass
class ReportPipelineOutcome:
    """Normalized result for PDF rendering regardless of pipeline version."""

    pipeline: str
    final_report: FinalReportOutput
    payload: dict[str, Any]
    pipeline_warnings: list[str]
    review_items: list[dict[str, Any]]
    degraded: bool = False
    low_confidence_messages: list[str] = field(default_factory=list)


def build_mark_moments_from_v2(result: PipelineResultV2) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, obs in enumerate(result.transcript_analysis.observations):
        t = float(obs.timestamp_sec)
        label_parts = [obs.section_id or f"obs_{obs.cue_index}"]
        if obs.report_type:
            label_parts.append(obs.report_type)
        out.append(
            {
                "index": i + 1,
                "t_sec": t,
                "time_display": f"{t:.1f}s",
                "label": " · ".join(label_parts),
                "confidence": obs.confidence,
            }
        )
    return out


def v2_payload_from_result(result: PipelineResultV2) -> dict[str, Any]:
    return {
        "status": "completed",
        "pipeline": PIPELINE_V2,
        "degraded": result.degraded,
        "stage_failures": list(result.stage_failures),
        "transcript_analysis": result.transcript_analysis.model_dump(),
        "domain_analysis": result.domain_analysis.model_dump(),
        "final_report": result.final_report.model_dump(),
        "qa_review": result.qa_review.model_dump(),
        "revision_applied": result.revision_applied,
        "frame_timestamps": [f.model_dump() for f in result.frame_timestamps],
        "matched_sections": [
            {
                "section_id": o.section_id,
                "report_type": o.report_type,
                "section_name": o.section_id,
                "timestamp_sec": o.timestamp_sec,
                "image_timestamp_sec": o.image_timestamp_sec,
                "summary": o.cleaned_observation,
            }
            for o in result.transcript_analysis.observations
        ],
    }


def run_v2_pipeline(
    video_path: Path,
    *,
    templates_json: str,
    on_progress: ProgressCallback,
    video_id: int | None = None,
    review_notes_json: str | None = None,
) -> ReportPipelineOutcome:
    """
    Run v2 pipeline — always returns a usable report (may be degraded).

    Never raises to Flask; catastrophic errors become structured fallbacks.
    """
    templates = json.loads(templates_json)
    log_job_step(video_id, "v2", f"start path={video_path.name}")

    try:
        with job_step_timer(video_id, "v2_crew"):
            result = generate_report_from_video(
                video_path,
                templates,
                verbose=crew_verbose(),
                on_progress=on_progress,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("v2 pipeline catastrophic failure")
        from crew.report_resilience import build_pipeline_result_fallback, empty_transcript_payload
        from utils.template_schema import ReportTemplate

        tpl_models = [ReportTemplate.model_validate(row) for row in templates]
        result = build_pipeline_result_fallback(
            empty_transcript_payload(),
            tpl_models,
            video_stem=video_path.stem,
            video_filename=video_path.name,
            stage_notes=[friendly_error_message(exc)],
        )

    if review_notes_json:
        merged = merge_review_notes(result.final_report, review_notes_json)
        result = result.model_copy(update={"final_report": merged})

    warnings = collect_v2_warnings(result)
    low_conf = build_low_confidence_user_messages(result)
    review_items = build_review_items(result.final_report, result.transcript_analysis)

    payload = v2_payload_from_result(result)
    payload["pipeline_warnings"] = warnings
    payload["review_items"] = review_items
    payload["low_confidence_messages"] = low_conf

    log_job_step(
        video_id,
        "v2",
        f"done degraded={result.degraded} sections={sum(len(b.sections) for b in result.final_report.reports.values())}",
    )

    return ReportPipelineOutcome(
        pipeline=PIPELINE_V2,
        final_report=result.final_report,
        payload=payload,
        pipeline_warnings=warnings,
        review_items=review_items,
        degraded=result.degraded,
        low_confidence_messages=low_conf,
    )


def run_v1_pipeline(
    video_path: Path,
    *,
    selected: list[str],
    templates_json: str,
    video_id: int,
    on_progress: ProgressCallback,
) -> ReportPipelineOutcome:
    from crew.crew import JobDocCrew
    from crew.report_resilience import build_review_items

    crew = JobDocCrew(
        video_path=str(video_path),
        selected_report_types=selected,
        report_templates_json=templates_json,
        verbose=crew_verbose(),
        on_progress=on_progress,
    )
    try:
        with job_step_timer(video_id, "v1_crew"):
            payload = crew.kickoff(
                inputs={
                    "video_path": str(video_path),
                    "selected_report_types": selected,
                    "report_templates_json": templates_json,
                    "job_metadata_json": json.dumps({"video_id": video_id, "phase": "generate_report"}),
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("v1 pipeline failed — attempting structured fallback")
        from crew.report_resilience import build_pipeline_result_fallback, empty_transcript_payload
        from utils.template_schema import ReportTemplate

        tpl_models = [ReportTemplate.model_validate(row) for row in json.loads(templates_json)]
        fb = build_pipeline_result_fallback(
            empty_transcript_payload(),
            tpl_models,
            video_stem=video_path.stem,
            video_filename=video_path.name,
            stage_notes=[friendly_error_message(exc)],
        )
        payload = v2_payload_from_result(fb)
        payload["pipeline"] = "v1"
        payload["pipeline_warnings"] = fb.stage_failures
        return ReportPipelineOutcome(
            pipeline="v1",
            final_report=fb.final_report,
            payload=payload,
            pipeline_warnings=list(fb.stage_failures),
            review_items=build_review_items(fb.final_report),
            degraded=True,
        )

    warnings = list(payload.get("pipeline_warnings") or [])
    payload["pipeline"] = "v1"
    final = FinalReportOutput.model_validate(payload["final_report"])
    return ReportPipelineOutcome(
        pipeline="v1",
        final_report=final,
        payload=payload,
        pipeline_warnings=warnings,
        review_items=build_review_items(final),
        degraded=False,
    )
