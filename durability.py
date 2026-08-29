"""durability.py — one place that knows which storage rung IRIS actually landed on.

Why this exists
---------------
``checkpointer.py`` and ``agent_memory.py`` each resolve a backend through the same
shape of chain — Postgres → SQLite → in-process — and each *silently* degrades one
rung on failure. That degradation is invisible from the outside and produces a bug
that is indistinguishable from a product defect: on Railway, SQLite lives on a
container filesystem that is wiped on every deploy, so "IRIS forgot my chat history"
and "Supabase was briefly unreachable at boot" look exactly the same to a user, and
nothing in the API surface tells them apart.

So two things live here:

* ``record_backend()`` / ``resolved_backends()`` — the builders report the rung they
  landed on, and ``/health`` publishes it. Diagnosing a degraded deploy becomes one
  HTTP call instead of a log archaeology session.
* ``require_durable()`` + ``enforce_durable()`` — with ``IRIS_REQUIRE_DURABLE=1``, a
  process that cannot reach Postgres raises at startup instead of booting into a
  state where it accepts conversations it will lose. Off by default so local dev
  (where Postgres genuinely is unreachable) keeps working untouched.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

logger = logging.getLogger(__name__)

Kind = Literal["checkpointer", "store"]

# "postgres" is the only rung that survives a Railway redeploy. "sqlite" is durable
# across a restart but NOT across a deploy on ephemeral container storage, and
# "memory" does not survive the process.
DURABLE_LABELS = ("postgres",)

_RESOLVED: dict[str, str] = {}

_TRUTHY = ("1", "true", "yes", "on", "require", "required")


def require_durable() -> bool:
    """True when this process must refuse to run without Postgres."""
    return os.getenv("IRIS_REQUIRE_DURABLE", "").strip().lower() in _TRUTHY


def record_backend(kind: Kind, label: str) -> None:
    """Record the rung a builder landed on: 'postgres' | 'sqlite' | 'memory'."""
    _RESOLVED[kind] = label
    if label in DURABLE_LABELS:
        logger.info("durability: %s resolved to %s", kind, label)
    else:
        # WARNING, not INFO: on a production host this is data loss waiting to be
        # noticed, even when it is the correct local-dev outcome.
        logger.warning(
            "durability: %s resolved to %s — this does NOT survive a redeploy on "
            "ephemeral storage. Set IRIS_REQUIRE_DURABLE=1 to fail fast instead.",
            kind, label,
        )


def resolved_backends() -> dict[str, str]:
    """What each subsystem is actually using right now (for /health)."""
    return dict(_RESOLVED)


def all_durable() -> bool:
    """True only when every recorded backend is on a durable rung."""
    return bool(_RESOLVED) and all(v in DURABLE_LABELS for v in _RESOLVED.values())


def enforce_durable(kind: Kind, label: str) -> None:
    """Raise when ``IRIS_REQUIRE_DURABLE`` is set and this rung is not durable.

    Called by each builder immediately after it records its rung, so the failure
    surfaces during FastAPI lifespan startup — before the app can accept a single
    request whose history it would silently drop.
    """
    if label in DURABLE_LABELS or not require_durable():
        return
    raise RuntimeError(
        f"IRIS_REQUIRE_DURABLE is set but the {kind} resolved to '{label}' instead of "
        f"Postgres. Refusing to start: conversations and memories written now would be "
        f"lost on the next deploy. Check SUPABASE_DB_URL / "
        f"IRIS_{'CHECKPOINT' if kind == 'checkpointer' else 'STORE'}_DB_URL reachability, "
        f"or unset IRIS_REQUIRE_DURABLE to accept a degraded backend."
    )
