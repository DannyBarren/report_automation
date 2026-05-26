"""
pdf_render.py — Helpers for template-driven PDF HTML (severity, summary, captions).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from utils.schemas import FinalReportOutput, ReportBundle, ReportSectionOutput
from utils.template_schema import ReportTemplate


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


def photo_caption(
    sec: ReportSectionOutput,
    *,
    max_words: int = 42,
    include_timestamp: bool = True,
) -> str:
    parts: list[str] = []
    if include_timestamp:
        t = sec.image_timestamp_sec if sec.image_timestamp_sec else sec.timestamp_sec
        parts.append(f"Recorded at {t:.1f}s")
    words = (sec.summary or "").split()
    snippet = " ".join(words[:max_words])
    if len(words) > max_words:
        snippet += "…"
    if snippet:
        parts.append(snippet)
    return " · ".join(parts) if parts else "Field documentation still."


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
                "one_line": " ".join((sec.summary or "").split()[:18]),
            }
        )
        rec = sec.field_values.get("recommendations") if sec.field_values else None
        if rec and str(rec).strip() and str(rec).lower() not in ("none", "n/a", "not stated"):
            recommendations.append(f"{sec.section_name}: {rec}")

    priority = [r for r in rows if r["band"] == "red"] + [r for r in rows if r["band"] == "yellow"]

    return {
        "section_count": len(bundle.sections),
        "counts": counts,
        "rows": rows,
        "priority_rows": priority[:6],
        "recommendations": recommendations[:8],
    }


def field_rows_for_section(sec: ReportSectionOutput) -> list[dict[str, str]]:
    """Human-readable field table (skip severity/recommendations shown elsewhere)."""
    skip = {"severity", "recommendations"}
    out: list[dict[str, str]] = []
    for key, val in (sec.field_values or {}).items():
        if key in skip:
            continue
        label = key.replace("_", " ").strip().title()
        out.append({"label": label, "value": str(val)})
    return out


def interpolate_styling_string(template: str, *, business_name: str, report_title: str) -> str:
    return (
        template.replace("{business_name}", business_name or "")
        .replace("{report_title}", report_title or "")
    )


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
        rec_text = str(rec).strip() if rec is not None else ""
        show_rec = bool(rec_text) and rec_text.lower() not in ("none", "n/a", "not stated", "no action")
        show_photo = (content_sec is None or content_sec.show_photo) and bool(sec.image_uri)
        show_badge = bool(content_sec and content_sec.show_severity_badge and fv.get("severity"))
        max_w = template.pdf_styling.photo_caption_max_words
        items.append(
            {
                "index": idx,
                "sec": sec,
                "severity": section_severity_display(sec, template),
                "fields": field_rows_for_section(sec),
                "caption": photo_caption(
                    sec,
                    max_words=max_w,
                    include_timestamp=template.pdf_styling.include_timestamps,
                ),
                "show_photo": show_photo,
                "show_badge": show_badge,
                "show_recommendation": show_rec,
                "recommendation": rec_text,
            }
        )
    return items


def photo_appendix_rows(bundle: ReportBundle, template: ReportTemplate) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for block in enrich_sections_for_pdf(bundle, template):
        if not block["show_photo"]:
            continue
        sec = block["sec"]
        rows.append(
            {
                "section_name": sec.section_name,
                "image_uri": sec.image_uri or "",
                "caption": block["caption"],
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
