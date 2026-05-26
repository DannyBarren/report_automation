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
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

from utils.job_progress import default_pipeline_steps, mark_all_completed, sync_steps_from_progress
from utils.pipeline_config import PIPELINE_V2, get_jobdoc_pipeline, is_v2_pipeline
from utils.pipeline_errors import PipelineError, friendly_error_message
from utils.prompt_utils import load_prompt
from utils.pipeline_logging import log_job_step
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
from utils.video_utils import extract_audio
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

db = SQLAlchemy(app)


@app.context_processor
def _inject_weasyprint_status() -> dict[str, Any]:
    return {"weasyprint_ready": WEASYPRINT_STATUS.ready}


def _refresh_weasyprint_status(*, auto_install: bool = False) -> None:
    global WEASYPRINT_STATUS
    reset_weasyprint_probe_cache()
    WEASYPRINT_STATUS = check_weasyprint_status(BASE_DIR, auto_install=auto_install)


class VideoUpload(db.Model):
    """
    Row per recorded upload. ``job_meta`` stores JSON for status polling + crew summary.
    """

    __tablename__ = "video_uploads"

    id = db.Column(db.Integer, primary_key=True)
    stored_filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=True)
    report_types_json = db.Column(db.Text, nullable=False)
    # JSON string: { "status", "detail", "crew_result", "updated_at", ... }
    job_meta = db.Column(db.Text, nullable=True)


def _ensure_sqlite_schema() -> None:
    """
    Lightweight migration for dev SQLite DBs created before new columns existed.

    Production should use Alembic; for the MVP we only add ``job_meta`` if missing.
    """
    if "sqlite" not in str(app.config.get("SQLALCHEMY_DATABASE_URI", "")):
        return
    try:
        insp = inspect(db.engine)
        cols = {c["name"] for c in insp.get_columns("video_uploads")}
    except Exception:  # noqa: BLE001
        return
    if "job_meta" in cols:
        return
    try:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE video_uploads ADD COLUMN job_meta TEXT"))
    except Exception:  # noqa: BLE001
        # If ALTER fails, operator can delete ``instance/jobdoc.db`` to recreate.
        pass


# -----------------------------------------------------------------------------
# Report template loading (single source of truth: JSON on disk)
# -----------------------------------------------------------------------------


def load_report_templates() -> list[ReportTemplate]:
    """Read every ``*.json`` in ``reports/`` (rich schema v2 + legacy)."""
    from utils.template_loader import load_all_report_templates

    return load_all_report_templates(REPORTS_DIR)


def templates_by_type() -> dict[str, ReportTemplate]:
    return {t.report_type: t for t in load_report_templates()}


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


# Canonical pair for one-click client demos (must exist under ``reports/*.json``).
QUICK_DEMO_TYPES_ORDER = ("people_report", "room_report")


def quick_demo_report_types() -> list[str]:
    """Return ``people_report`` + ``room_report`` when both templates are on disk."""
    by_type = templates_by_type()
    return [rt for rt in QUICK_DEMO_TYPES_ORDER if rt in by_type]


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


def _env_token_present(name: str) -> bool:
    """True if the env var exists and is non-empty (never exposed to clients)."""
    return bool(os.environ.get(name, "").strip())


def _recording_page_response(ordered: list[str]):
    """Shared by ``/start_recording`` (POST) and ``/demo`` (GET) — one recording UI."""
    session["selected_report_types"] = ordered

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


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


@app.route("/")
def index():
    reports = load_report_templates()
    return render_template("index.html", reports=reports)


@app.route("/test_generate")
def test_generate():
    """
    Shortcut for testing the pipeline with an existing video on disk.

    Usage:
        - ``/test_generate``: auto-pick a video from ``uploads/`` (prefers .mp4, then .mov)
        - ``/test_generate?filename=somefile.MOV``: pick a specific file from ``uploads/``
          (basename only; path traversal is rejected)

    This route creates a ``VideoUpload`` row pointing at that file, chooses default report types,
    and redirects to the standard preview workflow.
    """
    # Optional override (support both ?filename= and legacy ?video=).
    requested = str(request.args.get("filename") or request.args.get("video") or "").strip()
    requested = Path(requested).name if requested else ""

    def _pick_default_upload() -> Path | None:
        if not UPLOADS_DIR.is_dir():
            return None
        # Prefer MP4, then MOV (common iPhone default). Fall back to other known containers.
        patterns = ("*.mp4", "*.MP4", "*.mov", "*.MOV", "*.webm", "*.WEBM", "*.mkv", "*.MKV")
        for pat in patterns:
            hits = sorted(UPLOADS_DIR.glob(pat), key=lambda p: p.stat().st_mtime, reverse=True)
            if hits:
                return hits[0]
        return None

    if requested:
        test_path = UPLOADS_DIR / requested
    else:
        # Env override remains supported, but we no longer require a fixed filename.
        env_name = str(os.environ.get("JOBDOC_TEST_VIDEO", "test_video.MOV")).strip()
        test_path = (UPLOADS_DIR / Path(env_name).name) if env_name else None
        if not (test_path and test_path.is_file()):
            test_path = _pick_default_upload()

    if not test_path or not test_path.is_file():
        flash(
            "No test video found under uploads/. Add a file like `uploads/test.mov` or `uploads/test.mp4`, "
            "then click “Test with existing video”. You can also call /test_generate?filename=YOURFILE.MOV."
        )
        return redirect(url_for("index"))

    from utils.video_utils import normalize_video_to_mp4

    original_name = test_path.name
    normalized_path = normalize_video_to_mp4(test_path)
    stored_name = normalized_path.name

    by_type = templates_by_type()
    selected = quick_demo_report_types() or sorted(by_type.keys())
    if not selected:
        flash("No report templates available. Add JSON files under reports/ before running a test.")
        return redirect(url_for("index"))

    # Reuse an existing row for the same stored filename when possible to avoid clutter.
    existing = VideoUpload.query.filter_by(stored_filename=stored_name).order_by(VideoUpload.id.desc()).first()
    if existing:
        video_id = existing.id
    else:
        record = VideoUpload(
            stored_filename=stored_name,
            original_filename=original_name,
            report_types_json=json.dumps(selected),
            job_meta=json.dumps(_default_job_meta(), ensure_ascii=False),
        )
        db.session.add(record)
        db.session.commit()
        video_id = record.id

    return redirect(url_for("report_preview_detail", video_id=video_id))


@app.route("/demo")
def demo_quick_start():
    """
    Skip template selection: load **People + Room** reports and go straight to recording.

    Intended for live rooms — one tap from home to camera UI.
    """
    ordered = quick_demo_report_types()
    if len(ordered) < 2:
        flash("Quick demo needs both `people_report.json` and `room_report.json` under reports/.")
        return redirect(url_for("index"))
    return _recording_page_response(ordered)


@app.route("/health")
def health():
    """
    Readiness probe: API keys + WeasyPrint/GTK (never returns secret values).
    """
    wp = check_weasyprint_status(BASE_DIR, auto_install=False)
    keys_ok = _env_token_present("DEEPGRAM_API_KEY") and _env_token_present("ANTHROPIC_API_KEY")
    pdf_ok = wp.ready
    return jsonify(
        {
            "ok": keys_ok and pdf_ok,
            "service": "GenerSwift",
            "pipeline": get_jobdoc_pipeline(),
            "keys": {
                "DEEPGRAM_API_KEY": _env_token_present("DEEPGRAM_API_KEY"),
                "ANTHROPIC_API_KEY": _env_token_present("ANTHROPIC_API_KEY"),
            },
            "weasyprint": wp.to_dict(),
            "ready_for_full_demo": keys_ok and pdf_ok,
            "messages": {
                "pdf": wp.message if pdf_ok else (wp.install_hint or wp.message),
                "keys": (
                    "API keys configured."
                    if keys_ok
                    else "Set DEEPGRAM_API_KEY and ANTHROPIC_API_KEY in .env"
                ),
            },
        }
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

    return _recording_page_response(ordered)


@app.route("/upload_video", methods=["POST"])
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
    file.save(out_path)

    # iPhone compatibility: Safari/iOS commonly uploads `.mov`. Normalize to `.mp4` immediately so
    # the rest of the pipeline (audio extraction, frame grabs, transcription) has a consistent container.
    if out_path.suffix.lower() == ".mov":
        from utils.video_utils import normalize_video_to_mp4

        mp4_path = normalize_video_to_mp4(out_path)
        if mp4_path != out_path:
            try:
                out_path.unlink(missing_ok=True)  # type: ignore[call-arg]
            except TypeError:
                if out_path.exists():
                    out_path.unlink()
            out_path = mp4_path
            stored = out_path.name

    session["selected_report_types"] = selected
    session["last_upload_filename"] = stored

    record = VideoUpload(
        stored_filename=stored,
        original_filename=original,
        report_types_json=json.dumps(selected),
        job_meta=json.dumps(_default_job_meta(), ensure_ascii=False),
    )
    db.session.add(record)
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
    return render_template(
        "report_preview.html",
        saved_filename=record.stored_filename,
        selected_report_types=selected,
        selected_titles=selected_titles,
        video_id=record.id,
        job_status=meta,
        frame_gallery=gallery,
        mark_moments=mark_moments,
        pipeline_version=get_jobdoc_pipeline(),
        review_items=meta.get("review_items") or [],
        low_confidence_messages=meta.get("low_confidence_messages") or [],
        is_v2=is_v2_pipeline(),
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


def _run_v2_crew_phase(
    record: VideoUpload,
    video_path: Path,
    *,
    templates_json: str,
    steps_state: list[dict[str, Any]],
    pipeline: str,
    progress: Any,
    review_notes_json: str | None = None,
) -> tuple[FinalReportOutput, dict[str, Any], list[str], list[dict[str, Any]], list[str], bool]:
    from crew.flask_pipeline import run_v2_pipeline

    outcome = run_v2_pipeline(
        video_path,
        templates_json=templates_json,
        on_progress=progress,
        video_id=record.id,
        review_notes_json=review_notes_json,
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
) -> tuple[str, str, list[dict[str, str]], list[str]]:
    """Frames → HTML → PDF. Returns (pdf_name, download_url, gallery, extra_warnings)."""
    extra_warnings = list(pipeline_warnings)
    progress("pdf", "Generating PDF — extracting still frames…")
    frames_dir = OUTPUT_DIR / "frames" / str(record.id)
    frame_result = enrich_report_with_still_frames_resilient(final_model, video_path, frames_dir)
    enriched = frame_result.final
    extra_warnings.extend(frame_result.warnings)

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


@app.route("/generate_report", methods=["POST"])
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

    if run_crew:
        try:
            require_asr_credentials()
            if pipeline == PIPELINE_V2:
                require_crew_llm_credentials()
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        try:
            extract_audio(video_path)
            if pipeline == PIPELINE_V2:
                progress("transcription", "Audio ready — transcribing…")
            else:
                progress("audio", "Audio ready — starting intelligence pipeline…")
        except (RuntimeError, OSError, FileNotFoundError) as exc:
            body, code = _fail_job(record, steps_state, first_step, exc, pipeline=pipeline)
            return jsonify(body), code

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
            )
        else:
            from crew.flask_pipeline import run_v1_pipeline

            outcome = run_v1_pipeline(
                video_path,
                selected=selected,
                templates_json=templates_json,
                video_id=record.id,
                on_progress=progress,
            )
            final_model = outcome.final_report
            pipeline_warnings = outcome.pipeline_warnings
            payload = outcome.payload
            review_items = outcome.review_items
            low_confidence_messages = outcome.low_confidence_messages
            degraded = outcome.degraded

        mark_moments = build_mark_moments_from_payload(payload)
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
            )
        except Exception as exc:  # noqa: BLE001
            pdf_error = friendly_error_message(exc)
            log_job_step(record.id, "pdf", f"partial_fail: {pdf_error}")
            pipeline_warnings.append(pdf_error)

        mark_moments = build_mark_moments_from_payload(payload)
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
# Entrypoint
# -----------------------------------------------------------------------------


if __name__ == "__main__":
    import logging

    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("JOBDOC_LOG", "").lower() == "debug" else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    with app.app_context():
        db.create_all()
        _ensure_sqlite_schema()
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG", "1") == "1")
