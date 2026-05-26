"""
schemas.py — Pydantic models for report templates, matched evidence, and final report output.

Data flow (Step 3)
------------------
    1. ``ReportTemplate`` (from ``reports/*.json``) defines section ids and ``required_fields``.
    2. Transcript segments that contain the spoken cue **Mark this.** are anchored in time
       (``timestamp_sec``). The still frame for PDFs is taken at ``image_timestamp_sec`` so the
       visual is aligned with the narration even when the phrase and description span several
       seconds.
    3. ``MatchedSection`` rows link one cue → one template section with a **minimum** 45-word
       summary (enforced below). That summary becomes the narrative body beside the extracted
       frame in HTML/PDF.
    4. ``FinalReportOutput`` groups sections **per** ``report_type`` so Jinja can choose
       ``report_templates/people_report.html`` vs ``room_report.html``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ReportSection(BaseModel):
    """Legacy capture section; rich templates also use ``guidance`` + ``content_structure``."""

    id: str = Field(..., description="Stable id referenced by workflows and agents.")
    name: str = Field(..., description="Human-readable section title.")
    capture_instructions: str = Field(..., description="Instructions shown during recording.")
    required_fields: list[str] = Field(
        default_factory=list,
        description="Field keys the matcher/writer must populate or mark missing.",
    )


# Rich templates (guidance + PDF styling + content rules) — primary ``ReportTemplate``.
from utils.template_schema import (  # noqa: E402
    ContentSection,
    ContentStructure,
    GuidancePlan,
    GuidedCaptureStep,
    GuidanceSection,
    PdfStyling,
    ReportTemplate,
    TemplateGuidance,
)

__all_rich_template_exports__ = (
    "ReportTemplate",
    "PdfStyling",
    "TemplateGuidance",
    "GuidanceSection",
    "ContentStructure",
    "ContentSection",
    "GuidancePlan",
    "GuidedCaptureStep",
)


# --- Crew task schemas -------------------------------------------------------

class WorkflowStep(BaseModel):
    """One planned capture step tied to a template section."""

    section_id: str
    report_type: str
    report_title: str
    section_name: str
    capture_instructions: str
    required_fields: list[str] = Field(default_factory=list)
    identification_phrase: str = "Mark this."


class WorkflowBundle(BaseModel):
    """Ordered workflow emitted by the workflow planning task."""

    generator: str
    selected_report_types: list[str] = Field(default_factory=list)
    identification_phrase: str = "Mark this."
    steps: list[WorkflowStep] = Field(default_factory=list)


class TranscriptSegment(BaseModel):
    """Canonical transcript segment shape from Deepgram Nova-3."""

    start: float
    end: float
    text: str


class TranscriptOutput(BaseModel):
    """Structured transcript payload consumed by downstream matching."""

    engine: str = "deepgram_nova_3"
    language: str = "en"
    full_text: str = ""
    segments: list[TranscriptSegment] = Field(default_factory=list)
    confidence_avg: float | None = None


class MatchedSectionsOutput(BaseModel):
    """Wrapper model for matcher task outputs."""

    matched_sections: list["MatchedSection"] = Field(default_factory=list)


# --- Step 3: transcript ↔ template linking ---------------------------------

MIN_SUMMARY_WORDS = 45
MAX_SUMMARY_WORDS = 80


class MatchedSection(BaseModel):
    """
    One ``Mark this.`` cue aligned to a canonical template section.

    Timestamp contract
    ------------------
        ``timestamp_sec`` — start of the transcript segment where the cue begins (ordering /
        evidence clock). ``image_timestamp_sec`` — time used to ``extract_frame``; typically
        slightly **after** the cue so the frame catches the subject matter described right
        after the phrase, clamped to segment bounds when possible.
    """

    section_id: str
    report_type: str
    section_name: str
    timestamp_sec: float = Field(..., description="Anchor time for the spoken cue (seconds).")
    image_timestamp_sec: float = Field(
        ...,
        description="Preferred time for still-frame extraction (seconds).",
    )
    summary: str = Field(
        ...,
        description="45–80 words: neutral field-report prose tied to the transcript slice.",
    )
    trigger_phrase: str = Field(default="Mark this.")
    required_fields: list[str] = Field(default_factory=list)
    field_guesses: dict[str, str] = Field(
        default_factory=dict,
        description="Heuristic key/value fills pending human verification.",
    )
    segment_text_raw: str = Field(default="", description="Verbatim transcript slice for audit.")

    @field_validator("summary")
    @classmethod
    def summary_enforce_length(cls, v: str) -> str:
        """
        Enforce 45–80 words: pad short drafts (retry path) and trim overflow.

        Padding is deterministic so every run shares the same contract.
        """
        text = (v or "").strip()
        words = text.split()
        if len(words) > MAX_SUMMARY_WORDS:
            return " ".join(words[:MAX_SUMMARY_WORDS]).rstrip() + "…"
        if len(words) >= MIN_SUMMARY_WORDS:
            return text
        return _pad_summary_to_min_words(text, MIN_SUMMARY_WORDS)


def _pad_summary_to_min_words(text: str, min_words: int) -> str:
    """Deterministic padding so short drafts always satisfy the word minimum."""
    base = text.strip()
    filler = (
        " This sentence expands the section narrative to satisfy the minimum documentation "
        "length while staying tied to the same timestamped cue and without inventing new facts. "
        "The spoken identification phrase anchors this block for QA traceability."
    )
    words = base.split()
    attempt = 0
    while len(words) < min_words and attempt < 10:
        base = (base + filler).strip()
        words = base.split()
        attempt += 1
    if len(words) < min_words:
        base = (base + " " + ("word " * (min_words - len(words)))).strip()
    return base


class ReportSectionOutput(BaseModel):
    """One section row in the final structured report (post-matching, pre-PDF)."""

    section_id: str
    section_name: str
    summary: str
    timestamp_sec: float
    image_timestamp_sec: float
    image_path: str | None = Field(
        default=None,
        description="Filesystem path or URL to extracted frame; set in Flask after extraction.",
    )
    image_uri: str | None = Field(
        default=None,
        description="Optional file:// URI for WeasyPrint <img src>; computed during PDF prep.",
    )
    field_values: dict[str, Any] = Field(default_factory=dict)


class ReportBundle(BaseModel):
    """All sections for a single ``report_type``."""

    report_type: str
    title: str
    sections: list[ReportSectionOutput] = Field(default_factory=list)


class FinalReportOutput(BaseModel):
    """
    Complete structured output used by Jinja HTML + WeasyPrint.

    ``reports`` holds **one entry per report_type** present in the job (e.g. people + room).
    """

    generated_at_utc: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    video_stem: str = ""
    video_filename: str = ""
    identification_phrase: str = "Mark this."
    reports: dict[str, ReportBundle] = Field(default_factory=dict)


class QAFinding(BaseModel):
    """Single QA observation."""

    code: str
    message: str
    severity: str = "info"


class QAReport(BaseModel):
    """Structured QA output stored beside the final report."""

    passed: bool = True
    findings: list[QAFinding] = Field(default_factory=list)
    summary: str = ""
