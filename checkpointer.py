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

from durability import enforce_durable, record_backend

logger = logging.getLogger(__name__)


def _settle(label: str, saver: Any, *, is_async: bool) -> Any:
    """Cache the saver, publish the rung it landed on, then hand it back.

    Every ``return`` in both builders goes through here, so the rung reported by
    ``/health`` can never drift from the saver actually in use, and
    ``IRIS_REQUIRE_DURABLE`` gets its chance to abort startup at the exact moment
    the degradation happens rather than after the app is already serving.

    The cache assignment lives here too: if ``enforce_durable`` raises, the module
    global is cleared again, so a caller that catches the error cannot then be
    handed the very lossy saver we just refused.
    """
    global _BUILT, _ABUILT
    if is_async:
        _ABUILT = saver
    else:
        _BUILT = saver
    record_backend("checkpointer", label)
    try:
        enforce_durable("checkpointer", label)
    except Exception:
        if is_async:
            _ABUILT = None
        else:
            _BUILT = None
        raise
    return saver

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


def _pg_dsn_from_env() -> str:
    """The Postgres DSN, stripped of surrounding whitespace.

    The strip is not cosmetic. A DSN pasted into a hosting dashboard very easily
    carries a trailing newline, and it survives into the environment — where it
    lands on the END of the connection string, i.e. inside the database name.
    Postgres then rejects the connection with ``FATAL: database "postgres\\n" does
    not exist``, which reads like a missing database rather than a stray byte, and
    the whole chain degrades to SQLite over one invisible character. Measured on
    Railway: this cost a production deploy.
    """
    return (os.getenv("IRIS_CHECKPOINT_DB_URL") or os.getenv("SUPABASE_DB_URL", "")).strip()


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
        return _settle("memory", MemorySaver(), is_async=False)

    # 1. Postgres (explicit override or Supabase DSN already in .env).
    pg_dsn = _pg_dsn_from_env()
    if backend in ("auto", "postgres") and pg_dsn and _looks_like_postgres(pg_dsn):
        saver = _build_postgres(pg_dsn)
        if saver is not None:
            return _settle("postgres", saver, is_async=False)

    # 2. SQLite (durable across restarts, no external service needed).
    if backend in ("auto", "sqlite"):
        sqlite_path = os.getenv("IRIS_CHECKPOINT_DB_PATH", "iris_checkpoints.sqlite")
        saver = _build_sqlite(sqlite_path)
        if saver is not None:
            return _settle("sqlite", saver, is_async=False)

    # 3. Last resort — in-process (state lost on restart).
    logger.warning(
        "checkpointer: no durable backend available — falling back to in-process "
        "MemorySaver. Per-thread state will NOT survive a restart. Set "
        "IRIS_CHECKPOINT_DB_URL (Postgres) or IRIS_CHECKPOINT_DB_PATH (SQLite) "
        "for durability."
    )
    return _settle("memory", MemorySaver(), is_async=False)


def close_checkpointer() -> None:
    """Close any open DB connections held for the durable checkpointer."""
    global _BUILT
    _STACK.close()
    _BUILT = None


# ── Async durable checkpointer (ainvoke / Slack-webhook path) ────────────────
# Pool sizing. Small by default because Supabase's pooler enforces its own client
# ceiling and this process is one of several sharing it; raise only with that limit
# in view. min_size=1 keeps a warm connection so the first request pays no connect
# latency; max_size bounds the fan-out from concurrent /ask streams plus the
# startup recovery sweep.
_PG_POOL_MIN = int(os.getenv("IRIS_PG_POOL_MIN", "1"))
_PG_POOL_MAX = int(os.getenv("IRIS_PG_POOL_MAX", "10"))
# How long a caller waits for a free connection before failing. Bounded so a
# saturated pool surfaces as an error the retry layers can act on, rather than a
# request that hangs until the stream ceiling.
_PG_POOL_TIMEOUT = float(os.getenv("IRIS_PG_POOL_TIMEOUT", "30"))


async def _build_async_pool(dsn: str) -> Any | None:
    """An opened, self-healing ``AsyncConnectionPool`` for ``dsn``, or None.

    WHY A POOL AND NOT ``from_conn_string`` — this is the fix for a total
    production outage, not a tuning preference.

    ``AsyncPostgresSaver.from_conn_string()`` opens exactly ONE ``AsyncConnection``
    and holds it for the process lifetime. A single psycopg connection carries a
    single protocol stream, so it can only have one command in flight and has no
    way to abandon one. When a coroutine is CANCELLED mid-query the reply is never
    read, and the connection is left permanently mid-command. Every later use —
    from any request — then fails with:

        OperationalError: sending prepared query failed: another command is
                          already in progress
        OperationalError: failed to enter pipeline mode        (adelete_thread)

    Cancellation is routine here, not an edge case: a browser tab closing aborts
    the SSE response, web_api's wall-clock ceiling calls ``agen.aclose()`` on a live
    run (web_api.py:1192), and ``/ask`` can re-attach while a previous stream is
    still unwinding. Any one of those wedges the shared connection — and with
    ``IRIS_REQUIRE_DURABLE=1`` there is no rung to fall back to, so ``/health``
    kept reporting ``durable: true`` with ``checkpointer: "error"`` while every
    ``/ask``, history and status read 5xx'd until the next deploy. That is exactly
    what was observed live.

    A pool fixes both halves:

    * CONCURRENCY — each waiter gets its own connection, so two in-flight
      operations no longer contend for one protocol stream. (The saver's internal
      ``asyncio.Lock`` serialised them before, which merely made this slow; it
      could not make a cancelled query readable again.)
    * SELF-HEALING — ``check=AsyncConnectionPool.check_connection`` validates a
      connection on checkout and DISCARDS a broken one, replacing it with a fresh
      one. A wedged connection costs one request, not the process.

    ``kwargs`` mirrors what ``from_conn_string`` sets on its own connection
    (aio.py:81-82), because the saver's SQL depends on all three:
    ``row_factory=dict_row`` (it reads rows by column name), ``autocommit=True``
    (it manages its own transactions), and ``prepare_threshold=0`` (no server-side
    prepared statements — mandatory through a transaction-mode pooler, which
    multiplexes one server connection across clients and cannot honour a prepared
    statement created on another).
    """
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
    except ImportError:
        logger.warning(
            "checkpointer(async): psycopg / psycopg-pool unavailable — cannot build a "
            "connection pool; falling back to a single connection."
        )
        return None
    try:
        pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=_PG_POOL_MIN,
            max_size=_PG_POOL_MAX,
            timeout=_PG_POOL_TIMEOUT,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            # The self-healing behaviour described above. Without it the pool would
            # happily hand back the same wedged connection it was given.
            check=AsyncConnectionPool.check_connection,
            # open=False + an explicit await: constructing an already-open pool in
            # __init__ is deprecated in psycopg-pool 3.2+, and opening here means a
            # bad DSN fails inside this try instead of at first use.
            open=False,
        )
        await _ASTACK.enter_async_context(pool)
        # wait=True so an unreachable database is a startup failure we can report and
        # fall through from, rather than a pool that looks fine and errors later.
        await pool.open(wait=True, timeout=_PG_POOL_TIMEOUT)
        logger.info(
            "checkpointer(async): Postgres pool opened (min=%d max=%d timeout=%.0fs; "
            "broken connections are checked and replaced on checkout).",
            _PG_POOL_MIN, _PG_POOL_MAX, _PG_POOL_TIMEOUT,
        )
        return pool
    except Exception as exc:
        logger.warning(
            "checkpointer(async): Postgres pool init failed (%s) — trying a single connection.",
            exc,
        )
        return None
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

    # Preferred: a pool. See _build_async_pool for why this is not optional.
    # AsyncPostgresSaver takes an AsyncConnectionPool directly as `conn` — that is
    # what its `_ainternal.Conn` union and `_ainternal.get_connection` are for.
    pool = await _build_async_pool(dsn)
    if pool is not None:
        try:
            saver = AsyncPostgresSaver(conn=pool)
            await saver.setup()  # idempotent: creates checkpoint tables if missing
            logger.info("checkpointer(async): using durable AsyncPostgresSaver (pooled).")
            return saver
        except Exception as exc:
            logger.warning(
                "checkpointer(async): pooled saver setup failed (%s) — trying a single connection.",
                exc,
            )

    # Fallback: the original single-connection path. Kept because a degraded-but-
    # durable checkpointer beats refusing to start (with IRIS_REQUIRE_DURABLE=1 a
    # fall-through to SQLite is a hard startup failure), and a pool can be
    # unavailable for reasons that do not affect a plain connection at all.
    try:
        saver = await _ASTACK.enter_async_context(AsyncPostgresSaver.from_conn_string(dsn))
        await saver.setup()  # idempotent: creates checkpoint tables if missing
        logger.warning(
            "checkpointer(async): using AsyncPostgresSaver on a SINGLE connection — a "
            "cancelled request can wedge it ('another command is already in progress'). "
            "Investigate why the pool could not be created."
        )
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
        return _settle("memory", MemorySaver(), is_async=True)

    # 1. Postgres (explicit override or Supabase DSN already in .env).
    pg_dsn = _pg_dsn_from_env()
    if backend in ("auto", "postgres") and pg_dsn and _looks_like_postgres(pg_dsn):
        saver = await _build_async_postgres(pg_dsn)
        if saver is not None:
            return _settle("postgres", saver, is_async=True)

    # 2. SQLite (durable across restarts, no external service needed).
    if backend in ("auto", "sqlite"):
        sqlite_path = os.getenv("IRIS_CHECKPOINT_DB_PATH", "iris_checkpoints.sqlite")
        saver = await _build_async_sqlite(sqlite_path)
        if saver is not None:
            return _settle("sqlite", saver, is_async=True)

    # 3. Last resort — in-process (state lost on restart).
    logger.warning(
        "checkpointer(async): no durable backend available — falling back to "
        "in-process MemorySaver. Per-thread state will NOT survive a restart. Set "
        "IRIS_CHECKPOINT_DB_URL (Postgres) or IRIS_CHECKPOINT_DB_PATH (SQLite) "
        "for durability."
    )
    return _settle("memory", MemorySaver(), is_async=True)


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
