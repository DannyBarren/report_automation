"""
modal_app.py — Serve the existing GenerSwift / JobDoc Flask app on Modal.com.

What this does
--------------
    * Builds a CUDA-capable image with ffmpeg (NVDEC/NVENC hardware accel), the WeasyPrint
      PDF runtime libraries, and every Python dependency from ``requirements.txt``.
    * Mounts a single persistent ``modal.Volume`` named ``jobdoc-data`` at ``/data`` and
      symlinks the app's three writable directories into it, so ``uploads/``,
      ``reports_output/`` and ``instance/`` (the SQLite DB) all survive restarts and
      redeploys — with ZERO changes to ``app.py``.
    * Injects secrets (Deepgram / OpenAI / SECRET_KEY / auth) via a ``modal.Secret``.
    * Serves the unmodified Flask WSGI app publicly over HTTPS via ``@modal.wsgi_app`` —
      the resulting ``*.modal.run`` URL works directly on phones (secure context =
      camera/microphone recording works).

Deploy:   modal deploy modal_app.py
Dev/test: modal serve  modal_app.py     (hot-reloading ephemeral URL)
"""

from __future__ import annotations

import os
import shutil
import sys
import time

import modal

# Force image rebuild v2 - secret propagation fix
# -----------------------------------------------------------------------------
# App + persistent storage
# -----------------------------------------------------------------------------

app = modal.App("generswift-jobdoc")

# One persistent volume for ALL user data. A single modal.Volume can only be mounted at
# ONE path, so we mount it at /data and symlink uploads/ reports_output/ instance/ into it
# at container start (see _wire_persistent_dirs). This keeps app.py untouched.
data_volume = modal.Volume.from_name("jobdoc-data", create_if_missing=True)

VOLUME_MOUNT = "/data"
APP_ROOT = "/root"
# Directories app.py writes to (all resolved relative to the app.py location = /root).
PERSISTENT_DIRS = ("uploads", "reports_output", "instance")

# Secrets: create once on Modal, e.g.
#   modal secret create jobdoc-secrets \
#       OPENAI_API_KEY=sk-... DEEPGRAM_API_KEY=... SECRET_KEY=...
#
# IMPORTANT: attach the real named Secret directly (no empty-dict fallback). ``from_name`` is
# LAZY — it does not touch the network at import time, so plain ``import modal_app`` still
# works locally. Resolution happens at ``modal deploy``/``modal serve``: if the Secret is
# missing, the deploy now FAILS LOUDLY instead of silently shipping a keyless container
# (which is what made the app produce empty reports with "AI reasoning is unavailable").
# The Secret is attached directly on the web function below via modal.Secret.from_name(...).
SECRET_NAME = "jobdoc-secrets"

# The provider keys we expect the Secret to inject. Used only for startup diagnostics
# (presence/absence is logged — the values are NEVER printed).
_EXPECTED_SECRET_KEYS = ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "SECRET_KEY")
# Alias spellings that still count as "the key is present" (normalized at boot).
_KEY_ALIAS_HINTS = (
    "OPEN_AI_API_KEY", "OPENAI_KEY", "OPENAI_APIKEY", "OPENAI_SECRET_KEY", "OPENAI_TOKEN",
    "DEEPGRAM_KEY", "DEEPGRAM_APIKEY", "DG_API_KEY",
)

# -----------------------------------------------------------------------------
# Container image
# -----------------------------------------------------------------------------
# Mirrors the project Dockerfile: Python 3.11, ffmpeg (hardware decode/encode is used only
# when a GPU + NVIDIA driver are present at runtime — Modal injects the driver on GPU
# containers), and the WeasyPrint/Pango/Cairo stack for PDF export. GPU/CUDA is never probed
# at build time; the app detects it at runtime and degrades to CPU gracefully.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "ffmpeg",
        "libpango-1.0-0",
        "libpangocairo-1.0-0",
        "libcairo2",
        "libgdk-pixbuf-2.0-0",
        "libffi-dev",
        "libjpeg-dev",
        "libpng-dev",
        "shared-mime-info",
        "fonts-dejavu-core",
        "git",
        "curl",
    )
    # requirements.txt already pins the cu121 torch extra index internally; pass it here too
    # so the CUDA build of torch/torchaudio resolves reliably.
    .pip_install_from_requirements(
        "requirements.txt",
        extra_index_url="https://download.pytorch.org/whl/cu121",
    )
    # Ship the application code to /root. Anything writable or secret is excluded — those
    # come from the Volume (data) and the Secret (keys) at runtime instead.
    .add_local_dir(
        ".",
        remote_path=APP_ROOT,
        ignore=[
            ".venv",
            ".venv/**",
            ".git",
            ".git/**",
            "**/__pycache__",
            "**/*.pyc",
            ".env",
            "uploads",
            "uploads/**",
            "reports_output",
            "reports_output/**",
            "instance",
            "instance/**",
            "modal_app.py",
        ],
    )
)


def _start_volume_committer(interval_sec: int = 60) -> None:
    """Periodically persist Volume writes so data survives restarts/redeploys.

    Modal commits a Volume on container exit, but a long-running web server can be killed
    abruptly (scale-down, redeploy, OOM). A lightweight background committer flushes the
    SQLite DB, uploads, and generated PDFs on an interval so at most ~``interval_sec`` of
    work is ever at risk — without touching the untouched Flask app.
    """
    import threading

    def _loop() -> None:
        while True:
            time.sleep(interval_sec)
            try:
                data_volume.commit()
            except Exception as exc:  # noqa: BLE001 — never let committing crash the server
                print(f"[modal] volume commit skipped: {exc}", flush=True)

    threading.Thread(target=_loop, name="volume-committer", daemon=True).start()


def _wire_persistent_dirs() -> None:
    """Point app.py's writable dirs at the mounted Volume via symlinks (app.py stays untouched).

    app.py computes ``UPLOADS_DIR``/``OUTPUT_DIR``/``INSTANCE_DIR`` and the SQLite path from the
    location of ``app.py`` (=/root). We replace ``/root/<dir>`` with a symlink to
    ``/data/<dir>`` BEFORE importing app.py so every read/write lands on the persistent Volume.
    """
    for name in PERSISTENT_DIRS:
        target = os.path.join(VOLUME_MOUNT, name)
        link = os.path.join(APP_ROOT, name)
        os.makedirs(target, exist_ok=True)
        if os.path.islink(link):
            continue
        # If the image happened to ship a real dir here, move its contents into the volume once.
        if os.path.isdir(link):
            for entry in os.listdir(link):
                src = os.path.join(link, entry)
                dst = os.path.join(target, entry)
                if not os.path.exists(dst):
                    shutil.move(src, dst)
            shutil.rmtree(link, ignore_errors=True)
        elif os.path.exists(link):
            os.remove(link)
        os.symlink(target, link)


# -----------------------------------------------------------------------------
# Public WSGI web app
# -----------------------------------------------------------------------------
# GPU is attached to the web container because the report pipeline (ffmpeg audio/frame
# extraction, optional local Whisper) runs synchronously inside the request. Set gpu=None
# to run CPU-only and save cost — the app auto-detects and falls back gracefully.
#   - max_containers=1: single writer for the SQLite DB on the Volume + the app's in-memory
#     rate limiter / per-session key store / generation lock.
#   - @modal.concurrent: one container serves many simultaneous requests (e.g. status
#     polling stays responsive while a report generates) via Flask's threaded WSGI server.
#   - scaledown_window: keep the container warm briefly, then scale to zero when idle.
def _log_startup_key_status() -> None:
    """Log (never print) whether the Secret's provider keys reached the container.

    Prints a compact, value-free report so the Modal logs immediately show whether the
    ``jobdoc-secrets`` Secret was actually mounted and which keys are usable. This is the
    fastest way to confirm a fix without leaking any secret material.
    """
    print(f"[modal] mounting Modal Secret: '{SECRET_NAME}' onto flask_app()", flush=True)

    # Exact expected/alias names present (fast, value-free presence check).
    raw_present = sorted(
        name for name in (*_EXPECTED_SECRET_KEYS, *_KEY_ALIAS_HINTS)
        if os.environ.get(name, "").strip()
    )
    print(f"[modal] secret env names present (known): {raw_present or 'NONE'}", flush=True)

    try:
        from utils.credentials import normalize_provider_env, provider_env_name_report

        # Fuzzy diagnostic: show EVERY env var name that looks OpenAI/Deepgram related, with a
        # value-present flag. This surfaces a mis-named secret key — e.g. a trailing space
        # ("'OPENAI_API_KEY '") or hyphen — that a fixed-name check can never see, which is
        # the usual reason a "correctly recreated" secret still reads as MISSING.
        related = provider_env_name_report()
        print(f"[modal] provider-related env NAMES (repr, has_value): {related or 'NONE'}", flush=True)

        present = normalize_provider_env()  # exact + fuzzy fold → canonical names, idempotent
    except Exception as exc:  # noqa: BLE001 — never block boot on key normalization
        print(f"[modal] key normalization skipped: {exc}", flush=True)
        present = {
            "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
            "DEEPGRAM_API_KEY": bool(os.environ.get("DEEPGRAM_API_KEY", "").strip()),
        }

    openai_ok = present.get("OPENAI_API_KEY", False)
    deepgram_ok = present.get("DEEPGRAM_API_KEY", False)
    secret_ok = bool(os.environ.get("SECRET_KEY", "").strip())
    print(
        "[modal] provider keys after normalize -> "
        f"OPENAI_API_KEY={'OK' if openai_ok else 'MISSING'} "
        f"DEEPGRAM_API_KEY={'OK' if deepgram_ok else 'MISSING'} "
        f"SECRET_KEY={'OK' if secret_ok else 'MISSING'}",
        flush=True,
    )
    if not (openai_ok and deepgram_ok):
        print(
            f"[modal] WARNING: one or more provider keys are MISSING. The '{SECRET_NAME}' "
            "Secret is either not attached or does not contain a usable OPENAI_API_KEY / "
            "DEEPGRAM_API_KEY. Check the 'provider-related env NAMES' line above for a "
            "mis-typed KEY (stray space/hyphen/case), then recreate it with:\n"
            f"  modal secret create {SECRET_NAME} OPENAI_API_KEY=sk-... DEEPGRAM_API_KEY=... "
            "SECRET_KEY=...\n"
            "Reports will be empty (no transcription / no AI reasoning) until this is fixed.",
            flush=True,
        )


@app.function(
    image=image,
    gpu="T4",
    volumes={VOLUME_MOUNT: data_volume},
    # Attach the named Secret DIRECTLY so OPENAI_API_KEY / DEEPGRAM_API_KEY / SECRET_KEY are
    # injected as env vars into every container running this web app. No try/except and no
    # from_dict fallback — a missing Secret must fail the deploy, never ship keyless.
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    max_containers=1,
    scaledown_window=300,
    # Long walkthroughs (10–30+ min) transcribe + run four crew stages + extract many stills
    # inside one synchronous request. Allow up to an hour so a long video is never killed
    # mid-generation (the pipeline's own per-stage timeouts still bound each step).
    timeout=3600,
)
@modal.concurrent(max_inputs=100)
@modal.wsgi_app()
def flask_app():
    # Ensure the app dir is importable and is the working directory.
    if APP_ROOT not in sys.path:
        sys.path.insert(0, APP_ROOT)
    os.chdir(APP_ROOT)

    # Modal serves HTTPS, so keep secure cookies on (default). Do NOT set JOBDOC_COOKIE_INSECURE.
    # Longer recordings are fine on a GPU container — allow up to ~40 min walkthroughs and give
    # transcription enough headroom to pull audio + round-trip a long file to Deepgram.
    os.environ.setdefault("JOBDOC_MAX_VIDEO_SEC", "2400")
    os.environ.setdefault("JOBDOC_TRANSCRIPTION_TIMEOUT_SEC", "900")
    os.environ.setdefault("JOBDOC_CREW_TIMEOUT_SEC", "600")

    # Pull the latest committed Volume state so a freshly started container sees prior data,
    # then wire persistence BEFORE importing app.py (app.py creates the SQLite schema and
    # runtime directories at import time).
    try:
        data_volume.reload()
    except Exception as exc:  # noqa: BLE001 — a slow/unavailable Volume shouldn't block boot
        print(f"[modal] volume reload skipped: {exc}", flush=True)
    _wire_persistent_dirs()

    # Durability: flush Volume writes on an interval (SQLite DB, uploads, generated PDFs).
    _start_volume_committer()

    # Confirm the Secret reached the container and normalize any alias-spelled keys BEFORE
    # importing app. The Modal Secret `jobdoc-secrets` injects keys as env vars, but the
    # OpenAI key is often stored under a non-canonical name (OPEN_AI_API_KEY / OPENAI_KEY);
    # normalization folds recognized aliases into the canonical OPENAI_API_KEY /
    # DEEPGRAM_API_KEY the pipeline reads. The log line makes key presence obvious in `modal logs`.
    _log_startup_key_status()

    from app import app as flask_application  # the existing, unmodified Flask app

    return flask_application
