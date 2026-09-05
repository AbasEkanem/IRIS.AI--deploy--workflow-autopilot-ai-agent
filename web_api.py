"""web_api.py — FastAPI SSE bridge that lets the IRIS Next.js UI stream live.

The UI in ``ui/`` is already built to consume a Server-Sent-Events stream (see
``ui/src/lib/api.ts``): it POSTs to ``/ask`` / ``/resume`` and reads ``data: {json}``
lines off the response body, each a ``StreamEvent = {type, data}``. This module is
the missing backend half — it translates ``agent.astream(...)`` into exactly that
event protocol, reusing the SAME shared agent + durable async checkpointer that the
Slack path uses (``app.state.iris_agent``, built in ``app.py``'s lifespan).

Endpoints (mounted by app.py, no shared prefix):
  • POST /ask                              — stream a turn (tokens + activity + HITL)
  • POST /resume                           — approve / reject / edit a paused action
  • GET  /api/threads/{thread_id}/history  — replay a thread for the UI on load
  • POST /api/upload                       — save an attachment, return its real path
  • POST /api/greeting                     — optional time/day-aware greeting subline

Design notes verified by tmp/probe_arity.py before this was written:
  • ``stream_mode=["updates","messages"], subgraphs=True`` yields 3-tuples
    ``(ns, mode, data)`` (43/43 chunks in the probe).
  • ORCHESTRATOR-namespace (ns == ()) ``messages``-mode chunks carry INCREMENTAL
    text tokens (5 chunks for a one-sentence reply) → live typewriter passthrough
    works. Nemotron reasoning rides ``additional_kwargs["reasoning_content"]``, NOT
    ``content``, so streamed content is already free of ``<think>`` traces.
  • Subagent (ns != ()) message tokens are NOT streamed as answer text — subagent
    work surfaces as ``status`` rows instead, so specialist prose never pollutes the
    answer bubble. ``response_complete`` from authoritative state is always the final
    word, so the rendered answer is correct regardless of intermediate streaming.

HITL mirrors slack_webook.py's proven mechanics (interrupt extraction +
``Command(resume=...)``) but streams SSE instead of posting Slack cards.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import filetype
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

# Redis-backed per-user rate limiting (OI-4). The Limiter is created in this module
# (next to the routes it decorates) and wired into the app (app.state.limiter +
# 429 handler) by app.py.
from slowapi import Limiter
from slowapi.util import get_remote_address

# Command(resume=...) un-pauses a run that stopped on an interrupt_on gate — the
# same primitive slack_webook.py uses on its approval path.
from langchain_core.messages import RemoveMessage
from langgraph.types import Command

# Per-request identity: verifies the UI's Bearer session token and yields the
# authenticated user_id (the user's email). See auth.py.
from auth import get_current_user

# The single source of truth for every steering message the harness can inject —
# IRIS's own recovery/resume nudges plus the deepagents Nemotron profile's internal
# guards. Mirrored by ui/src/lib/corrections.ts. Without this, the persisted nudges
# render as though the USER typed them, and the budget-guard fallback AIMessage can
# be served to the user as IRIS's final answer.
import guardrail_taxonomy as gt

# The server-side list of a user's conversations. Without it the sidebar was pure
# localStorage, so a thread was addressable only from the browser profile that
# created it. See thread_index.py for why this lives in the LangGraph store.
import thread_index as ti

logger = logging.getLogger(__name__)

# Same env var as IRIS.py, slack_webook.py and recovery.py, so every entry point
# bounds the orchestrator's super-steps identically — bump the env var once and all
# four follow. See IRIS.py for the sizing rationale.
#
# The default MUST match those files' default of 1000. It used to be 400 here, and
# the comment claimed all three shared "the same 400 default" — both wrong. Because
# IRIS_RECURSION_LIMIT is unset on Railway, that drift meant the LIVE WEB PATH ran
# at 400 super-steps while Slack and crash-recovery ran at 1000: long multi-step web
# runs were dying on a recursion limit less than half what every other entry point
# allowed, and nothing said so.
RECURSION_LIMIT = int(os.getenv("IRIS_RECURSION_LIMIT", "1000"))

# Per-user memory namespace identity. IRIS_ID scopes the whole assistant instance;
# user_id (from the verified session token) scopes the individual user. Passed as
# context= at every astream site so create_memory_namespace lands each user in
# ("memory", IRIS_ID, user_id) — see agent_memory.create_memory_namespace. Kept in
# sync with slack_webook.py, which passes the same context= at its invoke sites.
IRIS_ID = os.getenv("IRIS_ID", "iris_default")

_PROJECT_ROOT = Path(__file__).parent
_UPLOAD_DIR = _PROJECT_ROOT / "workspace" / "uploads"
# Hard cap on a single upload so a huge or hostile file can't be read into RAM
# (memory-exhaustion DoS). The body is streamed to disk in chunks and an overflow
# is rejected with 413. Tunable without a code change via IRIS_MAX_UPLOAD_BYTES.
_MAX_UPLOAD_BYTES = int(os.getenv("IRIS_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))  # 25 MB
_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MB read granularity

# Small text-like attachments are inlined into the user message (covers the
# "summarize/use this file" intent without needing read_file, which the FC-4
# governance rule bars the orchestrator from calling). Larger/binary files are
# only referenced by real OS path for a specialist's domain tool to open.
_TEXT_EXTS = {".md", ".txt", ".csv", ".json", ".log", ".yaml", ".yml", ".tsv"}
_MAX_INLINE = 32 * 1024  # 32 KB

# OI-8: total wall-clock ceiling for one streamed run. A stalled or runaway run
# would otherwise hold the SSE connection (and a server worker slot) open forever;
# this bounds it. A TOTAL deadline — not an idle timeout — because a single slow
# tool call emits no intermediate events, so an idle timer would false-abort a
# legitimately long tool. On expiry the stream emits stream_abort + [DONE] and stops
# consuming; the durable checkpointer keeps the last completed super-step, so the
# client re-attaches (OI-9 /status, OI-11 null-message /ask) to collect the result.
#
# 1800s, raised from 600s. At RECURSION_LIMIT=1000 super-steps, wall-clock — not
# the recursion limit — is the binding constraint on a long task: a genuinely long
# multi-specialist run cannot finish inside 10 minutes, so it always ended in
# stream_abort. Model retries also live under this ceiling —
# ModelRetryMiddleware(max_retries=2) at a 120s client deadline is ≈360s for three
# attempts — so lowering it below ~600 would start cutting retries off before they
# can hand back a formatted error.
_STREAM_TIMEOUT_SECONDS = float(os.getenv("IRIS_STREAM_TIMEOUT_SECONDS", "1800"))

# ── Rate limiting (OI-4) ─────────────────────────────────────────────────────
# Per-authenticated-user throttle on the two request-driving endpoints (/ask,
# /resume). Redis-backed so the limit holds across worker processes; env-tunable
# via IRIS_RATE_LIMIT (default "10/minute"). Construction is guarded so a bad
# storage URI can never stop the module from importing.
#
# in_memory_fallback_enabled=True is LOAD-BEARING, not belt-and-suspenders.
# swallow_errors=True alone does NOT fail open on this decorator path — it fails
# with a 500. When Redis is unreachable, slowapi's __evaluate_limits raises inside
# limiter.hit() BEFORE it reaches `request.state.view_rate_limit = …`; the
# swallow_errors branch then returns quietly, but the @limiter.limit async_wrapper
# unconditionally does `self._inject_headers(response, request.state.view_rate_limit)`
# after awaiting the endpoint — which raises
# ``AttributeError: 'State' object has no attribute 'view_rate_limit'`` and turns
# every /ask and /resume into HTTP 500. (Verified live: with no Redis on :6379 the
# whole web UI was dead on arrival — the browser got a 500 before a single SSE byte.)
# With the in-memory fallback enabled, slowapi instead retries the evaluation
# against a process-local MemoryStorage, so view_rate_limit IS set and the request
# proceeds. A Redis outage then degrades the limit from cluster-wide to per-process
# — which is the intended fail-open posture, and what the comment above always
# claimed. swallow_errors stays on as the last line of defence for any other
# storage error.
_RATE_LIMIT = os.getenv("IRIS_RATE_LIMIT", "10/minute")
_RATE_LIMIT_STORAGE = os.getenv("REDIS_URL", "redis://localhost:6379")


def _user_key(request: Request) -> str:
    """Rate-limit bucket key: the authenticated user_id (set on request.state by
    auth.get_current_user), falling back to the client IP for any route without it."""
    return getattr(request.state, "user_id", None) or get_remote_address(request)


try:
    limiter = Limiter(
        key_func=_user_key,
        storage_uri=_RATE_LIMIT_STORAGE,
        strategy="fixed-window",
        swallow_errors=True,
        in_memory_fallback_enabled=True,  # see note above — without this a dead Redis 500s
    )
except Exception as _exc:  # pragma: no cover — never let limiter setup break serving
    logger.warning("rate limiter: storage init failed (%s) — using in-memory limiter.", _exc)
    limiter = Limiter(
        key_func=_user_key,
        strategy="fixed-window",
        swallow_errors=True,
        in_memory_fallback_enabled=True,
    )

router = APIRouter(tags=["web"])


# ═══════════════════════════════════════════════════════════════════════════════
# Request bodies (match ui/src/lib/api.ts)
# ═══════════════════════════════════════════════════════════════════════════════
# Client-supplied thread_id charset/length guard. The client sends only the RAW
# suffix; the server prepends the trusted ``web:{user_id}:`` namespace (see /ask,
# /resume, history). Bounding it here stops unbounded / path- or injection-shaped
# ids from ever reaching the checkpointer key. Lenient enough for UUIDs and the
# existing client ids: letters, digits, '-' and '_', 1–128 chars.
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _validate_thread_id(value: Any) -> str:
    """Raise ValueError unless ``value`` is a safe raw thread_id (Pydantic uses
    this in a field_validator → 422; route handlers wrap it in an HTTPException)."""
    if not isinstance(value, str) or not _THREAD_ID_RE.match(value):
        raise ValueError("thread_id must be 1–128 chars of letters, digits, '-' or '_'.")
    return value


class AskRequest(BaseModel):
    # OI-11: message is OPTIONAL. A null/empty message is a RE-ATTACH request — the
    # server drives the run from the persisted checkpoint (None input) to continue a
    # thread that was cut off mid-run (e.g. an OI-8 timeout or a dropped connection)
    # or to replay the final answer of a completed one, without appending a new turn.
    message: str | None = None
    thread_id: str
    attachments: list[str] | None = None
    # The edit pencil / regenerate / refresh: "this message REPLACES user turn N",
    # zero-based over genuine user turns. Without it a resubmit only rewrote the
    # browser's local array while the checkpoint kept the original — the model then
    # answered with BOTH requests in context, and a reload resurrected the message
    # the user thought they had edited away (the `messages` channel has an
    # add_messages reducer, so /ask can only ever append). Ordinal rather than a
    # message id because /ask sends content with no id and LangGraph assigns its
    # own, so the browser's ids do not exist server-side.
    replace_from_turn: int | None = None

    @field_validator("thread_id")
    @classmethod
    def _check_thread_id(cls, v: str) -> str:
        return _validate_thread_id(v)

    @field_validator("replace_from_turn")
    @classmethod
    def _check_replace_from_turn(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("replace_from_turn must be >= 0.")
        return v


class ResumeRequest(BaseModel):
    thread_id: str
    decision: str  # "approve" | "reject"
    edited_args: dict[str, Any] | None = None

    @field_validator("thread_id")
    @classmethod
    def _check_thread_id(cls, v: str) -> str:
        return _validate_thread_id(v)


class GreetingRequest(BaseModel):
    thread_id: str | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════════
def _resolve_agent(request: Request) -> Any:
    """Resolve the shared async agent (same lookup order as the Slack router)."""
    st = request.app.state
    agent = (
        getattr(st, "iris_agent", None)
        or getattr(st, "conversation_agent", None)
        or getattr(st, "agent", None)
    )
    if agent is None:
        raise HTTPException(status_code=503, detail="IRIS agent is not ready yet.")
    return agent


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}


def _as_text(content: Any) -> str:
    """Flatten message content (str or list-of-parts) to plain text.

    Reused verbatim from tmp/retest_multistep.py — content-parts (Anthropic-style
    ``[{type,text}, …]``) are joined; a plain string passes through.
    """
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(str(p.get("text", p)))
            else:
                parts.append(str(p))
        return " ".join(parts)
    return content or ""


# Strips only the <think> / </think> TAGS (not the text between them). The probe
# confirmed Nemotron reasoning never reaches ``content`` here, so this is cheap
# belt-and-suspenders rather than the primary defense.
_THINK_TAG_RE = re.compile(r"</?think\b[^>]*>", re.IGNORECASE)


def _strip_think(text: str) -> str:
    return _THINK_TAG_RE.sub("", text)


def _msg_id(msg: Any) -> str:
    """Stable per-message key for token accumulation.

    Every ``AIMessageChunk`` of one LLM call shares the same ``.id``, so the client
    can group chunks into messages instead of concatenating a whole multi-step turn
    into a single flat buffer. Some providers omit it; fall back to a constant so
    those runs still accumulate (into one bubble) rather than fragmenting per chunk.
    """
    return str(getattr(msg, "id", None) or "orch")


# LangChain's own discriminators for "this came from the model". ``.type`` is "ai"
# on AIMessage but "AIMessageChunk" on the streaming chunk class, so both spellings
# are listed; the class-name set is the belt-and-braces fallback for a provider
# subclass that overrides ``.type``.
_AI_TYPES = frozenset({"ai", "AIMessageChunk"})
_AI_CLASSES = frozenset({"AIMessage", "AIMessageChunk"})


def _is_ai_message(msg: Any) -> bool:
    """True only for the model's OWN output — not tool results or injected nudges.

    ``stream_mode="messages"`` hands back everything written to the messages channel
    at this namespace, tool results and harness-injected corrections included. Only
    AI messages are IRIS speaking, so only those belong in the answer bubble.
    """
    return getattr(msg, "type", None) in _AI_TYPES or type(msg).__name__ in _AI_CLASSES


def _event(event_type: str, data: Any) -> str:
    """Format one SSE line pair for the UI's reader (`data: {json}\\n\\n`)."""
    return "data: " + json.dumps({"type": event_type, "data": data}, ensure_ascii=False, default=str) + "\n\n"


_DONE = "data: [DONE]\n\n"


def _risk_of(name: str) -> str:
    """Map a gated tool name to the UI's risk band (drives ApprovalCard colour)."""
    n = (name or "").lower()
    if any(k in n for k in ("delete", "trash", "transition", "cancel", "remove")):
        return "high"
    if any(k in n for k in ("send", "schedule", "share", "publish", "update", "reply", "upload", "create")):
        return "medium"
    if "comment" in n:
        return "low"
    return "medium"  # matches the UI's own fallback


def _phase_of(name: str) -> str:
    """Map a tool name to a UI StatusStep phase (drives the activity-row icon)."""
    n = (name or "").lower()
    if n == "task":
        return "delegating"
    if n == "write_todos":
        return "writing"
    if any(k in n for k in ("search", "tavily", "web")):
        return "searching"
    if any(k in n for k in ("email", "gmail", "mail")):
        return "emailing"
    if any(k in n for k in ("memory", "remember", "recall")):
        return "memory"
    if any(k in n for k in ("write_file", "create_doc", "write", "draft")):
        return "writing"
    if any(k in n for k in ("read", "get", "list", "fetch", "find")):
        return "reading"
    return "tool"


def _humanize(name: str) -> str:
    return (name or "tool").replace("_", " ").strip().capitalize()


def _detail_for(name: str, args: dict) -> str:
    """Human label for a status row.

    No nesting marker here. This used to return ``f"↳ {label}"`` for any call
    made inside a subagent namespace, because that prefix was the ONLY way the
    UI could show that a row belonged to a specialist. It is not any more: the
    row now carries ``ns`` and ``parent_id``, the workspace builds a real tree
    from them, and every nested row is drawn indented with a connector rail. The
    glyph became a second, redundant nesting marker baked into the text — and
    text is the one part the frontend cannot undo, so it also reached the copied
    transcript and ate two characters of an already-truncated mobile line.
    """
    if name == "task":
        return f"Delegating to {args.get('subagent_type', 'a specialist')}"
    if name == "write_todos":
        return "Updating the task plan"
    return _humanize(name)


def _action_requests_from_interrupt(delta: Any) -> list[dict]:
    """Flatten action_requests out of a stream `__interrupt__` update value."""
    out: list[dict] = []
    items = delta if isinstance(delta, (list, tuple)) else [delta]
    for itr in items:
        val = getattr(itr, "value", itr)
        if isinstance(val, dict):
            out.extend(val.get("action_requests", []) or [])
    return out


def _pending_from_state(st: Any) -> list[dict]:
    """Authoritative pending action_requests from a persisted StateSnapshot.

    Reads ``st.interrupts`` first (langgraph 1.2.11 StateSnapshot carries pending
    interrupts there), then falls back to walking ``st.tasks[].interrupts``. Each
    interrupt value is a HITLRequest dict ``{action_requests, review_configs}``.
    """
    out: list[dict] = []
    for itr in (getattr(st, "interrupts", ()) or ()):
        val = getattr(itr, "value", itr)
        if isinstance(val, dict):
            out.extend(val.get("action_requests", []) or [])
    if out:
        return out
    for task in (getattr(st, "tasks", ()) or ()):
        for itr in (getattr(task, "interrupts", ()) or ()):
            val = getattr(itr, "value", itr)
            if isinstance(val, dict):
                out.extend(val.get("action_requests", []) or [])
    return out


def _segment_turns(msgs: list) -> list[list]:
    """Split a thread's persisted messages into turns.

    A HumanMessage with NO ``name`` is the only reliable marker of a genuine new user
    turn: every guardrail nudge in this codebase is persisted as
    ``HumanMessage(name=<source>)``. Same discriminator blank_recovery's
    ``_real_user_turn_key`` uses.

    ONE definition, two consumers — ``_reconstruct_history`` (what the browser
    reloads) and ``_rewind_ids_from_turn`` (what an edit deletes). They MUST agree:
    the edit pencil says "replace user turn N", and N is counted by the client over
    the very bubbles /history handed it. Two copies of this loop would eventually
    drift and an edit would then delete the wrong turn.
    """
    turns: list[list] = []
    for m in msgs:
        if type(m).__name__ == "HumanMessage" and not getattr(m, "name", None):
            turns.append([m])
        elif turns:
            turns[-1].append(m)
        else:
            # Anything before the first real user message (a resumed thread's
            # replayed tail, a system seed) — keep it rather than drop it.
            turns.append([m])
    return turns


def _rewind_ids_from_turn(msgs: list, turn_ordinal: int) -> list[str] | None:
    """Ids to delete so the user can rewrite user turn ``turn_ordinal`` and re-run.

    Everything from that turn's first message to the END of the thread goes: the
    request, its answer, the tool traffic, and any guardrail nudges in between. The
    caller then appends the edited text, so the thread reads as if the user had
    typed it that way the first time.

    Returns ``None`` when the ordinal does not land on a genuine user turn — the
    client counts bubbles and the server counts persisted messages, and those can
    drift (a nudge name the taxonomy fails to classify stays a visible user-role
    bubble here while NOT opening a new turn there). On ``None`` the caller declines
    the rewind and plain-appends: the old stacking behaviour for that one edit,
    which is survivable, versus deleting a turn the user never pointed at, which is
    not. Messages with no id are skipped — ``add_messages`` keys RemoveMessage on id
    and RAISES on one it cannot find (langgraph/graph/message.py:227-230), so only
    ids actually read back from state are ever passed.
    """
    turns = _segment_turns(msgs)
    user_turns = [
        t for t in turns
        if t and type(t[0]).__name__ == "HumanMessage" and not getattr(t[0], "name", None)
    ]
    if not (0 <= turn_ordinal < len(user_turns)):
        return None
    start = user_turns[turn_ordinal][0]
    ids: list[str] = []
    hit = False
    for m in msgs:
        if m is start:
            hit = True
        if hit and getattr(m, "id", None):
            ids.append(m.id)
    return ids or None


async def _rewind_thread(
    agent: Any, cfg: dict, turn_ordinal: int, thread_id: str, user_id: str
) -> int:
    """Delete user turn ``turn_ordinal`` and everything after it from the checkpoint.

    Fail-soft by design, and loud. Every failure path logs and returns 0, leaving
    /ask to plain-append: rejecting the request would throw away the message the
    user just typed, which is a worse outcome than the message-stacking the rewind
    exists to prevent. So a rewind is best-effort, and the log is how you find out
    it did not happen.
    """
    try:
        st = await agent.aget_state(cfg)
        # A pending approval OWNS the graph: it is suspended waiting for a resume
        # whose decisions are built from action_requests carried by the very messages
        # a rewind would delete. Never cut the ground out from under it.
        if _pending_from_state(st):
            logger.warning(
                "web.rewind_skipped_pending_approval thread=%s user=%s", thread_id, user_id
            )
            return 0
        vals = getattr(st, "values", {}) or {}
        msgs = list(vals.get("messages", []) or []) if isinstance(vals, dict) else []
        ids = _rewind_ids_from_turn(msgs, turn_ordinal) if msgs else None
        if not ids:
            logger.warning(
                "web.rewind_no_match thread=%s user=%s ordinal=%s messages=%d",
                thread_id, user_id, turn_ordinal, len(msgs),
            )
            return 0
        await agent.aupdate_state(cfg, {"messages": [RemoveMessage(id=i) for i in ids]})
        logger.info(
            "web.rewind thread=%s user=%s ordinal=%s removed=%d",
            thread_id, user_id, turn_ordinal, len(ids),
        )
        return len(ids)
    except Exception:  # noqa: BLE001 — see the fail-soft contract above
        logger.exception(
            "web.rewind_failed thread=%s user=%s ordinal=%s", thread_id, user_id, turn_ordinal
        )
        return 0


def _final_answer_from_state(st: Any, *, after_id: str | None = None) -> str:
    """Last no-tool-call AIMessage text — the authoritative final answer.

    ``after_id`` fences the search to the CURRENT turn: it is the id of the last
    message that existed BEFORE this run started, and the backwards walk stops there.
    Without it the walk runs off the end of the thread, so a turn ending in an empty
    completion serves the PREVIOUS turn's text as this turn's answer — the verbatim
    duplicate traced through 34 MB of checkpoint state, where the phrase existed
    exactly once on disk and twice in the browser. Keyword-with-a-default so the
    callers that DELIBERATELY replay a finished thread (/status, /resume's no-op, and
    /ask's null-message re-attach) keep their unfenced behaviour untouched.

    Also skips the NemotronProgressBudget fallback AIMessage. It is the only nudge
    delivered as an assistant turn — the profile returns it from wrap_model_call in
    place of a real model call, so it persists like genuine output — and without this
    exclusion "I could not complete this reliably within the harness step budget" is
    served to the user as IRIS's considered answer. The profile's own
    _is_final_answer excludes it too; it surfaces as a workspace ``correction``.
    """
    vals = getattr(st, "values", {}) or {}
    msgs = vals.get("messages", []) if isinstance(vals, dict) else []
    for m in reversed(msgs):
        if after_id and getattr(m, "id", None) == after_id:
            break  # reached the pre-run tail — everything older belongs to a prior turn
        if not _is_ai_message(m) or (getattr(m, "tool_calls", None) or []):
            continue
        if gt.is_budget_guard(m):
            continue
        text = _as_text(getattr(m, "content", ""))
        if text.strip():
            return _strip_think(text)
    return ""


def _is_rate_limit(exc: Exception) -> bool:
    s = f"{type(exc).__name__} {exc}".lower()
    return any(k in s for k in ("429", "rate limit", "rate_limit", "resourceexhausted", "too many requests", "quota"))


def _retry_after(exc: Exception, default: int = 30) -> int:
    m = re.search(r"retry[^0-9]{0,12}(\d+)", str(exc), re.IGNORECASE)
    if m:
        try:
            return max(1, min(300, int(m.group(1))))
        except ValueError:
            pass
    return default


def _unpack(chunk: Any) -> tuple[tuple, str, Any]:
    """Normalise an astream chunk to (ns, mode, data).

    Confirmed 3-tuple for list stream_mode + subgraphs=True, but stay defensive so
    a config change (single mode / subgraphs off) degrades gracefully instead of
    crashing the stream.
    """
    if isinstance(chunk, tuple) and len(chunk) == 3:
        return chunk[0], chunk[1], chunk[2]
    if isinstance(chunk, tuple) and len(chunk) == 2 and isinstance(chunk[0], tuple):
        # (ns, data) — single stream mode + subgraphs
        return chunk[0], "updates", chunk[1]
    if isinstance(chunk, tuple) and len(chunk) == 2:
        # (mode, data) — list stream mode, no subgraphs
        return (), chunk[0], chunk[1]
    return (), "updates", chunk


def _ns_key(ns: tuple) -> str:
    """Stable JSON-safe string key for a namespace tuple (also dict-usable)."""
    return "|".join(str(p) for p in (ns or ()))


def new_stream_ctx() -> dict:
    """Per-run bookkeeping for the workspace's delegation tree.

    deepagents emits NO structured dispatch signal — no ``get_stream_writer``, no
    custom events — so which specialist is doing what has to be synthesized from
    ``tool_calls[name == "task"]`` paired to its completion ToolMessage by
    ``tool_call_id``, with ``ns != ()`` marking nested work.
    """
    return {
        "open_tasks": {},   # task tool_call_id -> subagent_type
        "task_order": [],   # task tool_call_ids, oldest first
        "ns_owner": {},     # ns key -> the task tool_call_id that owns it
    }


def _normalize_todos(todos: Any) -> list[dict]:
    """Normalise write_todos args to the shape the UI checklist actually reads.

    LangChain's ``Todo`` TypedDict is ``{content, status}``, but the UI's normalizer
    only ever looked for ``description`` / ``task_description`` — so every item
    flattened to an empty string, was filtered out, and the live checklist has never
    once rendered. Emitting all three spellings fixes the panel without requiring the
    UI to ship in lockstep, and keeps working if a future ``Todo`` renames its key.
    """
    out: list[dict] = []
    for t in todos if isinstance(todos, list) else []:
        if isinstance(t, dict):
            content = t.get("content") or t.get("description") or t.get("task_description") or ""
            status = t.get("status") or t.get("task_status") or "pending"
        else:
            content = getattr(t, "content", "") or ""
            status = getattr(t, "status", "") or "pending"
        content = str(content).strip()
        if not content:
            continue
        status = str(status).strip().lower().replace("-", "_")
        if status not in ("pending", "in_progress", "completed"):
            status = "pending"
        # All three spellings, deliberately: see the docstring.
        out.append({
            "content": content,
            "description": content,
            "task_description": content,
            "status": status,
        })
    return out


# The Final Response Contract (prompts/iris/execution-protocol.md, §7) is five
# labelled fields inside a ━-fenced block. IRIS's only other user-visible block is
# the Intent Routing Log (role.md, "Intent Routing Log"), fenced identically — so the
# two are disambiguated on CONTENT, not on the fence: a contract carries STATUS +
# SUMMARY. Referenced by section rather than line number: both files are edited
# often and the line refs that used to be here had already drifted.
_CONTRACT_FENCE = re.compile(r"^[━─—=_-]{6,}$")
_CONTRACT_FIELD = re.compile(
    r"^\s*\**\s*(STATUS|SUMMARY|ARTIFACTS|BLOCKERS|LEARNING)\s*\**\s*:\s*(.*)$",
    re.IGNORECASE,
)
_ARTIFACT_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
_EMPTY_VALUES = frozenset({"none", "n/a", "na", "-", "nil", "empty"})


def _parse_final_contract(answer: str) -> dict | None:
    """Parse the Final Response Contract into the chat summary card's fields.

    Returns ``None`` when the text is not a contract, in which case the caller sends
    the whole answer as ``summary.raw`` and the chat renders it as ordinary prose. A
    parse miss must never produce an empty bubble — that is the whole point of
    routing prose to the workspace only once a summary is guaranteed.
    """
    text = answer or ""
    if not text.strip():
        return None
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if _CONTRACT_FENCE.match(line.strip()):
            continue
        m = _CONTRACT_FIELD.match(line)
        if m:
            current = m.group(1).upper()
            fields.setdefault(current, [])
            rest = m.group(2).strip()
            if rest:
                fields[current].append(rest)
            continue
        if current is not None:
            fields[current].append(line.rstrip())
    if "STATUS" not in fields or "SUMMARY" not in fields:
        return None

    def joined(key: str) -> str:
        val = "\n".join(fields.get(key, [])).strip()
        return "" if val.lower() in _EMPTY_VALUES else val

    artifacts: list[str] = []
    for ln in fields.get("ARTIFACTS", []):
        item = _ARTIFACT_BULLET.sub("", ln).strip()
        if item and item.lower() not in _EMPTY_VALUES:
            artifacts.append(item)

    raw_status = "\n".join(fields.get("STATUS", [])).strip().upper()
    status = next(
        (s for s in ("COMPLETED", "PARTIAL", "FAILED", "BLOCKED") if s in raw_status),
        raw_status[:24] or "COMPLETED",
    )
    return {
        "status": status,
        "summary": joined("SUMMARY"),
        "artifacts": artifacts,
        "blockers": joined("BLOCKERS"),
        "learning": joined("LEARNING"),
        "raw": gt.truncate(text, 4000),
    }


# ── Intent Routing Log — internal narration that must not reach the chat ──────
# role.md defines the log as a ━-fenced block of four labelled fields, emitted
# before the first `task()` call on a WORK turn. execution-protocol.md §0 is equally
# explicit that a non-task turn (a greeting, a thank-you, "what can you do?") emits
# NO routing log at all.
#
# Prod's orchestrator (nvidia/nemotron-3.5-lightning-30b-a3b) ignores that ~1 turn in
# 3: measured 2/6 on a bare "hi" against the live deployment, where the answer came
# back as the log itself — "🎯 USER INTENT : greet user…", "📁 DOMAIN(S) : …" — with
# the greeting buried underneath. role.md line 10 says the user does not know
# subagents exist, so this is the one output that most breaks that.
#
# Prose cannot fix it (three written rules already forbid it — see plan_guard.py for
# the same finding about write_todos), and unlike write_todos there is no tool call
# to gate: the log is plain text in the completion. So it is removed HERE, on the
# way out.
#
# SCOPE IS DELIBERATELY NARROW. The log is stripped only from an answer that has NO
# Final Response Contract — i.e. exactly the non-task turn where §0 forbids it. A
# work turn keeps its log, because that is what role.md asks for and the workspace
# panel renders alongside it. And if stripping would leave nothing at all, the
# original text is returned untouched: a visible-but-wrong answer beats an empty
# bubble.
_ROUTING_FIELD = re.compile(
    r"^\s*(?:[^\w\s]\s*)?\**\s*(USER\s+INTENT|DOMAIN\(?S?\)?|SPECIALIST\(?S?\)?|DEPENDENCY)"
    r"\s*\**\s*:",
    re.IGNORECASE,
)
# Two fields is the threshold: one line reading "Dependency: none" can legitimately
# belong to a real answer, four consecutive labelled fields cannot.
_ROUTING_MIN_FIELDS = 2


def _strip_routing_log(answer: str) -> str:
    """Remove an Intent Routing Log block from a non-work-turn answer.

    Returns ``answer`` unchanged when it carries a Final Response Contract (a work
    turn), when no log is present, or when removing the log would empty the answer.
    """
    text = answer or ""
    if not text.strip() or _parse_final_contract(text) is not None:
        return text

    lines = text.splitlines()
    keep: list[str] = []
    i = 0
    removed = False
    while i < len(lines):
        # A block starts at a fence or at the first routing field; consume the run of
        # fences and field lines (plus their wrapped continuations) as one unit.
        j = i
        fields = 0
        while j < len(lines):
            stripped = lines[j].strip()
            if not stripped or _CONTRACT_FENCE.match(stripped):
                j += 1
                continue
            if _ROUTING_FIELD.match(lines[j]):
                fields += 1
                j += 1
                continue
            break
        if fields >= _ROUTING_MIN_FIELDS:
            removed = True
            i = j
            continue
        keep.append(lines[i])
        i += 1

    if not removed:
        return text
    cleaned = "\n".join(keep).strip()
    if not cleaned:
        logger.warning("web.routing_log_only: the answer was nothing but a routing log — keeping it")
        return text
    logger.info("web.routing_log_stripped: removed an Intent Routing Log from a non-task answer")
    return cleaned


def _completion_events(answer: str) -> list[str]:
    """Final-answer events for a run that reached a clean end.

    ``response_complete`` keeps its existing bare-string shape so an older client
    still renders the answer unchanged; ``summary`` carries the parsed contract for
    the chat's summary card. ``terminal`` ALWAYS closes the run — including when
    there is no answer at all, which is exactly the case that would otherwise leave
    the chat spinning on a permanently empty bubble.
    """
    text = (answer or "").strip()
    if not text:
        return [_event("terminal", {"reason": "empty", "resumable": True})]
    # Non-task turns sometimes come back as the Intent Routing Log itself; §0 forbids
    # it and the user must never see it. Applied here, at the single place the final
    # answer is turned into events, so /ask, /resume and the timeout-recovery path all
    # get it — and so the `summary` card and `response_complete` can never disagree
    # about what the answer was.
    text = _strip_routing_log(text)
    return [
        _event("summary", _parse_final_contract(text) or {
            "status": "",
            "summary": "",
            "artifacts": [],
            "blockers": "",
            "learning": "",
            "raw": gt.truncate(text, 4000),
        }),
        _event("response_complete", text),
        _event("terminal", {"reason": "complete", "resumable": False}),
    ]


def _workspace_payloads_for_message(m: Any, ns: tuple, ctx: dict | None = None) -> list[tuple[str, Any]]:
    """Turn one graph message into ``(event_type, payload)`` workspace records.

    Emits ``status`` (tool activity), ``todo`` (checklist), ``subagent`` (delegation
    dispatch/return) and ``correction`` (a harness guardrail steering IRIS). MUTATES
    ``ctx`` — from ``new_stream_ctx()`` — to remember which ``task`` delegation owns
    which namespace, so the panel can nest a specialist's tool rows under the
    delegation that spawned them.

    Deliberately returns PAYLOADS, not encoded SSE frames, so the exact same
    derivation feeds two consumers: the live stream (via
    ``_status_events_for_message``) and the after-the-fact reconstruction that makes
    the workspace survive a reload (via ``_workspace_record``). One implementation
    is the point — a second copy would drift, and a reloaded panel that disagrees
    with the live one is worse than no panel.

    Only a CLASSIFICATION plus a truncated excerpt ever crosses the wire for a
    guardrail; raw tool output still does not, so the no-tool-output rule holds. The
    workspace must render any excerpt as inert text — it can contain third-party
    content (email bodies, web pages, Slack messages) carrying indirect injection.
    """
    ctx = ctx if ctx is not None else new_stream_ctx()
    out: list[tuple[str, Any]] = []
    tname = type(m).__name__
    tool_calls = getattr(m, "tool_calls", None) or []
    nskey = _ns_key(ns)

    # Which delegation owns this namespace? The first sighting of a nested namespace
    # is attributed to the most recently opened, still-open `task` call — exact for
    # the sequential delegation delegation-rules.md prescribes, best-effort if two
    # specialists ever run concurrently. Recorded once and reused, so a row never
    # changes parents mid-run.
    parent_id = None
    if nskey:
        parent_id = ctx["ns_owner"].get(nskey)
        if parent_id is None and ctx["task_order"]:
            parent_id = ctx["task_order"][-1]
            ctx["ns_owner"][nskey] = parent_id

    if tname in ("AIMessage", "AIMessageChunk") and tool_calls:
        for tc in tool_calls:
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            tcid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            args = (tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})) or {}
            out.append(("status", {
                "phase": _phase_of(name),
                "detail": _detail_for(name, args),
                "tool": name,
                "id": tcid,
                "done": False,
                # ns/parent_id are what let the workspace build the tree, so the
                # nesting is structural — see _detail_for on why the row's text
                # no longer carries a "↳" marker of its own.
                "ns": nskey,
                "parent_id": parent_id,
            }))
            if name == "task":
                subagent_type = str(args.get("subagent_type") or "specialist")
                if tcid:
                    ctx["open_tasks"][tcid] = subagent_type
                    ctx["task_order"].append(tcid)
                out.append(("subagent", {
                    "id": tcid,
                    "ns": nskey,
                    "parent_id": parent_id,
                    "subagent_type": subagent_type,
                    "description": gt.truncate(str(args.get("description") or ""), 300),
                    "status": "running",
                }))
            elif name == "write_todos":
                # Emit UNCONDITIONALLY, including for an empty list: the old
                # `if todos:` guard meant a cleared plan never cleared the panel.
                out.append(("todo", _normalize_todos(args.get("todos"))))

    elif tname in ("AIMessage", "AIMessageChunk"):
        # No tool calls. The one case worth surfacing is the budget-guard fallback,
        # which is an AIMessage and would otherwise pass as ordinary output.
        c = gt.classify(m, _as_text(getattr(m, "content", "")))
        if c:
            out.append(("correction", {**c, "ns": nskey, "parent_id": parent_id}))

    elif tname in ("ToolMessage", "ToolMessageChunk"):
        tcid = getattr(m, "tool_call_id", None)
        name = getattr(m, "name", None) or ""
        body = _as_text(getattr(m, "content", ""))
        # Always flip the matching row, even for a loop-guard short-circuit — the
        # row's spinner has to resolve regardless of how the call ended.
        out.append(("status", {
            "phase": "tool_done",
            "detail": "Completed",
            "tool": name,
            "id": tcid,
            "done": True,
            "ns": nskey,
            "parent_id": parent_id,
        }))
        if tcid and tcid in ctx["open_tasks"]:
            out.append(("subagent", {
                "id": tcid,
                "ns": nskey,
                "parent_id": parent_id,
                "subagent_type": ctx["open_tasks"].pop(tcid),
                # A blank task result is the documented hole subagents.py leaves (the
                # completion ToolMessage's content defaults to ""); blank_recovery
                # then fires, and its nudge arrives as its own `correction` row.
                "status": "done" if body.strip() else "blank",
            }))
        c = gt.classify(m, body)
        if c:
            out.append(("correction", {**c, "ns": nskey, "parent_id": parent_id}))

    elif tname in ("HumanMessage", "HumanMessageChunk"):
        # A HumanMessage here is either the real user turn (the chat already has it)
        # or a PERSISTED harness nudge wearing the user's role. Only the latter is
        # emitted — failing to tell them apart is what rendered internal steering as
        # the user's own chat bubbles.
        c = gt.classify(m, _as_text(getattr(m, "content", "")))
        if c:
            out.append(("correction", {**c, "ns": nskey, "parent_id": parent_id}))

    return out


def _status_events_for_message(m: Any, ns: tuple, ctx: dict | None = None) -> list[str]:
    """Encode ``_workspace_payloads_for_message`` as SSE frames for the live stream."""
    return [_event(etype, data) for etype, data in _workspace_payloads_for_message(m, ns, ctx)]


# ══════════════════════════════════════════════════════════════════════════════
#  Workspace reconstruction — rebuild a finished run's execution record
# ──────────────────────────────────────────────────────────────────────────────
#  The panel used to be live-only: a reload zeroed it, and the only messages that
#  survived /history were the guardrail nudges (HumanMessages with content and no
#  tool calls), so a reloaded thread degraded into a column of bare amber nudge
#  cards with the actual work — tools, delegations, todos — gone. Rebuilding
#  server-side from the checkpointer is what fixes that, and it fixes it
#  RETROACTIVELY: threads that already ran get their record back, because the
#  checkpointer has held the evidence all along.
#
#  These two helpers are a deliberate MIRROR of ui/src/lib/streamReducer.ts. They
#  must stay in step with it; the guard against drift is that both consume the
#  identical payloads out of _workspace_payloads_for_message.
# ══════════════════════════════════════════════════════════════════════════════

def _merge_status_row(rows: list[dict], d: dict, seq: int) -> None:
    """Mirror of ``mergeStatusStep`` (streamReducer.ts:29-65), in place.

    The one subtlety worth stating: a ``tool_done`` payload FLIPS the existing row
    rather than appending one, so a rebuilt row keeps its original phase and never
    carries ``phase == "tool_done"``. That matters — ``buildTree``
    (AgentWorkspace.tsx:387) filters ``tool_done`` rows out entirely, so emitting
    them would silently render an empty activity list.
    """
    tcid = d.get("id")

    if d.get("phase") == "tool_done" or d.get("done"):
        if not tcid:
            return
        for r in rows:
            if r.get("id") and r["id"] == tcid:
                r["done"] = True
        return  # a stray done with no start row is ignored, as in the reducer

    row = {
        "phase": d.get("phase"),
        "detail": d.get("detail"),
        "tool": d.get("tool"),
        "id": tcid,
        "done": False,
        "ns": d.get("ns"),
        "parent_id": d.get("parent_id"),
    }
    if tcid:
        for r in rows:
            if r.get("id") == tcid:
                # No `seq` in `row`, so an update never restamps a placed row.
                r.update(row)
                return
    rows.append({**row, "seq": seq})


def _workspace_record(turn: list) -> dict:
    """Rebuild one turn's workspace record from its persisted messages.

    Returns the same field names the live reducer writes onto a message
    (``statusSteps``, ``todos``, ``subagents``, ``corrections``,
    ``workspaceSegments``, ``terminal``), so the UI can hydrate a restored message
    through exactly the path a live one takes.

    ``seq`` is stamped here with the same ``len(statusSteps) + len(corrections)``
    formula ``arrivalSeq`` uses (streamReducer.ts:174-176). It is load-bearing:
    ``mergeStream`` (AgentWorkspace.tsx:457) splices each nudge in after the last
    row whose seq is ``<=`` its own, and a correction with a non-numeric seq is
    dumped at the bottom of the stream instead of where it fired.

    Honest limit, rendered rather than hidden: a specialist's own tool calls live
    in their own checkpoint namespace, not in root ``messages``, so a rebuilt
    record has the orchestrator's rows, the ``task`` delegation rows, the todos and
    the corrections — but not the specialists' internals. Wall-clock duration is
    not in state either, hence ``duration_known: False``.
    """
    ctx = new_stream_ctx()
    steps: list[dict] = []
    todos: list = []
    subagents: list[dict] = []
    corrections: list[dict] = []
    segments: list[dict] = []

    for m in turn:
        # ns=() throughout: root state is all there is. Nested namespaces are not
        # recoverable here (see the docstring), so every rebuilt row is a root row.
        for etype, data in _workspace_payloads_for_message(m, (), ctx):
            seq = len(steps) + len(corrections)
            if etype == "status":
                _merge_status_row(steps, data, seq)
            elif etype == "todo":
                # Whole-checklist semantics: the latest write wins.
                todos = data if isinstance(data, list) else []
            elif etype == "subagent":
                sid = data.get("id")
                existing = next((s for s in subagents if sid and s.get("id") == sid), None)
                if existing is None:
                    subagents.append(dict(data))
                else:
                    existing.update(data)  # merge, or finishing erases the description
            elif etype == "correction":
                # The reducer drops a label-less correction, so drop it here too
                # rather than shipping a row that silently vanishes.
                if data.get("label"):
                    corrections.append({**data, "seq": seq})

        # Orchestrator prose becomes the workspace transcript, matching the live
        # `token` channel:"workspace" route. Rendered as inert text by the panel.
        if type(m).__name__ in ("AIMessage", "AIMessageChunk") and not (getattr(m, "tool_calls", None) or []):
            text = _strip_think(_as_text(getattr(m, "content", "")))
            if text.strip():
                segments.append({"id": str(getattr(m, "id", "") or f"seg{len(segments)}"), "text": text})

    # A subagent row whose `task` call never produced a completion ToolMessage was
    # still in flight when the turn ended. Say so instead of leaving it spinning
    # forever in a panel that is definitionally not running.
    for s in subagents:
        if s.get("status") == "running":
            s["status"] = "blank"

    # Did this turn actually produce a written answer? Measured on real state, 7 of
    # 28 rebuilt records did NOT: the run was cut off mid-delegation (the trailing
    # `task` ToolMessage reads "was cancelled"), or it ended on an empty AIMessage,
    # or the loop guard stopped it. Reporting "complete" for those would be a
    # straight lie, and `reason` already has the honest value in its union
    # (chat.ts:89). The panel keys its own copy off this.
    answered = bool(segments)

    return {
        "statusSteps": steps,
        "todos": todos,
        "subagents": subagents,
        "corrections": corrections,
        "workspaceSegments": segments,
        # `resumable` stays False even for an unanswered turn: re-attach is
        # /api/threads/{id}/status's job and it reads live state, whereas this is a
        # historical record that the thread has long since moved past.
        "terminal": {"reason": "complete" if answered else "empty", "resumable": False},
        # Wall-clock time is not persisted anywhere, so the panel must not print a
        # fabricated "Worked for N seconds". It shows the record's shape instead.
        "duration_known": False,
    }


def _has_workspace_content(rec: dict) -> bool:
    """Does this record have anything the panel would actually show?

    Mirrors ``hasWorkspace`` (page.tsx:287-290), which ignores ``terminal`` and
    ``workedMs`` — so a record carrying only a terminal event renders no panel at
    all. Checking here keeps an empty record off the wire entirely.
    """
    return bool(
        rec.get("statusSteps") or rec.get("todos") or rec.get("subagents")
        or rec.get("corrections") or rec.get("workspaceSegments")
    )


# ═══════════════════════════════════════════════════════════════════════════════
# The core translator: astream → SSE StreamEvents
# ═══════════════════════════════════════════════════════════════════════════════
async def _stream_agent(agent: Any, agent_input: Any, cfg: dict, thread_id: str, user_id: str):
    """Async generator yielding SSE strings for one run (fresh turn OR resume).

    Emits: token (incremental answer text) · status (tool/subagent activity, merged
    by id) · todo (checklist) · interrupt (HITL pause) · response_complete
    (authoritative final answer) · rate_limit / error / stream_abort. Always ends
    with ``[DONE]``.

    ``cfg`` already carries the per-user checkpointer key (``web:{user_id}:{tid}``);
    ``thread_id`` is the UI's RAW id, echoed back in the interrupt event so /resume
    can re-prefix it. ``user_id`` is threaded into ``context=`` so the run's
    persistent-memory namespace is this user's alone.
    """
    seen_ids: set[str] = set()
    interrupt_emitted = False
    # OI-8: iterate the stream manually against a TOTAL deadline rather than a plain
    # `async for`, so a stalled or runaway run can't hold this SSE connection (and a
    # worker slot) open indefinitely.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STREAM_TIMEOUT_SECONDS
    aborted = False
    # The ceiling above is a TOTAL wall-clock deadline, so on expiry it cannot by
    # itself distinguish a WEDGED run from a merely SLOW one — both present as
    # silence, and every layer below (model retry, tool retry, the domain tools)
    # logs nothing on its success path. Track stream progress so the abort log can
    # separate the two cases: a large idle_s means genuinely stalled, while a small
    # idle_s with a high chunk count means the run was still streaming and simply
    # needed longer than the ceiling allows.
    chunks = 0
    last_chunk_at = loop.time()
    last_ns: tuple = ()
    last_node = ""
    # Per-run bookkeeping for the workspace's delegation tree; mutated by
    # _status_events_for_message as `task` calls open and close.
    ctx = new_stream_ctx()
    fence_id: str | None = None
    try:
        # Pre-run turn fence (see _final_answer_from_state). FRESH turns only:
        # agent_input is None on /ask's null-message re-attach, which deliberately
        # replays a finished thread's existing answer — fencing that would return "".
        # One extra aget_state per run is the cheapest boundary available, because
        # /ask hands LangGraph an id-less {"role": "user"} dict, so the new message's
        # id does not exist until the graph assigns one. Best-effort: a brand-new
        # thread simply has no fence, and no fence beats no run.
        if agent_input is not None:
            try:
                _pre = await agent.aget_state(cfg)
                _pre_vals = getattr(_pre, "values", {}) or {}
                _pre_msgs = _pre_vals.get("messages", []) if isinstance(_pre_vals, dict) else []
                if _pre_msgs:
                    fence_id = getattr(_pre_msgs[-1], "id", None)
            except Exception:  # noqa: BLE001 — degrade to the old unfenced behaviour
                fence_id = None
        agen = agent.astream(
            agent_input, cfg, stream_mode=["updates", "messages"], subgraphs=True,
            context={"iris_id": IRIS_ID, "user_id": user_id},
        )
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                aborted = True
                break
            try:
                # wait_for propagates an EXTERNAL cancel (client disconnect) as
                # CancelledError (handled below) but raises TimeoutError for OUR
                # deadline — so the two cases stay distinct.
                chunk = await asyncio.wait_for(agen.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                aborted = True
                break
            ns, mode, data = _unpack(chunk)
            chunks += 1
            last_chunk_at = loop.time()
            last_ns = ns

            # ── messages mode: incremental answer tokens (ORCH namespace only) ──
            if mode == "messages":
                msg = data[0] if isinstance(data, (tuple, list)) and data else data
                # ns == () keeps us on the orchestrator (subagent chatter stays as
                # `status` rows), and _is_ai_message keeps us on IRIS's own speech.
                # Without the second test, `messages` mode also hands us ToolMessages
                # and the harness's injected nudge messages, and their raw bodies were
                # rendering INSIDE the answer bubble: get_current_datetime's JSON,
                # "⚠️ LOOP GUARD …", "Updated todo list to […]", "Updated file /agent.md",
                # a delegated subagent's whole STATUS/SUMMARY/ARTIFACTS contract, and
                # every "You returned an EMPTY response …" correction. Those are harness
                # internals — `updates` mode already surfaces them as status rows.
                if ns == () and _is_ai_message(msg):
                    text = _as_text(getattr(msg, "content", ""))
                    # NOTE: test for emptiness, NOT `text.strip()`. A provider emits the
                    # space between two words as its own chunk; dropping whitespace-only
                    # chunks fused words together mid-stream ("across5 domain") until the
                    # final response_complete snapped them apart — and that one missing
                    # space also defeated the client's streamed-vs-final dedupe, so the
                    # whole answer got printed twice. Keep every chunk and let the client
                    # trim each message's LEADING run instead.
                    if text:
                        clean = _strip_think(text)
                        if clean:
                            # Carry the AIMessageChunk id so the client accumulates
                            # per-message (LangGraph messages-tuple semantics) rather
                            # than appending every chunk to one flat buffer.
                            # channel="workspace": IRIS's deliberation prose belongs
                            # in the execution panel, not the chat. The chat carries
                            # one live "IRIS is working…" line and then the summary.
                            # An older client ignores the extra key unchanged.
                            yield _event("token", {
                                "id": _msg_id(msg),
                                "text": clean,
                                "channel": "workspace",
                            })
                continue

            # ── updates mode: tool/subagent activity + interrupts ──
            if mode == "updates" and isinstance(data, dict):
                for node, delta in data.items():
                    last_node = node
                    if node == "__interrupt__":
                        # Emit exactly ONE interrupt per pause. With subgraphs=True
                        # the __interrupt__ update surfaces at both the nested and
                        # the parent namespace, so guard on interrupt_emitted and
                        # surface only the first action_request — /resume re-derives
                        # the full decision count authoritatively from state.
                        if not interrupt_emitted:
                            ars = _action_requests_from_interrupt(delta)
                            if ars:
                                ar = ars[0]
                                yield _event("interrupt", {
                                    "thread_id": thread_id,
                                    "tool": ar.get("name"),
                                    "args": ar.get("args", {}) or {},
                                    "risk": _risk_of(ar.get("name", "")),
                                })
                                interrupt_emitted = True
                        continue
                    if not isinstance(delta, dict):
                        continue
                    msgs = delta.get("messages")
                    if not msgs:
                        continue
                    if not isinstance(msgs, list):
                        msgs = [msgs]
                    for m in msgs:
                        mid = getattr(m, "id", None)
                        if mid and mid in seen_ids:
                            continue
                        if mid:
                            seen_ids.add(mid)
                        for ev in _status_events_for_message(m, ns, ctx):
                            yield ev

        # OI-8: total deadline hit mid-run. Stop consuming and tell the client; the
        # graph's last COMPLETED super-step is already persisted by the checkpointer,
        # so the client re-attaches (OI-9 /status, OI-11 null-message /ask) to finish.
        # aclose() unwinds the suspended run on our side; guarded so an
        # already-finishing generator is a harmless no-op.
        if aborted:
            logger.warning(
                "web.stream_timeout thread=%s user=%s ceiling=%ss "
                "chunks=%d idle_s=%.1f last_ns=%s last_node=%s",
                thread_id, user_id, _STREAM_TIMEOUT_SECONDS,
                chunks, loop.time() - last_chunk_at, last_ns or "()", last_node or "-",
            )
            try:
                await agen.aclose()
            except Exception:  # noqa: BLE001 — cleanup must not mask the abort
                pass
            # The ceiling is a wall-clock deadline, NOT evidence that nothing got
            # done: durability is "async", so every completed super-step — a finished
            # final answer included — is already flushed to the checkpointer. So
            # reconcile before giving up. This is the orphaned-run recovery: the run
            # whose answer sat in the checkpoint while the browser showed nothing and
            # the request had to be retyped.
            recovered = ""
            try:
                recovered = _final_answer_from_state(
                    await agent.aget_state(cfg), after_id=fence_id
                )
            except Exception:  # noqa: BLE001 — recovery is best-effort by design
                logger.exception("web.stream_timeout_recover_failed thread=%s", thread_id)
            if recovered:
                for ev in _completion_events(recovered):
                    yield ev
            else:
                yield _event("terminal", {"reason": "timeout", "resumable": True})
                yield _event("stream_abort", "")
            return

        # ── authoritative reconciliation from persisted state ──
        st = await agent.aget_state(cfg)
        pending = _pending_from_state(st)
        paused = bool(getattr(st, "next", ()) or [])
        if pending and not interrupt_emitted:
            ar = pending[0]
            yield _event("interrupt", {
                "thread_id": thread_id,
                "tool": ar.get("name"),
                "args": ar.get("args", {}) or {},
                "risk": _risk_of(ar.get("name", "")),
            })
            interrupt_emitted = True
        if interrupt_emitted or paused:
            # Stopped on a HITL gate rather than finishing. The chat's live line is
            # now the ONLY thing shown while a run is in flight, so without a terminal
            # event it spins forever behind the approval card.
            yield _event("terminal", {"reason": "paused", "resumable": True})
        else:
            for ev in _completion_events(
                _final_answer_from_state(st, after_id=fence_id)
            ):
                yield ev

    except asyncio.CancelledError:
        # Client disconnected — let the cancellation propagate cleanly.
        raise
    except Exception as exc:  # noqa: BLE001 — never leak a stack trace to the browser
        if _is_rate_limit(exc):
            yield _event("rate_limit", {"resume_at": int(time.time()) + _retry_after(exc)})
            yield _event("terminal", {"reason": "rate_limit", "resumable": True})
        else:
            logger.exception("web.stream_error thread=%s user=%s", thread_id, user_id)
            yield _event("error", "The assistant hit an internal error and stopped.")
            yield _event("terminal", {"reason": "error", "resumable": True})
            yield _event("stream_abort", "")
    finally:
        yield _DONE


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # disable proxy buffering so tokens flush live
}


def _build_user_content(message: str, attachments: list[str] | None) -> str:
    """Fold attachments into the user message.

    Small text-like files are inlined (delimited); every attachment is also listed
    by real OS path so a specialist's domain tool (send_research_email,
    upload_slack_file, upload_file_to_drive, add_jira_attachment) can open it — the
    outbound action then hits the existing HITL gate.
    """
    if not attachments:
        return message
    parts = [message]
    listed: list[str] = []
    for p in attachments:
        try:
            path = Path(p)
            listed.append(str(path))
            if (
                path.suffix.lower() in _TEXT_EXTS
                and path.exists()
                and path.stat().st_size <= _MAX_INLINE
            ):
                body = path.read_text(encoding="utf-8", errors="replace")
                parts.append(
                    f"\n\n--- Attached file: {path.name} ---\n{body}\n--- end of {path.name} ---"
                )
        except Exception:  # noqa: BLE001 — a bad path must not break the turn
            continue
    if listed:
        parts.append(
            "\n\nAttached files (server-side paths — route to the relevant specialist "
            "to attach/email/upload): " + ", ".join(listed)
        )
    return "".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# POST /ask — stream a fresh turn
# ═══════════════════════════════════════════════════════════════════════════════
@router.post("/ask")
@limiter.limit(_RATE_LIMIT)
async def ask(body: AskRequest, request: Request, user_id: str = Depends(get_current_user)):
    agent = _resolve_agent(request)
    thread_id = body.thread_id
    # Namespace the checkpointer key by the verified user: user A can never read or
    # resume user B's thread even with a guessed id (ownership by construction, no
    # DB lookup). The RAW thread_id is still what we echo in interrupt events.
    full_thread_id = f"web:{user_id}:{thread_id}"

    # OI-11: a null/empty message is a RE-ATTACH — drive the graph from the persisted
    # checkpoint (None input) instead of appending a new human turn. LangGraph then
    # continues any pending steps of a run that was cut off (OI-8 timeout / dropped
    # connection) or, on a finished thread, no-ops and the reconciliation step replays
    # the final answer. A fresh turn (non-empty message) takes the normal path.
    message = (body.message or "").strip()
    cfg = _cfg(full_thread_id)
    if message:
        # An edit / regenerate / refresh REPLACES a turn rather than appending after
        # it. Rewind the persisted thread to just before that turn FIRST, so the model
        # sees one clean request instead of the original and the rewrite stacked, and
        # a reload does not resurrect the message the user edited away.
        if body.replace_from_turn is not None:
            await _rewind_thread(agent, cfg, body.replace_from_turn, thread_id, user_id)
        content = _build_user_content(message, body.attachments)
        payload: Any = {"messages": [{"role": "user", "content": content}]}
    else:
        payload = None

    # Index the thread so it can be listed later. Awaited (not fired-and-forgotten)
    # so the entry exists before the stream can be interrupted, but wholly
    # best-effort inside record_thread — the index is a convenience over the
    # checkpointer, which stays the source of truth, so a store blip must never
    # cost the user their message. `title` is only honoured on FIRST write, so
    # passing the current message on every turn cannot rename an old thread.
    if message:
        await ti.record_thread(
            getattr(agent, "store", None), user_id, thread_id,
            title=ti.derive_title(message),
        )

    return StreamingResponse(
        _stream_agent(agent, payload, cfg, thread_id, user_id),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# POST /resume — approve / reject / edit a paused irreversible action
# ═══════════════════════════════════════════════════════════════════════════════
@router.post("/resume")
@limiter.limit(_RATE_LIMIT)
async def resume(body: ResumeRequest, request: Request, user_id: str = Depends(get_current_user)):
    agent = _resolve_agent(request)
    thread_id = body.thread_id
    # Same per-user namespacing as /ask — the paused state lives under the caller's
    # own key, so a resume can only ever act on a thread the caller owns.
    full_thread_id = f"web:{user_id}:{thread_id}"
    cfg = _cfg(full_thread_id)

    # Read pending actions authoritatively from persisted state (the /resume call
    # is a separate request — the interrupt is not on any in-memory result here).
    st = await agent.aget_state(cfg)
    pending = _pending_from_state(st)
    n = len(pending)

    if n == 0:
        # Nothing awaiting approval (already resolved, expired, or double-submit):
        # return the current final answer if there is one, else a soft error.
        async def _noop_gen():
            answer = _final_answer_from_state(st)
            if answer:
                yield _event("response_complete", answer)
            else:
                yield _event("error", "Nothing is awaiting approval on this conversation.")
            yield _DONE

        return StreamingResponse(_noop_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)

    # Build one decision per gated action_request — the HITL middleware raises if
    # the counts differ (human_in_the_loop.py after_model). Contract confirmed in
    # project_venv/.../langchain/agents/middleware/human_in_the_loop.py.
    decision = (body.decision or "").lower()
    if decision == "reject":
        # A directive rejection: a bare "rejected" invites the model to immediately
        # re-attempt the same gated call (a reject→retry→re-gate loop). Tell it the
        # action was declined and NOT to retry, so it abandons the step and moves on.
        reject_msg = (
            "The user DECLINED this action and it was NOT performed. Do not retry it "
            "or attempt an equivalent action. Treat this specific step as cancelled, "
            "then continue with any remaining work or finish and report what you did."
        )
        decisions = [{"type": "reject", "message": reject_msg} for _ in range(n)]
    elif decision == "approve" and body.edited_args:
        # The UI edits the single surfaced tool (index 0). Wrap its flat args dict
        # into the middleware's EditDecision shape; approve the rest unchanged.
        first = pending[0]
        decisions = [{"type": "edit", "edited_action": {"name": first.get("name"), "args": body.edited_args}}]
        decisions += [{"type": "approve"} for _ in range(n - 1)]
    else:  # approve (default)
        decisions = [{"type": "approve"} for _ in range(n)]

    agent_input = Command(resume={"decisions": decisions})
    return StreamingResponse(
        _stream_agent(agent, agent_input, cfg, thread_id, user_id),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/threads — the user's conversation list (the missing "chat history")
# ═══════════════════════════════════════════════════════════════════════════════
@router.get("/api/threads")
async def list_threads(
    request: Request,
    limit: int = 100,
    offset: int = 0,
    user_id: str = Depends(get_current_user),
):
    """List this user's threads, most recently used first.

    The index is namespaced by the AUTHENTICATED user, never by anything the caller
    sends, so there is no parameter here that could address another user's list.

    Returns 200 with a possibly-empty list; ``degraded`` says whether the store was
    readable, so the UI can tell "you have no conversations yet" from "I could not
    find out" — the same distinction the history endpoint now makes with its 503.
    """
    agent = _resolve_agent(request)
    store = getattr(agent, "store", None)
    threads = await ti.list_threads(store, user_id, limit=limit, offset=offset)
    return {"threads": threads, "degraded": store is None}


# ═══════════════════════════════════════════════════════════════════════════════
# DELETE /api/threads/{thread_id} — forget one conversation, for real
# ═══════════════════════════════════════════════════════════════════════════════
@router.delete("/api/threads/{thread_id}")
async def delete_thread(thread_id: str, request: Request, user_id: str = Depends(get_current_user)):
    """Remove a thread from the index AND delete its checkpoint.

    Both halves are needed and neither is sufficient. Index-only would leave the
    transcript in Postgres after the user asked IRIS to forget it — and, before the
    index existed, "delete" only ever edited localStorage, so the conversation was
    never actually gone. Checkpoint-only would leave a dead row in the sidebar.

    This IS irreversible: there is no undo and no trash. It is scoped to the
    caller's own ``web:{user}:{id}`` key, so it can only ever destroy the
    requester's own conversation. ``checkpoint_deleted`` is reported rather than
    assumed, so a partial failure is visible instead of being called success.
    """
    if not _THREAD_ID_RE.match(thread_id):
        raise HTTPException(status_code=400, detail="Invalid thread_id.")
    agent = _resolve_agent(request)
    index_removed = await ti.delete_thread(getattr(agent, "store", None), user_id, thread_id)

    checkpoint_deleted = False
    saver = getattr(agent, "checkpointer", None)
    adelete = getattr(saver, "adelete_thread", None)
    if adelete is not None:
        try:
            await adelete(f"web:{user_id}:{thread_id}")
            checkpoint_deleted = True
        except Exception:  # noqa: BLE001 — reported below, never a 500
            logger.exception("web.thread_delete_error thread=%s user=%s", thread_id, user_id)

    return {
        "ok": index_removed or checkpoint_deleted,
        "index_removed": index_removed,
        "checkpoint_deleted": checkpoint_deleted,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/threads/{thread_id}/history — replay a thread on UI load
# ═══════════════════════════════════════════════════════════════════════════════
@router.get("/api/threads/{thread_id}/history")
async def thread_history(thread_id: str, request: Request, user_id: str = Depends(get_current_user)):
    if not _THREAD_ID_RE.match(thread_id):
        raise HTTPException(status_code=400, detail="Invalid thread_id.")
    agent = _resolve_agent(request)
    # Read under the caller's own namespaced key — a user only ever sees their own
    # thread, and a guessed raw id resolves to the caller's (empty) namespace.
    full_thread_id = f"web:{user_id}:{thread_id}"
    try:
        st = await agent.aget_state(_cfg(full_thread_id))
    except Exception:
        logger.exception("web.history_error thread=%s user=%s", thread_id, user_id)
        # 503, not 200-with-empty-messages. An unreadable checkpointer and a thread
        # with nothing in it produced the SAME response before, so a Supabase blip
        # rendered as "your conversation is gone" and the UI could not tell the two
        # apart to offer a retry. Failing loudly here is what makes
        # fetchThreadHistory's `error: "server"` branch reachable.
        raise HTTPException(status_code=503, detail="Could not read thread history.")

    vals = getattr(st, "values", {}) or {}
    msgs = vals.get("messages", []) if isinstance(vals, dict) else []
    return {"messages": _reconstruct_history(msgs)}


def _reconstruct_history(msgs: list) -> list[dict]:
    """Turn a thread's persisted messages into the chat the UI reloads.

    Split out of the endpoint so it is testable without a Request, an agent, or
    auth — every interesting behaviour here (turn segmentation, nudge withholding,
    where the record lands) is a silent-failure risk that only real message
    sequences exercise. See tmp/probe_aget_state.py.
    """
    # ── Segment into turns ────────────────────────────────────────────────────
    # _segment_turns is the ONE definition of "a turn". The edit pencil's rewind
    # counts turns with the same helper, so a second copy of this loop here would
    # eventually drift and an edit would delete the wrong turn.
    turns = _segment_turns(msgs)

    out: list[dict] = []
    for turn in turns:
        record = _workspace_record(turn)
        has_record = _has_workspace_content(record)

        chat: list[dict] = []
        for m in turn:
            tname = type(m).__name__
            if tname == "HumanMessage":
                if getattr(m, "name", None):
                    # A named HumanMessage is a persisted harness nudge wearing the
                    # user's role. Withhold it from the chat ONLY when the workspace
                    # record actually carries it as a correction — its double-render
                    # as a standalone card is what turned a reloaded thread into a
                    # column of amber nudge boxes with the real work missing.
                    #
                    # The test is `gt.classify`, NOT `has_record`: a turn earns a
                    # record from its tool rows alone, so keying off has_record would
                    # withhold an UNRECOGNIZED nudge (one classify returns None for)
                    # that the panel never shows — and it would vanish entirely. An
                    # unclassifiable name falls through and stays a visible card.
                    if gt.classify(m, _as_text(getattr(m, "content", ""))):
                        continue
                role = "user"
            elif tname in ("AIMessage", "AIMessageChunk"):
                if getattr(m, "tool_calls", None) or []:
                    continue  # tool-call-only deliberation turn — not user-facing
                role = "assistant"
            else:
                continue  # skip ToolMessage / SystemMessage
            content = _as_text(getattr(m, "content", ""))
            if not content.strip():
                continue
            text = _strip_think(content)
            if role == "assistant":
                # Same removal the live stream applies in _completion_events. It has to
                # be repeated here because the offending text is PERSISTED — the model
                # emitted it as its answer — so a reload would otherwise resurrect a
                # routing log the user was never shown when the turn ran. Keeping the
                # two paths in agreement is the point; _strip_routing_log is a no-op on
                # a work turn's answer (it carries a Final Response Contract).
                text = _strip_routing_log(text)
                if not text.strip():
                    continue
            chat.append({
                "role": role,
                "content": text,
                "name": getattr(m, "name", None),
                "id": getattr(m, "id", None),
            })

        # Hang the record on the turn's LAST assistant message, so the panel sits
        # under the answer it produced — one panel per turn, as when it ran live.
        last_ai = next((i for i in range(len(chat) - 1, -1, -1) if chat[i]["role"] == "assistant"), None)
        if last_ai is not None:
            if has_record:
                chat[last_ai]["workspace"] = record
            contract = _parse_final_contract(chat[last_ai]["content"])
            if contract:
                chat[last_ai]["summary"] = contract
        elif has_record:
            # A turn that did real work but produced NO user-facing answer — cut
            # off mid-delegation, an empty completion, or a loop-guard stop. On
            # real state this is 1 turn in 4. With no assistant message to hang the
            # record on, the panel — and the nudges it withholds from the chat —
            # would both vanish, leaving the turn as the user's message alone with
            # its work erased. Synthesize the carrier the panel needs: an empty
            # assistant bubble, which the UI already renders as "this turn ended
            # without a written answer — open the workspace above" (page.tsx:407).
            chat.append({
                "role": "assistant",
                "content": "",
                "name": None,
                "id": f"ws-{getattr(turn[0], 'id', None) or len(out)}",
                "workspace": record,
            })
        out.extend(chat)

    return out


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/threads/{thread_id}/status — where does this run stand right now?
# ═══════════════════════════════════════════════════════════════════════════════
# OI-9: a cheap, unstreamed snapshot the UI polls to RE-ATTACH after a dropped
# connection or an OI-8 timeout (see OI-10). It answers three questions from the
# persisted checkpoint alone — is the run still going, is it paused for approval, or
# is it finished with an answer — so the client can decide whether to inject the
# answer, show the approval card, or re-drive via a null-message /ask (OI-11).
@router.get("/api/threads/{thread_id}/status")
async def thread_status(thread_id: str, request: Request, user_id: str = Depends(get_current_user)):
    if not _THREAD_ID_RE.match(thread_id):
        raise HTTPException(status_code=400, detail="Invalid thread_id.")
    agent = _resolve_agent(request)
    # Same per-user namespacing as /ask + history: a caller only ever reads their own
    # thread, and a guessed raw id resolves to the caller's own (empty) namespace.
    full_thread_id = f"web:{user_id}:{thread_id}"
    try:
        st = await agent.aget_state(_cfg(full_thread_id))
    except Exception as exc:  # noqa: BLE001
        # A status probe that can't read state must say "retry", not fabricate a
        # terminal state — lying "complete" here would strand a still-running thread.
        logger.exception("web.status_error thread=%s user=%s", thread_id, user_id)
        raise HTTPException(status_code=503, detail="Could not read thread state; retry.") from exc

    pending = _pending_from_state(st)
    # st.next is the tuple of nodes still queued to run. Non-empty with NO pending
    # HITL interrupt ⇒ the run was cut off mid-flight and has unfinished steps
    # ("running"); non-empty WITH an interrupt ⇒ "paused" for approval; empty ⇒ done.
    has_next = bool(getattr(st, "next", ()) or [])
    answer = _final_answer_from_state(st)

    if pending:
        state = "paused"
    elif has_next:
        state = "running"
    else:
        state = "complete"

    resp: dict[str, Any] = {
        "thread_id": thread_id,
        "state": state,
        "has_answer": bool(answer),
    }
    if answer:
        resp["answer"] = answer
    if pending:
        # Same shape as the SSE "interrupt" event, so the UI's existing
        # setPendingInterrupt path consumes it unchanged.
        ar = pending[0]
        resp["pending_interrupt"] = {
            "thread_id": thread_id,
            "tool": ar.get("name"),
            "args": ar.get("args", {}) or {},
            "risk": _risk_of(ar.get("name", "")),
        }
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# POST /api/upload — save an attachment, return its real OS path
# ═══════════════════════════════════════════════════════════════════════════════
# OI-5: content-type allowlist enforced by MAGIC BYTES, not the client-supplied
# Content-Type header (which is trivially spoofed). Extensions are env-overridable
# via IRIS_UPLOAD_ALLOWED_TYPES.
_ALLOWED_UPLOAD_EXTS = {
    e.strip().lower().lstrip(".")
    for e in os.getenv("IRIS_UPLOAD_ALLOWED_TYPES", "pdf,docx,xlsx,txt,csv,png,jpg").split(",")
    if e.strip()
}
# Text-like extensions carry NO magic signature — validated by extension alone
# (a filetype.guess() of None is the expected, correct result for these).
_NO_MAGIC_EXTS = {"txt", "csv", "md", "json", "log", "tsv", "yaml", "yml"}
# Expected filetype.guess() extension for each sniffable allowed extension. OOXML
# (.docx/.xlsx) are ZIP containers, so they legitimately sniff as "zip"; "jpeg" is
# an accepted spelling of a JPEG whose magic sniffs as "jpg".
_EXT_EXPECTED_MAGIC = {
    "pdf": {"pdf"},
    "png": {"png"},
    "jpg": {"jpg"}, "jpeg": {"jpg"},
    # OOXML files are ZIP containers; filetype returns "zip" for a partial first
    # chunk and the specific "docx"/"xlsx" when the container structure is present —
    # accept both so a genuine document is never false-rejected.
    "docx": {"zip", "docx"}, "xlsx": {"zip", "xlsx"},
    "gif": {"gif"}, "webp": {"webp"},
}


def _validate_upload(first_chunk: bytes, ext: str) -> None:
    """Reject (HTTP 415) an upload whose extension isn't allowlisted, or whose
    magic-byte content contradicts its extension. ``ext`` is lowercase, no dot.

    Defeats the "rename malware.exe → doc.pdf" bypass: the extension gate passes
    (``pdf`` is allowed) but the content sniffs as ``exe``, which no allowed
    extension expects, so it is rejected.
    """
    if ext not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(
            status_code=415,
            detail=f"File type '.{ext or '?'}' is not allowed. Allowed: {sorted(_ALLOWED_UPLOAD_EXTS)}.",
        )
    kind = filetype.guess(first_chunk) if first_chunk else None
    sniffed = kind.extension if kind is not None else None

    if ext in _NO_MAGIC_EXTS:
        # Text-like: must NOT carry a recognizable binary signature (a real binary
        # renamed .txt is rejected). A None sniff (plain text) is the expected case.
        if sniffed is not None:
            raise HTTPException(
                status_code=415,
                detail=f"Content of '.{ext}' upload looks like '{sniffed}', not text.",
            )
        return

    expected = _EXT_EXPECTED_MAGIC.get(ext)
    if expected is None:
        # Allowlisted but we have no magic rule for it — accept by extension.
        return
    if sniffed not in expected:
        raise HTTPException(
            status_code=415,
            detail=f"Content of '.{ext}' upload does not match its extension "
                   f"(detected: {sniffed or 'unknown'}).",
        )


@router.post("/api/upload")
async def upload(file: UploadFile = File(...), user_id: str = Depends(get_current_user)):
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitize to the bare filename (the Path(...).name idiom used in web_search.py)
    # so a crafted name can't escape the uploads dir; prefix a uuid to avoid clashes.
    safe = Path(file.filename or "upload").name or "upload"
    dest = _UPLOAD_DIR / f"{uuid.uuid4().hex}_{safe}"
    ext = Path(safe).suffix.lower().lstrip(".")
    # Stream to disk in bounded chunks with a hard size cap. Reading the whole body
    # into memory (await file.read()) let a large or hostile upload exhaust RAM; here
    # an overflow deletes the partial file and returns 413 instead of persisting it.
    # The first chunk (≫ the ~262 bytes filetype needs) is magic-byte validated
    # before we persist further — a disallowed/mismatched type is 415'd and deleted.
    written = 0
    validated = False
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                if not validated:
                    try:
                        _validate_upload(chunk, ext)
                    except HTTPException:
                        out.close()
                        dest.unlink(missing_ok=True)
                        raise
                    validated = True
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.",
                    )
                out.write(chunk)
        if not validated:
            # Empty upload — nothing to sniff and no valid zero-byte type.
            dest.unlink(missing_ok=True)
            raise HTTPException(status_code=415, detail="Empty file upload is not allowed.")
    finally:
        await file.close()
    return {"filename": safe, "path": str(dest.resolve())}


# ═══════════════════════════════════════════════════════════════════════════════
# POST /api/greeting — optional time/day-aware subline (static, zero-cost)
# ═══════════════════════════════════════════════════════════════════════════════
def _greeting_subline() -> str:
    """Short, warm, second-person, time/day-aware line — matches the UI's own
    getGreeting() tone. Static rotation: no model call, zero latency/cost."""
    now = datetime.now()
    h, minute, weekday = now.hour, now.minute, now.weekday()
    t = h + minute / 60.0

    if t < 5:
        pool = [
            "Tell me what you're working on.",
            "Up late. What are we tackling?",
        ]
    elif t < 7:
        pool = [
            "Early start. What's the goal?",
            "Good time to get ahead.",
        ]
    elif t < 12:
        pool = [
            "Good morning. What's the priority?",
            "Fresh start. What should we tackle first?",
            "Ready when you are.",
        ]
    elif t < 13.5:
        pool = [
            "Break time. Back at it soon?",
            "Midday check-in. What do you need?",
        ]
    elif t < 15:  # Afternoon (ends at 3 PM)
        pool = [
            "Deep focus window. What are we solving?",
            "Afternoon momentum. What's next?",
        ]
    elif t < 16:  # Late Afternoon (3 PM - 4 PM)
        pool = [
            "3 o'clock. Anything left to finish?",
            "Winding down from peak hours.",
        ]
    elif t < 18:  # Evening (4 PM - 6 PM)
        pool = [
            "Evening start. Anything before you close out?",
            "How can I help you finish the day?",
        ]
    elif t < 20.5:  # Late Evening (6 PM - 8:30 PM)
        pool = [
            "Good work today. Anything left?",
            "Evening. What can I take off your plate?",
        ]
    else:  # Night (8:30 PM+)
        pool = [
            "Rest well. Back at it tomorrow.",
            "Late hours. What can I wrap up for you?",
        ]

    if weekday >= 5:  # weekend
        pool.append("Weekend mode. What do you need?")

    return pool[minute % len(pool)]


@router.post("/api/greeting")
async def greeting(_: GreetingRequest):
    return {"subline": _greeting_subline()}
