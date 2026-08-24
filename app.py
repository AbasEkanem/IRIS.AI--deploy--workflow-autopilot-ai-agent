"""app.py — FastAPI assembly and entry point for the IRIS Slack service.

This is the async home the durable Human-in-the-Loop path needs. On startup it
builds the IRIS agent INSIDE the running event loop via ``acreate_iris_agent()``,
so the agent's checkpointer is an async-native durable saver:

  • a sync saver (SqliteSaver/PostgresSaver) raises ``NotImplementedError`` under
    ``ainvoke`` — the exact regression that used to crash every Slack message;
  • an async saver cannot be built at import time (it binds to the running loop),
    so it must be constructed here in the lifespan, not at module import.

The agent is attached to ``app.state.iris_agent`` — exactly where
``slack_webook.py`` resolves it, on both the initial draft/invoke and the
``Command(resume=...)`` approval path. Because it is ONE shared instance holding
ONE durable checkpointer, an interrupt raised on the first invoke persists and
can be resumed by a later button click on the same thread_id.

Run:
    project_venv/Scripts/python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000
or:
    project_venv/Scripts/python.exe app.py
"""

from __future__ import annotations

# Load .env BEFORE importing modules that read os.getenv at import time
# (slack_webook.py binds its SLACK_* config at module top).
from dotenv import load_dotenv

load_dotenv()

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from IRIS import acreate_iris_agent
from checkpointer import close_async_checkpointer
from agent_memory import close_async_store
from idempotency import _get_async_redis
from recovery import recover_crashed_runs
from slack_webook import router as slack_router
from web_api import router as web_router, limiter as web_limiter
from google_oauth import router as google_router

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the agent in-loop (async-safe + durable) and attach it where the
    Slack webhook looks for it; release the DB connections on shutdown."""
    logger.info("iris.startup: building async IRIS agent…")
    app.state.iris_agent = await acreate_iris_agent()
    logger.info("iris.startup: agent ready — HITL gate active on irreversible tools.")

    # OI-15: one consolidated line naming the ACTUAL resolved durable backends
    # (checkpointer + memory store). checkpointer.py / agent_memory.py log each
    # selection attempt (and any fall-through) as it happens; this reports the
    # winner so an operator sees at a glance whether Postgres/SQLite/in-memory is
    # live — the signal that was previously split across a WARNING + a later INFO.
    _cp = type(getattr(app.state.iris_agent, "checkpointer", None)).__name__
    _store = type(getattr(app.state.iris_agent, "store", None)).__name__
    logger.info("iris.startup: durable backends — checkpointer=%s store=%s", _cp, _store)

    # OI-7: probe Redis once at startup so the idempotency dedup layer's health is
    # visible in the boot log (it otherwise only warns lazily on first tool use).
    # Wrapped so a dead/slow Redis can never block or fail startup — idempotency is
    # fail-open by design (a missing Redis loses dedup, not availability).
    try:
        _redis = await _get_async_redis()
        if _redis is not None and await _redis.ping():
            logger.info("iris.startup: redis reachable — idempotency dedup active.")
        else:
            logger.warning(
                "iris.startup: redis client unavailable — idempotency running fail-open (no dedup)."
            )
    except Exception as exc:  # noqa: BLE001 — a Redis probe must never break startup
        logger.warning(
            "iris.startup: redis unreachable (%s) — idempotency running fail-open (no dedup).", exc
        )
    # Fire-and-forget crash-recovery sweep: resume any slack-* thread that was
    # mid-run when a previous process died. Non-blocking so /health readiness is
    # not delayed; the task self-guards and never raises into the loop. Keep a
    # reference so it isn't garbage-collected mid-flight (asyncio holds only a weak
    # ref to bare tasks).
    app.state.recovery_task = asyncio.create_task(recover_crashed_runs(app.state.iris_agent))
    try:
        yield
    finally:
        logger.info("iris.shutdown: closing async checkpointer + memory store…")
        await close_async_checkpointer()
        await close_async_store()
        logger.info("iris.shutdown: done.")


app = FastAPI(title="IRIS Service", lifespan=lifespan)

# CORS — the UI sends `credentials:"include"`, so allowed origins must be EXPLICIT
# (a "*" wildcard is illegal alongside credentials). In production the UI is served
# same-origin (NEXT_PUBLIC_API_URL="") and this is a no-op; it matters only for
# local dev, where the UI runs on :3000 and this API on :8000. Comma-split the env
# var to allow several origins.
_origins = [o.strip() for o in os.getenv("WEB_UI_ORIGIN", "http://localhost:3000").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(slack_router)   # Slack webhook + interactions (untouched)
app.include_router(web_router)     # /ask, /resume, /api/* — the streaming UI surface
app.include_router(google_router)  # /google/* — per-user Google connect flow

# Rate limiting (OI-4): register the shared limiter + its 429 handler. The limit
# is applied per-route via @limiter.limit on /ask and /resume (web_api.py). slowapi
# exempts decorated routes from SlowAPIMiddleware and lets the decorator enforce at
# call time — after get_current_user has set request.state.user_id — so the bucket
# keys on the authenticated user, and no middleware is needed.
app.state.limiter = web_limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.get("/health")
async def health():
    """Liveness + component readiness.

    Always HTTP 200 — this is a status report for uptime checks / operators, not a
    gate. A ``degraded`` Redis or a checkpointer that fell back to in-memory are
    non-fatal by design (both fail-open), so they are reported, not raised.
    Unauthenticated (per auth.py) and secret-free.
    """
    agent = getattr(app.state, "iris_agent", None)

    # Checkpointer probe: a cheap aget_state read on a sentinel thread id exercises
    # the durable backend's read path without mutating anything (aget_state never
    # writes a checkpoint). Guarded — an unreachable DB reports "error", not a 500.
    checkpointer = "unknown"
    if agent is not None:
        try:
            await agent.aget_state({"configurable": {"thread_id": "__health_probe__"}})
            checkpointer = "ok"
        except Exception:  # noqa: BLE001 — health must never raise
            checkpointer = "error"

    # Redis probe: reuse the idempotency layer's async client. "degraded" (not
    # "error") because idempotency is fail-open — a down Redis loses dedup only.
    redis_status = "degraded"
    try:
        _redis = await _get_async_redis()
        if _redis is not None and await _redis.ping():
            redis_status = "ok"
    except Exception:  # noqa: BLE001
        redis_status = "degraded"

    return {
        "ok": True,
        "agent_ready": agent is not None,
        "checkpointer": checkpointer,
        "redis": redis_status,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
