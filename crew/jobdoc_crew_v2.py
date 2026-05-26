"""
jobdoc_crew_v2.py — Narration-only four-stage CrewAI pipeline (production).
"""

from __future__ import annotations

import json
import logging
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
from crew.structured_pipeline import attach_template_titles, build_matched_sections
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


def _kickoff_crew(crew: Crew, *, timeout_sec: int, stage: str) -> None:
    verbose = crew_verbose()

    def _run() -> None:
        try:
            crew.kickoff()
        except TypeError:
            crew.kickoff(inputs={})

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


def collect_v2_warnings(result: PipelineResultV2) -> list[str]:
    """Human-readable pipeline notes for the UI."""
    notes: list[str] = list(result.stage_failures or [])
    if result.degraded:
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
    if not result.qa_review.passed:
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

    pdf_frames = _parse_frame_timestamps(frame_timestamps)
    if not pdf_frames:
        pdf_frames = _frame_timestamps_from_transcript(transcript, templates)

    transcript_json = json.dumps(transcript, indent=2, ensure_ascii=False)

    llm = get_shared_agent_llm()
    agents = build_v2_agents(templates, llm=llm, verbose=verbose)
    _task_list, task_map = build_v2_tasks(
        agents,
        templates=templates,
        transcript_json=transcript_json,
        video_stem=video_stem,
        video_filename=video_filename,
        report_type=primary_type,
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
        transcript_analysis = stub_transcript_analysis(transcript, templates)

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
        final_report = build_structured_fallback_report(
            transcript,
            templates,
            video_stem=video_stem,
            video_filename=video_filename,
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
) -> PipelineResultV2:
    """
    Full v2 path: transcribe (unless ``transcript`` provided) → four crew stages.

    Progress callbacks use step ids: transcription, analyze, domain, writing, qa.
    """
    video_path = Path(video_path)
    templates = _parse_templates(report_templates)

    if transcript is None:
        transcript = transcribe_video_file(video_path, on_progress=on_progress, allow_empty=True)

    pdf_frames = _frame_timestamps_from_transcript(transcript, templates)

    return generate_report(
        transcript,
        pdf_frames,
        [t.model_dump() for t in templates],
        video_stem=video_path.stem,
        video_filename=video_path.name,
        verbose=verbose,
        on_progress=on_progress,
    )
