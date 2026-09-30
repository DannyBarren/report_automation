"""
Translator adapter: a template written in another tool's vocabulary must still load.

The adapter runs before ``model_validate``, so these tests check both the rewrite itself and
that a template already written against this schema survives untouched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.jobdoc_adapter import BANDS, adapt_template_dict
from utils.template_loader import load_report_template
from utils.template_schema import ReportTemplate

FOREIGN = ROOT / "tests" / "fixtures" / "translator_template.json"
ROOFING = ROOT / "reports" / "roofing_realty_inspection.json"


def _foreign() -> dict:
    return json.loads(FOREIGN.read_text(encoding="utf-8"))


def test_prose_severity_bands_become_badge_tokens() -> None:
    bands = adapt_template_dict(_foreign())["pdf_styling"]["severity_bands"]
    assert bands["informational"] == "green"
    assert bands["minor"] == "green"
    assert bands["low"] == "green"
    assert bands["moderate"] == "yellow"
    assert bands["medium"] == "yellow"
    assert bands["major"] == "red"
    assert bands["safety_critical"] == "red"
    assert bands["high"] == "red"
    # A value that is already a token is left exactly as the author wrote it.
    assert bands["already_a_token"] == "yellow"
    assert set(bands.values()) <= set(BANDS)


def test_no_issue_and_none_always_resolve_to_green() -> None:
    """The writer may emit either spelling, so both must carry a badge."""
    source = _foreign()
    assert "no_issue" not in source["pdf_styling"]["severity_bands"]

    bands = adapt_template_dict(source)["pdf_styling"]["severity_bands"]
    assert bands["no_issue"] == "green"
    assert bands["none"] == "green"


def test_page_number_tokens_are_stripped_but_branding_survives() -> None:
    styling = adapt_template_dict(_foreign())["pdf_styling"]
    for token in ("{page}", "{total_pages}", "{total}", "{PAGE}", "{page_number}"):
        assert token not in styling["page_footer"]
        assert token not in styling["page_header"]
    assert "{business_name}" in styling["page_footer"]
    assert "Confidential" in styling["page_footer"]
    assert "{report_title}" in styling["page_header"]


def test_string_success_criteria_is_wrapped_in_a_list() -> None:
    sections = adapt_template_dict(_foreign())["guidance"]["sections"]
    by_id = {s["section_id"]: s for s in sections}
    assert by_id["overview"]["success_criteria"] == [
        "Whole roof visible from at least two corners."
    ]
    # An author who already used a list keeps their list untouched.
    assert len(by_id["penetrations"]["success_criteria"]) == 2


def test_foreign_template_validates_after_adapting() -> None:
    """The point of the adapter: prose in, a valid ReportTemplate out."""
    template = ReportTemplate.model_validate(adapt_template_dict(_foreign()))
    assert template.report_type == "foreign_translator_sample"
    assert set(template.pdf_styling.severity_bands.values()) <= set(BANDS)
    overview = template.guidance_for_section("overview")
    assert overview and overview.success_criteria == [
        "Whole roof visible from at least two corners."
    ]


def test_adapter_never_raises_on_missing_or_odd_keys() -> None:
    for payload in ({}, {"pdf_styling": None}, {"guidance": {"sections": [None, 7]}},
                    {"pdf_styling": {"severity_bands": "not a dict"}}, {"sections": None}):
        assert isinstance(adapt_template_dict(payload), dict)
    assert adapt_template_dict("not a dict") == "not a dict"  # type: ignore[arg-type]


def test_roofing_template_is_left_alone_in_the_adapted_fields() -> None:
    """Our own template already speaks this schema — adapting must not rewrite its meaning.

    Its footer does carry a page clause, which the PDF renderer has always stripped at render
    time; what must survive is the branding and every severity band the author chose.
    """
    raw = json.loads(ROOFING.read_text(encoding="utf-8"))
    adapted = adapt_template_dict(raw)

    assert adapted["pdf_styling"]["severity_bands"] == raw["pdf_styling"]["severity_bands"]
    assert adapted["pdf_styling"]["page_header"] == raw["pdf_styling"]["page_header"]
    assert "Structured Roofing Systems" in adapted["pdf_styling"]["page_footer"]
    assert "Confidential" in adapted["pdf_styling"]["page_footer"]

    for key in ("guidance", "content_structure", "report_type", "title", "business_name"):
        assert adapted.get(key) == raw.get(key)


def test_loader_runs_the_adapter_before_validating() -> None:
    """``load_report_template`` must produce a valid template for both templates."""
    roofing = load_report_template(ROOFING)
    assert roofing.report_type == "roofing_realty_inspection"
    assert set(roofing.pdf_styling.severity_bands.values()) <= set(BANDS)

    foreign = load_report_template(FOREIGN)
    assert foreign.report_type == "foreign_translator_sample"
    assert foreign.pdf_styling.severity_bands["major"] == "red"
