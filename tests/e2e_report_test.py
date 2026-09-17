"""
e2e_report_test.py — Realistic end-to-end verification of the JobDoc capture→report path.

This exercises the DETERMINISTIC content pipeline exactly as production runs it, but without
the cloud crew (no OpenAI/Deepgram needed): it feeds a representative transcript (multiple
"Mark this." cues + real narration), continuous-recording step boundaries, and manual MARK
taps through the authoritative matcher, then simulates a MISBEHAVING LLM writer (reordered
sections, an invented section id, a dropped section, and a generic summary that discards the
narration) and verifies the reconciliation forces the structured data to win.

It then extracts real still frames from a synthetic MP4 (ffmpeg) and renders the actual
client PDF (WeasyPrint), asserting real narration, correct section order + images, and no
template-variable leakage.

Run:
    PYTHONPATH=".venv/lib/python3.14/site-packages" PATH=".venv/bin:$PATH" \
        .venv/bin/python tests/e2e_report_test.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crew.structured_pipeline import (  # noqa: E402
    attach_template_titles,
    build_final_report_output,
    build_matched_sections,
    reconcile_final_report_with_matched,
)
from utils.report_pdf import (  # noqa: E402
    enrich_report_with_still_frames_resilient,
    render_report_html,
    write_pdf,
)
from utils.schemas import (  # noqa: E402
    FinalReportOutput,
    ReportBundle,
    ReportSectionOutput,
    ReportTemplate,
)
from utils.transcription import transcript_payload_from_segments  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {msg}")
    if not cond:
        FAILURES.append(msg)


def load_roofing_template() -> ReportTemplate:
    data = json.loads((ROOT / "reports" / "roofing_realty_inspection.json").read_text())
    return ReportTemplate.model_validate(data)


def build_synthetic_transcript() -> dict:
    """One continuous narrated walkthrough: Overview, Components, Substrates, Misc."""
    raw = [
        # Overview (step starts 0)
        (1.0, 3.0, "Mark this. This is the front slope, a moderately pitched gable roof in fair overall condition."),
        (3.0, 6.0, "The roof has three main slopes and a prominent ridge running east to west."),
        (7.0, 9.5, "Mark this. Here is the rear elevation showing the eave line and the gutters along the edge."),
        # Components (step starts 12)
        (13.0, 16.0, "Mark this. This is the main HVAC unit mounted on a curb near the center of the roof and the flashing looks intact."),
        (17.0, 20.5, "Mark this. Here is a plumbing vent stack with a cracked boot that is separating from the collar."),
        # Substrates (step starts 24)
        (25.0, 28.0, "Mark this. The roof field is TPO membrane with heat welded seams and the seams look sound."),
        (29.0, 32.5, "Mark this. There is a puncture in the membrane near this drain with granule debris scattered around it."),
        # Misc (step starts 36)
        (37.0, 40.0, "Mark this. Note the large overhanging tree on the north side that could drop debris onto the roof."),
    ]
    segments = [{"start": s, "end": e, "text": t} for (s, e, t) in raw]
    return transcript_payload_from_segments(segments, engine="synthetic_test")


def step_events() -> list[dict]:
    rt = "roofing_realty_inspection"
    return [
        {"t_sec": 0.0, "section_id": "overview", "report_type": rt, "title": "Overview"},
        {"t_sec": 12.0, "section_id": "components", "report_type": rt, "title": "Components"},
        {"t_sec": 24.0, "section_id": "substrates", "report_type": rt, "title": "Substrates"},
        {"t_sec": 36.0, "section_id": "misc", "report_type": rt, "title": "Misc"},
    ]


def mark_events() -> list[dict]:
    rt = "roofing_realty_inspection"
    return [
        {"t_sec": 14.0, "section_id": "components", "report_type": rt, "note": "HVAC unit on curb"},
        {"t_sec": 18.0, "section_id": "components", "report_type": rt, "note": "cracked vent boot"},
    ]


def make_test_video(dst: Path, seconds: int = 45) -> bool:
    if not shutil.which("ffmpeg"):
        print("  [WARN] ffmpeg not on PATH — skipping frame/PDF stages.")
        return False
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size=640x480:rate=15:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(dst),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        return dst.is_file() and dst.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] could not synthesize test video: {exc}")
        return False


def simulate_bad_llm_report(template: ReportTemplate) -> FinalReportOutput:
    """A writer output exhibiting every known failure mode we must defend against."""
    rt = template.report_type
    sections = [
        # 1) Reordered: substrates FIRST (should be 3rd) — and it DISCARDS the real narration
        #    (generic prose, no TPO/puncture/seam content) → structured summary must win.
        ReportSectionOutput(
            section_id="substrates",
            section_name="Substrates",
            summary=(
                "The roofing materials were reviewed during the inspection and appear consistent "
                "with a standard commercial assembly. Overall the area presented in a manner "
                "typical for its age and no unusual concerns were noted at the time of review. "
                "Routine maintenance is advised going forward as a general best practice."
            ),
            timestamp_sec=0.0,
            image_timestamp_sec=0.0,
            field_values={"severity": "informational"},
        ),
        # 2) An INVENTED section id not in the template → must be dropped.
        ReportSectionOutput(
            section_id="chimney_bonus",
            section_name="Chimney (invented)",
            summary="This fabricated section should never reach the final report or the PDF. " * 6,
            timestamp_sec=5.0,
            image_timestamp_sec=5.0,
            field_values={"severity": "major"},
        ),
        # 3) Overview: EMPTY summary (writer produced nothing) → structured narration must win.
        ReportSectionOutput(
            section_id="overview",
            section_name="Overview",
            summary="",
            timestamp_sec=0.0,
            image_timestamp_sec=0.0,
            field_values={},
        ),
        # 4) Components: a FAITHFUL, substantive professional summary grounded in the narration
        #    (mentions HVAC, vent boot) → this writer prose SHOULD be kept.
        ReportSectionOutput(
            section_id="components",
            section_name="Components",
            summary=(
                "Two roof penetrations were documented in this area. The first is a curb-mounted "
                "HVAC unit near the center of the roof whose flashing appears intact and sound. "
                "The second is a plumbing vent stack whose boot is cracked and separating from the "
                "collar, an active water-entry path that warrants prompt boot replacement. The "
                "remaining penetrations should be monitored and sealed as needed to protect the "
                "membrane and preserve watertightness across the roof field."
            ),
            timestamp_sec=13.0,
            image_timestamp_sec=14.0,
            field_values={"severity": "moderate", "recommendations": "Replace the cracked vent boot."},
        ),
        # 5) 'misc' section is DROPPED entirely by the writer → must be restored from matched.
    ]
    return FinalReportOutput(
        video_stem="roof_job_demo",
        video_filename="roof_job_demo.mp4",
        reports={rt: ReportBundle(report_type=rt, title=template.title, sections=sections)},
    )


def main() -> int:
    print("=" * 78)
    print("JobDoc END-TO-END VERIFICATION (deterministic content path)")
    print("=" * 78)

    template = load_roofing_template()
    templates = [template]
    transcript = build_synthetic_transcript()
    rt = template.report_type
    tpl_order = template.section_ids_in_order()
    print(f"\nTemplate: {template.report_type} — sections in order: {tpl_order}")

    # ---- Stage 1: authoritative deterministic matching ---------------------------------
    print("\n[1] build_matched_sections (spoken cues + manual taps + step windows)")
    matched = build_matched_sections(
        transcript, templates, mark_events=mark_events(), step_events=step_events()
    )
    by_id = {m.section_id: m for m in matched}
    check([m.section_id for m in matched] == tpl_order, "matched sections follow template order")
    check("puncture" in by_id["substrates"].segment_text_raw.lower(), "substrates narration preserved (puncture)")
    check("tpo" in by_id["substrates"].segment_text_raw.lower() or "membrane" in by_id["substrates"].segment_text_raw.lower(), "substrates narration preserved (TPO/membrane)")
    check("hvac" in by_id["components"].segment_text_raw.lower(), "components narration preserved (HVAC)")
    check("slope" in by_id["overview"].segment_text_raw.lower(), "overview narration preserved (slope)")
    check(by_id["overview"].image_timestamp_sec > 0.0, "overview image_timestamp_sec computed (>0)")
    check(len(by_id["components"].frames) >= 2, "components has multiple evidence frames (>=2 marks)")
    for m in matched:
        print(f"    - {m.section_id:11s} evidence={m.trigger_phrase:12s} frames={len(m.frames)} "
              f"img_t={m.image_timestamp_sec:.1f}s narr='{(m.segment_text_raw or '')[:48]}'")

    # ---- Stage 2: reconcile a misbehaving LLM writer against the match -----------------
    print("\n[2] reconcile_final_report_with_matched (structured data must win)")
    bad = simulate_bad_llm_report(template)
    reconciled = reconcile_final_report_with_matched(bad, matched, templates)
    secs = reconciled.reports[rt].sections
    ids = [s.section_id for s in secs]
    check(ids == tpl_order, f"final section set+order == template ({ids})")
    check("chimney_bonus" not in ids, "invented section id dropped")
    check("misc" in ids, "writer-dropped section restored")
    ov = next(s for s in secs if s.section_id == "overview")
    check("slope" in ov.summary.lower() or "ridge" in ov.summary.lower(),
          "overview: empty writer summary replaced by narration-grounded structured summary")
    sub = next(s for s in secs if s.section_id == "substrates")
    check("tpo" in sub.summary.lower() or "puncture" in sub.summary.lower() or "membrane" in sub.summary.lower(),
          "substrates: narration-discarding writer summary replaced by structured summary")
    comp = next(s for s in secs if s.section_id == "components")
    check("hvac" in comp.summary.lower() and "boot" in comp.summary.lower(),
          "components: faithful writer prose kept")
    check(all(len(s.summary.split()) >= 30 for s in secs), "every section summary is substantive (>=30 words)")
    for s in secs:
        check(bool(s.frames) or s.section_id in ("misc",),
              f"section '{s.section_id}' carries evidence frames")

    # ---- Stage 3: pure structured path (LLM-unavailable fallback) ----------------------
    print("\n[3] pure structured fallback (build_final_report_output)")
    structured = attach_template_titles(
        build_final_report_output(matched, video_stem="roof_job_demo", video_filename="roof_job_demo.mp4"),
        templates,
    )
    check(list(structured.reports[rt].sections and [x.section_id for x in structured.reports[rt].sections]) == tpl_order,
          "structured fallback preserves section order")

    # ---- Stage 4: real still-frame extraction (ffmpeg) + PDF (WeasyPrint) --------------
    print("\n[4] frame extraction + PDF render")
    workdir = Path(tempfile.mkdtemp(prefix="jobdoc_e2e_"))
    video = workdir / "roof_job_demo.mp4"
    have_video = make_test_video(video, seconds=45)
    if have_video:
        frames_dir = workdir / "frames"
        result = enrich_report_with_still_frames_resilient(
            reconciled, video, frames_dir, mark_times=[e["t_sec"] for e in mark_events()]
        )
        enriched = result.final
        # Count sections that actually received an image.
        with_img = 0
        for bundle in enriched.reports.values():
            for s in bundle.sections:
                imgs = [f for f in (s.frames or []) if f.image_uri]
                if imgs:
                    with_img += 1
                    print(f"    - {s.section_id:11s} → {len(imgs)} still(s) extracted")
        check(with_img >= 3, "at least 3 sections received real extracted stills")
        comp_e = next(s for s in enriched.reports[rt].sections if s.section_id == "components")
        check(len([f for f in comp_e.frames if f.image_uri]) >= 2,
              "components rendered multiple stills (one per mark)")

        # Render the real client PDF.
        try:
            from flask import Flask

            app = Flask(__name__, template_folder=str(ROOT / "templates"))
            html = render_report_html(app, enriched, templates_by_type={rt: template})
            check("{page}" not in html and "{total_pages}" not in html and "{business_name}" not in html,
                  "no template-variable leakage in rendered HTML")
            check("Structured Roofing Systems" in html, "footer/brand rendered from pdf_styling")
            pdf_path = workdir / "report.pdf"
            write_pdf(app, html, base_dir=ROOT, output_path=pdf_path)
            check(pdf_path.is_file() and pdf_path.stat().st_size > 5000,
                  f"PDF produced ({pdf_path.stat().st_size // 1024} KB) at {pdf_path}")
            # Keep the PDF for manual inspection.
            keep = ROOT / "reports_output" / "e2e_verification_report.pdf"
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(pdf_path, keep)
            print(f"    PDF copied for inspection: {keep}")
        except Exception as exc:  # noqa: BLE001
            check(False, f"PDF render raised: {exc}")
    else:
        print("  [SKIP] frame/PDF stages (no ffmpeg).")

    # ---- Stage 5: resilience (weak/empty transcription, no marks, no steps) ------------
    print("\n[5] resilience — empty transcript still yields a usable, ordered report")
    empty_tx = transcript_payload_from_segments([], engine="none")
    empty_matched = build_matched_sections(empty_tx, templates)
    check([m.section_id for m in empty_matched] == tpl_order,
          "empty transcript still produces all template sections in order")
    check(all(m.trigger_phrase == "none" for m in empty_matched), "empty transcript sections marked as unverified filler")
    check(all(len(m.summary.split()) >= 20 for m in empty_matched), "filler sections are non-empty unverified copy")
    check(all("not verified" in m.summary.lower() for m in empty_matched), "filler says not verified")
    ov_default = (template.content_for_section("overview").default_text or "").strip()
    check(
        ov_default.lower() not in empty_matched[0].summary.lower(),
        "filler does not present default_text as a finding",
    )
    empty_final = reconcile_final_report_with_matched(
        FinalReportOutput(reports={}), empty_matched, templates
    )
    check([s.section_id for s in empty_final.reports[rt].sections] == tpl_order,
          "reconcile with empty writer output still returns full ordered report")

    # ---- Stage 6: caption/severity quality regression checks ---------------------------
    print("\n[6] caption + severity quality")
    from utils.pdf_render import frame_caption, section_severity_display

    struct_secs = build_final_report_output(matched, video_stem="x", video_filename="x.mp4").reports[rt].sections
    sub_struct = next(s for s in struct_secs if s.section_id == "substrates")
    caps = [frame_caption(f, sub_struct) for f in sub_struct.frames]
    check(all("mark this" not in c.lower() for c in caps), "'Mark this.' never leaks into photo captions")
    disp = section_severity_display(sub_struct, template)
    check(disp["label"].lower() != "see narrative summary", "severity pill is a real severity (not a placeholder)")
    check(disp["band"] in ("green", "yellow", "red"), "severity resolves to a valid band")

    print("\n" + "=" * 78)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} CHECK(S) FAILED")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("RESULT: ALL CHECKS PASSED")
    return 0


def test_e2e_report_pipeline() -> None:
    """pytest entry so ``python -m pytest tests/e2e_report_test.py`` runs the script."""
    assert main() == 0, "e2e_report_test failed: " + "; ".join(FAILURES)


if __name__ == "__main__":
    raise SystemExit(main())
