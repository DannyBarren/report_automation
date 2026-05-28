# AGENTS.md

## Cursor Cloud specific instructions

### Overview

**jobdoc-demo** (GenerSwift) is a single-service Python Flask monolith that converts guided video field recordings into structured PDF reports using Deepgram (ASR) and Anthropic Claude (CrewAI reasoning). SQLite is the database (auto-created at `instance/jobdoc.db`).

### Running the app

```bash
source .venv/bin/activate
python app.py
```

The app runs on `http://0.0.0.0:5000`. See `README.md` for route documentation.

### Key endpoints

- `/` — Home page, report template selection
- `/health` — Readiness probe (API keys + WeasyPrint status)
- `/demo` — Quick demo with People + Room reports pre-selected
- `/api/guidance` — JSON guidance plan for selected report types

### External API keys

Full pipeline (transcription + AI report generation + PDF) requires `DEEPGRAM_API_KEY` and `ANTHROPIC_API_KEY` set in a `.env` file at the project root. Without these keys, the app starts and serves the UI, but `/generate_report` returns 400.

### System dependencies

WeasyPrint requires GTK3 libraries on Linux: `libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0`. ffmpeg is needed for video/audio processing.

### Notes

- No test suite exists in this repo (no pytest, unittest, or similar).
- No linting configuration exists (no flake8, ruff, pyproject.toml, etc.).
- The venv must be at `.venv` in the project root (used by `run_demo.sh`).
- Flask debug mode is on by default (`FLASK_DEBUG=1`). Hot reload works for Python file changes.
- `python3.12-venv` apt package is required to create the virtualenv on Ubuntu 24.04.
