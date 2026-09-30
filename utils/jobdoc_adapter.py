"""
jobdoc_adapter.py — Translate a foreign report template into this schema's vocabulary.

Templates authored elsewhere (or exported by another JobDoc tool) are usually only a few
idioms away from validating against ``utils.template_schema.ReportTemplate``:

    * ``pdf_styling.severity_bands`` uses prose ("Minor", "critical") where we need the
      three badge tokens ``green`` / ``yellow`` / ``red``.
    * ``page_footer`` / ``page_header`` embed page-number placeholders that WeasyPrint fills
      from CSS counters, so a literal ``{page}`` would print as text.
    * ``success_criteria`` is a single sentence where the schema wants a list.

``adapt_template_dict`` rewrites exactly those things on a COPY and is deliberately total: it
never raises, and anything it does not recognize is left untouched for the validator to judge.
Templates already written against this schema pass through unchanged.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["adapt_template_dict", "BANDS", "PAGE_NUMBER_TOKENS"]

BANDS = ("green", "yellow", "red")

# Prose severity words → badge band. Checked as substrings so "no_issue", "No Issues Found",
# and "minor / low" all land on the same band.
_BAND_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("safety_critical", "red"),
    ("critical", "red"),
    ("urgent", "red"),
    ("severe", "red"),
    ("major", "red"),
    ("high", "red"),
    ("moderate", "yellow"),
    ("medium", "yellow"),
    ("attention", "yellow"),
    ("no_issue", "green"),
    ("no issue", "green"),
    ("noissue", "green"),
    ("minor", "green"),
    ("informational", "green"),
    ("info", "green"),
    ("low", "green"),
    ("none", "green"),
    ("ok", "green"),
    ("satisfactory", "green"),
)

# Page-number placeholders the PDF stylesheet fills from CSS counters. Left in the string they
# would print literally. Branding tokens like {business_name} / {report_title} are NOT stripped.
PAGE_NUMBER_TOKENS = ("{page}", "{total_pages}", "{total}", "{PAGE}", "{page_number}")


def _band_for(value: Any) -> str | None:
    """Map a prose severity band to ``green|yellow|red``, or ``None`` if nothing matches."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in BANDS:
        return None  # already a token — leave the author's value alone
    normalized = text.replace("-", "_").replace(" ", "_")
    for keyword, band in _BAND_KEYWORDS:
        if keyword.replace(" ", "_") in normalized:
            return band
    return None


def _adapt_severity_bands(styling: dict[str, Any]) -> None:
    bands = styling.get("severity_bands")
    if not isinstance(bands, dict):
        return
    for key, value in list(bands.items()):
        mapped = _band_for(value)
        if mapped:
            bands[key] = mapped
    # The writer may emit either spelling for "nothing wrong here"; both must resolve to a badge.
    for key in ("no_issue", "none"):
        if not isinstance(bands.get(key), str) or bands.get(key) not in BANDS:
            bands[key] = "green"


def _strip_page_tokens(value: Any) -> Any:
    """Remove page-number placeholders and tidy the separators they leave behind."""
    if not isinstance(value, str):
        return value
    out = value
    for token in PAGE_NUMBER_TOKENS:
        out = out.replace(token, "")
    # "Page  of " and a trailing "·" are what stripping typically leaves; clean them up.
    for filler in ("Page of", "page of", "Page  of", "of  of"):
        out = out.replace(filler, "")
    out = " ".join(out.split())
    return out.strip(" ·-|,").strip()


def _adapt_success_criteria(container: dict[str, Any]) -> None:
    raw = container.get("success_criteria")
    if isinstance(raw, str):
        text = raw.strip()
        container["success_criteria"] = [text] if text else []


def adapt_template_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Return a schema-compatible copy of a raw template dict. Never raises."""
    if not isinstance(data, dict):
        return data
    try:
        out = copy.deepcopy(data)
    except Exception:  # noqa: BLE001 — an un-copyable blob is the validator's problem, not ours
        return data

    try:
        styling = out.get("pdf_styling")
        if isinstance(styling, dict):
            _adapt_severity_bands(styling)
            for key in ("page_footer", "page_header"):
                if key in styling:
                    styling[key] = _strip_page_tokens(styling[key])

        guidance = out.get("guidance")
        if isinstance(guidance, dict):
            _adapt_success_criteria(guidance)
            for section in guidance.get("sections") or []:
                if isinstance(section, dict):
                    _adapt_success_criteria(section)

        for section in (out.get("sections") or []):
            if isinstance(section, dict):
                _adapt_success_criteria(section)
    except Exception as exc:  # noqa: BLE001 — a partial adaptation still beats none
        logger.debug("template adapter left input partially unchanged: %s", exc)
    return out
