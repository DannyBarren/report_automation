"""
report_resilience.py — Fallback reports, review items, and partial PDF prep.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from crew.models_v2 import (
    LOW_CONFIDENCE_THRESHOLD,
    DomainExpertAnalysisOutput,
    PipelineResultV2,
    QAReviewOutput,
    TranscriptAnalysisOutput,
    resolve_domain_expert,
)
from crew.structured_pipeline import attach_template_titles, build_final_report_output, build_matched_sections
from utils.schemas import FinalReportOutput, ReportSectionOutput, ReportTemplate
from utils.transcription import transcript_payload_from_segments


def empty_transcript_payload(*, reason: str = "") -> dict[str, Any]:
    return transcript_payload_from_segments([], engine="none")


def assess_transcript_quality(transcript: dict[str, Any]) -> str | None:
    """
    Return a user-friendly warning when audio/narration looks unusable, else None.
    """
    segments = list(transcript.get("segments") or [])
    if not segments:
        return (
            "Audio too quiet or no speech detected — we built a template-only draft. "
            "Re-record closer to the microphone and say “Mark this.” before each section."
        )
    total_chars = sum(len(str(s.get("text", ""))) for s in segments)
    if total_chars < 40:
        return (
            "Very little speech was captured — audio may be too quiet. "
            "Check the draft below and re-record if sections are empty."
        )
    mark_hits = sum(
        1
        for s in segments
        if "mark this" in str(s.get("text", "")).lower()
    )
    if mark_hits == 0:
        return (
            'No “Mark this.” cues were heard — sections may be incomplete. '
            "Say “Mark this.” clearly before describing each area."
        )
    return None


def build_structured_fallback_report(
    transcript: dict[str, Any],
    templates: list[ReportTemplate],
    *,
    video_stem: str = "",
    video_filename: str = "",
    mark_events: list[dict[str, Any]] | None = None,
    step_events: list[dict[str, Any]] | None = None,
) -> FinalReportOutput:
    """Deterministic report when Crew stages fail or time out.

    ``mark_events`` / ``step_events`` are forwarded so a degraded run still anchors each
    section to the inspector's MARK taps and continuous-recording step windows (not just
    spoken cues) — otherwise later sections lose their frames and narration.
    """
    matched = build_matched_sections(
        transcript, templates, mark_events=mark_events, step_events=step_events
    )
    final = build_final_report_output(
        matched,
        video_stem=video_stem,
        video_filename=video_filename,
    )
    return attach_template_titles(final, templates)


def stub_transcript_analysis(
    transcript: dict[str, Any],
    templates: list[ReportTemplate],
) -> TranscriptAnalysisOutput:
    """Minimal analysis object when the analyzer agent fails."""
    matched = build_matched_sections(transcript, templates)
    from crew.models_v2 import ConfidenceBand, StructuredObservation

    observations: list[StructuredObservation] = []
    for i, m in enumerate(matched):
        raw = (m.segment_text_raw or m.summary or "")[:500]
        observations.append(
            StructuredObservation(
                observation_id=f"fallback_{i}",
                section_id=m.section_id,
                report_type=m.report_type,
                cue_index=i,
                timestamp_sec=m.timestamp_sec,
                image_timestamp_sec=m.image_timestamp_sec,
                trigger_phrase="Mark this.",
                speaker_intent="Document section condition",
                raw_narration=raw or "No narration captured.",
                cleaned_observation=m.summary[:400],
                confidence=0.45 if not m.segment_text_raw else 0.72,
                confidence_band=ConfidenceBand.low if not m.segment_text_raw else ConfidenceBand.medium,
                requires_site_verification=not bool(m.segment_text_raw),
            )
        )
    missing = sum(1 for m in matched if not m.segment_text_raw)
    return TranscriptAnalysisOutput(
        observations=observations,
        missing_cues=missing,
        unmatched_cues=0,
        analysis_notes="Structured fallback — transcript analyzer did not complete.",
    )


def stub_domain_analysis(report_type: str, template: ReportTemplate | None) -> DomainExpertAnalysisOutput:
    role, standards = resolve_domain_expert(report_type, template)
    return DomainExpertAnalysisOutput(
        report_type=report_type,
        domain_role=role,
        standards_context=standards,
        expert_notes="Domain pass skipped or unavailable — narrative from structured matching only.",
    )


def stub_qa_review(*, passed: bool = False, score: int = 55, notes: str = "") -> QAReviewOutput:
    return QAReviewOutput(
        passed=passed,
        overall_score=score,
        reviewer_notes=notes or "Automated fallback QA — please review flagged sections.",
        requires_revision=False,
    )


def build_pipeline_result_fallback(
    transcript: dict[str, Any],
    templates: list[ReportTemplate],
    *,
    video_stem: str,
    video_filename: str,
    stage_notes: list[str],
    mark_events: list[dict[str, Any]] | None = None,
    step_events: list[dict[str, Any]] | None = None,
) -> PipelineResultV2:
    primary = templates[0].report_type if templates else "report"
    final = build_structured_fallback_report(
        transcript,
        templates,
        video_stem=video_stem,
        video_filename=video_filename,
        mark_events=mark_events,
        step_events=step_events,
    )
    return PipelineResultV2(
        transcript_analysis=stub_transcript_analysis(transcript, templates),
        domain_analysis=stub_domain_analysis(primary, templates[0] if templates else None),
        final_report=final,
        qa_review=stub_qa_review(
            passed=False,
            score=50,
            notes="; ".join(stage_notes[:3]) if stage_notes else "Degraded pipeline run.",
        ),
        revision_applied=False,
        frame_timestamps=[],
        degraded=True,
        stage_failures=stage_notes,
    )


def build_review_items(
    final: FinalReportOutput,
    transcript_analysis: TranscriptAnalysisOutput | None = None,
) -> list[dict[str, Any]]:
    """Sections for the pre-PDF review UI (flags + editable notes)."""
    conf_by_section: dict[tuple[str, str], float] = {}
    if transcript_analysis:
        for obs in transcript_analysis.observations:
            key = (str(obs.report_type or ""), str(obs.section_id or ""))
            conf_by_section[key] = min(conf_by_section.get(key, 1.0), float(obs.confidence))

    items: list[dict[str, Any]] = []
    for rt, bundle in final.reports.items():
        for sec in bundle.sections:
            key = (rt, sec.section_id)
            conf = conf_by_section.get(key)
            flags: list[str] = []
            if conf is not None and conf < LOW_CONFIDENCE_THRESHOLD:
                flags.append("low_confidence")
            fv = sec.field_values or {}
            if str(fv.get("severity", "")).lower() in ("major", "safety_critical", "high"):
                flags.append("high_severity")
            if (
                not sec.summary
                or "no explicit" in sec.summary.lower()
                or any(str(v).lower() == "missing" for v in fv.values())
            ):
                flags.append("missing_mark")
            if "unverified" in sec.summary.lower():
                flags.append("unverified")
            # High confidence = clear audio AND no blocking flags → safe to one-tap accept.
            high_confidence = (not flags) and (conf is None or conf >= LOW_CONFIDENCE_THRESHOLD)
            items.append(
                {
                    "report_type": rt,
                    "section_id": sec.section_id,
                    "section_name": sec.section_name,
                    "summary_preview": " ".join((sec.summary or "").split()[:35]),
                    "confidence": conf,
                    "flags": flags,
                    "high_confidence": high_confidence,
                    "user_note": str(fv.get("inspector_notes") or ""),
                }
            )
    return items


def merge_review_notes(
    final: FinalReportOutput,
    review_notes_json: str | list[dict[str, Any]] | None,
) -> FinalReportOutput:
    """Apply quick notes from the review screen into section field_values."""
    if not review_notes_json:
        return final
    if isinstance(review_notes_json, str):
        try:
            rows = json.loads(review_notes_json)
        except json.JSONDecodeError:
            return final
    else:
        rows = review_notes_json
    if not isinstance(rows, list):
        return final

    note_map: dict[tuple[str, str], str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        rt = str(row.get("report_type") or "")
        sid = str(row.get("section_id") or "")
        note = str(row.get("user_note") or row.get("note") or "").strip()
        if rt and sid and note:
            note_map[(rt, sid)] = note

    if not note_map:
        return final

    new_reports: dict[str, Any] = {}
    for rt, bundle in final.reports.items():
        new_sections: list[ReportSectionOutput] = []
        for sec in bundle.sections:
            note = note_map.get((rt, sec.section_id))
            if not note:
                new_sections.append(sec)
                continue
            fv = dict(sec.field_values or {})
            fv["inspector_notes"] = note
            summary = sec.summary
            if note not in summary:
                summary = f"{summary.rstrip()} Inspector note: {note}"
            new_sections.append(
                sec.model_copy(update={"field_values": fv, "summary": summary})
            )
        new_reports[rt] = bundle.model_copy(update={"sections": new_sections})
    return final.model_copy(update={"reports": new_reports})


def pipeline_result_from_draft(payload: dict[str, Any]) -> PipelineResultV2 | None:
    """Rehydrate v2 result saved in job_meta between analysis and PDF phases."""
    try:
        fr = payload.get("final_report")
        ta = payload.get("transcript_analysis")
        if not fr or not ta:
            return None
        return PipelineResultV2(
            transcript_analysis=TranscriptAnalysisOutput.model_validate(ta),
            domain_analysis=DomainExpertAnalysisOutput.model_validate(
                payload.get("domain_analysis") or stub_domain_analysis("report", None).model_dump()
            ),
            final_report=FinalReportOutput.model_validate(fr),
            qa_review=QAReviewOutput.model_validate(payload.get("qa_review") or {}),
            revision_applied=bool(payload.get("revision_applied")),
            frame_timestamps=payload.get("frame_timestamps") or [],
            degraded=bool(payload.get("degraded")),
            stage_failures=list(payload.get("stage_failures") or []),
        )
    except Exception:
        return None
