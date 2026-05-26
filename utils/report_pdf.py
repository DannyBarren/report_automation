"""
report_pdf.py — Template-driven Jinja HTML + WeasyPrint PDF (no per-customer Python).

Quality validation examples (expected PDF appearance):

1. Home inspection (6 sections, mixed severity): Cover shows logo + "123 Oak Lane" +
   date; executive summary lists 2 red / 1 yellow / 3 green badges with priority callouts;
   photo appendix in 2-column grid with "Recorded at 42.3s · …" captions; each section
   starts on a new page with severity pill, narrative, recommendation box, and inline photo.

2. Room walkthrough (12+ sections, mostly green): Running header with report title;
   compact summary counts only; photo appendix spans multiple pages with clean breaks;
   long summaries wrap without overflow; footer reads "Acme Realty · Confidential · Page 3 of 14".

3. Safety-critical finding: One section with red "Action required" badge, bold recommendation
   callout on pale blue panel, disclaimer on final page; severity legend visible on summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import Flask, render_template

from utils.pdf_render import build_pdf_page_context
from utils.setup_utils import ensure_weasyprint_import_ready, project_root
from utils.schemas import FinalReportOutput, ReportBundle, ReportTemplate
from utils.video_utils import extract_frame


class FrameExtractionError(RuntimeError):
    """Raised when a still frame cannot be written for a report section."""


@dataclass
class FrameEnrichmentResult:
    """Result of still-frame extraction (partial success allowed)."""

    final: FinalReportOutput
    failed_sections: list[str]
    warnings: list[str]


def enrich_report_with_still_frames(
    final: FinalReportOutput,
    video_path: Path,
    frames_dir: Path,
    *,
    allow_partial: bool = True,
) -> FinalReportOutput:
    """Extract frames; raises unless ``allow_partial`` (default True)."""
    result = enrich_report_with_still_frames_resilient(final, video_path, frames_dir)
    if result.failed_sections and not allow_partial:
        raise FrameExtractionError(
            f"Failed to extract {len(result.failed_sections)} frame(s): "
            + ", ".join(result.failed_sections[:5])
        )
    return result.final


def enrich_report_with_still_frames_resilient(
    final: FinalReportOutput,
    video_path: Path,
    frames_dir: Path,
) -> FrameEnrichmentResult:
    """Extract stills per section; skip failures so PDF generation can continue."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    warnings: list[str] = []
    new_reports: dict[str, ReportBundle] = {}
    for rt, bundle in final.reports.items():
        new_sections = []
        for sec in bundle.sections:
            slug = f"{rt}__{sec.section_id}".replace("/", "-")
            out_path = frames_dir / f"{slug}.png"
            ok = extract_frame(video_path, float(sec.image_timestamp_sec), out_path)
            if not ok or not out_path.is_file():
                label = f"{bundle.title} — {sec.section_name}"
                failed.append(label)
                warnings.append(f"Still image unavailable for {label} — PDF will omit that photo.")
                new_sections.append(sec)
                continue
            new_sections.append(
                sec.model_copy(
                    update={
                        "image_path": str(out_path),
                        "image_uri": out_path.as_uri(),
                    }
                )
            )
        new_reports[rt] = bundle.model_copy(update={"sections": new_sections})
    return FrameEnrichmentResult(
        final=final.model_copy(update={"reports": new_reports}),
        failed_sections=failed,
        warnings=warnings,
    )


def render_report_html(
    app: Flask,
    final: FinalReportOutput,
    templates_by_type: dict[str, ReportTemplate] | None = None,
    *,
    job_addresses: dict[str, str] | None = None,
) -> str:
    """
    Render PDF HTML using each template's ``pdf_styling`` via ``template_driven.html``.

    Falls back to legacy ``report_templates/{report_type}.html`` only when
    ``schema_version`` < 2 or template missing.

    ``job_addresses`` maps report_type → property/site address for the cover page.
    """
    templates_by_type = templates_by_type or {}
    job_addresses = job_addresses or {}
    chunks: list[str] = []
    with app.app_context():
        for rt, bundle in final.reports.items():
            tpl = templates_by_type.get(rt)
            use_driven = tpl is not None and getattr(tpl, "schema_version", 1) >= 2
            if use_driven:
                ctx = build_pdf_page_context(
                    template=tpl,
                    bundle=bundle,
                    final=final,
                    job_address=job_addresses.get(rt, ""),
                )
                chunks.append(
                    render_template(
                        "report_templates/template_driven.html",
                        **ctx,
                    )
                )
            else:
                legacy = f"report_templates/{rt}.html"
                try:
                    chunks.append(render_template(legacy, bundle=bundle, final=final))
                except Exception:  # noqa: BLE001
                    chunks.append(
                        render_template(
                            "report_templates/generic_report.html",
                            bundle=bundle,
                            final=final,
                            report_type=rt,
                            template=tpl,
                        )
                    )
    return "\n".join(f'<div class="report-document">{html}</div>' for html in chunks)


def write_pdf(app: Flask, html_inner: str, *, base_dir: Path, output_path: Path) -> None:
    _ = app
    ensure_weasyprint_import_ready(project_root(base_dir))
    from weasyprint import HTML

    shell = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Field inspection report</title>
  <style>
    html {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
    body {{ margin: 0; padding: 0; background: #fff; }}
    .report-document + .report-document {{ page-break-before: always; }}
  </style>
</head>
<body>{html_inner}</body>
</html>"""
    HTML(string=shell, base_url=str(base_dir.resolve()) + "/").write_pdf(str(output_path))
