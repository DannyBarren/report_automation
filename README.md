---
title: GenerSwift - JobDoc Demo v1
emoji: ⚡
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: other
short_description: GenerSwift — record a narrated walkthrough → structured PDF report.
---

# GenerSwift - JobDoc Demo v1

> **GenerSwift** is the branded demo of the JobDoc application — **a Barren Business Development
> Automation Product**, **Built in Partnership and For JobDoc**. It is the same product with a
> dark-mode, neon-accented UI. All original JobDoc functionality (guided recording, Deepgram STT,
> CrewAI report writing, WeasyPrint PDF export, GPU media acceleration) is unchanged.

**Record a narrated walkthrough on your phone → get a professional, structured PDF report.**

JobDoc is a mobile-first tool for tradespeople, inspectors, and realty/compliance pros. On
site, you record a short guided video, say **“Mark this.”** (or tap **MARK THIS SECTION**) at
each finding, and describe what you see. JobDoc turns that into a clean PDF with still frames
and section-by-section narratives.

### How it works (and why it's reliable)

- **Speech-to-text with precise timestamps** — [Deepgram Nova-3](https://deepgram.com/) gives
  accurate word timing and dependable **“Mark this.”** cue detection.
- **Frontier-LLM reasoning** — [OpenAI](https://openai.com/) (via CrewAI)
  reads your narration against rich JSON report templates and writes the report.
- **Zero computer vision.** Findings come from what you *say*, not from image models. This is
  what makes JobDoc practical, predictable, and cost-effective for trades.
- **GPU is used only to accelerate the local media pipeline** — hardware video decode/encode
  and still-frame extraction (ffmpeg NVDEC/NVENC), plus an optional local Whisper STT backend.
  The AI reasoning stays cloud-based.

---

## Deploy on Hugging Face Spaces (with GPU acceleration)

JobDoc ships a single Docker image that runs correctly on **both CPU-only and GPU Spaces**.

1. **Create a Space** → choose **Docker** as the SDK and push this repo (it contains the
   `Dockerfile` and the README front-matter that sets `app_port: 7860`).
2. **Select hardware** → Space **Settings → Hardware** → pick a GPU tier (e.g. **T4**, **A10G**).
   - The app **automatically detects** the GPU at runtime and uses hardware-accelerated ffmpeg
     for video normalization and frame extraction.
   - Confirm it at **`/health`** → look for `"acceleration": { "media_mode": "gpu", ... }` and a
     log line like `GPU acceleration active (CUDA 12.1, Tesla T4)`.
3. **Provide API keys** — either:
   - **Per user:** open **API keys** (header link, or `/settings/keys`) and paste Deepgram +
     OpenAI keys, and optionally choose the transcription provider (Deepgram, or local
     faster-whisper on GPU). Keys are kept server-side for the session and never echoed back, **or**
   - **Space-wide:** set `DEEPGRAM_API_KEY` and `OPENAI_API_KEY` as **Space secrets** (used as a
     fallback when a user hasn't pasted their own).

   **User-pasted keys take precedence** over Space secrets, so each user is billed to their own
   account; Space secrets are the fallback. Per-user keys are stored server-side keyed by a random
   per-browser token, are **never logged or returned in responses**, are applied to the process
   environment **only for the duration of that user's generation request** (scoped via
   `teardown_request` + a generation lock so they can't leak into another session's request), and
   are **cleared on sign-out**. Transcription/LLM provider choices are non-secret session
   preferences. Surfaced via `/health` and `/metrics` without exposing key material.

4. **Set a session secret** — set `SECRET_KEY` as a Space secret (any long random string) so login
   cookies are signed with a stable, private key across restarts.

### Access control (login)

The hosted Space is gated by a simple shared-credential login so only authorized users can run
reports. There is no user database — it's basic access control, not user management.

- **Default credentials:** username `jobdoc` / password `Burger_Time_101_!`
- **Change them** with Space secrets `JOBDOC_AUTH_USER` and `JOBDOC_AUTH_PASSWORD`.
- **Disable the gate** entirely (e.g. trusted private deploy) with `JOBDOC_AUTH_DISABLE=1`.
- Users sign in once per browser (a signed, HTTP-only, `Secure` cookie lasts 7 days) and can
  **Sign out** from the header. The immersive recording flow is never interrupted by login.
- The non-sensitive `/health` and `/metrics` probes stay reachable without login for monitoring.

### Install to home screen (PWA)

JobDoc ships a web manifest (`/manifest.webmanifest`) and a conservative service worker
(`/sw.js`) that caches the static app shell and enables **Add to Home Screen** on mobile. The
service worker never caches uploads, API calls, or the live recording flow — the recorder's own
offline durability is handled by IndexedDB. *(For pixel-perfect iOS home-screen icons, add PNG
`192×192` / `512×512` icons alongside `static/icon.svg`.)*

### Monitoring

- **`/health`** — readiness: keys, WeasyPrint, runtime GPU/acceleration, max video length.
- **`/metrics`** — non-sensitive ops: uptime, total jobs, recent uploads (24h), disk usage
  (uploads + reports MB), retention window, and media mode. No secrets or user content.
- **Structured logs** — production logs are one JSON object per line with `job_id` + `step`
  correlation (set `JOBDOC_LOG_JSON=0` for plain text locally). The startup banner logs a single
  `PRODUCTION READY` event with `media`, `keys`, `auth`, and `weasyprint` fields.

### CPU-only Spaces

Everything works on the free CPU tier — just slower on the video steps. Runtime detection
reports `media_mode: "cpu"`, and the app uses software ffmpeg + cloud Deepgram. No GPU is ever
required.

### How GPU acceleration is kept HF-compliant

- **All** GPU detection (`torch.cuda.is_available()`, `ffmpeg -hwaccels`, NVENC probe) happens at
  **runtime**, inside the running container — never during `docker build` (HF build machines
  have no GPU). See `utils/gpu_utils.py`.
- Every hardware path **falls back to software automatically** on any failure, with clear logs.
- Memory is released (`torch.cuda.empty_cache()`) after heavy media operations.

### GPU vs CPU behavior

| Step                         | GPU Space                              | CPU Space                  |
| ---------------------------- | -------------------------------------- | -------------------------- |
| Video normalization (→ MP4)  | NVDEC decode + `h264_nvenc` encode     | `libx264` (software)       |
| Still-frame extraction       | `-hwaccel cuda` decode                 | software decode            |
| Speech-to-text               | Deepgram (default) / optional local Whisper on GPU | Deepgram (default) |
| Max recording length         | 8 min (`JOBDOC_MAX_VIDEO_SEC` override)| 3 min                      |
| LLM report writing           | OpenAI (cloud)                         | OpenAI (cloud)             |

### Production hardening (public Spaces)

For a public demo Space, JobDoc applies a few lightweight protections out of the box:

- **Rate limiting** — `/upload_video` and `/generate_report` are limited per client IP (defaults
  ~12 uploads / 20 generations per 10 min) to protect Deepgram/OpenAI spend. Exceeding a limit
  returns a friendly **429** with a `Retry-After`. Tune with `JOBDOC_RATELIMIT_*` (see
  [Configuration](#configuration)). Limits are per-worker and in-memory — no external store needed.
- **Disk reaper** — a daemon thread age-prunes `uploads/`, frames, and `reports_output/` every hour
  (default keep **24 h**; set `JOBDOC_RETENTION_HOURS`). Recent jobs stay playable/downloadable.
- **Resource cleanup** — the extracted `*_audio.wav` sidecar is removed and CUDA memory is released
  after every generation, on success **and** failure paths.
- **Security headers** — `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and a
  `Permissions-Policy` that allows camera/microphone only on the app's own origin.
- **Friendly error pages** — 404 / 429 / 500 render a clean page (or JSON for the recorder/API)
  with a clear next step.
- **Access control** — a shared-credential login gate (see [Access control](#access-control-login)).

On launch, a single structured banner summarizes readiness, e.g.:

```json
{"ts":"…","level":"INFO","logger":"app","msg":"PRODUCTION READY","event":"startup","media":"GPU","keys":"space secrets","auth":"enabled","weasyprint":"ok","retention_hours":24.0,"pipeline":"v2"}
```

---

## Go-Live Checklist

A one-page checklist to push and launch on Hugging Face Spaces (GPU or CPU).

**1 · Prepare the Space**
- [ ] Create a **Docker** SDK Space; push this repo (front-matter sets `app_port: 7860`).
- [ ] (GPU) **Settings → Hardware** → select a GPU tier (T4 / A10G / …). CPU works too.

**2 · Configure secrets (Settings → Variables and secrets)**
- [ ] `SECRET_KEY` — long random string (stable login-cookie signing).
- [ ] `DEEPGRAM_API_KEY` + `OPENAI_API_KEY` — recommended for a hosted demo (or let users paste in UI).
- [ ] `JOBDOC_AUTH_USER` / `JOBDOC_AUTH_PASSWORD` — change from the defaults (`jobdoc` / `Burger_Time_101_!`).
- [ ] (Optional) `JOBDOC_PIPELINE=v2`, `JOBDOC_RETENTION_HOURS`, `JOBDOC_RATELIMIT_*`, `JOBDOC_MAX_VIDEO_SEC`.

**3 · Verify after build**
- [ ] Startup log shows the `PRODUCTION READY` JSON banner with the expected `media`, `keys`, `auth`.
- [ ] `GET /health` → `ready_for_production: true`; GPU Spaces show `acceleration.media_mode: "gpu"`.
- [ ] `GET /metrics` → returns uptime / jobs / disk (no secrets).
- [ ] Visiting any page redirects to **/login**; signing in with your credentials reaches Home.

**4 · Mobile smoke test (real devices)**
- [ ] iPhone **Safari** + Android **Chrome**: log in → pick report type → full-screen record → tap **MARK** →
      stop → upload → review → **Download PDF**.
- [ ] Confirm **Add to Home Screen** is offered (PWA manifest + service worker registered).
- [ ] Confirm camera/mic permission prompts and the immersive overlays are readable outdoors.

**5 · Guardrails sanity**
- [ ] Rapid repeated submits return a friendly **429** (rate limiting active).
- [ ] Old artifacts are pruned after `JOBDOC_RETENTION_HOURS` (disk reaper running).

---

## Local development

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt              # add --extra-index-url https://download.pytorch.org/whl/cu121 for GPU torch
# System libs for PDF + media:
#   Debian/Ubuntu: sudo apt install ffmpeg libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0
#   macOS:         brew install ffmpeg pango gdk-pixbuf libffi
export JOBDOC_PIPELINE=v2
export JOBDOC_COOKIE_INSECURE=1
python app.py        # serves on http://0.0.0.0:7860  (override with PORT=...)
```

If you have a local CUDA GPU + CUDA-enabled ffmpeg, GPU detection works locally too. Force CPU
mode with `JOBDOC_DISABLE_GPU=1` to mirror a CPU-only Space.

To run the exact production container locally:

```bash
docker build -t jobdoc .
docker run --rm -p 7860:7860 \
  -e DEEPGRAM_API_KEY=... -e OPENAI_API_KEY=... \
  jobdoc
# GPU (needs nvidia-container-toolkit):  add  --gpus all
```

---

## Design notes (mobile-first UI)

JobDoc is a **production tool** — there are no demo, sample-video, or test shortcuts. The primary
path is always: **pick report type(s) → optional job details → guided recording → process → review →
PDF**, designed to complete on a phone in the first few minutes on site.

**Design language**
- Deep navy/charcoal canvas with high-contrast white text for bright daylight readability.
- Warm **amber** accent reserved for the most important actions (MARK and primary CTAs).
- Soft glassmorphism overlays, large thumb-friendly tap targets, safe-area insets
  (`env(safe-area-inset-*)`), and reduced-motion support.
- A small, reusable component layer lives in `static/css/app.css` (buttons, cards, badges, chips,
  segmented controls, stepper, skeletons, toasts, disclosures, glass surfaces).

**Immersive recording screen** (`templates/recording.html` + `static/css/field_recorder.css` +
`static/js/field_recorder.js`)
- The live camera `<video>` fills **100% of the viewport** (`object-fit: cover`); it is a standalone
  full-screen document, not the standard page chrome.
- Floating glass overlays: top bar (titles + job address + progress + step dots), a dismissible
  center **guidance card**, status chips (recording lamp, timer, auto-stop countdown, live mic
  meter), a transient auto-hiding toast, and a bottom control bar.
- The hero action is the big amber **MARK** button (with haptic feedback via the Vibration API);
  **tapping anywhere on the live preview also marks** while recording.
- All recorder robustness is preserved: `getUserMedia` constraint ladder, MP4/WebM negotiation,
  `mark_events` emission, IndexedDB crash recovery, upload retry, and file-upload fallback.

**Other screens**
- **Home** (`index.html`): report-type cards with one-tap selection, collapsible Job Details and
  API keys, and a sticky launch bar.
- **Review & generate** (`report_preview.html`): animated pipeline stepper, confidence badges,
  one-tap **Accept all high-confidence**, a celebratory **report-ready** state, and a Light/Fast PDF
  option. Supporting panels (hardware, keys) are tucked into disclosures.

**Testing guidance:** test full-screen recording on real **iPhone Safari** and **Android Chrome** —
verify overlays stay readable over varied backgrounds, controls are thumb-reachable, the MARK button
gives satisfying feedback, and orientation changes keep the preview full-coverage. Confirm no
demo/testing features remain.

---

## Mobile testing checklist

- [ ] Open the Space URL on **iPhone Safari** and **Android Chrome** (HTTPS required for camera).
- [ ] Grant camera + microphone permission; preview shows live video.
- [ ] **Switch camera** (front/rear) with the ⟳ button before recording.
- [ ] Pick a **quality preset** (Balanced / High).
- [ ] Live camera preview fills the **entire screen**; overlays stay readable over the video.
- [ ] Record a 60–180s walkthrough; tap the big amber **MARK** button (or tap anywhere on the
      preview) at each finding — and say “Mark this.” for best alignment.
- [ ] Marks appear in the mini-timeline; **Undo** removes the last mark.
- [ ] Use **Next step** to advance through template sections; voice prompts speak.
- [ ] Stop → upload (with retry); a dropped connection saves the recording in IndexedDB for retry.
- [ ] On the preview page: original video plays (webm is normalized to MP4 for iOS).
- [ ] **Analyze recording** → review flagged sections → add notes → **Generate PDF** → download.
- [ ] On a GPU Space, the **Accelerated processing** indicator is green and media steps are faster.

---

## Mobile Browser Recording on HF Spaces

JobDoc records **in the browser** with `getUserMedia` + `MediaRecorder`. These APIs only work
over **HTTPS** (Hugging Face serves every Space over HTTPS, so the deployed demo is fine — but
`http://localhost` recording will not work; use the HTTPS Space or a tunnel).

**Recommended browsers**

| Device  | Use            | Avoid                                                            |
| ------- | -------------- | --------------------------------------------------------------- |
| iPhone/iPad | **Safari**  | Chrome/Firefox on iOS *cannot record video* (no MediaRecorder). |
| Android | **Chrome**     | In-app webviews (Instagram/Facebook browser) — open in Chrome.  |

**Permission flow & the "Enable Camera & Microphone" gate**

The recorder shows a full-screen gate explaining why access is needed before anything is captured.
Tapping **Enable** triggers the native permission prompt. The recorder detects the platform and
shows tailored recovery tips for the most common failures:

- **`NotAllowedError` (blocked):** allow Camera + Microphone for the site, then reload.
  - *iPhone:* tap **aA** in the address bar → **Website Settings** → allow Camera/Microphone (or
    Settings → Safari → Camera/Microphone → Allow).
  - *Android Chrome:* tap the **lock icon** → **Permissions** → allow Camera/Microphone.
- **`NotReadableError` (camera busy):** close other camera apps (FaceTime, Zoom) and retry.
- **`NotFoundError` (no device):** try the other camera, or use **Upload a video instead**.
- **`OverconstrainedError`:** switch to **Balanced** quality or flip the camera.
- **`SecurityError`:** you're not on HTTPS — open the `https://` Space URL.

**Reliability features**

- **Manual marks** — tapping **MARK THIS SECTION** records the exact timestamp (and optional note).
  Marks are uploaded with the video and used to anchor section frames even when spoken
  “Mark this.” cues are missed (noisy/quiet audio). Both signals are merged server-side.
- **Live mic meter** confirms audio is being captured before you rely on the recording.
- **Auto-stop countdown** appears in the last 15s before the Space's duration limit.
- **Crash recovery** — the in-progress recording is snapshotted to IndexedDB every few seconds.
  If the page refreshes, the phone backgrounds the tab, or the battery dies, you're offered
  **Recover & upload** on the next load. A failed upload is likewise saved for **Retry upload**.
- **Upload fallback** — if the camera can't be used at all, **Upload a video instead** runs the
  exact same pipeline (mark detection from spoken cues).

**Quick check:** open `/health` on the device — the `recording` block reports the duration limit,
accepted formats, and recommended browser; `acceleration` shows GPU vs CPU media mode.

---

## Add your own report templates

Templates are the single source of truth — drop a JSON file in `reports/` and it appears in the
picker, drives the guided recorder script, the Crew payload, and the PDF layout. Use an existing
file (e.g. `reports/room_report.json`) as a starting point. Key fields: `report_type` (slug),
`title`, `sections[]` with `id`, `name`, `capture_instructions`, and (schema v2) rich guidance
like `voice_prompt`, `on_screen_text`, `required`, and `pdf_styling`.

---

## Configuration

| Env var                          | Purpose                                                                 |
| -------------------------------- | ----------------------------------------------------------------------- |
| `DEEPGRAM_API_KEY`               | Speech-to-text (or paste in UI). Required to transcribe.                |
| `OPENAI_API_KEY`                 | OpenAI reasoning (or paste in UI). Required for v2 report writing.      |
| `OPENAI_MODEL`                   | Override the OpenAI reasoning model (default `gpt-4o`).                 |
| `JOBDOC_PIPELINE`                | `v1` (default) or `v2` (recommended multi-stage narration pipeline).    |
| `PORT`                           | Listen port for `python app.py` (default **7860**).                     |
| `JOBDOC_MAX_VIDEO_SEC`           | Override max recording length (else 480s GPU / 180s CPU).               |
| `JOBDOC_STT_BACKEND`             | `deepgram` (default) · `local` (force faster-whisper) · `auto` (local if GPU). |
| `JOBDOC_WHISPER_MODEL`           | Local Whisper model name (default `small`).                             |
| `JOBDOC_DISABLE_GPU`             | `1` forces CPU mode (mirror a CPU Space locally).                       |
| `JOBDOC_LOG`                     | `debug` for verbose pipeline logs.                                      |
| `JOBDOC_RETENTION_HOURS`         | Age (hours) before the disk reaper prunes uploads/frames/PDFs (default `24`; `0` disables). |
| `JOBDOC_RATELIMIT_UPLOAD`        | Max `/upload_video` requests per IP per 10 min (default `12`).          |
| `JOBDOC_RATELIMIT_GENERATE`      | Max `/generate_report` requests per IP per 10 min (default `20`).       |
| `JOBDOC_RATELIMIT_DISABLE`       | `1` disables in-memory rate limiting (e.g. trusted internal deploys).   |
| `JOBDOC_MAX_UPLOAD_MB`           | Max upload size in MB (default `500`); larger uploads get a friendly 413. |
| `SECRET_KEY`                     | Flask session signing key — **set in production** (random string).      |
| `JOBDOC_AUTH_USER`               | Login username (default `jobdoc`).                                      |
| `JOBDOC_AUTH_PASSWORD`           | Login password (default `Burger_Time_101_!`).                           |
| `JOBDOC_AUTH_DISABLE`            | `1` disables the login gate (trusted/private deploys).                  |
| `JOBDOC_COOKIE_INSECURE`         | `1` drops the `Secure` cookie flag for local HTTP dev only.             |
| `JOBDOC_LOG_JSON`                | `1` (default) structured JSON logs · `0` plain text for local dev.      |

**Health check:** `/health` reports API-key presence/source, WeasyPrint readiness, and the live
GPU/ffmpeg acceleration status.

---

## Troubleshooting

| Symptom                                   | Fix                                                                                                          |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| **`/health` shows `media_mode: cpu` on a GPU Space** | Confirm a GPU tier is selected in Space Settings; check logs for the CUDA line. ffmpeg may lack `cuda` hwaccel — the app still works (software path). |
| **GPU ffmpeg fails mid-job**              | Expected to be rare; JobDoc logs a warning and falls back to the CPU path automatically — the job never crashes. |
| **Camera/mic not available**              | HTTPS is required (Spaces are HTTPS). On iOS, allow permissions in Settings → Safari.                        |
| **iOS can't play the preview video**      | Handled — Android `.webm` uploads are normalized to `.mp4`. If normalization failed, check that ffmpeg is installed. |
| **PDF export fails**                      | Ensure the WeasyPrint system libs are installed (the Docker image includes them). `/health` shows readiness. |
| **Large image / slow first build**        | The CUDA base image is large; the build is one-time and cached. CPU-only users can comment out torch in `requirements.txt`. |
| **Transcription says audio too quiet**    | Re-record closer to the mic; manual marks still drive frame extraction even with no spoken cues.             |

---

## Project layout

| Path                       | Role                                                                |
| -------------------------- | ------------------------------------------------------------------- |
| `app.py`                   | Flask app (routes, upload, generate, health, keys). Binds 7860.     |
| `utils/gpu_utils.py`       | **Runtime-only** GPU detection + hardware ffmpeg command builders.  |
| `utils/video_utils.py`     | Normalize / audio / frame extraction with GPU→CPU fallback.         |
| `utils/transcription.py`   | Deepgram Nova-3 (default) + optional local faster-whisper backend.  |
| `utils/credentials.py`     | Per-session user API keys (server-side, never echoed).              |
| `crew/`                    | v1/v2 CrewAI pipelines (resilient, structured fallbacks).           |
| `reports/`                 | Report templates (JSON — single source of truth).                   |
| `templates/`, `static/`    | Mobile recorder UI, preview, and PDF templates.                     |
| `Dockerfile`               | Single image for CPU + GPU Spaces (HF-compliant, runtime GPU only). |

---

## License

**Proprietary — © Danny Barren. All rights reserved.** This is a prototype demo, not a
production-grade app: no commercial use or revenue generation without explicit written consent,
and use is at your own risk with no warranties. See [`LICENSE.md`](LICENSE.md) for the full terms
(also available on request from Danny).
