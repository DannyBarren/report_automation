# JobDoc / GenerSwift — Production / Demo Readiness Audit

Read-only audit. No code was changed except this file. Secrets were not printed. Every claim is `path:line` or **NOT FOUND**.

---

## 0. IDENTITY — which tree is this?

Quoted command results (this workspace):

```
pwd
/home/danny-barren/Desktop/Bar_BizD_Local/BBD_Prods/report_writer/GenerSwift-JobDoc-Demo-v1

git remote -v
fatal: not a git repository (or any of the parent directories): .git

git branch --show-current
(failed — no .git)

git log -5 --oneline --decorate
(failed — no .git)

git status -sb
(failed — no .git)

ls -la (top level, abbreviated)
.dockerignore  .env  .gitattributes  .gitignore  .venv/
DEMO_SCRIPT.md  Dockerfile  LICENSE.md  README.md
__pycache__/  app.py  crew/  modal_app.py  models.py  pinggy.ps1
prompts/  reports/  reports_output/  requirements.txt
run_demo.bat  run_demo.sh  start.sh  static/  templates/  tests/  utils/

wc -c app.py
110086 app.py

test -f Dockerfile          → EXISTS
test -f start.sh            → EXISTS
ls modal*.py .modal         → modal_app.py EXISTS (13705 bytes); .modal/ MISSING
test -f .env                → EXISTS
test -f .env.example        → MISSING
test -d .git                → MISSING
```

**Classification: 2) Local GenerSwift-JobDoc-Demo-v1 evolution**

Why not the other boxes:

| Candidate | Why rejected |
|---|---|
| 1) `DannyBarren/report_automation` GitHub snapshot (~2026-05-28, `app.py` ~51,184 B) | No `.git`. `app.py` is **110,086** bytes, not ~51k. |
| 3) Hugging Face Spaces Docker tree (`app.py` ~77,878 B) | HF surface **is** present (README YAML `sdk: docker`, `Dockerfile`, `start.sh`, PORT 7860), but `app.py` is **110k**, not ~77k. Local `.venv`, `.env`, `DEMO_SCRIPT.md`, roofing-only `reports/`. |
| 4) Mixed / unknown | Deploy leftovers are mixed (see below), but the product tree is clearly the local Demo-v1 evolution. |

**Leftover Modal vs HF (do not confuse):**

- **HF is wired as the real deploy target:** `README.md:1-10` front-matter (`sdk: docker`, `app_port: 7860`); `Dockerfile:85` `CMD ["sh", "/app/start.sh"]`; `start.sh:10-18` gunicorn `app:app` on `${PORT:-7860}`; `app.py:2695-2699` local bind also defaults to 7860.
- **Modal is leftover / optional side door:** `modal_app.py` exists (CUDA image + `modal.Volume` + secrets). **`.modal/` is MISSING.** Nothing in `start.sh` / `Dockerfile` invokes Modal. Treat `modal_app.py` as leftover unless someone explicitly `modal deploy`s it.
- **This is not `DannyBarren/PDF_to_JSON`.** Template translator repo is absent. Templates live in `reports/*.json` and are loaded by `utils/template_loader.py`.

---

## A. REPO MAP

### Top-level files / dirs

| Path | Role |
|---|---|
| `app.py` | Flask app: auth, upload, generate, health, PDF |
| `modal_app.py` | Leftover Modal.com WSGI wrapper |
| `models.py` | SQLAlchemy models (Client, CaptureSession, VideoUpload, …) |
| `Dockerfile` / `start.sh` | HF Spaces Docker + gunicorn |
| `requirements.txt` | Python deps (CrewAI, WeasyPrint, torch, faster-whisper) |
| `README.md` | HF YAML + product/deploy docs |
| `DEMO_SCRIPT.md` | Stale conference-room script (port 5000, missing UI) |
| `run_demo.sh` / `run_demo.bat` | Local launchers |
| `LICENSE.md` | License |
| `pinggy.ps1` | Leftover Windows tunnel script (78,020 B, 2026-05-05) |
| `.env` | Local secrets file (EXISTS, gitignored) |
| `.gitignore` | Ignores `.env`, `.venv/`, `uploads/`, `reports_output/`, `instance/` |
| `.dockerignore` / `.gitattributes` | Docker/git attributes (no `.git` here) |
| `.venv/` | Local Python 3.14 venv |
| `crew/` | v1/v2 pipelines, CrewAI, structured matcher |
| `utils/` | STT, video, PDF, templates, GPU, creds |
| `templates/` | Flask HTML (recorder, preview, PDF Jinja) |
| `static/js/` `static/css/` | Field recorder + app CSS |
| `reports/` | Report JSON templates (one file) |
| `reports_output/` | Generated PDFs / frames |
| `prompts/` `prompts/v2/` | Crew prompt text |
| `tests/` | One smoke script: `e2e_report_test.py` |

### Required-path status

| Path | Status | One-line role |
|---|---|---|
| `app.py` | **EXISTS** (110,086 B) | Flask routes + generate-report orchestrator |
| `start.sh` | **EXISTS** | Kill/rebind port 7860, exec gunicorn `app:app` |
| `Dockerfile` | **EXISTS** | CUDA 12.1 Ubuntu 22.04 HF image; GPU probe forbidden at build |
| `requirements.txt` | **EXISTS** | Flask, CrewAI, WeasyPrint, moviepy, torch, faster-whisper |
| `README.md` | **EXISTS** | HF Spaces YAML + deploy/run docs |
| `crew/structured_pipeline.py` | **EXISTS** | Deterministic “Mark this.” / marks / step windows → `FinalReportOutput` |
| `crew/flask_pipeline.py` | **EXISTS** | Bridge: `run_v1_pipeline` / `run_v2_pipeline` |
| `crew/jobdoc_crew_v2.py` | **EXISTS** | v2: transcribe → Crew stages → reconcile |
| `crew/report_resilience.py` | **EXISTS** | Fallback report, review items, merge notes |
| `crew/models_v2.py` | **EXISTS** | v2 Pydantic contracts (`PipelineResultV2`, observations) |
| `utils/video_utils.py` | **EXISTS** | ffmpeg (GPU→CPU) + MoviePy fallback; **no OpenCV** |
| `utils/transcription.py` | **EXISTS** | Deepgram Nova-3 default; optional local Whisper |
| `utils/report_pdf.py` | **EXISTS** | Frames → Jinja HTML → WeasyPrint PDF |
| `utils/gpu_utils.py` | **EXISTS** | Runtime-only CUDA/ffmpeg hwaccel; `JOBDOC_DISABLE_GPU` |
| `utils/credentials.py` | **EXISTS** | Per-session key store + env snapshot/restore |
| `utils/template_loader.py` | **EXISTS** | Load/validate `reports/*.json` via Pydantic |
| `utils/template_guidance.py` | **EXISTS** | `build_guidance_plan` → recorder script |
| `utils/pipeline_config.py` | **EXISTS** | `JOBDOC_PIPELINE` default **v1** |
| `templates/report_templates/template_driven.html` | **EXISTS** | WeasyPrint print layout + footer counters |
| `static/js/field_recorder.js` | **EXISTS** | MARK taps, IndexedDB, upload retry, iOS notes |
| `static/css/field_recorder.css` | **EXISTS** | Immersive recorder styling |
| `reports/*.json` | **EXISTS — 1 file** | `reports/roofing_realty_inspection.json` only |
| `prompts/v2/*` | **EXISTS** | See list below |
| `tests/` | **EXISTS** | `tests/e2e_report_test.py` only |
| `DEMO_SCRIPT.md` | **EXISTS** | Stale (port 5000, “Start quick demo”, People+Room) |
| `run_demo.sh` | **EXISTS** | Creates venv, pip install, `python app.py` |
| `.env` | **EXISTS** | Gitignored (`.gitignore:4`). Values **not** inspected. |
| `.env.example` | **MISSING** | No checked-in key-name template |

**`reports/*.json` (every file):**

1. `reports/roofing_realty_inspection.json` — `schema_version: 3` (`reports/roofing_realty_inspection.json:2`)

**NOT FOUND in `reports/`:** `room_report.json`, `people_report.json`, `home_inspection.json`, `home_inspection_detailed.json`, `room_inspection.json`. README still cites `reports/room_report.json` as a starting point (`README.md:316`).

**`prompts/v2/*`:**

- `_template_ssot.txt`
- `_narration_policy.txt`
- `agent_report_writer.txt` / `task_report_writer.txt`
- `agent_transcript_analyzer.txt` / `task_transcript_analyzer.txt`
- `agent_quality_reviewer.txt` / `task_quality_reviewer.txt`
- `agent_domain_expert.txt` / `task_domain_expert.txt`

**`.env` key NAMES (from README/code only — `.env` values not read):**

Documented / used names: `DEEPGRAM_API_KEY`, `OPENAI_API_KEY` (+ aliases `OPEN_AI_API_KEY`, `OPENAI_KEY`, …), `SECRET_KEY`, `JOBDOC_PIPELINE`, `JOBDOC_AUTH_USER`, `JOBDOC_AUTH_PASSWORD`, `JOBDOC_AUTH_DISABLE`, `JOBDOC_COOKIE_INSECURE`, `JOBDOC_DISABLE_GPU`, `JOBDOC_STT_BACKEND`, `JOBDOC_WHISPER_MODEL`, `JOBDOC_MAX_VIDEO_SEC`, `JOBDOC_MAX_UPLOAD_MB`, `JOBDOC_RETENTION_HOURS`, `JOBDOC_RATELIMIT_UPLOAD`, `JOBDOC_RATELIMIT_GENERATE`, `JOBDOC_RATELIMIT_DISABLE`, `PORT`, `FLASK_DEBUG`, `OPENAI_MODEL`, `JOBDOC_LOG`, `JOBDOC_LOG_JSON`, `CREW_VERBOSE`, `JOBDOC_CREW_TIMEOUT_SEC`, `JOBDOC_TRANSCRIPTION_TIMEOUT_SEC`, `JOBDOC_GUIDANCE_AGENT`.

`.env` is gitignored (`.gitignore:4`). `.env.example` is **MISSING**.

---

## B. LIVE PIPELINE TRACE (P0)

### Real code path (file:line)

```
record (field_recorder.js) or upload (app.py:1437+)
  → mark_events + step_events stored on VideoUpload.job_meta (app.py:1532-1552)
  → POST /generate_report (app.py:2059)
  → try_extract_audio warm-up, non-fatal (app.py:2198-2208)
  → STT: transcribe_video → Deepgram Nova-3 and/or local Whisper (utils/transcription.py:56-87)
        called from transcribe_video_file (crew/jobdoc_crew_v2.py:858) on v2
        or JobDocCrew._build_transcript (crew/crew.py:58) on v1
  → spoken “Mark this.” via TRIGGER_RE + merge mark_events + step_events
        (crew/structured_pipeline.py:45, 478-753)  *** v2 only ***
  → section matching → MatchedSection (same)
  → FinalReportOutput:
        v2: Crew writer then reconcile_final_report_with_matched (jobdoc_crew_v2.py:795-836)
        v1: build_final_report_output from matched, no marks (crew.py:97-124)
  → enrich_report_with_still_frames_resilient (utils/report_pdf.py:104)
        extract_frame ffmpeg GPU→CPU→MoviePy (utils/video_utils.py:351)
  → render_report_html → template_driven.html (utils/report_pdf.py:312)
  → write_pdf WeasyPrint (utils/report_pdf.py:364)
```

### 1. Default pipeline

`get_jobdoc_pipeline()` returns **`v1` if `JOBDOC_PIPELINE` is unset**.

```13:18:utils/pipeline_config.py
def get_jobdoc_pipeline() -> str:
    """Return ``v1`` (default) or ``v2`` from ``JOBDOC_PIPELINE``."""
    raw = os.getenv("JOBDOC_PIPELINE", PIPELINE_V1).strip().lower()
    if raw in (PIPELINE_V2, "2", "narration", "narration_v2"):
        return PIPELINE_V2
    return PIPELINE_V1
```

`/generate_report` (`app.py:2112`, `2214-2248`):

- if `is_v2_pipeline()` → `_run_v2_crew_phase` → `crew.flask_pipeline.run_v2_pipeline` → `generate_report_from_video`
- else → `crew.flask_pipeline.run_v1_pipeline` → `JobDocCrew.kickoff`

README itself says v1 is default and v2 is recommended (`README.md:329`). Local README run recipe exports `JOBDOC_PIPELINE=v2` (`README.md:189`) but **`run_demo.sh` does not**. Whether this machine’s `.env` sets it was **not** checked (secret file).

**This is the highest-leverage live-demo risk.** v1 does not receive `mark_events` / `step_events` (see B.4).

### 2. Who constructs `FinalReportOutput`? Do structured summaries win?

**v2 (recommended):**

1. Deterministic `matched = build_matched_sections(..., mark_events=, step_events=)` — `crew/jobdoc_crew_v2.py:660-662`
2. Crew writer emits a `FinalReportOutput` — `jobdoc_crew_v2.py:795`
3. If writer fails → `build_final_report_output(matched)` — `jobdoc_crew_v2.py:808-813`
4. QA may swap in `revised_report` — `jobdoc_crew_v2.py:825-828`
5. **Authoritative reconcile** always runs: `reconcile_final_report_with_matched` — `jobdoc_crew_v2.py:836`

Reconcile (`crew/structured_pipeline.py:852-936`):

- Section set/order = matched (template capture order).
- Writer prose kept **only** if `_writer_summary_is_faithful` (≥ ~60% of min words **and** ≥ 12% token overlap with real narration when narration exists) — `structured_pipeline.py:816-849`.
- Otherwise structured `m.summary` + `field_guesses` win.
- Timestamps + `frames` always grafted from matched.

**Structured summaries WIN on v2 when the writer ignores narration.** Writer can still win with generic-but-overlapping prose (12% is a low bar — PARTIAL).

**v1 (default):**

- `FinalReportOutput` is built **only** from `build_matched_sections(tr, templates)` with **no** mark/step events — `crew/crew.py:97-124`.
- CrewAI still runs; its output is stored as `crew_raw`. Payload `final_report` is the **deterministic** object, not the writer’s JSON — `crew.py:174-184`.
- So on v1 the LLM does **not** overwrite PDF sections. But matching is spoken-cue / positional only.

`run_v1_pipeline` does **not** accept or forward `mark_events` — `crew/flask_pipeline.py:190-207`.

### 3. Where is `IDENTIFICATION_PHRASE` / “Mark this.” detected?

**Regex on STT segment text. Not LLM. Not a dedicated Deepgram word-timestamp “Mark this.” detector.**

| Layer | What it does |
|---|---|
| Constant | `IDENTIFICATION_PHRASE = "Mark this."` — `app.py:103`, `crew/agents.py:18`, `crew/models_v2.py:23`, `utils/template_schema.py:38` |
| Regex | `TRIGGER_RE = re.compile(r"mark\s*this\.?", re.IGNORECASE)` — `structured_pipeline.py:45` |
| Spoken cues | Segment text scanned: `if TRIGGER_RE.search(str(seg.get("text", "")))` — `structured_pipeline.py:522-524` |
| Deepgram | Utterances preferred; else word list chunked every 12 words; else whole transcript at 0.0s — `utils/transcription.py:228-289`. Words are a **fallback segment builder**, not a phrase aligner. |
| Local Whisper | Segment `start`/`end`/`text` — `transcription.py:114-127` |
| LLM | **Not used** for cue detection. Writer receives already-matched narration. |

Noisy audio that never transcribes as “mark this” will miss spoken cues. That is why `mark_events` exist — **on v2 only**.

### 4. Are `mark_events` merged with spoken cues, or does one drop the other?

**v2: merged. v1: recorder marks are dropped.**

Recorder emits both (`static/js/field_recorder.js:1152-1154`):

```javascript
fd.append('mark_events', JSON.stringify(state.marks || []));
fd.append('step_events', JSON.stringify(state.stepEvents || []));
```

Server stores them on upload (`app.py:1532-1552`) and reloads them at generate (`app.py:2102-2105`).

v2 `build_matched_sections` resolution order (`structured_pipeline.py:497-507, 618-742`):

1. Manual MARK tap(s) tagged to the section (or placed by step window)
2. Spoken cue(s) in that step window
3. Step window + narration (no tap/cue)
4. Step window, silent → still + “no narration” prose (may lead with `default_text`)
5. Nothing → filler, `image_timestamp_sec=0.0`, **no frames**

Taps and cues are **both** collected into `frames` via `_build_section_frames` (`structured_pipeline.py:414-475`). Near-duplicates < 1.2s are merged.

**v1:** `build_matched_sections(tr, templates)` — `crew.py:97`. `mark_events` / `step_events` never passed. Flask still saves them on the upload row, but v1 matching never sees them.

### 5. Still-frame extraction

**Preferred: ffmpeg. Fallback: MoviePy. OpenCV is NOT imported anywhere in project `utils/` / `crew/` / `app.py`.**

`extract_frame` (`utils/video_utils.py:351-414`):

1. `normalize_video_to_mp4`
2. Clamp timestamp to duration
3. `_transcode_with_fallback`: GPU ffmpeg if `should_use_hw_video()`, then CPU ffmpeg (`video_utils.py:84-86`, `380-390`)
4. MoviePy + PIL if ffmpeg fails

`enrich_report_with_still_frames_resilient` (`utils/report_pdf.py:104-233`):

- Uses `sec.frames`, else `image_timestamp_sec` **only if > 0.05** (`report_pdf.py:147-158`)
- `image_timestamp_sec` missing / 0.0 / “not reached”: **skip still**, log, keep section — **no crash** (`report_pdf.py:159-165`)
- Failed extract: warning; template shows `.td-photo-missing` badge (`template_driven.html:625-628`)
- If **zero** stills for the whole report: `_guarantee_report_images` uses real `mark_times` or even spread (`report_pdf.py:204-227`, `236-309`)
- Images stored as filesystem path + `out_path.as_uri()` (`report_pdf.py:183`) — `file://` URI for WeasyPrint

`fast=True` (form `pdf_mode=light|fast`) uses `FRAME_WIDTH_FAST=900` vs 1280 (`report_pdf.py:52-53`, `132`).

### 6. Does STT narration land in PDF section summaries?

**On v2, yes when matching found speech.** Summaries come from:

- `_professional_summary_from_narration` / `_fallback_multiframe_summary` (structured) — `structured_pipeline.py:124-167`
- or faithful writer prose (v2 reconcile)

`default_text` **does** become visible when narration is empty:

```378:392:crew/structured_pipeline.py
    base = (default_text or "").strip()
    ...
    if base:
        return f"{base} {limitation}"
```

Unreached sections lead with `default_text` or `capture_instructions` (`structured_pipeline.py:720-726`). e2e asserts this (`tests/e2e_report_test.py:301-303`).

`writer_instructions` are **not** printed as body text; they go into the writer prompt (`utils/template_guidance.py:141`). `capture_instructions` **can** leak into the “not reached” filler.

**On default v1:** only spoken-cue / leftover positional narration. Manual MARK without a transcribed “Mark this.” → later sections often get filler / `default_text`. That is the historical empty-PDF failure mode.

### 7. Footer / `{page}` / `{total_pages}` / `{total}`

**Present in template JSON:**

`reports/roofing_realty_inspection.json:24`:

`page_footer`: `Confidential · Structured Roofing Systems · Page {page} of {total_pages}`

Schema default (`utils/template_schema.py:112`):

`"{business_name} · Confidential · Page {page} of {total_pages}"`

**Sanitize / neutralize — YES, two layers (plus CrewAI brace strip).**

PDF styling interpolator (`utils/pdf_render.py:331-360`):

```python
def interpolate_styling_string(template: str, *, business_name: str, report_title: str) -> str:
    ...
    out = (template or "").replace("{business_name}", ...).replace("{report_title}", ...)
    out = _PAGE_CLAUSE_RE.sub("", out)   # drops "Page {page} of {total_pages}"
    out = _STYLING_TOKEN_RE.sub("", out) # strips leftover {token}
```

Jinja belt-and-suspenders (`templates/report_templates/template_driven.html:12`):

```jinja
{% set footer_prefix = footer_text | replace('{page}', '') | replace('{total_pages}', '') | replace('{total}', '') | trim %}
```

WeasyPrint live counters (`template_driven.html:39-40`):

```css
content: "{{ footer_prefix }} · Page " counter(page) " of " counter(pages);
```

CrewAI prompt sanitizer (`utils/prompt_utils.py:55-81`): `neutralize_crew_brace_tokens` rewrites `{word}` → `[word]` so `{page}` cannot abort CrewAI. Used by `load_prompt_v2` (`prompt_utils.py:137-139`).

e2e asserts rendered HTML has no `{page}` / `{total_pages}` / `{business_name}` (`tests/e2e_report_test.py:277-278`).

**Status: FIXED in the v2 template-driven path, if those functions actually run.** Legacy `schema_version < 2` HTML (`report_pdf.py:333`) would skip this path — no v1 templates are in `reports/` today.

---

## C. TEMPLATES + MOBILE RECORDER

### Templates on disk

| File | schema_version |
|---|---|
| `reports/roofing_realty_inspection.json` | **3** (`:2`) — not “legacy v1” |

**MISSING:** `room_report.json`, `people_report.json` (legacy v1 names), `home_inspection.json`, `home_inspection_detailed.json`, `room_inspection.json` (v2 names). Domain-expert map in `crew/models_v2.py:289-297` still names those slugs.

`utils/template_loader.py:15-18` / `20-33`: any `reports/*.json` that validates as `ReportTemplate` loads. `utils/template_schema.py:18-19, 375-401`: **v1 (`sections[]` only) and v2/v3 rich templates both accepted**; v1 is upgraded to guidance + content_structure on load.

**Live picker only shows roofing** (plus the synthetic Misc step from `utils/misc_section.py`, always appended — `app.py:406-407`).

### Does `build_guidance_plan` drive the recorder?

**Yes.** `app.py:675-698` calls `crew.jobdoc_crew_v2.build_guidance_plan` (wrapper around `utils.template_guidance.build_guidance_plan`, optional `JOBDOC_GUIDANCE_AGENT` polish — `jobdoc_crew_v2.py:76-86`).

`build_guidance_plan` copies `voice_prompt`, `on_screen_text`, `min_marks`, `suggested_phrases` onto each `GuidedCaptureStep` (`utils/template_guidance.py:121-145`). `step_payload` exposes them (`utils/guidance_session.py:86-95`). Recorder renders `on_screen_text` / `voice_prompt` and reads `min_marks` (`field_recorder.js:357-379`).

### `field_recorder.js`

**`mark_events` JSON shape** (`field_recorder.js:657-664`; server mirror `app.py:584-611`):

```json
{"t_sec": 12.34, "step_index": 1, "section_id": "overview", "report_type": "roofing_realty_inspection", "title": "Overview", "note": ""}
```

**`step_events`:** `{t_sec, section_id, report_type, title, …}` when a guided step becomes active (`field_recorder.js:497-500`).

**IndexedDB crash recovery:** DB `jobdoc_field_drafts` / store `recordings`; snapshot every 5s (`field_recorder.js:14-20`, `1298+`); restore draft + in-progress (`226-229`, `1236-1396`).

**Upload retry:** `retryOrFail` + `#btn-retry-upload` (`field_recorder.js:1411`, `168`); 300s XHR timeout (`1161`).

**iOS Safari MediaRecorder:** Chrome-on-iOS cannot record (`field_recorder.js:41-45`, `235-237`); prefer mp4 then webm (`705-716`); HTTPS required (`240-242`); wake-lock gap noted (`951`). README table matches (`README.md:276`).

### Leftover unprofessional demo UI?

README claim: “there are no demo, sample-video, or test shortcuts” (`README.md:210`).

**In live HTML/JS: claim holds.** No “Start quick demo”, no sample-video button in `templates/` (grep **NOT FOUND**).

**Stale / leftover that *would* look unprofessional if shown:**

- `DEMO_SCRIPT.md:10-34` — port **5000**, “Start quick demo”, “People + Room pre-selected” — **none of that exists**. App binds **7860** (`app.py:2698`). Only roofing template exists.
- `pinggy.ps1` (78k, May 2026) — leftover tunnel script at repo root.
- `modal_app.py` — leftover Modal deploy module (not in the UI).
- README still documents default login `jobdoc` / `Burger_Time_101_!` (`README.md:75`).

---

## D. PDF / WEASYPRINT

### `render_report_html` + `write_pdf`

- `render_report_html` — `utils/report_pdf.py:312-361`: schema_version ≥ 2 → `build_pdf_page_context` + `template_driven.html`; else legacy `report_templates/{rt}.html` or `generic_report.html`.
- `write_pdf` — `utils/report_pdf.py:364-411`: wraps HTML, `HTML(..., base_url=str(base_dir)+"/").write_pdf(...)`.
- Called from `_build_pdf_for_job` — `app.py:1754-1838`.

### Resilient frames + light/fast path

- `enrich_report_with_still_frames_resilient` — `report_pdf.py:104`
- Fast/light: `FRAME_WIDTH_FAST=900` when `pdf_mode` is `light` or `fast` — `app.py:2362`, `report_pdf.py:132`

### WeasyPrint bootstrap / `/health`

- Import-time: `WEASYPRINT_STATUS = bootstrap_weasyprint_for_app(BASE_DIR)` — `app.py:95`
- Windows-oriented GTK bootstrap: `utils/setup_weasyprint.py` (portable GTK extract)
- `/health` includes `"weasyprint": wp.to_dict()` — `app.py:1205`
- `WeasyPrintStatus.to_dict()` keys (`utils/setup_weasyprint.py:55-76`): `ready`, `platform`, `message`, `gtk_bin_dirs`, `gtk_runtime_path`, `auto_install_attempted`, `auto_install_succeeded`, `portable_extract_attempted`, `portable_extract_succeeded`, `installer_attempted`, `is_elevated`, `prefer_portable`, `install_hint`, `docs_url`, `manual_release_url`
- Dockerfile installs Pango/Cairo/GDK-Pixbuf (`Dockerfile:37-38`)
- If not ready, generate returns **503** with setup URLs (`app.py:2333-2350`)

### Image URI vs filesystem path

Extracted stills set **both** `image_path` (filesystem) and `image_uri` (`Path.as_uri()` → `file://…`) — `report_pdf.py:183`. HTML uses `photo.image_uri` (`template_driven.html:605`). WeasyPrint `base_url` is the project root (`report_pdf.py:400`). **Broken images if URI/path is wrong or the file was reaped** — demo killer; code path is file URI, not `/frames/…` HTTP.

### Captions = narration + timestamp?

Roofing template: `include_timestamps: true`, `include_narration_in_caption: true`, `photo_caption_max_words: 52` (`roofing_realty_inspection.json:22-23`).

`frame_caption` (`utils/pdf_render.py:185-203`): `"{Marked|Cue|Walkthrough} at {time} · {note|narration|summary}"`. Template prints `photo.caption` when `include_narration_in_caption` (`template_driven.html:607-620`).

---

## E. PRODUCTION / DEMO SURFACE

### Auth gate

**Present.** `app.py:262-306`: session gate; public endpoints `login`, `logout`, `static`, `health`, `metrics`, `service_worker`, `web_manifest`.

Defaults **hardcoded** (`app.py:270-271`):

- `JOBDOC_AUTH_USER` default `jobdoc`
- `JOBDOC_AUTH_PASSWORD` default `Burger_Time_101_!`

Same defaults in `README.md:75`. Override via those env vars. `JOBDOC_AUTH_DISABLE=1|true|yes` disables (`app.py:280-281`). Login: `app.py:887-905`. Logout clears session keys (`app.py:908-915`).

**Local HTTP gotcha:** `SESSION_COOKIE_SECURE` is **True unless** `JOBDOC_COOKIE_INSECURE=1` (`app.py:152`). `run_demo.sh` does **not** set it. Login cookies may not stick on `http://127.0.0.1` — looks like “login is broken.”

### `/health` JSON keys (`app.py:1169-1210`)

`ok`, `service`, `pipeline`, `keys` (`DEEPGRAM_API_KEY`, `OPENAI_API_KEY` booleans), `key_sources`, `providers` (`transcription`, `llm`), `acceleration`, `max_video_seconds`, `weasyprint`, `recording`, `ready_for_production`, `messages`

`acceleration` keys (`utils/gpu_utils.py:346-353`): `gpu`, `ffmpeg_available`, `ffmpeg_hwaccels`, `nvenc`, `hw_video_pipeline`, `label`, `media_mode`

### `/metrics` JSON keys (`app.py:1261-1280`)

`ok`, `service`, `uptime_seconds`, `uptime_human`, `started_at`, `jobs` (`total`, `recent_uploads_24h`), `disk` (`uploads_mb`, `reports_mb`, `total_mb`, `retention_hours`), `acceleration`, `pipeline`

### Per-session API keys

- Store: in-memory `_store` keyed by `session["kid"]` — `utils/credentials.py:60-62`, `app.py:121-127`
- Apply: `apply_session_keys_to_env` — `credentials.py:165-189`; user-pasted keys **overwrite** process env for the request
- Snapshot + lock: `app.py:165-169`, `2154-2166`
- Teardown restore: `app.py:314-340` → `credentials.restore_env`

**Can keys leak across users in the same worker?** Mitigated, not hermetic.

- Gunicorn: **1 worker, 4 threads** (`start.sh:12-13`). `os.environ` is process-global.
- Generation is serialized by `_GENERATION_LOCK` (600s timeout → 503) (`app.py:2159-2163`).
- Teardown restores env on every exit path **if** `g._key_env_snapshot` was set.
- Early return **before** the lock (validation 400s) never applies keys — OK.
- Early return **after** lock (e.g. `require_asr_credentials` at `app.py:2190`) still hits teardown — OK.
- Residual: any other thread that reads `os.environ["DEEPGRAM_API_KEY"]` during an in-flight generate sees the generating user’s pasted key. `/health` `effective_key_status` can mis-attribute that env value as `"space"` for a *different* browser (`credentials.py:224-230`). **No second generate can run concurrently** (lock). Treat as **P2**, not a second-user generate leak.

### Rate limit + disk reaper

- Rate limit: `app.py:173-259`; `@rate_limited("upload")` / `("generate")`. Env: `JOBDOC_RATELIMIT_*`, `JOBDOC_RATELIMIT_DISABLE`.
- Reaper: `utils/maintenance.py`; started in `_init_app` (`app.py:2669-2672`) on `uploads/` + `reports_output/`; default 24h; session-linked recordings protected.

### Deploy target actually wired

| Target | Wired? |
|---|---|
| **HF Spaces Docker** | **Yes** — Dockerfile CMD `start.sh`, PORT 7860, README `sdk: docker` |
| **Modal** | **Leftover** — `modal_app.py` only; no `.modal/` |
| **Local `python app.py`** | **Yes** — `app.py:2695-2699`, `run_demo.sh:24` |

### GPU

- Detection **runtime-only** (`Dockerfile:13-16`, `gpu_utils.py:90-129`, `_init_app` `app.py:2656-2664`)
- **No** `nvidia-smi` / `torch.cuda` during `docker build` (comment + no such RUN)
- `JOBDOC_DISABLE_GPU` honored (`gpu_utils.py:97-98`)

### `SECRET_KEY`

Default still insecure: `"dev-insecure-change-me"` (`app.py:143`). README tells you to set it (`README.md:67`, `341`).

### Local run path

1. `./run_demo.sh` — creates `.venv`, **pip install**, `python app.py` (does **not** set `JOBDOC_PIPELINE=v2` or `JOBDOC_COOKIE_INSECURE=1`)
2. `python app.py` — `0.0.0.0:${PORT:-7860}`
3. `docker run -p 7860:7860` — `README.md:199-204`

`DEMO_SCRIPT.md` still says `http://127.0.0.1:5000` — **wrong**.

---

## F. KNOWN REGRESSION CHECKLIST

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | CrewAI / Jinja `{page}` / `{total_pages}` / `{total}` footer leakage | **FIXED** (v2 template-driven path) | `pdf_render.py:340-360`, `template_driven.html:12,39-40`, `prompt_utils.py:55-81`, e2e `tests/e2e_report_test.py:277` |
| 2 | Sections marked missing / no still frames | **PARTIAL** | Skip + missing badge when `timestamp_sec` ~0 (`report_pdf.py:147-165`, `template_driven.html:625-628`). Whole-report safety net if **all** fail (`report_pdf.py:204-227`). v1 without marks still produces 0.0 anchors and skipped stills (`crew.py:97`). |
| 3 | PDF empty of real narration (template / `default_text`) | **PARTIAL** | v2: structured narration wins when present (`structured_pipeline.py:618-639, 852-914`). Empty/silent sections **intentionally** lead with `default_text` (`structured_pipeline.py:378-392, 720-726`). **Default v1 drops MARK taps** → this regression returns. |
| 4 | LLM writer overriding deterministic matched_sections | **FIXED on v2** / **N/A on v1 PDF** | v2 reconcile `structured_pipeline.py:852-936` + `jobdoc_crew_v2.py:836`. 12% overlap bar is weak (`:849`). v1 PDF uses deterministic `final_report` (`crew.py:116-184`) but without marks. |
| 5 | Frame extraction env-sensitive; OpenCV required | **FIXED** (OpenCV not required) | ffmpeg GPU→CPU→MoviePy (`video_utils.py:351-414`). `cv2` / `opencv` **NOT FOUND** in project Python. |
| 6 | Spoken “Mark this.” missed — `mark_events` should still anchor | **FIXED on v2** / **STILL BROKEN on default v1** | v2 merge `structured_pipeline.py:618-654`; forwarded `flask_pipeline.py:125-126`, `jobdoc_crew_v2.py:987-988`. v1 never passes them (`crew.py:97`, `flask_pipeline.py:190-207`). |
| 7 | Other `{report_title}` / `{business_name}` / `{page}` leakage in writer output | **PARTIAL / FIXED for prompts + footers** | `neutralize_crew_brace_tokens` (`prompt_utils.py:55`); styling interpolator strips leftovers (`pdf_render.py:355-356`). Writer body prose is **not** scanned for `{page}` after reconcile. e2e checks HTML only. |

---

## G. GO-LIVE BLOCKERS

| ID | Sev | Demo symptom | Root cause (file:line) | Next Cursor prompt title |
|---|---|---|---|---|
| G1 | **P0** | Live demo uses v1: MARK taps ignored; noisy audio → empty/generic sections, no stills | `get_jobdoc_pipeline()` defaults to v1 (`pipeline_config.py:13-18`); `run_v1_pipeline` drops marks (`flask_pipeline.py:190-207`, `crew.py:97`); `run_demo.sh:23-24` does not export `JOBDOC_PIPELINE=v2` | **Force v2 default and plumb marks into v1 or delete v1 from the demo path** |
| G2 | **P0** | Local HTTP login appears broken (session cookie never sticks) | `SESSION_COOKIE_SECURE` True unless `JOBDOC_COOKIE_INSECURE=1` (`app.py:152`); `run_demo.sh` unset | **Local-dev cookie flag: default insecure on HTTP** |
| G3 | **P1** | Presenter follows `DEMO_SCRIPT.md` → wrong port, missing “Start quick demo”, missing People/Room templates | `DEMO_SCRIPT.md:10-34` vs `app.py:2698` and only `reports/roofing_realty_inspection.json` | **Rewrite DEMO_SCRIPT for roofing-only :7860** |
| G4 | **P1** | Silent / unmatched sections print template `default_text` / capture intent as if it were findings | `structured_pipeline.py:378-392, 720-726` | **Stop printing default_text as client-facing findings** |
| G5 | **P1** | Writer can keep generic prose if ≥12% token overlap | `_writer_summary_is_faithful` (`structured_pipeline.py:843-849`) | **Tighten narration-overlap gate** |
| G6 | **P2** | Shared default login in source + README | `app.py:270-271`, `README.md:75` | **Require auth env; remove hardcoded password** |
| G7 | **P2** | Insecure `SECRET_KEY` default | `app.py:143` | **Fail closed if SECRET_KEY unset in prod** |
| G8 | **P2** | No `.env.example`; leftover `modal_app.py` / `pinggy.ps1` confuse deploy | `.env.example` NOT FOUND; `modal_app.py:1`; `pinggy.ps1` | **Add .env.example; quarantine leftover deploy files** |
| G9 | **P2** | Per-session keys sit on process `os.environ` for the generate window | `credentials.py:165-189`, `app.py:165-169` | **Don’t put pasted keys in process env** |
| G10 | **P1** | README cites missing `room_report.json`; picker is roofing-only | `README.md:316`; `reports/` has one JSON | **Align docs + restore or drop extra templates** |

### Tests / smoke that exist

- `tests/e2e_report_test.py` — deterministic matcher + bad-writer reconcile + ffmpeg stills + WeasyPrint; **no live Deepgram/OpenAI**; **does not hit `/generate_report` or the v1 Flask path**.
- `DEMO_SCRIPT.md` — stale human script, not executable.
- `run_demo.sh` — launcher, not a smoke test.

### What this audit DID NOT check

- Did not run the app, e2e test, Docker build, or a real recording→PDF.
- Did not read `.env` values (so unknown whether this machine already sets `JOBDOC_PIPELINE=v2` / `JOBDOC_COOKIE_INSECURE=1`).
- Did not open a produced PDF to confirm WeasyPrint counters vs leftover `{page}`.
- Did not exercise iOS Safari / Android Chrome cameras.
- Did not deploy to HF or Modal.
- Did not verify GTK/Pango on this Linux host beyond “WeasyPrint is importable in `.venv`.”
- Did not review CrewAI prompt files line-by-line for leftover `{page}` after neutralize.
- Did not check `reports_output/` leftover PDFs.
- Did not audit SQLite schema vs production Alembic (dev-only `_ensure_sqlite_schema`, `app.py:349`).

---

## H. NEXT 3 CURSOR PROMPTS

### 1. Force v2 default and make MARK taps survive every generate path

**Highest-leverage P0.** Change `get_jobdoc_pipeline()` default to `v2` (or make `run_demo.sh` / local Flask refuse to start without `JOBDOC_PIPELINE=v2`). If v1 remains, pass `mark_events` + `step_events` into `JobDocCrew` / `build_matched_sections` the same way v2 does. Add a Flask-level assertion/log when marks were uploaded but the matcher received zero taps.

### 2. Local demo boot: cookies, port, and a one-page runbook

Set `JOBDOC_COOKIE_INSECURE=1` automatically when binding HTTP localhost; fix `run_demo.sh` + `DEMO_SCRIPT.md` to port **7860**, roofing-only, no “Start quick demo” / People+Room. Confirm login → record → MARK → generate → PDF download on `http://127.0.0.1:7860` without a Secure-cookie trap.

### 3. Client-facing copy: never show `default_text` / capture_instructions as findings

When a section has no narration, print an explicit “not verified / no spoken findings” block and keep `default_text` out of the PDF body. Raise the reconcile overlap threshold so a generic LLM paragraph cannot replace real speech. Keep `{page}` sanitizers; add a generate-time HTML assert.

---

## EXECUTIVE SNAPSHOT (10 lines max, non-engineer readable)

- This folder is the **local GenerSwift-JobDoc-Demo-v1** product tree (not the old GitHub snapshot, not a clean HF-only copy). Hugging Face Docker **is** wired; Modal is leftover.
- The roofing walkthrough → transcript → “Mark this.” / MARK button → still frames → branded PDF path **exists and is well built on pipeline v2**.
- Footer `{page}` leakage has a real sanitizer now. Frames use ffmpeg, not computer vision. Recorder crash-recovery and upload retry are in the phone UI.
- **What will embarrass us:** if the app starts on default **v1**, tapping MARK does nothing on the server — noisy audio produces empty/generic sections and missing photos. The printed demo script still talks about a “Start quick demo” button and People/Room reports that are gone. Local login may fail because cookies are marked Secure on HTTP.
- **Fix first:** default (or hard-require) pipeline **v2** so MARK taps and spoken cues both land in the PDF, then make local HTTP login/cookies work, then rewrite the 12-minute demo script for the roofing-only app on port 7860.

PASTE THIS ENTIRE REPORT BACK INTO THE GROK CHAT.
