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
import tempfile as _tempfile
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
#
# THE PATH MUST BE WRITABLE, and on Railway the default was not. The container
# ships its source tree root-owned and runs as the unprivileged `iris` user
# (Dockerfile: `USER iris`), so the old default — <project_root>/google_token.json,
# i.e. /app/google_token.json — raised PermissionError inside store_refresh_token.
# That surfaced as the OAuth callback bouncing to the UI with
# `?reason=callback_PermissionError_…`: Google consent succeeded, the refresh token
# was in hand, and it was then thrown away. Every subsequent Grace delegation hit
# `_get_credentials` with no token and returned blank — the symptom recorded as
# guardrail E-34 ("blank Grace result = auth failure, not tool failure").
#
# So the location is now RESOLVED against writability instead of assumed:
# GOOGLE_TOKEN_FILE if set, else the first writable candidate. Each candidate is
# tested by the only thing that settles it — can this process create a file in
# that directory — because ownership, read-only mounts and volume permissions all
# fail differently and none of them are visible from the path string.
_PROJECT_DIR = _Path(__file__).parent


def _dir_is_writable(directory: _Path) -> bool:
    """True when ``directory`` already exists and this process can create a file in it.

    Two deliberate properties:

    * It does NOT create the directory. An earlier draft did, and on a Windows dev
      box ``/app/data`` resolves to ``C:\\app\\data`` — so probing the container
      path would have silently created a stray directory at the drive root. Every
      candidate below is a path something else is responsible for creating (the
      Dockerfile for ``/app/data``, the checkout for the project dir, the OS for
      temp), so requiring prior existence is also the correct test.
    * It probes with a real create/unlink rather than ``os.access(..., W_OK)``,
      which consults permission bits against the real uid and still returns True on
      some read-only mounts.
    """
    if not directory.is_dir():
        return False
    probe = directory / f".iris_write_probe_{os.getpid()}"
    try:
        probe.write_text("", encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001 — any failure means "not writable"
        return False
    finally:
        try:
            probe.unlink()
        except Exception:  # noqa: BLE001 — nothing to clean up if it never landed
            pass


def _resolve_token_file() -> _Path:
    """Where the Google refresh token is persisted, guaranteed writable.

    Order: explicit ``GOOGLE_TOKEN_FILE`` → ``/app/data`` (the Dockerfile's
    iris-owned volume mount, which also survives a redeploy) → the project
    directory (correct for local dev) → the OS temp dir.

    An explicit ``GOOGLE_TOKEN_FILE`` whose directory is NOT writable does not win.
    That is the Railway case specifically: a platform volume mounted at ``/app/data``
    arrives root-owned, so the Dockerfile's ``chown`` (which ran at build time, on the
    image's own directory) no longer applies to what is mounted there at run time —
    the configured path is real and the process still cannot write it. Falling
    through to a writable location keeps the Google connection working; the WARNING
    names both paths so the misconfiguration is fixable rather than invisible.

    The temp dir is last and is deliberately still a candidate: it does not survive
    a redeploy, but it does survive the rest of the container's life, which is the
    difference between "re-connect once" and "re-connect on every request".
    """
    candidates: list[_Path] = []
    explicit = os.getenv("GOOGLE_TOKEN_FILE", "").strip()
    if explicit:
        explicit_path = _Path(explicit)
        if _dir_is_writable(explicit_path.parent):
            return explicit_path
        _log.warning(
            "GOOGLE_TOKEN_FILE=%s is not writable by this process (uid=%s) — falling back "
            "to a writable location. A Google re-connect will work but will not persist "
            "there; point GOOGLE_TOKEN_FILE at a writable volume to fix this properly.",
            explicit_path,
            getattr(os, "geteuid", lambda: "n/a")(),
        )
        # Don't re-test the same directory twice on the way down.
        candidates = [c for c in (_Path("/app/data"), _PROJECT_DIR) if c != explicit_path.parent]
    else:
        candidates = [_Path("/app/data"), _PROJECT_DIR]

    for candidate in [*candidates, _Path(_tempfile.gettempdir())]:
        if _dir_is_writable(candidate):
            if candidate != _PROJECT_DIR:
                _log.info("Google token file resolved to %s", candidate)
            return candidate / "google_token.json"

    # Nothing was writable — keep the historical path so the error message names
    # something the operator recognises.
    _log.error(
        "No writable location found for the Google token file; falling back to %s. "
        "A UI Google re-connect will fail until GOOGLE_TOKEN_FILE points somewhere writable.",
        _PROJECT_DIR,
    )
    return _PROJECT_DIR / "google_token.json"


_TOKEN_FILE = _resolve_token_file()
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


# Process-local copy of a token the connect flow minted. It exists for exactly one
# case: the disk write below failed (unwritable path, full disk, read-only mount).
# Before this, such a failure raised out of store_refresh_token, google_oauth's
# /callback caught it and redirected to the UI with `?reason=callback_<Error>` —
# and the perfectly good refresh token Google had just issued was discarded. The
# user saw "connect failed" and every Grace delegation afterwards returned blank.
# Holding it here makes a failed WRITE cost persistence across restarts only,
# instead of costing the connection outright.
_MEMORY_REFRESH_TOKEN = ""

# Mirrors _DISCONNECT_FLAG for the same reason: if the sentinel cannot be written,
# a disconnect must still take effect for this process rather than silently doing
# nothing while the UI reports success.
_MEMORY_DISCONNECTED = False


def active_refresh_token() -> str:
    """The refresh token IRIS should use right now: the UI-connected token first,
    else the env token — unless the UI has explicitly disconnected."""
    if _MEMORY_DISCONNECTED or _DISCONNECT_FLAG.exists():
        return ""
    return _stored_refresh_token() or _MEMORY_REFRESH_TOKEN or _REFRESH_TOKEN


def store_refresh_token(token: str) -> None:
    """Persist a newly minted refresh token from the UI connect flow and make it
    active (clears any prior disconnect sentinel + the service cache).

    Never raises. A disk failure is logged and the token is kept in memory (see
    ``_MEMORY_REFRESH_TOKEN``) so the connection still works right now — losing
    durability across a restart is a far smaller failure than losing the token.
    """
    global _MEMORY_REFRESH_TOKEN, _MEMORY_DISCONNECTED  # noqa: PLW0603
    if not token:
        return
    # Set the in-memory copy FIRST, so it holds even if the write below fails.
    _MEMORY_REFRESH_TOKEN = token
    _MEMORY_DISCONNECTED = False
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(_json.dumps({"refresh_token": token}), encoding="utf-8")
    except Exception:  # noqa: BLE001 — a write failure must not fail the connect flow
        _log.error(
            "Could not persist the Google refresh token to %s — it is active for THIS "
            "process only and will be lost on restart. Set GOOGLE_TOKEN_FILE to a "
            "writable path (or GOOGLE_REFRESH_TOKEN in the environment) to make it durable.",
            _TOKEN_FILE,
            exc_info=True,
        )
    try:
        _DISCONNECT_FLAG.unlink()
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 — the in-memory flag above already cleared it
        _log.warning("Could not remove the Google disconnect sentinel %s", _DISCONNECT_FLAG)
    reset_service_cache()


def clear_stored_refresh_token() -> None:
    """UI disconnect: drop the stored token and suppress the env fallback so status
    reads 'disconnected'. Reconnecting via the OAuth flow restores access.

    Never raises — the in-memory flag is authoritative for this process, so the
    disconnect takes effect even where the sentinel file cannot be written.
    """
    global _MEMORY_REFRESH_TOKEN, _MEMORY_DISCONNECTED  # noqa: PLW0603
    _MEMORY_REFRESH_TOKEN = ""
    _MEMORY_DISCONNECTED = True
    try:
        _TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        _log.warning("Could not delete the stored Google token file %s", _TOKEN_FILE)
    try:
        _DISCONNECT_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _DISCONNECT_FLAG.write_text("1", encoding="utf-8")
    except Exception:  # noqa: BLE001
        _log.warning(
            "Could not write the Google disconnect sentinel %s — the disconnect holds "
            "for this process but not across a restart.",
            _DISCONNECT_FLAG,
        )
    reset_service_cache()


def _get_credentials() -> Any:
    """Build Google API credentials.
    
    Priority:
      1. User OAuth (active refresh token from UI / .env) — allows personal Drive/Sheets/Gmail access.
      2. Service Account JSON key (permanent cloud server-to-server credentials).
    """
    # ── 1. User OAuth2 Credentials (Primary) ──────────────────────────────────
    refresh_tok = active_refresh_token()
    if refresh_tok and _CLIENT_ID and _CLIENT_SECRET:
        try:
            creds = Credentials(
                token=None,
                refresh_token=refresh_tok,
                token_uri=_TOKEN_URI,
                client_id=_CLIENT_ID,
                client_secret=_CLIENT_SECRET,
                scopes=_SCOPES,
            )
            _log.debug("Loaded User OAuth Google credentials from active refresh token.")
            return creds
        except Exception as exc:
            _log.warning("Failed to initialize User OAuth credentials: %s", exc)

    # ── 2. Service Account: Railway / cloud env var ───────────────────────────
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
            raise EnvironmentError(
                f"GOOGLE_SERVICE_ACCOUNT_JSON is set but could not be parsed: {exc}\n"
                "Ensure the value is the full, valid JSON content of your service account key file."
            ) from exc

    # ── 3. Service Account: JSON key file on disk ────────────────────────────
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
            raise EnvironmentError(
                f"Failed to load Google Service Account key from '{sa_path}': {exc}\n"
                "Ensure GOOGLE_SERVICE_ACCOUNT_FILE points to a valid JSON key file."
            ) from exc

    raise EnvironmentError(
        "Google credentials not found.\n"
        "  • User OAuth: Connect via UI Settings or provide GOOGLE_REFRESH_TOKEN in .env\n"
        "  • Service Account: Place service_account.json in project root or set GOOGLE_SERVICE_ACCOUNT_JSON"
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

