"""
pipeline_errors.py — User-facing error messages for report generation.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base pipeline failure with a safe user message."""

    def __init__(self, message: str, *, user_message: str | None = None, step: str | None = None):
        super().__init__(message)
        self.user_message = user_message or message
        self.step = step


class PipelineTimeoutError(PipelineError):
    pass


class TranscriptionError(PipelineError):
    pass


class CrewExecutionError(PipelineError):
    pass


class ReportValidationError(PipelineError):
    pass


# Operator-safe messages keyed by failure class (field use)
AUDIO_TOO_QUIET = (
    "Audio too quiet — please try again. Hold the phone closer, reduce wind, "
    "and speak clearly after each “Mark this.” cue."
)
NETWORK_TRANSCRIPTION = (
    "Speech-to-text could not reach the server. Check mobile data or Wi‑Fi and tap Retry."
)
CREW_BUSY = (
    "AI processing is temporarily unavailable. Your draft report was saved from the recording structure — "
    "you can still generate a PDF or retry analysis."
)


def friendly_error_message(exc: BaseException) -> str:
    """Map exceptions to short, operator-safe text (no stack traces)."""
    if isinstance(exc, PipelineError):
        return exc.user_message

    text = str(exc).lower()
    if ("openai" in text or "anthropic" in text) and ("key" in text or "auth" in text):
        return (
            "AI reasoning is unavailable. Set OPENAI_API_KEY in .env and confirm billing/access."
        )
    if "deepgram" in text or "speech-to-text" in text or "asr" in text:
        return NETWORK_TRANSCRIPTION
    if "no audio" in text or "audio track" in text:
        return "This video has no usable audio track. Re-record with the microphone enabled."
    if "no segments" in text or "no speech" in text:
        return AUDIO_TOO_QUIET
    if "too quiet" in text or "little speech" in text:
        return AUDIO_TOO_QUIET
    if "timeout" in text or "timed out" in text:
        return "Processing took too long. Try a shorter recording or retry when signal is stronger."
    if "weasyprint" in text or "pdf" in text:
        return f"PDF export had a problem: {exc}. Text report data is still saved — retry PDF or see README."
    if "frame" in text and "extract" in text:
        return (
            "Some still images could not be extracted. The PDF may omit photos for those sections — "
            "install ffmpeg and verify the video plays locally."
        )
    if "transcript analyzer" in text or "finalreport" in text or "crew" in text:
        return CREW_BUSY
    return f"Something went wrong: {exc}. A partial report may still be available — check the review screen."
