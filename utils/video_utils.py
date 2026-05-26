"""
video_utils.py — Audio and image extraction from uploaded field videos.

``image_timestamp_sec`` (from the structured pipeline) selects the time for ``extract_frame``.
Audio extraction feeds Deepgram Nova-3 when transcribing the original video file.

Failed extraction raises or returns False — callers enforce strict policies (no synthetic frames).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

__all__ = [
    "normalize_video_to_mp4",
    "extract_audio",
    "extract_frame",
]

def normalize_video_to_mp4(input_path: str | Path) -> Path:
    """
    Normalize iPhone-friendly uploads to MP4 when needed.

    iOS devices commonly upload `.mov`. While MoviePy/ffmpeg can often read `.mov` directly,
    normalizing to `.mp4` improves compatibility across downstream steps (frame extraction,
    audio extraction, and some codec edge cases) and makes file handling consistent.

    Returns:
        Path to an `.mp4` file (original path if already non-`.mov`).

    Raises:
        RuntimeError: If conversion is required but cannot be performed.
    """
    src = Path(input_path)
    if src.suffix.lower() != ".mov":
        return src
    if not src.is_file():
        raise FileNotFoundError(str(src))

    dst = src.with_suffix(".mp4")
    try:
        if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst
    except OSError:
        pass

    # Prefer explicit ffmpeg invocation when available (fast, reliable, avoids MoviePy overhead).
    if shutil.which("ffmpeg"):
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(dst),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            if dst.is_file():
                return dst
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"ffmpeg failed to convert {src.name} to mp4: {exc}") from exc

    # Fallback: MoviePy (internally uses ffmpeg, but keeps behavior consistent if available).
    try:
        from moviepy.editor import VideoFileClip  # type: ignore

        clip = VideoFileClip(str(src))
        clip.write_videofile(
            str(dst),
            codec="libx264",
            audio_codec="aac",
            logger=None,
            threads=2,
        )
        clip.close()
        if dst.is_file():
            return dst
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Could not normalize {src.name} to mp4. Install ffmpeg or ensure MoviePy can access it. ({exc})"
        ) from exc

    raise RuntimeError(f"Could not normalize {src.name} to mp4 (unknown error).")


def extract_audio(video_path: str | Path) -> str:
    """
    Extract a ``.wav`` (mono) file next to the source video and return its path.

    Tries **MoviePy** first; if import or decode fails, falls back to **ffmpeg** if available.
    Raises ``RuntimeError`` if the file has no usable audio stream (speech-to-text requires audio).
    """
    video_path = normalize_video_to_mp4(video_path)
    if not video_path.is_file():
        raise FileNotFoundError(str(video_path))

    out = video_path.with_name(f"{video_path.stem}_audio.wav")
    if out.exists():
        out.unlink()

    try:
        from moviepy.editor import VideoFileClip  # type: ignore

        clip = VideoFileClip(str(video_path))
        if clip.audio is None:
            clip.close()
            if shutil.which("ffmpeg"):
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(video_path),
                    "-vn",
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    str(out),
                ]
                subprocess.run(cmd, check=True, capture_output=True)
                if out.is_file():
                    return str(out)
            raise RuntimeError(
                "No audio track found in this video (or ffmpeg could not extract audio). "
                "Speech-to-text requires an audible recording."
            )
        clip.audio.write_audiofile(str(out), logger=None)  # type: ignore[arg-type]
        clip.close()
        return str(out)
    except RuntimeError:
        raise
    except Exception:  # noqa: BLE001
        if shutil.which("ffmpeg"):
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(out),
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            return str(out)
        raise


def extract_frame(
    video_path: str | Path,
    timestamp_seconds: float,
    output_path: str | Path,
) -> bool:
    """
    Write a single RGB still at ``timestamp_seconds`` to ``output_path`` (PNG recommended).

    The timestamp is **clamped** to ``[0, duration)``. Returns ``False`` if the frame could not be
    written.
    """
    video_path = normalize_video_to_mp4(video_path)
    output_path = Path(output_path)
    if not video_path.is_file():
        return False
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from moviepy.editor import VideoFileClip  # type: ignore

        clip = VideoFileClip(str(video_path))
        dur = float(clip.duration or 0.0)
        t = max(0.0, min(float(timestamp_seconds), max(0.0, dur - 0.04)))
        frame = clip.get_frame(t)
        clip.close()
        try:
            from PIL import Image
        except ImportError:
            return False
        Image.fromarray(frame).save(str(output_path))
        return output_path.is_file()
    except Exception:  # noqa: BLE001
        if shutil.which("ffmpeg"):
            out = str(output_path)
            cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                str(max(0.0, float(timestamp_seconds))),
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                out,
            ]
            try:
                subprocess.run(cmd, check=True, capture_output=True)
                return Path(out).is_file()
            except Exception:  # noqa: BLE001
                return False
        return False
