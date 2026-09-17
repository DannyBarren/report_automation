"""
job_info_summary.py — Generate and store the LLM-written "Job Info Summary" for a session.

The Job Info Summary is a clean, professional client-handoff note synthesized from:
    * ``SessionSummary.structured_data`` — the structured main-report data, and
    * ``MiscData`` — the always-present supplemental step (raw transcript + summarized data).

Design notes
------------
    * Generation is **idempotent**: an existing summary is reused unless ``force=True``.
    * It never raises into the request/report flow — failures are logged and leave the row
      absent so the UI can offer a retry. The main report is unaffected.
    * The OpenAI key is passed explicitly to the model call so background generation does
      not depend on request-scoped ``os.environ`` (which is restored at request teardown).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Reuse the same OpenAI model the crew uses (litellm-style model id, e.g. "openai/gpt-4o").
try:
    from utils.crew_llm import get_openai_model as _get_model

    _MODEL = _get_model()
except Exception:  # noqa: BLE001 — extremely defensive; keep a sane default
    _MODEL = "openai/" + (os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o")

_MAX_TRANSCRIPT_CHARS = 8000
_MAX_STRUCTURED_CHARS = 8000


def resolve_openai_key(session_token: str | None = None) -> str | None:
    """Best-effort resolve of the effective OpenAI key (session key wins, else env).

    Robust to alias-spelled secrets (e.g. ``OPEN_AI_API_KEY`` on Modal) via
    ``credentials.normalize_provider_env``.
    """
    try:
        from utils import credentials

        if session_token:
            key = (credentials.keys_for_session(session_token) or {}).get("OPENAI_API_KEY")
            if key:
                return key
        credentials.normalize_provider_env()
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("OPENAI_API_KEY") or None


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…[truncated]"


def _pretty_json(obj: Any, limit: int) -> str:
    try:
        return _truncate(json.dumps(obj, indent=2, ensure_ascii=False, default=str), limit)
    except (TypeError, ValueError):
        return _truncate(str(obj), limit)


def build_prompt(
    *,
    session_meta: dict[str, Any],
    client_meta: dict[str, Any],
    structured_data: dict[str, Any] | None,
    misc_raw_transcript: str,
    misc_summarized: dict[str, Any] | None,
) -> str:
    """Assemble the user prompt from all captured data for this session."""
    lines: list[str] = []
    lines.append("JOB / CLIENT CONTEXT")
    lines.append(f"- Client: {client_meta.get('label') or client_meta.get('name') or 'Unknown'}")
    if client_meta.get("email"):
        lines.append(f"- Client email: {client_meta['email']}")
    if session_meta.get("job_address"):
        lines.append(f"- Job / property address: {session_meta['job_address']}")
    if session_meta.get("inspector_name"):
        lines.append(f"- Inspector: {session_meta['inspector_name']}")
    if session_meta.get("inspection_date"):
        lines.append(f"- Date of inspection: {session_meta['inspection_date']}")
    if session_meta.get("weather"):
        lines.append(f"- Weather / conditions: {session_meta['weather']}")
    if session_meta.get("report_type"):
        lines.append(f"- Primary report type: {session_meta['report_type']}")

    lines.append("")
    lines.append("STRUCTURED REPORT DATA (JSON — the documented sections and findings):")
    lines.append(_pretty_json(structured_data or {}, _MAX_STRUCTURED_CHARS))

    lines.append("")
    lines.append("SUPPLEMENTAL / MISC — SUMMARIZED (JSON — pricing, requests, follow-ups):")
    lines.append(_pretty_json(misc_summarized or {}, _MAX_STRUCTURED_CHARS))

    lines.append("")
    lines.append("SUPPLEMENTAL / MISC — RAW NARRATION (verbatim, may be noisy):")
    lines.append(_truncate(misc_raw_transcript, _MAX_TRANSCRIPT_CHARS) or "(none captured)")

    return "\n".join(lines)


_SYSTEM_PROMPT = (
    "You are a senior field-operations writer producing a concise, professional "
    "'Job Info Summary' — a client handoff note that a business owner could send directly "
    "to their client or keep for their records. Write in clear, neutral, professional prose. "
    "Only use facts present in the provided data; never invent findings, prices, or measurements. "
    "If information is missing, omit it gracefully rather than guessing. "
    "Structure the note with these plain-text headings, each on its own line, followed by "
    "content (use short paragraphs and simple '- ' bullets where helpful):\n"
    "  Overview\n"
    "  Work & Observations\n"
    "  Key Findings\n"
    "  Supplemental Notes\n"
    "  Recommended Next Steps\n"
    "Do not use Markdown symbols like #, *, or backticks. Keep it under ~450 words."
)


def _call_openai(prompt: str, *, api_key: str, model: str = _MODEL) -> str:
    """Single-shot OpenAI call; returns clean text. Raises on hard failure.

    Prefers litellm (bundled with CrewAI, matches the crew's model id). Falls back to the
    ``openai`` SDK if litellm is unavailable for any reason.
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    try:
        import litellm

        resp = litellm.completion(
            model=model,
            messages=messages,
            api_key=api_key,
            max_tokens=1200,
            temperature=0.3,
            timeout=90,
        )
        text = (resp.choices[0].message.content or "").strip()
    except ImportError:
        from openai import OpenAI

        # The OpenAI SDK expects the bare model id (no "openai/" litellm prefix).
        bare_model = model.split("/", 1)[-1]
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=bare_model,
            messages=messages,
            max_tokens=1200,
            temperature=0.3,
            timeout=90,
        )
        text = (resp.choices[0].message.content or "").strip()

    if not text:
        raise RuntimeError("OpenAI returned an empty Job Info Summary.")
    return text


def generate_summary_text(
    *,
    session_meta: dict[str, Any],
    client_meta: dict[str, Any],
    structured_data: dict[str, Any] | None,
    misc_raw_transcript: str,
    misc_summarized: dict[str, Any] | None,
    api_key: str,
    model: str = _MODEL,
) -> str:
    """Build the prompt and return the LLM-written summary text."""
    prompt = build_prompt(
        session_meta=session_meta,
        client_meta=client_meta,
        structured_data=structured_data,
        misc_raw_transcript=misc_raw_transcript,
        misc_summarized=misc_summarized,
    )
    return _call_openai(prompt, api_key=api_key, model=model)


# ---------------------------------------------------------------------------
# PDF rendering (simple, clean, standalone — reuses the report PDF writer)
# ---------------------------------------------------------------------------

def _summary_html(summary_text: str, *, title: str, subtitle: str, generated_on: str) -> str:
    """Build a clean single-column HTML body for the Job Info Summary PDF."""
    import html as _html

    body_blocks: list[str] = []
    known_headings = {
        "overview", "work & observations", "key findings",
        "supplemental notes", "recommended next steps",
    }
    for raw_line in (summary_text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        esc = _html.escape(line.strip())
        if line.strip().lower().rstrip(":") in known_headings:
            body_blocks.append(f'<h2 class="ji-h2">{esc.rstrip(":")}</h2>')
        elif line.lstrip().startswith(("- ", "• ")):
            body_blocks.append(f'<p class="ji-bullet">{esc.lstrip("-• ").strip()}</p>')
        else:
            body_blocks.append(f'<p class="ji-p">{esc}</p>')
    body = "\n".join(body_blocks) or '<p class="ji-p">No summary content available.</p>'

    return f"""
<style>
  @page {{ size: A4; margin: 20mm; }}
  .ji-doc {{ font-family: 'Helvetica Neue', Arial, sans-serif; color: #0f172a; font-size: 11pt; line-height: 1.55; }}
  .ji-head {{ border-bottom: 3px solid #0ea5e9; padding-bottom: 12px; margin-bottom: 18px; }}
  .ji-brand {{ font-size: 9pt; letter-spacing: 0.08em; text-transform: uppercase; color: #0ea5e9; font-weight: 700; }}
  .ji-title {{ font-size: 22pt; font-weight: 700; margin: 6px 0 2px; color: #0f172a; }}
  .ji-sub {{ font-size: 11pt; color: #475569; margin: 0; }}
  .ji-meta {{ font-size: 8.5pt; color: #94a3b8; margin-top: 8px; }}
  .ji-h2 {{ font-size: 12.5pt; color: #0369a1; font-weight: 700; margin: 16px 0 4px; }}
  .ji-p {{ margin: 0 0 8px; text-align: justify; }}
  .ji-bullet {{ margin: 0 0 4px 14px; position: relative; }}
  .ji-bullet::before {{ content: "•"; color: #0ea5e9; position: absolute; left: -14px; }}
  .ji-foot {{ margin-top: 24px; padding-top: 10px; border-top: 1px solid #e2e8f0; font-size: 8pt; color: #94a3b8; }}
</style>
<div class="ji-doc">
  <div class="ji-head">
    <div class="ji-brand">GenerSwift · Job Info Summary</div>
    <h1 class="ji-title">{_html.escape(title)}</h1>
    <p class="ji-sub">{_html.escape(subtitle)}</p>
    <p class="ji-meta">Generated {_html.escape(generated_on)}</p>
  </div>
  {body}
  <p class="ji-foot">This summary was generated by GenerSwift from a guided capture session. Verify time-sensitive details against the full field report and source recording.</p>
</div>
"""


def render_summary_pdf(
    summary_text: str,
    *,
    title: str,
    subtitle: str,
    generated_on: str,
    output_path: Path,
    base_dir: Path,
) -> Path:
    """Render the summary to a simple, clean PDF via the shared WeasyPrint writer."""
    from utils.report_pdf import write_pdf

    html_inner = _summary_html(
        summary_text, title=title, subtitle=subtitle, generated_on=generated_on
    )
    write_pdf(None, html_inner, base_dir=base_dir, output_path=output_path)
    return output_path
