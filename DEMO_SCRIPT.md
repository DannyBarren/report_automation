# Client demo script — jobdoc-demo

Use this in a **conference room** with a laptop (projector) and optionally a **phone** on the same Wi‑Fi. Target length: **12–18 minutes** including Q&A.

---

## 0. Before the client walks in (2 min)

1. On the laptop, start the app (`run_demo.bat` / `run_demo.sh` or `python app.py`).
2. Open a browser: `http://127.0.0.1:5000/health` — confirm `"ok": true` and which API keys show as configured (booleans only, no secrets).
3. Open the home page. Click **Start quick demo** once in a private tab to confirm you land on the **recording** screen (People + Room pre-selected).
4. Optional phone check: use your **LAN IP** (e.g. `http://192.168.1.50:5000`) or an **ngrok** HTTPS URL if mobile browsers require it.

**If something fails:** this build requires **live APIs** (Deepgram Nova-3 + Anthropic Claude Opus 4.6). Fix keys, billing, and network, then retry.

---

## 1. Hook (1–2 min) — what to say

**Say:**

> “Today we’re going to capture a short field-style video once, and turn it into a **structured PDF** — sections, timestamps, still frames, and narrative text — without anyone retyping notes.”

**Show:** Home page with **How this demo works** and the **Start quick demo** button.

**Key point:**

> “Every section you see in the app comes from **JSON files** in `reports/` — one source of truth for the UI, the AI, and the printed report.”

---

## 2. Start the guided recorder (1 min) — what to do

1. Click **Start quick demo** (or select templates manually if you need a custom mix).
2. **Say:** “This screen is the operator’s script — the same **section order** we’ll use in the PDF.”

**Key point:**

> “We use a single spoken phrase, **‘Mark this.’**, to pin a **timestamp** in the recording. The system lines those moments up with the **template sections** in order.”

---

## 3. Record the video (4–7 min) — exact actions

Work through the checklist **top to bottom**. For **each** section:

1. **Hold the camera steady** on the subject or area.
2. Say clearly: **“Mark this.”** (pause a beat).
3. **Then** describe what you are showing in **1–2 sentences** (name, room, condition, PPE, hazard, etc.).
4. **Stop talking** for a second before moving the camera to the next shot.

**Example lines (People report):**

- *“**Mark this.** I’m on a head-and-shoulders shot. This is Alex Chen, operations lead on site today.”*
- *“**Mark this.** Here’s the primary work area — dual monitors, no loose cables in the walk path.”*
- *“**Mark this.** PPE — high-vis vest and safety glasses on the desk, not worn; flag for supervisor review.”*

**Example lines (Room report / site):**

- *“**Mark this.** Wide establishing shot of the west corridor — egress signage visible.”*
- *“**Mark this.** Floor condition — dry, no pooling near the exit door.”*

**What the client sees:** Live camera + checklist mirroring the PDF sections.

---

## 4. Upload and preview (1 min)

1. **Stop** recording and **upload**.
2. Land on **Report preview** — **embedded original video** appears immediately.

**Say:**

> “Same browser URL we can share — everything for this job hangs off a **video ID**.”

---

## 5. Generate and narrate the pipeline (3–5 min)

1. Click **Generate report**.
2. **Talk through the pipeline row** as it advances (audio → transcription → matching → … → PDF).

**What the client sees:**

- Progress bar and **step descriptions**.
- Optional **Notes** if cues were missing or ASR fell back (trust-building).

**How the AI works — short explanations you can use:**

| Topic | What to say |
|--------|----------------|
| **Templates** | “Sections and fields are defined in JSON — not hard-coded — so you can ship new report types without redeploying logic.” |
| **Speech-to-text** | “We transcribe with timestamps using **Deepgram Nova-3** with a configured API key.” |
| **“Mark this.”** | “We align spoken cues to sections **in order**. Each cue gets a **time anchor** and a **still frame** near that speech.” |
| **Agents** | “CrewAI runs a sequential workflow for transparency in workshops — the **structured output** you care about is validated in Python before PDF export.” |
| **PDF** | “We render HTML per report type and print with **WeasyPrint** — good enough for a customer-ready artifact today.” |

---

## 6. Still frames + timeline + PDF (2–3 min)

After completion:

1. Scroll **Still frames** — one image per section story.
2. Show **“Mark this.” timeline** under the video — **tap a chip** to seek the recording to that cue (shows alignment between speech time and section).

**Say:**

> “These times come from the **matched sections** — same clock the PDF used for each still.”

3. Click **Download PDF** — open the PDF on screen if possible.

**Say:**

> “Same narrative appears beside each image — ready for email or archival.”

---

## 7. Close + Q&A (2–5 min)

**Say:**

> “If someone skips a cue, Python still produces section rows from deterministic template logic — check **pipeline notes** on the preview for gaps vs the transcript.”

**Invite questions:** integrations (CRM), branding, auth, retention, on-prem vs cloud ASR.

---

## Quick recovery lines

- **ASR/LLM errors:** “We need working API keys and network — see `/health` and the README for required env vars.”
- **PDF error on Windows:** “WeasyPrint may need extra system libraries; the app can write an HTML file under `reports_output/` for debugging per the README.”
