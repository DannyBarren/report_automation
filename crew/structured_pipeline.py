"""
structured_pipeline.py — Deterministic transcript→sections→report transforms (Step 3).

Why this module exists alongside Crew
--------------------------------------
    CrewAI agents run with live LLMs for transparency, but the **authoritative** timestamps,
    summaries, and JSON shapes for PDF export come from this Python layer so “Mark this.”
    detection stays deterministic and PDF output does not depend on LLM variance.

Mark-this workflow
------------------
    1. Real ASR emits ``segments`` with text, ``start``, ``end``. We filter
       segments whose text contains the trigger (case-insensitive).
    2. Evidence is assigned to a section by **identity, not list position**: a MARK tap's own
       ``section_id`` wins, then the section's ``step_events`` time window, then a spoken cue
       within ``_CUE_TAP_MERGE_SEC`` of one of that section's taps. Ordinal *k*-th-mark →
       *k*-th-section mapping is a legacy fallback used ONLY when no tap carries a
       ``section_id`` — position-based matching swaps photos whenever the inspector marks out
       of template order or marks twice in one area.
    3. ``timestamp_sec`` = segment start (when the cue begins). ``image_timestamp_sec`` =
       clamped midpoint of the segment (or start + 0.5s) so the still frame shows what was
       being described after the phrase.
    4. ``summary`` is built from the full narration window after each cue (same segment
       through the segment before the next cue), in professional 45–80 word form.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from utils.schemas import (
    MIN_SUMMARY_WORDS,
    FinalReportOutput,
    MatchedSection,
    QAReport,
    QAFinding,
    ReportBundle,
    ReportSection,
    ReportSectionOutput,
    ReportTemplate,
    SectionFrame,
)

logger = logging.getLogger(__name__)

TRIGGER_RE = re.compile(r"mark\s*this\.?", re.IGNORECASE)

# Multi-frame limits. The per-section ceiling is now high and configurable (env
# JOBDOC_MAX_FRAMES_PER_SECTION, default 80) so an inspector can mark many items in one area
# without losing images. Near-duplicate anchors closer than this gap are still merged so we
# don't store almost-identical stills.
_FRAME_MIN_GAP_SEC = 1.2

# A spoken cue that no step window claims may only be adopted by a section whose own MARK tap
# is this close to it. Beyond that the cue belongs to no section we can prove, and handing it to
# the next empty section is what previously printed one area's still under another's heading.
_CUE_TAP_MERGE_SEC = 2.0


def _max_frames_per_section() -> int:
    from utils.pipeline_config import max_frames_per_section

    return max_frames_per_section()


def _frame_context_window() -> tuple[float, float]:
    from utils.pipeline_config import frame_context_window_seconds

    return frame_context_window_seconds()


def flatten_sections_in_order(templates: list[ReportTemplate]) -> list[tuple[ReportTemplate, ReportSection]]:
    """Return (template, section) pairs in guidance capture order when available."""
    out: list[tuple[ReportTemplate, ReportSection]] = []
    for tpl in templates:
        if tpl.guidance and tpl.guidance.sections:
            sec_by_id = {s.id: s for s in tpl.sections}
            for g in tpl.sorted_guidance_sections():
                sec = sec_by_id.get(g.section_id)
                if sec:
                    out.append((tpl, sec))
            continue
        for sec in tpl.sections:
            out.append((tpl, sec))
    return out


def _strip_trigger_prefix(text: str) -> str:
    """Remove leading “Mark this.” (any casing) and leading punctuation/space."""
    t = text.strip()
    t = TRIGGER_RE.sub("", t, count=1).strip()
    t = t.lstrip(".,;: ").strip()
    return t


def _extract_narration_between_cues(
    segments: list[dict[str, Any]],
    cue_indexes: list[int],
    cue_pos: int,
) -> str:
    """Return full narration after cue `cue_pos` until the next cue or transcript end."""
    start_idx = cue_indexes[cue_pos]
    end_idx = cue_indexes[cue_pos + 1] if cue_pos + 1 < len(cue_indexes) else len(segments)

    narration_chunks: list[str] = []
    for idx in range(start_idx, end_idx):
        raw_text = str(segments[idx].get("text", "")).strip()
        if not raw_text:
            continue
        if idx == start_idx:
            text_after_cue = _strip_trigger_prefix(raw_text)
            if text_after_cue:
                narration_chunks.append(text_after_cue)
        else:
            narration_chunks.append(raw_text)
    return " ".join(chunk for chunk in narration_chunks if chunk).strip()


def _clean_fragment(text: str) -> str:
    """Tidy a raw narration fragment into a clean sentence (no pipeline scaffolding)."""
    t = TRIGGER_RE.sub("", (text or "").strip()).strip()
    t = t.lstrip(".,;:- ").strip()
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    if t and t[-1] not in ".!?":
        t += "."
    return t


def _professional_summary_from_narration(
    narration: str, section_name: str, default_text: str = ""
) -> str:
    """Deterministic fallback summary in clean inspector prose (no filler scaffolding).

    Used only when the LLM writer stage fails; describes the subject directly rather than
    restating that narration was provided.
    """
    words = narration.split()
    if not words:
        return _summary_visited_no_narration(section_name, default_text)

    compact = _clean_fragment(" ".join(words[:70]))
    summary = (
        f"{compact} These conditions were documented for {section_name.lower()} during the "
        f"guided walkthrough. Where the recording did not capture full detail, the finding "
        f"should be confirmed on site and any corrective action scoped accordingly."
    )
    if len(summary.split()) > 80:
        summary = " ".join(summary.split()[:80]).rstrip(" ,;:.") + "."
    return summary


def _fallback_multiframe_summary(
    section_name: str, frames: list[SectionFrame], default_text: str = ""
) -> str:
    """Deterministic fallback for a section with several marks — enumerate distinct findings."""
    findings: list[str] = []
    for i, fr in enumerate(frames, 1):
        detail = (fr.note or "").strip() or (fr.narration or "").strip() or (fr.context_narration or "").strip()
        detail = _clean_fragment(detail)
        if detail:
            findings.append(f"({i}) {detail}")
    if not findings:
        return _summary_visited_no_narration(section_name, default_text)
    body = " ".join(findings)
    summary = (
        f"{len(findings)} location(s) were marked in {section_name.lower()} during the "
        f"walkthrough, each shown in the corresponding still: {body} Conditions should be "
        f"confirmed on site where the recording did not provide full detail."
    )
    if len(summary.split()) > 80:
        summary = " ".join(summary.split()[:80]).rstrip(" ,;:.") + "."
    return summary


def _guess_fields_from_text(section_fields: list[str], blob: str) -> dict[str, str]:
    """Very lightweight heuristic fills — labeled so QA/report readers treat as provisional.

    ``severity`` defaults to a neutral ``informational`` (the deterministic path cannot grade
    severity from text) rather than a placeholder string, and ``recommendations`` is left blank
    so the PDF omits an empty callout instead of printing a "See narrative summary" placeholder.
    """
    guesses: dict[str, Any] = {}
    lower = blob.lower()
    for field in section_fields:
        fl = field.lower()
        if fl in ("severity", "severity_level", "risk", "risk_level"):
            guesses[field] = "informational"
        elif fl in ("recommendations", "recommendation", "recommended_actions", "next_steps"):
            # Blank → treated as noise downstream, so no placeholder recommendation is printed.
            guesses[field] = ""
        elif fl.endswith("_clear") or fl.startswith("egress"):
            guesses[field] = "yes" if any(x in lower for x in ("clear", "open", "unblocked")) else "unknown"
        elif "hazard" in fl or "flags" in fl:
            guesses[field] = []
        elif "name" in fl or "subject" in fl:
            guesses[field] = "Not stated"
        else:
            guesses[field] = "See narrative summary"
    return guesses


def _normalize_mark_events(mark_events: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Sort/clean client mark events (manual MARK taps) by timestamp."""
    out: list[dict[str, Any]] = []
    for ev in mark_events or []:
        if not isinstance(ev, dict):
            continue
        try:
            t = float(ev.get("t_sec", ev.get("time", 0.0)))
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "t_sec": max(0.0, t),
                "note": str(ev.get("note") or "").strip(),
                "section_id": str(ev.get("section_id") or "").strip(),
                "report_type": str(ev.get("report_type") or "").strip(),
                "title": str(ev.get("title") or "").strip(),
            }
        )
    out.sort(key=lambda r: r["t_sec"])
    return out


def _seg_start(seg: dict[str, Any]) -> float:
    try:
        return float(seg.get("start", seg.get("t_start_sec", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _seg_end(seg: dict[str, Any]) -> float:
    try:
        return float(seg.get("end", seg.get("t_end_sec", _seg_start(seg) + 1.0)))
    except (TypeError, ValueError):
        return _seg_start(seg) + 1.0


def _gap_to_span(t: float, span_start: float, span_end: float) -> float:
    """Distance from ``t`` to the nearest point of ``[span_start, span_end]`` (0.0 when inside).

    A spoken cue occupies a whole segment, so a MARK tap that lands anywhere inside that segment
    (or just past it) is describing the same finding.
    """
    if span_start <= t <= span_end:
        return 0.0
    return span_start - t if t < span_start else t - span_end


def _normalize_step_events(step_events: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Sort/clean continuous-recording step boundaries by timestamp."""
    out: list[dict[str, Any]] = []
    for ev in step_events or []:
        if not isinstance(ev, dict):
            continue
        try:
            t = float(ev.get("t_sec", ev.get("time", 0.0)))
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "t_sec": max(0.0, t),
                "section_id": str(ev.get("section_id") or "").strip(),
                "report_type": str(ev.get("report_type") or "").strip(),
                "title": str(ev.get("title") or "").strip(),
            }
        )
    out.sort(key=lambda r: r["t_sec"])
    return out


def _build_section_windows(
    step_events: list[dict[str, Any]] | None,
    transcript_end: float,
) -> dict[tuple[str, str], tuple[float, float]]:
    """Map ``(report_type, section_id)`` → ``(start, end)`` from step boundaries.

    In one continuous recording each step begins at its event time and ends when the next
    step begins (or the transcript ends). This gives the deterministic matcher an accurate
    per-section time window even when the spoken "Mark this." cue for a section was missed.
    """
    steps = _normalize_step_events(step_events)
    windows: dict[tuple[str, str], tuple[float, float]] = {}
    for idx, ev in enumerate(steps):
        if not ev["section_id"]:
            continue
        start = ev["t_sec"]
        end = steps[idx + 1]["t_sec"] if idx + 1 < len(steps) else max(transcript_end, start + 1.0)
        key = (ev["report_type"], ev["section_id"])
        windows.setdefault(key, (start, end))  # first occurrence of a section wins
    return windows


def _narration_in_window(segments: list[dict[str, Any]], t_start: float, t_end: float) -> str:
    """Join transcript text spoken within a step's time window (spoken cue not required)."""
    chunks: list[str] = []
    for seg in segments:
        st = _seg_start(seg)
        if t_start <= st < t_end:
            txt = str(seg.get("text", "")).strip()
            if txt:
                chunks.append(_strip_trigger_prefix(txt))
    return " ".join(c for c in chunks if c).strip()


def _narration_near_timestamp(
    segments: list[dict[str, Any]],
    t: float,
    *,
    window_before: float = 1.5,
    window_after: float = 8.0,
    clamp: tuple[float, float] | None = None,
) -> str:
    """Join transcript text spoken around a mark timestamp (short per-photo caption snippet).

    The ``after`` window is kept modest so a photo caption does not spill the *next* section's
    narration in, and the "Mark this." cue is always stripped so it never appears in a caption.
    ``clamp`` (the section's step window) bounds the snippet to this section's time span so a
    caption never borrows a neighbouring section's narration.
    """
    lo = t - window_before
    hi = t + window_after
    if clamp:
        lo = max(lo, clamp[0])
        hi = min(hi, clamp[1])
    chunks: list[str] = []
    for seg in segments:
        try:
            st = float(seg.get("start", seg.get("t_start_sec", 0.0)))
        except (TypeError, ValueError):
            continue
        if lo <= st <= hi:
            txt = str(seg.get("text", "")).strip()
            if not txt:
                continue
            chunks.append(_strip_trigger_prefix(txt))
    return " ".join(c for c in chunks if c).strip()


def _narration_near_timestamps(
    segments: list[dict[str, Any]],
    times: list[float],
    *,
    window_before: float = 1.5,
    window_after: float = 14.0,
) -> str:
    """Join narration spoken near ANY of several mark taps, de-duplicated and time-ordered.

    Used when an inspector taps MARK several times inside one section (multiple findings)
    without an enclosing step window — we gather the narration around every tap once.
    """
    if not times:
        return ""
    picked: dict[int, tuple[float, str]] = {}
    for idx, seg in enumerate(segments):
        st = _seg_start(seg)
        txt = str(seg.get("text", "")).strip()
        if not txt:
            continue
        for t in times:
            if t - window_before <= st <= t + window_after:
                picked[idx] = (st, _strip_trigger_prefix(txt))
                break
    ordered = [picked[i][1] for i in sorted(picked)]
    return " ".join(c for c in ordered if c).strip()


def _narration_context_window(
    segments: list[dict[str, Any]],
    t: float,
    *,
    before: float,
    after: float,
) -> tuple[str, float, float]:
    """Join narration from ``before`` seconds before to ``after`` seconds after a mark.

    Returns ``(text, window_start, window_end)``. This gives each photo the surrounding
    description (what the inspector said leading up to and just after the mark) rather than a
    single nearest sentence. A stray spoken cue very close to the mark is stripped.
    """
    start = max(0.0, t - before)
    end = t + after
    chunks: list[str] = []
    for seg in segments:
        st = _seg_start(seg)
        if start <= st <= end:
            txt = str(seg.get("text", "")).strip()
            if not txt:
                continue
            chunks.append(_strip_trigger_prefix(txt))
    return " ".join(c for c in chunks if c).strip(), round(start, 2), round(end, 2)


def _summary_visited_no_narration(section_name: str, default_text: str = "") -> str:
    """Client-facing copy when a section was visited but no spoken findings were recorded.

    ``default_text`` stays available to the writer/schema internally — it is NOT printed as
    an observed condition. The PDF must say the section is unverified.
    """
    _ = default_text
    return (
        f"Not verified — no spoken findings were recorded for {section_name.lower()} during "
        f"this walkthrough. Do not treat this section as a documented condition. Review any "
        f"still frame(s) against the source recording and recapture this area before relying "
        f"on it in the final report."
    )


def _apply_field_note(narration: str, note: str) -> str:
    if not note:
        return narration
    if narration:
        return f"{narration} Field note: {note}"
    return f"Field note: {note}"


def _cue_image_time(segments: list[dict[str, Any]], idx: int) -> float:
    """Clamped midpoint of a spoken-cue segment (frame lands on the described subject)."""
    t0 = _seg_start(segments[idx])
    t1 = _seg_end(segments[idx])
    return min(max(t0, (t0 + t1) / 2.0), t1)


def _build_section_frames(
    segments: list[dict[str, Any]],
    *,
    taps: list[dict[str, Any]],
    cues: list[int],
    window: tuple[float, float] | None,
) -> list[SectionFrame]:
    """Collect **every** evidence still for one section (not just the first).

    One frame per manual MARK tap and per spoken cue in the section, each anchored at its own
    moment with the narration nearest it. When the section was only visited (step window, no
    tap/cue) a single representative frame is taken from the window. Near-duplicate anchors are
    merged and the count is capped for performance.
    """
    candidates: list[tuple[float, float, str, str]] = []  # (anchor, image_time, source, note)
    for t in taps:
        tt = float(t["t_sec"])
        candidates.append((tt, tt, "manual_mark", str(t.get("note") or "")))
    for idx in cues:
        candidates.append((_seg_start(segments[idx]), _cue_image_time(segments, idx), "spoken_cue", ""))
    if not candidates and window:
        ws, we = window
        img = min(we - 0.1, ws + min(2.5, max(0.5, (we - ws) / 2.0)))
        candidates.append((ws, max(0.0, img), "step_window", ""))

    candidates.sort(key=lambda c: (c[0], 0 if c[2] == "manual_mark" else 1))

    # Merge near-duplicate anchors *within this section only* (this function never sees another
    # section's candidates). When a tap and a spoken cue describe the same moment the tap wins,
    # because ``mark_events[].t_sec`` is the authoritative still time the inspector chose.
    merged: list[tuple[float, float, str, str]] = []
    for cand in candidates:
        if merged and abs(cand[0] - merged[-1][0]) < _FRAME_MIN_GAP_SEC:
            if cand[2] == "manual_mark" and merged[-1][2] != "manual_mark":
                merged[-1] = cand
            continue
        merged.append(cand)
    candidates = merged

    before, after = _frame_context_window()
    max_frames = _max_frames_per_section()
    frames: list[SectionFrame] = []
    last_anchor: float | None = None
    for anchor_t, img_t, source, note in candidates:
        # Nearest-sentence snippet (short caption) + rich context window (5s before / 10s after).
        if source == "step_window" and window:
            snippet = _narration_in_window(segments, *window)
        else:
            snippet = _narration_near_timestamp(segments, anchor_t, clamp=window)
        context, ctx_start, ctx_end = _narration_context_window(
            segments, anchor_t, before=before, after=after
        )
        frames.append(
            SectionFrame(
                timestamp_sec=round(max(0.0, anchor_t), 2),
                image_timestamp_sec=round(max(0.0, img_t), 2),
                narration=snippet[:400],
                context_narration=context[:1200],
                context_start_sec=ctx_start,
                context_end_sec=ctx_end,
                source=source,
                note=note[:300],
            )
        )
        last_anchor = anchor_t
        if len(frames) >= max_frames:
            merged = len(candidates) - len(frames)
            logger.info(
                "[frames] section capped at %d frames (max=%d); %d additional mark(s) beyond cap.",
                len(frames), max_frames, max(0, merged),
            )
            break
    return frames


def build_matched_sections(
    transcript: dict[str, Any],
    templates: list[ReportTemplate],
    *,
    trigger: str = "Mark this.",
    mark_events: list[dict[str, Any]] | None = None,
    step_events: list[dict[str, Any]] | None = None,
) -> list[MatchedSection]:
    """
    Link template sections to evidence in capture order, using three signals:

    1. Spoken **“Mark this.”** segment starts (primary, deterministic).
    2. Client **mark events** (manual MARK taps with exact timestamps + optional notes) —
       used to (a) enrich a spoken match with the inspector's typed note, and (b) anchor a
       section's frame + pull nearby narration when no spoken cue was detected for it.
    3. **Step events** (continuous-recording step boundaries) — provide an accurate time
       window per section so, when neither a spoken cue nor a tap exists for a section, the
       narration actually spoken while on that step is still captured (instead of pure filler).

    Resolution per section (capture order), highest-trust signal first:
      1. **Manual MARK tap(s)** tagged to this section  → anchor the frame at the first tap;
         summarize the step-window narration (or narration around the taps). Multiple taps in
         one section are folded into the summary (count + all nearby narration).
      2. **Spoken cue(s)** whose time falls in this section's step window → anchor at the cue;
         summarize the window (or between-cue) narration.
      3. **Step window with narration** (visited + spoke, but no explicit mark) → summarize the
         window narration and anchor a frame inside it.
      4. **Step window without narration** (visited but silent) → still extract a representative
         frame from the window so the section is not empty; note that narration was absent.
      5. **Nothing** → deterministic template-only filler (clearly labeled unverified).

    Marks/cues are assigned to sections by the tap's own ``section_id`` and by the step-window
    time span, NOT by list position — so tapping several times in one area no longer bleeds
    those marks onto later sections, and marks are prioritized as the user's explicit intent.
    """
    _ = trigger
    flat = flatten_sections_in_order(templates)
    segments = list(transcript.get("segments") or [])
    marks = _normalize_mark_events(mark_events)
    transcript_end = max((_seg_end(s) for s in segments), default=0.0)
    section_windows = _build_section_windows(step_events, transcript_end)
    valid_keys = {(tpl.report_type, sec.id) for tpl, sec in flat}

    # All spoken "Mark this." cue segment indexes, in time order.
    cue_indexes: list[int] = [
        idx for idx, seg in enumerate(segments) if TRIGGER_RE.search(str(seg.get("text", "")))
    ]
    cue_pos_of_idx = {idx: pos for pos, idx in enumerate(cue_indexes)}

    # --- Assign manual taps to sections (by the tap's own section tag, else by window time).
    taps_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    leftover_taps: list[dict[str, Any]] = []
    for m in marks:
        key = (m["report_type"], m["section_id"])
        if m["section_id"] and key in valid_keys:
            taps_by_key.setdefault(key, []).append(m)
            continue
        placed = False
        for wkey, (ws, we) in section_windows.items():
            if wkey in valid_keys and ws <= m["t_sec"] < we:
                taps_by_key.setdefault(wkey, []).append(m)
                placed = True
                break
        if not placed:
            leftover_taps.append(m)

    # --- Assign spoken cues to sections by which step window contains them (time-based).
    cues_by_key: dict[tuple[str, str], list[int]] = {}
    leftover_cues: list[int] = []
    for idx in cue_indexes:
        st = _seg_start(segments[idx])
        placed = False
        for wkey, (ws, we) in section_windows.items():
            if wkey in valid_keys and ws <= st < we:
                cues_by_key.setdefault(wkey, []).append(idx)
                placed = True
                break
        if not placed:
            leftover_cues.append(idx)

    # --- Leftover cues: adopt one ONLY when a section's own MARK tap sits within
    # ``_CUE_TAP_MERGE_SEC`` of it (the tap and the cue describe the same finding). A cue that
    # cannot be tied to a tap is left unassigned rather than handed to the next empty section —
    # dumping leftovers "to use them up" is what put a components mark's still under overview.
    tagged_taps_exist = any(taps_by_key.values())
    unassigned_cues: list[int] = []
    for idx in leftover_cues:
        cue_start, cue_end = _seg_start(segments[idx]), _seg_end(segments[idx])
        best_key: tuple[str, str] | None = None
        best_gap: float | None = None
        for tkey, tlist in taps_by_key.items():
            for tap in tlist:
                gap = _gap_to_span(float(tap["t_sec"]), cue_start, cue_end)
                if gap <= _CUE_TAP_MERGE_SEC and (best_gap is None or gap < best_gap):
                    best_key, best_gap = tkey, gap
        if best_key is not None:
            cues_by_key.setdefault(best_key, []).append(idx)
        else:
            unassigned_cues.append(idx)

    # --- Legacy ordinal fallback: ONLY when no tap carries a section_id at all (older recorder
    # builds sent bare timestamps). Once any tap is tagged, section_id is authoritative and
    # positional guessing must not run, or an out-of-order tap swaps two sections' photos.
    if not tagged_taps_exist:
        sections_without_evidence = [
            (tpl, sec) for (tpl, sec) in flat
            if (tpl.report_type, sec.id) not in taps_by_key
            and (tpl.report_type, sec.id) not in cues_by_key
            and (tpl.report_type, sec.id) not in section_windows
        ]
        li = 0
        for tpl, sec in sections_without_evidence:
            key = (tpl.report_type, sec.id)
            if li < len(unassigned_cues):
                cues_by_key.setdefault(key, []).append(unassigned_cues[li])
                li += 1
        unassigned_cues = unassigned_cues[li:]

        # Untagged taps: fold each into the section holding a cue it sits next to, before any
        # positional guessing, so a tap describing the same finding as a cue keeps that
        # section's still instead of being handed to an unrelated empty section.
        remaining_taps: list[dict[str, Any]] = []
        for tap in leftover_taps:
            t_sec = float(tap["t_sec"])
            near_key: tuple[str, str] | None = None
            near_gap: float | None = None
            for ckey, cidxs in cues_by_key.items():
                for cidx in cidxs:
                    gap = _gap_to_span(t_sec, _seg_start(segments[cidx]), _seg_end(segments[cidx]))
                    if gap <= _CUE_TAP_MERGE_SEC and (near_gap is None or gap < near_gap):
                        near_key, near_gap = ckey, gap
            if near_key is not None:
                taps_by_key.setdefault(near_key, []).append(tap)
            else:
                remaining_taps.append(tap)
        leftover_taps = remaining_taps

        ti = 0
        still_empty = [
            (tpl, sec) for (tpl, sec) in flat
            if (tpl.report_type, sec.id) not in taps_by_key
            and (tpl.report_type, sec.id) not in cues_by_key
            and (tpl.report_type, sec.id) not in section_windows
        ]
        for tpl, sec in still_empty:
            key = (tpl.report_type, sec.id)
            if ti < len(leftover_taps):
                taps_by_key.setdefault(key, []).append(leftover_taps[ti])
                ti += 1
        leftover_taps = leftover_taps[ti:]

    if unassigned_cues:
        logger.info(
            "[match] %d spoken cue(s) could not be tied to a tagged section (no step window, no "
            "tap within %.1fs) — left unassigned rather than placed on an unrelated section: %s",
            len(unassigned_cues), _CUE_TAP_MERGE_SEC,
            [round(_seg_start(segments[i]), 1) for i in unassigned_cues],
        )
    if leftover_taps:
        logger.warning(
            "[match] %d MARK tap(s) carried no usable section_id and no step window contains "
            "them — no still assigned (times=%s).",
            len(leftover_taps), [round(float(t["t_sec"]), 1) for t in leftover_taps],
        )

    # Diagnostics: exactly how many manual taps / spoken cues landed in each section, so a
    # "only the first mark shows up" report can be traced to tagging vs. matching immediately.
    if marks or cue_indexes:
        placement = {
            f"{rt}/{sid}": len(taps_by_key.get((rt, sid), []))
            for (rt, sid) in {(t.report_type, s.id) for t, s in flat}
        }
        logger.info(
            "[match] taps=%d cues=%d step_windows=%d | leftover_taps=%d leftover_cues=%d | "
            "taps_per_section=%s",
            len(marks), len(cue_indexes), len(section_windows),
            len(leftover_taps), len(leftover_cues),
            {k: v for k, v in placement.items() if v},
        )

    def _tap_notes(taps: list[dict[str, Any]]) -> str:
        notes = [t["note"] for t in taps if t.get("note")]
        return " ".join(notes).strip()

    matched: list[MatchedSection] = []
    for tpl, sec in flat:
        key = (tpl.report_type, sec.id)
        window = section_windows.get(key)
        # Template content rules are authoritative for fallback prose: prefer the section's
        # ``default_text`` over inventing content when narration is missing.
        content_sec = tpl.content_for_section(sec.id)
        default_text = content_sec.default_text if content_sec else ""
        taps = sorted(taps_by_key.get(key, []), key=lambda m: m["t_sec"])
        cues = sorted(cues_by_key.get(key, []), key=lambda i: _seg_start(segments[i]))

        # Collect ALL evidence stills for this section (one per tap / cue), not just the first.
        frames = _build_section_frames(segments, taps=taps, cues=cues, window=window)

        if taps:
            # 1) Manual mark(s) — the strongest signal of user intent. Anchor at the first tap.
            first_t = float(taps[0]["t_sec"])
            tap_times = [float(t["t_sec"]) for t in taps]
            if window:
                narration = _narration_in_window(segments, *window)
            else:
                narration = _narration_near_timestamps(segments, tap_times)
            narration = _apply_field_note(narration, _tap_notes(taps))
            if len(taps) > 1:
                stamps = ", ".join(f"{t:.0f}s" for t in tap_times)
                narration = (
                    f"{narration} The inspector marked {len(taps)} item(s) in this area (at {stamps})."
                ).strip()
            # Multi-frame sections: enumerate distinct findings so the deterministic fallback
            # also lines up with the numbered photos instead of one lumped paragraph.
            if len(frames) > 1:
                summary = _fallback_multiframe_summary(sec.name, frames, default_text)
            elif narration.strip():
                summary = _professional_summary_from_narration(narration, sec.name, default_text)
            else:
                summary = _summary_visited_no_narration(sec.name, default_text)
            matched.append(
                MatchedSection(
                    section_id=sec.id,
                    report_type=tpl.report_type,
                    section_name=sec.name,
                    timestamp_sec=first_t,
                    image_timestamp_sec=first_t,
                    summary=summary,
                    trigger_phrase="manual_mark",
                    required_fields=list(sec.required_fields),
                    field_guesses=_guess_fields_from_text(sec.required_fields, narration),
                    segment_text_raw=narration,
                    frames=frames,
                )
            )
        elif cues:
            # 2) Spoken cue(s) placed in this section by time window (or positional fallback).
            first_idx = cues[0]
            seg = segments[first_idx]
            t0 = _seg_start(seg)
            t1 = _seg_end(seg)
            if window:
                narration = _narration_in_window(segments, *window)
            else:
                pos = cue_pos_of_idx.get(first_idx, 0)
                narration = _extract_narration_between_cues(segments, cue_indexes, pos)
            image_t = min(max(t0, (t0 + t1) / 2.0), t1)
            summary = (
                _professional_summary_from_narration(narration, sec.name, default_text)
                if narration.strip()
                else _summary_visited_no_narration(sec.name, default_text)
            )
            matched.append(
                MatchedSection(
                    section_id=sec.id,
                    report_type=tpl.report_type,
                    section_name=sec.name,
                    timestamp_sec=t0,
                    image_timestamp_sec=image_t,
                    summary=summary,
                    trigger_phrase="Mark this.",
                    required_fields=list(sec.required_fields),
                    field_guesses=_guess_fields_from_text(sec.required_fields, narration),
                    segment_text_raw=narration,
                    frames=frames,
                )
            )
        elif window:
            # 3/4) The inspector was on this step (continuous recording proves it). Summarize
            # any narration in the window; if silent, still extract a representative frame so
            # the section is documented visually rather than left as empty filler.
            t_start, t_end = window
            narration = _narration_in_window(segments, t_start, t_end)
            image_t = min(t_end - 0.1, t_start + min(2.5, max(0.5, (t_end - t_start) / 2.0)))
            if narration.strip():
                summary = _professional_summary_from_narration(narration, sec.name, default_text)
                guesses = _guess_fields_from_text(sec.required_fields, narration)
            else:
                summary = _summary_visited_no_narration(sec.name, default_text)
                guesses = {k: "not stated" for k in sec.required_fields}
            matched.append(
                MatchedSection(
                    section_id=sec.id,
                    report_type=tpl.report_type,
                    section_name=sec.name,
                    timestamp_sec=t_start,
                    image_timestamp_sec=max(0.0, image_t),
                    summary=summary,
                    trigger_phrase="step_boundary",
                    required_fields=list(sec.required_fields),
                    field_guesses=guesses,
                    segment_text_raw=narration,
                    frames=frames,
                )
            )
        else:
            # 5) No signal at all for this section — deterministic template-only filler.
            # Anchor at 0.0 and emit NO frames: the section was never reached, so extracting a
            # still (which would land on frame 0.0s / the video start) would be misleading.
            anchor = 0.0
            # default_text / capture_instructions stay internal — never printed as findings.
            filler_summary = (
                f"Not verified — {sec.name} was not reached during this walkthrough, so no "
                f"spoken findings are documented for it. This section was not inspected on the "
                f"recording and must not be treated as an observed condition. Capture this area "
                f"in a brief follow-up recording before relying on it in the final report."
            )
            matched.append(
                MatchedSection(
                    section_id=sec.id,
                    report_type=tpl.report_type,
                    section_name=sec.name,
                    timestamp_sec=anchor,
                    image_timestamp_sec=anchor,
                    summary=filler_summary,
                    trigger_phrase="none",
                    required_fields=list(sec.required_fields),
                    field_guesses={k: "missing" for k in sec.required_fields},
                    segment_text_raw="",
                    frames=frames,
                )
            )

    # Per-section evidence audit: one line each so a swapped or missing photo can be traced to
    # tap tagging vs. matching without a rerun. Image paths are logged later, at extraction time
    # (utils/report_pdf.py), because the JPEGs do not exist yet here.
    for pos, m in enumerate(matched):
        key = (m.report_type, m.section_id)
        sec_taps = taps_by_key.get(key, [])
        tap_times = [round(float(t["t_sec"]), 2) for t in sec_taps]
        extract_at = [round(float(f.image_timestamp_sec), 2) for f in m.frames]
        logger.info(
            "[frames] section=%s n_taps=%d tap_times=%s n_cues=%d extract_at_sec=%s source=%s",
            m.section_id, len(sec_taps), tap_times,
            len(cues_by_key.get(key, [])), extract_at, m.trigger_phrase,
        )
        # A later section that the inspector explicitly marked must never extract at 0.0s — that
        # is the video's opening frame leaking in place of the real mark moment.
        if pos > 0 and sec_taps and any(t <= 0.05 for t in extract_at):
            logger.error(
                "[frames] section=%s had %d MARK tap(s) at %s but a still is anchored at 0.0s "
                "(extract_at_sec=%s) — the tap timestamp was lost.",
                m.section_id, len(sec_taps), tap_times, extract_at,
            )

    with_narration = sum(1 for m in matched if (m.segment_text_raw or "").strip())
    total_frames = sum(len(m.frames) for m in matched)
    logger.info(
        "[match] built %d section(s); %d have real narration; %d total evidence frame(s) | "
        "frames_per_section=%s | evidence=%s",
        len(matched), with_narration, total_frames,
        {m.section_id: len(m.frames) for m in matched},
        {m.section_id: m.trigger_phrase for m in matched},
    )
    return matched


def reconcile_section_frames(
    final: FinalReportOutput,
    matched: list[MatchedSection],
) -> FinalReportOutput:
    """
    Overwrite each final-report section's frame timestamps with the **authoritative**
    deterministic values from ``matched`` (keyed by report_type + section_id).

    The LLM writer can drift on numeric timestamps; this guarantees the extracted still
    frame lands on the spoken cue / manual mark moment regardless of LLM variance.
    """
    by_key: dict[tuple[str, str], MatchedSection] = {}
    for m in matched:
        by_key[(str(m.report_type), str(m.section_id))] = m

    changed = False
    new_reports: dict[str, ReportBundle] = {}
    for rt, bundle in final.reports.items():
        new_sections: list[ReportSectionOutput] = []
        for sec in bundle.sections:
            m = by_key.get((rt, sec.section_id))
            # The LLM writer never emits `frames`; always graft the deterministic evidence list
            # (all MARK taps / cues) onto its section so the PDF renders every still.
            if m and (m.image_timestamp_sec or m.timestamp_sec or m.frames):
                new_sections.append(
                    sec.model_copy(
                        update={
                            "timestamp_sec": m.timestamp_sec,
                            "image_timestamp_sec": m.image_timestamp_sec,
                            "frames": [f.model_copy() for f in m.frames],
                        }
                    )
                )
                changed = True
            else:
                new_sections.append(sec)
        new_reports[rt] = bundle.model_copy(update={"sections": new_sections})
    if not changed:
        return final
    return final.model_copy(update={"reports": new_reports})


_RECONCILE_STOPWORDS = frozenset(
    {
        "about", "after", "again", "along", "areas", "around", "based", "being", "could",
        "during", "found", "front", "their", "there", "these", "thing", "those", "under",
        "where", "which", "while", "would", "narration", "section", "report", "should",
        "inspector", "walkthrough", "documented", "condition", "conditions",
    }
)


def _reconcile_tokens(text: str) -> set[str]:
    """Content words (len>=5, not stopwords) for measuring narration grounding."""
    return {
        w for w in re.findall(r"[a-zA-Z]{5,}", (text or "").lower())
        if w not in _RECONCILE_STOPWORDS
    }


def _writer_summary_is_faithful(
    writer_summary: str,
    matched: MatchedSection,
    min_words: int,
) -> bool:
    """True when the LLM writer's summary may be kept over the structured summary.

    The structured ``matched`` row is authoritative. We keep the LLM's professional prose
    ONLY when it (a) is substantive enough and (b) actually reflects the captured narration
    when narration exists — i.e. it polished/expanded the real content rather than discarding
    or ignoring it. Otherwise the deterministic, narration-grounded summary wins so real spoken
    content is never dropped.
    """
    text = (writer_summary or "").strip()
    if not text:
        return False
    # Must reach a reasonable fraction of the template's word floor (tolerate mild shortfall;
    # the schema padder handles exact minimums — we only reject clearly empty/stub output).
    if len(text.split()) < max(20, int(min_words * 0.6)):
        return False

    narration = (matched.segment_text_raw or "").strip()
    if not narration:
        # No spoken content to preserve — keep writer prose only if it does not invent findings.
        # Template default_text must not be treated as an observed condition.
        lowered = text.lower()
        if "not verified" in lowered or "no spoken" in lowered or "not reached" in lowered:
            return True
        return False

    # Narration exists: require the writer to have grounded its prose in it. Low overlap means
    # the writer ignored the real spoken content and substituted generic language.
    src = _reconcile_tokens(narration)
    if not src:
        return True
    overlap = len(src & _reconcile_tokens(text)) / len(src)
    return overlap >= 0.12


def reconcile_final_report_with_matched(
    final: FinalReportOutput,
    matched: list[MatchedSection],
    templates: list[ReportTemplate],
) -> FinalReportOutput:
    """Force the final report's section SET, ORDER, and evidence to the authoritative match.

    ``build_matched_sections`` is the single source of truth for which sections exist, their
    order (template capture order), the narration attached to each, and the frames/timestamps.
    The LLM writer may only *polish/expand* the prose. This function guarantees that:

      * Every matched section appears exactly once, in matched (template) order.
      * Section ids invented by the writer are dropped; sections the writer omitted are restored.
      * A section with real narration never ends up with prose that discarded that narration —
        the structured, narration-grounded summary is used instead.
      * Deterministic timestamps + evidence frames are grafted on (LLM never sets frames).

    When the writer produced faithful, substantive prose for a matched section, that prose is
    kept (with its field_values); otherwise the deterministic structured summary/fields win.
    """
    writer_by_key: dict[tuple[str, str], ReportSectionOutput] = {}
    for rt, bundle in final.reports.items():
        for sec in bundle.sections:
            writer_by_key[(str(rt), str(sec.section_id))] = sec

    min_words_by_key: dict[tuple[str, str], int] = {}
    title_by_type: dict[str, str] = {}
    for t in templates:
        title_by_type[t.report_type] = t.title
        if t.content_structure:
            for c in t.content_structure.sections:
                min_words_by_key[(t.report_type, c.section_id)] = c.min_summary_words

    # Group matched rows by report_type, preserving their (template capture) order.
    matched_by_type: dict[str, list[MatchedSection]] = {}
    for m in matched:
        matched_by_type.setdefault(str(m.report_type), []).append(m)

    kept_writer = used_structured = 0
    new_reports: dict[str, ReportBundle] = {}
    for rt, rows in matched_by_type.items():
        sections: list[ReportSectionOutput] = []
        for m in rows:
            key = (rt, str(m.section_id))
            min_words = min_words_by_key.get(key, MIN_SUMMARY_WORDS)
            writer_sec = writer_by_key.get(key)
            if writer_sec is not None and _writer_summary_is_faithful(
                writer_sec.summary, m, min_words
            ):
                # Keep the writer's professional prose + field_values, but backfill any required
                # field key it dropped from the structured guesses so field tables stay complete.
                field_values = dict(writer_sec.field_values or {})
                for fkey, fval in (m.field_guesses or {}).items():
                    field_values.setdefault(fkey, fval)
                summary = writer_sec.summary
                kept_writer += 1
            else:
                # Structured (narration-grounded) summary wins: the writer ignored/discarded the
                # real spoken content, produced a stub, or omitted this section entirely.
                summary = m.summary
                field_values = dict(m.field_guesses or {})
                used_structured += 1
            sections.append(
                ReportSectionOutput(
                    section_id=m.section_id,
                    section_name=m.section_name,
                    summary=summary,
                    timestamp_sec=m.timestamp_sec,
                    image_timestamp_sec=m.image_timestamp_sec,
                    field_values=field_values,
                    frames=[f.model_copy() for f in m.frames],
                )
            )
        title = title_by_type.get(rt) or (
            final.reports[rt].title if rt in final.reports else rt
        )
        new_reports[rt] = ReportBundle(report_type=rt, title=title, sections=sections)

    logger.info(
        "[reconcile] enforced structured SSOT: %d section(s) across %d report(s) | "
        "writer_prose_kept=%d structured_prose_used=%d",
        sum(len(b.sections) for b in new_reports.values()), len(new_reports),
        kept_writer, used_structured,
    )
    return final.model_copy(update={"reports": new_reports})


def build_final_report_output(
    matched: list[MatchedSection],
    *,
    video_stem: str,
    video_filename: str,
) -> FinalReportOutput:
    """Group matched rows into ``ReportBundle``s keyed by ``report_type``."""
    bundles: dict[str, ReportBundle] = {}
    for row in matched:
        tpl_key = row.report_type
        if tpl_key not in bundles:
            # title filled when we merge template list in app; placeholder here.
            bundles[tpl_key] = ReportBundle(report_type=tpl_key, title=tpl_key, sections=[])
        bundles[tpl_key].sections.append(
            ReportSectionOutput(
                section_id=row.section_id,
                section_name=row.section_name,
                summary=row.summary,
                timestamp_sec=row.timestamp_sec,
                image_timestamp_sec=row.image_timestamp_sec,
                field_values={**row.field_guesses},
                frames=[f.model_copy() for f in row.frames],
            )
        )
    return FinalReportOutput(
        video_stem=video_stem,
        video_filename=video_filename,
        identification_phrase="Mark this.",
        reports=bundles,
    )


def attach_template_titles(final: FinalReportOutput, templates: list[ReportTemplate]) -> FinalReportOutput:
    """Copy human titles from JSON templates onto bundles."""
    by_type = {t.report_type: t for t in templates}
    for key, bundle in final.reports.items():
        if key in by_type:
            bundle.title = by_type[key].title
    return final


def run_qa_checks(final: FinalReportOutput, templates: list[ReportTemplate]) -> QAReport:
    """
    Deterministic QA pass: required sections present, summary lengths, missing cues.

    This complements any LLM QA task — results merge in ``JobDocCrew`` output.
    """
    findings: list[QAFinding] = []
    expected_types = {t.report_type for t in templates}
    for rt in expected_types:
        if rt not in final.reports:
            findings.append(
                QAFinding(
                    code="MISSING_REPORT_BUNDLE",
                    message=f"No bundle produced for report_type `{rt}`.",
                    severity="error",
                )
            )

    tpl_sections = {(t.report_type, s.id) for t in templates for s in t.sections}
    covered_set: set[tuple[str, str]] = set()
    for rt, bundle in final.reports.items():
        for sec in bundle.sections:
            covered_set.add((rt, sec.section_id))

    for rt, sid in tpl_sections:
        if (rt, sid) not in covered_set:
            findings.append(
                QAFinding(
                    code="MISSING_SECTION",
                    message=f"Section `{sid}` missing for `{rt}`.",
                    severity="warning",
                )
            )

    # Per-section minimum word counts come from the template content_structure (SSOT), so QA
    # enforces exactly what each section's writer contract requires.
    min_words_by_key: dict[tuple[str, str], int] = {}
    for t in templates:
        if not t.content_structure:
            continue
        for c in t.content_structure.sections:
            min_words_by_key[(t.report_type, c.section_id)] = c.min_summary_words

    for rt, bundle in final.reports.items():
        for sec in bundle.sections:
            words = sec.summary.split()
            required = min_words_by_key.get((rt, sec.section_id), 45)
            if len(words) < required:
                findings.append(
                    QAFinding(
                        code="SUMMARY_TOO_SHORT",
                        message=(
                            f"Section {sec.section_id} summary has {len(words)} words; "
                            f"template requires >= {required}."
                        ),
                        severity="error",
                    )
                )

    passed = not any(f.severity == "error" for f in findings)
    return QAReport(
        passed=passed,
        findings=findings,
        summary="; ".join(f.message for f in findings[:5]) if findings else "No blocking issues.",
    )


def dumps_json(obj: Any) -> str:
    """Serialize pydantic models, lists thereof, or plain dicts for prompts and API payloads."""
    if isinstance(obj, list):
        if obj and hasattr(obj[0], "model_dump"):
            return json.dumps([x.model_dump() for x in obj], indent=2, ensure_ascii=False)
        return json.dumps(obj, indent=2, ensure_ascii=False)
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(), indent=2, ensure_ascii=False)
    return json.dumps(obj, indent=2, ensure_ascii=False)
