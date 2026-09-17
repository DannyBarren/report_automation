"""
transcription.py — Production speech-to-text.

Default backend: **Deepgram Nova-3** (best timestamp accuracy + reliable "Mark this."
cue detection). Configuration via ``DEEPGRAM_API_KEY``.

Optional local backend: **faster-whisper**, gated behind runtime GPU detection. This is a
high-performance, lower-cost, private alternative/complement that runs on the Space's GPU
when one is selected. It is NEVER used by default — Deepgram stays primary. Select it with::

    JOBDOC_STT_BACKEND=local   # force local faster-whisper (requires GPU + package)
    JOBDOC_STT_BACKEND=auto    # use local only when GPU is present, else Deepgram
    JOBDOC_STT_BACKEND=deepgram # (default) always Deepgram

Backend selection is runtime-only — GPU is detected after the container starts.

Segment shape
-------------
Each segment returned by ``transcribe_video`` has this schema::

    {
        "start": float,
        "end": float,
        "text": str,
    }
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEEPGRAM_NOVA3_MODEL = "nova-3"


def _stt_backend() -> str:
    """Resolve the configured STT backend (runtime-only GPU awareness)."""
    raw = os.environ.get("JOBDOC_STT_BACKEND", "deepgram").strip().lower()
    if raw in ("local", "whisper", "faster-whisper"):
        return "local"
    if raw == "auto":
        try:
            from utils.gpu_utils import is_gpu_available

            return "local" if is_gpu_available() else "deepgram"
        except Exception:  # noqa: BLE001
            return "deepgram"
    return "deepgram"


def transcribe_video(video_path: str | Path) -> list[dict[str, Any]]:
    """
    Transcribe ``video_path`` and return timestamped segments.

    Uses Deepgram by default; optionally uses the local GPU Whisper backend when
    ``JOBDOC_STT_BACKEND`` requests it (with automatic fallback to Deepgram on failure).

    Raises:
        RuntimeError: No ASR provider configured or provider failure.
        FileNotFoundError: Missing file.
    """
    # Normalize to `.mp4` for best compatibility (iPhone `.mov`, Android `.webm`).
    from utils.video_utils import normalize_video_to_mp4

    path = normalize_video_to_mp4(video_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))

    backend = _stt_backend()
    if backend == "local":
        try:
            return _transcribe_local_whisper(path)
        except Exception as exc:  # noqa: BLE001
            # Never hard-fail solely because the optional local backend is unavailable;
            # fall back to Deepgram when a key is present.
            if os.environ.get("DEEPGRAM_API_KEY", "").strip():
                logger.warning(
                    "Local Whisper STT failed (%s) — falling back to Deepgram Nova-3.", exc
                )
                return _transcribe_deepgram(path)
            raise
    return _transcribe_deepgram(path)


def _transcribe_local_whisper(path: Path) -> list[dict[str, Any]]:
    """
    Local STT via faster-whisper (CUDA when available, else CPU int8).

    Requires the optional ``faster-whisper`` package (installed in the GPU image).
    Returns timestamped segments compatible with the Deepgram path.
    """
    from faster_whisper import WhisperModel  # type: ignore

    from utils.gpu_utils import is_gpu_available, release_gpu_memory

    # Decode audio to a clean 16 kHz mono WAV for the most reliable transcription.
    from utils.video_utils import extract_audio

    audio_path = extract_audio(path)

    use_gpu = is_gpu_available()
    device = "cuda" if use_gpu else "cpu"
    compute_type = "float16" if use_gpu else "int8"
    model_name = os.environ.get("JOBDOC_WHISPER_MODEL", "small")

    logger.info("[jobdoc] Local Whisper STT (%s/%s, model=%s)", device, compute_type, model_name)
    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    try:
        segments, _info = model.transcribe(
            audio_path, language="en", vad_filter=True, beam_size=5
        )
        out: list[dict[str, Any]] = []
        for seg in segments:
            text = str(getattr(seg, "text", "")).strip()
            if text:
                out.append(
                    {
                        "start": float(getattr(seg, "start", 0.0) or 0.0),
                        "end": float(getattr(seg, "end", 0.0) or 0.0),
                        "text": text,
                    }
                )
        return out
    finally:
        release_gpu_memory()


def transcript_payload_from_segments(
    segments: list[dict[str, Any]],
    *,
    engine: str,
    language: str = "en",
) -> dict[str, Any]:
    """Build the transcript dict expected by ``build_matched_sections``."""
    full_text = " ".join(str(s.get("text", "")).strip() for s in segments if s.get("text"))
    return {
        "engine": engine,
        "language": language,
        "full_text": full_text.strip(),
        "segments": segments,
        "confidence_avg": None,
    }


def _guess_media_type(path: Path) -> str:
    suf = path.suffix.lower()
    return {
        ".webm": "video/webm",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
    }.get(suf, "application/octet-stream")


def _transcribe_deepgram(path: Path) -> list[dict[str, Any]]:
    api_key = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "DEEPGRAM_API_KEY is not set — cannot call Deepgram. On Modal, add it to the "
            "'jobdoc-secrets' Secret."
        )
    logger.info(
        "[jobdoc] Deepgram Nova-3 STT starting: file=%s size=%.1fKB key=***%s",
        path.name, path.stat().st_size / 1024.0, api_key[-4:],
    )

    options = {
        "model": DEEPGRAM_NOVA3_MODEL,
        "smart_format": True,
        "punctuate": True,
        "utterances": True,
        "language": "en",
    }
    return _transcribe_deepgram_rest(path, api_key, options)


def _transcribe_deepgram_rest(path: Path, api_key: str, options: dict[str, Any]) -> list[dict[str, Any]]:
    import urllib.error
    import urllib.request

    params = {
        "model": options.get("model", DEEPGRAM_NOVA3_MODEL),
        "smart_format": "true",
        "punctuate": "true",
        "utterances": "true",
        "language": options.get("language", "en"),
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"https://api.deepgram.com/v1/listen?{query}"
    req = urllib.request.Request(
        url,
        data=path.read_bytes(),
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": _guess_media_type(path),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Include the response body (e.g. auth/quota message) so failures are diagnosable in
        # logs. 401/403 almost always means a bad/missing DEEPGRAM_API_KEY.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        logger.error("Deepgram HTTP %s for %s: %s", getattr(exc, "code", "?"), path.name, detail)
        raise RuntimeError(f"Deepgram HTTP error {getattr(exc, 'code', '?')}: {detail or exc}") from exc
    except Exception as exc:  # noqa: BLE001 — network/DNS/timeout
        logger.error("Deepgram request failed for %s: %s", path.name, exc)
        raise RuntimeError(f"Deepgram request failed: {exc}") from exc

    segments = _segments_from_deepgram_dict(payload)
    logger.info("[jobdoc] Deepgram returned %d segment(s) for %s", len(segments), path.name)
    return segments


def _segments_from_deepgram_dict(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse Deepgram JSON (utterances preferred, else words)."""
    results = payload.get("results") or {}
    top_utts = results.get("utterances") or payload.get("utterances")
    if top_utts:
        return [
            {
                "start": float(u.get("start", 0.0)),
                "end": float(u.get("end", 0.0)),
                "text": str(u.get("transcript", "")).strip(),
            }
            for u in top_utts
            if str(u.get("transcript", "")).strip()
        ]

    channels = results.get("channels") or []
    if not channels:
        return []
    alternatives = (channels[0].get("alternatives") or [{}])[0]
    utterances = alternatives.get("utterances") or results.get("utterances")
    segs: list[dict[str, Any]] = []
    if utterances:
        for u in utterances:
            segs.append(
                {
                    "start": float(u.get("start", 0.0)),
                    "end": float(u.get("end", 0.0)),
                    "text": str(u.get("transcript", u.get("text", ""))).strip(),
                }
            )
        return [s for s in segs if s["text"]]

    words = alternatives.get("words") or []
    if words:
        cur_start = float(words[0].get("start", 0.0))
        buf: list[str] = []
        buf_end = cur_start
        for w in words:
            st = float(w.get("start", 0.0))
            en = float(w.get("end", st))
            wd = str(w.get("word", w.get("punctuated_word", ""))).strip()
            if not buf:
                cur_start = st
            buf.append(wd)
            buf_end = en
            if len(buf) >= 12:
                segs.append(
                    {
                        "start": cur_start,
                        "end": buf_end,
                        "text": " ".join(buf),
                    }
                )
                buf = []
        if buf:
            segs.append({"start": cur_start, "end": buf_end, "text": " ".join(buf)})
        return segs

    transcript = str(alternatives.get("transcript", "")).strip()
    if transcript:
        return [{"start": 0.0, "end": 0.0, "text": transcript}]
    return []
