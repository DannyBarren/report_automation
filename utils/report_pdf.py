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

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import Flask, render_template

from utils.pdf_render import build_pdf_page_context
from utils.setup_utils import ensure_weasyprint_import_ready, project_root
from utils.schemas import FinalReportOutput, ReportBundle, ReportTemplate, SectionFrame
from utils.video_utils import extract_frame, probe_duration_seconds

logger = logging.getLogger(__name__)


class FrameExtractionError(RuntimeError):
    """Raised when a still frame cannot be written for a report section."""


@dataclass
class FrameEnrichmentResult:
    """Result of still-frame extraction (partial success allowed)."""

    final: FinalReportOutput
    failed_sections: list[str]
    warnings: list[str]


# Still-frame width presets. "Full" stays crisp for print; "fast" trades a little detail
# for dramatically faster WeasyPrint rendering and a lighter PDF (better on job-site data).
FRAME_WIDTH_FULL = 1280
FRAME_WIDTH_FAST = 900


def enrich_report_with_still_frames(
    final: FinalReportOutput,
    video_path: Path,
    frames_dir: Path,
    *,
    allow_partial: bool = True,
    fast: bool = False,
) -> FinalReportOutput:
    """Extract frames; raises unless ``allow_partial`` (default True)."""
    result = enrich_report_with_still_frames_resilient(final, video_path, frames_dir, fast=fast)
    if result.failed_sections and not allow_partial:
        raise FrameExtractionError(
            f"Failed to extract {len(result.failed_sections)} frame(s): "
            + ", ".join(result.failed_sections[:5])
        )
    return result.final


def _extract_frame_with_fallback(
    video_path: Path,
    target_sec: float,
    out_path: Path,
    *,
    max_width: int,
) -> bool:
    """Extract a still, retrying at nearby offsets if the exact seek fails.

    Field recordings and step-boundary timestamps can land right at a scene cut, past the
    trimmed end, or on a corrupt frame. Trying a few nearby moments (and the start) recovers
    a usable photo instead of leaving the section image-less.
    """
    base = max(0.0, float(target_sec or 0.0))
    # Ordered candidates: the exact time, small nudges around it, then safe fallbacks.
    candidates = [base, base + 1.0, base - 1.0, base + 2.5, max(0.5, base - 2.5), 0.5]
    seen: set[float] = set()
    for cand in candidates:
        cand = round(max(0.0, cand), 2)
        if cand in seen:
            continue
        seen.add(cand)
        try:
            if extract_frame(video_path, cand, out_path, max_width=max_width) and out_path.is_file():
                return True
        except Exception as exc:  # noqa: BLE001 — never let one frame abort the PDF
            logger.debug("extract_frame failed at %.2fs: %s", cand, exc)
    return False


def enrich_report_with_still_frames_resilient(
    final: FinalReportOutput,
    video_path: Path,
    frames_dir: Path,
    *,
    fast: bool = False,
    mark_times: list[float] | None = None,
    mark_events: list[dict[str, Any]] | None = None,
) -> FrameEnrichmentResult:
    """Extract stills per section; skip failures so PDF generation can continue.

    ``mark_events`` are the recorder's MARK taps *with* their ``section_id``. They are used only
    by the whole-report safety net below: if the normal per-section pass produces ZERO images but
    the user actually recorded (a readable video exists), each section can recover a still from
    its OWN tap, so a real recording never yields a completely image-less report.

    ``mark_times`` (bare floats, no section identity) is kept for older callers; it cannot place
    a still on a specific section — pass ``mark_events`` for that.
    """
    try:
        frames_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # A slow/unavailable Volume can make the frames dir unwritable — degrade to a
        # photo-less (but still complete) report rather than crashing report generation.
        logger.warning("Could not create frames dir %s: %s — skipping stills.", frames_dir, exc)
        return FrameEnrichmentResult(
            final=final,
            failed_sections=["all"],
            warnings=["Photo storage was unavailable — the report was generated without stills."],
        )

    max_width = FRAME_WIDTH_FAST if fast else FRAME_WIDTH_FULL
    failed: list[str] = []
    warnings: list[str] = []
    new_reports: dict[str, ReportBundle] = {}
    total = 0
    ok_count = 0
    for rt, bundle in final.reports.items():
        new_sections = []
        for sec in bundle.sections:
            slug = f"{rt}__{sec.section_id}".replace("/", "-")
            # Extract EVERY evidence frame for the section (one per MARK tap / cue). Legacy
            # reports carry no `frames`, so fall back to the single image_timestamp_sec — but
            # ONLY when it points at a real moment (> 0). A section with no frames and a 0.0s
            # anchor is "not reached" filler; extracting there would wrongly stamp the video's
            # opening frame onto the section, so we emit no still for it.
            if sec.frames:
                frame_specs = list(sec.frames)
            elif float(sec.image_timestamp_sec or 0.0) > 0.05:
                frame_specs = [
                    SectionFrame(
                        timestamp_sec=sec.timestamp_sec,
                        image_timestamp_sec=sec.image_timestamp_sec,
                        source="manual_mark",
                    )
                ]
            else:
                frame_specs = []
            if not frame_specs:
                logger.info(
                    "Frame extraction: section '%s' has no evidence frame — skipping still.",
                    sec.section_id,
                )
                new_sections.append(sec.model_copy(update={"frames": []}))
                continue
            new_frames: list[SectionFrame] = []
            primary_path: str | None = None
            primary_uri: str | None = None
            sec_ok = 0
            for i, fr in enumerate(frame_specs):
                total += 1
                # JPEG keeps the PDF small and renders faster in WeasyPrint than PNG.
                out_path = frames_dir / f"{slug}__{i:02d}.jpg"
                ok = _extract_frame_with_fallback(
                    video_path, float(fr.image_timestamp_sec), out_path, max_width=max_width
                )
                if not ok or not out_path.is_file():
                    new_frames.append(fr)  # keep the spec (no image) so counts stay honest
                    continue
                ok_count += 1
                sec_ok += 1
                new_frames.append(
                    fr.model_copy(update={"image_path": str(out_path), "image_uri": out_path.as_uri()})
                )
                if primary_uri is None:  # first successful still = section's primary (back-compat)
                    primary_path, primary_uri = str(out_path), out_path.as_uri()
            update: dict[str, Any] = {"frames": new_frames}
            if primary_uri:
                update["image_path"] = primary_path
                update["image_uri"] = primary_uri
            else:
                label = f"{bundle.title} — {sec.section_name}"
                failed.append(label)
                warnings.append(f"Still image unavailable for {label} — PDF will note it instead.")
            logger.info(
                "Frame extraction: section '%s' → %d/%d still(s) at %s sec.",
                sec.section_id, sec_ok, len(frame_specs),
                [round(float(f.image_timestamp_sec), 1) for f in frame_specs],
            )
            new_sections.append(sec.model_copy(update=update))
        new_reports[rt] = bundle.model_copy(update={"sections": new_sections})
    logger.info("Frame extraction: %d/%d stills produced (%d section(s) with no image).", ok_count, total, len(failed))

    # Whole-report safety net: a real recording must never yield a completely image-less PDF.
    # If NOTHING was extracted (e.g. a fully degraded run where every section fell to filler),
    # recover a still for each section that has a MARK tap tagged for it. Sections with no tap of
    # their own stay photo-less — a still from elsewhere in the walkthrough is worse than none.
    if ok_count == 0:
        new_reports, recovered = _guarantee_report_images(
            new_reports, video_path, frames_dir, max_width=max_width,
            mark_times=mark_times, mark_events=mark_events,
        )
        if recovered:
            ok_count += recovered
            # Only the sections that are STILL photo-less remain "failed".
            failed = [
                f"{bundle.title} — {sec.section_name}"
                for bundle in new_reports.values()
                for sec in bundle.sections
                if not sec.image_uri
            ]
            warnings.append(
                "Exact section frames were unavailable — stills were recovered at each area's "
                "own MARK tap. Verify against the source video."
            )
            logger.warning(
                "[frames] whole-report safety net engaged: recovered %d still(s); %d section(s) "
                "remain photo-less.", recovered, len(failed),
            )
        else:
            logger.warning(
                "[frames] report is image-less and the safety net could not extract any still "
                "(video unreadable at %s) — PDF will note missing images per section.", video_path,
            )

    return FrameEnrichmentResult(
        final=final.model_copy(update={"reports": new_reports}),
        failed_sections=failed,
        warnings=warnings,
    )


def _guarantee_report_images(
    reports: dict[str, ReportBundle],
    video_path: Path,
    frames_dir: Path,
    *,
    max_width: int,
    mark_times: list[float] | None,
    mark_events: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, ReportBundle], int]:
    """Last-resort: give a section a representative still so the report isn't image-less.

    Returns ``(updated_reports, num_recovered)``. A section may only take a real MARK time from
    an event tagged for THAT section (matching ``section_id``, and ``report_type`` when the event
    carries one). A section with no matching tap gets no still — never 0.0s, and never another
    section's mark time, which is what the old k-th-mark → k-th-section mapping produced.

    ``mark_times`` (bare floats) cannot be attributed to a section at all, so it can no longer
    place a still; when that is all we have, stills are spaced evenly across the recording and
    labeled representative. Never raises.
    """
    section_slots: list[tuple[str, int]] = []  # (report_type, section_index)
    for rt, bundle in reports.items():
        for idx in range(len(bundle.sections)):
            section_slots.append((rt, idx))
    if not section_slots:
        return reports, 0

    # Real MARK times keyed by the section the inspector tapped them on.
    times_by_key: dict[tuple[str, str], list[float]] = {}
    for ev in mark_events or []:
        if not isinstance(ev, dict):
            continue
        sid = str(ev.get("section_id") or "").strip()
        if not sid:
            continue  # untagged tap — no identity, so it may not claim any section
        try:
            t = float(ev.get("t_sec", ev.get("time", 0.0)))
        except (TypeError, ValueError):
            continue
        if t <= 0.05:
            continue
        key = (str(ev.get("report_type") or "").strip(), sid)
        times_by_key.setdefault(key, []).append(round(t, 2))

    # Candidate time per section, by identity only.
    candidate_by_slot: dict[tuple[str, int], float] = {}
    for rt, idx in section_slots:
        sid = str(reports[rt].sections[idx].section_id)
        # Prefer an event tagged with this report_type; accept one that omitted report_type.
        options = times_by_key.get((rt, sid)) or times_by_key.get(("", sid))
        if options:
            candidate_by_slot[(rt, idx)] = min(options)

    if not times_by_key:
        # Nothing tagged to match against: fall back to evenly spaced representative moments
        # (never a mark time, so no section can be credited with another's tap).
        duration = probe_duration_seconds(video_path) or 0.0
        n = len(section_slots)
        for i, slot in enumerate(section_slots):
            if duration > 1.0:
                # Evenly spread across the middle of the recording (avoid the very start/end).
                candidate_by_slot[slot] = round(duration * (i + 1) / (n + 1), 2)
            else:
                candidate_by_slot[slot] = 1.0  # tiny/unknown-duration clip — grab an early frame

    mutable = {rt: [s.model_copy() for s in bundle.sections] for rt, bundle in reports.items()}
    recovered = 0
    skipped: list[str] = []
    for rt, idx in section_slots:
        sec = mutable[rt][idx]
        if sec.image_uri:  # already has an image — leave it
            continue
        ts = candidate_by_slot.get((rt, idx))
        if ts is None:
            # No tap tagged for this section. Leave it photo-less; the PDF shows the
            # missing-photo badge rather than a still from somewhere else in the walkthrough.
            skipped.append(sec.section_id)
            continue
        # Candidates are either all tap-derived or all evenly-spaced; never mixed.
        from_tap = bool(times_by_key)
        slug = f"{rt}__{sec.section_id}".replace("/", "-")
        out_path = frames_dir / f"{slug}__rep.jpg"
        if not _extract_frame_with_fallback(video_path, float(ts), out_path, max_width=max_width):
            continue
        rep = SectionFrame(
            timestamp_sec=float(ts),
            image_timestamp_sec=float(ts),
            source="manual_mark" if from_tap else "representative",
            note=(
                "Still recovered at this section's MARK tap."
                if from_tap
                else "Representative still from the recording (exact mark unavailable)."
            ),
            image_path=str(out_path),
            image_uri=out_path.as_uri(),
        )
        mutable[rt][idx] = sec.model_copy(
            update={
                "frames": [rep],
                "image_path": str(out_path),
                "image_uri": out_path.as_uri(),
            }
        )
        recovered += 1
        logger.info(
            "[frames] safety-net still for section '%s' at %.1fs (source=%s).",
            sec.section_id, float(ts), "manual_mark" if from_tap else "representative",
        )

    if skipped:
        logger.info(
            "[frames] safety net left %d section(s) photo-less because no MARK tap was tagged "
            "for them: %s", len(skipped), skipped,
        )
    if not recovered:
        return reports, 0
    updated = {
        rt: bundle.model_copy(update={"sections": mutable[rt]})
        for rt, bundle in reports.items()
    }
    return updated, recovered


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
    if not (html_inner or "").strip():
        raise RuntimeError("Report HTML was empty — nothing to render to PDF.")
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
    /* Render/perf safety net: cap embedded stills + avoid splitting figures/headings across
       pages. Keeps WeasyPrint fast and the layout clean regardless of per-template CSS. */
    img {{ max-width: 100%; height: auto; }}
    figure, .photo, .section-photo {{ page-break-inside: avoid; break-inside: avoid; }}
    h1, h2, h3 {{ page-break-after: avoid; }}
  </style>
</head>
<body>{html_inner}</body>
</html>"""
    # Ensure the destination dir exists (a fresh Volume may not have it yet) and time the
    # render so slow PDF passes are visible in logs.
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("PDF output dir not writable (%s): %s", output_path.parent, exc)
        raise RuntimeError("Report storage was unavailable while writing the PDF. Please retry.") from exc

    started = time.time()
    try:
        HTML(string=shell, base_url=str(base_dir.resolve()) + "/").write_pdf(str(output_path))
    except Exception as exc:  # noqa: BLE001
        logger.exception("WeasyPrint render failed for %s", output_path.name)
        raise RuntimeError(f"PDF rendering failed: {exc}") from exc

    if not output_path.is_file() or output_path.stat().st_size == 0:
        logger.error("WeasyPrint produced no output at %s", output_path)
        raise RuntimeError("The PDF renderer produced an empty file. Please retry generation.")
    logger.info(
        "PDF rendered: %s (%.1f KB) in %.2fs",
        output_path.name, output_path.stat().st_size / 1024.0, time.time() - started,
    )
