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

import logging
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

# CrewAI interpolates task descriptions/agent prompts by scanning for single-brace
# ``{identifier}`` tokens and RAISES ("Missing required template variable … not found in
# inputs") if one is absent from its inputs dict. Our prompts embed template/transcript JSON
# verbatim, and some template JSON carries PDF page-furniture placeholders like ``{page}``,
# ``{total_pages}``, ``{report_title}`` (used only by the PDF renderer). Those bare tokens
# make CrewAI abort the whole crew before any stage runs.
#
# We must eliminate the token so it survives EVERY CrewAI version. Inserting spaces
# (``{ page }``) is NOT enough — CrewAI >= 0.70 interpolation is whitespace-tolerant and still
# treats ``{ page }`` as a required variable. Instead we REMOVE the braces entirely, rewriting
# a lone ``{word}`` (letters/digits/underscore/dot, optional inner whitespace) to ``[word]``.
# ``[word]`` can never be a format/interpolation variable under any version, stays human/LLM
# readable, and only affects the prompt COPY of the template (the PDF renderer uses the real
# ReportTemplate object, never this string). Real JSON braces (``{"key": ...}``) never match
# because the token body must be a bare identifier (no quotes/colons/commas/spaces).
# Matches a lone ``{word}`` / ``{ word }`` token: an optional-whitespace-wrapped bare identifier
# (letters/digits/underscore/dot). Real JSON objects never match because the body must be a bare
# identifier — ``{"key": ...}`` starts with a quote, ``{1,2}`` starts with a digit-run+comma, etc.
_CREW_BRACE_TOKEN_RE = re.compile(r"\{\s*([A-Za-z_][\w.]*)\s*\}")


def find_crew_brace_tokens(text: str) -> list[str]:
    """Return the identifiers of any lone ``{word}`` tokens still present (for diagnostics)."""
    return _CREW_BRACE_TOKEN_RE.findall(text or "")


def neutralize_crew_brace_tokens(text: str, *, label: str = "") -> str:
    """Strip lone ``{word}`` tokens to ``[word]`` so CrewAI never treats them as variables.

    Aggressive + bounded fixpoint: runs until stable so that any already-spaced ``{ page }``
    left by an older build (or nested transforms) is also removed. Logs the count/identifiers of
    neutralized tokens so every report generation records what furniture placeholders were
    defanged (``page``, ``total_pages``, ``report_title``, …).
    """
    if not text:
        return text
    tokens = find_crew_brace_tokens(text)
    prev = None
    out = text
    # Identifier-only tokens can't regenerate, so this converges in 1-2 passes.
    for _ in range(5):
        out = _CREW_BRACE_TOKEN_RE.sub(r"[\1]", out)
        if out == prev:
            break
        prev = out
    if tokens:
        logger.info(
            "[prompt] neutralized %d brace token(s)%s: %s",
            len(tokens),
            f" in {label}" if label else "",
            sorted(set(tokens))[:20],
        )
    return out


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
    rendered = load_prompt(name, subfolder="v2", **defaults)
    # v2 prompts are handed to CrewAI Task descriptions — strip any stray ``{var}`` tokens the
    # injected JSON may contain so CrewAI's input interpolation cannot abort the crew.
    return neutralize_crew_brace_tokens(rendered, label=f"v2/{name}")
