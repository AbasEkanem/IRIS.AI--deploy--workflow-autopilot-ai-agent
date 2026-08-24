"""checkpointer.py — Durable per-thread checkpointer factory for IRIS.

Why this exists
---------------
IRIS.py originally hard-wired ``MemorySaver`` as the LangGraph short-term
checkpointer. ``MemorySaver`` is **in-process**: it keeps every thread's state
in a plain dict, so the moment the process restarts (deploy, crash, scale event)
all per-conversation memory is gone. On the Slack path (slack_webook.py) each
inbound message is a fresh ``ainvoke`` keyed by a per-thread ``thread_id``; with
an in-memory saver a restart silently drops the durable state a long,
multi-step run needs to survive across invocations.

This module provides one factory — ``build_checkpointer()`` — that returns a
durable saver when a database is configured, and only falls back to the
in-memory saver when nothing else is available. The selection is env-driven and
fail-safe: a misconfigured or unreachable database degrades to ``MemorySaver``
with a loud warning rather than crashing agent assembly.

Selection order (first that succeeds wins)
------------------------------------------
1. ``IRIS_CHECKPOINT_BACKEND`` explicitly set to ``memory`` → in-memory (opt-out).
2. Postgres — if ``IRIS_CHECKPOINT_DB_URL`` or ``SUPABASE_DB_URL`` looks like a
   postgres DSN (and langgraph-checkpoint-postgres is installed).
3. SQLite — a local file (``IRIS_CHECKPOINT_DB_PATH``, default
   ``./iris_checkpoints.sqlite``) via langgraph-checkpoint-sqlite.
4. ``MemorySaver`` — last-resort in-process fallback.

Both langgraph-checkpoint-sqlite and langgraph-checkpoint-postgres are already
pinned in requirements.txt, and ``SUPABASE_DB_URL`` already exists in .env, so
the durable path works out of the box without new dependencies.

Sync vs async
-------------
``build_checkpointer()`` returns a **sync** saver (SqliteSaver/PostgresSaver).
Those raise ``NotImplementedError`` on the async methods LangGraph calls during
``await agent.ainvoke(...)`` — so they are only safe for sync ``.invoke`` and for
the LangGraph Platform graph (which supplies its own persistence). The production
Slack webhook drives IRIS via ``ainvoke`` and needs native HITL to persist the
paused state on that same async path, so it must use an **async-native** saver.

``build_async_checkpointer()`` is that async builder: it returns
AsyncPostgres → AsyncSqlite → MemorySaver. Async savers bind to the running event
loop at construction (``AsyncSqliteSaver.__init__`` calls
``asyncio.get_running_loop()``), so it MUST be awaited from inside a running loop
(the FastAPI lifespan / ``acreate_iris_agent``), never at sync import time.

Both builders hold their DB connections open for the process lifetime (a sync
``ExitStack`` / an async ``AsyncExitStack``) and call ``.setup()`` once so the
checkpoint tables exist. Release them at shutdown via ``close_checkpointer()`` /
``await close_async_checkpointer()``.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)

# Holds the open context managers (DB connections) for the process lifetime so
# the saver stays usable after build_checkpointer() returns. Closed atexit or
# via close_checkpointer().
_STACK = contextlib.ExitStack()
_BUILT: Any | None = None

# Async counterparts (for the ainvoke / webhook path). Kept separate from the
# sync stack above: async connections must be opened and closed inside a running
# event loop, so they live on an AsyncExitStack closed from the async shutdown
# hook, never atexit.
_ASTACK = contextlib.AsyncExitStack()
_ABUILT: Any | None = None


def _looks_like_postgres(dsn: str) -> bool:
    return dsn.startswith("postgres://") or dsn.startswith("postgresql://")


def _build_postgres(dsn: str) -> Any | None:
    """Try to build a Postgres checkpointer. Returns None if unavailable."""
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError:
        logger.warning(
            "checkpointer: langgraph-checkpoint-postgres not installed — "
            "cannot use Postgres backend; falling through."
        )
        return None
    try:
        saver = _STACK.enter_context(PostgresSaver.from_conn_string(dsn))
        saver.setup()  # idempotent: creates checkpoint tables if missing
        logger.info("checkpointer: using durable PostgresSaver.")
        return saver
    except Exception as exc:  # unreachable DB, bad DSN, auth failure, etc.
        logger.warning(
            "checkpointer: Postgres init failed (%s) — falling through to next option.",
            exc,
        )
        return None


def _build_sqlite(path: str) -> Any | None:
    """Try to build a SQLite checkpointer. Returns None if unavailable."""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:
        logger.warning(
            "checkpointer: langgraph-checkpoint-sqlite not installed — "
            "cannot use SQLite backend; falling through."
        )
        return None
    try:
        saver = _STACK.enter_context(SqliteSaver.from_conn_string(path))
        # SqliteSaver.setup() is safe to call repeatedly.
        with contextlib.suppress(Exception):
            saver.setup()
        logger.info("checkpointer: using durable SqliteSaver at %s", path)
        return saver
    except Exception as exc:
        logger.warning(
            "checkpointer: SQLite init failed (%s) — falling back to MemorySaver.",
            exc,
        )
        return None


def build_checkpointer() -> Any:
    """Return a durable checkpointer, or MemorySaver as a safe last resort.

    Cached: the first successfully built saver is reused for the process so we
    don't open a second DB connection per agent (re)build.
    """
    global _BUILT
    if _BUILT is not None:
        return _BUILT

    backend = os.getenv("IRIS_CHECKPOINT_BACKEND", "auto").strip().lower()

    # Explicit opt-out — keep the original in-memory behaviour on request.
    if backend == "memory":
        logger.info("checkpointer: IRIS_CHECKPOINT_BACKEND=memory — using in-process MemorySaver.")
        _BUILT = MemorySaver()
        return _BUILT

    # 1. Postgres (explicit override or Supabase DSN already in .env).
    pg_dsn = os.getenv("IRIS_CHECKPOINT_DB_URL") or os.getenv("SUPABASE_DB_URL", "")
    if backend in ("auto", "postgres") and pg_dsn and _looks_like_postgres(pg_dsn):
        saver = _build_postgres(pg_dsn)
        if saver is not None:
            _BUILT = saver
            return _BUILT

    # 2. SQLite (durable across restarts, no external service needed).
    if backend in ("auto", "sqlite"):
        sqlite_path = os.getenv("IRIS_CHECKPOINT_DB_PATH", "iris_checkpoints.sqlite")
        saver = _build_sqlite(sqlite_path)
        if saver is not None:
            _BUILT = saver
            return _BUILT

    # 3. Last resort — in-process (state lost on restart).
    logger.warning(
        "checkpointer: no durable backend available — falling back to in-process "
        "MemorySaver. Per-thread state will NOT survive a restart. Set "
        "IRIS_CHECKPOINT_DB_URL (Postgres) or IRIS_CHECKPOINT_DB_PATH (SQLite) "
        "for durability."
    )
    _BUILT = MemorySaver()
    return _BUILT


def close_checkpointer() -> None:
    """Close any open DB connections held for the durable checkpointer."""
    global _BUILT
    _STACK.close()
    _BUILT = None


# ── Async durable checkpointer (ainvoke / Slack-webhook path) ────────────────
async def _build_async_postgres(dsn: str) -> Any | None:
    """Try to build an async Postgres checkpointer. Returns None if unavailable."""
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError:
        logger.warning(
            "checkpointer(async): langgraph-checkpoint-postgres not installed — "
            "cannot use Postgres backend; falling through."
        )
        return None
    try:
        saver = await _ASTACK.enter_async_context(AsyncPostgresSaver.from_conn_string(dsn))
        await saver.setup()  # idempotent: creates checkpoint tables if missing
        logger.info("checkpointer(async): using durable AsyncPostgresSaver.")
        return saver
    except Exception as exc:  # unreachable DB, bad DSN, auth failure, etc.
        logger.warning(
            "checkpointer(async): Postgres init failed (%s) — falling through to next option.",
            exc,
        )
        return None


async def _build_async_sqlite(path: str) -> Any | None:
    """Try to build an async SQLite checkpointer. Returns None if unavailable."""
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    except ImportError:
        logger.warning(
            "checkpointer(async): langgraph-checkpoint-sqlite not installed — "
            "cannot use SQLite backend; falling through."
        )
        return None
    try:
        saver = await _ASTACK.enter_async_context(AsyncSqliteSaver.from_conn_string(path))
        with contextlib.suppress(Exception):
            await saver.setup()  # safe to call repeatedly
        logger.info("checkpointer(async): using durable AsyncSqliteSaver at %s", path)
        return saver
    except Exception as exc:
        logger.warning(
            "checkpointer(async): SQLite init failed (%s) — falling back to MemorySaver.",
            exc,
        )
        return None


async def build_async_checkpointer() -> Any:
    """Async-native durable checkpointer, built INSIDE the running event loop.

    Mirrors ``build_checkpointer()``'s env-driven selection order but returns
    savers that are safe on the ``await agent.ainvoke(...)`` path:
    AsyncPostgres → AsyncSqlite → MemorySaver. This is the checkpointer the
    resume-capable webhook/HITL path must use.

    MUST be awaited from within a running loop (the FastAPI lifespan /
    ``acreate_iris_agent``); constructing an async saver at import time raises
    ``RuntimeError: no running event loop``.
    """
    global _ABUILT
    if _ABUILT is not None:
        return _ABUILT

    backend = os.getenv("IRIS_CHECKPOINT_BACKEND", "auto").strip().lower()

    # Explicit opt-out — MemorySaver is async-safe and loop-agnostic.
    if backend == "memory":
        logger.info("checkpointer(async): IRIS_CHECKPOINT_BACKEND=memory — using in-process MemorySaver.")
        _ABUILT = MemorySaver()
        return _ABUILT

    # 1. Postgres (explicit override or Supabase DSN already in .env).
    pg_dsn = os.getenv("IRIS_CHECKPOINT_DB_URL") or os.getenv("SUPABASE_DB_URL", "")
    if backend in ("auto", "postgres") and pg_dsn and _looks_like_postgres(pg_dsn):
        saver = await _build_async_postgres(pg_dsn)
        if saver is not None:
            _ABUILT = saver
            return _ABUILT

    # 2. SQLite (durable across restarts, no external service needed).
    if backend in ("auto", "sqlite"):
        sqlite_path = os.getenv("IRIS_CHECKPOINT_DB_PATH", "iris_checkpoints.sqlite")
        saver = await _build_async_sqlite(sqlite_path)
        if saver is not None:
            _ABUILT = saver
            return _ABUILT

    # 3. Last resort — in-process (state lost on restart).
    logger.warning(
        "checkpointer(async): no durable backend available — falling back to "
        "in-process MemorySaver. Per-thread state will NOT survive a restart. Set "
        "IRIS_CHECKPOINT_DB_URL (Postgres) or IRIS_CHECKPOINT_DB_PATH (SQLite) "
        "for durability."
    )
    _ABUILT = MemorySaver()
    return _ABUILT


async def close_async_checkpointer() -> None:
    """Close async DB connections held for the durable async checkpointer.

    Call from the async shutdown hook (FastAPI lifespan). Not atexit-registered:
    closing an async connection requires a running loop.
    """
    global _ABUILT
    await _ASTACK.aclose()
    _ABUILT = None


# Best-effort cleanup if the caller never calls close_checkpointer() explicitly.
atexit.register(close_checkpointer)
