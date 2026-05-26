"""
prompt_utils.py — Load and render prompt templates from the `prompts/` folder.

How this fits the larger system
-------------------------------
    - Prompt files are plain text with **Jinja2** placeholders written as ``{{ variable }}``.
    - At runtime we **never** treat prompts as f-strings in Python: that would mix user
      data into Python code and invite injection mistakes. Instead, ``load_prompt()`` reads
      the file and renders it as a Jinja2 template with explicit ``kwargs``.
    - Agents (CrewAI) and Flask routes share the same loader, so wording stays in one
      place under ``prompts/*.txt`` and code only supplies structured context.

Why Jinja2 for both prompts and Flask?
    - Jinja2 is already a dependency (Flask templates). Re-using it keeps one templating
      mental model: ``{{ }}`` in ``prompts/foo.txt`` matches how designers think about
      placeholders in ``templates/*.html``.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader


def load_prompt(name: str, *, subfolder: str = "", **kwargs: object) -> str:
    """
    Load a prompt from prompts/{subfolder}/{name}.txt safely as plain text.

    Args:
        name: File stem or ``foo.txt``.
        subfolder: Optional subdirectory under ``prompts/`` (e.g. ``v2``).
    """
    stem = name[:-4] if name.endswith(".txt") else name
    base = Path("prompts") / subfolder if subfolder else Path("prompts")
    env = Environment(
        loader=FileSystemLoader(str(base)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = env.get_template(f"{stem}.txt")
    return template.render(**kwargs)


def load_prompt_v2(name: str, **kwargs: object) -> str:
    """Load a prompt from ``prompts/v2/{name}.txt`` with shared v2 policy/rubric injected."""
    from crew.models_v2 import (
        CONFIDENCE_RUBRIC_FOR_PROMPTS,
        LOW_CONFIDENCE_THRESHOLD,
        MIN_SUMMARY_WORDS,
        NARRATION_ONLY_RULES,
    )

    policy_path = Path("prompts/v2/_narration_policy.txt")
    if policy_path.is_file():
        narration_policy = policy_path.read_text(encoding="utf-8").strip()
    else:
        narration_policy = NARRATION_ONLY_RULES

    ssot_path = Path("prompts/v2/_template_ssot.txt")
    template_ssot_policy = (
        ssot_path.read_text(encoding="utf-8").strip()
        if ssot_path.is_file()
        else "Follow ReportTemplate JSON as the single source of truth."
    )
    narration_policy = f"{narration_policy}\n\n{template_ssot_policy}"

    defaults: dict[str, object] = {
        "narration_policy": narration_policy,
        "template_ssot_policy": template_ssot_policy,
        "confidence_rubric": CONFIDENCE_RUBRIC_FOR_PROMPTS.strip(),
        "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
        "min_summary_words": MIN_SUMMARY_WORDS,
    }
    defaults.update(kwargs)
    return load_prompt(name, subfolder="v2", **defaults)
