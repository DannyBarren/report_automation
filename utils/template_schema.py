"""
template_schema.py — GOLD-STANDARD ReportTemplate schema (schema_version 3).

This module is the ABSOLUTE SINGLE SOURCE OF TRUTH for the entire JobDoc system:

    guidance          → what the recording UI says/shows and how a brand-new worker captures.
    content_structure → what the writing agent is contractually allowed/required to produce.
    pdf_styling       → the ONLY place layout, color, footer, and photo rules live.

Design contract (enforced by model_validators + utils/template_validator.py)
----------------------------------------------------------------------------
1. Section order and ``section_id`` values come ONLY from the template JSON. Nothing in the
   pipeline or the agents may invent, rename, or reorder them.
2. STRICT MIRRORING: every ``section_id`` in ``guidance.sections`` MUST appear in
   ``content_structure.sections`` and vice-versa. The model AUTO-REPAIRS a mismatch (so a
   template is never silently dropped at load time) and logs a warning;
   ``utils/template_validator.py`` RAISES on the same condition for authoring / CI.
3. Backward compatibility: legacy v1 (``sections[]`` only) and v2 templates upgrade
   automatically on load. Every new field has a safe default, so old JSON still validates.
4. ``document_class`` lets the same schema drive inspection reports, estimates, invoices,
   compliance checklists, and work orders without a second schema.

Breaking changes vs v2 are documented in ``docs``-style module constants below
(``GOLD_STANDARD_CONTRACT`` / ``BREAKING_CHANGES_V3``).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from utils.schemas import ReportSection

logger = logging.getLogger(__name__)

IDENTIFICATION_PHRASE = "Mark this."
SCHEMA_VERSION = 3

# Allowed universal document classes. The same guidance/content/pdf machinery renders each.
DocumentClass = Literal[
    "inspection_report",
    "estimate",
    "invoice",
    "compliance",
    "work_order",
    "custom",
]

# Canonical severity vocabulary the writer/QA/PDF all share.
SEVERITY_VALUES = (
    "no_issue",
    "informational",
    "minor",
    "moderate",
    "major",
    "safety_critical",
)


GOLD_STANDARD_CONTRACT = """
GOLD-STANDARD CONTRACT (schema_version 3)
=========================================
The ReportTemplate JSON is the law. Every subsystem obeys it:

* document_class          — inspection_report | estimate | invoice | compliance | work_order | custom
* guidance                — the complete day-one capture script (intro/outro, per-section
                            voice_prompt, on_screen_text, worker_instructions, success_criteria,
                            suggested_phrases, min_marks, capture_order, estimated_seconds).
* content_structure       — the writer's binding contract (writer_instructions, tone,
                            min_summary_words, fields, default_text, image_placement, layout_hints).
* pdf_styling             — the ONLY source of colors, fonts, headers/footers, photo grid,
                            severity colors/bands, margins, disclaimer.

Mirroring: guidance.section_id set == content_structure.section_id set (strict, auto-repaired).
Order:     capture_order in guidance defines the one true section sequence end to end.
"""

BREAKING_CHANGES_V3 = """
BREAKING CHANGES (v2 → v3)
--------------------------
* schema_version default is now 3.
* New top-level required-with-default field: document_class (defaults to "inspection_report").
* GuidanceSection gains worker_instructions, success_criteria, estimated_seconds (all defaulted).
* ContentSection gains image_placement (object) and layout_hints (object) (both defaulted).
* TemplateGuidance gains estimated_total_minutes (auto-computed from section estimates if unset).
* Strict section_id mirroring between guidance and content_structure is now enforced
  (auto-repaired on load; hard-failed by utils/template_validator.py for authoring/CI).
All v2 (and v1) templates continue to load unchanged because every new field is defaulted.
"""


class PdfStyling(BaseModel):
    """PDF layout and branding — the ONLY place document formatting rules live.

    Consumed exclusively by ``report_templates/template_driven.html`` (via
    ``utils/pdf_render.build_pdf_page_context``). Do not scatter layout rules elsewhere.
    """

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
            "no_issue": "green",
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

    # --- Optional furniture for non-inspection document classes (estimate/invoice/work order).
    # Kept here (not scattered) so every document class is styled from one place. Renderers may
    # ignore these until a matching template partial uses them.
    currency_symbol: str = "$"
    show_totals_table: bool = Field(
        default=False,
        description="Estimates/invoices: render a totals table from numeric field_values.",
    )
    totals_label: str = "Total"
    show_signature_block: bool = Field(
        default=False,
        description="Work orders / compliance: render an approval/signature area.",
    )
    signature_labels: list[str] = Field(
        default_factory=lambda: ["Technician", "Client / Authorized signer"],
    )

    def resolve_severity_band(self, raw: str | None) -> str:
        s = str(raw or "informational").strip().lower().replace(" ", "_")
        aliases = {
            "info": "informational",
            "ok": "informational",
            "none": "informational",
            "no_issue": "informational",
            "noissue": "informational",
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
    """One guided capture step — everything a brand-new worker needs to nail this section.

    ``voice_prompt`` is spoken/shown as the instruction; ``on_screen_text`` is the short mobile
    UI label; ``worker_instructions`` explains in plain English what "done" looks like; and
    ``success_criteria`` are the pass/fail checks a supervisor could verify.
    """

    section_id: str
    title: str
    voice_prompt: str
    on_screen_text: str = ""
    worker_instructions: str = Field(
        default="",
        description="Plain-English explanation of what a complete, correct capture looks like.",
    )
    success_criteria: list[str] = Field(
        default_factory=list,
        description="Clear pass/fail conditions a supervisor could check for this section.",
    )
    required: bool = True
    min_marks: int = Field(default=1, ge=0)
    capture_order: int | None = None
    estimated_seconds: int = Field(
        default=60,
        ge=0,
        description="Rough time budget for this section (used to compute session length).",
    )
    suggested_phrases: list[str] = Field(default_factory=list)
    identification_phrase: str = IDENTIFICATION_PHRASE


class TemplateGuidance(BaseModel):
    """The complete guided-capture script for a template (day-one worker ready)."""

    intro_script: str = ""
    outro_script: str = "Recording complete. Upload when ready."
    identification_phrase: str = IDENTIFICATION_PHRASE
    estimated_total_minutes: int = Field(
        default=0,
        ge=0,
        description="Auto-computed from section estimated_seconds when left at 0.",
    )
    sections: list[GuidanceSection] = Field(default_factory=list)

    @model_validator(mode="after")
    def compute_total_minutes(self) -> TemplateGuidance:
        if self.estimated_total_minutes and self.estimated_total_minutes > 0:
            return self
        if not self.sections:
            return self
        total_sec = sum(max(0, s.estimated_seconds) for s in self.sections)
        if total_sec:
            self.estimated_total_minutes = max(1, round(total_sec / 60))
        return self


class ContentFieldSpec(BaseModel):
    key: str
    label: str = ""
    field_type: str = "text"
    required: bool = True


class ImagePlacement(BaseModel):
    """Controls how a section's still frames are placed in the rendered document."""

    required: bool = Field(
        default=True,
        description="True when this section is expected to carry at least one photo.",
    )
    position: Literal["after_summary", "before_summary", "appendix_only", "inline"] = "after_summary"
    caption_style: Literal["timestamp_narration", "timestamp_only", "narration_only", "none"] = (
        "timestamp_narration"
    )
    max_images: int = Field(default=12, ge=0)


class LayoutHints(BaseModel):
    """Per-section rendering hints (never override global pdf_styling; only refine it)."""

    page_break_before: bool = True
    callout_style: Literal["border-left accent", "boxed", "plain", "none"] = "border-left accent"
    columns: int = Field(default=1, ge=1, le=2)
    emphasis: Literal["normal", "highlight", "muted"] = "normal"


class ContentSection(BaseModel):
    """The writer's BINDING contract for one section — the agent may not deviate from this.

    * ``writer_instructions`` (mandatory, detailed) — exactly what to write.
    * ``fields`` — the exact keys that MUST appear in ``field_values``.
    * ``default_text`` — used ONLY when narration is missing; the writer states the limitation.
    * ``min_summary_words`` — enforced.
    * ``image_placement`` / ``layout_hints`` — how the section renders.
    """

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
    image_placement: ImagePlacement = Field(default_factory=ImagePlacement)
    layout_hints: LayoutHints = Field(default_factory=LayoutHints)

    @model_validator(mode="after")
    def keep_fields_and_specs_consistent(self) -> ContentSection:
        """Ensure ``fields`` covers every declared spec key (specs are the richer source)."""
        if self.field_specs:
            spec_keys = [fs.key for fs in self.field_specs]
            missing = [k for k in spec_keys if k not in self.fields]
            if missing:
                self.fields = [*self.fields, *missing]
        return self


class ContentStructure(BaseModel):
    summary_page_title: str = "Executive Summary"
    organization: str = "section_order"
    sections: list[ContentSection] = Field(default_factory=list)


class ReportTemplate(BaseModel):
    """
    Full gold-standard template: guidance (capture) + content_structure (writer/PDF) + pdf_styling.

    ``sections`` (legacy ``ReportSection`` list) is derived automatically and kept only for the
    deterministic v1 pipeline flattening path — authors should NOT hand-write it.
    """

    report_type: str
    title: str
    document_class: DocumentClass = Field(
        default="inspection_report",
        description="inspection_report | estimate | invoice | compliance | work_order | custom.",
    )
    business_name: str = ""
    logo_url: str | None = None
    job_address: str = Field(
        default="",
        description="Shown on cover (property address, site name, or job id).",
    )
    schema_version: int = SCHEMA_VERSION
    featured: bool = Field(
        default=False,
        description="When true, listed first on the homepage as a recommended template.",
    )
    template_description: str = Field(
        default="",
        description="Short marketing blurb for template selection UI.",
    )
    domain_expert_role: str | None = Field(
        default=None,
        description="Optional override for the domain-expert agent persona.",
    )
    domain_expert_standards: str | None = Field(
        default=None,
        description="Optional override for the domain-expert standards context.",
    )
    pdf_styling: PdfStyling = Field(default_factory=PdfStyling)
    guidance: TemplateGuidance | None = None
    content_structure: ContentStructure | None = None
    sections: list[ReportSection] = Field(default_factory=list)

    # ------------------------------------------------------------------ validators

    @model_validator(mode="after")
    def sync_legacy_and_rich(self) -> ReportTemplate:
        """Upgrade legacy templates and backfill whichever rich block is missing.

        v1 (``sections[]`` only) → synthesize guidance + content_structure.
        Rich (guidance/content) → synthesize the legacy ``sections`` list for the v1 matcher.
        """
        if self.guidance and self.guidance.sections and not self.sections:
            sections = []
            content_by_id = {
                c.section_id: c
                for c in (self.content_structure.sections if self.content_structure else [])
            }
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
            self.sections = sections
            return self

        if self.sections and (not self.guidance or not self.guidance.sections):
            g_sections = [
                GuidanceSection(
                    section_id=s.id,
                    title=s.name,
                    voice_prompt=s.capture_instructions,
                    on_screen_text=s.name,
                    worker_instructions=s.capture_instructions,
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
                intro_script=(
                    f"Welcome. We will document {self.title}. "
                    f"Say '{IDENTIFICATION_PHRASE}' before each section."
                ),
                sections=g_sections,
            )
            content = ContentStructure(sections=c_sections)
            self.guidance = guidance
            self.content_structure = content
            return self
        return self

    @model_validator(mode="after")
    def enforce_section_mirroring(self) -> ReportTemplate:
        """AUTO-REPAIR strict mirroring so a mismatch never strands a template at load.

        Every guidance ``section_id`` must have a matching ``content_structure`` section and
        vice-versa. Missing counterparts are synthesized from the side that has them; the extra
        content order is aligned to guidance capture order. Warnings are logged so authors can
        run ``utils/template_validator.py`` (which HARD-FAILS the same condition).
        """
        if not self.guidance or not self.guidance.sections:
            return self
        content = self.content_structure or ContentStructure()
        g_by_id = {g.section_id: g for g in self.guidance.sections}
        c_by_id = {c.section_id: c for c in content.sections}

        repaired = False
        new_content_sections: list[ContentSection] = list(content.sections)

        # 1) Guidance section without a content counterpart → synthesize one.
        for g in self.sorted_guidance_sections():
            if g.section_id not in c_by_id:
                repaired = True
                logger.warning(
                    "[template:%s] guidance section '%s' had no content_structure entry — "
                    "auto-generated one (run template_validator for a hard check).",
                    self.report_type, g.section_id,
                )
                synthesized = ContentSection(
                    section_id=g.section_id,
                    title=g.title,
                    default_text="No findings were recorded for this section.",
                    fields=["condition_summary", "specific_findings", "recommendations", "severity"],
                    writer_instructions=(
                        g.worker_instructions
                        or g.voice_prompt
                        or f"Summarize the observations captured for {g.title}."
                    ),
                )
                new_content_sections.append(synthesized)
                c_by_id[g.section_id] = synthesized

        # 2) Content section with no guidance counterpart → keep it (do not silently delete
        #    author intent) but log; it simply will not be captured during recording.
        for c in content.sections:
            if c.section_id not in g_by_id:
                logger.warning(
                    "[template:%s] content_structure section '%s' has no guidance step — it "
                    "cannot be captured during recording. Add a guidance section or remove it.",
                    self.report_type, c.section_id,
                )

        # 3) Order content sections to match guidance capture order (authoritative sequence).
        order = {g.section_id: i for i, g in enumerate(self.sorted_guidance_sections())}
        new_content_sections.sort(key=lambda c: order.get(c.section_id, len(order)))

        if repaired or new_content_sections != content.sections:
            content.sections = new_content_sections
            self.content_structure = content
        return self

    # ------------------------------------------------------------------ helpers

    def sorted_guidance_sections(self) -> list[GuidanceSection]:
        if not self.guidance or not self.guidance.sections:
            return []
        rows = list(self.guidance.sections)
        # Order by explicit ``capture_order`` when provided, but fall back to the section's
        # DECLARATION ORDER (not section_id) when it is absent, so sequence never scrambles.
        indexed = list(enumerate(rows))
        indexed.sort(
            key=lambda pair: (
                pair[1].capture_order if pair[1].capture_order is not None else pair[0],
                pair[0],
            )
        )
        return [row for _, row in indexed]

    def content_for_section(self, section_id: str) -> ContentSection | None:
        if not self.content_structure:
            return None
        for c in self.content_structure.sections:
            if c.section_id == section_id:
                return c
        return None

    def guidance_for_section(self, section_id: str) -> GuidanceSection | None:
        if not self.guidance:
            return None
        for g in self.guidance.sections:
            if g.section_id == section_id:
                return g
        return None

    def section_ids_in_order(self) -> list[str]:
        return [g.section_id for g in self.sorted_guidance_sections()]

    def writer_context_json(self) -> dict[str, Any]:
        """Compact template rules for Report Writer prompts (the writer's binding contract)."""
        return {
            "report_type": self.report_type,
            "title": self.title,
            "document_class": self.document_class,
            "business_name": self.business_name,
            "section_order": self.section_ids_in_order(),
            "content_structure": self.content_structure.model_dump() if self.content_structure else {},
            "pdf_styling": self.pdf_styling.model_dump(),
        }


class GuidedCaptureStep(BaseModel):
    """One step returned to Flask / recording UI (built ONLY from template fields)."""

    step_index: int
    total_steps: int
    section_id: str
    report_type: str
    document_class: str = "inspection_report"
    report_title: str
    title: str
    voice_prompt: str
    on_screen_text: str
    worker_instructions: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    required: bool
    min_marks: int
    estimated_seconds: int = 60
    capture_order: int | None = None
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
    estimated_total_minutes: int = 0
    steps: list[GuidedCaptureStep] = Field(default_factory=list)
    templates: list[str] = Field(default_factory=list)
    source: str = Field(
        default="template",
        description="template | template+agent — JSON template remains authoritative for order/ids.",
    )
