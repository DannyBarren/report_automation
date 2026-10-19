"""Fail-closed loading of Flask SECRET_KEY and shared-login credentials.

Production (the default) refuses to start unless ``SECRET_KEY`` is set, and unless
``JOBDOC_AUTH_USER`` + ``JOBDOC_AUTH_PASSWORD`` are set when the login gate is on.

``JOBDOC_DEV=1`` is an explicit local-dev escape hatch: an ephemeral SECRET_KEY may be
generated, and missing login credentials turn the gate off. Never set ``JOBDOC_DEV`` in
production.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from typing import Mapping

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})

DEV_FLAG = "JOBDOC_DEV"

_SECRET_KEY_ERROR = """\
Refusing to start: SECRET_KEY is not set.

Set SECRET_KEY to a long random value before launching this app:
  python -c "import secrets; print(secrets.token_hex(32))"

Copy .env.example to .env and fill it in, or export the variable in the process
environment. On Hugging Face Spaces or Modal, store SECRET_KEY as a secret.

Local development only: export JOBDOC_DEV=1 to allow an ephemeral process-local
SECRET_KEY. Never set JOBDOC_DEV in production.
"""

_AUTH_ERROR = """\
Refusing to start: JOBDOC_AUTH_USER and JOBDOC_AUTH_PASSWORD must both be set
when the login gate is enabled.

Set both variables (see .env.example). To disable the login gate on a trusted
private deploy, set JOBDOC_AUTH_DISABLE=1.

Local development only: export JOBDOC_DEV=1 to start without shared-login
credentials (the login gate is then off unless both variables are provided).
Never set JOBDOC_DEV in production.
"""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def is_dev_mode(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return _truthy(env.get(DEV_FLAG))


def is_auth_disabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return _truthy(env.get("JOBDOC_AUTH_DISABLE"))


@dataclass(frozen=True)
class RuntimeSecrets:
    secret_key: str
    auth_user: str
    auth_password: str
    auth_enabled: bool
    dev_mode: bool
    ephemeral_secret_key: bool


def load_runtime_secrets(environ: Mapping[str, str] | None = None) -> RuntimeSecrets:
    """Load SECRET_KEY and shared-login credentials, or refuse to start.

    Pass ``environ`` in tests. Production callers omit it and read ``os.environ``.
    """
    env = os.environ if environ is None else environ
    dev_mode = is_dev_mode(env)
    auth_disabled_flag = is_auth_disabled(env)

    secret_key = (env.get("SECRET_KEY") or "").strip()
    ephemeral = False
    if not secret_key:
        if not dev_mode:
            raise SystemExit(_SECRET_KEY_ERROR)
        secret_key = secrets.token_hex(32)
        ephemeral = True
        logger.warning(
            "JOBDOC_DEV=1: using an ephemeral SECRET_KEY; sessions will not survive restart."
        )

    auth_user = (env.get("JOBDOC_AUTH_USER") or "").strip()
    auth_password = (env.get("JOBDOC_AUTH_PASSWORD") or "").strip()
    have_creds = bool(auth_user and auth_password)

    if auth_disabled_flag:
        auth_enabled = False
    elif have_creds:
        auth_enabled = True
    elif dev_mode:
        auth_enabled = False
        logger.warning(
            "JOBDOC_DEV=1: JOBDOC_AUTH_USER / JOBDOC_AUTH_PASSWORD unset; login gate is off."
        )
    else:
        raise SystemExit(_AUTH_ERROR)

    return RuntimeSecrets(
        secret_key=secret_key,
        auth_user=auth_user,
        auth_password=auth_password,
        auth_enabled=auth_enabled,
        dev_mode=dev_mode,
        ephemeral_secret_key=ephemeral,
    )
