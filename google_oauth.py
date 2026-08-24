"""google_oauth.py — FastAPI router for the UI's Google Workspace connect flow.

Backs the endpoints ui/src/lib/api.ts calls:
  • GET  /google/status         — is a usable Google refresh token present?
  • POST /google/connect-ticket — mint a single-use ticket authorizing /connect
  • GET  /google/connect        — 302 into Google's OAuth consent screen
  • GET  /google/callback       — exchange the code, persist the token, bounce to UI
  • POST /google/disconnect     — drop the stored token (UI toggle)

Single-user design (matches deployed reality — see the plan's Part C/F): there is
no per-user identity layer, so this connects ONE Google account for the whole
service. /status and /disconnect work immediately against the existing env token;
/connect additionally needs a **Web-application** OAuth client + GOOGLE_REDIRECT_URI
(the existing get_google_refresh_token.py uses a Desktop/loopback client, which
can't drive a server redirect). Until that web client exists, /status reports
``available:false`` and the UI shows a graceful non-connectable state.

Everything reuses google_auth.py: its scopes, token URI, client id/secret, the
active-token accessor, and the store/clear + cache-reset helpers added for this
flow — so a token connected here is exactly what Grace's tools then use.
"""

from __future__ import annotations

import logging
import os
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

import google_auth
from auth import get_current_user

logger = logging.getLogger(__name__)

# The UI to bounce back to after the OAuth round-trip (page.tsx reads
# ?google_connected=1|0&reason=…). Same env var the CORS config uses in app.py.
_UI_ORIGIN = os.getenv("WEB_UI_ORIGIN", "http://localhost:3000").split(",")[0].strip().rstrip("/")

# Where Google sends the user back. MUST be registered on the web OAuth client.
_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/google/callback")

# Dev convenience: oauthlib refuses a plain-http redirect unless told the transport
# is intentionally insecure. Only relax it for a localhost redirect (never prod).
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"

# ── OAuth CSRF state (OI-1/OI-2) ───────────────────────────────────────────────
# The per-request CSRF state is carried in a short-lived, HttpOnly cookie set on
# the /connect 302 and verified STRICTLY on /callback (reject on mismatch). This
# replaces the former module-global _oauth_state: it is per-browser (concurrent
# connects don't clobber each other) and survives a server restart between the
# two legs (the state lives in the browser, not server memory) — so the strict
# check no longer risks the spurious hard-fail the old warn-only check hedged against.
_STATE_COOKIE = "iris_oauth_state"
# Long enough to sit on Google's consent screen (pick an account, review scopes),
# short enough that an abandoned /connect leaves nothing replayable behind.
_STATE_COOKIE_MAX_AGE = 600  # seconds

# ── PKCE code_verifier (must survive /connect → /callback) ────────────────────
# google_auth_oauthlib's Flow defaults to autogenerate_code_verifier=True, so
# authorization_url() mints a 128-char verifier and sends its S256 code_challenge
# to Google — which then binds the auth code to that challenge. /callback builds a
# SECOND Flow, whose code_verifier is None (only authorization_url generates one),
# so the exchange posted no verifier and Google rejected it:
#   InvalidGrantError: (invalid_grant) Missing code verifier
# That made the Connect button unable to restore a connection at all. The verifier
# rides along in its own short-lived HttpOnly cookie, exactly like the CSRF state:
# per-browser, and it survives a server restart between the two legs.
_VERIFIER_COOKIE = "iris_oauth_verifier"

# ── Single-use connect ticket (OI-3) ───────────────────────────────────────────
# /connect is a full-page browser navigation, so it cannot carry a Bearer header
# and any ambient session cookie would make it CSRF-able. Instead the UI mints a
# ticket over the authenticated POST /connect-ticket and hands it to /connect,
# which consumes it before redirecting to Google. Fail-closed: no ticket, an
# expired one, or a replayed one is rejected outright.
#
# The store below is in-process, which assumes a SINGLE worker — true for the current
# deployment (REDIS_URL unset, one uvicorn worker). A multi-worker deployment
# must either pin one worker for this flow or replace _issue_ticket/_consume_ticket
# with a shared Redis store using an atomic GETDEL (same fail-closed, single-use
# semantics); intentionally not added here to keep this security fix minimal.
_TICKET_TTL = 120  # seconds
_connect_tickets: dict[str, float] = {}  # ticket -> expiry timestamp


def _issue_ticket() -> str:
    """Mint a single-use connect ticket valid for _TICKET_TTL seconds."""
    now = time.time()
    # Opportunistic purge so a stream of un-consumed tickets can't grow the dict.
    for tok, exp in list(_connect_tickets.items()):
        if exp <= now:
            _connect_tickets.pop(tok, None)
    ticket = secrets.token_urlsafe(32)
    _connect_tickets[ticket] = now + _TICKET_TTL
    return ticket


def _consume_ticket(ticket: str) -> bool:
    """Atomically consume a ticket. True only if it existed and hadn't expired.

    dict.pop is atomic under the GIL, so a concurrent replay finds nothing — the
    ticket is single-use by construction.
    """
    exp = _connect_tickets.pop(ticket, None)
    return exp is not None and exp > time.time()


router = APIRouter(prefix="/google", tags=["google"])


def _connect_available() -> bool:
    """True only when the interactive /connect flow can actually run."""
    return bool(google_auth._CLIENT_ID and google_auth._CLIENT_SECRET and _REDIRECT_URI)


def _client_config() -> dict:
    """Assemble the 'web' OAuth client config inline from env (no client_secret.json)."""
    return {
        "web": {
            "client_id": google_auth._CLIENT_ID,
            "client_secret": google_auth._CLIENT_SECRET,
            "auth_uri": _AUTH_URI,
            "token_uri": google_auth._TOKEN_URI,
            "redirect_uris": [_REDIRECT_URI],
        }
    }


def _ui_redirect(connected: bool, reason: str | None = None) -> RedirectResponse:
    url = f"{_UI_ORIGIN}/?google_connected={'1' if connected else '0'}"
    if reason:
        url += f"&reason={reason}"
    return RedirectResponse(url, status_code=302)


# ═══════════════════════════════════════════════════════════════════════════════
# GET /google/status
# ═══════════════════════════════════════════════════════════════════════════════
@router.get("/status")
async def google_status(user_id: str = Depends(get_current_user)):
    """Probe the active refresh token with a real refresh-token grant.

    Requires a signed-in user (any authenticated user may view the SHARED Google
    connection this pass — per-user Google is the deferred follow-up). Returns
    {connected, available, detail?} exactly as ui/src/lib/api.ts expects.
    ``connected`` = the token actually mints an access token; ``available`` =
    the server is configured to run the interactive /connect flow.
    """
    available = _connect_available()
    token = google_auth.active_refresh_token()
    if not token:
        return {"connected": False, "available": available, "detail": "No Google token connected."}
    if not (google_auth._CLIENT_ID and google_auth._CLIENT_SECRET):
        return {"connected": False, "available": available, "detail": "OAuth client not configured."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                google_auth._TOKEN_URI,
                data={
                    "client_id": google_auth._CLIENT_ID,
                    "client_secret": google_auth._CLIENT_SECRET,
                    "refresh_token": token,
                    "grant_type": "refresh_token",
                },
            )
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        connected = resp.status_code == 200 and isinstance(body, dict) and "access_token" in body
        detail = None if connected else f"Token refresh failed ({resp.status_code}: {body.get('error', 'unknown')})."
        return {"connected": connected, "available": available, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        logger.warning("google.status_probe_failed: %s", exc)
        return {"connected": False, "available": available, "detail": "Could not reach Google to verify the token."}


# ═══════════════════════════════════════════════════════════════════════════════
# POST /google/connect-ticket
# ═══════════════════════════════════════════════════════════════════════════════
@router.post("/connect-ticket")
async def google_connect_ticket(user_id: str = Depends(get_current_user)):
    """Mint a single-use ticket that authorizes ONE /connect navigation (OI-3).

    Requires a valid session (Bearer). The UI calls this, then navigates the
    browser to /connect?ticket=<ticket>. A POST authenticated by a Bearer header
    (not an ambient cookie) is not itself CSRF-able, so no extra token is needed.
    """
    if not _connect_available():
        return JSONResponse({"error": "not_configured"}, status_code=409)
    return JSONResponse({"ticket": _issue_ticket()})


# ═══════════════════════════════════════════════════════════════════════════════
# GET /google/connect
# ═══════════════════════════════════════════════════════════════════════════════
@router.get("/connect")
async def google_connect(ticket: str | None = None):
    """302 into Google's consent screen (full-page browser nav from the UI).

    Authorized by a single-use connect ticket (OI-3): the ticket is consumed
    here, BEFORE the redirect to Google, so a ticket that leaks via the Referer
    header is already spent. The per-request CSRF state is stashed in an HttpOnly
    cookie (OI-1/OI-2) and verified strictly on /callback.
    """
    if not _connect_available():
        # No web OAuth client configured — bounce back so the UI shows the
        # non-connectable state instead of a dead end.
        logger.error("google.connect_unavailable: missing client id/secret or redirect uri")
        return _ui_redirect(False, reason="not_configured")
    if not ticket or not _consume_ticket(ticket):
        logger.warning("google.connect_rejected: missing or invalid connect ticket")
        return _ui_redirect(False, reason="unauthorized")
    try:
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(
            _client_config(), scopes=google_auth._SCOPES, redirect_uri=_REDIRECT_URI
        )
        auth_url, state = flow.authorization_url(
            access_type="offline",          # ask for a refresh token
            prompt="consent",               # force a refresh token even on re-consent
            include_granted_scopes="true",
        )
        resp = RedirectResponse(auth_url, status_code=302)
        # OI-1/OI-2: per-request CSRF state in a short-lived HttpOnly cookie
        # (replaces the module global). SameSite=Lax so the browser still sends
        # it on Google's top-level GET callback; Secure only under an https
        # redirect (http://localhost dev must still be able to set it).
        resp.set_cookie(
            key=_STATE_COOKIE,
            value=state,
            max_age=_STATE_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=_REDIRECT_URI.startswith("https"),
            path="/google",
        )
        # PKCE: authorization_url() just minted the verifier whose S256 challenge
        # went to Google. Carry it to /callback under the same cookie settings —
        # without it the exchange fails with "Missing code verifier".
        if flow.code_verifier:
            resp.set_cookie(
                key=_VERIFIER_COOKIE,
                value=flow.code_verifier,
                max_age=_STATE_COOKIE_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=_REDIRECT_URI.startswith("https"),
                path="/google",
            )
        # Leak hygiene: don't hand the (already-spent) ticket URL to Google via
        # the Referer header, and keep the redirect out of any cache.
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as exc:  # noqa: BLE001
        logger.exception("google.connect_failed")
        return _ui_redirect(False, reason="connect_error")


# ═══════════════════════════════════════════════════════════════════════════════
# GET /google/callback
# ═══════════════════════════════════════════════════════════════════════════════
@router.get("/callback")
async def google_callback(request: Request):
    """Exchange the auth code for tokens, persist the refresh token, bounce to UI.

    The per-request CSRF state cookie (set on /connect) is verified STRICTLY here
    and cleared on every exit (OI-1/OI-2): a missing or mismatched state is
    rejected outright rather than merely logged.
    """
    def _done(resp: RedirectResponse) -> RedirectResponse:
        resp.delete_cookie(_STATE_COOKIE, path="/google")
        resp.delete_cookie(_VERIFIER_COOKIE, path="/google")
        return resp

    params = request.query_params
    if params.get("error"):
        return _done(_ui_redirect(False, reason=params.get("error")))
    code = params.get("code")
    if not code:
        return _done(_ui_redirect(False, reason="no_code"))

    # OI-1/OI-2: strict CSRF state check against the per-request cookie set on
    # /connect. Reject (don't just warn) on any absence or mismatch.
    cookie_state = request.cookies.get(_STATE_COOKIE)
    returned_state = params.get("state")
    if not cookie_state or not returned_state or returned_state != cookie_state:
        logger.warning("google.callback_state_mismatch — rejecting")
        return _done(_ui_redirect(False, reason="state_mismatch"))

    try:
        from google_auth_oauthlib.flow import Flow

        # PKCE: replay the verifier minted on /connect (see _VERIFIER_COOKIE). Pass
        # it at construction AND disable autogeneration — a fresh Flow would other-
        # wise leave code_verifier None and post no verifier at all, which is what
        # Google rejected as invalid_grant: Missing code verifier.
        flow = Flow.from_client_config(
            _client_config(),
            scopes=google_auth._SCOPES,
            redirect_uri=_REDIRECT_URI,
            code_verifier=request.cookies.get(_VERIFIER_COOKIE),
            autogenerate_code_verifier=False,
        )
        # Fetch token using authorization code
        try:
            flow.fetch_token(code=code)
        except Exception:
            flow.fetch_token(authorization_response=str(request.url))

        creds = flow.credentials
        refresh_token = getattr(creds, "refresh_token", None)
        if not refresh_token:
            # Google only returns a refresh token with prompt=consent + offline; if
            # it's missing the account was already consented without offline access.
            logger.error("google.callback_no_refresh_token")
            return _done(_ui_redirect(False, reason="no_refresh_token"))
        google_auth.store_refresh_token(refresh_token)
        logger.info("google.connected: stored new refresh token and reset service cache.")
        return _done(_ui_redirect(True))
    except Exception as exc:  # noqa: BLE001
        logger.exception("google.callback_failed: %s", exc)
        import urllib.parse
        err_msg = urllib.parse.quote(f"{type(exc).__name__}_{str(exc)}"[:80])
        return _done(_ui_redirect(False, reason=f"callback_{err_msg}"))


# ═══════════════════════════════════════════════════════════════════════════════
# POST /google/disconnect
# ═══════════════════════════════════════════════════════════════════════════════
@router.post("/disconnect")
async def google_disconnect(user_id: str = Depends(get_current_user)):
    """Drop the stored token and suppress the env fallback (see google_auth). The
    UI only checks res.ok; a follow-up /status will then read disconnected.

    Requires a signed-in user. Acts on the SHARED connection this pass (deferred:
    per-user Google). /connect and /callback are full-page browser navigations
    that cannot carry an Authorization: Bearer header, so /connect is instead
    authorized by a single-use connect ticket (see google_connect) and /callback
    by the CSRF state cookie that ticket's redirect sets.
    """
    try:
        google_auth.clear_stored_refresh_token()
        return JSONResponse({"ok": True})
    except Exception as exc:  # noqa: BLE001
        logger.exception("google.disconnect_failed")
        return JSONResponse({"ok": False, "detail": "Failed to disconnect."}, status_code=500)
