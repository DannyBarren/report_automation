"""
template_schema.py — Rich report templates (guidance + PDF + content) in ``reports/*.json``.

Legacy templates with only ``sections[]`` are upgraded automatically on load.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from utils.schemas import ReportSection

IDENTIFICATION_PHRASE = "Mark this."


class PdfStyling(BaseModel):
    """PDF layout and branding — consumed by ``template_driven.html`` only."""

    primary_color: str = "#1e3a8a"
    secondary_color: str = "#64748b"
    accent_color: str = "#2563eb"
    font_family: str = "Helvetica, Arial, sans-serif"
    header_style: str = "bold"
    photo_grid: str = "2-column with caption below"
    photo_aspect_ratio: str = "4/3"
    photo_caption_max_words: int = 42
    include_timestamps: bool = True
    include_narration_in_caption: bool = True
    page_margin_mm: str = "16mm 14mm 22mm 14mm"
    page_footer: str = "{business_name} · Confidential · Page {page} of {total_pages}"
    page_header: str = "{report_title}"
    logo_placement: str = "header-left"
    logo_max_height_px: int = 56
    cover_show_job_address: bool = True
    job_address_label: str = "Property / job site"
    show_running_header: bool = True
    severity_colors: dict[str, str] = Field(
        default_factory=lambda: {
            "informational": "#64748b",
            "minor": "#ca8a04",
            "moderate": "#ea580c",
            "major": "#dc2626",
            "safety_critical": "#7f1d1d",
        },
    )
    severity_bands: dict[str, str] = Field(
        default_factory=lambda: {
            "informational": "green",
            "minor": "green",
            "moderate": "yellow",
            "major": "red",
            "safety_critical": "red",
            "none": "green",
            "low": "green",
            "medium": "yellow",
            "high": "red",
        },
        description="Maps severity keys to green | yellow | red for badges.",
    )
    band_styles: dict[str, dict[str, str]] = Field(
        default_factory=lambda: {
            "green": {"bg": "#dcfce7", "text": "#166534", "border": "#86efac", "label": "Satisfactory"},
            "yellow": {"bg": "#fef9c3", "text": "#854d0e", "border": "#fde047", "label": "Attention"},
            "red": {"bg": "#fee2e2", "text": "#991b1b", "border": "#fca5a5", "label": "Action required"},
        },
    )
    show_summary_page: bool = True
    summary_show_severity_counts: bool = True
    show_disclaimer: bool = True
    disclaimer_text: str = (
        "This report documents conditions observed during a guided field inspection based on "
        "technician narration and time-stamped still images. It is not a warranty, guarantee, "
        "or code-compliance certification unless expressly stated."
    )
    recommendation_callout_style: str = "border-left accent"
    table_style: str = "striped"
    section_page_break: str = "before"
    cover_subtitle: str = "Professional inspection report"
    show_photo_appendix: bool = True

    def resolve_severity_band(self, raw: str | None) -> str:
        s = str(raw or "informational").strip().lower().replace(" ", "_")
        aliases = {
            "info": "informational",
            "ok": "informational",
            "none": "informational",
            "low": "minor",
            "med": "moderate",
            "medium": "moderate",
            "high": "major",
            "critical": "safety_critical",
            "urgent": "safety_critical",
        }
        key = aliases.get(s, s if s else "informational")
        return self.severity_bands.get(key, "yellow")

    def band_style(self, band: str) -> dict[str, str]:
        return dict(self.band_styles.get(band, self.band_styles["yellow"]))


class GuidanceSection(BaseModel):
    """One guided capture step (voice + on-screen)."""

    section_id: str
    title: str
    voice_prompt: str
    on_screen_text: str = ""
    required: bool = True
    min_marks: int = Field(default=1, ge=0)
    capture_order: int | None = None
    suggested_phrases: list[str] = Field(default_factory=list)
    identification_phrase: str = IDENTIFICATION_PHRASE


class TemplateGuidance(BaseModel):
    intro_script: str = ""
    outro_script: str = "Recording complete. Upload when ready."
    identification_phrase: str = IDENTIFICATION_PHRASE
    sections: list[GuidanceSection] = Field(default_factory=list)


class ContentFieldSpec(BaseModel):
    key: str
    label: str = ""
    field_type: str = "text"
    required: bool = True


class ContentSection(BaseModel):
    """Writer / PDF content rules per section."""

    section_id: str
    title: str
    default_text: str = ""
    fields: list[str] = Field(default_factory=list)
    field_specs: list[ContentFieldSpec] = Field(default_factory=list)
    writer_instructions: str = ""
    tone: str = "professional neutral"
    min_summary_words: int = 45
    show_photo: bool = True
    show_severity_badge: bool = True


class ContentStructure(BaseModel):
    summary_page_title: str = "Executive Summary"
    organization: str = "section_order"
    sections: list[ContentSection] = Field(default_factory=list)


class ReportTemplate(BaseModel):
    """
    Full template: guidance (capture) + content_structure (AI/PDF) + pdf_styling.

    ``sections`` is kept for backward compatibility with v1 pipeline flattening.
    """

    report_type: str
    title: str
    business_name: str = ""
    logo_url: str | None = None
    job_address: str = Field(
        default="",
        description="Shown on cover (property address, site name, or job id).",
    )
    schema_version: int = 2
    featured: bool = Field(
        default=False,
        description="When true, listed first on the homepage as a recommended template.",
    )
    template_description: str = Field(
        default="",
        description="Short marketing blurb for template selection UI.",
    )
    pdf_styling: PdfStyling = Field(default_factory=PdfStyling)
    guidance: TemplateGuidance | None = None
    content_structure: ContentStructure | None = None
    sections: list[ReportSection] = Field(default_factory=list)

    @model_validator(mode="after")
    def sync_legacy_and_rich(self) -> ReportTemplate:
        if self.guidance and self.guidance.sections and not self.sections:
            sections = []
            content_by_id = {c.section_id: c for c in (self.content_structure.sections if self.content_structure else [])}
            for g in self.sorted_guidance_sections():
                c = content_by_id.get(g.section_id)
                fields = list(c.fields) if c else []
                sections.append(
                    ReportSection(
                        id=g.section_id,
                        name=g.title,
                        capture_instructions=g.voice_prompt,
                        required_fields=fields,
                    )
                )
            return self.model_copy(update={"sections": sections})

        if self.sections and (not self.guidance or not self.guidance.sections):
            g_sections = [
                GuidanceSection(
                    section_id=s.id,
                    title=s.name,
                    voice_prompt=s.capture_instructions,
                    on_screen_text=s.name,
                    required=True,
                    min_marks=1,
                )
                for s in self.sections
            ]
            c_sections = [
                ContentSection(
                    section_id=s.id,
                    title=s.name,
                    default_text="No major issues observed unless noted.",
                    fields=list(s.required_fields),
                )
                for s in self.sections
            ]
            guidance = TemplateGuidance(
                intro_script=f"Welcome. We will document {self.title}. Say '{IDENTIFICATION_PHRASE}' before each section.",
                sections=g_sections,
            )
            content = ContentStructure(sections=c_sections)
            return self.model_copy(update={"guidance": guidance, "content_structure": content})
        return self

    def sorted_guidance_sections(self) -> list[GuidanceSection]:
        if not self.guidance or not self.guidance.sections:
            return []
        rows = list(self.guidance.sections)
        rows.sort(key=lambda s: (s.capture_order if s.capture_order is not None else 999, s.section_id))
        return rows

    def content_for_section(self, section_id: str) -> ContentSection | None:
        if not self.content_structure:
            return None
        for c in self.content_structure.sections:
            if c.section_id == section_id:
                return c
        return None

    def writer_context_json(self) -> dict[str, Any]:
        """Compact template rules for Report Writer prompts."""
        return {
            "report_type": self.report_type,
            "title": self.title,
            "business_name": self.business_name,
            "content_structure": self.content_structure.model_dump() if self.content_structure else {},
            "pdf_styling": self.pdf_styling.model_dump(),
        }


class GuidedCaptureStep(BaseModel):
    """One step returned to Flask / recording UI."""

    step_index: int
    total_steps: int
    section_id: str
    report_type: str
    report_title: str
    title: str
    voice_prompt: str
    on_screen_text: str
    required: bool
    min_marks: int
    suggested_phrases: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    writer_instructions: str = ""
    tone: str = "professional neutral"
    min_summary_words: int = 45
    identification_phrase: str = IDENTIFICATION_PHRASE
    transition_line: str = Field(
        default="",
        description="Spoken bridge from the previous section (template or agent-polished).",
    )
    spoken_intro: str = Field(
        default="",
        description="Full TTS script for this step (transition + cue instruction).",
    )


class GuidancePlan(BaseModel):
    """Full guided session plan for one or more templates."""

    identification_phrase: str = IDENTIFICATION_PHRASE
    intro_script: str = ""
    outro_script: str = ""
    steps: list[GuidedCaptureStep] = Field(default_factory=list)
    templates: list[str] = Field(default_factory=list)
    source: str = Field(
        default="template",
        description="template | template+agent — JSON template remains authoritative for order/ids.",
    )
