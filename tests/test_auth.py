"""Login gate: unauthenticated requests are blocked; public probes stay open."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from flask import Flask, session

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.auth_gate import check_credentials, enforce_login_request, request_wants_json
from utils.runtime_secrets import load_runtime_secrets

pytestmark = pytest.mark.auth

USER = "pytest-user"
PASSWORD = "pytest-password"
SECRET = "pytest-secret-key"


def _gated_app() -> Flask:
    runtime = load_runtime_secrets(
        {
            "SECRET_KEY": SECRET,
            "JOBDOC_AUTH_USER": USER,
            "JOBDOC_AUTH_PASSWORD": PASSWORD,
        }
    )
    app = Flask(__name__)
    app.config["SECRET_KEY"] = runtime.secret_key
    app.config["TESTING"] = True
    app.config["SESSION_COOKIE_SECURE"] = False

    @app.before_request
    def _enforce_login():
        return enforce_login_request(
            auth_enabled=runtime.auth_enabled,
            wants_json=request_wants_json(),
        )

    @app.route("/")
    def index():
        return "home-ok"

    @app.route("/health")
    def health():
        return "health-ok"

    @app.route("/login", methods=["GET", "POST"])
    def login():
        from flask import redirect, request, url_for

        if not runtime.auth_enabled or session.get("authed"):
            return redirect(url_for("index"))
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            if check_credentials(username, password, runtime.auth_user, runtime.auth_password):
                session["authed"] = True
                session.permanent = True
                return redirect(url_for("index"))
            return "Incorrect username or password.", 401
        return "login-form", 200

    @app.route("/logout")
    def logout():
        from flask import redirect, url_for

        session.pop("authed", None)
        return redirect(url_for("login"))

    return app


@pytest.fixture()
def client():
    return _gated_app().test_client()


def test_login_required_for_home(client) -> None:
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_json_caller_gets_401(client) -> None:
    resp = client.get("/", headers={"Accept": "application/json"})
    assert resp.status_code == 401
    payload = resp.get_json()
    assert payload["ok"] is False
    assert "login_url" in payload


def test_health_is_public(client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "health-ok"


def test_login_rejects_wrong_password(client) -> None:
    resp = client.post(
        "/login",
        data={"username": USER, "password": "not-the-password"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    follow = client.get("/", follow_redirects=False)
    assert follow.status_code == 302


def test_login_success_reaches_home(client) -> None:
    resp = client.post(
        "/login",
        data={"username": USER, "password": PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    home = client.get("/")
    assert home.status_code == 200
    assert home.get_data(as_text=True) == "home-ok"


def test_empty_expected_credentials_never_match() -> None:
    assert check_credentials("anyone", "anything", "", "x") is False
    assert check_credentials("anyone", "anything", "x", "") is False
    assert check_credentials(USER, PASSWORD, USER, PASSWORD) is True
