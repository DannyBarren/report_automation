"""
models_v2.py — Pydantic contracts for the narration-only JobDoc v2 pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

from utils.schemas import FinalReportOutput, ReportTemplate
from utils.template_schema import (  # re-export for crew / Flask convenience
    ContentSection,
    ContentStructure,
    GuidancePlan,
    GuidedCaptureStep,
    GuidanceSection,
    PdfStyling,
    TemplateGuidance,
)

IDENTIFICATION_PHRASE = "Mark this."
MIN_SUMMARY_WORDS = 45
MAX_SUMMARY_WORDS = 120

# Confidence thresholds (aligned across analyzer → domain → writer → QA)
LOW_CONFIDENCE_THRESHOLD = 0.65
VERY_LOW_CONFIDENCE_THRESHOLD = 0.50

NARRATION_ONLY_RULES = (
    "You have no access to video images. Base every conclusion exclusively on the technician's spoken words. "
    "Be faithful to what was narrated. Do not invent details. If something is unclear, note it professionally. "
    "Write in a professional, neutral, factual tone suitable for compliance reports and real estate transactions."
)


class SeverityLevel(str, Enum):
    informational = "informational"
    minor = "minor"
    moderate = "moderate"
    major = "major"
    safety_critical = "safety_critical"


class ConfidenceBand(str, Enum):
    """Derived band from numeric ``confidence`` for downstream handling."""

    high = "high"  # >= 0.85
    medium = "medium"  # 0.70 – 0.84
    low = "low"  # 0.50 – 0.69
    very_low = "very_low"  # < 0.50


class AsrNoiseFactor(str, Enum):
    """Common field-audio degradation tags (analyzer sets applicable factors)."""

    background_noise = "background_noise"
    wind = "wind"
    tool_or_equipment = "tool_or_equipment"
    accent_or_dialect = "accent_or_dialect"
    fast_speech = "fast_speech"
    mumbling = "mumbling"
    dropped_words = "dropped_words"
    interruption = "interruption"
    crosstalk = "crosstalk"
    homophone_error = "homophone_error"
    proper_noun_unclear = "proper_noun_unclear"


def confidence_to_band(value: float) -> ConfidenceBand:
    """Map 0–1 score to a discrete band (shared contract for all agents)."""
    v = max(0.0, min(1.0, float(value)))
    if v >= 0.85:
        return ConfidenceBand.high
    if v >= 0.70:
        return ConfidenceBand.medium
    if v >= 0.50:
        return ConfidenceBand.low
    return ConfidenceBand.very_low


def requires_site_verification(confidence: float) -> bool:
    return float(confidence) < LOW_CONFIDENCE_THRESHOLD


class PdfFrameTimestamp(BaseModel):
    section_id: str | None = None
    report_type: str | None = None
    timestamp_sec: float
    image_timestamp_sec: float


class RawTranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    speaker_label: str | None = None


class StructuredObservation(BaseModel):
    """
    One “Mark this.” observation — narration is the sole evidence source.

    Confidence scoring (analyzer must apply)
    ----------------------------------------
    | Score   | Band      | Meaning |
    | 0.85–1.0| high      | Clear speech, complete thought, minimal ASR fixes |
    | 0.70–0.84| medium   | Minor fixes; core facts reliable |
    | 0.50–0.69| low      | Partial/garbled; qualified language required downstream |
    | < 0.50  | very_low  | Heavy guesswork; verify on site |
    """

    observation_id: str
    section_id: str | None = None
    report_type: str | None = None
    cue_index: int = Field(..., ge=0)
    timestamp_sec: float
    image_timestamp_sec: float
    trigger_phrase: str = IDENTIFICATION_PHRASE
    speaker_intent: str
    raw_narration: str = Field(..., description="Verbatim ASR after cue (unchanged).")
    cleaned_observation: str = Field(
        ...,
        description="Professional statement; use 'possibly/likely' only when context supports inference.",
    )
    context_before: str = Field(
        default="",
        description="1–2 prior segments that disambiguate the cue (timestamps optional).",
    )
    context_after: str = Field(
        default="",
        description="1–2 following segments before next cue.",
    )
    context: str = Field(
        default="",
        description="Legacy combined context; prefer context_before/after when set.",
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    confidence_band: ConfidenceBand = ConfidenceBand.medium
    noise_factors: list[AsrNoiseFactor] = Field(default_factory=list)
    asr_noise_notes: str = Field(
        default="",
        description="What was noisy, what was inferred, and what remains uncertain.",
    )
    inferred_terms: list[str] = Field(
        default_factory=list,
        description="Words/phrases inferred from context (not literal ASR).",
    )
    requires_site_verification: bool = False

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        return max(0.0, min(1.0, float(v)))

    @field_validator("noise_factors", mode="before")
    @classmethod
    def coerce_noise_factors(cls, v: object) -> list[AsrNoiseFactor]:
        if not v:
            return []
        out: list[AsrNoiseFactor] = []
        for item in v:
            if isinstance(item, AsrNoiseFactor):
                out.append(item)
            else:
                try:
                    out.append(AsrNoiseFactor(str(item)))
                except ValueError:
                    continue
        return out

    @model_validator(mode="after")
    def derive_confidence_metadata(self) -> StructuredObservation:
        band = confidence_to_band(self.confidence)
        verify = requires_site_verification(self.confidence)
        combined_ctx = self.context.strip()
        if not combined_ctx and (self.context_before or self.context_after):
            parts = []
            if self.context_before.strip():
                parts.append(f"Before: {self.context_before.strip()}")
            if self.context_after.strip():
                parts.append(f"After: {self.context_after.strip()}")
            combined_ctx = " | ".join(parts)
        return self.model_copy(
            update={
                "confidence_band": band,
                "requires_site_verification": verify,
                "context": combined_ctx or self.context,
            }
        )


class TranscriptAnalysisOutput(BaseModel):
    engine: str = "jobdoc_transcript_analyzer_v2_narration"
    identification_phrase: str = IDENTIFICATION_PHRASE
    full_transcript_text: str = ""
    segments: list[RawTranscriptSegment] = Field(default_factory=list)
    observations: list[StructuredObservation] = Field(default_factory=list)
    unmatched_cues: int = 0
    missing_cues: int = 0
    low_confidence_count: int = Field(
        default=0,
        description="Observations with confidence < LOW_CONFIDENCE_THRESHOLD.",
    )
    analysis_notes: str = ""

    @model_validator(mode="after")
    def count_low_confidence(self) -> TranscriptAnalysisOutput:
        count = sum(1 for o in self.observations if requires_site_verification(o.confidence))
        return self.model_copy(update={"low_confidence_count": count})


class DomainFinding(BaseModel):
    observation_id: str
    section_id: str | None = None
    severity: SeverityLevel = SeverityLevel.informational
    narration_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    domain_assessment: str
    code_or_standard_refs: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    client_talking_points: list[str] = Field(default_factory=list)
    low_confidence_flag: bool = False
    verification_recommended: bool = False

    @model_validator(mode="after")
    def derive_low_confidence_flags(self) -> DomainFinding:
        low = self.narration_confidence < LOW_CONFIDENCE_THRESHOLD
        verify = self.narration_confidence < LOW_CONFIDENCE_THRESHOLD
        return self.model_copy(
            update={
                "low_confidence_flag": low,
                "verification_recommended": verify,
            }
        )


class DomainExpertAnalysisOutput(BaseModel):
    report_type: str
    domain_role: str
    standards_context: str
    findings: list[DomainFinding] = Field(default_factory=list)
    global_risks: list[str] = Field(default_factory=list)
    expert_notes: str = ""


class QARevisionItem(BaseModel):
    section_id: str
    report_type: str
    issue: str
    suggested_fix: str
    priority: str = "medium"


class QAReviewOutput(BaseModel):
    passed: bool = True
    overall_score: int = Field(default=0, ge=0, le=100)
    strengths: list[str] = Field(default_factory=list)
    revisions: list[QARevisionItem] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    missing_details: list[str] = Field(default_factory=list)
    tone_issues: list[str] = Field(default_factory=list)
    low_confidence_sections: list[str] = Field(default_factory=list)
    requires_revision: bool = False
    revision_summary: str = ""
    revised_report: FinalReportOutput | None = None
    reviewer_notes: str = ""


class PipelineResultV2(BaseModel):
    generated_at_utc: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    transcript_analysis: TranscriptAnalysisOutput
    domain_analysis: DomainExpertAnalysisOutput
    final_report: FinalReportOutput
    qa_review: QAReviewOutput
    revision_applied: bool = False
    frame_timestamps: list[PdfFrameTimestamp] = Field(default_factory=list)
    degraded: bool = Field(
        default=False,
        description="True when one or more stages used fallback/partial output.",
    )
    stage_failures: list[str] = Field(
        default_factory=list,
        description="Human-readable notes per failed or skipped stage.",
    )


DOMAIN_EXPERT_PROFILES: dict[str, tuple[str, str]] = {
    "room_report": (
        "Facilities & Building Inspector",
        "ICC building practices, NFPA 101 egress, OSHA walking-working surfaces.",
    ),
    "people_report": (
        "Workplace Safety & HR Compliance Specialist",
        "OSHA 1910.132 PPE, general duty clause.",
    ),
    "home_inspection": (
        "Licensed Home Inspector",
        "InterNACHI / ASHI SOP, IRC residential systems.",
    ),
    "electrical_report": (
        "Electrical Compliance Specialist",
        "NEC NFPA 70 (210, 250, 406), NFPA 70E.",
    ),
    "plumbing_report": (
        "Plumbing Inspector",
        "IPC / UPC fixtures, leak and water damage risk.",
    ),
    "hvac_report": (
        "HVAC Technician",
        "ASHRAE comfort, combustion venting, refrigerant safety.",
    ),
}


def resolve_domain_expert(report_type: str, template: ReportTemplate | None = None) -> tuple[str, str]:
    if template is not None:
        extra = getattr(template, "domain_expert_role", None)
        standards = getattr(template, "domain_expert_standards", None)
        if extra and standards:
            return str(extra), str(standards)
    return DOMAIN_EXPERT_PROFILES.get(
        report_type,
        (
            "Professional Field Inspector",
            "Neutral inspection documentation; conclusions only from narrated facts.",
        ),
    )


# Exported rubric text for prompts (kept in sync with thresholds above)
CONFIDENCE_RUBRIC_FOR_PROMPTS = """
Confidence rubric (apply to every observation):
- 0.85–1.00 (high): Clear, complete narration; at most minor typo fixes.
- 0.70–0.84 (medium): Understandable with context; small inferred words noted in asr_noise_notes.
- 0.50–0.69 (low): Garbled/partial; cleaned_observation uses qualified language (possibly, reportedly).
- Below 0.50 (very_low): Heavy inference or missing subject; list inferred_terms; requires_site_verification=true.

Penalties (subtract roughly 0.05–0.15 each, floor 0.25): wind/tool noise drowning words, interruption mid-sentence,
mumbling, >3 homophone fixes, no measurable details when speaker tried to give them.

Never state precise numbers in cleaned_observation unless spoken or strongly supported by context; otherwise use "approximately" or omit.
"""
