"""blank_recovery.py — Recover IRIS from blank subtask results and empty completions.

Problem this solves
--------------------
The orchestrator model (nvidia/nemotron-3-ultra-550b-a55b) intermittently emits
completely empty completions, and its subagents sometimes finish with no
non-empty final ``AIMessage``. Two gaps in the harness turn either event into a
silent dead-end that strands a multi-step run partway through its plan:

* **Blank ``task`` result.** deepagents' subagent extractor defaults the forwarded
  content to ``""`` when the subagent produced no non-empty final AIMessage, and
  returns it wrapped in a ``Command`` (deepagents/middleware/subagents.py). Because
  it is a ``Command`` and not a raw ``ToolMessage``, the Nemotron profile's
  empty→"(empty tool result)" normalizer (which only rewrites raw ToolMessage
  returns) never touches it — the orchestrator receives a blank ``task`` result.
* **Empty completion.** The orchestrator then emits an ``AIMessage`` with no text
  and no tool calls. LangGraph ends the run on a tool-call-less AIMessage, and
  every Nemotron guard is gated behind ``_is_final_answer`` — which requires
  NON-EMPTY text — so an empty completion is invisible to all of them. The run
  settles ``idle`` mid-plan; the remaining (often HITL-gated) steps never run.

Behaviour
---------
Two bounded, orchestrator-scoped recoveries (this middleware is not propagated to
subagents — see loop_breaker.py:226), each aligned to IRIS's own governance rules
(D-04 / FC-7 "blank result = FAILED, never success"; D-01 / FC-8 "one material
retry, never an identical redispatch"; FC-5 "never emit filler / stall"):

* **Hook A — ``before_model`` (persisted):** when the last message is a blank
  ``task`` result, append a recovery nudge to graph state — telling the
  orchestrator to treat the blank as FAILED and either re-dispatch that step once
  with a materially-changed brief or mark it blocked and advance, never stop on
  it. The nudge is **persisted** into the conversation (not request-only): it
  stays in the model's context on every following turn and is visible in the
  transcript, so the correction keeps steering the run instead of evaporating
  after a single call. A blank ``task`` result is ``messages[-1]`` only for the
  model call that immediately follows it, so the nudge is appended exactly once
  per blank result with no bookkeeping.
* **Hook B — ``after_agent`` (``can_jump_to=["model"]``):** the backstop. When the
  agent is about to finish on an empty completion (no text, no tool calls),
  ``after_agent`` REMOVES the blank turn (so the model does not echo it back),
  APPENDS a stronger continue-or-finalize nudge to state (persisted), bumps a
  private counter, and jumps back to the model. Hard-capped at
  ``_MAX_EMPTY_RECOVERIES`` jumps (and skipped once history is large) so it can
  never itself loop — past the cap the run is allowed to end.

Persisted state is the recovery nudges themselves (so they remain in the model's
context and in the transcript/UI) plus one private counter in the ``state_schema``
(mirroring the Nemotron policy-nudge guards). The guard keeps no mutable instance
state and is correct on both the ``.invoke`` and ``.ainvoke`` paths and across
resumed threads.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    PrivateStateAttr,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

logger = logging.getLogger(__name__)

# The delegation tool added by deepagents' SubAgentMiddleware.
_TASK_TOOL = "task"

# Placeholder the Nemotron shim writes for empty *raw* ToolMessage results; a
# blank `task` result usually arrives as "" (it is Command-wrapped, so the shim
# misses it), but we treat the placeholder as blank too for completeness.
_EMPTY_TOOL_PLACEHOLDER = "(empty tool result)"

# Message-name tags for the injected nudges (kept distinct so they are easy to
# spot in a transcript and never collide with a real user turn). The nudges are
# persisted, so these names appear on the recovery messages written to graph state.
_BLANK_TASK_SOURCE = "iris_blank_result_recovery"
_EMPTY_COMPLETION_SOURCE = "iris_empty_completion_recovery"

# Hook B is hard-capped so it can never loop: at most this many jump-backs per
# USER TURN. Past the cap the turn stops with an honest terminal answer
# (_GIVE_UP_TEXT) instead of jumping again — idle beats an infinite loop, but a
# blank bubble beats neither. Two covers a transient empty completion plus one
# follow-up.
#
# PER TURN, not per thread. This counter used to be a thread-LIFETIME total, which
# quietly switched the guard off forever: once a thread had spent its 2 recoveries
# at any point in its history, every LATER turn tripped the cap on its very first
# empty completion and the run was allowed to end blank. A forensic sweep of the 17
# most recent web threads found 12 of them already pinned at 2 — i.e. empty-
# completion recovery was dead on most live conversations. See
# _real_user_turn_key + the fence in _empty_completion_recovery.
_MAX_EMPTY_RECOVERIES = 2

# Belt-and-suspenders ceiling (mirrors the Nemotron profile's _repair_loop_risk):
# never jump back when the history is already this large — an empty completion on
# a huge run is treated as a genuine stop rather than risking more churn.
_MAX_AI_MESSAGES = 50
_MAX_TOOL_MESSAGES = 110


# ─────────────────────────────────────────────────────────────────────────────
# Local helpers (self-contained — no cross-package import of the vendored
# Nemotron profile's private helpers, which a `pip install` would wipe).
# ─────────────────────────────────────────────────────────────────────────────
def _messages(state: Any) -> list:
    """Pull the message list out of agent state (dict or attribute form)."""
    if isinstance(state, dict):
        return state.get("messages") or []
    return getattr(state, "messages", None) or []


def _state_int(source: Any, key: str) -> int:
    """Read an int-valued private counter from state.

    Tolerant of dict or attribute form and of a missing/None value, so a schema
    change can never turn a read into a hard failure (fail-safe: 0)."""
    state = source.state if hasattr(source, "state") else source
    if isinstance(state, dict):
        value = state.get(key)
    else:
        value = getattr(state, key, None) if state is not None else None
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _message_text(message: Any) -> str:
    """Flatten message content (str or list-of-blocks) to plain text."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _content_is_blank(message: Any) -> bool:
    """True when a message carries no usable text (empty, whitespace, or the
    empty-result placeholder)."""
    text = _message_text(message).strip()
    return text == "" or text == _EMPTY_TOOL_PLACEHOLDER


def _is_empty_completion(message: Any) -> bool:
    """True for a dead-end assistant turn: an ``AIMessage`` with no tool calls and
    no text. This is the shape LangGraph ends a run on."""
    return (
        isinstance(message, AIMessage)
        and not message.tool_calls
        and not _message_text(message).strip()
    )


def _task_call_for(messages: list, tool_message: ToolMessage) -> dict | None:
    """Return the `task` tool_call this ToolMessage answers, or None.

    Only `task` calls qualify — a blank result from an ordinary domain tool is
    often legitimate (e.g. an empty search), so scoping to `task` avoids false
    nudges on those.
    """
    target = getattr(tool_message, "tool_call_id", None)
    if not target:
        return None
    for msg in messages:
        if isinstance(msg, AIMessage):
            for call in msg.tool_calls or []:
                if call.get("id") == target:
                    return call if call.get("name") == _TASK_TOOL else None
    return None


def _count(messages: list, cls: type) -> int:
    return sum(1 for m in messages if isinstance(m, cls))


def _state_str(source: Any, key: str) -> str:
    """Read a str-valued private field from state (fail-safe: "")."""
    state = source.state if hasattr(source, "state") else source
    if isinstance(state, dict):
        value = state.get(key)
    else:
        value = getattr(state, key, None) if state is not None else None
    return str(value) if isinstance(value, str) else ""


def _real_user_turn_key(messages: list) -> str:
    """Stable identifier for the CURRENT user turn.

    Every guardrail nudge in this codebase is persisted as
    ``HumanMessage(name=<source>)``, so a ``HumanMessage`` with NO ``name`` is the
    only marker of a genuine new user turn. The key pairs that message's id with
    the running count of real user messages: the id alone is normally enough, but
    LangGraph only assigns ids on ingestion, and the count keeps the key changing
    per turn even if an id is ever absent.

    Deliberately constant for the whole turn — including across a HITL approval and
    across a crash resume, neither of which may hand back a fresh recovery budget
    (the same empty completion would then be retried indefinitely).
    """
    n = 0
    last_id = ""
    for msg in messages:
        if isinstance(msg, HumanMessage) and not getattr(msg, "name", None):
            n += 1
            last_id = str(getattr(msg, "id", "") or "")
    return f"{n}:{last_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Nudge text + persisted-nudge planners (pure — no request/runtime object, so
# they are trivially unit-testable offline; see tmp/test_blank_recovery.py).
# ─────────────────────────────────────────────────────────────────────────────
def _blank_task_text(subagent: str) -> str:
    return (
        f"The last subtask you delegated to `{subagent}` returned NO output (a blank result). "
        "Per the delegation rules a blank or missing subagent result is a FAILURE, not a success "
        "(D-04 / FC-7): do not treat it as done, and do NOT end the run here. Take ONE of these "
        "actions now:\n"
        "1) Re-dispatch that step ONCE with a materially-changed brief — clearer inputs, a "
        "narrower ask, or an explicit expected-output contract. An IDENTICAL redispatch is "
        "blocked by the loop guard (D-01 / FC-8), so you MUST change the brief.\n"
        "2) If that step is not required for the user's objective, mark it blocked/failed in "
        "write_todos and advance to the next pending step.\n"
        "Continue until every planned todo reaches a terminal state — never stop on a blank result."
    )


_EMPTY_COMPLETION_TEXT = (
    "You returned an EMPTY response with no tool call. That would end the run with the "
    "user's objective unfinished, which is not a valid completion (FC-5). Do NOT stop here. "
    "Check your write_todos plan and take ONE of these actions:\n"
    "1) If any planned step is still pending or in_progress, dispatch it now via task(...).\n"
    "2) If the last step failed or returned blank, retry it ONCE with a materially-changed "
    "brief, or mark it blocked/failed and move to the next step.\n"
    "3) ONLY if every planned todo is genuinely in a terminal state, emit the Final Response "
    "Contract (STATUS / SUMMARY / ARTIFACTS / BLOCKERS / LEARNING) — never an empty message."
)


# Written into state when the recovery budget for THIS turn is spent. Hook B used
# to `return None` here, which handed the caller an empty AIMessage as the turn's
# result: the chat rendered a blank bubble and the run "ended with failure" with
# nothing to show. Giving up is still correct — jumping again would loop — but it
# must give up OUT LOUD. This replaces the blank turn so the answer-extraction in
# web_api always finds real text, and the user learns what actually happened.
_GIVE_UP_TEXT = (
    "**STATUS:** INCOMPLETE\n\n"
    "**SUMMARY:** I stopped returning content mid-run — the model produced empty responses "
    f"{_MAX_EMPTY_RECOVERIES} times in a row on this turn, and the recovery guard is now "
    "exhausted, so I ended the turn rather than loop.\n\n"
    "**BLOCKERS:** Whatever work was already completed is preserved in this thread's history "
    "and does not need redoing. Send the request again — mentioning only the step that is "
    "still outstanding — and I will continue from there."
)


def _blank_task_nudge(messages: list) -> HumanMessage | None:
    """Return the recovery ``HumanMessage`` to PERSIST when the last message is a
    blank ``task`` result, or ``None`` to leave state unchanged.

    Persisted (not request-only): the caller appends the returned message to graph
    state via ``before_model``, so it stays in the model's context on every later
    turn and is visible in the transcript. Only ``task`` results qualify — a blank
    result from an ordinary domain tool (e.g. an empty search) is often legitimate.
    """
    if not messages:
        return None
    last = messages[-1]
    if not (isinstance(last, ToolMessage) and _content_is_blank(last)):
        return None
    call = _task_call_for(messages, last)
    if call is None:
        return None  # Not a `task` result — leave ordinary tools alone.
    subagent = str((call.get("args") or {}).get("subagent_type") or "the subtask")
    logger.warning(
        "blank_recovery: blank `task` result from subagent=%s — persisting recovery nudge",
        subagent,
    )
    return HumanMessage(content=_blank_task_text(subagent), name=_BLANK_TASK_SOURCE)


class BlankRecoveryState(AgentState):
    """State schema for the blank-result / empty-completion recovery guard."""

    # Count of empty-completion jump-backs performed for the CURRENT user turn
    # (Hook B hard cap). The recovery nudges themselves are persisted into
    # ``messages``; these two fields are the only extra private state.
    iris_empty_completion_recoveries: NotRequired[Annotated[int, PrivateStateAttr]]

    # Which turn the counter above belongs to (see _real_user_turn_key). When the
    # live turn key differs from this, the counter is stale and the budget resets —
    # that reset is the whole fix: without it the counter was a thread-lifetime
    # total and the guard died permanently after two recoveries.
    iris_empty_completion_turn: NotRequired[Annotated[str, PrivateStateAttr]]


class BlankResultRecoveryMiddleware(AgentMiddleware):
    """Make blank subtask results and empty completions recoverable, not terminal.

    See the module docstring for the full rationale. The recovery nudges are
    PERSISTED into graph state — Hook A (``before_model``) appends the blank-task
    nudge, Hook B (``after_agent``) removes the empty turn, appends the
    continue-or-finalize nudge, and supplies the bounded jump-back — so the
    corrections stay in the model's context and in the transcript instead of
    vanishing after one call. Both sync and async variants are implemented so the
    guard holds on ``.invoke`` and ``.ainvoke`` (the Slack webhook uses the async
    path). Orchestrator-only by design — the middleware list is not propagated to
    subagents.
    """

    name = "BlankResultRecoveryMiddleware"
    state_schema = BlankRecoveryState

    # ── Hook A: blank `task` result → persist a one-step recovery nudge ──────
    def before_model(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        """Append the blank-task recovery nudge to state before the next model call."""
        nudge = _blank_task_nudge(_messages(state))
        return {"messages": [nudge]} if nudge is not None else None

    async def abefore_model(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        """Async variant of `before_model`."""
        nudge = _blank_task_nudge(_messages(state))
        return {"messages": [nudge]} if nudge is not None else None

    @staticmethod
    def _give_up(messages: list, turn_key: str) -> dict[str, Any]:
        """Swap the empty final turn for an honest terminal answer (see _GIVE_UP_TEXT).

        Deliberately an UNNAMED ``AIMessage``: a ``name`` would file it in the
        guardrail taxonomy and the UI would render it as a collapsed correction
        card, leaving the chat bubble empty again — the exact symptom being fixed.
        This text IS the turn's answer, so it must look like one. No ``jump_to``:
        the run ends here, and because the tail is no longer an empty completion
        this hook is a no-op if it is ever re-entered.
        """
        updates: list = []
        empty_id = getattr(messages[-1], "id", None)
        if empty_id:
            updates.append(RemoveMessage(id=empty_id))
        updates.append(
            AIMessage(
                content=_GIVE_UP_TEXT,
                response_metadata={"iris_blank_recovery_exhausted": True},
            )
        )
        return {
            "messages": updates,
            "iris_empty_completion_recoveries": _MAX_EMPTY_RECOVERIES,
            "iris_empty_completion_turn": turn_key,
        }

    # ── Hook B: empty completion backstop → strip blank turn, persist nudge,
    #    jump back to the model ────────────────────────────────────────────────
    @staticmethod
    def _empty_completion_recovery(state: Any) -> dict[str, Any] | None:
        """Recover (bounded per turn) when the run is about to end on an empty
        completion. Removes the blank turn, appends the continue-or-finalize nudge
        (persisted), bumps the counter, and jumps back to the model.
        """
        messages = _messages(state)
        if not messages or not _is_empty_completion(messages[-1]):
            return None

        # ── Per-TURN budget fence ────────────────────────────────────────────
        # A stored key from an earlier turn (or no key at all, which is every
        # thread written before this fix) means this turn has spent nothing yet.
        # That makes already-exhausted threads un-stick themselves on their next
        # message, so no checkpoint migration is needed.
        turn_key = _real_user_turn_key(messages)
        recoveries = _state_int(state, "iris_empty_completion_recoveries")
        if _state_str(state, "iris_empty_completion_turn") != turn_key:
            recoveries = 0

        if recoveries >= _MAX_EMPTY_RECOVERIES:
            logger.warning(
                "blank_recovery: empty completion after %d recoveries this turn (%s) — "
                "ending the turn with an explicit incomplete answer",
                recoveries,
                turn_key,
            )
            return BlankResultRecoveryMiddleware._give_up(messages, turn_key)
        if _count(messages, AIMessage) >= _MAX_AI_MESSAGES or _count(messages, ToolMessage) >= _MAX_TOOL_MESSAGES:
            # History already huge — treat the empty stop as genuine, but still say
            # so out loud rather than handing back a blank bubble.
            logger.warning(
                "blank_recovery: empty completion with oversized history "
                "(ai=%d tool=%d) — ending the turn with an explicit incomplete answer",
                _count(messages, AIMessage),
                _count(messages, ToolMessage),
            )
            return BlankResultRecoveryMiddleware._give_up(messages, turn_key)

        logger.warning(
            "blank_recovery: empty completion (recovery %d/%d this turn) — persisting nudge and jumping back to the model",
            recoveries + 1,
            _MAX_EMPTY_RECOVERIES,
        )

        # Drop the blank turn (so the model does not echo it back) then append the
        # persisted nudge. An empty completion has no tool calls, so removing it
        # can never orphan a tool_call. RemoveMessage needs the message id; if it
        # is somehow absent we simply leave the blank turn in place.
        updates: list = []
        empty_id = getattr(messages[-1], "id", None)
        if empty_id:
            updates.append(RemoveMessage(id=empty_id))
        updates.append(HumanMessage(content=_EMPTY_COMPLETION_TEXT, name=_EMPTY_COMPLETION_SOURCE))
        return {
            "messages": updates,
            "iris_empty_completion_recoveries": recoveries + 1,
            "iris_empty_completion_turn": turn_key,
            "jump_to": "model",
        }

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        """Recover once (bounded) when the run is about to end on an empty completion."""
        return self._empty_completion_recovery(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        """Async variant of `after_agent`."""
        return self._empty_completion_recovery(state)
