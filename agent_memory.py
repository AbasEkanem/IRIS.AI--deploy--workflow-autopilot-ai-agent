from __future__ import annotations
from deepagents.backends import( 
    CompositeBackend as _router,
    StoreBackend as _persistent_memory,
    FilesystemBackend as _filesystem_backend
)
from langgraph.store.memory import InMemoryStore
from pathlib import Path
import hashlib
import logging
import re

logger = logging.getLogger(__name__)

# create the instance of the InMemoryStore
memory_store = InMemoryStore()


# langgraph's BaseStore._validate_namespace (store/base/__init__.py) rejects any
# namespace label containing a period, and reserves "langgraph" as the root label.
# Our per-user memory namespace is ("memory", iris_id, user_id) where user_id is the
# signed-in user's EMAIL — whose domain ALWAYS contains dots — so the raw email is an
# illegal label and persisting any per-user memory would raise InvalidNamespaceError.
# _safe_label maps an arbitrary identity to a legal label: every char outside
# [A-Za-z0-9_-] becomes '_' (keeps it readable), then a short stable hash of the
# ORIGINAL value is appended so the mapping stays INJECTIVE — two distinct users can
# never collide onto the same namespace. Collision-freeness here is a security
# property (the per-user memory isolation boundary), not just cosmetic hygiene.
_UNSAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_label(value: str) -> str:
    raw = str(value)
    cleaned = _UNSAFE_LABEL_RE.sub("_", raw) or "x"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{cleaned}_{digest}"


def create_memory_namespace(runtime) -> tuple[str, ...]:
    context = getattr(runtime, "context", None)

    if context is None:
        logger.warning(
            "create_memory_namespace: no context on Runtime — falling back to "
            "shared 'iris_default'/'user_default' namespace. Persistent "
            "memory will NOT be isolated per user until the caller passes "
            "context={'iris_id': ..., 'user_id': ...} to agent.invoke()."
        )
        return ("memory", "iris_default", "user_default")
    if isinstance(context, dict):
        iris_id = context.get("iris_id", "iris_default")
        user_id = context.get("user_id", "user_default")
    else:
        iris_id = getattr(context, "iris_id", "iris_default")
        user_id = getattr(context, "user_id", "user_default")

    return ("memory", _safe_label(iris_id), _safe_label(user_id))


# Project root directory for IRIS workspace and resources
project_root = Path(__file__).parent.resolve()


# create iris's memory + filesystem composite backend
def create_iris_composite_backend(execution_dir: Path | None = None):
    """
    Build IRIS composite memory backend spanning project root and persistent memories.
    """
    root_dir = Path(execution_dir).resolve() if execution_dir else project_root
    workspace_dir = root_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    return _router(
        default=_filesystem_backend(root_dir=root_dir, virtual_mode=True),
        routes={
            "/memories/": _persistent_memory(
                namespace=create_memory_namespace,
            )
        },
    )


# instantiated backend for direct use by the harness middleware
memory_backend = create_iris_composite_backend()


# ── Durable async memory store (per-user memories that survive restarts) ──────
# The module-level `memory_store` above is an InMemoryStore: fast, but everything
# in it vanishes on every process restart. Once memory is namespaced per user
# (create_memory_namespace reads runtime.context), "real" per-user memory should
# also be DURABLE. This factory mirrors checkpointer.build_async_checkpointer()
# exactly: it returns an async-native store bound to the running loop —
# AsyncPostgres → AsyncSqlite → InMemory — with the same env-driven selection and
# fail-safe fallback (a misconfigured/unreachable DB degrades to InMemoryStore
# with a loud warning rather than crashing agent assembly).
#
# StoreBackend (deepagents/backends/store.py) resolves the store at call time via
# get_store(), i.e. whatever is passed to create_deep_agent(store=...). So passing
# the store built here to the agent (IRIS.acreate_iris_agent) fully takes effect —
# the per-user namespace then lands in a durable backend.
#
# Async stores bind to the running event loop at construction, so build_async_store()
# MUST be awaited from inside the loop (the FastAPI lifespan / acreate_iris_agent),
# never at sync import time. Connections stay open for the process lifetime on an
# AsyncExitStack, released by close_async_store() from the async shutdown hook. The
# store's SQLite file is kept SEPARATE from the checkpointer's (iris_store.sqlite vs
# iris_checkpoints.sqlite) — different data, different schema.
import os as _os
import contextlib as _contextlib

from durability import enforce_durable as _enforce_durable, record_backend as _record_backend

# Pool settings are SHARED with the checkpointer rather than re-declared, so one
# env change moves both and they can never drift into different ceilings against
# the same Supabase pooler. Imported from checkpointer.py because that is where the
# failure they fix was diagnosed (see checkpointer._build_async_pool).
from checkpointer import (
    _PG_POOL_MAX,
    _PG_POOL_MIN,
    _PG_POOL_TIMEOUT,
)

try:
    from psycopg_pool import AsyncConnectionPool as _AsyncConnectionPool
except ImportError:  # pragma: no cover — psycopg-pool is pinned, but never hard-fail
    _AsyncConnectionPool = None  # type: ignore[assignment]

_ASTORE_STACK = _contextlib.AsyncExitStack()
_ABUILT_STORE = None


def _settle_store(label: str, store):
    """Cache the store, publish the rung it landed on, then hand it back.

    Twin of ``checkpointer._settle``. Every ``return`` in ``build_async_store()``
    goes through here so ``/health`` reports the store actually in use, and
    ``IRIS_REQUIRE_DURABLE`` can abort startup rather than let IRIS accept facts
    into a store that a redeploy will wipe. On a refusal the cache is cleared, so
    a caller that swallows the error cannot then be handed the rejected store.
    """
    global _ABUILT_STORE
    _ABUILT_STORE = store
    _record_backend("store", label)
    try:
        _enforce_durable("store", label)
    except Exception:
        _ABUILT_STORE = None
        raise
    return store


def _looks_like_postgres(dsn: str) -> bool:
    return dsn.startswith("postgres://") or dsn.startswith("postgresql://")


async def _build_async_postgres_store(dsn: str):
    """Try to build an async Postgres store. Returns None if unavailable.

    POOLED, for the same reason as the checkpointer — see
    ``checkpointer._build_async_pool`` for the full account. In short:
    ``from_conn_string`` holds ONE ``AsyncConnection``, a cancelled coroutine
    leaves that connection permanently mid-command, and every later query then
    fails with ``another command is already in progress``. Cancellation is routine
    on this service (aborted SSE streams, the wall-clock ceiling calling
    ``agen.aclose()``), so the shared connection gets wedged and stays wedged.

    The store is on exactly the same failure path as the checkpointer: it backs the
    per-user memory namespace AND the thread index that the sidebar reads
    (thread_index.py), so a wedged store connection is what makes "IRIS lost my
    conversation list" appear alongside the checkpoint errors.

    ``AsyncPostgresStore`` accepts a pool as ``conn`` (its ``from_conn_string``
    builds one itself when given ``pool_config``), so this passes one explicitly —
    with ``check_connection`` on checkout, which is the part that makes a broken
    connection cost one request instead of the process.
    """
    try:
        from langgraph.store.postgres.aio import AsyncPostgresStore
    except ImportError:
        logger.warning("store(async): langgraph-checkpoint-postgres not installed — skipping Postgres store.")
        return None

    # Preferred: pooled. `pool_config` is the store's own supported way in, and it
    # applies the same autocommit / prepare_threshold=0 / dict_row connection kwargs
    # the store's SQL requires (store/postgres/aio.py:199-212) — including the
    # prepare_threshold=0 that a transaction-mode pooler like Supabase's demands.
    if _AsyncConnectionPool is not None:
        try:
            store = await _ASTORE_STACK.enter_async_context(
                AsyncPostgresStore.from_conn_string(
                    dsn,
                    pool_config={
                        "min_size": _PG_POOL_MIN,
                        "max_size": _PG_POOL_MAX,
                        "timeout": _PG_POOL_TIMEOUT,
                        # Discard and replace a connection that is no longer usable —
                        # the self-healing half of the fix.
                        "check": _AsyncConnectionPool.check_connection,
                    },
                )
            )
            await store.setup()  # idempotent: creates store tables if missing
            logger.info(
                "store(async): using durable AsyncPostgresStore (pooled, min=%d max=%d).",
                _PG_POOL_MIN, _PG_POOL_MAX,
            )
            return store
        except Exception as exc:
            logger.warning(
                "store(async): pooled Postgres store init failed (%s) — trying a single connection.", exc
            )
    else:
        logger.warning("store(async): psycopg-pool unavailable — cannot pool the store connection.")

    # Fallback: single connection. Same reasoning as the checkpointer's — with
    # IRIS_REQUIRE_DURABLE=1, falling through to SQLite is a hard startup failure,
    # so a degraded-but-durable store is the better outcome. Logged at WARNING
    # because this connection can be wedged by one cancelled request.
    try:
        store = await _ASTORE_STACK.enter_async_context(AsyncPostgresStore.from_conn_string(dsn))
        await store.setup()
        logger.warning(
            "store(async): using AsyncPostgresStore on a SINGLE connection — a cancelled "
            "request can wedge it. Investigate why the pool could not be created."
        )
        return store
    except Exception as exc:  # unreachable DB, bad DSN, auth failure, etc.
        logger.warning("store(async): Postgres init failed (%s) — falling through to next option.", exc)
        return None


async def _build_async_sqlite_store(path: str):
    """Try to build an async SQLite store. Returns None if unavailable."""
    try:
        from langgraph.store.sqlite.aio import AsyncSqliteStore
    except ImportError:
        logger.warning("store(async): langgraph-checkpoint-sqlite not installed — skipping SQLite store.")
        return None
    try:
        store = await _ASTORE_STACK.enter_async_context(AsyncSqliteStore.from_conn_string(path))
        with _contextlib.suppress(Exception):
            await store.setup()  # safe to call repeatedly
        logger.info("store(async): using durable AsyncSqliteStore at %s", path)
        return store
    except Exception as exc:
        logger.warning("store(async): SQLite init failed (%s) — falling back to InMemoryStore.", exc)
        return None


async def build_async_store():
    """Async-native durable memory store, built INSIDE the running event loop.

    Selection order mirrors checkpointer.build_async_checkpointer():
    AsyncPostgres (IRIS_STORE_DB_URL / SUPABASE_DB_URL) → AsyncSqlite
    (IRIS_STORE_DB_PATH, default ./iris_store.sqlite) → InMemoryStore. Set
    IRIS_STORE_BACKEND=memory to force the in-process store.

    Cached: the first successfully built store is reused for the process so we
    don't open a second DB connection per agent (re)build. MUST be awaited from
    within a running loop; constructing an async store at import time raises
    ``RuntimeError: no running event loop``.
    """
    global _ABUILT_STORE
    if _ABUILT_STORE is not None:
        return _ABUILT_STORE

    backend = _os.getenv("IRIS_STORE_BACKEND", "auto").strip().lower()

    # Explicit opt-out — InMemoryStore is async-safe and loop-agnostic.
    if backend == "memory":
        logger.info("store(async): IRIS_STORE_BACKEND=memory — using in-process InMemoryStore.")
        return _settle_store("memory", InMemoryStore())

    # 1. Postgres (explicit override or the Supabase DSN already in .env).
    # .strip() is load-bearing: a DSN pasted into a hosting dashboard easily carries
    # a trailing newline, which lands inside the database name and gets rejected as
    # FATAL: database "postgres\n" does not exist — an obscure way to lose durability
    # over one invisible byte. Measured on Railway. Mirrors checkpointer._pg_dsn_from_env.
    pg_dsn = (_os.getenv("IRIS_STORE_DB_URL") or _os.getenv("SUPABASE_DB_URL", "")).strip()
    if backend in ("auto", "postgres") and pg_dsn and _looks_like_postgres(pg_dsn):
        store = await _build_async_postgres_store(pg_dsn)
        if store is not None:
            return _settle_store("postgres", store)

    # 2. SQLite (durable across restarts, no external service needed).
    if backend in ("auto", "sqlite"):
        sqlite_path = _os.getenv("IRIS_STORE_DB_PATH", "iris_store.sqlite")
        store = await _build_async_sqlite_store(sqlite_path)
        if store is not None:
            return _settle_store("sqlite", store)

    # 3. Last resort — in-process (memories lost on restart).
    logger.warning(
        "store(async): no durable backend available — falling back to in-process "
        "InMemoryStore. Per-user memories will NOT survive a restart. Set "
        "IRIS_STORE_DB_URL (Postgres) or IRIS_STORE_DB_PATH (SQLite) for durability."
    )
    return _settle_store("memory", InMemoryStore())


async def close_async_store():
    """Close async DB connections held for the durable memory store.

    Call from the async shutdown hook (FastAPI lifespan). Not atexit-registered:
    closing an async connection requires a running loop.
    """
    global _ABUILT_STORE
    await _ASTORE_STACK.aclose()
    _ABUILT_STORE = None





from langchain_core.tools import tool
from langgraph.store.base import BaseStore
from langgraph.runtime import get_runtime

@tool
async def remember_user_fact(key: str, value: str) -> str:
    """Save a durable fact about the user (e.g. preferences, name, favorite color)
    that should be remembered across all future conversations."""
    runtime = get_runtime()
    store: BaseStore = runtime.store
    namespace = create_memory_namespace(runtime)  # already handles user_id scoping
    await store.aput(namespace, key, {"value": value})
    return f"Remembered: {key} = {value}"

@tool
async def recall_user_facts(key: str | None = None) -> str:
    """Retrieve previously remembered facts about the user. Pass a specific key,
    or leave empty to list everything remembered."""
    runtime = get_runtime()
    store: BaseStore = runtime.store
    namespace = create_memory_namespace(runtime)
    if key:
        item = await store.aget(namespace, key)
        return str(item.value) if item else f"No memory found for '{key}'"
    items = await store.asearch(namespace)
    return "\n".join(f"{i.key}: {i.value}" for i in items) or "No memories stored yet."