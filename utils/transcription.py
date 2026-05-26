"""
transcription.py — Production speech-to-text via Deepgram Nova-3 only.

Required configuration
----------------------
``DEEPGRAM_API_KEY`` must be set. The module fails fast when missing.

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
import os
from pathlib import Path
from typing import Any

DEEPGRAM_NOVA3_MODEL = "nova-3"


def transcribe_video(video_path: str | Path) -> list[dict[str, Any]]:
    """
    Transcribe ``video_path`` and return timestamped segments.

    Raises:
        RuntimeError: No ASR provider configured or provider failure.
        FileNotFoundError: Missing file.
    """
    # Normalize `.mov` (common on iPhone) to `.mp4` for best compatibility.
    # Deepgram can accept multiple container types, but normalizing avoids codec/container edge cases.
    from utils.video_utils import normalize_video_to_mp4

    path = normalize_video_to_mp4(video_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))

    return _transcribe_deepgram(path)


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
        raise RuntimeError("DEEPGRAM_API_KEY is set empty — cannot call Deepgram.")

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
        raise RuntimeError(f"Deepgram HTTP error: {exc}") from exc

    return _segments_from_deepgram_dict(payload)


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
