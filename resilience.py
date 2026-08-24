"""resilience.py — transient-error retry at the ainvoke boundary (Part 1).

A network / DB / checkpointer blip mid-run should NOT kill the run. It should
retry the SAME thread so LangGraph replays from the last durable checkpoint —
completed super-steps (and their tool results, restored from pending-writes) are
NOT re-executed. This is the "if the agent is interrupted with a network issue,
it restarts exactly from where it left off" requirement, made real.

Layering (outermost last):
    ChatNVIDIA timeout (loadenv.py)         → a hang becomes a Timeout exception
    ToolRetryMiddleware  (IRIS.py)          → retries a tool's transient failure
    ModelRetryMiddleware (IRIS.py)          → retries a model call's failure
    ainvoke_with_retry   (HERE)             → retries the WHOLE invoke, resuming
                                              from the checkpoint

So this wrapper only ever fires for failures that escape every in-run
middleware: the initial connection never opened, the checkpointer commit blipped,
or the model failed past its middleware budget. Its response — re-invoke the same
thread_id — is the correct one for an invoke-level failure, because the durable
checkpointer means "re-invoke" means "resume".

Idempotency (idempotency.py, Part 3) is the necessary companion: it makes the one
super-step that gets replayed on resume a no-op for any external side effect that
already landed (the crash window between "side effect committed at the API" and
"checkpoint committed locally").

HITL-safe by construction: a top-level interrupt is RETURNED as
``result["__interrupt__"]`` (see slack_webook._pending_actions), not raised, so it
flows back through this wrapper as a normal successful result. We additionally
never catch GraphInterrupt / GraphBubbleUp as a guard.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ── Command detection ────────────────────────────────────────────────────────
# A resume Command (HITL decision) must be re-sent verbatim on retry, never
# swapped for a checkpoint-resume — it carries the approve/reject payload the
# interrupt is waiting on.
try:
    from langgraph.types import Command
except Exception:  # pragma: no cover - defensive
    Command = None  # type: ignore[assignment,misc]

# ── Non-retryable control-flow signals (must always propagate) ────────────────
# These are LangGraph's interrupt/bubble-up signals. In practice a top-level
# interrupt is returned, not raised, but if a future version raises one it must
# reach the caller so the HITL card is posted — never retried away.
_NON_RETRYABLE: tuple[type[BaseException], ...] = ()
for _name in ("GraphInterrupt", "GraphBubbleUp"):
    try:
        from langgraph import errors as _lg_errors  # noqa: PLC0415

        _exc = getattr(_lg_errors, _name, None)
        if isinstance(_exc, type) and issubclass(_exc, BaseException):
            _NON_RETRYABLE += (_exc,)
    except Exception:  # pragma: no cover - defensive
        pass

# ── Transient exception taxonomy (retry only these) ───────────────────────────
# Deliberately NARROW: a real tool error or a programming bug must propagate, not
# be silently retried. Built defensively so a missing optional dependency never
# breaks the import.
_TRANSIENT: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, asyncio.TimeoutError)

try:  # requests (sync ChatNVIDIA transport, Gemini, most HTTP tools)
    from requests.exceptions import RequestException as _ReqExc, Timeout as _ReqTimeout

    _TRANSIENT += (_ReqExc, _ReqTimeout)
except Exception:  # pragma: no cover
    pass

try:  # aiohttp (async ChatNVIDIA transport — the production path)
    from aiohttp.client_exceptions import (
        ServerTimeoutError as _AioServerTimeout,
        SocketTimeoutError as _AioSocketTimeout,
    )

    _TRANSIENT += (_AioServerTimeout, _AioSocketTimeout)
except Exception:  # pragma: no cover
    pass

try:  # aiohttp connection drops (separate import so a rename can't sink the above)
    from aiohttp.client_exceptions import (
        ClientConnectionError as _AioConnErr,
        ClientConnectorError as _AioConnectorErr,
    )

    _TRANSIENT += (_AioConnErr, _AioConnectorErr)
except Exception:  # pragma: no cover
    pass

try:  # httpx (used by some tool clients)
    import httpx as _httpx

    _TRANSIENT += (_httpx.TimeoutException, _httpx.TransportError)
except Exception:  # pragma: no cover
    pass

for _db_mod in ("psycopg", "psycopg2"):  # checkpointer commit blip (Postgres saver)
    try:
        _m = __import__(_db_mod)
        _oe = getattr(_m, "OperationalError", None)
        if isinstance(_oe, type) and issubclass(_oe, BaseException):
            _TRANSIENT += (_oe,)
    except Exception:  # pragma: no cover
        pass

# De-dup while preserving order (some aliases collapse to the same class).
_seen: set[type[BaseException]] = set()
_TRANSIENT = tuple(t for t in _TRANSIENT if not (t in _seen or _seen.add(t)))


def _snap_has_values(snap: Any) -> bool:
    """True if the checkpoint carries any committed state (messages/values)."""
    try:
        vals = getattr(snap, "values", None)
        if isinstance(vals, dict):
            return bool(vals.get("messages") or vals)
        return bool(vals)
    except Exception:  # pragma: no cover - defensive
        return False


async def _resume_input(agent: Any, config: Any, original_input: Any) -> Any:
    """Decide what to feed a RETRY so the run continues from where it left off.

    The rule that makes "resume, don't restart" correct:

      • A resume ``Command`` is idempotent against the interrupted checkpoint
        (its decision is simply unused if already applied), so re-send it as-is.

      • A message input is APPENDED by the ``add_messages`` reducer every time it
        is sent. LangGraph commits the input into the thread's first checkpoint
        *before* running any node, so a failed attempt already recorded it.
        Re-sending would append a DUPLICATE user turn and re-run from scratch —
        the opposite of resuming. So we probe committed state and, when any
        exists, resume with ``None`` (continue the pending tasks exactly from the
        interruption point). Only when NOTHING was committed — the very first
        checkpoint write itself failed — is it safe to re-send the input.

    On a state-probe failure we favour ``None`` (resume-not-duplicate): a lost
    turn is recoverable and visible; a duplicated side-effect-bearing turn is
    worse (and idempotency only guards sends, not re-narration).
    """
    if Command is not None and isinstance(original_input, Command):
        return original_input
    try:
        snap = await agent.aget_state(config)
        has_state = snap is not None and (bool(getattr(snap, "next", None)) or _snap_has_values(snap))
        if has_state:
            return None
        return original_input
    except Exception as exc:
        logger.warning("resilience.state_probe_failed", error=str(exc))
        return None


def _backoff_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    """Equal-jitter exponential backoff: half fixed, half random."""
    ceil = min(max_delay, base_delay * (2 ** attempt))
    return (ceil / 2.0) + random.uniform(0.0, ceil / 2.0)


async def ainvoke_with_retry(
    agent: Any,
    input: Any,
    config: Any | None = None,
    *,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    **invoke_kwargs: Any,
) -> Any:
    """``await agent.ainvoke(input, config=..., **kwargs)`` with transient-retry.

    On a transient failure the SAME thread (``config['configurable']['thread_id']``)
    is re-invoked, which replays from the last durable checkpoint. The first
    attempt uses ``input`` as given; subsequent attempts feed ``_resume_input`` so
    a message input is not re-appended and completed work is not re-run.

    Raises the last transient exception if all attempts fail; propagates any
    non-transient exception (and GraphInterrupt/GraphBubbleUp) immediately.
    """
    tid = None
    try:
        tid = (config or {}).get("configurable", {}).get("thread_id")
    except Exception:
        pass

    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            invoke_input = input if attempt == 0 else await _resume_input(agent, config, input)
            return await agent.ainvoke(invoke_input, config=config, **invoke_kwargs)
        except _NON_RETRYABLE:
            # HITL / control-flow signal — must reach the caller untouched.
            raise
        except _TRANSIENT as exc:
            last_exc = exc
            if attempt >= max_attempts - 1:
                logger.error(
                    "resilience.retry_exhausted",
                    thread_id=tid,
                    attempts=max_attempts,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                raise
            delay = _backoff_delay(attempt, base_delay, max_delay)
            logger.warning(
                "resilience.transient_retry",
                thread_id=tid,
                attempt=attempt + 1,
                next_in_s=round(delay, 2),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            await asyncio.sleep(delay)

    # Unreachable (the final attempt either returns or raises), but keeps mypy
    # and any future edit honest.
    if last_exc is not None:  # pragma: no cover
        raise last_exc
    raise RuntimeError("ainvoke_with_retry exhausted with no captured exception")
