"""
maintenance.py — Lightweight disk hygiene for long-lived public Spaces.

A public Hugging Face Space accumulates uploaded videos, extracted still frames, and
generated PDFs over time. This module provides a small, dependency-free background reaper
that age-prunes those directories so the Space disk never fills up.

Design:
    * Age-based deletion only (by mtime) — recent jobs are kept so their preview pages and
      downloads keep working; old artifacts are removed.
    * Best-effort and defensive — never raises into the request path or crashes the thread.
    * Configurable via ``JOBDOC_RETENTION_HOURS`` (default 24h; ``0`` disables the reaper).
    * Runs in a daemon thread, so it never blocks shutdown.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

__all__ = ["retention_hours", "reap_once", "start_reaper"]

# Provider returning filenames that must NEVER be pruned (e.g. recordings linked to a saved
# capture session, kept for audit / future invoicing). Evaluated once per pass.
ProtectedNamesProvider = Callable[[], set[str]]

_started = False
_lock = threading.Lock()


def retention_hours() -> float:
    """Hours to keep generated artifacts. ``0`` (or invalid) disables the reaper."""
    raw = os.environ.get("JOBDOC_RETENTION_HOURS", "24").strip()
    try:
        value = float(raw)
    except ValueError:
        return 24.0
    return max(0.0, value)


def _prune_dir(directory: Path, cutoff: float, *, recurse: bool, protected: set[str]) -> int:
    """Delete files under ``directory`` older than ``cutoff`` (epoch seconds). Returns count.

    Files whose name is in ``protected`` are always kept regardless of age.
    """
    if not directory.is_dir():
        return 0
    removed = 0
    try:
        for entry in directory.iterdir():
            try:
                if entry.is_dir():
                    if recurse:
                        removed += _prune_dir(entry, cutoff, recurse=True, protected=protected)
                        # Remove now-empty per-job frame folders.
                        try:
                            next(entry.iterdir())
                        except StopIteration:
                            entry.rmdir()
                    continue
                if entry.name in protected:
                    continue  # session recording kept for audit / invoicing
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError as exc:  # noqa: PERF203 — per-entry safety
                logger.debug("reaper skip %s: %s", entry, exc)
    except OSError as exc:
        logger.debug("reaper could not list %s: %s", directory, exc)
    return removed


def reap_once(
    targets: list[Path],
    *,
    max_age_hours: float | None = None,
    protected_names: set[str] | None = None,
) -> int:
    """Run a single prune pass over ``targets``. Returns the number of files removed."""
    hours = retention_hours() if max_age_hours is None else max_age_hours
    if hours <= 0:
        return 0
    cutoff = time.time() - hours * 3600.0
    protected = protected_names or set()
    total = 0
    for target in targets:
        total += _prune_dir(target, cutoff, recurse=True, protected=protected)
    if total:
        logger.info(
            "[jobdoc] reaper removed %d old artifact(s) (> %.0fh; %d protected).",
            total, hours, len(protected),
        )
    return total


def start_reaper(
    targets: list[Path],
    *,
    interval_seconds: int = 3600,
    protected_names_provider: ProtectedNamesProvider | None = None,
) -> None:
    """
    Start the background reaper once per process. No-op if retention is disabled.

    Runs an immediate pass, then repeats every ``interval_seconds`` in a daemon thread.
    ``protected_names_provider`` (if given) is called once per pass to list filenames that must
    never be pruned — e.g. recordings linked to a saved capture session.
    """
    global _started
    with _lock:
        if _started:
            return
        if retention_hours() <= 0:
            logger.info("[jobdoc] disk reaper disabled (JOBDOC_RETENTION_HOURS=0).")
            _started = True
            return
        _started = True

    def _protected() -> set[str]:
        if not protected_names_provider:
            return set()
        try:
            return protected_names_provider() or set()
        except Exception as exc:  # noqa: BLE001 — never let protection lookup kill the reaper
            logger.debug("protected-names provider failed: %s", exc)
            return set()

    def _loop() -> None:
        while True:
            try:
                reap_once(targets, protected_names=_protected())
            except Exception as exc:  # noqa: BLE001 — never let the reaper die
                logger.debug("reaper pass failed: %s", exc)
            time.sleep(max(60, interval_seconds))

    thread = threading.Thread(target=_loop, name="jobdoc-reaper", daemon=True)
    thread.start()
    logger.info(
        "[jobdoc] disk reaper started (retention=%.0fh, interval=%ds).",
        retention_hours(), interval_seconds,
    )
