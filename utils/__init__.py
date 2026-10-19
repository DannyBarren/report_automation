"""
utils package — shared helpers for jobdoc-demo (video, paths, prompts, etc.).

Imports of heavy modules (Pydantic schemas, video/ffmpeg) are lazy so lightweight
helpers such as ``utils.auth_gate`` and ``utils.runtime_secrets`` can be tested
without the full pipeline stack.
"""

from __future__ import annotations

__all__ = [
    "ReportSection",
    "ReportTemplate",
    "load_prompt",
    "extract_audio",
    "extract_frame",
]


def __getattr__(name: str):
    if name in ("ReportSection", "ReportTemplate"):
        from utils.schemas import ReportSection, ReportTemplate

        return ReportSection if name == "ReportSection" else ReportTemplate
    if name == "load_prompt":
        from utils.prompt_utils import load_prompt

        return load_prompt
    if name in ("extract_audio", "extract_frame"):
        from utils.video_utils import extract_audio, extract_frame

        return extract_audio if name == "extract_audio" else extract_frame
    raise AttributeError(f"module {__name__!r} has no attribute {name}")
