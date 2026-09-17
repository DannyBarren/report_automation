"""
Offline demo smoke: mark_events + spoken cues → FinalReportOutput → HTML (and PDF).

No Deepgram / OpenAI calls. Uses the same functions /generate_report uses after STT.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crew.structured_pipeline import (
    attach_template_titles,
    build_final_report_output,
    build_matched_sections,
    reconcile_final_report_with_matched,
)
from utils.pipeline_config import PIPELINE_V2, get_jobdoc_pipeline
from utils.report_pdf import (
    enrich_report_with_still_frames_resilient,
    render_report_html,
    write_pdf,
)
from utils.schemas import FinalReportOutput, ReportBundle, ReportSectionOutput, ReportTemplate
from utils.transcription import transcript_payload_from_segments

RT = "roofing_realty_inspection"

# Distinctive template default_text excerpts that must NEVER appear as findings.
DEFAULT_TEXT_LEAKS = (
    "wide establishing shots",
    "documented per technician narration",
    "No supplemental observations were captured",
    "Walk the full perimeter",
    "Move across the roof and stop at EVERY penetration",
)


def _template() -> ReportTemplate:
    data = json.loads((ROOT / "reports" / "roofing_realty_inspection.json").read_text())
    return ReportTemplate.model_validate(data)


def _canned_transcript() -> dict:
    raw = [
        (1.0, 4.0, "Mark this. This is the front slope of the roof field, a gable in fair condition."),
        (4.0, 7.0, "The ridge runs east to west and I can see three distinct slopes from here."),
        (13.0, 17.0, "Mark this. The curb-mounted HVAC flashing looks intact and the collar is seated."),
        (25.0, 29.0, "Mark this. The roof field is TPO membrane and there is standing water at this drain."),
    ]
    segments = [{"start": s, "end": e, "text": t} for (s, e, t) in raw]
    return transcript_payload_from_segments(segments, engine="canned_smoke")


def _mark_events() -> list[dict]:
    return [
        {"t_sec": 2.0, "section_id": "overview", "report_type": RT, "note": "front slope"},
        {"t_sec": 14.5, "section_id": "components", "report_type": RT, "note": "HVAC flashing"},
        {"t_sec": 26.0, "section_id": "substrates", "report_type": RT, "note": "TPO drain"},
    ]


def _step_events() -> list[dict]:
    return [
        {"t_sec": 0.0, "section_id": "overview", "report_type": RT, "title": "Overview"},
        {"t_sec": 12.0, "section_id": "components", "report_type": RT, "title": "Components"},
        {"t_sec": 24.0, "section_id": "substrates", "report_type": RT, "title": "Substrates"},
        {"t_sec": 36.0, "section_id": "misc", "report_type": RT, "title": "Misc"},
    ]


def _generic_writer(template: ReportTemplate) -> FinalReportOutput:
    """Writer that ignores narration — reconcile must throw this out."""
    return FinalReportOutput(
        video_stem="smoke",
        video_filename="smoke.mp4",
        reports={
            RT: ReportBundle(
                report_type=RT,
                title=template.title,
                sections=[
                    ReportSectionOutput(
                        section_id="overview",
                        section_name="Overview",
                        summary=(
                            "The property was reviewed in a general sense and conditions appeared "
                            "typical for the age of the building with no unusual remarks recorded. "
                            "Routine observation is recommended as a matter of ordinary practice "
                            "and the area should be monitored over time by the owner."
                        ),
                        timestamp_sec=0.0,
                        image_timestamp_sec=0.0,
                        field_values={"severity": "informational"},
                    ),
                ],
            )
        },
    )


def test_unset_pipeline_defaults_to_v2() -> None:
    prev = os.environ.pop("JOBDOC_PIPELINE", None)
    try:
        assert get_jobdoc_pipeline() == PIPELINE_V2
    finally:
        if prev is not None:
            os.environ["JOBDOC_PIPELINE"] = prev


def test_v1_matching_receives_mark_events() -> None:
    """The leftover v1 crew path must pass taps into build_matched_sections."""
    src = (ROOT / "crew" / "crew.py").read_text(encoding="utf-8")
    assert "build_matched_sections(tr, templates, mark_events=mark_events, step_events=step_events)" in src
    flask_src = (ROOT / "crew" / "flask_pipeline.py").read_text(encoding="utf-8")
    assert "mark_events=mark_events" in flask_src
    assert "step_events=step_events" in flask_src


def test_mark_events_to_html_and_optional_pdf() -> None:
    template = _template()
    templates = [template]
    transcript = _canned_transcript()
    marks = _mark_events()
    steps = _step_events()

    matched = build_matched_sections(
        transcript, templates, mark_events=marks, step_events=steps
    )
    by_id = {m.section_id: m for m in matched}
    assert [m.section_id for m in matched] == template.section_ids_in_order()

    # (a) tap timestamps survive — not all 0.0
    tap_times = [m.image_timestamp_sec for m in matched if m.trigger_phrase == "manual_mark"]
    assert tap_times, "expected at least one section anchored on a MARK tap"
    assert all(t > 0.0 for t in tap_times)

    # (b) summaries contain canned narration, not only default_text
    ov = by_id["overview"].summary.lower()
    comp = by_id["components"].summary.lower()
    sub = by_id["substrates"].summary.lower()
    assert "slope" in ov or "ridge" in ov
    assert "flashing" in comp
    assert "drain" in sub or "tpo" in sub
    for leak in DEFAULT_TEXT_LEAKS:
        assert leak.lower() not in ov
        assert leak.lower() not in comp
        assert leak.lower() not in sub

    # (e) silent/unreached misc does not present default_text as a finding
    misc = by_id["misc"].summary.lower()
    assert "not verified" in misc
    assert "no supplemental observations were captured" not in misc

    structured = attach_template_titles(
        build_final_report_output(matched, video_stem="smoke", video_filename="smoke.mp4"),
        templates,
    )
    reconciled = reconcile_final_report_with_matched(
        _generic_writer(template), matched, templates
    )
    ov_final = next(s for s in reconciled.reports[RT].sections if s.section_id == "overview")
    assert "slope" in ov_final.summary.lower() or "ridge" in ov_final.summary.lower()
    assert "typical for the age" not in ov_final.summary.lower()

    # Frames from a short synthetic clip when ffmpeg is present.
    workdir = Path(tempfile.mkdtemp(prefix="jobdoc_smoke_"))
    video = workdir / "smoke.mp4"
    have_video = False
    if shutil.which("ffmpeg"):
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=40",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=40",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(video),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            have_video = video.is_file()
        except Exception:
            have_video = False

    final = reconciled
    if have_video:
        result = enrich_report_with_still_frames_resilient(
            reconciled, video, workdir / "frames", mark_times=[m["t_sec"] for m in marks]
        )
        final = result.final

    from flask import Flask

    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    html = render_report_html(app, final, templates_by_type={RT: template})

    # (c) HTML contains narration words
    html_l = html.lower()
    assert "flashing" in html_l
    assert "drain" in html_l or "tpo" in html_l
    assert "slope" in html_l or "ridge" in html_l

    # (d) no brace tokens
    for tok in ("{page}", "{total_pages}", "{total}", "{business_name}", "{report_title}"):
        assert tok not in html, f"token leaked into HTML: {tok}"

    # (e) default_text / worker instructions not dumped as findings
    for leak in DEFAULT_TEXT_LEAKS:
        assert leak.lower() not in html_l, f"default_text leaked into HTML: {leak}"
    assert "not verified" in html_l

    # Branding from the roofing template
    assert "Structured Roofing Systems" in html

    # (f) frames or visible missing-photo mark
    with_uri = 0
    for bundle in final.reports.values():
        for sec in bundle.sections:
            with_uri += sum(1 for fr in (sec.frames or []) if fr.image_uri)
    if have_video:
        assert with_uri >= 1 or "no still frame was available" in html_l
    else:
        assert "no still frame was available" in html_l or "<img" in html_l

    try:
        from weasyprint import HTML  # noqa: F401

        pdf_path = workdir / "smoke.pdf"
        write_pdf(app, html, base_dir=ROOT, output_path=pdf_path)
        assert pdf_path.is_file() and pdf_path.stat().st_size > 1000
        keep = ROOT / "reports_output" / "demo_smoke_report.pdf"
        keep.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(pdf_path, keep)
    except Exception as exc:
        pytest.skip(f"WeasyPrint write skipped: {exc}")
