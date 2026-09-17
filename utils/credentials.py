"""
credentials.py — Per-session API key handling for the public demo.

On Hugging Face Spaces, users paste their own Deepgram + OpenAI keys in the UI. We
keep secrets **server-side**, keyed by a random per-browser token stored in the signed
session cookie (the keys themselves are never placed in the cookie). Space owners can
alternatively set ``DEEPGRAM_API_KEY`` / ``OPENAI_API_KEY`` as Space secrets, in which
case the UI is optional.

At pipeline entry we apply the session's keys to ``os.environ`` for the duration of the
request, because downstream SDKs (litellm/CrewAI/OpenAI for reasoning, Deepgram REST) read
keys from the environment. Space-secret env values take precedence when present; otherwise
the per-session keys are used.
"""

from __future__ import annotations

import os
import re
import threading

KEY_NAMES = ("DEEPGRAM_API_KEY", "OPENAI_API_KEY")

# Accepted environment-variable spellings for each canonical key. Modal Secrets (and .env
# files) are hand-entered, so the OpenAI key is frequently stored as ``OPEN_AI_API_KEY`` or
# ``OPENAI_KEY`` instead of the canonical ``OPENAI_API_KEY`` the SDKs read. We normalize any
# recognized alias into the canonical name so the pipeline finds the key regardless of the
# exact spelling used when creating the secret. First non-empty alias wins.
KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "OPENAI_API_KEY": (
        "OPENAI_API_KEY",
        "OPEN_AI_API_KEY",
        "OPENAI_KEY",
        "OPENAI_APIKEY",
        "OPENAI_SECRET_KEY",
        "OPENAI_TOKEN",
    ),
    "DEEPGRAM_API_KEY": (
        "DEEPGRAM_API_KEY",
        "DEEPGRAM_KEY",
        "DEEPGRAM_APIKEY",
        "DG_API_KEY",
    ),
}

# "Squashed" (letters+digits only, uppercased) forms that should be treated as each canonical
# key. This catches names the exact-alias list misses: trailing/leading spaces in the secret
# KEY name (``"OPENAI_API_KEY "``), hyphens (``OPENAI-API-KEY``), lowercase, or odd
# underscore placement (``OPEN_AI_API_KEY``) — the exact class of typo that survives a
# delete-and-recreate of the Modal Secret.
KEY_FUZZY_FORMS: dict[str, frozenset[str]] = {
    "OPENAI_API_KEY": frozenset(
        {"OPENAIAPIKEY", "OPENAIKEY", "OPENAIAPI", "OPENAITOKEN", "OPENAISECRETKEY", "OPENAIACCESSKEY"}
    ),
    "DEEPGRAM_API_KEY": frozenset(
        {"DEEPGRAMAPIKEY", "DEEPGRAMKEY", "DEEPGRAMTOKEN", "DGAPIKEY", "DEEPGRAMSECRETKEY"}
    ),
}

_lock = threading.Lock()
# session_token -> {"DEEPGRAM_API_KEY": "...", "OPENAI_API_KEY": "..."}
_store: dict[str, dict[str, str]] = {}


def _squash(name: str) -> str:
    """Uppercase and drop everything but A–Z/0–9 (so spaces, hyphens, underscores collapse)."""
    return re.sub(r"[^A-Z0-9]", "", (name or "").upper())


def _clean_value(value: str) -> str:
    """Trim whitespace and a single pair of surrounding quotes users sometimes paste in."""
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1].strip()
    return v


def normalize_provider_env() -> dict[str, bool]:
    """Copy recognized key spellings into their canonical env var names (idempotent).

    Two passes so a mis-named secret key still lands on the canonical name:
      1. exact alias names (fast path), then
      2. a fuzzy scan of *every* env var — matching by squashed name — which recovers keys
         stored with stray spaces/hyphens/case in the Secret's KEY field.

    Safe to call at startup and again per request. Never overwrites a canonical value that is
    already set. Returns ``{canonical: present}`` after normalization for logging/health.
    """
    # Pass 0 — sanitize a value already on the canonical name (strip stray quotes/whitespace
    # a user may have pasted into the Secret value, e.g. OPENAI_API_KEY="sk-...").
    for canonical in KEY_ALIASES:
        raw = os.environ.get(canonical)
        if raw is not None:
            cleaned = _clean_value(raw)
            if cleaned != raw:
                os.environ[canonical] = cleaned

    # Pass 1 — exact alias names.
    for canonical, aliases in KEY_ALIASES.items():
        if os.environ.get(canonical, "").strip():
            continue
        for alias in aliases:
            if alias == canonical:
                continue
            value = _clean_value(os.environ.get(alias, ""))
            if value:
                os.environ[canonical] = value
                break

    # Pass 2 — fuzzy scan of ALL env vars for anything still missing.
    missing = [c for c in KEY_ALIASES if not os.environ.get(c, "").strip()]
    if missing:
        for raw_name, raw_value in list(os.environ.items()):
            value = _clean_value(raw_value)
            if not value:
                continue
            squashed = _squash(raw_name)
            for canonical in missing:
                if canonical in os.environ and os.environ[canonical].strip():
                    continue
                if squashed in KEY_FUZZY_FORMS.get(canonical, frozenset()):
                    os.environ[canonical] = value
                    break

    return {canonical: bool(os.environ.get(canonical, "").strip()) for canonical in KEY_ALIASES}


def provider_env_name_report() -> list[tuple[str, bool]]:
    """Diagnostics: env var NAMES (never values) that look OpenAI/Deepgram related.

    Returns ``[(repr(name), has_value), …]`` so startup logs reveal a mis-named or
    empty-valued secret key — e.g. ``("'OPENAI_API_KEY '", True)`` exposes a trailing space
    in the Secret's KEY field that would otherwise be invisible.
    """
    hits: list[tuple[str, bool]] = []
    for raw_name, raw_value in sorted(os.environ.items()):
        upper = raw_name.upper()
        if any(tok in upper for tok in ("OPENAI", "OPEN_AI", "DEEPGRAM", "DG_", "GPT")):
            hits.append((repr(raw_name), bool(_clean_value(raw_value))))
    return hits


def remember_keys(session_token: str, keys: dict[str, str]) -> None:
    """Store non-empty keys for a session token (overwrites only provided values)."""
    if not session_token:
        return
    with _lock:
        bucket = _store.setdefault(session_token, {})
        for name in KEY_NAMES:
            value = (keys.get(name) or "").strip()
            if value:
                bucket[name] = value


def keys_for_session(session_token: str) -> dict[str, str]:
    with _lock:
        return dict(_store.get(session_token, {}))


def clear_keys(session_token: str) -> None:
    with _lock:
        _store.pop(session_token, None)


def apply_session_keys_to_env(session_token: str) -> dict[str, bool]:
    """
    Ensure required keys are present in ``os.environ`` for this request.

    **User-pasted (per-session) keys take precedence** — when a user has provided their own
    key it is applied for this request so their usage is billed to their own account. A
    Space secret (env value) is used only as a fallback when the user has not set a key.
    Returns a map of ``{KEY_NAME: present}`` reflecting effective availability after applying.

    IMPORTANT: ``os.environ`` is process-global and shared by every gunicorn worker thread.
    Callers that apply per-session keys MUST snapshot/restore around the request (see
    ``snapshot_env`` / ``restore_env``) so one user's pasted key can never bleed into a later
    or concurrent request that belongs to a different browser session.
    """
    # Ensure any alias-spelled secret (e.g. OPEN_AI_API_KEY) is visible under the canonical
    # name before we decide what is present for this request.
    normalize_provider_env()
    session_keys = keys_for_session(session_token)
    result: dict[str, bool] = {}
    for name in KEY_NAMES:
        if session_keys.get(name):
            # User's own key wins (billed to them); restored after the request.
            os.environ[name] = session_keys[name]
        result[name] = bool(os.environ.get(name, "").strip())
    return result


def snapshot_env() -> dict[str, str | None]:
    """Capture current values of the managed key env vars (``None`` when unset)."""
    return {name: os.environ.get(name) for name in KEY_NAMES}


def restore_env(snapshot: dict[str, str | None]) -> None:
    """
    Restore the managed key env vars to a previous ``snapshot``.

    This reverts any per-session keys applied during a request so process-global
    ``os.environ`` only ever retains genuine Space secrets between requests.
    """
    for name in KEY_NAMES:
        prev = snapshot.get(name)
        if prev is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev


def effective_key_status(session_token: str) -> dict[str, dict[str, object]]:
    """
    Report, per key, whether it is available and where it comes from (without leaking it).

    ``source`` is one of: ``"session"`` (user-pasted), ``"space"`` (env/secret), ``"none"``.
    User-pasted keys take precedence, so a present session key is reported as ``"session"``
    even when a Space secret also exists (the session key is what will actually be used).
    """
    normalize_provider_env()
    session_keys = keys_for_session(session_token)
    status: dict[str, dict[str, object]] = {}
    for name in KEY_NAMES:
        env_val = os.environ.get(name, "").strip()
        if session_keys.get(name):
            source = "session"
            present = True
        elif env_val:
            source = "space"
            present = True
        else:
            source = "none"
            present = False
        status[name] = {"present": present, "source": source}
    return status
