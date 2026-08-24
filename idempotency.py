"""idempotency.py — Idempotency keys for IRIS's state-changing tools.

Problem this solves
-------------------
A crash-resume replay (recovery.py) or a model re-dispatch can run the SAME
state-changing tool twice — creating a duplicate Attio note, sending a client
email twice, double-posting to Slack (all seen in the live run). The durable
checkpointer restores already-committed tool results on resume, but a tool whose
side effect landed *before* its checkpoint committed (the crash window), or a
model that simply re-emits a completed call, both slip past that.

Fix
---
An ``@idempotent(op, key_args)`` decorator applied UNDER ``@tool`` (so the wrapped
callable stays a plain function that ``@tool`` can still introspect for its
schema). It:

  * builds a key from ``op`` + the *salient* arguments — the recipient/target is
    always part of ``key_args``, so distinct recipients/records never collapse
    into one key;
  * before running, returns any cached result for that key (no second side
    effect) — the caller sees the original success string and moves on;
  * after a SUCCESSFUL run, caches the result string under the key for ``ttl``.

Only successful results are cached. A failure string (``⚠️ …``, ``❌ …``,
``Email delivery failed…``) is never cached, so a genuine retry is always
allowed. The key is deliberately NOT thread-scoped: a duplicate *across* runs
(the "duplicate note created across runs" incident) is caught too.

Redis is best-effort / fail-open: if it is unset, unreachable, or errors, the
tool runs exactly as before (the decorator only ever *suppresses a proven
duplicate*, never blocks real work). Short socket timeouts keep a dead server
from hanging a tool call.

Both sync and async tools are supported — the decorator selects the matching
wrapper via ``inspect.iscoroutinefunction``. ``functools.wraps`` preserves the
wrapped function's name/doc/signature (``__wrapped__``) so ``@tool`` schema
inference is completely unchanged.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import logging
import os
from typing import Any, Callable, Sequence

logger = logging.getLogger(__name__)

_IDEM_PREFIX = "iris:idem:"
_DEFAULT_TTL = 6 * 3600  # 6 hours — long enough to cover a crash+restart window.

# Fail-open config. from_url() is lazy (no connect until first command), so a
# short socket timeout is what actually bounds a dead-server call.
_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
_SOCKET_TIMEOUT = float(os.getenv("IRIS_IDEM_REDIS_TIMEOUT", "0.5"))

_sync_redis: Any = None
_async_redis: Any = None
_redis_warned = False


def _warn_once(exc: Exception) -> None:
    """Log the first Redis problem at WARNING, then stay quiet (fail-open)."""
    global _redis_warned
    if not _redis_warned:
        logger.warning("idempotency: Redis unavailable (%s) — running fail-open (no dedup)", exc)
        _redis_warned = True


def _get_sync_redis():
    """Lazily build a sync Redis client, or None if it cannot be constructed."""
    global _sync_redis
    if _sync_redis is None:
        try:
            import redis  # local import: optional dependency, fail-open if absent
            _sync_redis = redis.from_url(
                _REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=_SOCKET_TIMEOUT,
                socket_timeout=_SOCKET_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001 — never let Redis setup break a tool
            _warn_once(e)
            return None
    return _sync_redis


async def _get_async_redis():
    """Lazily build an async Redis client, or None if it cannot be constructed."""
    global _async_redis
    if _async_redis is None:
        try:
            import redis.asyncio as aioredis  # local import: optional dependency
            _async_redis = aioredis.from_url(
                _REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=_SOCKET_TIMEOUT,
                socket_timeout=_SOCKET_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001
            _warn_once(e)
            return None
    return _async_redis


def _canonical(value: Any) -> str:
    """Deterministic JSON for hashing (sorted keys; tolerant of odd types)."""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _build_key(op: str, sig: inspect.Signature, key_args: Sequence[str],
               args: tuple, kwargs: dict) -> str:
    """Key = sha256(op + salient-args). Salient args are bound by name from the
    call so positional or keyword invocation produces the same key."""
    try:
        bound = sig.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        salient = {k: bound.arguments.get(k) for k in key_args}
    except Exception:
        # Binding should not fail for a real tool call, but never crash the tool.
        salient = {k: kwargs.get(k) for k in key_args}
    raw = f"{op}\x00{_canonical(salient)}"
    return _IDEM_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Failure markers across the tool suite: Attio/Slack/Jira emit ⚠️/❌ prefixes,
# Gmail emits "Email delivery failed…". A result that STARTS like a failure is
# never cached (a real retry stays possible). Prefix-checked, not "contains", so
# a success whose body mentions "error" (e.g. a note titled "Error handling") is
# still cached correctly.
_FAILURE_EMOJI = ("⚠", "❌", "🚫", "🛑", "❗")
_FAILURE_PREFIXES = (
    "error", "failed", "failure", "email delivery failed", "could not", "unable to",
)


def _is_success(result: Any) -> bool:
    """True when a tool result string represents a real success worth caching."""
    if not isinstance(result, str):
        return False
    s = result.strip()
    if not s:
        return False
    if s[0] in _FAILURE_EMOJI:  # ⚠️ = U+26A0 + VS16, so s[0] is the base ⚠
        return False
    return not s.lower().startswith(_FAILURE_PREFIXES)


def idempotent(op: str, key_args: Sequence[str], *, ttl: int = _DEFAULT_TTL) -> Callable:
    """Decorate a state-changing tool so a duplicate call is a no-op.

    Args:
        op: stable operation name (namespaces the key; keep it unique per tool).
        key_args: names of the arguments that define this call's identity — MUST
            include the recipient/target so distinct recipients never collapse.
        ttl: seconds to remember a successful result (default 6h).

    Apply UNDER ``@tool``::

        @tool
        @idempotent("create_attio_note", key_args=["parent_record_id", "title", "content"])
        def create_attio_note(...): ...
    """
    def deco(fn: Callable) -> Callable:
        sig = inspect.signature(fn)

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                key = _build_key(op, sig, key_args, args, kwargs)
                r = await _get_async_redis()
                if r is not None:
                    try:
                        cached = await r.get(key)
                    except Exception as e:  # noqa: BLE001
                        _warn_once(e)
                        cached, r = None, None
                    if cached is not None:
                        logger.info("idempotency: '%s' deduplicated (cache hit) — returning stored result", op)
                        return cached
                result = await fn(*args, **kwargs)
                if r is not None and _is_success(result):
                    try:
                        await r.set(key, result, ex=ttl, nx=True)
                    except Exception as e:  # noqa: BLE001
                        _warn_once(e)
                return result
            return awrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = _build_key(op, sig, key_args, args, kwargs)
            r = _get_sync_redis()
            if r is not None:
                try:
                    cached = r.get(key)
                except Exception as e:  # noqa: BLE001
                    _warn_once(e)
                    cached, r = None, None
                if cached is not None:
                    logger.info("idempotency: '%s' deduplicated (cache hit) — returning stored result", op)
                    return cached
            result = fn(*args, **kwargs)
            if r is not None and _is_success(result):
                try:
                    r.set(key, result, ex=ttl, nx=True)
                except Exception as e:  # noqa: BLE001
                    _warn_once(e)
            return result
        return wrapper
    return deco
