"""
misc_section.py — App-level Misc / Supplemental capture step.

The Misc step is **not** defined in any ``reports/*.json`` template. It is hardcoded here and
always appended to the end of every capture session, regardless of which report template the
user chose. It behaves like any other guided step in the recorder (same "Mark this." UI and
narration), and its captured content is stored separately in the ``MiscData`` table so it can
be queried later for on-demand document generation (invoices, proposals, estimates, etc.).

Implementation note
-------------------
The Misc step is modeled as a single-section ``ReportTemplate`` with its own ``report_type``.
By injecting it into ``templates_by_type()`` and appending its ``report_type`` to the selected
list when starting a recording, the existing guidance/recording/pipeline code treats Misc as
just another report — no special cases needed in the recorder. After processing, the Misc
bundle is split out of the final report into ``MiscData`` and excluded from the client PDF.
"""

from __future__ import annotations

from utils.template_schema import (
    ContentSection,
    ContentStructure,
    GuidanceSection,
    ImagePlacement,
    LayoutHints,
    ReportTemplate,
    TemplateGuidance,
)

MISC_REPORT_TYPE = "misc_supplemental"
MISC_SECTION_ID = "misc_supplemental"
MISC_TITLE = "Misc / Supplemental Data"

_MISC_VOICE_PROMPT = (
    "Final step — capture anything useful for later that did not fit the inspection sections. "
    "Say Mark this. and describe pricing discussions, client requests, estimate notes, site "
    "conditions, access details, follow-up items, or anything you want on record for this job."
)

_MISC_WRITER_INSTRUCTIONS = (
    "Organize the supplemental narration into clear, useful notes for future document generation "
    "(invoices, proposals, estimates, follow-ups). Capture any prices, quantities, client "
    "requests, commitments, and action items verbatim where possible. Do not invent details."
)


def misc_report_template() -> ReportTemplate:
    """The hardcoded, app-level Misc / Supplemental capture template (one section)."""
    guidance = TemplateGuidance(
        intro_script=(
            "Last step. Capture any supplemental information for this job before you upload."
        ),
        outro_script="Supplemental capture complete. Stop recording and upload when ready.",
        identification_phrase="Mark this.",
        sections=[
            GuidanceSection(
                section_id=MISC_SECTION_ID,
                title=MISC_TITLE,
                voice_prompt=_MISC_VOICE_PROMPT,
                on_screen_text="Misc — pricing, requests, estimates, follow-ups",
                worker_instructions=(
                    "Optional final catch-all. If there is anything worth recording for a future "
                    "document — pricing discussed, client requests, estimate notes, site or access "
                    "details, follow-up items — say 'Mark this.' and describe it in a full sentence. "
                    "Skip this step if nothing applies; it never appears in the client PDF."
                ),
                success_criteria=[
                    "Any supplemental item is marked with a spoken explanation of why it matters.",
                    "Recording zero marks here is acceptable (this step is optional).",
                ],
                estimated_seconds=90,
                required=False,
                min_marks=0,
                capture_order=1,
                suggested_phrases=[
                    "price",
                    "estimate",
                    "client requested",
                    "follow up",
                    "quote",
                    "site condition",
                ],
            )
        ],
    )
    content = ContentStructure(
        summary_page_title="Supplemental Notes",
        sections=[
            ContentSection(
                section_id=MISC_SECTION_ID,
                title=MISC_TITLE,
                default_text="No supplemental notes captured.",
                fields=["condition_summary", "specific_findings", "recommendations", "severity"],
                writer_instructions=_MISC_WRITER_INSTRUCTIONS,
                tone="practical neutral",
                min_summary_words=30,
                show_photo=True,
                show_severity_badge=False,
                image_placement=ImagePlacement(
                    required=False, position="appendix_only", caption_style="timestamp_only"
                ),
                layout_hints=LayoutHints(callout_style="plain", emphasis="muted"),
            )
        ],
    )
    return ReportTemplate(
        report_type=MISC_REPORT_TYPE,
        title=MISC_TITLE,
        document_class="custom",
        business_name="",
        schema_version=3,
        featured=False,
        template_description="App-level supplemental data capture (always appended).",
        guidance=guidance,
        content_structure=content,
    )
