"""loop_breaker.py — Defensive anti-loop guard for IRIS orchestration.

Problem this solves
--------------------
IRIS delegates work by calling the ``task`` tool (``subagent_type`` +
``description``). When a subagent yielded partial or empty output, IRIS could
read it as "not done" and re-dispatch the *identical* subtask — an unbounded
redispatch loop (the E-21 duplicate-Forms incident). The governance rules
(D-01 / FC-8 "no looping") forbid it, but prompt rules are advisory: the model
can ignore them. This middleware enforces the rule *structurally*, at the
tool-call boundary, so the loop cannot happen regardless of what the model
decides.

Behaviour
---------
* A ``task`` call whose ``(subagent_type, description)`` signature already
  **completed successfully** on this turn is short-circuited — IRIS receives
  the previous result plus an explicit "already done, do not redispatch"
  instruction, and no second subagent run occurs.
* Exactly one **material retry** after a failure is still allowed (D-01);
  identical attempts are then hard-capped so execution always terminates.
* Signatures are exact (whitespace/case-normalised), so legitimately different
  subtasks — and legitimate *continuations* carrying a new description — are
  never blocked. This preserves long-running, multi-step work while killing the
  degenerate repeat.

The decision is derived purely from the message history in agent state, which
the checkpointer persists per ``thread_id``. The guard therefore keeps no
mutable state of its own and cannot leak memory.

Scope: every scan is **turn-scoped**, not thread-scoped — it starts at the last
``HumanMessage`` carrying no ``name`` (the only marker of a genuine user turn;
all harness nudges carry names). This was originally thread-scoped, deliberately,
on the reasoning that a redispatch is a redispatch whenever it happens. In
practice that made the guard misfire on the ordinary case of asking the same
thing twice in one thread: "search Jira for API issues" on turn 1 and again on
turn 5 hash to the same signature, so turn 5 was short-circuited and handed
turn 1's stale result as though it were fresh — with a "do NOT redispatch"
instruction attached, which the model has no way to argue with. The same applies
to the per-tool tiers: a thread-lifetime count of ``tavily_search`` calls
eventually disables a legitimately busy tool for reasons that have nothing to do
with the current turn. A loop is a within-turn phenomenon, so the scan boundary
is now the turn boundary, matching ``_count_task_dispatches_this_turn`` (which
always worked this way) and ``todo_reconcile._turn_start_index``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

# The delegation tool added by deepagents' SubAgentMiddleware.
_TASK_TOOL = "task"

# Identical attempts allowed before a hard stop: 1 initial + 1 material retry.
# The 3rd identical dispatch (prior_count >= this) is blocked outright.
_MAX_IDENTICAL_ATTEMPTS = 2

# Maximum TOTAL task() dispatches per user turn (regardless of whether signatures
# differ). Catches the "semantic loop" where the model slightly rewords each
# dispatch to evade the identical-signature guard — each description is unique
# but the agent is chasing its tail. 10 covers a legitimate 8-step research plan
# with room for one retry; past that, further delegation is blocked and the agent
# must synthesise from what it has. Counted from the last real HumanMessage.
_MAX_TOTAL_TASK_DISPATCHES_PER_TURN = 10


def _normalize_description(text: Any) -> str:
    """Collapse whitespace and case so trivial reformatting still matches."""
    return " ".join(str(text or "").split()).lower()


def _task_signature(args: dict) -> str:
    """Stable signature for a task call: (subagent_type, normalised description)."""
    subagent = str((args or {}).get("subagent_type", "")).strip().lower()
    desc = _normalize_description((args or {}).get("description", ""))
    return hashlib.sha256(f"{subagent}\x00{desc}".encode("utf-8")).hexdigest()


def _extract_messages(state: Any) -> list:
    """Pull the message list out of agent state (dict, attribute, or list forms)."""
    if state is None:
        return []
    if isinstance(state, dict):
        return state.get("messages") or []
    msgs = getattr(state, "messages", None)
    if msgs is not None:
        return msgs
    if isinstance(state, list):
        return state
    return []


def _tool_result(messages: list, tool_call_id: str) -> tuple[str | None, bool]:
    """Return ``(content, is_error)`` for the ToolMessage answering tool_call_id."""
    for msg in messages:
        is_tool_msg = getattr(msg, "type", None) == "tool" or msg.__class__.__name__ == "ToolMessage"
        if is_tool_msg and getattr(msg, "tool_call_id", None) == tool_call_id:
            content = getattr(msg, "content", None)
            if isinstance(content, str):
                text = content
            elif content:
                text = str(content)
            else:
                text = ""
            is_error = getattr(msg, "status", None) == "error"
            return (text or None, is_error)
    return (None, False)


def _turn_start_index(messages: list) -> int:
    """Index of the message that opened the current user turn (0 if none).

    A ``HumanMessage`` with no ``name`` is the turn marker — every harness nudge
    in this repo carries one (``iris_loop_terminator``,
    ``iris_blank_result_recovery``, ``iris_toolcall_repair``,
    ``iris_todo_reconcile``), so an unnamed one can only have come from a real
    user. Everything at or after the LAST such message belongs to the live turn.

    Kept local rather than imported from ``todo_reconcile`` on purpose: the guards
    in this repo are deliberately independent, so a change to one cannot silently
    alter the scope of another.
    """
    start = 0
    for index, msg in enumerate(messages):
        if isinstance(msg, HumanMessage) and not getattr(msg, "name", None):
            start = index
    return start


def _scan_prior(
    messages: list, signature: str, current_id: str | None
) -> tuple[int, str | None]:
    """Count prior identical task dispatches and capture the first successful result.

    Scoped to the current turn (see the module docstring). The current tool call
    (``current_id``) is excluded so only *earlier* dispatches count. The *first*
    success is kept so the original real result — not a later loop-guard notice —
    is what gets reproduced back to IRIS.

    ``_tool_result`` is still given the FULL message list: the answering
    ToolMessage always follows its call, so the window never hides it, and passing
    the whole list keeps the lookup independent of the scan boundary.
    """
    prior_count = 0
    first_success: str | None = None
    for msg in messages[_turn_start_index(messages):]:
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            if tc.get("name") != _TASK_TOOL:
                continue
            tc_id = tc.get("id")
            if current_id is not None and tc_id == current_id:
                continue
            if _task_signature(tc.get("args", {})) != signature:
                continue
            prior_count += 1
            content, is_error = _tool_result(messages, tc_id)
            if content and not is_error and first_success is None:
                first_success = content
    return prior_count, first_success


def _count_task_dispatches_this_turn(messages: list, current_id: str | None) -> int:
    """Count ALL task() dispatches since the last real user message.

    Turn scope comes from ``_turn_start_index`` — this function's inline version of
    that scan is what the helper was extracted from.
    """
    count = 0
    for msg in messages[_turn_start_index(messages):]:
        for tc in getattr(msg, "tool_calls", None) or []:
            if tc.get("name") != _TASK_TOOL:
                continue
            tc_id = tc.get("id")
            if current_id is not None and tc_id == current_id:
                continue
            count += 1
    return count


def _decide(request: ToolCallRequest) -> ToolMessage | None:
    """Return a short-circuit ToolMessage to block a redispatch, or None to allow.

    Only inspects ``task`` calls; every other tool passes straight through.
    """
    tool_call = request.tool_call or {}
    if tool_call.get("name") != _TASK_TOOL:
        return None

    current_id = tool_call.get("id")
    if not current_id:
        # Without an id we cannot build a valid ToolMessage or exclude the
        # current call from the scan — let it through untouched.
        return None

    args = tool_call.get("args", {}) or {}
    signature = _task_signature(args)
    subagent = str(args.get("subagent_type", "?"))

    messages = _extract_messages(request.state)
    prior_count, first_success = _scan_prior(messages, signature, current_id)

    if prior_count == 0:
        # First dispatch of this subtask — check total budget before allowing.
        total = _count_task_dispatches_this_turn(messages, current_id)
        if total >= _MAX_TOTAL_TASK_DISPATCHES_PER_TURN:
            logger.warning(
                "loop_breaker: total task dispatch budget exhausted (%d dispatches this turn)",
                total,
            )
            return ToolMessage(
                tool_call_id=current_id,
                status="success",
                content=(
                    f"⚠️ DISPATCH BUDGET — you have already dispatched {total} subtasks "
                    f"on this turn, which is the maximum allowed. Do NOT dispatch any more. "
                    f"Synthesise a final answer from the results you already have, or "
                    f"report any blockers to the user and finish your turn."
                ),
            )
        return None  # Under budget — first dispatch allowed.

    if first_success is not None:
        logger.warning(
            "loop_breaker: blocked redundant redispatch of completed subtask (subagent=%s)",
            subagent,
        )
        return ToolMessage(
            tool_call_id=current_id,
            status="success",
            content=(
                f"⚠️ LOOP GUARD — this exact subtask was already delegated to "
                f"`{subagent}` and completed. Do NOT redispatch it. The previous "
                f"result is reproduced below; use it and continue to the next step "
                f"(or report completion to the user).\n\n"
                f"--- previous result ---\n{first_success}"
            ),
        )

    if prior_count >= _MAX_IDENTICAL_ATTEMPTS:
        logger.warning(
            "loop_breaker: hard-stopped subtask after %d identical failed attempts (subagent=%s)",
            prior_count,
            subagent,
        )
        return ToolMessage(
            tool_call_id=current_id,
            status="success",
            content=(
                f"⚠️ LOOP GUARD — this exact subtask has already been attempted "
                f"{prior_count} time(s) against `{subagent}` without a usable result. "
                f"Stop retrying it. Report the blocker to the user and move on to any "
                f"remaining independent work; do not dispatch this same subtask again."
            ),
        )

    return None  # Exactly one prior (failed) attempt — allow a single material retry.


class SubagentLoopBreakerMiddleware(AgentMiddleware):
    """Blocks the identical-``task``-redispatch loop at the tool-call boundary.

    See the module docstring for the full rationale. Both the sync and async
    interceptors are implemented so the guard holds on ``.invoke()`` and
    ``.ainvoke()`` paths alike (the Slack webhook uses the async path).
    """

    name = "SubagentLoopBreakerMiddleware"

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        short_circuit = _decide(request)
        if short_circuit is not None:
            return short_circuit
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        short_circuit = _decide(request)
        if short_circuit is not None:
            return short_circuit
        return await handler(request)


# ─────────────────────────────────────────────────────────────────────────────
# General per-tool loop breaker
# ─────────────────────────────────────────────────────────────────────────────
# SubagentLoopBreakerMiddleware above guards ONLY the orchestrator's `task`
# dispatch. It does nothing for a *subagent* that loops on one of its own domain
# tools — e.g. Tavia calling `tavily_search` with the same query over and over
# (the websearch-looping incident). This middleware closes that gap: it caps
# identical (tool name + arguments) calls inside whichever agent it is attached
# to, and is therefore added to IRIS AND to every subagent (subagent middleware
# is isolated — the orchestrator's list does not propagate down).
#
# `task` is intentionally SKIPPED here — its redispatch semantics (one material
# retry, reproduce-prior-result) are owned by SubagentLoopBreakerMiddleware.

# Identical (name, args) tool calls allowed before the SOFT notice fires. The 3rd
# such call (prior_count >= this) gets the reproduce-prior-result note below,
# which also stops the tool from actually EXECUTING again (so no further side
# effects). Two attempts cover a legitimate retry.
_MAX_IDENTICAL_TOOL_CALLS = 2

# Identical calls allowed before the HARD terminator fires. Once a tool has been
# emitted this many times with the same signature, the soft notice has provably
# failed to break the loop, so the companion `wrap_model_call` removes the tool
# from the model's available set and forces the agent to finalise (see
# ToolCallLoopBreakerMiddleware._apply_loop_terminator). Set a little above
# _MAX_IDENTICAL_TOOL_CALLS so the model always gets the gentle correction first
# and only a genuinely stuck agent is force-stopped.
_HARD_STOP_AFTER = 3

# Same-PATH (content ignored) file writes allowed before the HARD terminator
# fires — the churn bound that _tool_signature's content-sensitivity gives up.
# Set well above _HARD_STOP_AFTER because same-path writes with NEW content are
# legitimate progress, not a loop: § 6 of the execution protocol appends a
# learning to `agent.md` on every synthesis, and a real edit_file run touches one
# source file several times. The headroom means only a model with nothing left to
# say — rewriting one path this many times — is force-stopped.
_HARD_STOP_SAME_PATH_AFTER = 8

# Cap on the previous result reproduced back into the block notice, so injecting
# it cannot itself balloon the context of an already-struggling small model.
_MAX_REPRODUCED_RESULT_CHARS = 4000

# File tools whose target PATH is normalised into the loop key (see
# _tool_signature). Membership does NOT exempt the mutating args from the key: it
# only means the path is whitespace-canonicalised before hashing, so one target
# reads as one target rather than two. `read_file` is deliberately absent — it is
# non-mutating and keeps plain full-args hashing.
_PATH_KEYED_TOOLS = {"write_file", "edit_file"}
_PATH_ARG_CANDIDATES = ("file_path", "path", "filename")

# Message-name tag for the finalisation directive the terminator injects, kept
# distinct so it is easy to spot in a transcript/log and never collides with a
# real user turn (mirrors blank_recovery.py's named-nudge convention).
_LOOP_TERMINATION_SOURCE = "iris_loop_terminator"


def _tool_signature(name: str, args: Any) -> str:
    """Stable signature for a tool call: (name, canonicalised full arguments).

    Content is part of the key for file writes, and that is the whole invariant:
    an IDENTICAL repeat is a loop (blocked); a repeat carrying CHANGED content is
    progress (allowed). Keying write_file/edit_file on the path ALONE — as this
    did — collapses every write to one path into a single signature, so the 2nd
    or 3rd *legitimate* append is short-circuited. That is not hypothetical: the
    execution protocol (prompts/iris/execution-protocol.md § 6) mandates
    appending a `[GUARDRAIL E-XX]` learning to `agent.md` via write_file before
    every synthesis, so on a long thread the self-improvement log silently
    stopped being written — the guard ate the append, not a loop.

    Path-keyed tools still normalise the path's whitespace so one target reads as
    one target, but the mutating args (`content`, `old_string`/`new_string`) are
    hashed in alongside it. Distinct-content rewrites of one file therefore stay
    distinct here; the genuinely stuck rewriter is still caught, one tier up, by
    _looping_tool_names/_HARD_STOP_AFTER counting same-path calls (which is where
    same-path collapsing belongs — a count, not an execution veto). Falls back to
    plain full-args hashing when the path arg is missing.
    """
    if name in _PATH_KEYED_TOOLS and isinstance(args, dict):
        path = next((args[k] for k in _PATH_ARG_CANDIDATES if args.get(k)), None)
        if path:
            key = " ".join(str(path).split())
            try:
                rest = json.dumps(
                    {k: v for k, v in args.items() if k not in _PATH_ARG_CANDIDATES},
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                )
            except Exception:
                rest = str(args)
            return hashlib.sha256(
                f"{name}\x00path\x00{key}\x00{rest}".encode("utf-8")
            ).hexdigest()
        # Path missing — fall through to conservative full-args hashing.
    try:
        canon = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        canon = str(args)
    return hashlib.sha256(f"{name}\x00{canon}".encode("utf-8")).hexdigest()


def _tool_path_signature(name: str, args: Any) -> str:
    """Coarser key for the HARD terminator: the target path, ignoring content.

    _tool_signature deliberately admits content-varied writes so real progress is
    never blocked. That leaves the original failure mode — a model rewriting ONE
    file forever with slightly reworded content, each attempt unique under the
    fine key — uncaught. This key restores the same-path collapse, but only for
    counting toward _HARD_STOP_AFTER: enough same-path churn disables the tool and
    forces finalisation, while any single write with new content still executes.
    Non-path tools are unchanged — they defer to _tool_signature.
    """
    if name in _PATH_KEYED_TOOLS and isinstance(args, dict):
        path = next((args[k] for k in _PATH_ARG_CANDIDATES if args.get(k)), None)
        if path:
            key = " ".join(str(path).split())
            return hashlib.sha256(f"{name}\x00path\x00{key}".encode("utf-8")).hexdigest()
    return _tool_signature(name, args)


def _scan_prior_tool(
    messages: list, name: str, signature: str, current_id: str | None
) -> tuple[int, str | None]:
    """Count prior identical calls to `name` and capture the first good result.

    Mirrors _scan_prior — same turn scope, same full-list ToolMessage lookup — but
    keyed on the full (name, args) signature rather than the task-specific
    (subagent_type, description) pair.
    """
    prior_count = 0
    first_success: str | None = None
    for msg in messages[_turn_start_index(messages):]:
        for tc in getattr(msg, "tool_calls", None) or []:
            if tc.get("name") != name:
                continue
            tc_id = tc.get("id")
            if current_id is not None and tc_id == current_id:
                continue
            if _tool_signature(name, tc.get("args", {})) != signature:
                continue
            prior_count += 1
            content, is_error = _tool_result(messages, tc_id)
            if content and not is_error and first_success is None:
                first_success = content
    return prior_count, first_success


def _decide_tool(request: ToolCallRequest) -> ToolMessage | None:
    """Return a short-circuit ToolMessage to block a repeated call, or None.

    Skips `task` (owned by SubagentLoopBreakerMiddleware) and any call without an
    id (cannot build a valid ToolMessage or exclude the current call from scan).
    """
    tool_call = request.tool_call or {}
    name = tool_call.get("name")
    if not name or name == _TASK_TOOL:
        return None

    current_id = tool_call.get("id")
    if not current_id:
        return None

    args = tool_call.get("args", {}) or {}
    signature = _tool_signature(name, args)

    messages = _extract_messages(request.state)
    prior_count, first_success = _scan_prior_tool(messages, name, signature, current_id)

    if prior_count < _MAX_IDENTICAL_TOOL_CALLS:
        return None  # First/second identical call — allowed.

    logger.warning(
        "loop_breaker: hard-stopped tool `%s` after %d identical call(s) with the "
        "same arguments",
        name,
        prior_count,
    )
    note = (
        f"⚠️ LOOP GUARD — the tool `{name}` has already been called {prior_count} "
        f"time(s) in this run with these exact arguments. Do NOT call it again with "
        f"the same arguments. "
    )
    if first_success:
        reproduced = first_success
        if len(reproduced) > _MAX_REPRODUCED_RESULT_CHARS:
            reproduced = reproduced[:_MAX_REPRODUCED_RESULT_CHARS] + "\n…[truncated]"
        note += (
            "Use the previous result below; if you need different information, change "
            f"the arguments or move on to the next step.\n\n"
            f"--- previous `{name}` result ---\n{reproduced}"
        )
    else:
        note += (
            "The previous attempts returned no usable result. Stop repeating this "
            "call — try a different approach or report the blocker and continue."
        )
    return ToolMessage(tool_call_id=current_id, status="success", content=note)


# ─────────────────────────────────────────────────────────────────────────────
# Hard terminator (model-call side)
# ─────────────────────────────────────────────────────────────────────────────
# The soft notice above stops the looping tool from *executing* again, but a
# determined model can keep re-emitting the call every turn (the live run). The
# real terminator lives on the model-call boundary: once a tool has been emitted
# _HARD_STOP_AFTER times with the same signature (or, for file writes,
# _HARD_STOP_SAME_PATH_AFTER times against one path regardless of content), we
# remove it from the tools the model can see on the next call and append a
# directive to finalise. Unable to call the tool and told to wrap up, the agent
# emits a final answer and its turn ends cleanly — for a subagent this returns
# control (with a partial summary) to the orchestrator via the `task` tool. This
# only rewrites the model input for the call; it never raises and never
# interferes with GraphInterrupt/HITL.


def _tool_name_of(tool: Any) -> str | None:
    """Best-effort tool name from a BaseTool instance or an OpenAI-style dict."""
    name = getattr(tool, "name", None)
    if name:
        return name
    if isinstance(tool, dict):
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return fn["name"]
        return tool.get("name")
    return None


def _looping_tool_names(messages: list) -> set[str]:
    """Names of non-`task` tools the agent is genuinely stuck on.

    Two counts per call, each with its own ceiling, because "stuck" has two
    shapes. The exact signature (content included) trips at _HARD_STOP_AFTER —
    the same call re-emitted verbatim. For file writes the coarse path signature
    also trips, at the much higher _HARD_STOP_SAME_PATH_AFTER: content-varied
    rewrites of one path each look unique to the exact key, so without this the
    forever-rewriter escapes both tiers, but the ceiling has to leave room for the
    mandated `agent.md` appends and for multi-edit work on one source file.

    Grouping by signature means a tool called many times with *different*
    arguments (legitimate distinct work) is never flagged. `task` is excluded
    (owned by SubagentLoopBreakerMiddleware). Counts are turn-scoped: a tool
    disabled for the rest of *this* turn must be available again on the next one,
    which is also exactly what the terminator's own directive promises the model
    ("DISABLED for the rest of this turn").
    """
    counts: dict[tuple[str, str], int] = {}
    stuck: set[str] = set()
    for msg in messages[_turn_start_index(messages):]:
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name")
            if not name or name == _TASK_TOOL:
                continue
            args = tc.get("args", {}) or {}
            # (key, ceiling) pairs: the exact key always applies; the coarse
            # path key only adds a second, laxer bound for the file tools.
            probes = [((name, _tool_signature(name, args)), _HARD_STOP_AFTER)]
            if name in _PATH_KEYED_TOOLS:
                probes.append(
                    ((name, _tool_path_signature(name, args)), _HARD_STOP_SAME_PATH_AFTER)
                )
            for key, ceiling in probes:
                counts[key] = counts.get(key, 0) + 1
                if counts[key] >= ceiling:
                    stuck.add(name)
    return stuck


def _finalization_directive(names: set[str]) -> HumanMessage:
    """The steering nudge injected when the terminator fires.

    A ``HumanMessage`` with a distinctive ``name`` — the channel the Nemotron
    orchestrator reliably acts on and the transcript/UI renders as a correction
    (mirrors blank_recovery.py), rather than a mid-conversation system message.
    """
    listed = ", ".join(sorted(f"`{n}`" for n in names))
    return HumanMessage(
        name=_LOOP_TERMINATION_SOURCE,
        content=(
            f"⛔ LOOP TERMINATOR — you have called {listed} repeatedly on the same "
            f"target without making progress, so it is now DISABLED for the rest of "
            f"this turn and is no longer available to you. Do NOT try to call it again "
            f"and do NOT restate this notice. Do this instead, now:\n"
            f"1) Briefly summarise what you actually completed and what is blocked by "
            f"this failure.\n"
            f"2) If independent steps remain that do NOT need the disabled tool, "
            f"continue with them.\n"
            f"3) Otherwise finish your turn with that summary. If you are a subagent, "
            f"return the summary to the orchestrator so it can decide how to proceed."
        ),
    )


def _apply_loop_terminator(request: ModelRequest) -> ModelRequest:
    """Force finalisation when the agent is stuck repeating a tool.

    Rewrites only the model input for this one call — strips the stuck tool(s)
    from the available set and appends the finalisation directive — then returns
    the (possibly overridden) request. Returns the original request unchanged
    when nothing is looping. Pure and framework-safe: never raises, never touches
    GraphInterrupt, skips `task`.
    """
    messages = list(getattr(request, "messages", None) or [])
    stuck = _looping_tool_names(messages)
    if not stuck:
        return request

    kept = [t for t in (request.tools or []) if _tool_name_of(t) not in stuck]

    # request.tools is rebuilt full on every model call, so we must re-strip each
    # turn while stuck; but only append the directive once — if our named nudge is
    # already in the recent tail, don't stack another copy.
    already = any(getattr(m, "name", None) == _LOOP_TERMINATION_SOURCE for m in messages[-5:])
    if already:
        return request.override(tools=kept)

    logger.warning(
        "loop_breaker: TERMINATING loop — disabling %s (>=%d identical, or >=%d "
        "same-path, call(s)); forcing finalisation",
        ", ".join(sorted(stuck)),
        _HARD_STOP_AFTER,
        _HARD_STOP_SAME_PATH_AFTER,
    )
    return request.override(tools=kept, messages=[*messages, _finalization_directive(stuck)])


class ToolCallLoopBreakerMiddleware(AgentMiddleware):
    """Caps identical (name + args) tool calls within a single agent.

    Complements SubagentLoopBreakerMiddleware: that one guards `task` at the
    orchestrator; this one guards every OTHER tool wherever it is attached, so a
    subagent (e.g. Tavia looping on search, or aurther rewriting one file) cannot
    loop on a domain tool. Two tiers:

    * **Soft (tool-call side).** The 2nd+ identical call is short-circuited with a
      reproduce-prior-result notice and does not execute — no further side effects.
    * **Hard (model-call side).** Once a tool hits _HARD_STOP_AFTER identical
      calls, `wrap_model_call` removes it from the model's tool set for the next
      call and injects a finalisation directive, so the agent stops re-emitting it
      and wraps up. See `_apply_loop_terminator`.

    Stateless — every decision is derived from message history in agent state — so
    it is safe on both the sync and async paths and on every agent instance. All
    four hooks are implemented so the guard holds on `.invoke()` and `.ainvoke()`.
    """

    name = "ToolCallLoopBreakerMiddleware"

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        short_circuit = _decide_tool(request)
        if short_circuit is not None:
            return short_circuit
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        short_circuit = _decide_tool(request)
        if short_circuit is not None:
            return short_circuit
        return await handler(request)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(_apply_loop_terminator(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> ModelResponse:
        return await handler(_apply_loop_terminator(request))
