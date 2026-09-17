"""
gpu_utils.py — Runtime-only GPU detection + hardware-accelerated ffmpeg helpers.

WHY THIS EXISTS
---------------
JobDoc uses **zero computer vision**. GPU hardware (when a Hugging Face Space is
configured with a GPU tier) is used *exclusively* to accelerate the local media
pipeline — ffmpeg hardware decode/encode (NVDEC/NVENC) and still-frame extraction —
plus an optional local Whisper STT backend. The frontier-LLM reasoning (OpenAI via
CrewAI) and the default STT (Deepgram Nova-3) stay cloud-based regardless of hardware.

CRITICAL HUGGING FACE SPACES RULE
---------------------------------
GPU hardware is only present at **runtime** (after the container starts). It is NEVER
available during ``docker build`` on HF build machines. Therefore:

    * Nothing in this module probes hardware at import time.
    * Every detection helper runs lazily on first call **inside the running app** and
      caches its result.
    * Every code path degrades gracefully to CPU/software when GPU is unavailable —
      the app must work identically (just slower on media steps) on CPU-only Spaces.

The detection helpers are intentionally defensive: any import error, missing binary,
or subprocess failure resolves to "CPU mode" rather than raising.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "GpuRuntime",
    "gpu_runtime_info",
    "is_gpu_available",
    "ffmpeg_available",
    "ffmpeg_hwaccels",
    "ffmpeg_has_nvenc",
    "should_use_hw_video",
    "build_normalize_cmd",
    "build_frame_cmd",
    "build_audio_cmd",
    "accel_label",
    "acceleration_status",
    "release_gpu_memory",
    "log_gpu_status",
    "reset_detection_cache",
]

# -----------------------------------------------------------------------------
# Lazy, thread-safe runtime caches (populated on first call inside the container)
# -----------------------------------------------------------------------------

_lock = threading.Lock()
_gpu_runtime: "GpuRuntime | None" = None
_ffmpeg_hwaccels: "set[str] | None" = None
_ffmpeg_nvenc: "bool | None" = None
_logged_once = False


@dataclass
class GpuRuntime:
    """Snapshot of GPU availability detected at runtime."""

    available: bool = False
    torch_available: bool = False
    device_count: int = 0
    device_name: str = ""
    cuda_version: str = ""
    detail: str = "CPU mode — no CUDA device detected."

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "torch_available": self.torch_available,
            "device_count": self.device_count,
            "device_name": self.device_name,
            "cuda_version": self.cuda_version,
            "detail": self.detail,
        }


def _detect_gpu_runtime() -> GpuRuntime:
    """
    Probe ``torch.cuda`` at runtime. Never raises — returns a CPU snapshot on any failure.

    ``JOBDOC_DISABLE_GPU=1`` forces CPU mode (useful for debugging or to mirror a
    CPU-only Space locally).
    """
    if os.environ.get("JOBDOC_DISABLE_GPU", "").strip() in ("1", "true", "yes"):
        return GpuRuntime(detail="CPU mode — GPU disabled via JOBDOC_DISABLE_GPU.")

    try:
        import torch  # type: ignore
    except Exception as exc:  # noqa: BLE001 — torch is optional on CPU Spaces
        return GpuRuntime(
            torch_available=False,
            detail=f"CPU mode — PyTorch not installed ({type(exc).__name__}).",
        )

    try:
        if not torch.cuda.is_available():
            return GpuRuntime(
                torch_available=True,
                detail="CPU mode — torch.cuda.is_available() returned False.",
            )
        count = int(torch.cuda.device_count())
        name = torch.cuda.get_device_name(0) if count else ""
        cuda_ver = getattr(torch.version, "cuda", "") or ""
        return GpuRuntime(
            available=True,
            torch_available=True,
            device_count=count,
            device_name=name,
            cuda_version=cuda_ver,
            detail=f"GPU acceleration active (CUDA {cuda_ver}, {name}).",
        )
    except Exception as exc:  # noqa: BLE001
        return GpuRuntime(
            torch_available=True,
            detail=f"CPU mode — CUDA probe failed ({type(exc).__name__}: {exc}).",
        )


def gpu_runtime_info() -> GpuRuntime:
    """Return cached runtime GPU snapshot (detected lazily on first call)."""
    global _gpu_runtime
    if _gpu_runtime is None:
        with _lock:
            if _gpu_runtime is None:
                _gpu_runtime = _detect_gpu_runtime()
    return _gpu_runtime


def is_gpu_available() -> bool:
    """True only at runtime when a CUDA device is usable."""
    return gpu_runtime_info().available


# -----------------------------------------------------------------------------
# ffmpeg capability probes (runtime-only, cached)
# -----------------------------------------------------------------------------


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffmpeg_hwaccels() -> set[str]:
    """
    Return the set of hardware accelerators ffmpeg reports (e.g. ``{"cuda", "vaapi"}``).

    Cached. Runs ``ffmpeg -hwaccels`` once at runtime; returns an empty set on any failure.
    """
    global _ffmpeg_hwaccels
    if _ffmpeg_hwaccels is not None:
        return _ffmpeg_hwaccels
    with _lock:
        if _ffmpeg_hwaccels is not None:
            return _ffmpeg_hwaccels
        accels: set[str] = set()
        if ffmpeg_available():
            try:
                out = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-hwaccels"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                for line in out.stdout.splitlines():
                    token = line.strip().lower()
                    if token and token != "hardware acceleration methods:":
                        accels.add(token)
            except Exception as exc:  # noqa: BLE001
                logger.debug("ffmpeg -hwaccels probe failed: %s", exc)
        _ffmpeg_hwaccels = accels
        return _ffmpeg_hwaccels


def ffmpeg_has_nvenc() -> bool:
    """True if ffmpeg lists the ``h264_nvenc`` encoder (cached, runtime-only)."""
    global _ffmpeg_nvenc
    if _ffmpeg_nvenc is not None:
        return _ffmpeg_nvenc
    with _lock:
        if _ffmpeg_nvenc is not None:
            return _ffmpeg_nvenc
        has = False
        if ffmpeg_available():
            try:
                out = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-encoders"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                has = "h264_nvenc" in out.stdout
            except Exception as exc:  # noqa: BLE001
                logger.debug("ffmpeg -encoders probe failed: %s", exc)
        _ffmpeg_nvenc = has
        return _ffmpeg_nvenc


def should_use_hw_video() -> bool:
    """
    Decide whether to *attempt* CUDA-accelerated ffmpeg.

    Requires a runtime CUDA device AND ffmpeg advertising the ``cuda`` hwaccel.
    Callers must still fall back to software if the hardware command fails.
    """
    if not is_gpu_available():
        return False
    return "cuda" in ffmpeg_hwaccels()


# -----------------------------------------------------------------------------
# ffmpeg command builders (HW-accelerated + software variants)
# -----------------------------------------------------------------------------


def build_normalize_cmd(src: Path | str, dst: Path | str, *, use_hw: bool) -> list[str]:
    """
    Build the ffmpeg command to transcode an upload to a clean, fast-start MP4.

    HW path uses CUDA decode (``-hwaccel cuda``) + NVENC (``h264_nvenc``).
    SW path uses libx264. Both produce a browser-friendly ``yuv420p`` MP4 with
    ``+faststart`` so the preview ``<video>`` plays on iOS Safari and Android Chrome.
    """
    src, dst = str(src), str(dst)
    # Input-robustness flags: mobile recorders (esp. iOS) often ship MP4s with a broken/late
    # moov atom, missing/incorrect timestamps, or truncated tails. ``+genpts`` regenerates
    # presentation timestamps, ``+igndts`` ignores bad decode timestamps, and
    # ``-err_detect ignore_err`` lets the decoder push through minor corruption instead of
    # aborting with "Invalid data found" / "could not find corresponding trex".
    robust_in = ["-fflags", "+genpts+igndts", "-err_detect", "ignore_err"]
    if use_hw:
        return [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            *robust_in,
            "-hwaccel", "cuda",
            "-i", src,
            "-c:v", "h264_nvenc",
            "-preset", "p4",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "128k",
            dst,
        ]
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *robust_in,
        "-i", src,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "128k",
        dst,
    ]


def build_frame_cmd(
    src: Path | str,
    timestamp_sec: float,
    dst: Path | str,
    *,
    use_hw: bool,
    max_width: int | None = None,
) -> list[str]:
    """
    Build the ffmpeg command to grab a single still at ``timestamp_sec``.

    HW path uses ``-hwaccel cuda`` for fast decoding; the frame is implicitly
    downloaded to system memory so the (CPU) image encoder works unchanged.
    Input seeking (``-ss`` before ``-i``) keeps extraction fast on long videos.

    ``max_width`` (optional) downscales the still to at most that width (preserving aspect,
    never upscaling). Smaller stills make WeasyPrint render dramatically faster and keep the
    PDF lightweight for fast download on a job-site connection. The output codec is chosen by
    ``dst``'s extension (use ``.jpg`` for the smallest PDFs).
    """
    src, dst = str(src), str(dst)
    ts = f"{max(0.0, float(timestamp_sec)):.3f}"
    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if use_hw:
        base += ["-hwaccel", "cuda"]
    base += ["-ss", ts, "-i", src, "-frames:v", "1"]
    if max_width and max_width > 0:
        # Even target width keeps yuv420p/JPEG encoders happy; -2 keeps height divisible by 2.
        base += ["-vf", f"scale='min(iw,{int(max_width)})':-2"]
    base += ["-q:v", "3", dst]
    return base


def build_audio_cmd(src: Path | str, dst: Path | str) -> list[str]:
    """
    Build the ffmpeg command to extract mono 16 kHz PCM WAV for STT.

    Audio decode is cheap and not meaningfully GPU-bound, so this is always a
    software path (works identically on CPU and GPU Spaces).

    Input-robustness flags (``+genpts+igndts``, ``-err_detect ignore_err``) let ffmpeg
    recover audio from imperfect mobile MP4s (broken moov / bad timestamps) instead of
    failing with "failed to read the duration of file" / "Invalid data found".
    """
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+igndts", "-err_detect", "ignore_err",
        "-i", str(src),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(dst),
    ]


# -----------------------------------------------------------------------------
# Status reporting + memory hygiene
# -----------------------------------------------------------------------------


def accel_label() -> str:
    """Short human label for UI / logs."""
    info = gpu_runtime_info()
    if info.available and should_use_hw_video():
        return f"GPU acceleration active (CUDA {info.cuda_version})"
    if info.available:
        return "GPU detected — ffmpeg hardware accel unavailable; using CPU media path"
    return "CPU mode — hardware video accel unavailable"


def acceleration_status() -> dict:
    """Structured status for the ``/health`` endpoint and preview UI."""
    info = gpu_runtime_info()
    hw_video = should_use_hw_video()
    return {
        "gpu": info.to_dict(),
        "ffmpeg_available": ffmpeg_available(),
        "ffmpeg_hwaccels": sorted(ffmpeg_hwaccels()),
        "nvenc": ffmpeg_has_nvenc(),
        "hw_video_pipeline": hw_video,
        "label": accel_label(),
        "media_mode": "gpu" if hw_video else "cpu",
    }


def release_gpu_memory() -> None:
    """
    Best-effort release of cached CUDA memory after heavy media operations.

    No-op (and never raises) when torch/CUDA are absent — safe on CPU Spaces.
    """
    info = gpu_runtime_info()
    if not info.available:
        return
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception as exc:  # noqa: BLE001
        logger.debug("release_gpu_memory no-op: %s", exc)


def log_gpu_status() -> None:
    """Log the runtime acceleration mode exactly once (call at app startup)."""
    global _logged_once
    if _logged_once:
        return
    _logged_once = True
    status = acceleration_status()
    info = status["gpu"]
    if info["available"]:
        logger.info(
            "[jobdoc] %s | ffmpeg hwaccels=%s nvenc=%s | media pipeline: %s",
            info["detail"],
            status["ffmpeg_hwaccels"],
            status["nvenc"],
            status["media_mode"].upper(),
        )
    else:
        logger.info(
            "[jobdoc] %s | ffmpeg=%s | media pipeline: CPU (fully functional, slower on video steps)",
            info["detail"],
            status["ffmpeg_available"],
        )


def reset_detection_cache() -> None:
    """Clear cached probes (tests only — production detects once per process)."""
    global _gpu_runtime, _ffmpeg_hwaccels, _ffmpeg_nvenc, _logged_once
    with _lock:
        _gpu_runtime = None
        _ffmpeg_hwaccels = None
        _ffmpeg_nvenc = None
        _logged_once = False
