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
import re
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

# ── HTTP status-code errors (retry ONLY on 429/5xx) ───────────────────────────
# `httpx.HTTPStatusError` / `requests.exceptions.HTTPError` are raised by
# `raise_for_status()` on any non-2xx response. `httpx.HTTPStatusError` is a
# sibling of `TransportError` (both derive from `HTTPError`), NOT a subclass, so
# it never matched `_TRANSIENT` above — a real gap: NVIDIA's hosted NIM endpoint
# for nemotron-3-ultra-550b-a55b returns HTTP 500 when the reasoning budget is
# set HIGH (fine with reasoning off), and that propagated straight past this
# module and killed the run — exactly the class of blip this wrapper exists to
# survive.
#
# Handled SEPARATELY from `_TRANSIENT` because retrying is conditional on the
# status code: a 4xx (bad request, auth failure, bad schema) is a real error and
# must propagate immediately. That conditionality is why `_is_transient` tests
# this tuple FIRST — see the ordering note in its docstring.
try:
    _HTTP_STATUS_ERRORS: tuple[type[BaseException], ...] = (_httpx.HTTPStatusError,)
except NameError:  # pragma: no cover - the httpx import above failed
    _HTTP_STATUS_ERRORS = ()

try:
    from requests.exceptions import HTTPError as _ReqHTTPError

    _HTTP_STATUS_ERRORS += (_ReqHTTPError,)
except Exception:  # pragma: no cover
    pass

# 429 (rate limit) and the 5xx family. Deliberately NOT 4xx.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# ── Status codes carried in the MESSAGE rather than a response object ──────────
# Measured, not guessed: tmp/probe_nemotron.py (2026-08-26, 40 live calls) found
# hosted nemotron-3-ultra-550b-a55b failing 30% of tool-carrying calls, and every
# one of those failures arrived as
#     Exception("[500] {'message': 'Internal server error', ...}")
# — a BARE Exception with no `.response` attribute at all. That is how ChatNVIDIA
# surfaces an upstream HTTP error, so NONE of the machinery above saw it: it is not
# in _HTTP_STATUS_ERRORS (no response to inspect) and not in _TRANSIENT (a bare
# Exception), so _is_transient returned False and the 500 propagated and killed the
# run. The existing test case labelled "httpx 500 (hosted Ultra …)" asserts the
# right INTENT against the wrong exception shape — httpx.HTTPStatusError is not what
# this transport raises.
#
# The leading "[NNN]" is anchored at the start of the message, which is ChatNVIDIA's
# own format, so a false positive would need an exception whose text begins with a
# bracketed 3-digit number that also happens to be a retryable HTTP code.
_STATUS_IN_MESSAGE = re.compile(r"^\s*\[(\d{3})\]")


def _status_code_in_message(exc: BaseException) -> int | None:
    """Status code parsed from the exception TEXT (ChatNVIDIA's bare-Exception form)."""
    match = _STATUS_IN_MESSAGE.match(str(exc))
    return int(match.group(1)) if match else None


def _status_code_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status code from an httpx/requests status-error.

    Structured only — reads `exc.response.status_code`. The message-parsed
    fallback is deliberately a SEPARATE function so this one keeps meaning
    "the transport told us the code", which is what the 4xx exclusion relies on.
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    code = getattr(resp, "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _is_transient(exc: BaseException) -> bool:
    """True if `exc` should trigger a checkpoint-resuming retry.

    Three families, tested in this order:
      * HTTP status-code exceptions (`_HTTP_STATUS_ERRORS`) — transient ONLY when
        the code is 429 or 5xx; a 4xx propagates.
      * connection/timeout-shaped exceptions (`_TRANSIENT`) — always transient,
        exactly the prior behaviour.
      * anything else whose MESSAGE begins with a bracketed status code — the
        hosted-NIM shape (see `_STATUS_IN_MESSAGE`), gated on the same code set, so
        a bare `Exception("[500] …")` retries and a bare `Exception("[404] …")`
        still propagates.

    ORDER IS LOAD-BEARING. `requests.exceptions.HTTPError` subclasses
    `RequestException`, which is already in `_TRANSIENT` — so testing `_TRANSIENT`
    first would match EVERY requests status error, including 4xx, and blanket-retry
    bad requests and auth failures while never reaching the status-code gate.
    Status codes must be checked first for the 4xx exclusion to mean anything.
    (`httpx.HTTPStatusError` has no such overlap; `requests` does.)

    The message-parsed family is tested LAST, and only after both isinstance checks
    have already declined, which makes it purely additive: every exception retried
    before this third branch existed is still matched by an earlier branch, so the
    new path can only ever turn a previous False into a True.
    """
    if isinstance(exc, _HTTP_STATUS_ERRORS):
        return _status_code_of(exc) in _RETRYABLE_STATUS_CODES
    if isinstance(exc, _TRANSIENT):
        return True
    return _status_code_in_message(exc) in _RETRYABLE_STATUS_CODES



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
        except Exception as exc:  # noqa: BLE001 — filtered on the next line
            # Exception, not BaseException: asyncio.CancelledError,
            # KeyboardInterrupt and SystemExit derive from BaseException, so they
            # are never caught here regardless. Anything non-transient is
            # re-raised immediately below, so this is equivalent in effect to the
            # old `except _TRANSIENT as exc` — just widened enough that
            # `_is_transient` can inspect status-code errors, whose retryability
            # depends on a response attribute an isinstance tuple cannot express.
            if not _is_transient(exc):
                raise
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
