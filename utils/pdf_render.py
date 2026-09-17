"""
pdf_render.py — Helpers for template-driven PDF HTML (severity, summary, captions).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from utils.schemas import FinalReportOutput, ReportBundle, ReportSectionOutput, SectionFrame
from utils.template_schema import ReportTemplate

# Deterministic boilerplate injected by the fallback/padding paths (schemas + structured
# pipeline). It keeps machine contracts satisfied but reads poorly in a client PDF, so we
# strip it for display while leaving the underlying data intact for audit/QA.
_BOILERPLATE_PATTERNS = [
    re.compile(
        r"This summary reflects only the narration provided after the cue\.?", re.IGNORECASE
    ),
    re.compile(
        r"No additional unstated assumptions were added,? and this summary is restricted "
        r"to the narrated details provided in the recording\.?",
        re.IGNORECASE,
    ),
    re.compile(
        r"This sentence expands the section narrative to satisfy the minimum documentation "
        r"length while staying tied to the same timestamped cue and without inventing new "
        r"facts\.?",
        re.IGNORECASE,
    ),
    re.compile(
        r"The spoken identification phrase anchors this block for QA traceability\.?",
        re.IGNORECASE,
    ),
    # Repeated deterministic padding token ("word word word …").
    re.compile(r"(?:\bword\b[\s]*){3,}", re.IGNORECASE),
    # Pure pipeline-mechanic filler (whole sentence) — strip if any slips through.
    re.compile(
        r"\bno (?:explicit )?[“\"']?mark this[”\"']?\.? ?cue[s]? (?:was|were) (?:detected|found|heard)"
        r"[^.]*\.?",
        re.IGNORECASE,
    ),
]

# "The speaker reports/stated …" and "the narration says/indicates …" → drop the scaffold,
# keep the actual clause that follows. Longest alternatives first + \b so prefixes like
# "report" don't get chopped out of "reported".
_SPEAKER_SCAFFOLD_RE = re.compile(
    r"\bthe (?:speaker|technician|narrator)\s+"
    r"(?:reported|reports|report|stated|states|state|said|says|say)\b\s*:?\s*",
    re.IGNORECASE,
)
_NARRATION_SCAFFOLD_RE = re.compile(
    r"\bthe narration\s+(?:reflects|reflect|indicates|indicate|shows|show|said|says|say)\b\s*:?\s*",
    re.IGNORECASE,
)
# Lead-in phrases that only reference the pipeline source — strip the lead-in (up to an
# optional comma) but KEEP the real clause that follows.
_NARRATION_LEADIN_RE = re.compile(
    r"\b(?:based (?:solely )?on the narration(?: provided)?|according to the narration|"
    r"per the narration|from the narration)\s*,?\s*",
    re.IGNORECASE,
)

# Leading scaffolding like "For Roof Covering, the speaker reports: ...".
_LEADING_REPORTS_RE = re.compile(
    r"^\s*For\s+.{1,80}?,\s*the speaker reports:\s*", re.IGNORECASE
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def clean_display_text(text: str | None) -> str:
    """Strip machine boilerplate/padding and normalize whitespace for client-facing prose.

    Never returns garbage: if cleaning would empty the string, the original (trimmed) text is
    returned so the section is never blank in the PDF.
    """
    original = (text or "").strip()
    if not original:
        return ""
    cleaned = _LEADING_REPORTS_RE.sub("", original)
    cleaned = _SPEAKER_SCAFFOLD_RE.sub("", cleaned)
    cleaned = _NARRATION_SCAFFOLD_RE.sub("", cleaned)
    cleaned = _NARRATION_LEADIN_RE.sub("", cleaned)
    for pat in _BOILERPLATE_PATTERNS:
        cleaned = pat.sub("", cleaned)
    # Capitalize after stripping a leading scaffold; collapse whitespace/duplicate punctuation.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([.,;:])", r"\1", cleaned)
    cleaned = re.sub(r"([.,;:]){2,}", r"\1", cleaned).strip(" .,;:").strip()
    # Recapitalize sentence starts (scaffold removal can leave a lowercase word mid-string).
    cleaned = re.sub(
        r"([.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), cleaned
    )
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    if cleaned and cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned or original


def summarize_to_words(text: str | None, max_words: int) -> str:
    """Sentence-aware trim: keep whole sentences up to ``max_words``; never cut mid-word."""
    cleaned = clean_display_text(text)
    if not cleaned:
        return ""
    words = cleaned.split()
    if len(words) <= max_words:
        return cleaned
    # Prefer to end on a sentence boundary within the budget.
    out: list[str] = []
    count = 0
    for sentence in _SENTENCE_SPLIT_RE.split(cleaned):
        n = len(sentence.split())
        if out and count + n > max_words:
            break
        out.append(sentence)
        count += n
    if out:
        return " ".join(out).strip()
    # Single very long sentence: hard-trim on a word boundary.
    return " ".join(words[:max_words]).rstrip(" ,;:") + "…"


def format_report_date(iso_utc: str) -> str:
    if not iso_utc:
        return datetime.now().strftime("%B %d, %Y")
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        return dt.strftime("%B %d, %Y")
    except ValueError:
        return iso_utc[:10]


def normalize_severity_key(raw: object) -> str:
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
    return aliases.get(s, s if s else "informational")


def _format_timestamp(seconds: float) -> str:
    """Human-friendly capture time: seconds under a minute, else m:ss."""
    seconds = max(0.0, float(seconds or 0.0))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    rem = int(round(seconds % 60))
    return f"{minutes}:{rem:02d}"


def photo_caption(
    sec: ReportSectionOutput,
    *,
    max_words: int = 42,
    include_timestamp: bool = True,
) -> str:
    parts: list[str] = []
    if include_timestamp:
        t = sec.image_timestamp_sec if sec.image_timestamp_sec else sec.timestamp_sec
        parts.append(f"Captured at {_format_timestamp(t)}")
    snippet = summarize_to_words(sec.summary, max_words)
    if snippet:
        parts.append(snippet)
    return " · ".join(parts) if parts else "Field documentation still."


_SOURCE_LABELS = {
    "manual_mark": "Marked",
    "spoken_cue": "Cue",
    "step_window": "Walkthrough",
}


def frame_caption(
    frame: SectionFrame,
    sec: ReportSectionOutput,
    *,
    max_words: int = 30,
    include_timestamp: bool = True,
) -> str:
    """Per-photo caption: capture time + this frame's own narration/note (falls back to summary)."""
    parts: list[str] = []
    if include_timestamp:
        t = frame.image_timestamp_sec or frame.timestamp_sec
        label = _SOURCE_LABELS.get(frame.source, "Captured")
        parts.append(f"{label} at {_format_timestamp(t)}")
    # Prefer the inspector's own note, then the narration nearest this frame, then the summary.
    detail = (frame.note or "").strip() or (frame.narration or "").strip()
    snippet = summarize_to_words(detail, max_words) if detail else summarize_to_words(sec.summary, max_words)
    if snippet:
        parts.append(snippet)
    return " · ".join(parts) if parts else "Field documentation still."


def section_photos(
    sec: ReportSectionOutput,
    template: ReportTemplate,
    *,
    include_timestamp: bool = True,
) -> list[dict[str, Any]]:
    """All renderable photos for a section (multi-frame), newest schema first, legacy fallback.

    Returns a list of ``{image_uri, caption, timestamp, index}`` for every frame that has an
    extracted still. Legacy single-image reports (no ``frames``) yield one entry.
    """
    max_w = template.pdf_styling.photo_caption_max_words
    per_photo_words = max(18, min(34, max_w))
    photos: list[dict[str, Any]] = []
    for i, fr in enumerate(sec.frames or []):
        if not fr.image_uri:
            continue
        photos.append(
            {
                "image_uri": fr.image_uri,
                "caption": frame_caption(fr, sec, max_words=per_photo_words, include_timestamp=include_timestamp),
                "timestamp": fr.image_timestamp_sec,
                "index": i + 1,
            }
        )
    if not photos and sec.image_uri:  # legacy single-frame report
        photos.append(
            {
                "image_uri": sec.image_uri,
                "caption": photo_caption(sec, max_words=max_w, include_timestamp=include_timestamp),
                "timestamp": sec.image_timestamp_sec,
                "index": 1,
            }
        )
    return photos


def section_severity_display(sec: ReportSectionOutput, template: ReportTemplate) -> dict[str, str]:
    raw = sec.field_values.get("severity") if sec.field_values else None
    key = normalize_severity_key(raw)
    band = template.pdf_styling.resolve_severity_band(key)
    style = template.pdf_styling.band_style(band)
    label = str(raw or style.get("label", key.replace("_", " ").title()))
    return {
        "key": key,
        "band": band,
        "label": label,
        "bg": style.get("bg", "#f1f5f9"),
        "text": style.get("text", "#0f172a"),
        "border": style.get("border", "#cbd5e1"),
    }


def build_summary_findings(
    bundle: ReportBundle,
    template: ReportTemplate,
) -> dict[str, Any]:
    """Executive summary data: counts, priority rows, recommendation snippets."""
    counts = {"green": 0, "yellow": 0, "red": 0}
    rows: list[dict[str, Any]] = []
    recommendations: list[str] = []

    for sec in bundle.sections:
        disp = section_severity_display(sec, template)
        counts[disp["band"]] = counts.get(disp["band"], 0) + 1
        rows.append(
            {
                "section_name": sec.section_name,
                "severity_label": disp["label"],
                "band": disp["band"],
                "bg": disp["bg"],
                "text": disp["text"],
                "border": disp["border"],
                "one_line": summarize_to_words(sec.summary, 22),
            }
        )
        rec = sec.field_values.get("recommendations") if sec.field_values else None
        rec_text = clean_display_text(str(rec)) if rec is not None else ""
        if rec_text and rec_text.lower() not in ("none.", "n/a.", "not stated."):
            recommendations.append(f"{sec.section_name}: {rec_text}")

    priority = [r for r in rows if r["band"] == "red"] + [r for r in rows if r["band"] == "yellow"]

    return {
        "section_count": len(bundle.sections),
        "counts": counts,
        "rows": rows,
        "priority_rows": priority[:6],
        "recommendations": recommendations[:8],
        # True when nothing needs attention — lets the template show a positive, clean state
        # instead of an empty "Priority findings" gap (reduces sparse-looking reports).
        "all_clear": counts.get("red", 0) == 0 and counts.get("yellow", 0) == 0,
    }


# Placeholder tokens the heuristic field-guesser emits — noise in a client PDF.
_NOISE_FIELD_VALUES = {"", "missing", "unknown", "n/a", "na", "none", "not stated", "-"}


def _format_field_value(val: Any) -> str:
    """Render a field value cleanly (join lists, drop noise, tidy prose)."""
    if isinstance(val, (list, tuple)):
        items = [str(v).strip() for v in val if str(v).strip()]
        return ", ".join(items)
    text = str(val).strip()
    return text


def field_rows_for_section(sec: ReportSectionOutput) -> list[dict[str, str]]:
    """Human-readable field table (skip severity/recommendations + placeholder noise)."""
    skip = {"severity", "recommendations"}
    out: list[dict[str, str]] = []
    for key, val in (sec.field_values or {}).items():
        if key in skip:
            continue
        value = _format_field_value(val)
        if value.strip().lower() in _NOISE_FIELD_VALUES:
            continue  # hide "missing"/"unknown" rows that make tables look unfinished
        if value == "See narrative summary":
            continue
        label = key.replace("_", " ").strip().title()
        out.append({"label": label, "value": value})
    return out


_STYLING_TOKEN_RE = re.compile(r"\{\s*[A-Za-z_][\w.]*\s*\}")
# A page-number clause in the raw footer (e.g. "Page {page} of {total_pages}"). The WeasyPrint
# @page CSS ALWAYS appends its own live "· Page N of M" counter, so any authored page clause is
# removed here whole — leaving just the branding prefix — to prevent a doubled/garbled footer.
_PAGE_CLAUSE_RE = re.compile(
    r"\bpage\b\s*\{[^}]*\}?\s*(?:of\s*\{[^}]*\}?)?", re.IGNORECASE
)


def interpolate_styling_string(template: str, *, business_name: str, report_title: str) -> str:
    """Fill supported styling tokens and STRIP everything else so no placeholder leaks into the PDF.

    Substitutes ``{business_name}`` / ``{report_title}``, removes any authored page-number clause
    (the CSS page counter renders the real "Page N of M"), and deletes any other stray ``{token}``
    (a mistyped/unsupported placeholder) rather than printing it verbatim. The result is the clean
    branding PREFIX; ``template_driven.html`` appends the live counter after it.
    """
    out = (
        (template or "")
        .replace("{business_name}", business_name or "")
        .replace("{report_title}", report_title or "")
    )
    # Drop any authored "Page {page} of {total_pages}" style clause in full.
    out = _PAGE_CLAUSE_RE.sub("", out)
    # Remove any remaining stray {token} (leakage from a mistyped placeholder).
    out = _STYLING_TOKEN_RE.sub("", out)
    # Tidy separators/whitespace left behind by removals.
    out = re.sub(r"(\s*·\s*)+", " · ", out)
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip(" ·").strip()


def enrich_sections_for_pdf(
    bundle: ReportBundle,
    template: ReportTemplate,
) -> list[dict[str, Any]]:
    """Per-section UI payload for Jinja (severity, captions, fields, flags)."""
    items: list[dict[str, Any]] = []
    for idx, sec in enumerate(bundle.sections):
        content_sec = template.content_for_section(sec.section_id)
        fv = sec.field_values or {}
        rec = fv.get("recommendations")
        rec_text = clean_display_text(str(rec)) if rec is not None else ""
        show_rec = bool(rec_text) and rec_text.lower() not in (
            "none.", "n/a.", "not stated.", "no action.", "none", "n/a",
        )
        wants_photo = content_sec is None or content_sec.show_photo
        photos = section_photos(
            sec, template, include_timestamp=template.pdf_styling.include_timestamps
        )
        show_photo = wants_photo and len(photos) > 0
        # Expected a still but extraction failed — the template shows a tasteful placeholder
        # instead of a blank gap so the section never looks broken/sparse.
        photo_missing = wants_photo and len(photos) == 0
        show_badge = bool(content_sec and content_sec.show_severity_badge and fv.get("severity"))
        max_w = template.pdf_styling.photo_caption_max_words
        narrative = clean_display_text(sec.summary)
        items.append(
            {
                "index": idx,
                "sec": sec,
                # Cleaned, client-ready narrative (falls back to raw text if empty).
                "narrative": narrative or (sec.summary or "").strip(),
                "severity": section_severity_display(sec, template),
                "fields": field_rows_for_section(sec),
                "caption": photo_caption(
                    sec,
                    max_words=max_w,
                    include_timestamp=template.pdf_styling.include_timestamps,
                ),
                "photos": photos,
                "photo_count": len(photos),
                "show_photo": show_photo,
                "photo_missing": photo_missing,
                "show_badge": show_badge,
                "show_recommendation": show_rec,
                "recommendation": rec_text,
            }
        )
    return items


def photo_appendix_rows(bundle: ReportBundle, template: ReportTemplate) -> list[dict[str, str]]:
    """One appendix row per extracted still across ALL sections (multi-frame aware)."""
    rows: list[dict[str, str]] = []
    for block in enrich_sections_for_pdf(bundle, template):
        if not block["show_photo"]:
            continue
        sec = block["sec"]
        multi = len(block["photos"]) > 1
        for photo in block["photos"]:
            name = f"{sec.section_name} ({photo['index']})" if multi else sec.section_name
            rows.append(
                {
                    "section_name": name,
                    "image_uri": photo["image_uri"] or "",
                    "caption": photo["caption"],
                }
            )
    return rows


def build_pdf_page_context(
    *,
    template: ReportTemplate,
    bundle: ReportBundle,
    final: FinalReportOutput,
    job_address: str = "",
) -> dict[str, Any]:
    address = (job_address or template.job_address or "").strip()
    if not address and final.video_stem:
        address = final.video_stem.replace("_", " ").replace("-", " ").title()

    style = template.pdf_styling
    return {
        "template": template,
        "bundle": bundle,
        "final": final,
        "style": style,
        "report_date": format_report_date(final.generated_at_utc),
        "job_address": address,
        "footer_text": interpolate_styling_string(
            style.page_footer,
            business_name=template.business_name,
            report_title=bundle.title,
        ),
        "header_text": interpolate_styling_string(
            style.page_header,
            business_name=template.business_name,
            report_title=bundle.title,
        ),
        "summary": build_summary_findings(bundle, template),
        "photo_grid_columns": 1 if "1-column" in style.photo_grid else 2,
        "sections_ui": enrich_sections_for_pdf(bundle, template),
        "photo_appendix": photo_appendix_rows(bundle, template),
    }
