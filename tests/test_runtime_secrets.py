"""Boot-time secret loading: fail closed unless JOBDOC_DEV=1."""

from __future__ import annotations

from pathlib import Path

import pytest

from utils.runtime_secrets import load_runtime_secrets

pytestmark = pytest.mark.auth

APP_PY = Path(__file__).resolve().parent.parent / "app.py"


def test_missing_secret_key_refuses_start() -> None:
    with pytest.raises(SystemExit, match="SECRET_KEY is not set"):
        load_runtime_secrets(
            {
                "JOBDOC_AUTH_USER": "ci-user",
                "JOBDOC_AUTH_PASSWORD": "ci-password",
            }
        )


def test_missing_auth_credentials_refuse_start() -> None:
    with pytest.raises(SystemExit, match="JOBDOC_AUTH_USER and JOBDOC_AUTH_PASSWORD"):
        load_runtime_secrets({"SECRET_KEY": "unit-test-secret"})


def test_partial_auth_credentials_refuse_start() -> None:
    with pytest.raises(SystemExit, match="JOBDOC_AUTH_USER and JOBDOC_AUTH_PASSWORD"):
        load_runtime_secrets(
            {
                "SECRET_KEY": "unit-test-secret",
                "JOBDOC_AUTH_USER": "ci-user",
            }
        )


def test_auth_disable_allows_missing_credentials() -> None:
    loaded = load_runtime_secrets(
        {
            "SECRET_KEY": "unit-test-secret",
            "JOBDOC_AUTH_DISABLE": "1",
        }
    )
    assert loaded.auth_enabled is False
    assert loaded.secret_key == "unit-test-secret"
    assert loaded.ephemeral_secret_key is False


def test_full_credentials_enable_login_gate() -> None:
    loaded = load_runtime_secrets(
        {
            "SECRET_KEY": "unit-test-secret",
            "JOBDOC_AUTH_USER": "ci-user",
            "JOBDOC_AUTH_PASSWORD": "ci-password",
        }
    )
    assert loaded.auth_enabled is True
    assert loaded.auth_user == "ci-user"
    assert loaded.auth_password == "ci-password"
    assert loaded.dev_mode is False


def test_dev_flag_allows_missing_env() -> None:
    loaded = load_runtime_secrets({"JOBDOC_DEV": "1"})
    assert loaded.dev_mode is True
    assert loaded.auth_enabled is False
    assert loaded.ephemeral_secret_key is True
    assert len(loaded.secret_key) >= 32


def test_dev_flag_keeps_login_when_credentials_set() -> None:
    loaded = load_runtime_secrets(
        {
            "JOBDOC_DEV": "1",
            "SECRET_KEY": "dev-secret",
            "JOBDOC_AUTH_USER": "dev-user",
            "JOBDOC_AUTH_PASSWORD": "dev-password",
        }
    )
    assert loaded.auth_enabled is True
    assert loaded.ephemeral_secret_key is False
    assert loaded.secret_key == "dev-secret"


def test_app_py_has_no_insecure_secret_key_default() -> None:
    src = APP_PY.read_text(encoding="utf-8")
    assert "***REMOVED***" not in src
    assert "load_runtime_secrets" in src
