"""
crew package — CrewAI agents, tasks, and orchestration for jobdoc-demo.

This package is intentionally minimal in the MVP foundation step.
Wire `JobDocCrew` from `crew.crew` once API keys and tools are configured.
"""

from __future__ import annotations

__all__ = ["JobDocCrew", "generate_report"]



def __getattr__(name: str):
    """Lazy import so `from crew import JobDocCrew` works without circular import issues."""
    if name == "JobDocCrew":
        from crew.crew import JobDocCrew

        return JobDocCrew
    if name == "generate_report":
        from crew.jobdoc_crew_v2 import generate_report as generate_report_v2

        return generate_report_v2
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
