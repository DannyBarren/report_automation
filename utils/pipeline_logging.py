"""
pipeline_logging.py — Structured logs for field debugging (correlate by job_id / video_id).

In production (HF Spaces) emit one JSON object per line so the platform log viewer can index
and filter by ``job_id`` and ``step``. Locally, fall back to a readable plain format. Toggle with
``JOBDOC_LOG_JSON`` (defaults to JSON on; set ``0`` for plain text).
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

logger = logging.getLogger("jobdoc.pipeline")

# Standard LogRecord attributes we never want to duplicate into the JSON payload.
_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render each record as a single-line JSON object for structured log ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Promote any structured extras (e.g. job_id, step) attached via ``extra=...``.
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = str(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _json_logging_enabled() -> bool:
    return os.environ.get("JOBDOC_LOG_JSON", "1").strip().lower() not in ("0", "false", "no")


def configure_logging() -> None:
    """
    Install the root log handler exactly once.

    Uses structured JSON in production (default) or a readable plain format for local dev.
    Honors ``JOBDOC_LOG=debug`` for verbose output.
    """
    level = logging.DEBUG if os.environ.get("JOBDOC_LOG", "").lower() == "debug" else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    # Replace any pre-existing handlers so re-import (gunicorn workers) stays idempotent.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    if _json_logging_enabled():
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
    root.addHandler(handler)


def log_job_step(
    video_id: int | str | None,
    step: str,
    detail: str,
    *,
    level: int = logging.INFO,
    extra: dict[str, Any] | None = None,
) -> None:
    """Log a pipeline step, attaching ``job_id`` + ``step`` as structured fields."""
    fields: dict[str, Any] = {
        "job_id": str(video_id) if video_id is not None else None,
        "step": step,
    }
    if extra:
        fields["detail_extra"] = extra
    logger.log(level, detail, extra=fields)


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
