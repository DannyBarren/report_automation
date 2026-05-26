"""
crew.py — JobDocCrew orchestrates the sequential CrewAI pipeline using **real** APIs only.

Truth layer for PDF export
---------------------------
Deterministic builders in ``structured_pipeline.py`` compute the authoritative artifacts::

    transcript → matched_sections → FinalReportOutput → QAReport

Real speech-to-text feeds ``build_matched_sections``. Crew agents run with **live** Claude Opus 4.6
reasoning so workshop observers see genuine LLM decisions; task
prompts embed the same transcript and reference JSON produced above so outputs stay aligned.

The pipeline is API-only with no offline branches.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from crewai import Crew, Process

from crew import agents as agents_module
from crew import tasks as tasks_module
from crew.structured_pipeline import (
    TRIGGER_RE,
    attach_template_titles,
    build_final_report_output,
    build_matched_sections,
    dumps_json,
    flatten_sections_in_order,
    run_qa_checks,
)
from utils.crew_llm import get_shared_agent_llm
from utils.runtime_config import require_asr_credentials
from utils.schemas import FinalReportOutput, MatchedSection, QAReport, ReportTemplate, TranscriptOutput
from utils.transcription import transcribe_video, transcript_payload_from_segments
from utils.workflow_stub import build_workflow_json_bundle, workflow_bundle_to_pretty_json

ProgressCallback = Callable[[str, str], None]


@dataclass
class JobDocCrew:
    video_path: str | None = None
    selected_report_types: list[str] | None = None
    report_templates_json: str | None = None
    verbose: bool = True
    on_progress: ProgressCallback | None = field(default=None)

    def _notify(self, step: str, detail: str) -> None:
        if self.on_progress:
            self.on_progress(step, detail)

    def _build_transcript(self, video_path: str) -> dict[str, Any]:
        """Call Deepgram Nova-3 and validate transcript structure."""
        require_asr_credentials()
        segments = transcribe_video(video_path)
        if not segments:
            raise RuntimeError(
                "Speech-to-text returned no segments. Verify the recording has audible speech "
                "and API quotas/billing are healthy."
            )
        tr = transcript_payload_from_segments(segments, engine="deepgram_nova_3")
        tr = TranscriptOutput.model_validate(tr).model_dump()
        self._notify(
            "transcription",
            "Speech-to-text complete — detecting “Mark this.” markers and aligning sections…",
        )
        return tr

    def kickoff(self, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        inputs = inputs or {}

        video_path = str(inputs.get("video_path") or self.video_path or "")
        selected = list(inputs.get("selected_report_types") or self.selected_report_types or [])
        templates_json = str(inputs.get("report_templates_json") or self.report_templates_json or "[]")
        job_metadata_json = str(
            inputs.get("job_metadata_json") or json.dumps({"phase": "crew_kickoff", "video_path": video_path})
        )

        templates = _parse_templates(templates_json)
        video_filename = Path(video_path).name if video_path else ""
        video_stem = Path(video_path).stem if video_path else ""

        self._notify("workflow", "Planning capture workflow from templates…")
        wf_bundle = build_workflow_json_bundle(templates=templates, selected_report_types=selected)
        wf_json = workflow_bundle_to_pretty_json(wf_bundle)

        tr = self._build_transcript(video_path)
        tr_pretty = json.dumps(tr, indent=2, ensure_ascii=False)

        self._notify("matching", "Detecting “Mark this.” markers and assigning timestamps…")
        matched: list[MatchedSection] = build_matched_sections(tr, templates) if templates else []
        matched_json = dumps_json(matched)

        pipeline_warnings: list[str] = []
        segs = list(tr.get("segments") or [])
        mark_count = sum(1 for s in segs if TRIGGER_RE.search(str(s.get("text", ""))))
        n_expected = len(flatten_sections_in_order(templates)) if templates else 0
        if n_expected and mark_count == 0:
            pipeline_warnings.append(
                "No “Mark this.” phrases were found in the transcript. "
                "Sections were filled using template guidance and safe time defaults — review the raw recording."
            )
        elif n_expected and 0 < mark_count < n_expected:
            pipeline_warnings.append(
                f"Found {mark_count} “Mark this.” cue(s) for {n_expected} section(s). "
                "Extra sections use timing defaults; consider re-recording with one cue per section in order."
            )

        self._notify("writing", "Writing detailed per-section summaries (45+ words)…")
        final: FinalReportOutput = attach_template_titles(
            build_final_report_output(
                matched,
                video_stem=video_stem,
                video_filename=video_filename,
            ),
            templates,
        )
        final_json = dumps_json(final)

        self._notify("qa", "Running structured QA checks…")
        qa: QAReport = run_qa_checks(final, templates)
        qa_json = dumps_json(qa)

        # Single shared production reasoning model (Claude Opus 4.6) for all crew agents.
        shared_llm = get_shared_agent_llm()

        agent_map = agents_module.build_full_pipeline_agents(
            workflow_llm=shared_llm,
            transcriber_llm=shared_llm,
            matcher_llm=shared_llm,
            report_writer_llm=shared_llm,
            qa_llm=shared_llm,
            verbose=self.verbose,
        )

        task_list = tasks_module.build_pipeline_tasks(
            agent_map,
            report_templates_json=templates_json,
            selected_report_types=selected,
            video_path=video_path,
            job_metadata_json=job_metadata_json,
            reference_transcript_json=tr_pretty,
            reference_matched_json=matched_json,
            reference_final_report_json=final_json,
            reference_qa_json=qa_json,
        )

        self._notify("crew", "Running CrewAI sequential chain…")
        crew = Crew(
            agents=list(agent_map.values()),
            tasks=task_list,
            process=Process.sequential,
            verbose=self.verbose,
        )
        kickoff_inputs = {
            "report_templates_json": templates_json,
            "selected_report_types": ", ".join(selected),
            "video_path": video_path,
            "job_metadata": job_metadata_json,
        }
        try:
            result = crew.kickoff(inputs=kickoff_inputs)
        except TypeError:
            result = crew.kickoff()

        raw = getattr(result, "raw", None)

        return {
            "status": "completed",
            "crew_raw": raw if raw is not None else str(result),
            "planner_llm_mode": "live",
            "crew_agent_llm_mode": "live",
            "video_path": video_path,
            "selected_report_types": selected,
            "deterministic_workflow_json": wf_json,
            "transcript": tr,
            "matched_sections": [m.model_dump() for m in matched],
            "final_report": final.model_dump(),
            "qa_report": qa.model_dump(),
            "pipeline_warnings": pipeline_warnings,
        }


def _parse_templates(templates_json: str) -> list[ReportTemplate]:
    try:
        payload = json.loads(templates_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    out: list[ReportTemplate] = []
    for row in payload:
        try:
            out.append(ReportTemplate.model_validate(row))
        except Exception:  # noqa: BLE001
            continue
    return out
