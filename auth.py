"""auth.py — verify the UI's per-user session token on every web request.

The IRIS Next.js UI (``ui/``) signs a short-lived HS256 token in its NextAuth
``session`` callback (see ``ui/src/app/api/auth/[...nextauth]/route.ts``) and sends
it as ``Authorization: Bearer <token>`` on every backend call
(``ui/src/lib/api.ts``). This module is the backend half: a FastAPI dependency,
``get_current_user``, that verifies that token and returns the authenticated
``user_id`` (the user's email — the same value the UI treats as identity).

The token is a PLAIN signed JWS (not NextAuth's encrypted session JWE), so the
two sides only need to share ONE symmetric secret:

  • UI signs with   NEXTAUTH_SECRET       (ui/.env.local)
  • backend verifies with BACKEND_JWT_SECRET (.env)  ← must be the SAME value

Set both to one strong value (e.g. ``python -c "import secrets;
print(secrets.token_urlsafe(48))"``). If ``BACKEND_JWT_SECRET`` is unset the
dependency fails CLOSED with 503 (never open) — an unconfigured backend must not
silently accept unauthenticated requests.

Only the data/action endpoints depend on this (``/ask``, ``/resume``, thread
history, upload, and the shared Google status/disconnect). ``/api/greeting`` and
``/health`` are intentionally left open (no user data). The Slack webhook path
authenticates by its own HMAC signature and does NOT use this dependency.
"""

from __future__ import annotations

import logging
import os

import jwt  # PyJWT
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

# Shared symmetric secret — MUST equal the UI's NEXTAUTH_SECRET. Read fresh from
# the environment (load_dotenv runs in app.py before this is imported).
_SECRET = os.getenv("BACKEND_JWT_SECRET", "")

# Audience claim the UI stamps on the token; verified here so a token minted for
# some other service can't be replayed against this backend.
_AUDIENCE = os.getenv("BACKEND_JWT_AUDIENCE", "iris-backend")

# auto_error=False so a MISSING credential yields our own 401 (FastAPI's default
# HTTPBearer raises 403 for a missing header) — one consistent status for both
# "no token" and "bad token".
_bearer = HTTPBearer(auto_error=False)

_UNAUTHENTICATED_HEADERS = {"WWW-Authenticate": "Bearer"}


def get_current_user(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Verify the Bearer session token and return the authenticated user_id.

    The user_id is the token ``sub`` (the user's email). Raises 401 on a missing,
    malformed, expired, wrong-audience, or wrong-signature token; 503 if the
    backend has no secret configured (fail closed).
    """
    if not _SECRET:
        # Misconfiguration, not the caller's fault — but never fall open.
        logger.error("auth.no_secret: BACKEND_JWT_SECRET is unset — refusing all requests.")
        raise HTTPException(status_code=503, detail="Authentication is not configured on the server.")

    if cred is None or not cred.credentials:
        raise HTTPException(
            status_code=401,
            detail="Missing session token.",
            headers=_UNAUTHENTICATED_HEADERS,
        )

    try:
        claims = jwt.decode(
            cred.credentials,
            _SECRET,
            algorithms=["HS256"],
            audience=_AUDIENCE,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        # One opaque message to the client; detail stays in the server log.
        logger.info("auth.reject: %s: %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session token.",
            headers=_UNAUTHENTICATED_HEADERS,
        ) from exc

    user_id = claims.get("sub") or claims.get("email")
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Session token has no subject.",
            headers=_UNAUTHENTICATED_HEADERS,
        )
    # Expose the authenticated identity on request.state so the rate limiter's key
    # function (web_api._user_key) can bucket by user instead of by IP. Set only on
    # the success path — a rejected request never reaches a rate-limited route.
    request.state.user_id = str(user_id)
    return str(user_id)
