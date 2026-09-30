"""
Jobs-by-address shell: ``/api/jobs`` upserts, GET filtering, and the /jobs pages.

Runs against a throwaway SQLite file so it never touches ``instance/jobdoc.db``.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "tests" / "fixtures" / "jobs_list.json"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A logged-in-equivalent test client on an empty database."""
    monkeypatch.setenv("JOBDOC_AUTH_DISABLE", "1")
    monkeypatch.setenv("JOBDOC_RETENTION_HOURS", "0")
    monkeypatch.setenv("JOBDOC_DATABASE_URI", f"sqlite:///{tmp_path / 'jobs_test.db'}")

    for mod in ("app", "models"):
        sys.modules.pop(mod, None)
    import app as app_module

    app_module.app.config.update(TESTING=True)
    with app_module.app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()
    yield app_module.app.test_client()
    sys.modules.pop("app", None)
    sys.modules.pop("models", None)


def _fixture_jobs() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_import_creates_placeholder_rows(client) -> None:
    """The sample list creates one placeholder per addressed row and skips the address-less one."""
    body = _fixture_jobs()
    resp = client.post("/api/jobs", json=body)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["created"] == 3
    assert data["skipped"] == 1  # the row with no address

    for job in data["jobs"]:
        assert job["source"] == "json_import"
        assert job["session_kind"] is None
        assert job["report_type"] is None
        assert job["client_id"]  # placeholder client keeps the NOT NULL FK satisfied

    addresses = {j["job_address"] for j in data["jobs"]}
    assert "12 Oak St" in addresses
    # Leading/trailing whitespace is trimmed but inner spelling is preserved for display.
    assert "401   Maple Road" in addresses


def test_upsert_by_address_does_not_duplicate(client) -> None:
    """Re-posting the same address updates the existing placeholder instead of adding one."""
    first = client.post("/api/jobs", json={"jobs": [{"address": "12 Oak St", "status": "new"}]})
    assert first.get_json()["created"] == 1
    original_id = first.get_json()["jobs"][0]["id"]

    # Same job site, different spelling + a status change.
    again = client.post(
        "/api/jobs", json={"jobs": [{"address": "  12   oak st ", "status": "active"}]}
    )
    payload = again.get_json()
    assert payload["created"] == 0
    assert payload["updated"] == 1
    assert payload["jobs"][0]["id"] == original_id
    assert payload["jobs"][0]["status"] == "active"
    assert payload["jobs"][0]["job_address"] == "12   oak st"

    listing = client.get("/api/jobs").get_json()["jobs"]
    assert len(listing) == 1
    assert listing[0]["session_count"] == 1


def test_upsert_by_external_id_wins_over_address(client) -> None:
    """``external_id`` is the strongest key: a moved/corrected address updates the same row."""
    created = client.post(
        "/api/jobs",
        json={"jobs": [{"address": "88 Beech Ave", "external_id": "JD-2001", "status": "new"}]},
    ).get_json()
    row_id = created["jobs"][0]["id"]

    moved = client.post(
        "/api/jobs",
        json={
            "jobs": [
                {
                    "address": "88 Beech Avenue, Unit 4",
                    "external_id": "JD-2001",
                    "inspection_date": "2026-10-05",
                }
            ]
        },
    ).get_json()
    assert moved["updated"] == 1
    assert moved["created"] == 0
    assert moved["jobs"][0]["id"] == row_id
    assert moved["jobs"][0]["job_address"] == "88 Beech Avenue, Unit 4"
    assert moved["jobs"][0]["inspection_date"] == "2026-10-05"


def test_post_without_address_is_rejected(client) -> None:
    """A payload where no row has an address is a 400, not a silent success."""
    resp = client.post("/api/jobs", json={"jobs": [{"status": "new", "external_id": "JD-X"}]})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert client.get("/api/jobs").get_json()["jobs"] == []

    assert client.post("/api/jobs", json={"nope": 1}).status_code == 400


def test_get_filters_by_status_and_groups_by_address(client) -> None:
    client.post("/api/jobs", json=_fixture_jobs())
    client.post("/api/jobs", json={"jobs": [{"address": "5 Pine Ct", "status": "completed"}]})

    default_view = client.get("/api/jobs").get_json()
    assert default_view["days"] == 7
    open_addresses = {j["address"] for j in default_view["jobs"]}
    assert "5 Pine Ct" not in open_addresses  # completed is hidden by default
    assert "12 Oak St" in open_addresses

    everything = {j["address"] for j in client.get("/api/jobs?status=all").get_json()["jobs"]}
    assert "5 Pine Ct" in everything

    only_active = client.get("/api/jobs?status=active").get_json()["jobs"]
    assert {j["address"] for j in only_active} == {"401   Maple Road"}

    # ``days`` is clamped to the documented 14-day ceiling.
    assert client.get("/api/jobs?days=99").get_json()["days"] == 14
    assert client.get("/api/jobs?days=abc").get_json()["days"] == 7


def test_capture_sessions_group_under_their_job_address(client) -> None:
    """Sessions with the same normalized address are one job with one session list."""
    client.post("/api/jobs", json={"jobs": [{"address": "12 Oak St", "status": "new"}]})
    with client.application.app_context():
        from models import CaptureSession, db

        placeholder = CaptureSession.query.first()
        for kind in ("capture_1", "capture_2"):
            db.session.add(
                CaptureSession(
                    client_id=placeholder.client_id,
                    job_address="12  OAK ST",
                    session_kind=kind,
                    status="recording",
                )
            )
        db.session.commit()

    jobs = client.get("/api/jobs").get_json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["session_count"] == 3
    assert jobs[0]["session_kinds"] == ["capture_1", "capture_2"]


def test_jobs_pages_render(client) -> None:
    client.post("/api/jobs", json={"jobs": [{"address": "12 Oak St", "status": "new"}]})

    listing = client.get("/jobs")
    assert listing.status_code == 200
    assert b"12 Oak St" in listing.data

    detail = client.get("/jobs/12 Oak St")
    assert detail.status_code == 200
    assert b"Capture 1" in detail.data
    assert b"Capture 2" in detail.data
    # The launcher must carry the address through to the existing /start_session route.
    assert b'name="job_address" value="12 Oak St"' in detail.data
    assert b'name="session_kind" value="capture_1"' in detail.data

    # A different spelling of the same address resolves to the same job.
    assert client.get("/jobs/12%20%20oak%20st").status_code == 200


def test_jobs_create_form_makes_a_placeholder(client) -> None:
    resp = client.post(
        "/jobs/create",
        data={"address": "77 Cedar Way", "external_id": "JD-77"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    jobs = client.get("/api/jobs").get_json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["address"] == "77 Cedar Way"
    assert jobs[0]["external_id"] == "JD-77"
    assert jobs[0]["sessions"][0]["source"] == "manual"
    assert jobs[0]["sessions"][0]["session_kind"] is None

    # Address is required.
    client.post("/jobs/create", data={"address": "   "}, follow_redirects=True)
    assert len(client.get("/api/jobs").get_json()["jobs"]) == 1


def test_missing_sqlite_columns_are_added_in_place(client, tmp_path) -> None:
    """``_ensure_sqlite_schema`` backfills the JobDoc columns on a pre-existing DB."""
    import app as app_module
    from sqlalchemy import inspect, text

    with app_module.app.app_context():
        with app_module.db.engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_capture_sessions_jobdoc_external_id"))
            for col in ("jobdoc_external_id", "source", "session_kind"):
                conn.execute(text(f"ALTER TABLE capture_sessions DROP COLUMN {col}"))
        cols = {c["name"] for c in inspect(app_module.db.engine).get_columns("capture_sessions")}
        assert "session_kind" not in cols

        app_module._ensure_sqlite_schema()
        cols = {c["name"] for c in inspect(app_module.db.engine).get_columns("capture_sessions")}
        assert {"jobdoc_external_id", "source", "session_kind"} <= cols


def test_export_zip_bundles_pdf_and_stills(client) -> None:
    """A session with a PDF (and stills) downloads as one zip; an empty session is a 404."""
    import zipfile

    import app as app_module
    from models import CaptureSession, db

    client.post("/api/jobs", json={"jobs": [{"address": "12 Oak St", "status": "new"}]})
    with client.application.app_context():
        placeholder = CaptureSession.query.first()
        capture = CaptureSession(
            client_id=placeholder.client_id,
            job_address="12 Oak St",
            session_kind="capture_1",
            status="completed",
            pdf_filename="pack_test_report.pdf",
        )
        db.session.add(capture)
        db.session.commit()
        upload = app_module.VideoUpload(
            stored_filename="pack_test.mp4",
            report_types_json="[]",
            capture_session_id=capture.id,
        )
        db.session.add(upload)
        db.session.commit()
        session_id, upload_id = capture.id, upload.id
        empty_id = placeholder.id

    out = app_module.OUTPUT_DIR
    (out / "pack_test_report.pdf").write_bytes(b"%PDF-1.4 pack test")
    frames_dir = out / "frames" / str(upload_id)
    frames_dir.mkdir(parents=True, exist_ok=True)
    (frames_dir / "overview_1.jpg").write_bytes(b"jpeg-bytes")
    try:
        resp = client.get(f"/jobs/session/{session_id}/export.zip")
        assert resp.status_code == 200
        assert resp.mimetype == "application/zip"
        assert "12_Oak_St_capture_1.zip" in resp.headers["Content-Disposition"]
        names = zipfile.ZipFile(io.BytesIO(resp.data)).namelist()
        assert "pack_test_report.pdf" in names
        assert "stills/overview_1.jpg" in names

        # A placeholder job record has neither a PDF nor stills.
        assert client.get(f"/jobs/session/{empty_id}/export.zip").status_code == 404

        detail = client.get("/jobs/12 Oak St")
        assert b"Download pack" in detail.data
    finally:
        (out / "pack_test_report.pdf").unlink(missing_ok=True)
        (frames_dir / "overview_1.jpg").unlink(missing_ok=True)


def test_roofing_template_declares_the_still_window() -> None:
    """Every roofing section carries the template-owned still window."""
    from utils.template_loader import load_report_template

    template = load_report_template(ROOT / "reports" / "roofing_realty_inspection.json")
    assert template.content_structure and template.content_structure.sections
    for section in template.content_structure.sections:
        assert section.image_placement.still_before_sec == 5.0
        assert section.image_placement.still_after_sec == 10.0


def test_section_still_window_beats_the_env_fallback(monkeypatch) -> None:
    """A section's image_placement wins; sections without one fall back to the env global."""
    from crew.structured_pipeline import _frame_context_window
    from utils.template_schema import ContentSection, ImagePlacement

    monkeypatch.setenv("JOBDOC_FRAME_CONTEXT_BEFORE_SEC", "1")
    monkeypatch.setenv("JOBDOC_FRAME_CONTEXT_AFTER_SEC", "2")
    assert _frame_context_window(None) == (1.0, 2.0)

    section = ContentSection(
        section_id="overview",
        title="Overview",
        image_placement=ImagePlacement(still_before_sec=7.5, still_after_sec=12.0),
    )
    assert _frame_context_window(section) == (7.5, 12.0)


def test_test_database_is_isolated(client) -> None:
    """Guard: the fixture must not have pointed the app at the real instance DB."""
    import app as app_module

    assert "jobs_test.db" in app_module.app.config["SQLALCHEMY_DATABASE_URI"]
