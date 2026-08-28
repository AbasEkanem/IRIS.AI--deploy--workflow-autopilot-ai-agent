"""recovery.py — crash-recovery startup sweep (Part 2).

The durable checkpointer snapshots every super-step, but the only thing that ever
RESUMED a thread was a human clicking Approve on a HITL card. So a thread that was
mid-run when the process died (deploy, crash, OOM, network/DB blip that killed the
worker) just stayed dead — its work silently abandoned.

This module is the missing resume trigger. On startup ``recover_crashed_runs`` is
launched as a non-blocking background task (app.py lifespan). It:

  1. Builds a candidate set of ``slack-*`` threads — the Redis active-run registry
     first (threads that were dispatched and never cleanly finished), unioned with
     a bounded newest-first enumeration of the durable checkpointer (so a crash
     that predates the registry, or a registry flush, is still caught).
  2. Classifies each with ``agent.aget_state`` — the AUTHORITATIVE signal:
        • next == ()                         → completed/idle        → skip
        • next != () and pending interrupts  → HITL, awaiting human  → skip
          (resuming would auto-approve an irreversible action — never do this)
        • next != () and no interrupts       → crashed mid-run       → RESUME
  3. Resumes a crashed thread with ``ainvoke_with_retry(agent, None, ...)`` — the
     ``None`` input continues the pending tasks from the last checkpoint (completed
     work is restored from pending-writes, NOT re-run), which is exactly "restart
     from where it left off". A short-TTL ``iris:run:recovering:`` NX lock stops two
     workers booting together from double-resuming one thread.
  4. Routes the finished (or re-paused) result through the same
     ``_process_agent_result`` the live path uses, so the human still gets the
     reply / the next approval card — the resume is delivered, not silent.

Idempotency (idempotency.py, Part 3) is the safety net for the one super-step that
gets replayed: any external side effect that already landed becomes a no-op.

Everything here is fail-open and fully guarded: Redis down → fall back to
enumeration; one thread's failure → logged, sweep continues; the whole sweep
failing → logged, never crashes the app.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

import structlog

from checkpointer import build_async_checkpointer
from idempotency import _get_async_redis
from resilience import ainvoke_with_retry

logger = structlog.get_logger(__name__)

# Slack threads are keyed ``slack-{channel}-{thread_ts}`` (slack_webook.py). The
# sweep only ever touches these — never web/studio threads, which have their own
# lifecycle and no Slack card to deliver a resumed result to.
_SLACK_PREFIX = "slack-"

_ACTIVE_PREFIX = "iris:run:active:"
_RECOVERING_PREFIX = "iris:run:recovering:"

# Same env var + default as IRIS.py / slack_webook.py so all paths share one limit.
RECURSION_LIMIT = int(os.getenv("IRIS_RECURSION_LIMIT", "1000"))

# Bounds worst-case startup work: how many distinct threads the enumeration will
# consider, and how many raw checkpoints it will scan to find them (newest-first,
# so the freshest — most-likely-crashed — threads are seen first).
_MAX_THREADS = int(os.getenv("IRIS_RECOVERY_MAX_THREADS", "200"))
_MAX_CHECKPOINT_SCAN = int(os.getenv("IRIS_RECOVERY_MAX_SCAN", "5000"))

# Let /health and the first webhooks settle before the sweep competes for the loop.
_STARTUP_DELAY = float(os.getenv("IRIS_RECOVERY_STARTUP_DELAY", "3.0"))

# The recovering-lock lives long enough to cover a full resumed run, then expires
# so a genuinely-crashed re-attempt is never permanently blocked.
_RECOVERING_TTL = int(os.getenv("IRIS_RECOVERY_LOCK_TTL", "1800"))  # 30 min


def _thread_config(thread_id: str, *, resumed: bool = False) -> dict:
    cfg: dict[str, Any] = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    if resumed:
        # Read by ResumeContextMiddleware (Part 4) to tell the model it resumed.
        cfg["configurable"]["resumed"] = True
        # ...and a token unique to THIS resume. The middleware's "already told the
        # model" flag lives in graph state, which the checkpointer persists per
        # thread — so a bare bool made the directive fire once per THREAD, and a
        # second crash on the same long Slack thread resumed silently. Keying the
        # flag on this token makes it once per RESUME, which is what it was for.
        cfg["configurable"]["resume_id"] = uuid.uuid4().hex
    return cfg


def _has_pending_interrupt(snapshot: Any) -> bool:
    """True if the thread is paused awaiting a human decision.

    Checks the top-level ``StateSnapshot.interrupts`` (this langgraph version
    surfaces pending interrupts there) AND every ``PregelTask.interrupts`` as a
    belt-and-braces guard, so a HITL-paused thread is never mistaken for a crash
    and auto-resumed (which would auto-approve an irreversible action).
    """
    try:
        if getattr(snapshot, "interrupts", None):
            return True
        for task in (getattr(snapshot, "tasks", None) or ()):
            if getattr(task, "interrupts", None):
                return True
    except Exception:  # pragma: no cover - defensive
        # If we cannot tell, assume paused: skipping is always safe (worst case a
        # crashed run waits for the next sweep); auto-resuming a real HITL pause is not.
        return True
    return False


def _classify(snapshot: Any) -> str:
    """'done' | 'hitl' | 'crashed' from a thread's current checkpoint."""
    if snapshot is None:
        return "done"
    if not getattr(snapshot, "next", None):
        return "done"
    if _has_pending_interrupt(snapshot):
        return "hitl"
    return "crashed"


async def _registry_ctx_map(r: Any) -> dict[str, dict]:
    """thread_id → stored ctx, from the active-run registry (empty if Redis down).

    slack_webook writes the FULL ctx as the active key's value, so a resumed run
    can rebuild the channel/user/message context and deliver its result without
    the original Slack event.
    """
    out: dict[str, dict] = {}
    if r is None:
        return out
    try:
        async for key in r.scan_iter(match=f"{_ACTIVE_PREFIX}*", count=100):
            tid = key[len(_ACTIVE_PREFIX):]
            ctx: dict = {}
            try:
                raw = await r.get(key)
                if raw:
                    loaded = json.loads(raw)
                    if isinstance(loaded, dict):
                        ctx = loaded
            except Exception:
                pass
            ctx.setdefault("thread_id", tid)
            out[tid] = ctx
    except Exception as exc:
        logger.warning("recovery.registry_scan_failed", error=str(exc))
    return out


async def _enumerate_slack_threads(saver: Any) -> list[str]:
    """Distinct ``slack-*`` thread ids from the durable checkpointer, newest-first.

    ``alist(None)`` lists checkpoints across ALL threads ordered newest-first;
    we collapse to distinct thread ids, bounded by both a distinct-thread cap and
    a raw-scan cap so a huge store can never make startup crawl.
    """
    seen: dict[str, None] = {}  # insertion-ordered set
    scanned = 0
    try:
        async for ct in saver.alist(None, limit=_MAX_CHECKPOINT_SCAN):
            scanned += 1
            try:
                tid = ct.config["configurable"]["thread_id"]
            except Exception:
                continue
            if isinstance(tid, str) and tid.startswith(_SLACK_PREFIX) and tid not in seen:
                seen[tid] = None
                if len(seen) >= _MAX_THREADS:
                    break
            if scanned >= _MAX_CHECKPOINT_SCAN:
                break
    except Exception as exc:
        logger.warning("recovery.enumerate_failed", error=str(exc))
    return list(seen.keys())


async def _claim_recovering(r: Any, thread_id: str) -> bool:
    """NX lock so co-booting workers don't double-resume one thread.

    Redis unavailable → return True (single-worker assumption; correctness still
    holds because idempotency absorbs any duplicate side effect on the replay)."""
    if r is None:
        return True
    try:
        return bool(await r.set(f"{_RECOVERING_PREFIX}{thread_id}", "1", nx=True, ex=_RECOVERING_TTL))
    except Exception as exc:
        logger.warning("recovery.lock_failed", thread_id=thread_id, error=str(exc))
        return True


async def _release_recovering(r: Any, thread_id: str) -> None:
    if r is None:
        return
    try:
        await r.delete(f"{_RECOVERING_PREFIX}{thread_id}")
    except Exception:
        pass


async def _resume_one(agent: Any, thread_id: str, ctx: dict, r: Any) -> bool:
    """Resume a single crashed thread and deliver its result. Returns True if resumed."""
    if not await _claim_recovering(r, thread_id):
        logger.info("recovery.skip_locked", thread_id=thread_id)
        return False
    try:
        # Import here (not at module top) so app.py can import recovery without a
        # circular hop through slack_webook at load time.
        from slack_webook import _process_agent_result

        user_id = ctx.get("user_id")
        context = {"iris_id": os.getenv("IRIS_ID", "iris_default")}
        if user_id:
            context["user_id"] = user_id

        logger.info("recovery.resuming", thread_id=thread_id)
        try:
            result = await ainvoke_with_retry(
                agent,
                None,  # None input = continue pending tasks from the last checkpoint
                config=_thread_config(thread_id, resumed=True),
                context=context,
            )
        except Exception:
            # The graph itself did not get through. Nothing was delivered, and the
            # thread is still a recovery candidate for the next boot.
            logger.error("recovery.resume_failed", thread_id=thread_id, exc_info=True)
            return False

        # Split from the above deliberately. These two failures need opposite
        # responses and used to share one log line: a resume_failed means the run
        # did not finish, while a delivery_failed means it DID — the work is done and
        # persisted, and only the Slack post of its result was lost. Reading the
        # second as the first sends someone hunting a phantom agent failure, and
        # hides the case where the human is waiting on a reply that exists.
        try:
            await _process_agent_result(agent, result, ctx)
        except Exception:
            logger.error(
                "recovery.delivery_failed",
                thread_id=thread_id,
                detail="run resumed and completed; delivering its result failed",
                exc_info=True,
            )
            return False

        logger.info("recovery.resumed_ok", thread_id=thread_id)
        return True
    except Exception:
        # Anything outside the two guarded calls — the lazy import, ctx handling.
        logger.error("recovery.resume_failed", thread_id=thread_id, exc_info=True)
        return False
    finally:
        await _release_recovering(r, thread_id)


async def recover_crashed_runs(agent: Any) -> None:
    """Startup sweep: resume threads that were mid-run when the process died.

    Launched non-blocking from the FastAPI lifespan. Never raises — a failure
    anywhere is logged and the app keeps serving.
    """
    try:
        if _STARTUP_DELAY > 0:
            await asyncio.sleep(_STARTUP_DELAY)

        saver = await build_async_checkpointer()
        # A non-durable MemorySaver loses all threads on restart, so there is
        # nothing to recover — skip the scan entirely.
        if type(saver).__name__ == "MemorySaver":
            logger.info("recovery.skip_memory_saver", reason="non-durable backend; no persisted threads")
            return

        r = await _get_async_redis()
        registry = await _registry_ctx_map(r)
        enumerated = await _enumerate_slack_threads(saver)

        # Registry threads first (definitely-not-clean-exit), then enumerated ones
        # not already covered. Preserves order, de-dups.
        ordered: list[str] = list(registry.keys())
        for tid in enumerated:
            if tid not in registry:
                ordered.append(tid)

        if not ordered:
            logger.info("recovery.nothing_to_scan")
            return

        logger.info(
            "recovery.sweep_start",
            candidates=len(ordered),
            from_registry=len(registry),
            from_enumeration=len(enumerated),
        )

        resumed = skipped_done = skipped_hitl = failed = 0
        for tid in ordered:
            try:
                snapshot = await agent.aget_state({"configurable": {"thread_id": tid}})
            except Exception:
                logger.error("recovery.aget_state_failed", thread_id=tid, exc_info=True)
                failed += 1
                continue

            cls = _classify(snapshot)
            if cls == "done":
                skipped_done += 1
                continue
            if cls == "hitl":
                logger.info("recovery.skip_hitl_paused", thread_id=tid)
                skipped_hitl += 1
                continue

            ctx = registry.get(tid) or _ctx_from_thread_id(tid)
            if await _resume_one(agent, tid, ctx, r):
                resumed += 1
            else:
                failed += 1

        logger.info(
            "recovery.sweep_done",
            resumed=resumed,
            skipped_done=skipped_done,
            skipped_hitl=skipped_hitl,
            failed=failed,
        )
    except Exception:
        logger.error("recovery.sweep_crashed", exc_info=True)


def _ctx_from_thread_id(thread_id: str) -> dict:
    """Best-effort ctx when the registry has no stored ctx (Redis was down at
    dispatch, or the key expired). The thread id is ``slack-{channel}-{thread_ts}``
    — channel ids and Slack ts values contain no '-', so a 2-split recovers both,
    enough for _process_agent_result to post the reply to the right channel/thread.
    Missing user_id/event_text degrade gracefully (the card builders use defaults).
    """
    ctx: dict[str, Any] = {"thread_id": thread_id}
    if thread_id.startswith(_SLACK_PREFIX):
        rest = thread_id[len(_SLACK_PREFIX):]
        parts = rest.split("-", 1)
        if len(parts) == 2:
            ctx["channel_id"], ctx["thread_ts"] = parts[0], parts[1]
            ctx["ts"] = parts[1]
    return ctx
