"""
agents_v2.py — Narration-only four-agent definitions for JobDoc v2.
"""

from __future__ import annotations

from typing import Any

from crewai import Agent

from crew.models_v2 import IDENTIFICATION_PHRASE, MIN_SUMMARY_WORDS, resolve_domain_expert
from utils.crew_llm import get_shared_agent_llm
from utils.prompt_utils import load_prompt_v2
from utils.schemas import ReportTemplate


def build_transcript_analyzer_agent(*, llm: Any | None = None, verbose: bool = True) -> Agent:
    llm = llm or get_shared_agent_llm()
    return Agent(
        role="Transcript Analyzer",
        goal=(
            "Extract every Mark-this cue into structured observations with context-aware cleaning, "
            "noise tagging, and calibrated 0-1 confidence for messy field ASR."
        ),
        backstory=load_prompt_v2(
            "agent_transcript_analyzer",
            identification_phrase=IDENTIFICATION_PHRASE,
        ),
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
    )


def build_domain_expert_agent(
    *,
    report_type: str,
    template: ReportTemplate | None = None,
    llm: Any | None = None,
    verbose: bool = True,
) -> Agent:
    llm = llm or get_shared_agent_llm()
    role, standards = resolve_domain_expert(report_type, template)
    return Agent(
        role=role,
        goal=f"Interpret narrated field observations for {report_type} with {role} expertise.",
        backstory=load_prompt_v2(
            "agent_domain_expert",
            domain_role=role,
            report_type=report_type,
            standards_context=standards,
        ),
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
    )


def build_report_writer_agent(*, llm: Any | None = None, verbose: bool = True) -> Agent:
    llm = llm or get_shared_agent_llm()
    return Agent(
        role="Senior Report Writer",
        goal="Produce FinalReportOutput JSON with rich narration-based section summaries.",
        backstory=load_prompt_v2(
            "agent_report_writer",
            min_summary_words=MIN_SUMMARY_WORDS,
        ),
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
    )


def build_quality_reviewer_agent(*, llm: Any | None = None, verbose: bool = True) -> Agent:
    llm = llm or get_shared_agent_llm()
    return Agent(
        role="Quality Reviewer",
        goal="QA the report for completeness, tone, and narration fidelity; one revision max.",
        backstory=load_prompt_v2("agent_quality_reviewer"),
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
    )


def build_v2_agents(
    templates: list[ReportTemplate],
    *,
    llm: Any | None = None,
    verbose: bool = True,
) -> dict[str, Agent]:
    primary = templates[0] if templates else None
    report_type = primary.report_type if primary else "generic_report"
    llm = llm or get_shared_agent_llm()
    return {
        "transcript_analyzer": build_transcript_analyzer_agent(llm=llm, verbose=verbose),
        "domain_expert": build_domain_expert_agent(
            report_type=report_type,
            template=primary,
            llm=llm,
            verbose=verbose,
        ),
        "report_writer": build_report_writer_agent(llm=llm, verbose=verbose),
        "quality_reviewer": build_quality_reviewer_agent(llm=llm, verbose=verbose),
    }
