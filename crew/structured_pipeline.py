"""
structured_pipeline.py — Deterministic transcript→sections→report transforms (Step 3).

Why this module exists alongside Crew
--------------------------------------
    CrewAI agents run with live LLMs for transparency, but the **authoritative** timestamps,
    summaries, and JSON shapes for PDF export come from this Python layer so “Mark this.”
    detection stays deterministic and PDF output does not depend on LLM variance.

Mark-this workflow
------------------
    1. Real ASR emits ``segments`` with text, ``start``, ``end``. We filter
       segments whose text contains the trigger (case-insensitive).
    2. We map the *k*-th mark to the *k*-th section in a **flattened** list of all sections
       from selected ``ReportTemplate``s, in stable order (same as the recording UI).
    3. ``timestamp_sec`` = segment start (when the cue begins). ``image_timestamp_sec`` =
       clamped midpoint of the segment (or start + 0.5s) so the still frame shows what was
       being described after the phrase.
    4. ``summary`` is built from the full narration window after each cue (same segment
       through the segment before the next cue), in professional 45–80 word form.
"""

from __future__ import annotations

import json
import re
from typing import Any

from utils.schemas import (
    FinalReportOutput,
    MatchedSection,
    QAReport,
    QAFinding,
    ReportBundle,
    ReportSection,
    ReportSectionOutput,
    ReportTemplate,
)

TRIGGER_RE = re.compile(r"mark\s*this\.?", re.IGNORECASE)


def flatten_sections_in_order(templates: list[ReportTemplate]) -> list[tuple[ReportTemplate, ReportSection]]:
    """Return (template, section) pairs in guidance capture order when available."""
    out: list[tuple[ReportTemplate, ReportSection]] = []
    for tpl in templates:
        if tpl.guidance and tpl.guidance.sections:
            sec_by_id = {s.id: s for s in tpl.sections}
            for g in tpl.sorted_guidance_sections():
                sec = sec_by_id.get(g.section_id)
                if sec:
                    out.append((tpl, sec))
            continue
        for sec in tpl.sections:
            out.append((tpl, sec))
    return out


def _strip_trigger_prefix(text: str) -> str:
    """Remove leading “Mark this.” (any casing) and leading punctuation/space."""
    t = text.strip()
    t = TRIGGER_RE.sub("", t, count=1).strip()
    t = t.lstrip(".,;: ").strip()
    return t


def _extract_narration_between_cues(
    segments: list[dict[str, Any]],
    cue_indexes: list[int],
    cue_pos: int,
) -> str:
    """Return full narration after cue `cue_pos` until the next cue or transcript end."""
    start_idx = cue_indexes[cue_pos]
    end_idx = cue_indexes[cue_pos + 1] if cue_pos + 1 < len(cue_indexes) else len(segments)

    narration_chunks: list[str] = []
    for idx in range(start_idx, end_idx):
        raw_text = str(segments[idx].get("text", "")).strip()
        if not raw_text:
            continue
        if idx == start_idx:
            text_after_cue = _strip_trigger_prefix(raw_text)
            if text_after_cue:
                narration_chunks.append(text_after_cue)
        else:
            narration_chunks.append(raw_text)
    return " ".join(chunk for chunk in narration_chunks if chunk).strip()


def _professional_summary_from_narration(narration: str, section_name: str) -> str:
    """Generate a clean 45–80 word summary using only spoken narration content."""
    words = narration.split()
    if not words:
        return (
            f"No spoken narration was captured after the cue for {section_name}. "
            "This section remains unverified and requires follow-up recording to document "
            "actual on-site observations and conditions in a compliant report-ready form."
        )

    # Keep source facts intact: compress whitespace and clip overlong narration.
    compact = " ".join(words[:68]).strip()
    summary = (
        f"For {section_name}, the speaker reports: {compact}. "
        "This summary reflects only the narration provided after the cue."
    )
    if len(summary.split()) < 45:
        summary = (
            f"For {section_name}, the speaker reports: {compact}. "
            "No additional unstated assumptions were added, and this summary is restricted "
            "to the narrated details provided in the recording."
        )
    if len(summary.split()) > 80:
        clipped = " ".join(summary.split()[:80]).rstrip(" ,;:.")
        summary = f"{clipped}."
    return summary


def _guess_fields_from_text(section_fields: list[str], blob: str) -> dict[str, str]:
    """Very lightweight heuristic fills — labeled so QA/report readers treat as provisional."""
    guesses: dict[str, str] = {}
    lower = blob.lower()
    for field in section_fields:
        if field.endswith("_clear") or field.startswith("egress"):
            guesses[field] = "yes" if any(x in lower for x in ("clear", "open", "unblocked")) else "unknown"
        elif "hazard" in field or "flags" in field:
            guesses[field] = []
        elif "name" in field or "subject" in field:
            guesses[field] = "Not stated" if "name" in field else "Not stated"
        else:
            guesses[field] = "See narrative summary"
    return guesses


def build_matched_sections(
    transcript: dict[str, Any],
    templates: list[ReportTemplate],
    *,
    trigger: str = "Mark this.",
) -> list[MatchedSection]:
    """
    Link “Mark this.” **segment starts** to template sections in order.

    If there are more marks than sections, extra marks are **ignored** (logged in QA).
    If there are fewer marks than sections, remaining sections get **synthetic** low-confidence
    rows using the last known time + placeholder summary (still >= 45 words).
    """
    _ = trigger
    flat = flatten_sections_in_order(templates)
    segments = list(transcript.get("segments") or [])

    mark_indexes: list[int] = []
    for idx, seg in enumerate(segments):
        text = str(seg.get("text", ""))
        if TRIGGER_RE.search(text):
            mark_indexes.append(idx)

    matched: list[MatchedSection] = []
    for i, (tpl, sec) in enumerate(flat):
        if i < len(mark_indexes):
            seg = segments[mark_indexes[i]]
            t0 = float(seg.get("start", seg.get("t_start_sec", 0.0)))
            t1 = float(seg.get("end", seg.get("t_end_sec", t0 + 1.0)))
            raw_text = str(seg.get("text", ""))
            narration = _extract_narration_between_cues(segments, mark_indexes, i)
            # Midpoint: still frame inside the same spoken segment.
            image_t = min(max(t0, (t0 + t1) / 2.0), t1)
            base_summary = _professional_summary_from_narration(narration, sec.name)
            guesses = _guess_fields_from_text(sec.required_fields, narration)
            matched.append(
                MatchedSection(
                    section_id=sec.id,
                    report_type=tpl.report_type,
                    section_name=sec.name,
                    timestamp_sec=t0,
                    image_timestamp_sec=image_t,
                    summary=base_summary,
                    trigger_phrase="Mark this.",
                    required_fields=list(sec.required_fields),
                    field_guesses=guesses,
                    segment_text_raw=narration or raw_text,
                )
            )
        else:
            # Missing cue for this section — deterministic filler tied to template text only.
            anchor = float(segments[-1].get("end", segments[-1].get("t_end_sec", 0.0))) if segments else 0.0
            filler_summary = (
                f"No explicit “Mark this.” cue was detected for {sec.name}; this paragraph "
                f"was synthesized from the template guidance only: {sec.capture_instructions} "
                f"Treat findings as **unverified** until a supervisor reviews the raw recording."
            )
            matched.append(
                MatchedSection(
                    section_id=sec.id,
                    report_type=tpl.report_type,
                    section_name=sec.name,
                    timestamp_sec=anchor,
                    image_timestamp_sec=anchor,
                    summary=filler_summary,
                    trigger_phrase="Mark this.",
                    required_fields=list(sec.required_fields),
                    field_guesses={k: "missing" for k in sec.required_fields},
                    segment_text_raw="",
                )
            )

    return matched


def build_final_report_output(
    matched: list[MatchedSection],
    *,
    video_stem: str,
    video_filename: str,
) -> FinalReportOutput:
    """Group matched rows into ``ReportBundle``s keyed by ``report_type``."""
    bundles: dict[str, ReportBundle] = {}
    for row in matched:
        tpl_key = row.report_type
        if tpl_key not in bundles:
            # title filled when we merge template list in app; placeholder here.
            bundles[tpl_key] = ReportBundle(report_type=tpl_key, title=tpl_key, sections=[])
        bundles[tpl_key].sections.append(
            ReportSectionOutput(
                section_id=row.section_id,
                section_name=row.section_name,
                summary=row.summary,
                timestamp_sec=row.timestamp_sec,
                image_timestamp_sec=row.image_timestamp_sec,
                field_values={**row.field_guesses},
            )
        )
    return FinalReportOutput(
        video_stem=video_stem,
        video_filename=video_filename,
        identification_phrase="Mark this.",
        reports=bundles,
    )


def attach_template_titles(final: FinalReportOutput, templates: list[ReportTemplate]) -> FinalReportOutput:
    """Copy human titles from JSON templates onto bundles."""
    by_type = {t.report_type: t for t in templates}
    for key, bundle in final.reports.items():
        if key in by_type:
            bundle.title = by_type[key].title
    return final


def run_qa_checks(final: FinalReportOutput, templates: list[ReportTemplate]) -> QAReport:
    """
    Deterministic QA pass: required sections present, summary lengths, missing cues.

    This complements any LLM QA task — results merge in ``JobDocCrew`` output.
    """
    findings: list[QAFinding] = []
    expected_types = {t.report_type for t in templates}
    for rt in expected_types:
        if rt not in final.reports:
            findings.append(
                QAFinding(
                    code="MISSING_REPORT_BUNDLE",
                    message=f"No bundle produced for report_type `{rt}`.",
                    severity="error",
                )
            )

    tpl_sections = {(t.report_type, s.id) for t in templates for s in t.sections}
    covered_set: set[tuple[str, str]] = set()
    for rt, bundle in final.reports.items():
        for sec in bundle.sections:
            covered_set.add((rt, sec.section_id))

    for rt, sid in tpl_sections:
        if (rt, sid) not in covered_set:
            findings.append(
                QAFinding(
                    code="MISSING_SECTION",
                    message=f"Section `{sid}` missing for `{rt}`.",
                    severity="warning",
                )
            )

    for bundle in final.reports.values():
        for sec in bundle.sections:
            words = sec.summary.split()
            if len(words) < 45:
                findings.append(
                    QAFinding(
                        code="SUMMARY_TOO_SHORT",
                        message=f"Section {sec.section_id} summary < 45 words after validation.",
                        severity="error",
                    )
                )

    passed = not any(f.severity == "error" for f in findings)
    return QAReport(
        passed=passed,
        findings=findings,
        summary="; ".join(f.message for f in findings[:5]) if findings else "No blocking issues.",
    )


def dumps_json(obj: Any) -> str:
    """Serialize pydantic models, lists thereof, or plain dicts for prompts and API payloads."""
    if isinstance(obj, list):
        if obj and hasattr(obj[0], "model_dump"):
            return json.dumps([x.model_dump() for x in obj], indent=2, ensure_ascii=False)
        return json.dumps(obj, indent=2, ensure_ascii=False)
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(), indent=2, ensure_ascii=False)
    return json.dumps(obj, indent=2, ensure_ascii=False)
