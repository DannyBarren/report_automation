"""
jobdoc_push.py — Optional hand-off of a finished export pack to a receiving system.

Entirely opt-in: without ``JOBDOC_RECEIVE_URL`` this module logs and returns a skipped result,
so nothing in the request path changes for an operator who never configures it. The POST is a
plain stdlib multipart upload (no new dependency) and **never raises** into a Flask handler —
a failed hand-off is reported in the return value, never as a 500 on a download the user asked
for.

Fields sent alongside the ``pack`` file: ``address``, ``external_id``, ``session_id``,
``session_kind``.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["receive_url", "push_enabled", "push_session_pack"]

PUSH_TIMEOUT_SECONDS = 30


def receive_url() -> str:
    return (os.environ.get("JOBDOC_RECEIVE_URL") or "").strip()


def push_enabled() -> bool:
    """True when a receiving endpoint is configured (used to hide the UI button)."""
    return bool(receive_url())


def _multipart_body(
    fields: dict[str, str], *, file_field: str, file_path: Path
) -> tuple[bytes, str]:
    """Encode ``fields`` plus one file as multipart/form-data. Returns ``(body, content_type)``."""
    boundary = f"----jobdoc{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{file_path.name}\"\r\n"
        f"Content-Type: application/zip\r\n\r\n".encode()
    )
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def push_session_pack(session_id: int, zip_path: str | Path | None = None) -> dict:
    """POST one session's export pack to ``JOBDOC_RECEIVE_URL``.

    Returns a result dict — ``{"ok": False, "skipped": True}`` when no receiver is configured,
    ``{"ok": True, "status": <code>}`` on success, or ``{"ok": False, "error": ...}`` on any
    failure. Never raises.
    """
    url = receive_url()
    if not url:
        logger.info(
            "[jobdoc] push skipped for session %s — JOBDOC_RECEIVE_URL is not set.", session_id
        )
        return {"ok": False, "skipped": True, "reason": "JOBDOC_RECEIVE_URL not set"}

    try:
        from models import CaptureSession, db

        capture = db.session.get(CaptureSession, session_id)
        if capture is None:
            return {"ok": False, "skipped": False, "error": f"session {session_id} not found"}

        path = Path(zip_path) if zip_path else None
        if path is None or not path.is_file():
            return {"ok": False, "skipped": False, "error": "no export pack to push"}

        body, content_type = _multipart_body(
            {
                "address": capture.job_address or "",
                "external_id": capture.jobdoc_external_id or "",
                "session_id": str(capture.id),
                "session_kind": capture.session_kind or "",
            },
            file_field="pack",
            file_path=path,
        )
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": content_type}, method="POST"
        )
        with urllib.request.urlopen(request, timeout=PUSH_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None) or response.getcode()
        logger.info("[jobdoc] pushed session %s pack → %s (HTTP %s)", session_id, url, status)
        return {"ok": 200 <= int(status) < 300, "skipped": False, "status": int(status)}
    except urllib.error.URLError as exc:
        logger.warning("[jobdoc] push failed for session %s: %s", session_id, exc)
        return {"ok": False, "skipped": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — a hand-off must never break the request
        logger.warning("[jobdoc] push failed for session %s: %s", session_id, exc)
        return {"ok": False, "skipped": False, "error": str(exc)}
