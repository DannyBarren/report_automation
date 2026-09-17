"""Render the defect scenario (tagged marks, NO step_events) so photo placement is eyeballable.

Not a test — a helper for manual inspection. Writes reports_output/frame_matching_inspect.{html,pdf}
using the same fixture as tests/test_frame_matching.py, whose synthetic video burns the elapsed
second into each frame, so the still under each heading can be read off the page.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask

from crew.structured_pipeline import build_matched_sections, reconcile_final_report_with_matched
from tests.test_frame_matching import (
    RT,
    _marks_in_order,
    _template,
    _transcript,
    _writer_that_swaps_sections,
)
from utils.report_pdf import (
    enrich_report_with_still_frames_resilient,
    render_report_html,
    write_pdf,
)


def main() -> int:
    logging.disable(logging.CRITICAL)
    root = Path(__file__).resolve().parent.parent
    out = root / "reports_output"
    out.mkdir(exist_ok=True)
    video = out / "demo_smoke_fixture.mp4"
    if not video.is_file():
        print("fixture video missing — run tests/test_demo_smoke.py first")
        return 1

    tpl = _template()
    marks = _marks_in_order()
    matched = build_matched_sections(_transcript(), [tpl], mark_events=marks, step_events=None)
    final = reconcile_final_report_with_matched(_writer_that_swaps_sections(tpl), matched, [tpl])
    result = enrich_report_with_still_frames_resilient(
        final, video, out / "frame_match_frames", mark_times=[m["t_sec"] for m in marks]
    )
    final = result.final

    for bundle in final.reports.values():
        for sec in bundle.sections:
            imgs = [
                (Path(f.image_path).name, f.image_timestamp_sec)
                for f in (sec.frames or [])
                if f.image_uri
            ]
            print(f"  {sec.section_id:11s} -> {imgs if imgs else 'no still [unverified]'}")

    app = Flask(__name__, template_folder=str(root / "templates"))
    html = render_report_html(app, final, templates_by_type={RT: tpl})
    (out / "frame_matching_inspect.html").write_text(html, encoding="utf-8")
    write_pdf(app, html, base_dir=root, output_path=out / "frame_matching_inspect.pdf")
    print("PDF bytes:", (out / "frame_matching_inspect.pdf").stat().st_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
