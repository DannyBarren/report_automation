"""
jobdoc_crew_v2.py — Narration-only four-stage CrewAI pipeline (production).
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any, Callable

from crewai import Crew, Process

from crew.agents_v2 import build_v2_agents
from crew.models_v2 import (
    LOW_CONFIDENCE_THRESHOLD,
    DomainExpertAnalysisOutput,
    PdfFrameTimestamp,
    PipelineResultV2,
    QAReviewOutput,
    TranscriptAnalysisOutput,
    resolve_domain_expert,
)
from crew.structured_pipeline import (
    attach_template_titles,
    build_final_report_output,
    build_matched_sections,
    reconcile_final_report_with_matched,
    reconcile_section_frames,
)
from crew.tasks_v2 import build_v2_tasks
from utils.crew_llm import get_shared_agent_llm
from crew.report_resilience import (
    assess_transcript_quality,
    build_pipeline_result_fallback,
    build_structured_fallback_report,
    empty_transcript_payload,
    stub_domain_analysis,
    stub_qa_review,
    stub_transcript_analysis,
)
from utils.pipeline_config import (
    crew_max_retries,
    crew_timeout_seconds,
    crew_verbose,
    transcription_max_retries,
    transcription_timeout_seconds,
)
from utils.pipeline_errors import (
    AUDIO_TOO_QUIET,
    CrewExecutionError,
    PipelineError,
    PipelineTimeoutError,
    TranscriptionError,
)
from utils.schemas import FinalReportOutput, GuidancePlan, ReportTemplate, TranscriptOutput
from utils.template_guidance import (
    build_guidance_plan as _build_guidance_plan,
    enrich_guidance_plan_with_agent,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, str], None]

# User-facing progress step ids (Flask /status)
STAGE_TRANSCRIPTION = "transcription"
STAGE_ANALYZE = "analyze"
STAGE_DOMAIN = "domain"
STAGE_WRITING = "writing"
STAGE_QA = "qa"


def build_guidance_plan(report_templates: list[dict[str, Any]] | dict[str, Any] | str) -> GuidancePlan:
    """
    Structured guided-capture steps for Flask recording UI.

    Built from template JSON (order, ids, fields). Optionally polished by the
    guidance agent when ``JOBDOC_GUIDANCE_AGENT=1``.
    """
    templates = _parse_templates(report_templates)
    plan = _build_guidance_plan(templates)
    return enrich_guidance_plan_with_agent(plan, templates)


def _parse_templates(report_templates: list[dict[str, Any]] | dict[str, Any] | str) -> list[ReportTemplate]:
    if isinstance(report_templates, str):
        payload = json.loads(report_templates)
    elif isinstance(report_templates, dict):
        payload = [report_templates]
    else:
        payload = report_templates
    return [ReportTemplate.model_validate(row) for row in payload]


def _parse_frame_timestamps(
    frame_timestamps: list[dict[str, Any]] | list[PdfFrameTimestamp] | None,
) -> list[PdfFrameTimestamp]:
    if not frame_timestamps:
        return []
    out: list[PdfFrameTimestamp] = []
    for row in frame_timestamps:
        if isinstance(row, PdfFrameTimestamp):
            out.append(row)
        else:
            out.append(PdfFrameTimestamp.model_validate(row))
    return out


def _frame_timestamps_from_transcript(
    transcript: dict[str, Any],
    templates: list[ReportTemplate],
) -> list[PdfFrameTimestamp]:
    matched = build_matched_sections(transcript, templates)
    return [
        PdfFrameTimestamp(
            section_id=m.section_id,
            report_type=m.report_type,
            timestamp_sec=m.timestamp_sec,
            image_timestamp_sec=m.image_timestamp_sec,
        )
        for m in matched
    ]


def _frame_timestamps_from_marks(
    mark_events: list[dict[str, Any]] | None,
) -> list[PdfFrameTimestamp]:
    """
    Seed frame timestamps from the recorder's explicit "MARK THIS SECTION" taps.

    Used as a production reliability fallback: when spoken "Mark this." detection finds no
    cues (quiet audio, accent, background noise), the user's manual marks still drive
    still-frame extraction at the exact moments they captured.
    """
    out: list[PdfFrameTimestamp] = []
    for ev in mark_events or []:
        try:
            t = float(ev.get("t_sec", 0.0))
        except (TypeError, ValueError):
            continue
        out.append(
            PdfFrameTimestamp(
                section_id=str(ev.get("section_id") or "") or None,
                report_type=str(ev.get("report_type") or "") or None,
                timestamp_sec=max(0.0, t),
                image_timestamp_sec=max(0.0, t),
            )
        )
    return out


def _frame_timestamps_from_matched(matched: list[Any]) -> list[PdfFrameTimestamp]:
    """Derive PDF frame timestamps from already-built MatchedSection rows.

    Filler ("none") sections carry no real evidence — emitting a timestamp for them would
    extract a misleading still (typically frame 0.0s). They are skipped so only sections with
    an actual mark / cue / step window contribute a frame.
    """
    out: list[PdfFrameTimestamp] = []
    for m in matched:
        if getattr(m, "trigger_phrase", "") == "none" and not getattr(m, "frames", None):
            continue
        out.append(
            PdfFrameTimestamp(
                section_id=m.section_id,
                report_type=m.report_type,
                timestamp_sec=m.timestamp_sec,
                image_timestamp_sec=m.image_timestamp_sec,
            )
        )
    return out


def _log_frame_extraction_plan(matched: list[Any], *, path: str) -> None:
    """Log which report path was used and the exact timestamps each section will extract at.

    Makes "wrong image / everything at 0.0s" trivially diagnosable from logs: you can see the
    per-section frame count and the real anchor timestamps that the still extractor will use.
    """
    plan: dict[str, Any] = {}
    for m in matched:
        frames = list(getattr(m, "frames", []) or [])
        if frames:
            stamps = [round(float(getattr(f, "image_timestamp_sec", 0.0)), 1) for f in frames]
        elif getattr(m, "trigger_phrase", "") != "none":
            stamps = [round(float(getattr(m, "image_timestamp_sec", 0.0)), 1)]
        else:
            stamps = []  # filler → no image extracted
        plan[getattr(m, "section_id", "?")] = {
            "evidence": getattr(m, "trigger_phrase", ""),
            "frames": len(stamps),
            "extract_at_sec": stamps,
        }
    total = sum(v["frames"] for v in plan.values())
    logger.info(
        "[v2] PATH=%s | frame plan: %d total still(s) across %d section(s) | %s",
        path, total, len(plan), plan,
    )


def _section_narration_digest(matched: list[Any]) -> str:
    """Compact per-section narration captured deterministically (marks + step windows).

    Gives the report writer the ACTUAL spoken content mapped to each section — including
    narration recovered from step-event windows when no spoken "Mark this." cue was detected —
    so it writes from what the inspector really said instead of falling back to generic
    template guidance.
    """
    rows: list[dict[str, Any]] = []
    for m in matched:
        narration = (getattr(m, "segment_text_raw", "") or "").strip()
        frames = list(getattr(m, "frames", []) or [])
        # Per-frame evidence so the writer knows how many distinct stills/marks a section holds
        # and can address each one instead of assuming a single photo.
        frame_rows = [
            {
                "n": i + 1,
                "t_sec": round(float(getattr(fr, "image_timestamp_sec", 0.0)), 1),
                "source": getattr(fr, "source", ""),
                "note": (getattr(fr, "note", "") or "").strip()[:200],
                # Rich 5s-before/10s-after context so the writer describes each photo from what
                # was actually said around it (falls back to the nearest-sentence snippet).
                "context": (
                    (getattr(fr, "context_narration", "") or getattr(fr, "narration", "") or "").strip()[:500]
                ),
            }
            for i, fr in enumerate(frames)
        ]
        rows.append(
            {
                "report_type": getattr(m, "report_type", ""),
                "section_id": getattr(m, "section_id", ""),
                "section_name": getattr(m, "section_name", ""),
                "evidence": getattr(m, "trigger_phrase", ""),
                "captured_narration": (narration[:900] + "…") if len(narration) > 900 else narration,
                "has_narration": bool(narration),
                "evidence_frame_count": len(frame_rows),
                "evidence_frames": frame_rows,
            }
        )
    return json.dumps(rows, indent=2, ensure_ascii=False)


_BANNED_WRITER_PHRASES = (
    "the speaker reports",
    "the speaker stated",
    "the narration says",
    "based on the narration",
    "this summary reflects only the narration",
    "no explicit mark this cue",
    "no mark this cue",
)

_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "along",
        "areas",
        "around",
        "based",
        "being",
        "could",
        "during",
        "found",
        "front",
        "their",
        "there",
        "these",
        "thing",
        "those",
        "under",
        "where",
        "which",
        "while",
        "would",
        "narration",
        "section",
        "report",
        "should",
    }
)


def _content_tokens(text: str) -> set[str]:
    """Lowercase content words (len >= 5, not stopwords) used to measure evidence grounding."""
    return {
        w
        for w in re.findall(r"[a-zA-Z]{5,}", (text or "").lower())
        if w not in _STOPWORDS
    }


def _audit_evidence_grounding(final_report: Any, matched: list[Any]) -> None:
    """Log, per section, whether the written summary is grounded in the captured evidence.

    Verifies the writer actually used real narration/frame content (keyword overlap) rather than
    generic template prose, and flags any banned pipeline-filler phrases that slipped through.
    """
    ev_by_id: dict[str, dict[str, Any]] = {}
    for m in matched:
        sid = getattr(m, "section_id", "")
        if not sid:
            continue
        narration = (getattr(m, "segment_text_raw", "") or "").strip()
        ev_by_id[sid] = {
            "tokens": _content_tokens(narration),
            "has_narration": bool(narration),
            "frames": len(list(getattr(m, "frames", []) or [])),
            "evidence": getattr(m, "trigger_phrase", ""),
        }

    grounded = generic = flagged = total = 0
    for bundle in getattr(final_report, "reports", {}).values():
        for sec in getattr(bundle, "sections", []) or []:
            total += 1
            sid = getattr(sec, "section_id", "")
            summary = getattr(sec, "summary", "") or ""
            info = ev_by_id.get(sid, {})
            src_tokens = info.get("tokens", set())
            out_tokens = _content_tokens(summary)

            banned = [p for p in _BANNED_WRITER_PHRASES if p in summary.lower()]
            if banned:
                flagged += 1
                logger.warning(
                    "[v2][audit] section '%s' contains banned filler %s — check writer prompt.",
                    sid,
                    banned,
                )

            if info.get("has_narration") and src_tokens:
                overlap = len(src_tokens & out_tokens) / max(1, len(src_tokens))
                if overlap >= 0.15:
                    grounded += 1
                else:
                    generic += 1
                    logger.info(
                        "[v2][audit] section '%s' has narration (%d frame(s)) but only %.0f%% "
                        "keyword overlap — summary may be too generic.",
                        sid,
                        info.get("frames", 0),
                        overlap * 100,
                    )

    logger.info(
        "[v2][audit] evidence grounding: %d/%d narrated sections grounded, %d generic, "
        "%d with banned filler (of %d sections).",
        grounded,
        grounded + generic,
        generic,
        flagged,
        total,
    )


def _normalize_mark_events_for_prompt(
    mark_events: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Compact, ordered mark list (timestamp + step + note) for the analyzer prompt."""
    rows: list[dict[str, Any]] = []
    for ev in mark_events or []:
        if not isinstance(ev, dict):
            continue
        try:
            t = float(ev.get("t_sec", ev.get("time", 0.0)))
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "t_sec": round(max(0.0, t), 2),
                "section_id": str(ev.get("section_id") or ""),
                "title": str(ev.get("title") or ""),
                "note": str(ev.get("note") or ""),
            }
        )
    rows.sort(key=lambda r: r["t_sec"])
    return rows


def _task_output_pydantic(task: Any) -> Any | None:
    for attr in ("output", "result"):
        container = getattr(task, attr, None)
        if container is None:
            continue
        pydantic = getattr(container, "pydantic", None)
        if pydantic is not None:
            return pydantic
        raw = getattr(container, "raw", None)
        if raw:
            try:
                return json.loads(str(raw))
            except json.JSONDecodeError:
                pass
    return None


def _crew_boundary_safe_inputs(crew: Crew) -> dict[str, str]:
    """Provide a value for EVERY ``{word}`` token still present in the crew's prompts.

    Final defense against CrewAI's "Template variable '<x>' not found in inputs dictionary":
    whatever brace tokens survive in any task description/expected_output or agent
    role/goal/backstory (e.g. a stray ``{page}`` from injected template JSON), we hand CrewAI a
    harmless value for each so interpolation resolves instead of aborting the crew. The value is
    the readable ``[word]`` form — matching how the neutralizer would have rendered it.
    """
    from utils.prompt_utils import find_crew_brace_tokens

    tokens: set[str] = set()
    for task in getattr(crew, "tasks", []) or []:
        for attr in ("description", "expected_output"):
            tokens.update(find_crew_brace_tokens(str(getattr(task, attr, "") or "")))
    for agent in getattr(crew, "agents", []) or []:
        for attr in ("role", "goal", "backstory"):
            tokens.update(find_crew_brace_tokens(str(getattr(agent, attr, "") or "")))
    return {t: f"[{t}]" for t in tokens}


def _kickoff_crew(crew: Crew, *, timeout_sec: int, stage: str) -> None:
    verbose = crew_verbose()

    # Compute a safe inputs dict up-front. If any brace token slipped through neutralization,
    # this guarantees CrewAI can resolve it rather than raising. Log it loudly for diagnostics.
    safe_inputs = _crew_boundary_safe_inputs(crew)
    if safe_inputs:
        logger.warning(
            "[v2] stage=%s: %d brace token(s) survived into CrewAI prompts — supplying safe "
            "values so interpolation cannot abort the crew: %s",
            stage, len(safe_inputs), sorted(safe_inputs),
        )

    def _run() -> None:
        try:
            crew.kickoff(inputs=safe_inputs)
        except TypeError:
            # Very old CrewAI whose kickoff() takes no inputs — interpolation is skipped there.
            crew.kickoff()

    logger.info("Crew kickoff stage=%s timeout=%ss", stage, timeout_sec)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run)
        try:
            future.result(timeout=timeout_sec)
        except FuturesTimeoutError as exc:
            raise PipelineTimeoutError(
                f"Crew stage '{stage}' exceeded {timeout_sec}s",
                user_message=(
                    f"The '{stage}' step took too long ({timeout_sec}s limit). "
                    "A structured draft will be used so you can still get a PDF."
                ),
                step=stage,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            # If this is still a template-variable interpolation error, log the EXACT offending
            # string(s) so we can see precisely which prompt/token leaked through.
            msg = str(exc)
            if "not found in inputs" in msg or "Template variable" in msg or "Missing required" in msg:
                from utils.prompt_utils import find_crew_brace_tokens

                for task in getattr(crew, "tasks", []) or []:
                    desc = str(getattr(task, "description", "") or "")
                    toks = find_crew_brace_tokens(desc)
                    if toks:
                        logger.error(
                            "[v2] stage=%s interpolation error — task still contains tokens %s. "
                            "Offending description (first 500 chars): %r",
                            stage, sorted(set(toks)), desc[:500],
                        )
            raise CrewExecutionError(
                str(exc),
                user_message=f"AI step '{stage}' did not finish — continuing with a structured draft.",
                step=stage,
            ) from exc


def _kickoff_crew_with_retry(crew: Crew, *, timeout_sec: int, stage: str) -> None:
    """Run crew kickoff with bounded retries (transient API/network failures)."""
    import time

    retries = crew_max_retries()
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            _kickoff_crew(crew, timeout_sec=timeout_sec, stage=stage)
            return
        except (PipelineTimeoutError, CrewExecutionError) as exc:
            last_exc = exc
            if attempt >= retries:
                raise
            delay = min(8.0, 1.5 * (attempt + 1))
            logger.warning(
                "Crew stage=%s attempt %s failed (%s); retry in %.1fs",
                stage,
                attempt + 1,
                exc,
                delay,
            )
            time.sleep(delay)
    if last_exc:
        raise last_exc


def _coerce_analysis(raw: Any) -> TranscriptAnalysisOutput:
    if isinstance(raw, TranscriptAnalysisOutput):
        return raw
    if isinstance(raw, dict):
        return TranscriptAnalysisOutput.model_validate(raw)
    raise CrewExecutionError(
        "Transcript analyzer returned invalid output",
        user_message="Could not parse transcript analysis. Please retry.",
        step=STAGE_ANALYZE,
    )


def _coerce_domain(raw: Any, report_type: str, template: ReportTemplate | None) -> DomainExpertAnalysisOutput:
    if isinstance(raw, DomainExpertAnalysisOutput):
        return raw
    if isinstance(raw, dict):
        return DomainExpertAnalysisOutput.model_validate(raw)
    if raw is not None and hasattr(raw, "model_dump"):
        return DomainExpertAnalysisOutput.model_validate(raw.model_dump())
    role, standards = resolve_domain_expert(report_type, template)
    return DomainExpertAnalysisOutput(
        report_type=report_type,
        domain_role=role,
        standards_context=standards,
        expert_notes="Domain expert output missing — writer used transcript analysis only.",
    )


def _coerce_report(raw: Any) -> FinalReportOutput:
    if isinstance(raw, FinalReportOutput):
        return raw
    if isinstance(raw, dict):
        return FinalReportOutput.model_validate(raw)
    raise CrewExecutionError(
        "Report writer returned invalid output",
        user_message="Could not build the structured report. Please retry.",
        step=STAGE_WRITING,
    )


def _coerce_qa(raw: Any) -> QAReviewOutput:
    if isinstance(raw, QAReviewOutput):
        return raw
    if isinstance(raw, dict):
        return QAReviewOutput.model_validate(raw)
    if raw is not None and hasattr(raw, "model_dump"):
        return QAReviewOutput.model_validate(raw.model_dump())
    return QAReviewOutput(
        passed=False,
        overall_score=65,
        reviewer_notes="QA output not parsed; using writer draft — manual review recommended.",
    )


# Stage-abort strings that describe an *internal* Crew failure. Once a structured report has
# actually been built from the inspector's marks + narration, these are noise — they read like
# "your report failed" when the PDF is complete. Matched case-insensitively on a substring.
_INTERNAL_STAGE_NOISE = (
    "could not parse transcript analysis",
    "could not build the structured report",
    "did not finish — continuing with a structured draft",
    "took too long",
    "a structured draft will be used",
    "could not start",
)

# The single line that replaces them.
_FALLBACK_USED_NOTE = (
    "Polished AI writer did not finish — this PDF uses your spoken findings and MARK timestamps."
)


def collect_v2_warnings(result: PipelineResultV2) -> list[str]:
    """Human-readable pipeline notes for the UI.

    A degraded run that still produced a real report is NOT a failed job: the deterministic
    matcher built the PDF from the spoken findings and MARK times. So the per-stage Crew abort
    messages collapse into one plain-language note, and only the notes the inspector can act on
    (missed cues, unclear audio) are kept.
    """
    report_built = any(
        bundle.sections for bundle in (result.final_report.reports or {}).values()
    )
    fallback_used = bool(result.degraded and report_built)

    notes: list[str] = []
    for note in result.stage_failures or []:
        lowered = note.lower()
        if report_built and any(key in lowered for key in _INTERNAL_STAGE_NOISE):
            continue  # internal stage detail — the fallback note below covers it
        notes.append(note)

    if fallback_used:
        if _FALLBACK_USED_NOTE not in notes:
            notes.insert(0, _FALLBACK_USED_NOTE)
    elif result.degraded:
        notes.append(
            "This report used backup processing for one or more steps — review flagged sections before sharing."
        )
    ta = result.transcript_analysis
    if ta.missing_cues:
        notes.append(
            f"{ta.missing_cues} section(s) had no “Mark this.” cue — those areas are marked unverified in the PDF."
        )
    if ta.unmatched_cues:
        notes.append(f"{ta.unmatched_cues} extra “Mark this.” cue(s) were not mapped to a template section.")
    if ta.low_confidence_count:
        notes.append(
            f"{ta.low_confidence_count} section(s) have unclear audio (below {LOW_CONFIDENCE_THRESHOLD:.0%} confidence) — "
            "verify on site or re-record those segments."
        )
    # A stub QA score from a stage that never ran is not a real review result — printing it
    # next to the fallback note just reads as a second failure.
    if not result.qa_review.passed and not fallback_used:
        notes.append(
            f"Quality review score {result.qa_review.overall_score}/100 — "
            f"{result.qa_review.revision_summary or 'see reviewer notes'}."
        )
    if result.revision_applied:
        notes.append("Quality reviewer applied one automatic revision pass.")
    if result.qa_review.low_confidence_sections:
        notes.append(
            "QA flagged unclear narration: "
            + ", ".join(result.qa_review.low_confidence_sections[:5])
        )
    return notes


def build_low_confidence_user_messages(result: PipelineResultV2) -> list[str]:
    """Short banners for the review screen (low-confidence / verification)."""
    msgs: list[str] = []
    for obs in result.transcript_analysis.observations:
        if obs.confidence >= LOW_CONFIDENCE_THRESHOLD:
            continue
        label = obs.section_id or f"cue {obs.cue_index + 1}"
        if obs.confidence < 0.5:
            msgs.append(
                f"“{label}”: audio was very hard to understand — site verification strongly recommended."
            )
        else:
            msgs.append(
                f"“{label}”: narration was partially unclear — confirm findings on site."
            )
    return msgs[:8]


def generate_report(
    transcript: dict[str, Any],
    frame_timestamps: list[dict[str, Any]] | list[PdfFrameTimestamp] | None,
    report_templates: list[dict[str, Any]] | dict[str, Any] | str,
    *,
    video_stem: str = "",
    video_filename: str = "",
    verbose: bool | None = None,
    on_progress: ProgressCallback | None = None,
    mark_events: list[dict[str, Any]] | None = None,
    step_events: list[dict[str, Any]] | None = None,
) -> PipelineResultV2:
    """Run analyze → domain → write → QA; never aborts — uses structured fallbacks per stage."""
    verbose = crew_verbose() if verbose is None else verbose
    timeout_sec = crew_timeout_seconds()
    stage_failures: list[str] = []
    degraded = False

    def notify(step: str, detail: str) -> None:
        logger.info("[%s] %s", step, detail)
        if on_progress:
            on_progress(step, detail)

    templates = _parse_templates(report_templates)
    if not templates:
        stage_failures.append("No report templates loaded — using empty placeholder.")
        degraded = True
        transcript = TranscriptOutput.model_validate(transcript or {}).model_dump()
        return build_pipeline_result_fallback(
            transcript,
            templates or [],
            video_stem=video_stem,
            video_filename=video_filename,
            stage_notes=stage_failures,
        )

    transcript = TranscriptOutput.model_validate(transcript).model_dump()
    quality_note = assess_transcript_quality(transcript)
    if quality_note:
        stage_failures.append(quality_note)
        degraded = True

    primary_type = templates[0].report_type

    # Authoritative deterministic match (spoken cues + manual MARK taps + continuous-recording
    # step boundaries). Used both to seed PDF frame timestamps and to reconcile the LLM
    # writer's per-section frame timings.
    matched = build_matched_sections(
        transcript, templates, mark_events=mark_events, step_events=step_events
    )

    # Diagnostics: make it obvious in logs what evidence the report is being built from, so a
    # "sparse PDF" can be traced to no transcript / no marks / no cues rather than a silent bug.
    _seg_count = len(transcript.get("segments") or [])
    _spoken_cues = sum(
        1 for m in matched if getattr(m, "trigger_phrase", "") == "Mark this."
    )
    _manual = sum(1 for m in matched if getattr(m, "trigger_phrase", "") == "manual_mark")
    _step_only = sum(1 for m in matched if getattr(m, "trigger_phrase", "") == "step_boundary")
    _filler = sum(1 for m in matched if getattr(m, "trigger_phrase", "") == "none")
    logger.info(
        "[v2] evidence: transcript_segments=%d mark_taps=%d step_events=%d | sections: "
        "spoken_cue=%d manual_mark=%d step_window=%d empty=%d",
        _seg_count, len(mark_events or []), len(step_events or []),
        _spoken_cues, _manual, _step_only, _filler,
    )

    pdf_frames = _parse_frame_timestamps(frame_timestamps)
    if not pdf_frames:
        pdf_frames = _frame_timestamps_from_matched(matched)
    if not pdf_frames and mark_events:
        pdf_frames = _frame_timestamps_from_marks(mark_events)

    transcript_json = json.dumps(transcript, indent=2, ensure_ascii=False)
    mark_events_json = json.dumps(
        _normalize_mark_events_for_prompt(mark_events), indent=2, ensure_ascii=False
    )
    # Deterministic per-section narration (from marks + step windows) — the writer's primary
    # source of what was actually said in each section.
    section_narration_json = _section_narration_digest(matched)

    # Build the CrewAI agents + tasks. This is the ONE place a prompt-interpolation error
    # (e.g. a stray "{page}" token in the injected template JSON) can raise BEFORE the guarded
    # per-stage kickoff loop. If construction fails we must NOT let it escape to the caller,
    # because the outer catch would rebuild the report with an empty transcript and no
    # marks/steps — the exact failure that made every section read "not reached" with a 0.0s
    # frame. Instead we fall back to the deterministic report built from the authoritative
    # `matched` sections, which preserves the inspector's marks, step windows, and frames.
    try:
        llm = get_shared_agent_llm()
        logger.info("[v2] LLM reasoning model=%s (OpenAI key present) — running crew stages", llm)
        agents = build_v2_agents(templates, llm=llm, verbose=verbose)
        _task_list, task_map = build_v2_tasks(
            agents,
            templates=templates,
            transcript_json=transcript_json,
            video_stem=video_stem,
            video_filename=video_filename,
            report_type=primary_type,
            mark_events_json=mark_events_json,
            section_narration_json=section_narration_json,
        )
    except Exception as exc:  # noqa: BLE001 — crew construction must never strand the report
        logger.exception(
            "[v2] PATH=deterministic — crew construction failed (%s); building report from "
            "%d matched section(s) with marks/steps preserved.",
            type(exc).__name__, len(matched),
        )
        stage_failures.append(
            "AI writer could not start — produced a structured report from your marks and narration."
        )
        final_report = attach_template_titles(
            build_final_report_output(
                matched, video_stem=video_stem, video_filename=video_filename
            ),
            templates,
        )
        if video_stem:
            final_report = final_report.model_copy(update={"video_stem": video_stem})
        if video_filename:
            final_report = final_report.model_copy(update={"video_filename": video_filename})
        final_report = reconcile_section_frames(final_report, matched)
        try:
            _audit_evidence_grounding(final_report, matched)
        except Exception:  # pragma: no cover
            logger.debug("[v2][audit] skipped", exc_info=True)
        _log_frame_extraction_plan(matched, path="deterministic")
        return PipelineResultV2(
            transcript_analysis=stub_transcript_analysis(
                transcript, templates, mark_events=mark_events, step_events=step_events
            ),
            domain_analysis=stub_domain_analysis(primary_type, templates[0]),
            final_report=final_report,
            qa_review=stub_qa_review(
                passed=False, score=55,
                notes="Structured report from marks/narration (AI writer unavailable).",
            ),
            revision_applied=False,
            frame_timestamps=pdf_frames,
            degraded=True,
            stage_failures=stage_failures,
        )

    stages = [
        (STAGE_ANALYZE, "Analyzing transcript…", task_map["analyze"]),
        (STAGE_DOMAIN, "Applying domain expertise…", task_map["domain"]),
        (STAGE_WRITING, "Writing professional report…", task_map["write"]),
        (STAGE_QA, "Quality review…", task_map["qa"]),
    ]

    for step_id, label, task in stages:
        notify(step_id, label)
        crew = Crew(
            agents=[task.agent],
            tasks=[task],
            process=Process.sequential,
            verbose=verbose,
        )
        try:
            _kickoff_crew_with_retry(crew, timeout_sec=timeout_sec, stage=step_id)
        except PipelineError as exc:
            logger.warning("Stage %s failed: %s", step_id, exc)
            stage_failures.append(exc.user_message)
            degraded = True

    notify(STAGE_ANALYZE, "Parsing transcript analysis…")
    try:
        transcript_analysis = _coerce_analysis(_task_output_pydantic(task_map["analyze"]))
    except CrewExecutionError as exc:
        stage_failures.append(exc.user_message)
        degraded = True
        transcript_analysis = stub_transcript_analysis(
            transcript, templates, mark_events=mark_events, step_events=step_events
        )

    notify(STAGE_DOMAIN, "Parsing domain findings…")
    domain_analysis = _coerce_domain(
        _task_output_pydantic(task_map["domain"]),
        primary_type,
        templates[0],
    )
    if domain_analysis.expert_notes and "missing" in domain_analysis.expert_notes.lower():
        degraded = True

    notify(STAGE_WRITING, "Parsing report document…")
    try:
        final_report = _coerce_report(_task_output_pydantic(task_map["write"]))
    except CrewExecutionError as exc:
        stage_failures.append(exc.user_message)
        degraded = True
        # Build the fallback report from the ALREADY-computed authoritative match so the
        # inspector's MARK taps + step-window narration are preserved even when the LLM writer
        # stage fails. (Recomputing without marks/steps here is what previously reduced degraded
        # reports to "only the first cue" with generic per-section text.)
        logger.warning(
            "[v2] writer stage unusable — building structured report from %d matched section(s) "
            "(marks/steps preserved).",
            len(matched),
        )
        final_report = attach_template_titles(
            build_final_report_output(
                matched, video_stem=video_stem, video_filename=video_filename
            ),
            templates,
        )

    final_report = attach_template_titles(final_report, templates)
    if video_stem:
        final_report = final_report.model_copy(update={"video_stem": video_stem})
    if video_filename:
        final_report = final_report.model_copy(update={"video_filename": video_filename})

    qa_review = _coerce_qa(_task_output_pydantic(task_map["qa"]))
    if not qa_review.passed or qa_review.overall_score < 70:
        degraded = True
    revision_applied = False
    if qa_review.requires_revision and qa_review.revised_report is not None:
        final_report = attach_template_titles(qa_review.revised_report, templates)
        revision_applied = True
        notify(STAGE_QA, "Applied quality reviewer revision (1 pass).")

    # AUTHORITATIVE RECONCILIATION — the deterministic ``matched`` sections are the single
    # source of truth. This enforces the exact section set + template order, drops any section
    # ids the writer invented, restores any it omitted, and — critically — falls back to the
    # narration-grounded structured summary whenever the writer's prose discarded/ignored the
    # real spoken content. It also grafts the deterministic timestamps + evidence frames so the
    # correct still lands on each section regardless of LLM drift.
    final_report = reconcile_final_report_with_matched(final_report, matched, templates)

    # Verify the writer grounded its prose in the captured evidence (logs only; never raises).
    try:
        _audit_evidence_grounding(final_report, matched)
    except Exception:  # pragma: no cover - diagnostics must never break the pipeline
        logger.debug("[v2][audit] evidence-grounding audit skipped", exc_info=True)

    _log_frame_extraction_plan(matched, path="llm" if not degraded else "llm+fallback")

    return PipelineResultV2(
        transcript_analysis=transcript_analysis,
        domain_analysis=domain_analysis,
        final_report=final_report,
        qa_review=qa_review,
        revision_applied=revision_applied,
        frame_timestamps=pdf_frames,
        degraded=degraded,
        stage_failures=stage_failures,
    )


def transcribe_video_file(
    video_path: str | Path,
    *,
    on_progress: ProgressCallback | None = None,
    allow_empty: bool = False,
) -> dict[str, Any]:
    """Deepgram ASR with timeout, retries, and validation."""
    import time

    from utils.runtime_config import require_asr_credentials
    from utils.transcription import transcribe_video, transcript_payload_from_segments

    def notify(detail: str) -> None:
        if on_progress:
            on_progress(STAGE_TRANSCRIPTION, detail)

    timeout_sec = transcription_timeout_seconds()
    max_retries = transcription_max_retries()
    last_exc: BaseException | None = None

    for attempt in range(max_retries + 1):
        try:
            require_asr_credentials()
        except RuntimeError as exc:
            if allow_empty:
                notify("Speech-to-text unavailable — continuing with template structure.")
                return empty_transcript_payload()
            raise TranscriptionError(
                str(exc),
                user_message=str(exc),
                step=STAGE_TRANSCRIPTION,
            ) from exc

        def _run():
            return transcribe_video(str(video_path))

        notify(
            "Transcribing audio…"
            if attempt == 0
            else f"Transcribing audio (retry {attempt + 1})…"
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run)
            try:
                segments = future.result(timeout=timeout_sec)
            except FuturesTimeoutError as exc:
                last_exc = PipelineTimeoutError(
                    f"Transcription exceeded {timeout_sec}s",
                    user_message="Speech-to-text timed out. A template-only draft will be used.",
                    step=STAGE_TRANSCRIPTION,
                )
                if attempt < max_retries:
                    time.sleep(min(6.0, 2.0 * (attempt + 1)))
                    continue
                if allow_empty:
                    return empty_transcript_payload()
                raise last_exc from exc
            except Exception as exc:  # noqa: BLE001
                last_exc = TranscriptionError(
                    str(exc),
                    user_message=(
                        "Speech-to-text failed. Check network and DEEPGRAM_API_KEY — "
                        "a template-only draft will be used if you continue."
                    ),
                    step=STAGE_TRANSCRIPTION,
                )
                if attempt < max_retries:
                    time.sleep(min(6.0, 2.0 * (attempt + 1)))
                    continue
                if allow_empty:
                    return empty_transcript_payload()
                raise last_exc from exc

        if not segments:
            last_exc = TranscriptionError(
                "No segments",
                user_message=AUDIO_TOO_QUIET,
                step=STAGE_TRANSCRIPTION,
            )
            if allow_empty:
                return empty_transcript_payload()
            raise last_exc

        return transcript_payload_from_segments(segments, engine="deepgram_nova_3")

    if allow_empty:
        return empty_transcript_payload()
    if last_exc:
        raise last_exc
    raise TranscriptionError("Transcription failed", user_message=AUDIO_TOO_QUIET, step=STAGE_TRANSCRIPTION)


def generate_report_from_video(
    video_path: str | Path,
    report_templates: list[dict[str, Any]] | str,
    *,
    verbose: bool | None = None,
    on_progress: ProgressCallback | None = None,
    transcript: dict[str, Any] | None = None,
    mark_events: list[dict[str, Any]] | None = None,
    step_events: list[dict[str, Any]] | None = None,
) -> PipelineResultV2:
    """
    Full v2 path: transcribe (unless ``transcript`` provided) → four crew stages.

    Progress callbacks use step ids: transcription, analyze, domain, writing, qa.

    ``mark_events`` (explicit recorder taps) seed frame timestamps when spoken-cue matching
    finds nothing — a production reliability path for noisy/quiet field audio.

    ``step_events`` (continuous-recording step boundaries) give the matcher accurate
    per-section time windows so narration aligns to the correct section.
    """
    video_path = Path(video_path)
    templates = _parse_templates(report_templates)

    if transcript is None:
        transcript = transcribe_video_file(video_path, on_progress=on_progress, allow_empty=True)

    # Let generate_report build the authoritative match (spoken cues + manual marks) and derive
    # frames from it. mark_events flow all the way into matching + reconciliation.
    return generate_report(
        transcript,
        None,
        [t.model_dump() for t in templates],
        video_stem=video_path.stem,
        video_filename=video_path.name,
        verbose=verbose,
        on_progress=on_progress,
        mark_events=mark_events,
        step_events=step_events,
    )
