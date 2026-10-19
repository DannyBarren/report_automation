"""Shared-credential login gate used by the Flask app.

This is basic access control for a hosted demo (one username/password from the
environment), not a user-management system. Credential comparison is constant-time.
"""

from __future__ import annotations

import hmac
from typing import Any

from flask import jsonify, redirect, request, session, url_for

# Endpoints reachable without a session (login itself, static assets, PWA files, and the
# non-sensitive monitoring probes so uptime checks keep working behind the gate).
PUBLIC_ENDPOINTS = frozenset(
    {
        "login",
        "logout",
        "static",
        "health",
        "metrics",
        "service_worker",
        "web_manifest",
    }
)


def check_credentials(
    username: str,
    password: str,
    expected_user: str,
    expected_password: str,
) -> bool:
    """Constant-time credential check. Empty expected values never match."""
    if not expected_user or not expected_password:
        return False
    user_ok = hmac.compare_digest(username or "", expected_user)
    pass_ok = hmac.compare_digest(password or "", expected_password)
    return user_ok and pass_ok


def request_wants_json() -> bool:
    """True when the caller is the SPA recorder/JS or explicitly wants JSON."""
    return (
        request.headers.get("X-JobDoc-Client") == "1"
        or request.is_json
        or request.path.startswith("/api/")
        or "application/json" in (request.headers.get("Accept", ""))
    )


def enforce_login_request(*, auth_enabled: bool, wants_json: bool) -> Any:
    """Redirect unauthenticated users to login (JSON 401 for API/SPA callers).

    Returns ``None`` when the request may proceed.
    """
    if not auth_enabled:
        return None
    if (request.endpoint or "") in PUBLIC_ENDPOINTS:
        return None
    if session.get("authed"):
        return None
    if wants_json:
        return jsonify(
            {
                "ok": False,
                "error": "Please sign in to continue.",
                "login_url": url_for("login"),
            }
        ), 401
    next_path = request.full_path if request.query_string else request.path
    return redirect(url_for("login", next=next_path))
