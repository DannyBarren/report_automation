"""
app.py — Main Flask application for jobdoc-demo (guided video report MVP).

Design notes
------------
    - Report definitions live only in ``reports/*.json`` (validated via Pydantic).
    - ``/start_recording`` loads those templates, renders the workflow generator prompt
      (``prompts/workflow_generator.txt``), and shows a deterministic plan that matches the
      Crew payload (single source of truth).
    - ``/upload_video`` stores bytes under ``uploads/`` with a timestamp + report-slug
      filename; ``/status/<id>`` lets the UI poll lightweight job JSON stored in
      ``VideoUpload.job_meta``.
    - ``/generate_report`` runs the full pipeline: real ASR (Deepgram Nova-3), ``JobDocCrew``
      with live LLMs, deterministic “Mark this.” matching for PDF truth, still frames,
      Jinja HTML, and WeasyPrint PDF under ``reports_output/`` with ``/download_report/<id>``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
from flask import Flask, flash, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from sqlalchemy import inspect, text

from models import CaptureSession, Client, JobInfoSummary, MiscData, SessionSummary, VideoUpload, db
from utils.misc_section import MISC_REPORT_TYPE, misc_report_template

from utils.job_progress import default_pipeline_steps, mark_all_completed, sync_steps_from_progress
from utils.pipeline_config import PIPELINE_V1, PIPELINE_V2, get_jobdoc_pipeline, is_v2_pipeline
from utils.pipeline_errors import PipelineError, friendly_error_message
from utils.prompt_utils import load_prompt
from utils.pipeline_logging import configure_logging, log_job_step
from utils.report_pdf import (
    enrich_report_with_still_frames_resilient,
    render_report_html,
    write_pdf,
)
from utils.setup_weasyprint import (
    bootstrap_weasyprint_for_app,
    check_weasyprint_status,
    ensure_weasyprint_deps,
    reset_weasyprint_probe_cache,
)
from crew.report_resilience import merge_review_notes
from utils.runtime_config import require_asr_credentials, require_crew_llm_credentials
from utils.schemas import FinalReportOutput, ReportTemplate
from utils.video_utils import probe_duration_seconds, try_extract_audio
from utils import gpu_utils
from utils import credentials
from utils import maintenance
from utils.workflow_stub import (
    build_workflow_json_bundle,
    build_workflow_steps_from_templates,
    workflow_bundle_to_pretty_json,
)

# Ensure instance folder exists and force an absolute SQLite file path.
# On Windows, relative sqlite URIs can resolve against an unexpected cwd and
# cause `sqlite3.OperationalError: unable to open database file`.
basedir = os.path.abspath(os.path.dirname(__file__))
instance_dir = os.path.join(basedir, "instance")
os.makedirs(instance_dir, exist_ok=True)
db_path = os.path.join(instance_dir, "jobdoc.db")

# -----------------------------------------------------------------------------
# Paths and environment
# -----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

# Load ``.env`` early so FLASK_DEBUG, DATABASE_URL, and future API keys are available.
load_dotenv(BASE_DIR / ".env")

# Fold any alias-spelled provider secrets (e.g. OPEN_AI_API_KEY / OPENAI_KEY from a Modal
# Secret or .env) into the canonical OPENAI_API_KEY / DEEPGRAM_API_KEY the SDKs read. Done
# once here at import so the very first request already sees the key.
credentials.normalize_provider_env()

# Configure structured (JSON) logging before anything logs at import time.
configure_logging()

# GTK / WeasyPrint (PDF) — apply DLL paths early; lazy-import in report_pdf until PDF is built.
WEASYPRINT_STATUS = bootstrap_weasyprint_for_app(BASE_DIR)

REPORTS_DIR = BASE_DIR / "reports"
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "reports_output"
STATIC_DIR = BASE_DIR / "static"
INSTANCE_DIR = BASE_DIR / "instance"

IDENTIFICATION_PHRASE = "Mark this."
HERO_INSTRUCTION = (
    "Remember: Hold steady, say “Mark this.”, then describe what you are showing "
    "before you move the camera. Work through the sections in order below."
)


def max_video_seconds() -> int:
    """
    Max accepted recording length. GPU Spaces decode/encode faster, so we allow longer
    videos there (8 min) than on CPU Spaces (3 min). Override with JOBDOC_MAX_VIDEO_SEC.
    """
    override = os.environ.get("JOBDOC_MAX_VIDEO_SEC", "").strip()
    if override.isdigit():
        return max(30, int(override))
    return 480 if gpu_utils.is_gpu_available() else 180


def session_token() -> str:
    """Stable per-browser token used to key server-side API credentials (not the keys)."""
    token = session.get("kid")
    if not token:
        token = uuid.uuid4().hex
        session["kid"] = token
    return token


def ensure_runtime_directories() -> None:
    """Create folders expected at runtime if they are missing."""
    for path in (UPLOADS_DIR, OUTPUT_DIR, INSTANCE_DIR, STATIC_DIR):
        path.mkdir(parents=True, exist_ok=True)


ensure_runtime_directories()

# -----------------------------------------------------------------------------
# Flask + database
# -----------------------------------------------------------------------------

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("JOBDOC_MAX_UPLOAD_MB", "500")) * 1024 * 1024
# Keep the login cookie around so users sign in once per browser, not once per visit.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
def _session_cookie_secure() -> bool:
    """Secure cookies on HTTPS (HF). Off for explicit local HTTP or FLASK_DEBUG.

    Never inspect the Host header — a public hostname must not flip this off.
    """
    if os.environ.get("JOBDOC_COOKIE_INSECURE", "").strip().lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("FLASK_DEBUG", "").strip() in ("1", "true", "yes"):
        return False
    return True


# HF Spaces serve over HTTPS; secure cookies in production unless local HTTP / explicit opt-out.
app.config["SESSION_COOKIE_SECURE"] = _session_cookie_secure()

db.init_app(app)

# Process start time for the /metrics uptime field.
_START_TIME = time.time()


@app.context_processor
def _inject_weasyprint_status() -> dict[str, Any]:
    return {"weasyprint_ready": WEASYPRINT_STATUS.ready}


# Serializes the credentialed generation phase within a worker process. Because per-session
# API keys are applied to process-global ``os.environ``, two report generations must not run
# concurrently inside the same worker (their keys would race). Polling/status routes never take
# this lock, so the UI stays responsive while a report is generated.
_GENERATION_LOCK = threading.Lock()


# -----------------------------------------------------------------------------
# Lightweight in-memory rate limiting (per client IP, per bucket)
# -----------------------------------------------------------------------------
# Dependency-free sliding-window limiter sized for a public demo Space. Limits are per
# worker process (HF runs a small fixed worker pool), which is the right granularity for
# protecting Deepgram/OpenAI spend and disk without external infrastructure. Tune via
# JOBDOC_RATELIMIT_* env vars; set JOBDOC_RATELIMIT_DISABLE=1 to turn off entirely.
_rate_lock = threading.Lock()
_rate_hits: dict[str, list[float]] = {}


def _rate_disabled() -> bool:
    return os.environ.get("JOBDOC_RATELIMIT_DISABLE", "").strip() in ("1", "true", "yes")


def _rate_limit_for(bucket: str) -> tuple[int, int]:
    """Return ``(max_requests, window_seconds)`` for a bucket (env-overridable)."""
    defaults = {
        "upload": (int(os.environ.get("JOBDOC_RATELIMIT_UPLOAD", "12")), 600),
        "generate": (int(os.environ.get("JOBDOC_RATELIMIT_GENERATE", "20")), 600),
    }
    return defaults.get(bucket, (30, 600))


def _client_ip() -> str:
    """Best-effort client IP, honoring the proxy header HF Spaces sets in front of the app."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _rate_limit_ok(bucket: str) -> tuple[bool, int]:
    """
    Record a hit for ``(ip, bucket)`` and report whether it is within the window limit.

    Returns ``(allowed, retry_after_seconds)``.
    """
    if _rate_disabled():
        return True, 0
    max_req, window = _rate_limit_for(bucket)
    key = f"{bucket}:{_client_ip()}"
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key, []) if now - t < window]
        if len(hits) >= max_req:
            retry_after = int(window - (now - hits[0])) + 1
            _rate_hits[key] = hits
            return False, max(1, retry_after)
        hits.append(now)
        _rate_hits[key] = hits
        # Opportunistic cleanup so the dict cannot grow without bound on a busy Space.
        if len(_rate_hits) > 2048:
            for k in [k for k, v in _rate_hits.items() if not any(now - t < window for t in v)]:
                _rate_hits.pop(k, None)
    return True, 0


def rate_limited(bucket: str):
    """Decorator: enforce the in-memory rate limit for ``bucket`` with a friendly 429."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            ok, retry_after = _rate_limit_ok(bucket)
            if not ok:
                msg = (
                    "You're going a bit fast — this public Space limits how often reports can be "
                    f"submitted. Please wait about {retry_after}s and try again."
                )
                if request.headers.get("X-JobDoc-Client") == "1" or request.is_json:
                    resp = app.make_response(jsonify({"ok": False, "error": msg}))
                else:
                    try:
                        body = render_template(
                            "error.html", code=429, title="Slow down a moment", message=msg
                        )
                    except Exception:  # noqa: BLE001
                        body = msg
                    resp = app.make_response(body)
                resp.status_code = 429
                resp.headers["Retry-After"] = str(retry_after)
                return resp
            return view(*args, **kwargs)

        return wrapped

    return decorator


# -----------------------------------------------------------------------------
# Simple session-based access control (gate the public demo Space)
# -----------------------------------------------------------------------------
# Single shared credential — this is basic access control for a hosted demo, not a user
# management system. Override the defaults with JOBDOC_AUTH_USER / JOBDOC_AUTH_PASSWORD,
# or disable the gate entirely with JOBDOC_AUTH_DISABLE=1 (e.g. trusted private deploys).
import hmac  # noqa: E402 — grouped with the auth helpers it supports

_AUTH_USER = os.environ.get("JOBDOC_AUTH_USER", "jobdoc")
_AUTH_PASSWORD = os.environ.get("JOBDOC_AUTH_PASSWORD", "Burger_Time_101_!")

# Endpoints reachable without a session (login itself, static assets, PWA files, and the
# non-sensitive monitoring probes so uptime checks keep working behind the gate).
_PUBLIC_ENDPOINTS = {
    "login", "logout", "static", "health", "metrics", "service_worker", "web_manifest",
}


def _auth_enabled() -> bool:
    return os.environ.get("JOBDOC_AUTH_DISABLE", "").strip() not in ("1", "true", "yes")


def _check_credentials(username: str, password: str) -> bool:
    """Constant-time credential check (avoids trivial timing leaks)."""
    user_ok = hmac.compare_digest(username or "", _AUTH_USER)
    pass_ok = hmac.compare_digest(password or "", _AUTH_PASSWORD)
    return user_ok and pass_ok


@app.before_request
def _enforce_login():
    """Redirect unauthenticated users to the login page (JSON 401 for API/SPA callers)."""
    if not _auth_enabled():
        return None
    if (request.endpoint or "") in _PUBLIC_ENDPOINTS:
        return None
    if session.get("authed"):
        return None
    if _wants_json():
        return jsonify({
            "ok": False,
            "error": "Please sign in to continue.",
            "login_url": url_for("login"),
        }), 401
    return redirect(url_for("login", next=request.full_path if request.query_string else request.path))


@app.context_processor
def _inject_auth_state() -> dict[str, Any]:
    return {"auth_enabled": _auth_enabled(), "is_authed": bool(session.get("authed"))}


@app.teardown_request
def _restore_session_key_env(_exc: BaseException | None = None) -> None:
    """
    After any request that applied per-session keys, restore ``os.environ`` and release the
    generation lock. Runs on every code path (success, early return, or exception) so a user's
    pasted Deepgram/OpenAI key can never persist into a later request in the same worker.
    """
    snap = g.pop("_key_env_snapshot", None)
    if snap is not None:
        credentials.restore_env(snap)
    if "_stt_backend_snapshot" in g:
        prev = g.pop("_stt_backend_snapshot")
        if prev is None:
            os.environ.pop("JOBDOC_STT_BACKEND", None)
        else:
            os.environ["JOBDOC_STT_BACKEND"] = prev
    if g.pop("_gen_lock_held", False):
        try:
            _GENERATION_LOCK.release()
        except RuntimeError:
            pass
        # Release any cached CUDA memory on every generation path — including early returns
        # and exceptions where _cleanup_intermediate_media() may not have run.
        try:
            gpu_utils.release_gpu_memory()
        except Exception:  # noqa: BLE001
            pass


def _refresh_weasyprint_status(*, auto_install: bool = False) -> None:
    global WEASYPRINT_STATUS
    reset_weasyprint_probe_cache()
    WEASYPRINT_STATUS = check_weasyprint_status(BASE_DIR, auto_install=auto_install)


def _ensure_sqlite_schema() -> None:
    """
    Lightweight migration for dev SQLite DBs created before new columns existed.

    Production should use Alembic; for the MVP we add missing columns in place so existing
    ``instance/jobdoc.db`` files keep working after the Phase 2 client/session changes.
    """
    if "sqlite" not in str(app.config.get("SQLALCHEMY_DATABASE_URI", "")):
        return
    try:
        insp = inspect(db.engine)
        cols = {c["name"] for c in insp.get_columns("video_uploads")}
    except Exception:  # noqa: BLE001
        return
    pending: list[str] = []
    if "job_meta" not in cols:
        pending.append("ALTER TABLE video_uploads ADD COLUMN job_meta TEXT")
    if "capture_session_id" not in cols:
        pending.append("ALTER TABLE video_uploads ADD COLUMN capture_session_id INTEGER")
    for stmt in pending:
        try:
            with db.engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception:  # noqa: BLE001
            # If ALTER fails, operator can delete ``instance/jobdoc.db`` to recreate.
            pass


# -----------------------------------------------------------------------------
# Report template loading (single source of truth: JSON on disk)
# -----------------------------------------------------------------------------


def load_report_templates() -> list[ReportTemplate]:
    """Read every ``*.json`` in ``reports/`` (rich schema v2 + legacy).

    This is the homepage-selectable list — it deliberately excludes the app-level Misc step,
    which is always appended automatically and never chosen from disk.
    """
    from utils.template_loader import load_all_report_templates

    return load_all_report_templates(REPORTS_DIR)


def templates_by_type() -> dict[str, ReportTemplate]:
    """Lookup of every template by ``report_type``, including the app-level Misc step.

    The Misc template is injected here (not loaded from ``reports/``) so guidance, recording,
    and the report pipeline can resolve ``misc_supplemental`` like any other report type.
    """
    by_type = {t.report_type: t for t in load_report_templates()}
    by_type[MISC_REPORT_TYPE] = misc_report_template()
    return by_type


def append_misc_report_type(ordered: list[str]) -> list[str]:
    """Ensure the always-present Misc step is the final report type for a recording."""
    out = [rt for rt in ordered if rt != MISC_REPORT_TYPE]
    out.append(MISC_REPORT_TYPE)
    return out


def report_templates_json_for_types(selected_types: list[str]) -> str:
    """JSON list of template dicts for Crew + prompts (canonical dump)."""
    by_type = templates_by_type()
    objs = [by_type[rt].model_dump() for rt in selected_types if rt in by_type]
    return json.dumps(objs, ensure_ascii=False)


def merge_sections_for_recording(selected_types: list[str]) -> list[dict[str, Any]]:
    """Flat guided steps for recording UI (rich guidance + legacy fallback)."""
    from utils.template_guidance import build_guidance_plan

    by_type = templates_by_type()
    templates = [by_type[rt] for rt in selected_types if rt in by_type]
    plan = build_guidance_plan(templates)
    if plan.steps:
        return [
            {
                "report_type": s.report_type,
                "report_title": s.report_title,
                "id": s.section_id,
                "name": s.title,
                "capture_instructions": s.voice_prompt,
                "voice_prompt": s.voice_prompt,
                "on_screen_text": s.on_screen_text,
                "required": s.required,
                "min_marks": s.min_marks,
                "suggested_phrases": s.suggested_phrases,
                "required_fields": s.required_fields,
                "step_index": s.step_index,
                "total_steps": s.total_steps,
                "transition_line": s.transition_line,
                "spoken_intro": s.spoken_intro,
            }
            for s in plan.steps
        ]
    merged: list[dict[str, Any]] = []
    for tpl in templates:
        for sec in tpl.sections:
            merged.append(
                {
                    "report_type": tpl.report_type,
                    "report_title": tpl.title,
                    "id": sec.id,
                    "name": sec.name,
                    "capture_instructions": sec.capture_instructions,
                    "required_fields": sec.required_fields,
                }
            )
    return merged


def _sanitize_token(value: str) -> str:
    """Filesystem-safe token derived from a report type slug."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value)
    return (cleaned or "report")[:48]


def build_unique_upload_basename(selected_report_types: list[str], suffix: str) -> str:
    """
    Build ``{UTC timestamp}_{reports}_{rand8}{suffix}`` for traceable uploads.

    The selected report types are reflected in the filename (not only in sidecar JSON)
    so operators can identify jobs in shared folders.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = "_".join(_sanitize_token(rt) for rt in selected_report_types) or "reports"
    short = uuid.uuid4().hex[:8]
    return f"{ts}_{slug}_{short}{suffix}"


def _default_job_meta() -> dict[str, Any]:
    return {
        "status": "uploaded",
        "detail": "Video stored; ready for processing.",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_job_meta(record: VideoUpload) -> dict[str, Any]:
    if not record.job_meta:
        return _default_job_meta()
    try:
        data = json.loads(record.job_meta)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return _default_job_meta()


def save_job_meta(record: VideoUpload, payload: dict[str, Any]) -> None:
    """Merge into existing job_meta and persist."""
    current = parse_job_meta(record)
    current.update(payload)
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    record.job_meta = json.dumps(current, ensure_ascii=False, default=str)
    db.session.add(record)
    db.session.commit()


def build_mark_moments_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Timeline chips for preview — v1 matched_sections or v2 observations."""
    if payload.get("pipeline") == PIPELINE_V2:
        obs = (payload.get("transcript_analysis") or {}).get("observations") or []
        section_names: dict[str, str] = {}
        final = payload.get("final_report") or {}
        for bundle in (final.get("reports") or {}).values():
            if not isinstance(bundle, dict):
                continue
            for sec in bundle.get("sections") or []:
                if isinstance(sec, dict) and sec.get("section_id"):
                    section_names[str(sec["section_id"])] = str(
                        sec.get("section_name") or sec["section_id"]
                    )
        out: list[dict[str, Any]] = []
        for i, row in enumerate(obs):
            if not isinstance(row, dict):
                continue
            try:
                t = float(row.get("timestamp_sec", 0.0))
            except (TypeError, ValueError):
                t = 0.0
            sid = str(row.get("section_id") or f"Section {i + 1}")
            sec_name = section_names.get(sid, sid)
            rt = str(row.get("report_type") or "")
            label = f"{sec_name} · {rt}" if rt else sec_name
            out.append(
                {
                    "index": i + 1,
                    "t_sec": t,
                    "time_display": f"{t:.1f}s",
                    "label": label,
                    "confidence": row.get("confidence"),
                }
            )
        if out:
            return out
    return build_mark_moments_from_crew_payload(payload)


def build_mark_moments_from_crew_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """
    One timeline row per matched section — ``timestamp_sec`` is where the spoken cue starts.

    Used on the preview page so presenters can jump the embedded video to each “Mark this.” moment.
    """
    raw = payload.get("matched_sections") or []
    out: list[dict[str, Any]] = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            continue
        try:
            t = float(row.get("timestamp_sec", 0.0))
        except (TypeError, ValueError):
            t = 0.0
        sec_name = str(row.get("section_name") or "Section")
        rt = str(row.get("report_type") or "")
        label = f"{sec_name} · {rt}" if rt else sec_name
        out.append(
            {
                "index": i + 1,
                "t_sec": t,
                "time_display": f"{t:.1f}s",
                "label": label,
            }
        )
    return out


def _parse_mark_events(raw: str | None) -> list[dict[str, Any]]:
    """
    Parse the recorder's ``mark_events`` JSON into a normalized, bounded list.

    Each event: ``{"t_sec": float, "note": str, "step_index": int, "section_id": str,
    "report_type": str, "title": str}``. Invalid/oversized payloads degrade to [].
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for row in data[:200]:  # bound to avoid abuse
        if not isinstance(row, dict):
            continue
        try:
            t = float(row.get("t_sec", row.get("time", 0.0)))
        except (TypeError, ValueError):
            t = 0.0
        out.append(
            {
                "t_sec": max(0.0, t),
                "note": str(row.get("note") or "")[:500],
                "step_index": row.get("step_index"),
                "section_id": str(row.get("section_id") or "")[:120],
                "report_type": str(row.get("report_type") or "")[:120],
                "title": str(row.get("title") or "")[:200],
            }
        )
    out.sort(key=lambda r: r["t_sec"])
    return out


def _mark_moments_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build preview-timeline chips from manual mark events (shown before generation)."""
    out: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        t = float(ev.get("t_sec", 0.0))
        title = ev.get("title") or ev.get("section_id") or f"Mark {i + 1}"
        note = ev.get("note")
        label = f"{title} — {note}" if note else str(title)
        out.append(
            {
                "index": i + 1,
                "t_sec": t,
                "time_display": f"{t:.1f}s",
                "label": label[:160],
                "manual": True,
            }
        )
    return out


def _env_token_present(name: str) -> bool:
    """True if the env var exists and is non-empty (never exposed to clients)."""
    return bool(os.environ.get(name, "").strip())


def _recording_page_response(ordered: list[str], *, job_address: str = "", job_notes: str = ""):
    """Build the guided recording UI for the selected report templates."""
    session["selected_report_types"] = ordered
    if job_address or job_notes:
        session["job_address"] = job_address
        session["job_notes"] = job_notes

    by_type = templates_by_type()
    template_objs = [by_type[rt] for rt in ordered if rt in by_type]
    selected_titles = [tpl.title for tpl in template_objs]

    report_templates_json = json.dumps(
        [tpl.model_dump() for tpl in template_objs],
        indent=2,
        ensure_ascii=False,
    )

    workflow_generator_plan = load_prompt(
        "workflow_generator",
        report_templates_json=report_templates_json,
        selected_report_types=", ".join(ordered),
        job_metadata=json.dumps({"phase": "recording", "route": "recording_page"}),
        output_language="English",
    )

    workflow_bundle = build_workflow_json_bundle(
        templates=template_objs,
        selected_report_types=ordered,
    )
    workflow_bundle_json = workflow_bundle_to_pretty_json(workflow_bundle)
    workflow_steps = build_workflow_steps_from_templates(template_objs)
    merged_sections = merge_sections_for_recording(ordered)

    from crew.jobdoc_crew_v2 import build_guidance_plan

    guidance_plan = build_guidance_plan([t.model_dump() for t in template_objs])

    session["last_report_templates_json"] = report_templates_json

    return render_template(
        "recording.html",
        selected_report_types=ordered,
        selected_titles=selected_titles,
        merged_sections=merged_sections,
        workflow_steps=workflow_steps,
        workflow_bundle_json=workflow_bundle_json,
        workflow_generator_plan=workflow_generator_plan,
        guidance_plan=guidance_plan.model_dump(),
        hero_instruction=HERO_INSTRUCTION,
        identification_phrase=guidance_plan.identification_phrase,
        guided_recording=True,
        max_video_seconds=max_video_seconds(),
        job_address=job_address or session.get("job_address", ""),
        job_notes=job_notes or session.get("job_notes", ""),
        # Pre-session Voice Prompts preference (default on). Drives whether the recorder speaks
        # step prompts aloud; the in-recording Mute button can still override during capture.
        voice_prompts=session.get("voice_prompts", True),
    )


@app.route("/api/guidance", methods=["GET", "POST"])
def api_guidance_plan():
    """
    JSON guided capture plan for selected ``report_types``.

    GET: ``?report_types=home_inspection&report_types=room_inspection``
    POST: ``{"report_types": ["home_inspection"]}``
    """
    from utils.template_guidance import build_guidance_plan

    if request.method == "POST" and request.is_json:
        body = request.get_json(silent=True) or {}
        selected = list(body.get("report_types") or [])
    else:
        selected = request.args.getlist("report_types")
    if not selected:
        return jsonify({"ok": False, "error": "report_types required"}), 400
    by_type = templates_by_type()
    templates = [by_type[rt] for rt in selected if rt in by_type]
    if not templates:
        return jsonify({"ok": False, "error": "no matching templates"}), 404
    from utils.template_guidance import enrich_guidance_plan_with_agent

    plan = build_guidance_plan(templates)
    plan = enrich_guidance_plan_with_agent(plan, templates)
    return jsonify(
        {
            "ok": True,
            "guidance": plan.model_dump(),
            "step_count": len(plan.steps),
            "identification_phrase": plan.identification_phrase,
            "source": plan.source,
        }
    )


def _guidance_plan_for_report_types(selected: list[str]):
    """Build guidance plan from template JSON (single source of truth)."""
    from crew.jobdoc_crew_v2 import build_guidance_plan

    by_type = templates_by_type()
    template_objs = [by_type[rt] for rt in selected if rt in by_type]
    if not template_objs:
        return None, by_type
    plan = build_guidance_plan([t.model_dump() for t in template_objs])
    return plan, by_type


@app.route("/api/guidance/session/start", methods=["POST"])
def api_guidance_session_start():
    """
    Start a user-driven capture session. Body: ``{"report_types": ["home_inspection_detailed"]}``.
    """
    from utils.guidance_session import create_session, session_state_payload

    body = request.get_json(silent=True) or {}
    selected = list(body.get("report_types") or request.form.getlist("report_types"))
    if not selected:
        return jsonify({"ok": False, "error": "report_types required"}), 400

    plan, by_type = _guidance_plan_for_report_types(selected)
    if not plan or not plan.steps:
        return jsonify({"ok": False, "error": "no matching templates or empty guidance"}), 404

    session = create_session(plan, selected)
    payload = session_state_payload(session)
    return jsonify({"ok": True, **payload})


@app.route("/api/guidance/current_step", methods=["GET"])
def api_guidance_current_step():
    """Return the current step for a capture session: ``?session_id=...``."""
    from utils.guidance_session import get_current_step

    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"ok": False, "error": "session_id required"}), 400
    payload = get_current_step(session_id)
    if not payload:
        return jsonify({"ok": False, "error": "session not found or expired"}), 404
    return jsonify({"ok": True, **payload})


@app.route("/api/guidance/next_step", methods=["POST"])
def api_guidance_advance_step():
    """Advance to the next template section (user pressed Next Step)."""
    from utils.guidance_session import advance_step

    body = request.get_json(silent=True) or {}
    session_id = str(body.get("session_id") or request.args.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"ok": False, "error": "session_id required"}), 400
    payload = advance_step(session_id)
    if not payload:
        return jsonify({"ok": False, "error": "session not found or expired"}), 404
    return jsonify({"ok": True, **payload})


@app.route("/api/guidance/goto_step", methods=["POST"])
def api_guidance_goto_step():
    """Jump to a 1-based step (checklist review). Body: session_id, step_index."""
    from utils.guidance_session import go_to_step

    body = request.get_json(silent=True) or {}
    session_id = str(body.get("session_id") or "").strip()
    step_index = int(body.get("step_index") or 1)
    if not session_id:
        return jsonify({"ok": False, "error": "session_id required"}), 400
    payload = go_to_step(session_id, step_index)
    if not payload:
        return jsonify({"ok": False, "error": "session not found or expired"}), 404
    return jsonify({"ok": True, **payload})


@app.errorhandler(413)
def request_entity_too_large(_exc: Exception) -> tuple[Any, int]:
    """Friendly message when mobile upload exceeds JOBDOC_MAX_UPLOAD_MB."""
    limit = os.environ.get("JOBDOC_MAX_UPLOAD_MB", "500")
    if request.headers.get("X-JobDoc-Client") == "1":
        return jsonify({"ok": False, "error": f"Video exceeds upload limit ({limit} MB)."}), 413
    flash(f"Video too large (max {limit} MB).")
    return redirect(url_for("index")), 413


def _wants_json() -> bool:
    """True when the caller is the SPA recorder/JS or explicitly wants JSON."""
    return (
        request.headers.get("X-JobDoc-Client") == "1"
        or request.is_json
        or request.path.startswith("/api/")
        or "application/json" in (request.headers.get("Accept", ""))
    )


def _error_response(code: int, title: str, message: str):
    """Render a friendly error page (HTML) or JSON for API/SPA clients."""
    if _wants_json():
        return jsonify({"ok": False, "error": message}), code
    try:
        return render_template("error.html", code=code, title=title, message=message), code
    except Exception:  # noqa: BLE001 — never let the error page itself fail
        return f"{code} — {title}: {message}", code


@app.errorhandler(404)
def _not_found(_exc: Exception):
    return _error_response(
        404, "Page not found",
        "That link doesn’t exist. Head back to start a new report.",
    )


@app.errorhandler(429)
def _too_many_requests(_exc: Exception):
    return _error_response(
        429, "Slow down a moment",
        "This public Space limits how often reports can be submitted. Please wait a moment and try again.",
    )


@app.errorhandler(500)
def _server_error(exc: Exception):
    logger.exception("Unhandled 500: %s", exc)
    return _error_response(
        500, "Something went wrong",
        "An unexpected error occurred. Your recording is safe — please try again, or start a new report.",
    )


@app.after_request
def _security_headers(resp):
    """Apply conservative security headers suitable for a public mobile web app."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Camera + microphone are required for in-browser recording on this same origin only.
    resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(self), geolocation=()")
    return resp


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login():
    """Minimal shared-credential login that gates the hosted demo."""
    if not _auth_enabled() or session.get("authed"):
        return redirect(url_for("index"))
    error = None
    next_url = request.values.get("next") or url_for("index")
    # Only allow same-origin relative redirects (avoid open-redirect).
    if not next_url.startswith("/"):
        next_url = url_for("index")
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if _check_credentials(username, password):
            session["authed"] = True
            session.permanent = True
            return redirect(next_url)
        error = "Incorrect username or password. Please try again."
    return render_template("login.html", error=error, next_url=next_url), (401 if error else 200)


@app.route("/logout")
def logout():
    """Clear the session (including any per-session API keys) and return to login."""
    credentials.clear_keys(session.get("kid", ""))
    session.pop("authed", None)
    session.pop("stt_backend", None)
    flash("You've been signed out. Your API keys for this session have been cleared.")
    return redirect(url_for("login"))


@app.route("/manifest.webmanifest")
def web_manifest():
    """PWA manifest (served with the correct content type for installability)."""
    manifest = {
        "name": "GenerSwift - JobDoc Demo v1",
        "short_name": "GenerSwift",
        "description": "GenerSwift, a Barren Business Development Automation Product — built in partnership and for JobDoc. Record a narrated walkthrough and generate a professional PDF report on site.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0f172a",
        "theme_color": "#0f172a",
        "icons": [
            {"src": url_for("static", filename="icon.svg"), "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"},
        ],
    }
    resp = app.make_response(jsonify(manifest))
    resp.headers["Content-Type"] = "application/manifest+json"
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/sw.js")
def service_worker():
    """Service worker served from root scope so it can control the whole origin."""
    resp = send_from_directory(STATIC_DIR, "sw.js")
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/")
def index():
    reports = load_report_templates()
    clients = Client.query.order_by(Client.name.asc()).all()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return render_template(
        "index.html",
        reports=reports,
        clients=clients,
        today=today,
        default_inspector=session.get("inspector_name", ""),
        key_status=credentials.effective_key_status(session_token()),
        acceleration=gpu_utils.acceleration_status(),
        job_address=session.get("job_address", ""),
        job_notes=session.get("job_notes", ""),
    )


# -----------------------------------------------------------------------------
# Client management (Phase 2 — client-aware capture)
# -----------------------------------------------------------------------------


def _resolve_or_create_client(form: Any) -> tuple[Client | None, str | None]:
    """
    Resolve the selected client, creating one on the fly when new-client fields are filled.

    Returns ``(client, error_message)``. ``client`` is ``None`` when an error occurred.
    """
    client_id = (form.get("client_id") or "").strip()
    if client_id and client_id.isdigit():
        client = db.session.get(Client, int(client_id))
        if client:
            return client, None
        return None, "Selected client could not be found. Pick another or create a new one."

    # New client: name and email are the only required fields (everything else optional).
    name = (form.get("new_client_name") or "").strip()[:255]
    email = (form.get("new_client_email") or "").strip()[:255]
    if not name:
        return None, "Select an existing client or enter a client name to create a new one."
    if not email:
        return None, "A client email is required to create a new client."

    client = Client(
        name=name,
        company=(form.get("new_client_company") or "").strip()[:255] or None,
        email=email or None,
        phone=(form.get("new_client_phone") or "").strip()[:64] or None,
        address=(form.get("new_client_address") or "").strip()[:500] or None,
        notes=(form.get("new_client_notes") or "").strip() or None,
    )
    db.session.add(client)
    db.session.commit()
    return client, None


@app.route("/clients")
def clients_list():
    """Client directory: list existing clients and create new ones."""
    clients = Client.query.order_by(Client.name.asc()).all()
    return render_template("clients.html", clients=clients)


@app.route("/clients/create", methods=["POST"])
def clients_create():
    """Create a client from the client directory form."""
    name = (request.form.get("name") or request.form.get("new_client_name") or "").strip()[:255]
    if not name:
        flash("A client name is required.")
        return redirect(url_for("clients_list"))
    client = Client(
        name=name,
        company=(request.form.get("company") or "").strip()[:255] or None,
        email=(request.form.get("email") or "").strip()[:255] or None,
        phone=(request.form.get("phone") or "").strip()[:64] or None,
        address=(request.form.get("address") or "").strip()[:500] or None,
        notes=(request.form.get("notes") or "").strip() or None,
    )
    db.session.add(client)
    db.session.commit()
    flash(f"Client “{client.name}” created.")
    return redirect(url_for("clients_list"))


@app.route("/clients/<int:client_id>")
def client_detail(client_id: int):
    """Client profile: past capture sessions with Job Info Summary access."""
    client = Client.query.get_or_404(client_id)
    sessions = (
        CaptureSession.query.filter_by(client_id=client.id)
        .order_by(CaptureSession.started_at.desc())
        .all()
    )
    return render_template("client_detail.html", client=client, sessions=sessions)


@app.route("/api/clients", methods=["GET", "POST"])
def api_clients():
    """
    Search clients (GET ``?q=...``) or create one (POST JSON/form) for the start-session UI.
    """
    if request.method == "POST":
        body = request.get_json(silent=True) or request.form
        name = (body.get("name") or "").strip()[:255]
        if not name:
            return jsonify({"ok": False, "error": "name is required"}), 400
        client = Client(
            name=name,
            company=(body.get("company") or "").strip()[:255] or None,
            email=(body.get("email") or "").strip()[:255] or None,
            phone=(body.get("phone") or "").strip()[:64] or None,
            address=(body.get("address") or "").strip()[:500] or None,
            notes=(body.get("notes") or "").strip() or None,
        )
        db.session.add(client)
        db.session.commit()
        return jsonify({"ok": True, "client": client.to_dict()})

    q = (request.args.get("q") or "").strip()
    query = Client.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Client.name.ilike(like),
                Client.company.ilike(like),
                Client.email.ilike(like),
            )
        )
    clients = query.order_by(Client.name.asc()).limit(50).all()
    return jsonify({"ok": True, "clients": [c.to_dict() for c in clients]})


@app.route("/start_session", methods=["POST"])
def start_session():
    """
    Client-aware capture start: select/create a client, save session metadata, then record.

    Mandatory: a client (existing client, or a new one with name + email) + at least one report
    type. All other details (address, inspector, weather, access notes) are optional. The
    always-present Misc step is appended automatically before recording begins.
    """
    selected = request.form.getlist("report_types")
    if not selected:
        flash("Select a report template before continuing.")
        return redirect(url_for("index"))

    # Job / property address is now optional — only the client (name + email) is required.
    job_address = (request.form.get("job_address") or "").strip()[:500]

    client, error = _resolve_or_create_client(request.form)
    if error or not client:
        flash(error or "Could not resolve a client.")
        return redirect(url_for("index"))

    seen: set[str] = set()
    ordered: list[str] = []
    for rt in selected:
        if rt not in seen and rt != MISC_REPORT_TYPE:
            seen.add(rt)
            ordered.append(rt)

    inspector_name = (request.form.get("inspector_name") or "").strip()[:255]
    inspection_date = (request.form.get("inspection_date") or "").strip()[:32]
    weather = (request.form.get("weather") or "").strip()[:255]
    access_notes = (request.form.get("access_notes") or "").strip()

    # Voice-prompt preference for the whole session. The checkbox is checked by default, so an
    # absent value means the user turned it OFF. On-screen text always shows regardless.
    voice_prompts = (request.form.get("voice_prompts") or "").strip().lower() in ("on", "1", "true", "yes")

    capture = CaptureSession(
        client_id=client.id,
        report_type=ordered[0] if ordered else None,
        job_address=job_address,
        inspector_name=inspector_name or None,
        inspection_date=inspection_date or None,
        weather=weather or None,
        access_notes=access_notes or None,
        status="recording",
    )
    db.session.add(capture)
    db.session.commit()

    session["capture_session_id"] = capture.id
    session["inspector_name"] = inspector_name
    session["job_address"] = job_address
    session["job_notes"] = access_notes
    session["voice_prompts"] = voice_prompts

    job_notes_parts = [f"Client: {client.display_label}"]
    if inspector_name:
        job_notes_parts.append(f"Inspector: {inspector_name}")
    if weather:
        job_notes_parts.append(f"Weather: {weather}")
    if access_notes:
        job_notes_parts.append(access_notes)
    job_notes = " · ".join(job_notes_parts)[:1000]

    ordered = append_misc_report_type(ordered)
    return _recording_page_response(ordered, job_address=job_address, job_notes=job_notes)


@app.route("/health")
def health():
    """
    Readiness probe: API keys + WeasyPrint + runtime GPU/acceleration (never returns secrets).

    GPU and ffmpeg hardware-accel detection runs at request time inside the container —
    this endpoint is the canonical way to confirm a GPU Space is actually using CUDA.
    """
    wp = check_weasyprint_status(BASE_DIR, auto_install=False)
    token = session_token()
    key_status = credentials.effective_key_status(token)
    keys_ok = key_status["DEEPGRAM_API_KEY"]["present"] and key_status["OPENAI_API_KEY"]["present"]
    pdf_ok = wp.ready
    accel = gpu_utils.acceleration_status()
    return jsonify(
        {
            "ok": keys_ok and pdf_ok,
            "service": "GenerSwift - JobDoc Demo v1",
            "pipeline": get_jobdoc_pipeline(),
            "keys": {
                "DEEPGRAM_API_KEY": key_status["DEEPGRAM_API_KEY"]["present"],
                "OPENAI_API_KEY": key_status["OPENAI_API_KEY"]["present"],
            },
            "key_sources": {name: info["source"] for name, info in key_status.items()},
            "providers": {
                "transcription": _effective_stt_backend(),
                "llm": "openai",
            },
            "acceleration": accel,
            "max_video_seconds": max_video_seconds(),
            "weasyprint": wp.to_dict(),
            # Mobile/browser recording readiness — helps diagnose camera/mic issues on Spaces.
            # getUserMedia requires a secure (HTTPS) context; HF serves Spaces over HTTPS.
            "recording": {
                "https_required": True,
                "max_recording_seconds": max_video_seconds(),
                "upload_endpoint": url_for("upload_video"),
                "accepts": ["video/mp4", "video/webm", "video/quicktime", "video/x-matroska"],
                "supports_manual_marks": True,
                "recommended_browsers": {
                    "ios": "Safari (Chrome/Firefox on iOS cannot record video)",
                    "android": "Chrome",
                },
                "tip": "If the camera is blocked, allow Camera + Microphone for this site and reload.",
            },
            "ready_for_production": keys_ok and pdf_ok,
            "messages": {
                "pdf": wp.message if pdf_ok else (wp.install_hint or wp.message),
                "keys": (
                    "API keys configured."
                    if keys_ok
                    else "Add your Deepgram + OpenAI keys in the UI, or set them as Space secrets."
                ),
                "acceleration": accel["label"],
            },
        }
    )


def _dir_usage_mb(directory: Path) -> float:
    """Sum of file sizes under ``directory`` in MB (best-effort, never raises)."""
    total = 0
    try:
        for path in directory.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0.0
    return round(total / (1024 * 1024), 2)


def _recent_upload_count(hours: float = 24.0) -> int:
    """Count upload files modified within the last ``hours`` (proxy for recent jobs)."""
    cutoff = time.time() - hours * 3600.0
    count = 0
    try:
        for path in UPLOADS_DIR.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime >= cutoff:
                    count += 1
            except OSError:
                continue
    except OSError:
        return 0
    return count


@app.route("/metrics")
def metrics():
    """
    Lightweight, non-sensitive operational metrics for monitoring a public Space.

    Exposes uptime, job counts, disk usage, and the runtime acceleration mode. Contains no
    secrets, API keys, or user content — safe to leave reachable behind the login gate.
    """
    uptime = max(0.0, time.time() - _START_TIME)
    try:
        with app.app_context():
            jobs_total = db.session.query(VideoUpload).count()
    except Exception:  # noqa: BLE001
        jobs_total = None
    uploads_mb = _dir_usage_mb(UPLOADS_DIR)
    reports_mb = _dir_usage_mb(OUTPUT_DIR)
    return jsonify(
        {
            "ok": True,
            "service": "GenerSwift - JobDoc Demo v1",
            "uptime_seconds": round(uptime, 1),
            "uptime_human": _format_uptime(uptime),
            "started_at": datetime.fromtimestamp(_START_TIME, tz=timezone.utc).isoformat(),
            "jobs": {
                "total": jobs_total,
                "recent_uploads_24h": _recent_upload_count(24.0),
            },
            "disk": {
                "uploads_mb": uploads_mb,
                "reports_mb": reports_mb,
                "total_mb": round(uploads_mb + reports_mb, 2),
                "retention_hours": maintenance.retention_hours(),
            },
            "acceleration": gpu_utils.acceleration_status().get("media_mode", "cpu"),
            "pipeline": get_jobdoc_pipeline(),
        }
    )


def _format_uptime(seconds: float) -> str:
    secs = int(seconds)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


STT_BACKENDS = ("deepgram", "local", "auto")


def _effective_stt_backend() -> str:
    """The transcription backend that will actually be used (session pref → env default)."""
    pref = (session.get("stt_backend") or "").strip().lower()
    if pref in STT_BACKENDS:
        return pref
    return (os.environ.get("JOBDOC_STT_BACKEND", "deepgram").strip().lower() or "deepgram")


def _keys_payload(token: str) -> dict[str, Any]:
    """Non-sensitive key + provider status (never includes key material)."""
    status = credentials.effective_key_status(token)
    return {
        "ok": True,
        "keys": {name: info for name, info in status.items()},
        "ready": status["DEEPGRAM_API_KEY"]["present"] and status["OPENAI_API_KEY"]["present"],
        "providers": {
            "transcription": _effective_stt_backend(),
            "llm": "openai",
        },
        "gpu_available": gpu_utils.is_gpu_available(),
    }


@app.route("/api/keys", methods=["GET", "POST"])
def api_keys():
    """
    Store user-supplied API keys + provider prefs for this browser session, or report status.

    POST body (form or JSON): ``deepgram_api_key``, ``openai_api_key``, ``stt_backend``.
    Keys are held server-side keyed by a random session token, are **never logged or echoed
    back** (only presence + source), and user-pasted keys take precedence over Space secrets.
    """
    token = session_token()
    if request.method == "POST":
        body = request.get_json(silent=True) or request.form
        credentials.remember_keys(
            token,
            {
                "DEEPGRAM_API_KEY": (body.get("deepgram_api_key") or "").strip(),
                "OPENAI_API_KEY": (body.get("openai_api_key") or "").strip(),
            },
        )
        backend = (body.get("stt_backend") or "").strip().lower()
        if backend in STT_BACKENDS:
            session["stt_backend"] = backend
    return jsonify(_keys_payload(token))


@app.route("/settings/keys")
def manage_keys():
    """Dedicated, mobile-friendly page to manage per-session API keys + providers."""
    return render_template(
        "keys.html",
        key_status=credentials.effective_key_status(session_token()),
        stt_backend=_effective_stt_backend(),
        gpu_available=gpu_utils.is_gpu_available(),
    )


@app.route("/setup/weasyprint")
def setup_weasyprint_page():
    """Friendly fix page when GTK / WeasyPrint is missing (Windows field installs)."""
    _refresh_weasyprint_status(auto_install=False)
    if WEASYPRINT_STATUS.ready:
        flash("PDF export libraries are ready.")
        return redirect(url_for("index"))
    return render_template("setup_weasyprint.html", status=WEASYPRINT_STATUS)


@app.route("/setup/weasyprint/install", methods=["POST"])
def setup_weasyprint_install():
    """Browser one-click retry: portable extract GTK into project gtk3/runtime."""
    os.environ["JOBDOC_AUTO_INSTALL_GTK"] = "1"
    os.environ["JOBDOC_PREFER_PORTABLE_GTK"] = "1"
    _refresh_weasyprint_status(auto_install=True)
    if WEASYPRINT_STATUS.ready:
        flash(
            "PDF libraries are ready. Restart run_demo.bat once, then generate your PDF."
        )
        return redirect(url_for("index"))
    flash(
        "Automatic setup did not finish. Use Option A (Run as administrator) on this page, "
        "or install 7-Zip and click Fix dependencies again."
    )
    return render_template("setup_weasyprint.html", status=WEASYPRINT_STATUS)


@app.route("/setup/weasyprint/required")
def setup_weasyprint_required():
    """Full-page friendly blocker when PDF export is requested without GTK."""
    _refresh_weasyprint_status(auto_install=False)
    if WEASYPRINT_STATUS.ready:
        return redirect(url_for("index"))
    return render_template("weasyprint_required.html")


@app.route("/setup/weasyprint/retry")
def setup_weasyprint_retry():
    """Re-check after user installs GTK manually."""
    _refresh_weasyprint_status(auto_install=False)
    if WEASYPRINT_STATUS.ready:
        flash("WeasyPrint is ready.")
        return redirect(url_for("index"))
    return redirect(url_for("setup_weasyprint_page"))


@app.route("/start_recording", methods=["POST"])
def start_recording():
    """
    Build the recording view from selected templates + workflow generator.

    The **Workflow Generator** path here is intentionally synchronous and template-driven:
    we render ``prompts/workflow_generator.txt`` with the same JSON the Crew will see, and
    we also build a deterministic ``workflow_bundle`` from ``utils/workflow_stub`` so the
    UI and backend stay aligned without calling an LLM on this route.
    """
    selected = request.form.getlist("report_types")
    if not selected:
        flash("Select at least one report before continuing.")
        return redirect(url_for("index"))

    seen: set[str] = set()
    ordered: list[str] = []
    for rt in selected:
        if rt not in seen:
            seen.add(rt)
            ordered.append(rt)

    job_address = (request.form.get("job_address") or "").strip()[:200]
    job_notes = (request.form.get("job_notes") or "").strip()[:1000]
    # The Misc / Supplemental step is always appended as the final section of every recording.
    ordered = append_misc_report_type(ordered)
    return _recording_page_response(ordered, job_address=job_address, job_notes=job_notes)


@app.route("/upload_video", methods=["POST"])
@rate_limited("upload")
def upload_video():
    """
    Accept multipart video plus ``report_types``.

    Browser clients send ``X-JobDoc-Client: 1`` to receive JSON + redirect URL for SPA-style flows.
    """
    selected = request.form.getlist("report_types")
    if not selected:
        flash("Missing report selection on upload.")
        return redirect(url_for("index"))

    wants_json = request.headers.get("X-JobDoc-Client") == "1"

    file = request.files.get("video")
    if not file or file.filename == "":
        flash("Please choose a video file.")
        return _recording_page_response(selected)

    original = file.filename or "recording.bin"
    ext = Path(original).suffix.lower()
    if not ext:
        ext = ".webm"
    if ext not in {".webm", ".mp4", ".mov", ".mkv", ".bin"}:
        # MediaRecorder typically yields .webm; keep lenient for mobile MIME quirks.
        ext = ".webm"
    stored = build_unique_upload_basename(selected, ext)
    out_path = UPLOADS_DIR / stored
    # A slow/temporarily-unavailable Volume can make the uploads dir missing — recreate it,
    # then persist the upload. Surface a friendly, retryable error rather than a 500.
    try:
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        file.save(out_path)
    except OSError as exc:
        logger.error("Upload save failed for %s: %s", stored, exc)
        msg = "Storage was busy saving your video. Please try uploading again in a moment."
        if wants_json:
            return jsonify({"ok": False, "error": msg}), 503
        flash(msg)
        return _recording_page_response(selected)

    # Reject empty/truncated uploads early with a clear message (avoids confusing pipeline errors).
    try:
        saved_size = out_path.stat().st_size if out_path.exists() else 0
    except OSError:
        saved_size = 0
    if saved_size == 0:
        out_path.unlink(missing_ok=True)  # type: ignore[call-arg]
        msg = "The uploaded video was empty. Please re-record and try again."
        log_job_step(None, "upload", "empty_upload_rejected")
        if wants_json:
            return jsonify({"ok": False, "error": msg}), 400
        flash(msg)
        return _recording_page_response(selected)
    log_job_step(None, "upload", f"received bytes={saved_size} name={stored}")

    # Mobile compatibility: iOS uploads `.mov`, Android MediaRecorder uploads `.webm`
    # (which iOS Safari cannot play back). Normalize to `.mp4` immediately — GPU-accelerated
    # when available — so the preview video plays everywhere and the pipeline (audio, frame
    # seeking, transcription) has a consistent container.
    if out_path.suffix.lower() in {".webm", ".mov", ".mkv", ".bin"}:
        from utils.video_utils import normalize_video_to_mp4

        try:
            mp4_path = normalize_video_to_mp4(out_path)
        except Exception as exc:  # noqa: BLE001 — keep original on failure; pipeline still tries
            log_job_step(None, "upload", f"normalize_failed: {exc}")
            mp4_path = out_path
        if mp4_path != out_path:
            try:
                out_path.unlink(missing_ok=True)  # type: ignore[call-arg]
            except TypeError:
                if out_path.exists():
                    out_path.unlink()
            out_path = mp4_path
            stored = out_path.name

    # Guardrail: reject overly long recordings (limit is higher on GPU Spaces).
    duration = probe_duration_seconds(out_path)
    limit = max_video_seconds()
    if duration and duration > limit + 5:
        try:
            out_path.unlink(missing_ok=True)  # type: ignore[call-arg]
        except TypeError:
            if out_path.exists():
                out_path.unlink()
        msg = (
            f"Recording is {int(duration)}s — the limit on this Space is {limit}s "
            f"({limit // 60} min). Record a shorter walkthrough and try again."
        )
        if wants_json:
            return jsonify({"ok": False, "error": msg}), 413
        flash(msg)
        return _recording_page_response(selected)

    # Capture explicit mark events from the recorder (exact timestamps + optional notes).
    mark_events = _parse_mark_events(request.form.get("mark_events"))
    # Step boundaries from the single continuous recording (when each guided step began).
    step_events = _parse_mark_events(request.form.get("step_events"))

    session["selected_report_types"] = selected
    session["last_upload_filename"] = stored

    initial_meta = _default_job_meta()
    # Job details (address / notes) — captured on the home screen, used on the PDF cover.
    job_address = (request.form.get("job_address") or session.get("job_address") or "").strip()[:200]
    job_notes = (request.form.get("job_notes") or session.get("job_notes") or "").strip()[:1000]
    if job_address:
        initial_meta["job_address"] = job_address
    if job_notes:
        initial_meta["job_notes"] = job_notes
    if mark_events:
        initial_meta["mark_events"] = mark_events
        initial_meta["mark_moments"] = _mark_moments_from_events(mark_events)
    if step_events:
        initial_meta["step_events"] = step_events

    # Link this upload to the active client-aware capture session (new flow).
    capture_session_id = session.get("capture_session_id")
    record = VideoUpload(
        stored_filename=stored,
        original_filename=original,
        report_types_json=json.dumps(selected),
        job_meta=json.dumps(initial_meta, ensure_ascii=False),
        capture_session_id=capture_session_id,
    )
    db.session.add(record)

    if capture_session_id:
        capture = db.session.get(CaptureSession, capture_session_id)
        if capture and capture.status in ("started", "recording"):
            capture.status = "uploaded"
            db.session.add(capture)

    db.session.commit()

    session["last_video_id"] = record.id

    if wants_json:
        return jsonify(
            {
                "ok": True,
                "video_id": record.id,
                "filename": stored,
                "redirect_url": url_for("report_preview_detail", video_id=record.id),
            }
        )

    return redirect(url_for("report_preview_detail", video_id=record.id))


@app.route("/report_preview/<int:video_id>")
def report_preview_detail(video_id: int):
    """Review screen after an upload (shareable URL per ``video_id``)."""
    record = VideoUpload.query.get_or_404(video_id)
    selected = json.loads(record.report_types_json)
    by_type = templates_by_type()
    selected_titles = [by_type[rt].title for rt in selected if rt in by_type]
    meta = parse_job_meta(record)
    gallery = meta.get("frame_gallery") or []
    mark_moments = meta.get("mark_moments") or []
    capture_session_id = getattr(record, "capture_session_id", None)
    return render_template(
        "report_preview.html",
        saved_filename=record.stored_filename,
        selected_report_types=selected,
        selected_titles=selected_titles,
        video_id=record.id,
        capture_session_id=capture_session_id,
        job_status=meta,
        frame_gallery=gallery,
        mark_moments=mark_moments,
        pipeline_version=get_jobdoc_pipeline(),
        review_items=meta.get("review_items") or [],
        low_confidence_messages=meta.get("low_confidence_messages") or [],
        is_v2=is_v2_pipeline(),
        acceleration=gpu_utils.acceleration_status(),
        key_status=credentials.effective_key_status(session_token()),
    )


@app.route("/status/<int:video_id>")
def upload_status(video_id: int):
    """Rich JSON for polling: pipeline steps, descriptions, gallery hints, friendly errors."""
    record = VideoUpload.query.get_or_404(video_id)
    meta = parse_job_meta(record)
    steps = meta.get("pipeline_steps")
    if not steps:
        steps = default_pipeline_steps()
    return jsonify(
        {
            "video_id": record.id,
            "filename": record.stored_filename,
            "reports": json.loads(record.report_types_json),
            "job": meta,
            "pipeline_steps": steps,
            "current_step": meta.get("progress_step"),
            "current_detail": meta.get("detail"),
            "friendly_error": meta.get("friendly_error"),
            "pipeline_notes": meta.get("pipeline_notes") or [],
            "frame_gallery": meta.get("frame_gallery") or [],
            "mark_moments": meta.get("mark_moments") or [],
            "pipeline": meta.get("pipeline") or get_jobdoc_pipeline(),
            "review_items": meta.get("review_items") or [],
            "low_confidence_messages": meta.get("low_confidence_messages") or [],
            "draft_ready": bool(meta.get("draft_crew_result")),
            "degraded": bool(meta.get("degraded")),
        }
    )


@app.route("/uploaded_video/<int:video_id>")
def serve_uploaded_video(video_id: int):
    """
    Serve the original recording for in-browser playback on the preview page.

    Scoped to the row’s ``stored_filename`` so arbitrary paths cannot be read.
    """
    record = VideoUpload.query.get_or_404(video_id)
    return send_from_directory(UPLOADS_DIR, record.stored_filename)


@app.route("/frames/<int:video_id>/<path:filename>")
def serve_frame(video_id: int, filename: str):
    """Serve extracted PNGs for the preview gallery (filename basename only)."""
    safe = Path(filename).name
    folder = OUTPUT_DIR / "frames" / str(video_id)
    target = folder / safe
    if not target.is_file():
        return ("Not found", 404)
    return send_from_directory(folder, safe)


def _cleanup_intermediate_media(video_path: Path) -> None:
    """
    Best-effort removal of derived temp files after a job (keeps the Space disk tidy).

    Removes the extracted ``*_audio.wav`` sidecar. The original upload + frames are kept
    (the preview page plays the video and shows the gallery). Never raises.
    """
    try:
        wav = video_path.with_name(f"{video_path.stem}_audio.wav")
        if wav.exists():
            wav.unlink()
    except Exception as exc:  # noqa: BLE001
        logger.debug("cleanup skipped: %s", exc)
    # Release any cached CUDA memory used by the media pipeline.
    gpu_utils.release_gpu_memory()


def _fail_job(
    record: VideoUpload,
    steps_state: list[dict[str, Any]],
    step: str,
    exc: BaseException,
    *,
    pipeline: str,
) -> tuple[dict[str, Any], int]:
    """Hard failure — only for missing files / invalid setup (not crew degradation)."""
    friendly = friendly_error_message(exc)
    log_job_step(record.id, step, f"hard_fail: {friendly}", extra={"exc": str(exc)})
    steps_state = sync_steps_from_progress(
        steps_state, step, friendly, failed=True, pipeline=pipeline
    )
    save_job_meta(
        record,
        {
            "status": "failed",
            "detail": str(exc),
            "progress_step": "failed",
            "pipeline_steps": steps_state,
            "friendly_error": friendly,
            "pipeline": pipeline,
        },
    )
    if isinstance(exc, PipelineError) and exc.step == "transcription":
        status_code = 400
    elif isinstance(exc, (RuntimeError, OSError, FileNotFoundError, ValueError)):
        status_code = 400
    else:
        status_code = 500
    return {"ok": False, "error": friendly, "video_id": record.id}, status_code


def _warn_if_uploaded_marks_unused(
    video_id: int,
    mark_events: list[dict[str, Any]],
    payload: dict[str, Any],
    final_model: FinalReportOutput | None,
) -> None:
    """ERROR if the recorder sent N>0 taps but matching assigned none of them."""
    n_uploaded = len(mark_events or [])
    if n_uploaded <= 0:
        return
    taps_used = 0
    for row in payload.get("matched_sections") or []:
        if isinstance(row, dict) and str(row.get("trigger_phrase") or "") == "manual_mark":
            taps_used += 1
    if taps_used == 0 and final_model is not None:
        for bundle in final_model.reports.values():
            for sec in bundle.sections:
                for fr in sec.frames or []:
                    if getattr(fr, "source", "") == "manual_mark":
                        taps_used += 1
    if taps_used == 0:
        logger.error(
            "mark_events uploaded but matching used 0 taps video_id=%s N=%s",
            video_id, n_uploaded,
        )
        log_job_step(video_id, "generate_report", f"ERROR mark_events N={n_uploaded} but matching used 0 taps")


def _run_v2_crew_phase(
    record: VideoUpload,
    video_path: Path,
    *,
    templates_json: str,
    steps_state: list[dict[str, Any]],
    pipeline: str,
    progress: Any,
    review_notes_json: str | None = None,
    mark_events: list[dict[str, Any]] | None = None,
    step_events: list[dict[str, Any]] | None = None,
) -> tuple[FinalReportOutput, dict[str, Any], list[str], list[dict[str, Any]], list[str], bool]:
    from crew.flask_pipeline import run_v2_pipeline

    outcome = run_v2_pipeline(
        video_path,
        templates_json=templates_json,
        on_progress=progress,
        video_id=record.id,
        review_notes_json=review_notes_json,
        mark_events=mark_events,
        step_events=step_events,
    )
    return (
        outcome.final_report,
        outcome.payload,
        outcome.pipeline_warnings,
        outcome.review_items,
        outcome.low_confidence_messages,
        outcome.degraded,
    )


def _build_pdf_for_job(
    record: VideoUpload,
    final_model: FinalReportOutput,
    video_path: Path,
    selected: list[str],
    steps_state: list[dict[str, Any]],
    pipeline: str,
    pipeline_warnings: list[str],
    *,
    progress: Any,
    fast: bool = False,
) -> tuple[str, str, list[dict[str, str]], list[str]]:
    """Frames → HTML → PDF. Returns (pdf_name, download_url, gallery, extra_warnings)."""
    extra_warnings = list(pipeline_warnings)
    progress(
        "pdf",
        "Generating light PDF — extracting still frames…" if fast
        else "Generating PDF — extracting still frames…",
    )
    frames_dir = OUTPUT_DIR / "frames" / str(record.id)
    # Real MARK taps power the whole-report image safety net: if per-section extraction yields
    # nothing (fully degraded run), each section recovers a still from its OWN tap. The taps are
    # passed whole — their `section_id` is what keeps one area's photo off another's heading.
    _pdf_meta = parse_job_meta(record)
    _mark_events = [ev for ev in (_pdf_meta.get("mark_events") or []) if isinstance(ev, dict)]
    frame_result = enrich_report_with_still_frames_resilient(
        final_model,
        video_path,
        frames_dir,
        fast=fast,
        mark_times=[float(ev.get("t_sec", 0.0)) for ev in _mark_events],
        mark_events=_mark_events,
    )
    enriched = frame_result.final
    extra_warnings.extend(frame_result.warnings)

    # The Misc / Supplemental step is captured for future document generation but is never
    # printed in the client-facing PDF — strip it from the model used for HTML/gallery.
    if MISC_REPORT_TYPE in enriched.reports:
        enriched = enriched.model_copy(
            update={
                "reports": {
                    k: v for k, v in enriched.reports.items() if k != MISC_REPORT_TYPE
                }
            }
        )

    gallery: list[dict[str, str]] = []
    for rt, bundle in enriched.reports.items():
        for sec in bundle.sections:
            if sec.image_uri and sec.image_path:
                fn = Path(sec.image_path).name
                gallery.append(
                    {
                        "label": f"{bundle.title} — {sec.section_name}",
                        "url": url_for("serve_frame", video_id=record.id, filename=fn),
                    }
                )

    tpl_map = templates_by_type()
    meta = parse_job_meta(record)
    job_addr = str(meta.get("job_address") or meta.get("property_address") or "").strip()
    try:
        html_inner = render_report_html(
            app,
            enriched,
            templates_by_type={rt: tpl_map[rt] for rt in enriched.reports if rt in tpl_map},
            job_addresses={rt: job_addr for rt in enriched.reports} if job_addr else None,
        )
    except Exception as exc:  # noqa: BLE001
        log_job_step(record.id, "pdf", f"html_render_fail: {exc}")
        raise

    if not html_inner.strip():
        raise RuntimeError("Report HTML was empty — check templates under templates/report_templates/")

    pdf_name = f"report_{record.id}_{uuid.uuid4().hex[:10]}.pdf"
    pdf_path = OUTPUT_DIR / pdf_name
    try:
        write_pdf(app, html_inner, base_dir=BASE_DIR, output_path=pdf_path)
    except Exception as exc:  # noqa: BLE001
        log_job_step(record.id, "pdf", f"weasyprint_fail: {exc}")
        raise RuntimeError(friendly_error_message(exc)) from exc

    download_url = url_for("download_report", video_id=record.id)
    return pdf_name, download_url, gallery, extra_warnings


def _extract_misc_payload(
    payload: dict[str, Any], final_model: FinalReportOutput
) -> tuple[str, dict[str, Any]]:
    """
    Split the always-present Misc step out of the report results for ``MiscData``.

    Returns ``(raw_transcript, summarized_data)``:
      - ``raw_transcript`` — verbatim narration tied to the Misc step (cleaned observations,
        falling back to the full transcript text so nothing is lost).
      - ``summarized_data`` — the structured Misc bundle (section summaries + field values),
        ready to query later for invoices / proposals / estimates.
    """
    misc_bundle = final_model.reports.get(MISC_REPORT_TYPE)
    summarized: dict[str, Any] = misc_bundle.model_dump() if misc_bundle else {}

    misc_lines: list[str] = []
    analysis = payload.get("transcript_analysis") or {}
    for obs in analysis.get("observations") or []:
        if isinstance(obs, dict) and obs.get("report_type") == MISC_REPORT_TYPE:
            text = str(obs.get("cleaned_observation") or "").strip()
            if text:
                misc_lines.append(text)

    if misc_lines:
        raw_transcript = "\n".join(misc_lines)
    else:
        raw_transcript = str(analysis.get("full_transcript_text") or "").strip()

    return raw_transcript, summarized


def _structured_data_for_summary(
    payload: dict[str, Any], final_model: FinalReportOutput
) -> dict[str, Any]:
    """Clean structured report data (Misc excluded) for ``SessionSummary.structured_data``."""
    reports = {
        rt: bundle.model_dump()
        for rt, bundle in final_model.reports.items()
        if rt != MISC_REPORT_TYPE
    }
    return {
        "generated_at_utc": final_model.generated_at_utc,
        "identification_phrase": final_model.identification_phrase,
        "pipeline": payload.get("pipeline"),
        "reports": reports,
        "mark_moments": payload.get("matched_sections") or [],
    }


def _persist_session_results(
    record: VideoUpload,
    payload: dict[str, Any],
    final_model: FinalReportOutput,
    pdf_name: str | None,
) -> None:
    """
    Finalize the client-aware data model for a completed capture session.

    Writes (idempotently) the structured ``SessionSummary``, the separate ``MiscData`` record,
    and marks the ``CaptureSession`` completed with its PDF filename. Never raises — persistence
    failures must not break the report response.
    """
    session_id = getattr(record, "capture_session_id", None)
    if not session_id:
        return
    try:
        capture = db.session.get(CaptureSession, session_id)
        if not capture:
            return

        structured = _structured_data_for_summary(payload, final_model)
        summary = (
            SessionSummary.query.filter_by(capture_session_id=capture.id).first()
            or SessionSummary(capture_session_id=capture.id)
        )
        summary.structured_data = structured
        db.session.add(summary)

        # Always persist a MiscData row for a completed session (even if empty) so supplemental
        # data is reliably queryable later for on-demand invoices / proposals.
        raw_transcript, summarized = _extract_misc_payload(payload, final_model)
        # Link the full source recording to the Misc data so the entire audio/video is
        # retrievable for auditing without a schema migration (stored in the summarized JSON).
        recording_ref = {
            "video_id": record.id,
            "stored_filename": record.stored_filename,
            "original_filename": record.original_filename,
            "duration_sec": probe_duration_seconds(UPLOADS_DIR / record.stored_filename)
            if (UPLOADS_DIR / record.stored_filename).is_file() else None,
            "download_url": url_for("download_session_recording", session_id=capture.id),
        }
        if isinstance(summarized, dict):
            summarized = {**summarized, "_recording": recording_ref}
        else:
            summarized = {"_recording": recording_ref}
        misc = (
            MiscData.query.filter_by(capture_session_id=capture.id).first()
            or MiscData(capture_session_id=capture.id)
        )
        misc.raw_transcript = raw_transcript
        misc.summarized_data = summarized
        db.session.add(misc)

        capture.status = "completed" if pdf_name else "partial"
        capture.completed_at = datetime.now(timezone.utc)
        if pdf_name:
            capture.pdf_filename = pdf_name
        db.session.add(capture)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("persist_session_results failed: %s", exc)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


# -----------------------------------------------------------------------------
# Job Info Summary (LLM-written client handoff note, one per capture session)
# -----------------------------------------------------------------------------


def _generate_and_store_job_info_summary(
    session_id: int, *, api_key: str | None, force: bool = False
) -> JobInfoSummary | None:
    """
    Idempotently create/refresh the Job Info Summary for a session (runs in app context).

    Combines ``SessionSummary.structured_data`` + ``MiscData`` and asks OpenAI to write a
    clean handoff note. Never raises — returns the row on success, ``None`` on failure so the
    UI can offer a retry. Safe to call in a background thread.
    """
    from utils.job_info_summary import generate_summary_text

    try:
        capture = db.session.get(CaptureSession, session_id)
        if not capture:
            logger.warning("job_info: session %s not found", session_id)
            return None

        existing = JobInfoSummary.query.filter_by(capture_session_id=session_id).first()
        if existing and existing.summary_text and not force:
            return existing  # idempotent — don't regenerate unless forced

        if not api_key:
            logger.warning("job_info: no OpenAI key available for session %s", session_id)
            return None

        summary_row = SessionSummary.query.filter_by(capture_session_id=session_id).first()
        misc_row = MiscData.query.filter_by(capture_session_id=session_id).first()
        client = db.session.get(Client, capture.client_id) if capture.client_id else None

        structured = summary_row.structured_data if summary_row else {}
        misc_summarized = misc_row.summarized_data if misc_row else {}
        misc_raw = (misc_row.raw_transcript if misc_row else "") or ""

        # Nothing meaningful captured — skip quietly (avoids an empty, low-value summary).
        if not structured and not misc_summarized and not misc_raw.strip():
            logger.info("job_info: no captured data for session %s — skipping", session_id)
            return None

        client_meta = client.to_dict() if client else {}
        session_meta = capture.to_dict()

        from utils.job_info_summary import _MODEL

        text = generate_summary_text(
            session_meta=session_meta,
            client_meta=client_meta,
            structured_data=structured,
            misc_raw_transcript=misc_raw,
            misc_summarized=misc_summarized,
            api_key=api_key,
        )

        row = existing or JobInfoSummary(capture_session_id=session_id)
        row.summary_text = text
        row.model_name = _MODEL
        # Invalidate any stale cached PDF when the text changes.
        row.pdf_filename = None
        db.session.add(row)
        db.session.commit()
        log_job_step(None, "job_info", f"summary stored for session={session_id} chars={len(text)}")
        return row
    except Exception as exc:  # noqa: BLE001 — background/best-effort; never break callers
        logger.warning("job_info: generation failed for session %s: %s", session_id, exc)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _trigger_job_info_summary_async(session_id: int | None) -> None:
    """Fire-and-forget Job Info Summary generation after a report completes (non-breaking).

    The effective OpenAI key is captured **now** (in request context) and passed to the
    worker thread, so generation does not depend on request-scoped ``os.environ`` (which is
    restored at request teardown).
    """
    if not session_id:
        return
    from utils.job_info_summary import resolve_openai_key

    try:
        api_key = resolve_openai_key(session_token())
    except Exception:  # noqa: BLE001
        api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return

    def _worker() -> None:
        with app.app_context():
            _generate_and_store_job_info_summary(session_id, api_key=api_key, force=False)

    threading.Thread(target=_worker, name=f"job-info-{session_id}", daemon=True).start()


@app.route("/generate_report", methods=["POST"])
@rate_limited("generate")
def generate_report():
    """
    Run pipeline stages (v1/v2) → optional review → PDF.

    ``pipeline_stage`` form field:
      - ``analysis`` — transcribe + crew only; saves draft for review (v2 recommended).
      - ``pdf`` — build PDF from saved draft + ``review_notes`` JSON.
      - ``full`` (default) — analysis + PDF in one request (always returns a usable report).
    """
    selected = request.form.getlist("report_types")
    saved_filename = request.form.get("saved_filename")
    video_id_raw = request.form.get("video_id")
    pipeline_stage = (request.form.get("pipeline_stage") or "full").strip().lower()
    review_notes_json = request.form.get("review_notes") or None

    if not video_id_raw:
        return jsonify({"ok": False, "error": "video_id is required"}), 400

    try:
        video_id = int(video_id_raw)
    except ValueError:
        return jsonify({"ok": False, "error": "video_id must be an integer"}), 400

    record = VideoUpload.query.get(video_id)
    if not record:
        return jsonify({"ok": False, "error": "upload not found"}), 404

    if saved_filename and saved_filename != record.stored_filename:
        return jsonify({"ok": False, "error": "saved_filename does not match stored upload"}), 400

    if not selected:
        selected = json.loads(record.report_types_json)

    video_path = UPLOADS_DIR / record.stored_filename
    if not video_path.is_file():
        save_job_meta(record, {"status": "failed", "detail": f"Missing file: {video_path.name}"})
        return jsonify({"ok": False, "error": "video file missing on disk"}), 400

    log_job_step(record.id, "generate_report", f"stage={pipeline_stage}")

    # Explicit mark events captured during recording (exact timestamps + notes).
    existing_meta = parse_job_meta(record)
    mark_events = list(existing_meta.get("mark_events") or [])
    # Step boundaries from the single continuous recording (guided-step alignment signal).
    step_events = list(existing_meta.get("step_events") or [])
    log_job_step(
        record.id,
        "generate_report",
        f"marks={len(mark_events)} steps={len(step_events)} reports={selected}",
    )

    pipeline = get_jobdoc_pipeline()
    templates_json = report_templates_json_for_types(selected)
    steps_state = default_pipeline_steps(pipeline)
    first_step = "transcription" if pipeline == PIPELINE_V2 else "audio"

    def progress(step: str, detail: str) -> None:
        nonlocal steps_state
        steps_state = sync_steps_from_progress(steps_state, step, detail, pipeline=pipeline)
        save_job_meta(
            record,
            {
                "status": "running",
                "progress_step": step,
                "detail": detail,
                "pipeline_steps": steps_state,
                "friendly_error": None,
                "pipeline": pipeline,
            },
        )

    save_job_meta(
        record,
        {
            "status": "running",
            "progress_step": first_step,
            "detail": "Preparing audio for speech-to-text…",
            "pipeline_steps": steps_state,
            "friendly_error": None,
            "pipeline": pipeline,
        },
    )

    final_model: FinalReportOutput | None = None
    pipeline_warnings: list[str] = []
    payload: dict[str, Any] = {}
    review_items: list[dict[str, Any]] = []
    low_confidence_messages: list[str] = []
    degraded = False

    run_crew = pipeline_stage in ("analysis", "full")
    run_pdf = pipeline_stage in ("pdf", "full")

    # Apply the caller's API keys ONLY for the credentialed (crew) phase, scoped to this
    # request. We serialize on a worker-wide lock and snapshot/restore os.environ (via
    # teardown_request) so concurrent or later requests in the same worker never inherit
    # another browser session's pasted keys. The PDF-only stage needs no API keys.
    if run_crew:
        if not _GENERATION_LOCK.acquire(timeout=600):
            return jsonify({
                "ok": False,
                "error": "The server is busy generating another report. Please try again in a moment.",
            }), 503
        g._gen_lock_held = True
        g._key_env_snapshot = credentials.snapshot_env()
        credentials.apply_session_keys_to_env(session_token())
        # Apply the user's per-session transcription backend choice (request-scoped).
        _stt_pref = (session.get("stt_backend") or "").strip().lower()
        if _stt_pref in STT_BACKENDS:
            g._stt_backend_snapshot = os.environ.get("JOBDOC_STT_BACKEND")
            os.environ["JOBDOC_STT_BACKEND"] = _stt_pref

        # Diagnostics: confirm (without leaking values) which keys are available for this
        # request and how much recorder evidence arrived. Makes "empty report" causes obvious.
        _ks = credentials.effective_key_status(session_token())
        log_job_step(
            record.id, first_step,
            "keys: DEEPGRAM_API_KEY=%s(%s) OPENAI_API_KEY=%s(%s) | stt=%s | marks=%d step_events=%d" % (
                _ks["DEEPGRAM_API_KEY"]["present"], _ks["DEEPGRAM_API_KEY"]["source"],
                _ks["OPENAI_API_KEY"]["present"], _ks["OPENAI_API_KEY"]["source"],
                _effective_stt_backend(), len(mark_events or []), len(step_events or []),
            ),
        )

    if run_crew:
        try:
            require_asr_credentials()
            if pipeline == PIPELINE_V2:
                require_crew_llm_credentials()
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        # Warm up + validate audio locally, but DO NOT fail the report if local extraction
        # can't produce a WAV. Imperfect mobile MP4s (broken moov atom, bad timestamps) may
        # trip local ffmpeg while the cloud STT provider — which receives the video container
        # directly — still transcribes them fine. We degrade gracefully here and let the
        # transcription step surface any genuine "no speech" problem with a friendly message.
        try:
            audio_ready = try_extract_audio(video_path) is not None
        except Exception as exc:  # noqa: BLE001 — never let the warm-up abort the report
            audio_ready = False
            log_job_step(record.id, first_step, f"audio warm-up error (continuing): {exc}")
        if not audio_ready:
            log_job_step(
                record.id, first_step,
                "local audio extraction unavailable — continuing via cloud transcription",
                level=logging.WARNING,
            )
        if pipeline == PIPELINE_V2:
            progress("transcription", "Preparing audio — transcribing…")
        else:
            progress("audio", "Preparing audio — starting intelligence pipeline…")

        if is_v2_pipeline():
            (
                final_model,
                payload,
                pipeline_warnings,
                review_items,
                low_confidence_messages,
                degraded,
            ) = _run_v2_crew_phase(
                record,
                video_path,
                templates_json=templates_json,
                steps_state=steps_state,
                pipeline=pipeline,
                progress=progress,
                review_notes_json=review_notes_json if pipeline_stage == "full" else None,
                mark_events=mark_events,
                step_events=step_events,
            )
        else:
            from crew.flask_pipeline import run_v1_pipeline

            outcome = run_v1_pipeline(
                video_path,
                selected=selected,
                templates_json=templates_json,
                video_id=record.id,
                on_progress=progress,
                mark_events=mark_events,
                step_events=step_events,
            )
            final_model = outcome.final_report
            pipeline_warnings = outcome.pipeline_warnings
            payload = outcome.payload
            review_items = outcome.review_items
            low_confidence_messages = outcome.low_confidence_messages
            degraded = outcome.degraded

        mark_moments = build_mark_moments_from_payload(payload)
        if not mark_moments and mark_events:
            mark_moments = _mark_moments_from_events(mark_events)
        _warn_if_uploaded_marks_unused(record.id, mark_events, payload, final_model)
        save_job_meta(
            record,
            {
                "draft_crew_result": payload,
                "crew_result": payload,
                "review_items": review_items,
                "low_confidence_messages": low_confidence_messages,
                "pipeline_notes": pipeline_warnings,
                "mark_moments": mark_moments,
                "degraded": degraded,
            },
        )

        if pipeline_stage == "analysis":
            steps_state = sync_steps_from_progress(
                steps_state,
                "qa",
                "Draft ready — add notes and generate PDF.",
                pipeline=pipeline,
            )
            for row in steps_state:
                if row["id"] != "pdf":
                    row["status"] = "completed"
            save_job_meta(
                record,
                {
                    "status": "awaiting_review",
                    "progress_step": "review",
                    "detail": "Review your sections, add quick notes, then generate the PDF.",
                    "pipeline_steps": steps_state,
                    "friendly_error": None,
                },
            )
            _cleanup_intermediate_media(video_path)
            return jsonify(
                {
                    "ok": True,
                    "video_id": record.id,
                    "status": "awaiting_review",
                    "pipeline": pipeline,
                    "crew": payload,
                    "pipeline_notes": pipeline_warnings,
                    "review_items": review_items,
                    "low_confidence_messages": low_confidence_messages,
                    "mark_moments": mark_moments,
                    "degraded": degraded,
                }
            )

    if run_pdf:
        if final_model is None:
            meta = parse_job_meta(record)
            draft = meta.get("draft_crew_result") or meta.get("crew_result")
            if draft and isinstance(draft, dict):
                payload = draft
                final_model = FinalReportOutput.model_validate(draft.get("final_report") or {})
                pipeline_warnings = list(meta.get("pipeline_notes") or [])
                review_items = list(meta.get("review_items") or [])
                low_confidence_messages = list(meta.get("low_confidence_messages") or [])
                degraded = bool(meta.get("degraded"))
            else:
                return jsonify(
                    {
                        "ok": False,
                        "error": "No draft report found — run Analyze recording first.",
                        "video_id": record.id,
                    }
                ), 400

            if review_notes_json:
                final_model = merge_review_notes(final_model, review_notes_json)
                if payload.get("final_report"):
                    payload = dict(payload)
                    payload["final_report"] = final_model.model_dump()

        pdf_name: str | None = None
        download_url: str | None = None
        gallery: list[dict[str, str]] = []
        pdf_error: str | None = None

        if not WEASYPRINT_STATUS.ready:
            _refresh_weasyprint_status(auto_install=False)
        if not WEASYPRINT_STATUS.ready:
            setup_url = url_for("setup_weasyprint_page", _external=False)
            fix_url = url_for("setup_weasyprint_required", _external=False)
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "PDF export is not available — GTK3 libraries are missing. "
                        f"Open {fix_url} or {setup_url} to fix dependencies."
                    ),
                    "setup_url": setup_url,
                    "fix_url": fix_url,
                    "weasyprint": WEASYPRINT_STATUS.to_dict(),
                    "video_id": record.id,
                }
            ), 503

        try:
            pdf_name, download_url, gallery, pipeline_warnings = _build_pdf_for_job(
                record,
                final_model,
                video_path,
                selected,
                steps_state,
                pipeline,
                pipeline_warnings,
                progress=progress,
                fast=(request.form.get("pdf_mode") or "").strip().lower() in ("light", "fast"),
            )
        except Exception as exc:  # noqa: BLE001
            pdf_error = friendly_error_message(exc)
            log_job_step(record.id, "pdf", f"partial_fail: {pdf_error}")
            pipeline_warnings.append(pdf_error)

        mark_moments = build_mark_moments_from_payload(payload)
        if not mark_moments and mark_events:
            mark_moments = _mark_moments_from_events(mark_events)
        status = "completed" if pdf_name else "partial"
        detail_done = "PDF ready." if pdf_name else "Report data saved — PDF export failed; retry Generate PDF."
        if pipeline_warnings:
            detail_done += " " + " ".join(pipeline_warnings[:2])

        if pdf_name:
            steps_state = mark_all_completed(steps_state, "All steps finished.")
        else:
            steps_state = sync_steps_from_progress(
                steps_state,
                "pdf",
                pdf_error or "PDF failed",
                failed=True,
                pipeline=pipeline,
            )

        save_job_meta(
            record,
            {
                "status": status,
                "progress_step": "completed" if pdf_name else "partial",
                "detail": detail_done,
                "pipeline_steps": steps_state,
                "crew_result": payload,
                "pdf_filename": pdf_name,
                "pdf_download_url": download_url,
                "frame_gallery": gallery,
                "friendly_error": pdf_error,
                "pipeline_notes": pipeline_warnings,
                "mark_moments": mark_moments,
                "review_items": review_items,
                "low_confidence_messages": low_confidence_messages,
                "degraded": degraded,
                "pipeline": pipeline,
            },
        )

        if final_model is not None:
            _persist_session_results(record, payload, final_model, pdf_name)
            # Fire-and-forget: write the LLM Job Info Summary after the report is persisted.
            # Runs even when the PDF failed (pdf_name is None) since the data model is saved.
            _trigger_job_info_summary_async(getattr(record, "capture_session_id", None))

        _cleanup_intermediate_media(video_path)
        return jsonify(
            {
                "ok": True,
                "video_id": record.id,
                "status": status,
                "pipeline": pipeline,
                "selected_report_types": selected,
                "saved_filename": record.stored_filename,
                "crew": payload,
                "pdf_filename": pdf_name,
                "pdf_download_url": download_url,
                "frame_gallery": gallery,
                "pipeline_notes": pipeline_warnings,
                "mark_moments": mark_moments,
                "review_items": review_items,
                "low_confidence_messages": low_confidence_messages,
                "degraded": degraded,
                "pdf_error": pdf_error,
            }
        )

    return jsonify({"ok": False, "error": "Invalid pipeline_stage", "video_id": record.id}), 400


def _session_recording_upload(session_id: int) -> VideoUpload | None:
    """Most recent uploaded recording linked to a capture session whose file still exists."""
    rows = (
        VideoUpload.query
        .filter_by(capture_session_id=session_id)
        .order_by(VideoUpload.id.desc())
        .all()
    )
    for row in rows:
        if row.stored_filename and (UPLOADS_DIR / row.stored_filename).is_file():
            return row
    return rows[0] if rows else None


@app.route("/session/<int:session_id>/recording")
def download_session_recording(session_id: int):
    """Download the full original recording (audio + video) for a capture session.

    Used for auditing and future invoice/proposal generation — the entire source media is
    retained (protected from the disk reaper) and served here as an attachment.
    """
    CaptureSession.query.get_or_404(session_id)
    upload = _session_recording_upload(session_id)
    if not upload or not upload.stored_filename or not (UPLOADS_DIR / upload.stored_filename).is_file():
        flash("The recording for this session is no longer available on disk.")
        return redirect(url_for("job_info_summary_view", session_id=session_id))
    download_name = upload.original_filename or upload.stored_filename
    return send_from_directory(
        UPLOADS_DIR, upload.stored_filename, as_attachment=True, download_name=download_name
    )


@app.route("/session/<int:session_id>/misc.json")
def session_misc_data(session_id: int):
    """Return the complete Misc/supplemental data for a session (raw transcript + summarized).

    Includes a link to the full recording so a downstream invoice/proposal tool can pull
    everything captured for the job in one call.
    """
    capture = CaptureSession.query.get_or_404(session_id)
    misc = MiscData.query.filter_by(capture_session_id=session_id).first()
    upload = _session_recording_upload(session_id)
    recording = None
    if upload:
        file_ok = bool(upload.stored_filename) and (UPLOADS_DIR / upload.stored_filename).is_file()
        recording = {
            "video_id": upload.id,
            "stored_filename": upload.stored_filename,
            "original_filename": upload.original_filename,
            "available": file_ok,
            "download_url": url_for("download_session_recording", session_id=session_id),
            "duration_sec": probe_duration_seconds(UPLOADS_DIR / upload.stored_filename)
            if file_ok else None,
        }
    return jsonify(
        {
            "session_id": capture.id,
            "status": capture.status,
            "job_address": capture.job_address,
            "misc": {
                "raw_transcript": (misc.raw_transcript if misc else "") or "",
                "summarized_data": misc.summarized_data if misc else {},
                "created_at": misc.created_at.isoformat() if (misc and misc.created_at) else None,
            },
            "recording": recording,
        }
    )


@app.route("/download_report/<int:video_id>")
def download_report(video_id: int):
    """Serve the generated PDF for a completed job."""
    record = VideoUpload.query.get_or_404(video_id)
    meta = parse_job_meta(record)
    fn = meta.get("pdf_filename")
    if not fn:
        flash("PDF not available yet.")
        return redirect(url_for("report_preview_detail", video_id=video_id))
    target = OUTPUT_DIR / fn
    if not target.is_file():
        flash("PDF missing on disk.")
        return redirect(url_for("report_preview_detail", video_id=video_id))
    return send_from_directory(
        OUTPUT_DIR,
        fn,
        as_attachment=True,
        download_name=fn,
    )


# -----------------------------------------------------------------------------
# Job Info Summary — view, (re)generate, and download as PDF
# -----------------------------------------------------------------------------


def _job_info_pdf_path(session_id: int) -> Path:
    return OUTPUT_DIR / f"job_info_summary_{session_id}.pdf"


def _render_job_info_pdf(summary: JobInfoSummary, capture: CaptureSession) -> str | None:
    """Render (and cache) the Job Info Summary PDF; returns the filename or None on failure."""
    from utils.job_info_summary import render_summary_pdf

    client = db.session.get(Client, capture.client_id) if capture.client_id else None
    title = "Job Info Summary"
    subtitle_bits = [client.display_label if client else "Client"]
    if capture.job_address:
        subtitle_bits.append(capture.job_address)
    subtitle = " · ".join(b for b in subtitle_bits if b)
    generated_on = (summary.updated_at or summary.created_at or datetime.now(timezone.utc)).strftime(
        "%B %d, %Y"
    )
    out_path = _job_info_pdf_path(capture.id)
    try:
        render_summary_pdf(
            summary.summary_text,
            title=title,
            subtitle=subtitle,
            generated_on=generated_on,
            output_path=out_path,
            base_dir=BASE_DIR,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("job_info: PDF render failed for session %s: %s", capture.id, exc)
        return None
    if not out_path.is_file() or out_path.stat().st_size == 0:
        return None
    summary.pdf_filename = out_path.name
    db.session.add(summary)
    db.session.commit()
    return out_path.name


@app.route("/session/<int:session_id>/summary")
def job_info_summary_view(session_id: int):
    """View the Job Info Summary for a session (formatted text + copy + PDF download)."""
    capture = CaptureSession.query.get_or_404(session_id)
    client = db.session.get(Client, capture.client_id) if capture.client_id else None
    summary = JobInfoSummary.query.filter_by(capture_session_id=session_id).first()
    return render_template(
        "job_info_summary.html",
        capture=capture,
        client=client,
        summary=summary,
    )


@app.route("/session/<int:session_id>/summary/generate", methods=["POST"])
def job_info_summary_generate(session_id: int):
    """Generate or regenerate the Job Info Summary on demand (synchronous, with feedback)."""
    from utils.job_info_summary import resolve_openai_key

    capture = CaptureSession.query.get_or_404(session_id)
    force = (request.form.get("force") or "").strip() in {"1", "true", "yes", "on"}
    api_key = resolve_openai_key(session_token())
    if not api_key:
        flash("An OpenAI API key is required to generate the summary. Add one in Setup.")
        return redirect(url_for("job_info_summary_view", session_id=session_id))

    row = _generate_and_store_job_info_summary(session_id, api_key=api_key, force=force)
    if row is None:
        flash("Could not generate the Job Info Summary. Please try again.")
    else:
        flash("Job Info Summary is ready.")
    # Refresh the cached PDF on regenerate so a stale copy is not served.
    if row is not None:
        _render_job_info_pdf(row, capture)
    return redirect(url_for("job_info_summary_view", session_id=session_id))


@app.route("/session/<int:session_id>/summary.pdf")
def job_info_summary_pdf(session_id: int):
    """Download the Job Info Summary as a clean, standalone PDF (rendered on demand)."""
    capture = CaptureSession.query.get_or_404(session_id)
    summary = JobInfoSummary.query.filter_by(capture_session_id=session_id).first()
    if not summary or not summary.summary_text:
        flash("No Job Info Summary exists yet. Generate it first.")
        return redirect(url_for("job_info_summary_view", session_id=session_id))

    fn = summary.pdf_filename
    if not fn or not (OUTPUT_DIR / fn).is_file():
        fn = _render_job_info_pdf(summary, capture)
    if not fn or not (OUTPUT_DIR / fn).is_file():
        flash("Could not build the summary PDF. Please try again.")
        return redirect(url_for("job_info_summary_view", session_id=session_id))

    return send_from_directory(OUTPUT_DIR, fn, as_attachment=True, download_name=fn)


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------




def _protected_recording_filenames() -> set[str]:
    """Filenames of recordings linked to a capture session — kept forever for audit/invoicing."""
    try:
        with app.app_context():
            rows = (
                VideoUpload.query
                .filter(VideoUpload.capture_session_id.isnot(None))
                .with_entities(VideoUpload.stored_filename)
                .all()
            )
            return {r[0] for r in rows if r and r[0]}
    except Exception as exc:  # noqa: BLE001 — protection is best-effort, never crash the reaper
        logger.debug("protected recording lookup failed: %s", exc)
        return set()


def _init_app() -> None:
    """
    One-time startup: create DB schema and log the runtime acceleration mode.

    Runs at import so the app initializes correctly under gunicorn (HF Spaces) as well as
    ``python app.py``. GPU detection here is runtime-only (inside the running container);
    it never executes during ``docker build``.
    """
    with app.app_context():
        db.create_all()
        _ensure_sqlite_schema()
    # Log "GPU acceleration active (CUDA …)" vs "CPU mode …" exactly once.
    gpu_utils.log_gpu_status()

    # Start the age-based disk reaper (no-op if JOBDOC_RETENTION_HOURS=0). Recordings linked to
    # a saved capture session are protected from pruning so the full audio/video remains
    # downloadable for later audit / invoice / proposal generation.
    maintenance.start_reaper(
        [UPLOADS_DIR, OUTPUT_DIR],
        protected_names_provider=_protected_recording_filenames,
    )

    # Single, clear production-readiness banner for HF Spaces logs.
    accel = "GPU" if gpu_utils.is_gpu_available() else "CPU"
    has_space_keys = bool(os.environ.get("DEEPGRAM_API_KEY")) and bool(os.environ.get("OPENAI_API_KEY"))
    key_mode = "space secrets" if has_space_keys else "per-user (UI)"
    pipeline = get_jobdoc_pipeline()
    logger.info(
        "PRODUCTION READY",
        extra={
            "event": "startup",
            "media": accel,
            "keys": key_mode,
            "auth": "enabled" if _auth_enabled() else "disabled",
            "weasyprint": "ok" if WEASYPRINT_STATUS.ready else "unavailable",
            "retention_hours": maintenance.retention_hours(),
            "pipeline": pipeline,
        },
    )
    if pipeline == PIPELINE_V1:
        logger.warning(
            "JOBDOC_PIPELINE=v1 is forced — prefer v2. MARK taps are forwarded into v1 "
            "matching; spoken-cue-only matching is no longer the sole path."
        )


_init_app()


if __name__ == "__main__":
    # Local HTTP bind: drop the Secure cookie flag so login sticks on http://127.0.0.1.
    # gunicorn / HF import path never hits this branch — production cookies stay Secure.
    if os.environ.get("JOBDOC_COOKIE_INSECURE", "").strip() == "":
        app.config["SESSION_COOKIE_SECURE"] = False
    # Local dev / direct run. HF Spaces uses gunicorn (see Dockerfile CMD).
    # Default to 7860 to match the Hugging Face Spaces convention.
    port = int(os.environ.get("PORT", "7860"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
