"""
template_validator.py — STRICT GOLD-STANDARD gate for ReportTemplate JSON (authoring / CI).

Two layers, on purpose
----------------------
* Runtime (``utils/template_schema.py``): TOLERANT. It auto-upgrades legacy templates and
  auto-repairs small slips (synthesizes a missing counterpart section, reorders content to
  match guidance) so the live app never loses a template.
* Authoring / CI (THIS module): STRICT. It inspects the RAW JSON (not the auto-repaired model)
  and FAILS HARD, listing every way a template falls short of the gold standard. Because it
  reads the raw dict, the model's auto-repair can never hide an incomplete authored file.

A template only "freezes" once it passes this validator.

Usage
-----
    python -m utils.template_validator                      # validate reports/ (exit 1 on fail)
    python -m utils.template_validator reports/foo.json     # validate one file
    from utils.template_validator import validate_template_file, assert_raw_template_gold_standard
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from utils.template_schema import ReportTemplate

# Allowed document classes (kept in lock-step with template_schema.DocumentClass).
_ALLOWED_DOCUMENT_CLASSES = {
    "inspection_report",
    "estimate",
    "invoice",
    "compliance",
    "work_order",
    "custom",
}

# Required, must-be-non-empty string fields per guidance section (day-one worker fields).
_GUIDANCE_STR_FIELDS = ("title", "voice_prompt", "on_screen_text", "worker_instructions")
# Required, must-be-non-empty list fields per guidance section.
_GUIDANCE_LIST_FIELDS = ("success_criteria", "suggested_phrases")
# Required, must-be-non-empty string fields per content section (writer contract).
_CONTENT_STR_FIELDS = ("title", "writer_instructions", "default_text")


class TemplateValidationError(ValueError):
    """Raised when a template violates the gold-standard contract. Message lists all issues."""


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_nonempty_str_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(_is_nonempty_str(v) for v in value)
    )


def raw_template_issues(data: dict) -> list[str]:
    """Return every gold-standard violation found in the RAW template dict (no auto-repair)."""
    issues: list[str] = []
    rt = str(data.get("report_type") or "").strip() or "<no report_type>"

    # 0) Parses cleanly under the (tolerant) runtime model — catches type errors early.
    try:
        ReportTemplate.model_validate(data)
    except ValidationError as exc:
        issues.append(f"[{rt}] does not parse under ReportTemplate: {exc.error_count()} error(s): {exc}")
        return issues  # nothing else is meaningful if it won't parse

    # 1) Top-level fields.
    if not _is_nonempty_str(data.get("report_type")):
        issues.append(f"[{rt}] report_type is empty.")
    if not _is_nonempty_str(data.get("title")):
        issues.append(f"[{rt}] title is empty.")
    sv = data.get("schema_version")
    if not isinstance(sv, int) or sv < 3:
        issues.append(f"[{rt}] schema_version must be an int >= 3 (gold standard); got {sv!r}.")
    dc = data.get("document_class")
    if dc not in _ALLOWED_DOCUMENT_CLASSES:
        issues.append(
            f"[{rt}] document_class must be one of {sorted(_ALLOWED_DOCUMENT_CLASSES)}; got {dc!r}."
        )

    # 2) pdf_styling must be an explicit object (not left to defaults for a production template).
    if not isinstance(data.get("pdf_styling"), dict) or not data["pdf_styling"]:
        issues.append(f"[{rt}] pdf_styling must be a populated object.")

    guidance = data.get("guidance")
    content = data.get("content_structure")
    if not isinstance(guidance, dict) or not guidance.get("sections"):
        issues.append(f"[{rt}] guidance.sections is missing or empty.")
    if not isinstance(content, dict) or not content.get("sections"):
        issues.append(f"[{rt}] content_structure.sections is missing or empty.")
    if issues and (not isinstance(guidance, dict) or not isinstance(content, dict)):
        return issues

    # 3) Guidance-level fields.
    for key in ("intro_script", "outro_script", "identification_phrase"):
        if not _is_nonempty_str(guidance.get(key)):
            issues.append(f"[{rt}] guidance.{key} is empty.")
    etm = guidance.get("estimated_total_minutes")
    if not isinstance(etm, int) or etm <= 0:
        issues.append(f"[{rt}] guidance.estimated_total_minutes must be a positive int; got {etm!r}.")

    g_sections = guidance.get("sections") or []
    c_sections = content.get("sections") or []
    g_ids = [s.get("section_id") for s in g_sections]
    c_ids = [s.get("section_id") for s in c_sections]

    # 4) STRICT mirroring (raw sets must be identical).
    g_set, c_set = set(g_ids), set(c_ids)
    for missing in sorted(x for x in (g_set - c_set) if x is not None):
        issues.append(
            f"[{rt}] section '{missing}' is in guidance but MISSING from content_structure "
            "(strict mirroring violated)."
        )
    for missing in sorted(x for x in (c_set - g_set) if x is not None):
        issues.append(
            f"[{rt}] section '{missing}' is in content_structure but MISSING from guidance "
            "(strict mirroring violated)."
        )
    if any(x is None for x in g_ids):
        issues.append(f"[{rt}] every guidance section must declare a section_id.")
    if any(x is None for x in c_ids):
        issues.append(f"[{rt}] every content_structure section must declare a section_id.")
    if len([x for x in g_ids if x is not None]) != len(g_set - {None}):
        issues.append(f"[{rt}] duplicate section_id in guidance: {g_ids}.")
    if len([x for x in c_ids if x is not None]) != len(c_set - {None}):
        issues.append(f"[{rt}] duplicate section_id in content_structure: {c_ids}.")

    # 5) capture_order present, integer, and unique across guidance.
    orders = [s.get("capture_order") for s in g_sections]
    if any(not isinstance(o, int) for o in orders):
        issues.append(
            f"[{rt}] every guidance section must set an explicit integer capture_order; got {orders}."
        )
    elif len(set(orders)) != len(orders):
        issues.append(f"[{rt}] capture_order values must be unique; got {orders}.")

    # 6) Day-one worker fields on every guidance section.
    for s in g_sections:
        gid = s.get("section_id") or "<no id>"
        for key in _GUIDANCE_STR_FIELDS:
            if not _is_nonempty_str(s.get(key)):
                issues.append(f"[{rt}:{gid}] guidance field '{key}' is empty/missing.")
        for key in _GUIDANCE_LIST_FIELDS:
            if not _is_nonempty_str_list(s.get(key)):
                issues.append(
                    f"[{rt}:{gid}] guidance field '{key}' must be a non-empty list of strings."
                )
        mm = s.get("min_marks")
        if not isinstance(mm, int) or mm < 0:
            issues.append(f"[{rt}:{gid}] min_marks must be an int >= 0; got {mm!r}.")
        elif s.get("required", True) and mm < 1:
            issues.append(
                f"[{rt}:{gid}] a required section must expect >= 1 mark (min_marks >= 1); got {mm}."
            )
        es = s.get("estimated_seconds")
        if not isinstance(es, int) or es <= 0:
            issues.append(f"[{rt}:{gid}] estimated_seconds must be a positive int; got {es!r}.")

    # 7) Writer contract on every content section.
    for s in c_sections:
        cid = s.get("section_id") or "<no id>"
        for key in _CONTENT_STR_FIELDS:
            if not _is_nonempty_str(s.get(key)):
                issues.append(f"[{rt}:{cid}] content field '{key}' is empty/missing.")
        fields = s.get("fields")
        if not _is_nonempty_str_list(fields):
            issues.append(f"[{rt}:{cid}] fields must be a non-empty list of key strings.")
        msw = s.get("min_summary_words")
        if not isinstance(msw, int) or msw < 1:
            issues.append(f"[{rt}:{cid}] min_summary_words must be an int >= 1; got {msw!r}.")
        if not isinstance(s.get("image_placement"), dict) or not s["image_placement"]:
            issues.append(f"[{rt}:{cid}] image_placement must be a populated object.")
        if not isinstance(s.get("layout_hints"), dict) or not s["layout_hints"]:
            issues.append(f"[{rt}:{cid}] layout_hints must be a populated object.")
        if s.get("show_severity_badge") and isinstance(fields, list) and "severity" not in fields:
            issues.append(
                f"[{rt}:{cid}] show_severity_badge is true but 'severity' is not in fields — "
                "the badge would have no value to render."
            )

    return issues


def assert_raw_template_gold_standard(data: dict, *, label: str = "") -> ReportTemplate:
    """Raise ``TemplateValidationError`` (listing every issue) unless ``data`` is gold standard.

    Returns the parsed ``ReportTemplate`` on success.
    """
    issues = raw_template_issues(data)
    if issues:
        rt = data.get("report_type") or label or "<template>"
        raise TemplateValidationError(
            f"Template '{rt}'{f' ({label})' if label else ''} failed gold-standard validation "
            f"with {len(issues)} issue(s):\n  - " + "\n  - ".join(issues)
        )
    return ReportTemplate.model_validate(data)


# Backwards-compatible alias (older callers used the model-based name).
def assert_template_gold_standard(tpl: ReportTemplate) -> None:
    """Strict check against a parsed template by re-dumping to raw first."""
    assert_raw_template_gold_standard(tpl.model_dump(), label=tpl.report_type)


def validate_template_dict(data: dict) -> ReportTemplate:
    """Validate a raw template dict → parsed template (raises on any gold-standard violation)."""
    return assert_raw_template_gold_standard(data)


def validate_template_file(path: str | Path) -> ReportTemplate:
    """Load + validate a single template JSON file against the gold standard (raises on failure)."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        return assert_raw_template_gold_standard(data, label=path.name)
    except TemplateValidationError as exc:
        raise TemplateValidationError(f"{path.name}: {exc}") from exc


def validate_reports_dir(reports_dir: str | Path) -> dict[str, list[str]]:
    """Validate every ``*.json`` in a directory. Returns ``{filename: [issues]}`` (empty = pass)."""
    reports_dir = Path(reports_dir)
    results: dict[str, list[str]] = {}
    for path in sorted(reports_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        results[path.name] = raw_template_issues(data)
    return results


def _main(argv: list[str]) -> int:
    if not argv:
        from utils.template_loader import REPORTS_DIR

        targets = [REPORTS_DIR]
    else:
        targets = [Path(a) for a in argv]

    failed = 0
    checked = 0
    for target in targets:
        files = sorted(target.glob("*.json")) if target.is_dir() else [target]
        for path in files:
            checked += 1
            issues = raw_template_issues(json.loads(path.read_text(encoding="utf-8")))
            if issues:
                failed += 1
                print(f"FAIL {path.name}:")
                for i in issues:
                    print(f"   - {i}")
            else:
                print(f"PASS {path.name}")
    print(f"\n{checked - failed}/{checked} template(s) passed the strict gold-standard validator.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
