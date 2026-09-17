"""
agents.py — CrewAI ``Agent`` definitions for the guided video report pipeline.

Step 3 adds **MatcherAgent**, **ReportWriterAgent**, and **QAAgent** to the graph. Prompts
for persona live in ``prompts/agent_*.txt``; task bodies live in ``prompts/*_prompt.txt``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crewai import Agent

from utils.crew_llm import get_openai_model
from utils.prompt_utils import load_prompt

IDENTIFICATION_PHRASE = "Mark this."
# Production reasoning model (OpenAI). Resolved from OPENAI_MODEL (default gpt-4o).
REASONING_LLM = get_openai_model()


@dataclass(frozen=True)
class AgentSpec:
    key: str
    role: str
    goal: str


AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        key="workflow_planner",
        role="Workflow planner",
        goal="Convert template JSON into an ordered capture workflow with valid section ids.",
    ),
    AgentSpec(
        key="transcriber",
        role="Field transcript specialist",
        goal="Emit structured JSON transcript objects consistent with real ASR output.",
    ),
    AgentSpec(
        key="matcher",
        role="Matcher — Mark this. linker",
        goal="Map timestamped transcript cues to section_ids with 45–80 word summaries and image times.",
    ),
    AgentSpec(
        key="report_writer",
        role="Structured report writer",
        goal="Emit FinalReportOutput JSON for Jinja/WeasyPrint with no invented facts.",
    ),
    AgentSpec(
        key="qa",
        role="QA reviewer",
        goal="Emit QAReport JSON checking coverage and contradictions against templates.",
    ),
)


def build_workflow_planner_agent(*, llm: Any = REASONING_LLM, verbose: bool = True) -> Agent:
    backstory = load_prompt(
        "agent_workflow_planner",
        identification_phrase=IDENTIFICATION_PHRASE,
    )
    return Agent(
        role=AGENT_SPECS[0].role,
        goal=AGENT_SPECS[0].goal,
        backstory=backstory,
        # Workflow planning is schema-heavy, so we use the strongest reasoning model.
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
    )


def build_transcriber_agent(*, llm: Any = REASONING_LLM, verbose: bool = True) -> Agent:
    backstory = load_prompt(
        "agent_transcriber",
        identification_phrase=IDENTIFICATION_PHRASE,
    )
    return Agent(
        role=AGENT_SPECS[1].role,
        goal=AGENT_SPECS[1].goal,
        backstory=backstory,
        # This agent reformats/normalizes transcript JSON to strict schema outputs.
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
    )


def build_matcher_agent(*, llm: Any = REASONING_LLM, verbose: bool = True) -> Agent:
    """MatcherAgent — links “Mark this.” segments to template sections with timestamps."""
    backstory = load_prompt(
        "agent_matcher",
        identification_phrase=IDENTIFICATION_PHRASE,
    )
    return Agent(
        role=AGENT_SPECS[2].role,
        goal=AGENT_SPECS[2].goal,
        backstory=backstory,
        # Matching needs precise timestamp-to-section reasoning.
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
    )


def build_report_writer_agent(*, llm: Any = REASONING_LLM, verbose: bool = True) -> Agent:
    """ReportWriterAgent — produces ``FinalReportOutput`` JSON."""
    backstory = load_prompt(
        "agent_report_writer",
        identification_phrase=IDENTIFICATION_PHRASE,
    )
    return Agent(
        role=AGENT_SPECS[3].role,
        goal=AGENT_SPECS[3].goal,
        backstory=backstory,
        # Writer emits nested FinalReportOutput JSON that must validate cleanly.
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
    )


def build_qa_agent(*, llm: Any = REASONING_LLM, verbose: bool = True) -> Agent:
    """QAAgent — produces ``QAReport`` JSON."""
    backstory = load_prompt(
        "agent_qa",
        identification_phrase=IDENTIFICATION_PHRASE,
    )
    return Agent(
        role=AGENT_SPECS[4].role,
        goal=AGENT_SPECS[4].goal,
        backstory=backstory,
        # QA requires contradiction detection and schema-aware checks.
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
    )


def build_full_pipeline_agents(
    *,
    workflow_llm: Any,
    transcriber_llm: Any,
    matcher_llm: Any,
    report_writer_llm: Any,
    qa_llm: Any,
    verbose: bool = True,
) -> dict[str, Agent]:
    """Build all agents using the production OpenAI reasoning model."""
    return {
        "workflow_planner": build_workflow_planner_agent(llm=workflow_llm, verbose=verbose),
        "transcriber": build_transcriber_agent(llm=transcriber_llm, verbose=verbose),
        "matcher": build_matcher_agent(llm=matcher_llm, verbose=verbose),
        "report_writer": build_report_writer_agent(llm=report_writer_llm, verbose=verbose),
        "qa": build_qa_agent(llm=qa_llm, verbose=verbose),
    }


# Backwards compatibility with earlier milestone imports.
def build_pipeline_agents(
    *,
    workflow_llm: Any,
    transcriber_llm: Any,
    matcher_llm: Any,
    verbose: bool = True,
) -> dict[str, Agent]:
    """
    Deprecated: use ``build_full_pipeline_agents`` for the five-agent graph.

    Fills writer/qa with the matcher LLM slot to avoid import errors in old tests.
    """
    return build_full_pipeline_agents(
        workflow_llm=workflow_llm,
        transcriber_llm=transcriber_llm,
        matcher_llm=matcher_llm,
        report_writer_llm=matcher_llm,
        qa_llm=matcher_llm,
        verbose=verbose,
    )
