"""
models.py — SQLAlchemy models for GenerSwift (clients, capture sessions, misc + report data).

Data model (Phase 2)
--------------------
    Client            — a person/company you do work for (reused across many jobs).
    CaptureSession    — one job/visit for a client: report type, address, inspector, status, PDF.
    SessionSummary    — clean structured data from the main report workflow (JSON).
    MiscData          — supplemental data captured by the always-present Misc step
                        (raw transcript + summarized JSON), queryable later for invoices/proposals.
    JobInfoSummary    — LLM-written client handoff note for a completed session (one per session).
    VideoUpload       — one recorded upload; links back to a CaptureSession.

A single shared ``db`` instance is created here and initialized in ``app.py`` via
``db.init_app(app)`` so models and the Flask app agree on one SQLAlchemy registry.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Client(db.Model):
    """A client (person or company) that capture sessions are performed for."""

    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    company = db.Column(db.String(255), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(64), nullable=True)
    address = db.Column(db.String(500), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    sessions = db.relationship(
        "CaptureSession", back_populates="client", cascade="all, delete-orphan"
    )

    @property
    def display_label(self) -> str:
        return f"{self.name} — {self.company}" if self.company else self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "company": self.company,
            "email": self.email,
            "phone": self.phone,
            "address": self.address,
            "notes": self.notes,
            "label": self.display_label,
            "session_count": len(self.sessions) if self.sessions is not None else 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CaptureSession(db.Model):
    """One capture job/visit for a client (the unit a recording + report belongs to)."""

    __tablename__ = "capture_sessions"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True
    )
    report_type = db.Column(db.String(120), nullable=True)
    job_address = db.Column(db.String(500), nullable=False, default="")
    inspector_name = db.Column(db.String(255), nullable=True)
    inspection_date = db.Column(db.String(32), nullable=True)  # ISO date (YYYY-MM-DD)
    weather = db.Column(db.String(255), nullable=True)
    access_notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(40), nullable=False, default="started", index=True)
    pdf_filename = db.Column(db.String(255), nullable=True)
    started_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    client = db.relationship("Client", back_populates="sessions")
    summaries = db.relationship(
        "SessionSummary", back_populates="capture_session", cascade="all, delete-orphan"
    )
    misc_records = db.relationship(
        "MiscData", back_populates="capture_session", cascade="all, delete-orphan"
    )
    uploads = db.relationship("VideoUpload", back_populates="capture_session")
    # One LLM-written client handoff note per session (created after the report completes).
    job_info_summary = db.relationship(
        "JobInfoSummary",
        back_populates="capture_session",
        cascade="all, delete-orphan",
        uselist=False,
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "client_id": self.client_id,
            "report_type": self.report_type,
            "job_address": self.job_address,
            "inspector_name": self.inspector_name,
            "inspection_date": self.inspection_date,
            "weather": self.weather,
            "access_notes": self.access_notes,
            "status": self.status,
            "pdf_filename": self.pdf_filename,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class SessionSummary(db.Model):
    """Clean, structured data produced by the main report workflow for a capture session."""

    __tablename__ = "session_summaries"

    id = db.Column(db.Integer, primary_key=True)
    capture_session_id = db.Column(
        db.Integer, db.ForeignKey("capture_sessions.id"), nullable=False, index=True
    )
    structured_data_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    capture_session = db.relationship("CaptureSession", back_populates="summaries")

    @property
    def structured_data(self) -> dict[str, Any]:
        if not self.structured_data_json:
            return {}
        try:
            data = json.loads(self.structured_data_json)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    @structured_data.setter
    def structured_data(self, value: dict[str, Any] | None) -> None:
        self.structured_data_json = json.dumps(value or {}, ensure_ascii=False, default=str)


class MiscData(db.Model):
    """Supplemental data from the always-present Misc step (raw transcript + summarized JSON)."""

    __tablename__ = "misc_data"

    id = db.Column(db.Integer, primary_key=True)
    capture_session_id = db.Column(
        db.Integer, db.ForeignKey("capture_sessions.id"), nullable=False, index=True
    )
    raw_transcript = db.Column(db.Text, nullable=True)
    summarized_data_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    capture_session = db.relationship("CaptureSession", back_populates="misc_records")

    @property
    def summarized_data(self) -> dict[str, Any]:
        if not self.summarized_data_json:
            return {}
        try:
            data = json.loads(self.summarized_data_json)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    @summarized_data.setter
    def summarized_data(self, value: dict[str, Any] | None) -> None:
        self.summarized_data_json = json.dumps(value or {}, ensure_ascii=False, default=str)


class JobInfoSummary(db.Model):
    """LLM-written 'Job Info Summary' — a readable client handoff note for a capture session.

    Synthesized from ``SessionSummary.structured_data`` (the main report data) plus all
    ``MiscData`` (raw transcript + summarized supplemental data). One per capture session;
    regeneration overwrites the same row.
    """

    __tablename__ = "job_info_summaries"

    id = db.Column(db.Integer, primary_key=True)
    capture_session_id = db.Column(
        db.Integer, db.ForeignKey("capture_sessions.id"), nullable=False, unique=True, index=True
    )
    summary_text = db.Column(db.Text, nullable=False, default="")
    # Model identifier used to write the summary (traceability).
    model_name = db.Column(db.String(120), nullable=True)
    # Optional cached PDF rendering of the summary.
    pdf_filename = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    capture_session = db.relationship("CaptureSession", back_populates="job_info_summary")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "capture_session_id": self.capture_session_id,
            "summary_text": self.summary_text,
            "model_name": self.model_name,
            "pdf_filename": self.pdf_filename,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class VideoUpload(db.Model):
    """
    Row per recorded upload. ``job_meta`` stores JSON for status polling + crew summary.

    Optionally linked to a ``CaptureSession`` (the new client-aware flow always links one).
    """

    __tablename__ = "video_uploads"

    id = db.Column(db.Integer, primary_key=True)
    stored_filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=True)
    report_types_json = db.Column(db.Text, nullable=False)
    # JSON string: { "status", "detail", "crew_result", "updated_at", ... }
    job_meta = db.Column(db.Text, nullable=True)
    capture_session_id = db.Column(
        db.Integer, db.ForeignKey("capture_sessions.id"), nullable=True, index=True
    )

    capture_session = db.relationship("CaptureSession", back_populates="uploads")
