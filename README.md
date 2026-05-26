# jobdoc-demo

AI-guided **video field reports**: operators record with the phrase **“Mark this.”**, upload video, and receive a structured **PDF** with still frames and narratives.

Report definitions live in **`reports/*.json`**. This application runs **only against real APIs**: Deepgram Nova-3 for transcription and Anthropic Claude for CrewAI reasoning. **Missing keys cause fast failures** with explicit messages.

---

## Windows — start here (one command)

**This is the primary path for field testing on Windows 10/11.**

### What you need first

1. **Python 3.10+** from [python.org](https://www.python.org/downloads/) — check **“Add python.exe to PATH”** during install.
2. Optional: **ffmpeg** on `PATH` (still frames / audio).
3. Copy **`.env`** and set **`DEEPGRAM_API_KEY`** and **`ANTHROPIC_API_KEY`**.

### One-click launch

1. Open the project folder in File Explorer.
2. Double-click **`run_demo.bat`** (normal double-click is fine for daily use).
3. Wait for the black window to show **`[4/4] Starting JobDoc`**.
4. Open **http://127.0.0.1:5000** in your browser.

### First-time PDF setup (GTK3, ~49 MB, one-time)

WeasyPrint needs **GTK3** DLLs (Pango, Cairo). `run_demo.bat` handles this automatically:

| Step | What happens |
|------|----------------|
| **Portable (default)** | Downloads the official GTK package and **extracts** it into `gtk3/runtime` — **no administrator** if 7-Zip or `py7zr` works. |
| **If that fails** | **Right-click `run_demo.bat` → Run as administrator** and run again (silent install, one-time). |
| **In the browser** | Open **http://127.0.0.1:5000/setup/weasyprint** — click **Fix dependencies now**. |

After GTK is installed once, **normal double-click** of `run_demo.bat` is enough forever — env vars are set each launch.

**Screenshot checklist (for your own docs):**

1. File Explorer → right-click `run_demo.bat` → **Run as administrator** (UAC prompt).
2. Command window → line **`[OK] WeasyPrint is ready`** at step `[3/4]`.
3. Browser → home page without the yellow “PDF setup” banner.

### Troubleshooting (Windows)

| Symptom | Fix |
|---------|-----|
| **“The requested operation requires elevation”** | Do **not** rely on silent install from a normal prompt. Use **Option A**: right-click **`run_demo.bat` → Run as administrator**, or use **Option B**: install [7-Zip](https://www.7-zip.org/) and run `run_demo.bat` again (portable extract). |
| **`libpangoft2-1.0-0` / DLL load failed** | Run **`run_demo.bat`** again, or visit **`/setup/weasyprint`**, or **`/setup/weasyprint/required`**. |
| **PDF button fails in the app** | Click **Fix dependencies** → restart **`run_demo.bat`** → **Retry check** on the setup page. |
| **Auto download fails** | Firewall/proxy; manual [GTK release](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases/tag/2022-01-04). |
| **Project path has spaces** | Prefer `C:\JobDoc\jobdoc-demo` if extract/install fails. |
| **`/health` → `weasyprint.ready: false`** | In `.venv`: `python -m utils.setup_weasyprint --ensure-gtk` |
| **Python not found** | Reinstall Python with **Add to PATH**; reopen terminal. |

---

## macOS / Linux quick start

```bash
cd jobdoc-demo
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**WeasyPrint system libraries:**

| OS | Install |
|----|---------|
| **macOS** | `brew install pango gdk-pixbuf libffi` |
| **Debian/Ubuntu** | `sudo apt install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0` |

```bash
python app.py
```

Or use **`./run_demo.sh`** (venv + pip; does not install GTK on Unix).

---

## Configuration

| Keys / env | Purpose |
|------------|---------|
| **`DEEPGRAM_API_KEY`** | Required ASR — [Deepgram Console](https://console.deepgram.com/). |
| **`ANTHROPIC_API_KEY`** | Required Crew reasoning. |
| **`JOBDOC_PIPELINE`** | `v1` (default) or `v2` (narration-only multi-stage). |
| **`JOBDOC_AUTO_INSTALL_GTK`** | `1` — allow download/setup (set by `run_demo.bat`). |
| **`JOBDOC_PREFER_PORTABLE_GTK`** | `1` (default) — extract DLLs without admin. |
| **`JOBDOC_ALLOW_GTK_INSTALLER`** | `1` — allow silent `.exe` install (admin). |
| **`JOBDOC_CREW_TIMEOUT_SEC`** | Default `480` |
| **`JOBDOC_TRANSCRIPTION_TIMEOUT_SEC`** | Default `180` |
| **`JOBDOC_LOG`** | `debug` for verbose pipeline logs |

**Health check:** **http://127.0.0.1:5000/health** — API keys + WeasyPrint/GTK readiness.

---

## Routes (short)

| Route | Role |
|-------|------|
| **`/`** | Choose reports or **Start quick demo**. |
| **`/health`** | Keys + WeasyPrint/GTK status. |
| **`/setup/weasyprint`** | Step-by-step PDF library fix + **Fix dependencies now**. |
| **`/setup/weasyprint/required`** | Friendly “PDF unavailable” page with big fix button. |
| **`/upload_video`** | POST multipart recording. |
| **`/report_preview/<id>`** | Progress, video, frames, PDF link. |
| **`/generate_report`** | `pipeline_stage`: `analysis` \| `pdf` \| `full` (v2). |

### v2 field workflow

1. Record guided video → upload (failed uploads saved in IndexedDB on device).
2. **Analyze recording** → draft + review flags.
3. **Review** → inspector notes per section.
4. **Generate PDF** → merges notes, frames, PDF.

---

## Failure modes (app behavior)

- **Missing API keys** — `generate_report` returns **400** with setup instructions.
- **Missing video file** — hard fail.
- **Quiet / empty audio** — friendly message; template-based draft (v2).
- **Crew / ASR issues** — degraded draft; see `pipeline_notes`.
- **WeasyPrint missing** — **503** on PDF step with `fix_url` → **`/setup/weasyprint/required`**; recording still works.
- **Frame extraction** — skips failed stills; PDF continues.

---

## Project layout

| Path | Role |
|------|------|
| `run_demo.bat` | **Windows one-command launcher** (venv, pip, GTK, app). |
| `utils/setup_weasyprint.py` | Portable GTK extract, `WEASYPRINT_DLL_DIRECTORIES`, health probe. |
| `utils/setup_utils.py` | Re-exports `setup_weasyprint` (backward compatible). |
| `gtk3/` | Local GTK runtime (gitignored; created by setup). |
| `reports/` | Report templates v2. |
| `crew/` | v1/v2 pipelines. |
| `utils/` | Transcription, video, PDF, schemas. |

---

## License

Internal / demo use unless you add a license file.
