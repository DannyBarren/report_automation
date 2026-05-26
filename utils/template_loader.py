"""
template_loader.py — Load and validate ``reports/*.json`` templates.
"""

from __future__ import annotations

import json
from pathlib import Path

from utils.template_schema import ReportTemplate

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def load_report_template(path: Path) -> ReportTemplate:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ReportTemplate.model_validate(data)


def load_all_report_templates(reports_dir: Path | None = None) -> list[ReportTemplate]:
    root = reports_dir or REPORTS_DIR
    templates: list[ReportTemplate] = []
    if not root.is_dir():
        return templates
    for path in sorted(root.glob("*.json")):
        try:
            templates.append(load_report_template(path))
        except Exception as exc:  # noqa: BLE001
            print(f"[jobdoc] Skipping template {path.name}: {exc}")
    return sorted(
        templates,
        key=lambda t: (not getattr(t, "featured", False), t.title.lower()),
    )


def templates_by_type(reports_dir: Path | None = None) -> dict[str, ReportTemplate]:
    return {t.report_type: t for t in load_all_report_templates(reports_dir)}
