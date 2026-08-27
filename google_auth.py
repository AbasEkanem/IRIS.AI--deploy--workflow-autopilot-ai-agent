"""
google_auth.py
==============
Google Service Account authentication module for IRIS.AI (Grace Subagent).

Provides a single `get_service(service_name)` factory that returns an
authenticated Google API service client for:
  - Calendar    → get_service("calendar")
  - Forms       → get_service("forms")
  - Drive       → get_service("drive")
  - Sheets      → get_service("sheets")

Authentication uses a Google Cloud Service Account JSON key (permanent credentials
that NEVER expire or get revoked). No OAuth refresh tokens, no browser login.

Required .env variable:
  GOOGLE_SERVICE_ACCOUNT_FILE  — Path to the service account JSON key file
                                  (default: service_account.json in project root)

Optional .env variable:
  GOOGLE_SERVICE_ACCOUNT_SUBJECT — Email of the user to impersonate via
                                    Domain-Wide Delegation (leave blank if not needed)
"""

from __future__ import annotations

import logging
import os
import json as _json
from functools import lru_cache
from pathlib import Path as _Path
from typing import Any

from dotenv import load_dotenv
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

_log = logging.getLogger(__name__)

# ── OAuth2 / Service Account credentials from environment ──────────────────────
_CLIENT_ID     = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "")

_SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    os.getenv("GOOGLE_APPLICATION_CREDENTIALS", str(_Path(__file__).parent / "service_account.json"))
)
_SERVICE_ACCOUNT_SUBJECT = os.getenv("GOOGLE_SERVICE_ACCOUNT_SUBJECT", "")

# Google token endpoint
_TOKEN_URI = "https://oauth2.googleapis.com/token"

# ── Required OAuth scopes (superset — covers all Google Workspace services) ───
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Service name → (API name, API version) mapping ───────────────────────────
_SERVICE_MAP: dict[str, tuple[str, str]] = {
    "gmail":    ("gmail",    "v1"),
    "calendar": ("calendar", "v3"),
    "forms":    ("forms",    "v1"),
    "sheets":   ("sheets",   "v4"),
    "drive":    ("drive",    "v3"),
}


# ── Re-connected token persistence (UI Google-connect flow) ───────────────────
# The UI's /google/connect → /google/callback flow (google_oauth.py) can mint a
# NEW refresh token for this process. It is written to a small on-disk file
# (GOOGLE_TOKEN_FILE) which takes PRECEDENCE over the env GOOGLE_REFRESH_TOKEN, so
# a UI re-connect is picked up without editing .env. A /google/disconnect writes a
# sentinel that also suppresses the env fallback — so the UI's connected/
# disconnected toggle reflects reality in this single-user deployment; re-connecting
# clears the sentinel. active_refresh_token() is read FRESH on every _get_credentials
# call (never the frozen module constant), and store/clear bust the get_service
# lru_cache so a new token takes effect on the next call.
_TOKEN_FILE = _Path(os.getenv("GOOGLE_TOKEN_FILE", str(_Path(__file__).parent / "google_token.json")))
_DISCONNECT_FLAG = _TOKEN_FILE.with_suffix(".disconnected")


def _stored_refresh_token() -> str:
    """The refresh token minted by the UI connect flow, if any."""
    try:
        if _TOKEN_FILE.exists():
            data = _json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            return (data or {}).get("refresh_token", "") or ""
    except Exception:  # noqa: BLE001 — a corrupt token file must not crash auth
        _log.warning("Failed reading stored Google token file", exc_info=True)
    return ""


def active_refresh_token() -> str:
    """The refresh token IRIS should use right now: the UI-connected token first,
    else the env token — unless the UI has explicitly disconnected."""
    if _DISCONNECT_FLAG.exists():
        return ""
    return _stored_refresh_token() or _REFRESH_TOKEN


def store_refresh_token(token: str) -> None:
    """Persist a newly minted refresh token from the UI connect flow and make it
    active (clears any prior disconnect sentinel + the service cache)."""
    if not token:
        return
    _TOKEN_FILE.write_text(_json.dumps({"refresh_token": token}), encoding="utf-8")
    try:
        _DISCONNECT_FLAG.unlink()
    except FileNotFoundError:
        pass
    reset_service_cache()


def clear_stored_refresh_token() -> None:
    """UI disconnect: drop the stored token and suppress the env fallback so status
    reads 'disconnected'. Reconnecting via the OAuth flow restores access."""
    try:
        _TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass
    _DISCONNECT_FLAG.write_text("1", encoding="utf-8")
    reset_service_cache()


def _get_credentials() -> Any:
    """Build Google API credentials.

    Priority:
    1. Direct User OAuth (active refresh token from .env or UI connect flow)
       -> Allows full creation of Google Sheets, Drive files, Calendar events, and Docs
          directly in the user's personal Google account.
    2. Service Account JSON (env var `GOOGLE_SERVICE_ACCOUNT_JSON` on Railway or `service_account.json` locally)
       -> Fallback for server-to-service calls.
    """
    # ── 1. User OAuth2 Credentials ───────────────────────────────────────────
    refresh_token = active_refresh_token()
    if _CLIENT_ID and _CLIENT_SECRET and refresh_token:
        try:
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri=_TOKEN_URI,
                client_id=_CLIENT_ID,
                client_secret=_CLIENT_SECRET,
                scopes=None,
            )
            _log.debug("Loaded Google User OAuth2 credentials.")
            return creds
        except Exception as exc:
            _log.warning("Failed creating User OAuth credentials: %s", exc)

    # ── 2. Service Account (Railway env var) ──────────────────────────────────
    sa_json_str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if sa_json_str:
        try:
            sa_info = _json.loads(sa_json_str)
            creds = service_account.Credentials.from_service_account_info(
                sa_info,
                scopes=_SCOPES,
                subject=_SERVICE_ACCOUNT_SUBJECT or None,
            )
            _log.debug("Loaded Google Service Account credentials from GOOGLE_SERVICE_ACCOUNT_JSON env var.")
            return creds
        except Exception as exc:
            _log.warning("Failed parsing GOOGLE_SERVICE_ACCOUNT_JSON env var: %s", exc)

    # ── 3. Service Account (Local JSON file) ──────────────────────────────────
    sa_path = _Path(_SERVICE_ACCOUNT_FILE)
    if sa_path.is_file():
        try:
            creds = service_account.Credentials.from_service_account_file(
                str(sa_path),
                scopes=_SCOPES,
                subject=_SERVICE_ACCOUNT_SUBJECT or None,
            )
            _log.debug("Loaded Google Service Account credentials from %s", sa_path)
            return creds
        except Exception as exc:
            _log.warning("Failed loading service account file %s: %s", sa_path, exc)

    raise EnvironmentError(
        "Google credentials missing. Please set GOOGLE_REFRESH_TOKEN, "
        "GOOGLE_SERVICE_ACCOUNT_JSON, or provide service_account.json."
    )


@lru_cache(maxsize=None)
def get_service(service_name: str) -> Any:
    """Return a cached authenticated Google API service client.

    Args:
        service_name: One of 'gmail', 'calendar', 'forms', 'drive'.

    Returns:
        Authenticated Google API Resource object.

    Raises:
        ValueError: If service_name is not recognized.
        EnvironmentError: If required OAuth credentials are missing from .env.
    """
    if service_name not in _SERVICE_MAP:
        raise ValueError(
            f"Unknown Google service: '{service_name}'. "
            f"Supported: {list(_SERVICE_MAP.keys())}"
        )

    api_name, api_version = _SERVICE_MAP[service_name]
    creds = _get_credentials()

    _log.debug("Building Google API service: %s %s", api_name, api_version)
    return build(api_name, api_version, credentials=creds, cache_discovery=False)


def reset_service_cache(service_name: str | None = None) -> None:
    """Clear the cached service client(s) so fresh connections are opened on retry."""
    get_service.cache_clear()
    _log.debug("Google API service cache cleared.")


_TRANSIENT_NET_ERRORS = (
    ConnectionError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
    OSError,  # On Windows, WinError 10053/10054 surface as OSError subclasses
)


def is_transient_error(exc: Exception) -> bool:
    """Detect transient transport / socket connection aborts on host machine."""
    if isinstance(exc, _TRANSIENT_NET_ERRORS):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "10053",
            "10054",
            "aborted",
            "reset by peer",
            "broken pipe",
            "connection",
            "timed out",
            "ssl",
            "handshake",
            "remotedisconnected",
        )
    )


def execute_with_retry(request_fn: Any, max_attempts: int = 3, initial_backoff: float = 0.5) -> Any:
    """Execute a Google API request with automatic connection reset and exponential backoff retry on transient socket drops.

    Args:
        request_fn: A zero-argument callable that performs the Google API request (or returns an executable request).
        max_attempts: Maximum number of retry attempts (default 3).
        initial_backoff: Initial sleep duration in seconds before retry (default 0.5s).

    Returns:
        The result of the executed Google API request.
    """
    import time
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            req = request_fn() if callable(request_fn) else request_fn
            if hasattr(req, "execute"):
                return req.execute()
            return req
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not is_transient_error(exc):
                raise
            # Clear the cached service so that subsequent calls create a fresh TCP/HTTP connection
            reset_service_cache()
            backoff = initial_backoff * (2 ** (attempt - 1))
            _log.warning(
                "Google API transport drop (%s, attempt %d/%d). Resetting connection and retrying in %.1fs...",
                exc, attempt, max_attempts, backoff
            )
            time.sleep(backoff)
    if last_exc:
        raise last_exc

