"""
pipeline_logging.py — Structured logs for field debugging (correlate by video_id).
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger("jobdoc.pipeline")


def log_job_step(
    video_id: int | str | None,
    step: str,
    detail: str,
    *,
    level: int = logging.INFO,
    extra: dict[str, Any] | None = None,
) -> None:
    vid = str(video_id) if video_id is not None else "—"
    msg = f"video_id={vid} step={step} {detail}"
    if extra:
        logger.log(level, "%s | extra=%s", msg, extra)
    else:
        logger.log(level, msg)


@contextmanager
def job_step_timer(
    video_id: int | str | None,
    step: str,
) -> Iterator[None]:
    log_job_step(video_id, step, "start")
    t0 = time.perf_counter()
    try:
        yield
        elapsed = time.perf_counter() - t0
        log_job_step(video_id, step, f"done in {elapsed:.1f}s")
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        log_job_step(
            video_id,
            step,
            f"failed after {elapsed:.1f}s: {exc}",
            level=logging.WARNING,
        )
        raise
