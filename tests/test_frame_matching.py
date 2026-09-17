"""
Offline guard: a MARK tap's still must land on the tap's OWN section.

Regression under test — before the section_id-first matcher, a spoken "Mark this." cue that no
step window claimed was handed to the next section with no evidence ("use them up"), so a
components mark at ~23s printed its still under overview/substrates.

No Deepgram / OpenAI calls. Uses the same functions /generate_report uses after STT.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crew.structured_pipeline import (
    build_matched_sections,
    reconcile_final_report_with_matched,
)
from utils.report_pdf import enrich_report_with_still_frames_resilient, render_report_html
from utils.schemas import (
    FinalReportOutput,
    MatchedSection,
    ReportBundle,
    ReportSectionOutput,
    ReportTemplate,
)
from utils.transcription import transcript_payload_from_segments

RT = "roofing_realty_inspection"

OVERVIEW_TAP = 5.0
COMPONENTS_TAP = 23.0
OVERVIEW_WORDS = ("slope", "ridge")
COMPONENTS_WORDS = ("flashing", "collar")


def _template() -> ReportTemplate:
    data = json.loads((ROOT / "reports" / "roofing_realty_inspection.json").read_text())
    return ReportTemplate.model_validate(data)


def _transcript() -> dict:
    """40s walkthrough: overview cue at ~4s, components cue at ~22s."""
    raw = [
        (4.0, 7.5, "Mark this. This is the front slope of the roof field and the ridge runs east to west."),
        (8.0, 12.0, "Three distinct slopes are visible from this corner of the gable."),
        (22.0, 26.0, "Mark this. The curb-mounted HVAC flashing is intact and the collar is seated correctly."),
        (27.0, 31.0, "The pipe boot beside it shows some cracking at the base."),
    ]
    return transcript_payload_from_segments(
        [{"start": s, "end": e, "text": t} for (s, e, t) in raw], engine="canned_frame_match"
    )


def _marks_in_order() -> list[dict]:
    return [
        {"t_sec": OVERVIEW_TAP, "section_id": "overview", "report_type": RT, "note": "front slope"},
        {"t_sec": COMPONENTS_TAP, "section_id": "components", "report_type": RT, "note": "HVAC flashing"},
    ]


def _marks_out_of_order() -> list[dict]:
    """Components tap listed BEFORE the overview tap — section_id must beat list position."""
    return list(reversed(_marks_in_order()))


def _matched(marks: list[dict], steps: list[dict] | None = None) -> dict[str, MatchedSection]:
    matched = build_matched_sections(
        _transcript(), [_template()], mark_events=marks, step_events=steps
    )
    return {m.section_id: m for m in matched}


def _extract_times(section: MatchedSection) -> list[float]:
    return [round(float(f.image_timestamp_sec), 2) for f in section.frames]


@pytest.mark.parametrize(
    "marks, label",
    [(_marks_in_order(), "in_order"), (_marks_out_of_order(), "out_of_order")],
)
def test_tap_still_lands_on_its_own_section(marks: list[dict], label: str) -> None:
    """A tap's still time is its own t_sec, whatever order the taps arrived in."""
    by_id = _matched(marks)

    # (1) overview extracts at its OWN tap (5.0s), never the components tap (23.0s).
    ov_times = _extract_times(by_id["overview"])
    assert ov_times, f"[{label}] overview lost its still entirely"
    assert by_id["overview"].image_timestamp_sec == pytest.approx(OVERVIEW_TAP, abs=1.0)
    assert all(abs(t - COMPONENTS_TAP) > 1.0 for t in ov_times), (
        f"[{label}] overview stole the components tap time: {ov_times}"
    )

    # (2) components extracts at 23.0s — not 5.0s and not 0.0s.
    comp_times = _extract_times(by_id["components"])
    assert comp_times, f"[{label}] components lost its still entirely"
    assert by_id["components"].image_timestamp_sec == pytest.approx(COMPONENTS_TAP, abs=1.0)
    assert all(abs(t - OVERVIEW_TAP) > 1.0 for t in comp_times), (
        f"[{label}] components stole the overview tap time: {comp_times}"
    )
    assert all(t > 0.05 for t in comp_times), f"[{label}] components fell back to 0.0s: {comp_times}"

    # (3)/(4) each section's prose is its own narration, with no bleed either way.
    ov = by_id["overview"].summary.lower()
    comp = by_id["components"].summary.lower()
    assert any(w in ov for w in OVERVIEW_WORDS), f"[{label}] overview lost slope/ridge: {ov[:120]}"
    assert not any(w in ov for w in COMPONENTS_WORDS), (
        f"[{label}] components narration bled into overview: {ov[:160]}"
    )
    assert any(w in comp for w in COMPONENTS_WORDS), (
        f"[{label}] components lost flashing/collar: {comp[:120]}"
    )
    assert not any(w in comp for w in OVERVIEW_WORDS), (
        f"[{label}] overview narration bled into components: {comp[:160]}"
    )


def test_unmarked_sections_get_no_borrowed_still() -> None:
    """Sections the inspector never marked must stay unverified, not adopt a leftover cue."""
    by_id = _matched(_marks_in_order())
    for sid in ("substrates", "misc"):
        assert by_id[sid].frames == [], (
            f"{sid} was never marked but received a still at "
            f"{_extract_times(by_id[sid])} — a leftover cue was dumped on it"
        )
        assert "not verified" in by_id[sid].summary.lower()


def test_no_marked_section_after_the_first_anchors_at_zero() -> None:
    """Assertion 5: a tapped section other than the first never extracts at 0.0s."""
    matched = build_matched_sections(
        _transcript(), [_template()], mark_events=_marks_in_order(), step_events=None
    )
    tapped = {"overview", "components"}
    for pos, m in enumerate(matched):
        if pos == 0 or m.section_id not in tapped:
            continue
        for t in _extract_times(m):
            assert t > 0.05, f"{m.section_id} (position {pos}) had a tap but extracts at {t}"


def test_out_of_order_marks_match_in_order_result() -> None:
    """section_id wins over the k-th-cue ordinal map: tap order must not change the outcome."""
    a = _matched(_marks_in_order())
    b = _matched(_marks_out_of_order())
    assert {k: _extract_times(v) for k, v in a.items()} == {
        k: _extract_times(v) for k, v in b.items()
    }


def test_step_windows_still_place_cues() -> None:
    """Regression guard: the step-window path (the recorder's normal output) still works."""
    steps = [
        {"t_sec": 0.0, "section_id": "overview", "report_type": RT, "title": "Overview"},
        {"t_sec": 20.0, "section_id": "components", "report_type": RT, "title": "Components"},
    ]
    by_id = _matched(_marks_in_order(), steps)
    assert by_id["overview"].image_timestamp_sec == pytest.approx(OVERVIEW_TAP, abs=1.0)
    assert by_id["components"].image_timestamp_sec == pytest.approx(COMPONENTS_TAP, abs=1.0)
    assert "flashing" not in by_id["overview"].summary.lower()


def test_tap_wins_when_merged_with_a_near_duplicate_cue() -> None:
    """A tap and a cue describing one finding collapse to ONE still, at the tap's t_sec."""
    by_id = _matched(_marks_in_order())
    assert _extract_times(by_id["overview"]) == [OVERVIEW_TAP]
    assert _extract_times(by_id["components"]) == [COMPONENTS_TAP]
    assert [f.source for f in by_id["overview"].frames] == ["manual_mark"]


def _writer_that_swaps_sections(template: ReportTemplate) -> FinalReportOutput:
    """LLM writer output with overview/components content crossed and bogus timestamps."""
    return FinalReportOutput(
        video_stem="frames",
        video_filename="frames.mp4",
        reports={
            RT: ReportBundle(
                report_type=RT,
                title=template.title,
                sections=[
                    ReportSectionOutput(
                        section_id="overview",
                        section_name="Overview",
                        summary=(
                            "The curb-mounted HVAC flashing is intact and the collar is seated "
                            "correctly, and the pipe boot beside it shows cracking at the base "
                            "which should be sealed during the next maintenance visit by the "
                            "roofing contractor before the wet season arrives on site."
                        ),
                        timestamp_sec=COMPONENTS_TAP,
                        image_timestamp_sec=COMPONENTS_TAP,
                        field_values={"severity": "informational"},
                    ),
                    ReportSectionOutput(
                        section_id="components",
                        section_name="Components",
                        summary=(
                            "This is the front slope of the roof field and the ridge runs east to "
                            "west with three distinct slopes visible from the corner of the gable, "
                            "all appearing to be in fair condition for the age of the covering as "
                            "observed from the ground level during this review."
                        ),
                        timestamp_sec=OVERVIEW_TAP,
                        image_timestamp_sec=OVERVIEW_TAP,
                        field_values={"severity": "informational"},
                    ),
                ],
            )
        },
    )


def test_reconcile_does_not_let_writer_swap_frame_times() -> None:
    """Rule E: reconcile must never take another section's image_timestamp_sec."""
    template = _template()
    matched = build_matched_sections(
        _transcript(), [template], mark_events=_marks_in_order(), step_events=None
    )
    final = reconcile_final_report_with_matched(
        _writer_that_swaps_sections(template), matched, [template]
    )
    secs = {s.section_id: s for s in final.reports[RT].sections}
    assert secs["overview"].image_timestamp_sec == pytest.approx(OVERVIEW_TAP, abs=1.0)
    assert secs["components"].image_timestamp_sec == pytest.approx(COMPONENTS_TAP, abs=1.0)
    for sid, frames in ((s, secs[s].frames) for s in ("overview", "components")):
        assert frames, f"{sid} lost its deterministic frames during reconcile"
    # The writer's crossed prose must not survive on the wrong section.
    assert "flashing" not in secs["overview"].summary.lower()


def _fixture_video(workdir: Path) -> Path | None:
    """Reuse the committed smoke fixture, else synthesize one, else give up (no ffmpeg)."""
    existing = ROOT / "reports_output" / "demo_smoke_fixture.mp4"
    if existing.is_file():
        return existing
    if not shutil.which("ffmpeg"):
        return None
    out = workdir / "fixture.mp4"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=40",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
            ],
            check=True, capture_output=True, timeout=180,
        )
    except Exception:
        return None
    return out if out.is_file() else None


def test_real_jpegs_are_per_section_and_never_reused() -> None:
    """End of the chain: distinct JPEGs per section, each bound to its own <img> in the HTML."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not on PATH — cannot prove real still extraction")
    workdir = Path(tempfile.mkdtemp(prefix="jobdoc_frames_"))
    video = _fixture_video(workdir)
    if video is None:
        pytest.skip("no fixture video available")

    template = _template()
    marks = _marks_in_order()
    matched = build_matched_sections(
        _transcript(), [template], mark_events=marks, step_events=None
    )
    final = reconcile_final_report_with_matched(
        _writer_that_swaps_sections(template), matched, [template]
    )
    result = enrich_report_with_still_frames_resilient(
        final, video, workdir / "frames", mark_times=[m["t_sec"] for m in marks]
    )

    by_id = {s.section_id: s for s in result.final.reports[RT].sections}
    ov_imgs = [f for f in (by_id["overview"].frames or []) if f.image_uri]
    comp_imgs = [f for f in (by_id["components"].frames or []) if f.image_uri]
    assert ov_imgs and comp_imgs, "both marked sections must produce a real still"

    # Each JPEG is named for its own section and holds its own tap moment.
    assert all("__overview__" in f.image_path for f in ov_imgs)
    assert all("__components__" in f.image_path for f in comp_imgs)
    assert ov_imgs[0].image_timestamp_sec == pytest.approx(OVERVIEW_TAP, abs=1.0)
    assert comp_imgs[0].image_timestamp_sec == pytest.approx(COMPONENTS_TAP, abs=1.0)

    # No JPEG is shared between sections, and the two frames are genuinely different pictures
    # (byte-compare — no computer vision involved).
    assert {f.image_path for f in ov_imgs}.isdisjoint({f.image_path for f in comp_imgs})
    assert Path(ov_imgs[0].image_path).read_bytes() != Path(comp_imgs[0].image_path).read_bytes()

    # Unmarked sections must not have borrowed a picture.
    for sid in ("substrates", "misc"):
        assert not [f for f in (by_id[sid].frames or []) if f.image_uri], (
            f"{sid} was never marked but shows a still"
        )

    # The HTML binds each <img> inside its own section block (no zip of images to sections).
    from flask import Flask

    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    html = render_report_html(app, result.final, templates_by_type={RT: template})
    ov_name = Path(ov_imgs[0].image_path).name
    comp_name = Path(comp_imgs[0].image_path).name
    assert ov_name in html and comp_name in html
    # Overview's section block must reference overview's JPEG before components' block starts.
    ov_at, comp_at = html.find(ov_name), html.find(comp_name)
    assert ov_at < comp_at, "section image order does not follow template section order"


def _filler_report(template: ReportTemplate) -> FinalReportOutput:
    """A fully degraded report: every section unverified, no frames, 0.0s anchors.

    This is the state that triggers the whole-report safety net — the per-section pass extracts
    nothing because there is nothing to extract from.
    """
    return FinalReportOutput(
        video_stem="frames",
        video_filename="frames.mp4",
        reports={
            RT: ReportBundle(
                report_type=RT,
                title=template.title,
                sections=[
                    ReportSectionOutput(
                        section_id=sec.id,
                        section_name=sec.name,
                        summary=f"Not verified — {sec.name} was not reached during this walkthrough.",
                        timestamp_sec=0.0,
                        image_timestamp_sec=0.0,
                        field_values={},
                        frames=[],
                    )
                    for sec in template.sections
                ],
            )
        },
    )


def test_safety_net_places_stills_by_section_id_not_list_position() -> None:
    """The image-less safety net must key on section_id, not the k-th mark → k-th section zip.

    The inspector walks OUT of template order: components is marked early (8s) and overview late
    (30s). The old net sorted the mark times and zipped them onto sections in template order, so
    overview took the 8s components tap and components took the 30s overview tap — inverted.
    """
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not on PATH — cannot prove real still extraction")
    workdir = Path(tempfile.mkdtemp(prefix="jobdoc_netfix_"))
    video = _fixture_video(workdir)
    if video is None:
        pytest.skip("no fixture video available")

    template = _template()
    components_at, overview_at = 8.0, 30.0
    marks = [
        {"t_sec": components_at, "section_id": "components", "report_type": RT},
        {"t_sec": overview_at, "section_id": "overview", "report_type": RT},
    ]
    # Template order is overview → components, but the taps run components → overview in time.
    assert template.section_ids_in_order().index("overview") < template.section_ids_in_order().index(
        "components"
    )
    assert marks[0]["t_sec"] < marks[1]["t_sec"]

    result = enrich_report_with_still_frames_resilient(
        _filler_report(template),
        video,
        workdir / "frames",
        mark_events=marks,
    )
    by_id = {s.section_id: s for s in result.final.reports[RT].sections}

    ov = [f for f in (by_id["overview"].frames or []) if f.image_uri]
    comp = [f for f in (by_id["components"].frames or []) if f.image_uri]
    assert ov, "overview has a tagged tap and should recover a still"
    assert comp, "components has a tagged tap and should recover a still"
    # Each section gets ITS OWN tap. The old sorted-zip would invert exactly these two.
    assert ov[0].image_timestamp_sec == pytest.approx(overview_at, abs=0.01)
    assert comp[0].image_timestamp_sec == pytest.approx(components_at, abs=0.01)

    # Never 0.0, never a neighbour's time, never the same JPEG twice.
    for sid, frames in (("overview", ov), ("components", comp)):
        assert frames[0].image_timestamp_sec > 0.05, f"{sid} recovered at 0.0s"
        assert f"__{sid}__" in frames[0].image_path
    assert ov[0].image_path != comp[0].image_path
    assert Path(ov[0].image_path).read_bytes() != Path(comp[0].image_path).read_bytes()

    # Sections with no tap of their own stay photo-less rather than borrowing one.
    for sid in ("substrates", "misc"):
        assert not [f for f in (by_id[sid].frames or []) if f.image_uri], (
            f"{sid} had no tagged tap but the safety net still gave it a photo"
        )
    assert any("substrates" in s.lower() for s in result.failed_sections), (
        f"untouched sections should still be reported as photo-less: {result.failed_sections}"
    )


def test_safety_net_ignores_untagged_marks_for_section_placement() -> None:
    """A tap with no section_id may not claim a section — it has no identity to match on."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not on PATH — cannot prove real still extraction")
    workdir = Path(tempfile.mkdtemp(prefix="jobdoc_netuntagged_"))
    video = _fixture_video(workdir)
    if video is None:
        pytest.skip("no fixture video available")

    template = _template()
    untagged = [{"t_sec": COMPONENTS_TAP}, {"t_sec": OVERVIEW_TAP}]
    result = enrich_report_with_still_frames_resilient(
        _filler_report(template), video, workdir / "frames", mark_events=untagged
    )
    for bundle in result.final.reports.values():
        for sec in bundle.sections:
            for fr in sec.frames or []:
                if not fr.image_uri:
                    continue
                # Whatever moment was used, it must not be sold as this section's MARK tap.
                assert fr.source == "representative", (
                    f"{sec.section_id} credited an untagged tap as its own mark"
                )
                assert fr.image_timestamp_sec > 0.05


def _stub_crewai() -> None:
    """jobdoc_crew_v2 imports crewai at module load; the notes helper needs no real Crew."""
    if "crewai" in sys.modules:
        return
    mod = types.ModuleType("crewai")

    class _Placeholder:
        def __init__(self, *args, **kwargs) -> None: ...

    for name in ("Crew", "Process", "Agent", "Task", "LLM"):
        setattr(mod, name, _Placeholder)
    sys.modules["crewai"] = mod


def test_crew_abort_notes_read_as_fallback_not_failure() -> None:
    """Defect 1: an aborted Crew that still produced a PDF is not reported as a failed job."""
    _stub_crewai()
    from crew.jobdoc_crew_v2 import collect_v2_warnings
    from crew.report_resilience import build_pipeline_result_fallback

    stage_notes = [
        "AI step 'analyze' did not finish — continuing with a structured draft.",
        "Could not parse transcript analysis. Please retry.",
        "AI step 'domain' did not finish — continuing with a structured draft.",
        "AI step 'writing' did not finish — continuing with a structured draft.",
        "Could not build the structured report. Please retry.",
        "AI step 'qa' did not finish — continuing with a structured draft.",
    ]
    result = build_pipeline_result_fallback(
        _transcript(),
        [_template()],
        video_stem="roof",
        video_filename="roof.mp4",
        stage_notes=stage_notes,
        mark_events=_marks_in_order(),
        step_events=None,
    )
    notes = collect_v2_warnings(result)
    blob = " ".join(notes).lower()

    # The fallback is described once, in plain language.
    assert sum(1 for n in notes if "did not finish" in n.lower()) == 1
    assert "uses your spoken findings and mark timestamps" in blob

    # Internal stage noise is gone now that a real report exists.
    assert "could not parse transcript analysis" not in blob
    assert "could not build the structured report" not in blob
    assert "please retry" not in blob
    assert "quality review score" not in blob

    # The actionable notes survive.
    assert "mark this" in blob or "unclear audio" in blob
    assert len(notes) < len(stage_notes), "notes should be fewer than the raw stage aborts"
