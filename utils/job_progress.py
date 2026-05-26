"""
job_progress.py — Pipeline steps for /status polling and the preview UI.
"""

from __future__ import annotations

import copy
from typing import Any

from utils.pipeline_config import PIPELINE_V2, get_jobdoc_pipeline

# v1 — legacy JobDocCrew
STEP_ORDER_V1: list[str] = [
    "audio",
    "workflow",
    "transcription",
    "matching",
    "writing",
    "qa",
    "crew",
    "pdf",
]

# v2 — narration-only pipeline (frontend-facing labels)
STEP_ORDER_V2: list[str] = [
    "transcription",
    "analyze",
    "domain",
    "writing",
    "qa",
    "pdf",
]


def step_order_for_pipeline(pipeline: str | None = None) -> list[str]:
    pipeline = (pipeline or get_jobdoc_pipeline()).lower()
    return STEP_ORDER_V2 if pipeline == PIPELINE_V2 else STEP_ORDER_V1


def default_pipeline_steps(pipeline: str | None = None) -> list[dict[str, Any]]:
    """Initial step list for the active pipeline version."""
    if (pipeline or get_jobdoc_pipeline()) == PIPELINE_V2:
        meta = [
            ("transcription", "Transcribe audio", "Transcribing audio…"),
            ("analyze", "Analyze transcript", "Analyzing transcript and Mark-this cues…"),
            ("domain", "Domain expertise", "Applying domain expertise…"),
            ("writing", "Write report", "Writing professional report…"),
            ("qa", "Quality review", "Quality review…"),
            ("pdf", "Generate PDF", "Generating PDF…"),
        ]
    else:
        meta = [
            ("audio", "Prepare audio", "Extracting an audio track from your recording…"),
            ("workflow", "Plan workflow", "Deriving capture steps from selected JSON templates…"),
            ("transcription", "Transcribe speech", "Running speech-to-text (Deepgram Nova-3)…"),
            ("matching", "Match “Mark this.” cues", "Detecting markers and aligning sections…"),
            ("writing", "Write report", "Building structured summaries for each section…"),
            ("qa", "Quality review", "Checking coverage and consistency…"),
            ("crew", "Agent synthesis", "Running CrewAI agents…"),
            ("pdf", "Generate PDF", "Extracting still frames and rendering the PDF…"),
        ]
    return [
        {"id": sid, "label": label, "status": "pending", "description": hint}
        for sid, label, hint in meta
    ]


def sync_steps_from_progress(
    steps: list[dict[str, Any]],
    current_step_id: str,
    detail: str,
    *,
    failed: bool = False,
    pipeline: str | None = None,
) -> list[dict[str, Any]]:
    out = copy.deepcopy(steps)
    if not out:
        out = default_pipeline_steps(pipeline)
    idx_map = {s["id"]: i for i, s in enumerate(out)}
    cur = idx_map.get(current_step_id, -1)
    for i, row in enumerate(out):
        if failed and i == cur:
            row["status"] = "failed"
            row["description"] = detail
            continue
        if cur < 0:
            continue
        if i < cur:
            row["status"] = "completed"
        elif i == cur:
            row["status"] = "failed" if failed else "running"
            row["description"] = detail
        else:
            if row["status"] != "failed":
                row["status"] = "pending"
    return out


def mark_all_completed(steps: list[dict[str, Any]], final_detail: str = "") -> list[dict[str, Any]]:
    out = copy.deepcopy(steps)
    for row in out:
        row["status"] = "completed"
        if final_detail and row["id"] == "pdf":
            row["description"] = final_detail
    return out


def merge_pipeline_into_meta(meta: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    m = dict(meta)
    m["pipeline_steps"] = steps
    return m
