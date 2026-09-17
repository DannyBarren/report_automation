"""
utils package — shared helpers for jobdoc-demo (video, paths, prompts, etc.).
"""

from utils.prompt_utils import load_prompt
from utils.schemas import ReportSection, ReportTemplate
from utils.video_utils import extract_audio, extract_frame

__all__ = [
    "ReportSection",
    "ReportTemplate",
    "load_prompt",
    "extract_audio",
    "extract_frame",
]
