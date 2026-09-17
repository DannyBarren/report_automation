"""
video_utils.py — Audio + still-frame extraction and upload normalization.

GPU ACCELERATION (HF Spaces compliant)
--------------------------------------
When the running container has a CUDA device AND ffmpeg advertises the ``cuda`` hwaccel
(see ``utils/gpu_utils``), video normalization and still-frame extraction *attempt*
hardware decode/encode (NVDEC/NVENC) for a major speedup on mobile video. Every
hardware path falls back automatically to the software (libx264 / CPU decode) path on
any failure, so behaviour is identical — just slower — on CPU-only Spaces.

All GPU detection is runtime-only and lives in ``utils/gpu_utils``; nothing here probes
hardware at import time.

``image_timestamp_sec`` (from the structured pipeline) selects the time for ``extract_frame``.
Audio extraction feeds Deepgram Nova-3 (or the optional local Whisper backend).
Failed extraction raises or returns False — callers enforce strict policies (no synthetic frames).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from utils.gpu_utils import (
    build_audio_cmd,
    build_frame_cmd,
    build_normalize_cmd,
    release_gpu_memory,
    should_use_hw_video,
)

logger = logging.getLogger(__name__)

__all__ = [
    "normalize_video_to_mp4",
    "extract_audio",
    "try_extract_audio",
    "remux_faststart",
    "extract_frame",
    "probe_duration_seconds",
]

# Containers we normalize to MP4 for consistent downstream handling + universal
# browser playback (iOS Safari cannot play .webm; normalizing fixes the preview video).
_NORMALIZE_SUFFIXES = {".mov", ".webm", ".mkv", ".bin"}

# Input-robustness flags for imperfect mobile recordings. Mobile MediaRecorder output
# (especially iOS) frequently has a broken/late moov atom, missing timestamps, or a
# truncated tail, which trips ffmpeg with "could not find corresponding trex",
# "failed to read the duration of file", or "Invalid data found when processing input".
# These flags regenerate timestamps and let the demuxer/decoder push past minor corruption.
_ROBUST_INPUT_FLAGS = ["-fflags", "+genpts+igndts", "-err_detect", "ignore_err"]


class FFmpegError(RuntimeError):
    """ffmpeg exited non-zero; carries the (trimmed) stderr for diagnostics."""


def _run_ffmpeg(cmd: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run an ffmpeg command, raising ``FFmpegError`` (with stderr tail) on non-zero exit."""
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        # Keep the tail — ffmpeg prints the actionable error last.
        tail = stderr[-1500:] if stderr else "(no stderr)"
        raise FFmpegError(f"ffmpeg exited {proc.returncode}: {tail}")
    return proc


def _transcode_with_fallback(
    src: Path, dst: Path, build, *, op: str, timeout: int = 600
) -> bool:
    """
    Attempt the hardware-accelerated ffmpeg command first (when available), then the
    software command. Returns True if ``dst`` was produced.

    ``build(use_hw: bool) -> list[str]`` returns the ffmpeg argv for each mode.
    """
    attempts: list[tuple[bool, str]] = []
    if should_use_hw_video():
        attempts.append((True, "gpu"))
    attempts.append((False, "cpu"))

    last_err: Exception | None = None
    for use_hw, mode in attempts:
        cmd = build(use_hw)
        started = time.perf_counter()
        try:
            _run_ffmpeg(cmd, timeout=timeout)
            if dst.is_file() and dst.stat().st_size > 0:
                elapsed = time.perf_counter() - started
                logger.info(
                    "[jobdoc] %s via %s ffmpeg in %.2fs → %s",
                    op, mode.upper(), elapsed, dst.name,
                )
                return True
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if use_hw:
                logger.warning(
                    "[jobdoc] %s: GPU ffmpeg failed — falling back to CPU path. %s",
                    op, exc,
                )
            # try next attempt (software)
        finally:
            if use_hw:
                release_gpu_memory()
    if last_err:
        logger.error(
            "[jobdoc] %s: all ffmpeg paths failed for %s: %s",
            op, Path(src).name, last_err,
        )
    return False


def remux_faststart(input_path: str | Path, output_path: str | Path | None = None) -> Path | None:
    """
    Repair/normalize a container by stream-copy remuxing with a rebuilt, front-loaded moov.

    This is the cheapest possible fix for the most common mobile problem: a valid
    (H.264/AAC) MP4 whose moov atom is missing from the front or whose timestamps are
    malformed. No re-encoding — it just rewrites the container with ``+faststart`` and
    regenerated timestamps, which resolves "could not find corresponding trex" and
    "failed to read the duration of file" for a large class of iOS/Android uploads.

    Returns the output ``Path`` on success, or ``None`` on failure (never raises).
    """
    src = Path(input_path)
    if not src.is_file() or not shutil.which("ffmpeg"):
        return None
    dst = Path(output_path) if output_path else src.with_name(f"{src.stem}_fixed.mp4")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *_ROBUST_INPUT_FLAGS,
        "-i", str(src),
        "-c", "copy",
        "-movflags", "+faststart",
        str(dst),
    ]
    try:
        _run_ffmpeg(cmd, timeout=180)
        if dst.is_file() and dst.stat().st_size > 0:
            logger.info("[jobdoc] remux (+faststart) repaired container → %s", dst.name)
            return dst
    except Exception as exc:  # noqa: BLE001 — remux is a best-effort repair
        logger.warning("[jobdoc] remux (+faststart) failed for %s: %s", src.name, exc)
    return None


def _extract_audio_once(src: Path, out: Path, *, timeout: int = 300) -> bool:
    """Run one robust ffmpeg audio-extraction attempt from ``src`` → ``out`` (never raises)."""
    if not src.is_file() or not shutil.which("ffmpeg"):
        return False
    try:
        _run_ffmpeg(build_audio_cmd(src, out), timeout=timeout)
        return out.is_file() and out.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jobdoc] audio extraction attempt failed for %s: %s", src.name, exc)
        return False


def probe_duration_seconds(video_path: str | Path) -> float | None:
    """
    Return media duration in seconds via ffprobe, or None if it cannot be determined.

    Used for guardrails (reject videos longer than the configured limit). Never raises.
    """
    path = Path(video_path)
    if not path.is_file() or not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True, capture_output=True, text=True, timeout=30,
        )
        value = out.stdout.strip()
        return float(value) if value else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("ffprobe duration probe failed: %s", exc)
        return None


def normalize_video_to_mp4(input_path: str | Path) -> Path:
    """
    Normalize a recorded upload to a browser-friendly, fast-start MP4 when needed.

    iOS uploads ``.mov``; Android MediaRecorder uploads ``.webm`` (which iOS Safari
    cannot play back). Normalizing both to ``.mp4`` makes the preview video universally
    playable and gives the rest of the pipeline (audio extraction, frame seeking,
    transcription) a consistent container with accurate seeking.

    Uses GPU-accelerated ffmpeg when available, falling back to software automatically.

    Returns:
        Path to an ``.mp4`` (original path when already MP4 or when normalization is
        unnecessary). The original ``.mp4`` is returned unchanged.

    Raises:
        RuntimeError: If conversion is required but cannot be performed by any backend.
    """
    src = Path(input_path)
    if src.suffix.lower() not in _NORMALIZE_SUFFIXES:
        return src
    if not src.is_file():
        raise FileNotFoundError(str(src))

    dst = src.with_suffix(".mp4")
    # Reuse an existing normalized file when it is at least as new as the source.
    try:
        if dst.is_file() and dst != src and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst
    except OSError:
        pass

    if shutil.which("ffmpeg"):
        ok = _transcode_with_fallback(
            src, dst,
            lambda use_hw: build_normalize_cmd(src, dst, use_hw=use_hw),
            op="normalize",
        )
        if ok:
            return dst
        # Re-encode failed (e.g. corrupt/partial mobile file). Try a cheap stream-copy
        # remux with +faststart — for an mp4-family container with only a bad moov this
        # often succeeds where a full decode/encode could not.
        remuxed = remux_faststart(src, dst)
        if remuxed and remuxed.is_file():
            return remuxed

    # Last-resort fallback: MoviePy (also wraps ffmpeg) for environments without a
    # usable ffmpeg CLI. Kept for backward compatibility / local dev.
    try:
        from moviepy.editor import VideoFileClip  # type: ignore

        clip = VideoFileClip(str(src))
        clip.write_videofile(
            str(dst), codec="libx264", audio_codec="aac", logger=None, threads=2
        )
        clip.close()
        if dst.is_file():
            return dst
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Could not normalize {src.name} to mp4. Install ffmpeg or ensure MoviePy "
            f"can access it. ({exc})"
        ) from exc

    raise RuntimeError(f"Could not normalize {src.name} to mp4 (unknown error).")


def extract_audio(video_path: str | Path) -> str:
    """
    Extract a mono 16 kHz ``.wav`` next to the source video and return its path.

    Highly resilient to imperfect mobile recordings. Strategies are attempted in order,
    each with timestamp-regenerating / error-tolerant ffmpeg flags:

        1. Normalize to a clean MP4 (re-encode, +faststart), then extract from it.
        2. Extract directly from the original upload.
        3. Remux (+faststart, stream copy) to repair the moov atom, then extract.
        4. MoviePy fallback (for environments without a usable ffmpeg CLI).

    Raises ``RuntimeError`` only when every strategy fails (genuinely no usable audio).
    Callers that must not fail the whole report should use :func:`try_extract_audio`.
    """
    src = Path(video_path)
    if not src.is_file():
        raise FileNotFoundError(str(src))

    # Normalization also produces the browser-friendly MP4 used elsewhere; never fatal here.
    normalized: Path | None = None
    try:
        normalized = normalize_video_to_mp4(src)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jobdoc] normalization failed for %s: %s — trying raw file.", src.name, exc)

    base = normalized if (normalized and normalized.is_file()) else src
    out = base.with_name(f"{base.stem}_audio.wav")
    if out.exists():
        try:
            out.unlink()
        except OSError:
            pass

    # Ordered source candidates (de-duplicated): normalized mp4, then the raw upload.
    candidates: list[Path] = []
    for cand in (base, src):
        if cand.is_file() and cand not in candidates:
            candidates.append(cand)

    for cand in candidates:
        if _extract_audio_once(cand, out):
            return str(out)

    # Repair the container (rebuild moov / regenerate timestamps) and try once more.
    repaired = remux_faststart(src, src.with_name(f"{src.stem}_repair.mp4"))
    if repaired and _extract_audio_once(repaired, out):
        return str(out)

    logger.warning(
        "[jobdoc] all ffmpeg audio strategies failed for %s — attempting MoviePy fallback.",
        src.name,
    )

    # MoviePy fallback (raises a friendly error when there is genuinely no audio track).
    try:
        from moviepy.editor import VideoFileClip  # type: ignore

        clip = VideoFileClip(str(base))
        if clip.audio is None:
            clip.close()
            raise RuntimeError(
                "No audio track found in this video. Speech-to-text requires an audible recording."
            )
        clip.audio.write_audiofile(str(out), logger=None)  # type: ignore[arg-type]
        clip.close()
        return str(out)
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Could not extract audio from {src.name}. The recording may be incomplete or "
            f"corrupted. ({exc})"
        ) from exc


def try_extract_audio(video_path: str | Path) -> str | None:
    """
    Best-effort audio extraction that **never raises** (returns ``None`` on failure).

    Use for non-critical warm-up/validation where the caller can still proceed — e.g. the
    cloud STT path sends the video container directly to the provider, so a local audio
    extraction failure must not abort report generation.
    """
    try:
        return extract_audio(video_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jobdoc] try_extract_audio: giving up on local audio (%s).", exc)
        return None


def extract_frame(
    video_path: str | Path,
    timestamp_seconds: float,
    output_path: str | Path,
    *,
    max_width: int | None = None,
) -> bool:
    """
    Write a single still at ``timestamp_seconds`` to ``output_path``.

    Tries GPU-accelerated ffmpeg decode first (when available), then software ffmpeg,
    then MoviePy. The timestamp is clamped to ``>= 0`` (and to the duration when known).
    Returns ``False`` if no backend could produce the frame.

    ``max_width`` (optional) downscales the still to at most that width — smaller frames make
    WeasyPrint render much faster and keep the PDF light for quick on-site downloads. The
    output image codec follows ``output_path``'s extension (``.jpg`` recommended for PDFs).
    """
    video_path = normalize_video_to_mp4(video_path)
    output_path = Path(output_path)
    if not video_path.is_file():
        return False
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ts = max(0.0, float(timestamp_seconds))
    duration = probe_duration_seconds(video_path)
    if duration and duration > 0:
        ts = min(ts, max(0.0, duration - 0.05))

    if shutil.which("ffmpeg"):
        ok = _transcode_with_fallback(
            video_path, output_path,
            lambda use_hw: build_frame_cmd(
                video_path, ts, output_path, use_hw=use_hw, max_width=max_width
            ),
            op="frame",
            timeout=120,
        )
        if ok:
            return True

    # MoviePy fallback for environments without an ffmpeg CLI.
    try:
        from moviepy.editor import VideoFileClip  # type: ignore
        from PIL import Image

        clip = VideoFileClip(str(video_path))
        dur = float(clip.duration or 0.0)
        t = max(0.0, min(ts, max(0.0, dur - 0.04)))
        frame = clip.get_frame(t)
        clip.close()
        img = Image.fromarray(frame)
        if max_width and img.width > max_width:
            new_h = max(1, round(img.height * (max_width / img.width)))
            img = img.resize((max_width, new_h))
        if output_path.suffix.lower() in (".jpg", ".jpeg"):
            img = img.convert("RGB")
            img.save(str(output_path), quality=82, optimize=True)
        else:
            img.save(str(output_path))
        return output_path.is_file()
    except Exception as exc:  # noqa: BLE001
        logger.debug("MoviePy frame fallback failed: %s", exc)
        return False
