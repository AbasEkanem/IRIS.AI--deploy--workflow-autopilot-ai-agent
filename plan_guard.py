"""plan_guard.py — Refuse a plan that describes IRIS's own procedure instead of the user's work.

The failure, measured
--------------------
On prod's orchestrator (`nvidia/nemotron-3.5-lightning-30b-a3b`) a bare ``hi`` produces
this, reproducibly (``tmp/diag_greeting_steps.py``): three ``write_todos`` calls whose
items are the *execution protocol's own steps* —

    1. Understand user intent and identify required domain(s)
    2. Plan multi-step work with write_todos if needed
    3. Execute delegated subtasks via task() to the appropriate specialist(s)
    4. Verify artifacts and persist learnings to agent.md
    5. Synthesize final response with Final Response Contract

— followed by an Intent Routing Log. The user asked nothing; the model answered by
narrating the harness at them.

Why more prompt text does not fix it
------------------------------------
It has been tried three times, and each round of instruction was verifiably present in
the prompt of the run that then ignored it:

* §0 of ``prompts/iris/execution-protocol.md`` already says a non-task turn takes **no**
  ``write_todos``, and lists greetings by name.
* Line 31 of that file already says, in bold, *"Never plan the protocol"*, and names four
  of the exact strings above as examples of what not to write.
* ``langchain``'s own ``write_todos`` description already says to skip the tool for
  anything under three steps and for "purely conversational" turns
  (``middleware/todo.py:73-79``).

Three "don't" rules in ~17k tokens of prefix, all present, all ignored — on a model with
~3B active parameters that resolves a long instruction stack by doing the most mechanical
thing in front of it. The prefix hands it a numbered procedure and a planning tool; it
plans the procedure. Prose is the wrong instrument: this is the harness's job. Instructing
the model not to do X is advisory. Making X not happen is structural.

What this does
--------------
One ``wrap_tool_call`` gate on ``write_todos``. If the items describe this system's own
operating procedure rather than deliverables for the user, the call is **not executed** —
the guard returns a corrective ``ToolMessage`` naming the offending items and saying what
to write instead. Because the tool never runs, its ``Command`` never lands, so
``state["todos"]`` is never written and no phantom plan is rendered to the user or
persisted to the checkpoint.

Deliberate scope
----------------
* **Turn-type agnostic.** A protocol-shaped plan is wrong on a work turn too, so the guard
  does not try to classify the turn. It only asks "do these items name the user's work, or
  mine?". That keeps it out of the business of deciding what counts as a task — the thing
  prose has repeatedly got wrong here.
* **Two-item threshold.** One item like "Verify outputs" can legitimately belong to a user
  deliverable. Two or more items phrased as my own procedure is not a coincidence. A single
  item that names a harness internal outright (``write_todos``, ``task()``, ``agent.md``,
  the Final Response Contract, the Intent Routing Log) is enough on its own — no user
  deliverable is ever "call write_todos".
* **Budgeted, and it degrades open.** At most ``max_rejections_per_turn`` refusals per real
  user turn, counted off the message history (no new private state, same technique as
  ``todo_reconcile.py``). If a model somehow insists past the cap, the write is allowed
  through — today's cosmetic bug — rather than the turn deadlocking on a guard. Structural
  guards must never be able to hang a live run.

Related: ``todo_reconcile.py`` (stops the *self-correction* this used to trigger),
``prompts/iris/execution-protocol.md`` §0 (the rule this enforces).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

#: The tool this gate is about. Nothing else is inspected.
_GUARDED_TOOL = "write_todos"

#: Stamped into every refusal so prior refusals in the same turn can be counted straight
#: off the transcript. Also what a test greps for.
REJECTION_MARKER = "[PLAN GUARD]"

#: Refusals allowed per real user turn before the guard stands down and lets the write
#: through. Two is enough for the model to read the correction and rewrite; past that,
#: continuing to refuse would trade a cosmetic bug for a stuck turn.
_MAX_REJECTIONS_PER_TURN = 2

#: How many protocol-shaped items make a plan a *protocol* plan. One can be a coincidence
#: ("Verify outputs of the Q3 rollup" is real work); two cannot.
_PROTOCOL_ITEM_THRESHOLD = 2

#: Phrasings lifted from the execution protocol's own step list. Every one of these was
#: observed verbatim in a live or local phantom plan (see the module docstring, the
#: ``todo_reconcile`` docstring, and tmp/diag_greeting_steps.py output) — this is a list of
#: measured failures, not a guess at what a model might say.
_PROTOCOL_STEP_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(understand|capture|identify|determine|clarify|assess)\s+(the\s+)?(user'?s?\s+)?intent\b",
        r"\bidentify\s+(the\s+)?(required\s+|relevant\s+|needed\s+)?domains?\b",
        r"\broute\s+(\S+\s+){0,3}?to\s+(the\s+)?(appropriate|correct|right|relevant|proper)\s+"
        r"(specialist|subagent|sub-agent|agent|domain)",
        r"\b(execute|run|perform|carry\s+out)\s+(the\s+)?delegated\b",
        r"\b(delegate|dispatch)\s+(the\s+)?subtasks?\b",
        r"\bsynthesi[sz]e\s+(the\s+|a\s+)?final\s+(response|answer|output)\b",
        r"\bverify\s+(the\s+)?(artifacts?|artefacts?|outputs?|subagent\s+output)\b",
        r"\bpersist\s+(the\s+|any\s+|novel\s+)*learnings?\b",
        r"\bground\s+(the\s+)?(current\s+)?(datetime|date|time)\b",
        r"\bplan\s+(the\s+)?multi[-\s]?step\b",
        r"\bapply\s+(the\s+)?(final\s+)?response\s+contract\b",
    )
)

#: Names of this system's own machinery. A single item mentioning one of these is decisive
#: on its own — a deliverable for the user is never "call write_todos" or "append to
#: agent.md". Kept separate from the phrasing patterns for exactly that reason.
_HARNESS_SELF_REFERENCE: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bwrite_todos\b",
        r"\btask\(\)",
        r"\bagent\.md\b",
        r"\bfinal\s+response\s+contract\b",
        r"\bintent\s+routing\s+log\b",
        r"\bexecution\s+protocol\b",
        r"\bcompletion\s+contract\b",
        r"\bhitl\s+(gate|approval)\b",
    )
)

def _extract_messages(state: Any) -> list:
    """Pull the message list out of agent state (dict, attribute, or list forms).

    Deliberately a local copy of ``loop_breaker._extract_messages``: the guards in this
    repo stay independent so a change to one cannot silently move another's scope.
    """
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


def _turn_start_index(messages: list) -> int:
    """Index of the message that opened the current user turn (0 if none).

    A ``HumanMessage`` with no ``name`` is the turn marker — every harness nudge in this
    repo carries one — so an unnamed one can only have come from a real user. Same
    convention as ``loop_breaker._turn_start_index`` and ``todo_reconcile``.
    """
    start = 0
    for index, msg in enumerate(messages):
        if isinstance(msg, HumanMessage) and not getattr(msg, "name", None):
            start = index
    return start


def _todo_contents(args: Any) -> list[str]:
    """The ``content`` string of each todo in a ``write_todos`` argument payload.

    Tolerates the shapes a small model actually emits: the documented list of
    ``{"content": ..., "status": ...}`` dicts, bare strings, and dicts that use
    ``task``/``title`` instead of ``content``.
    """
    todos = (args or {}).get("todos") if isinstance(args, dict) else None
    if not isinstance(todos, list):
        return []
    out: list[str] = []
    for item in todos:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            text = item.get("content") or item.get("task") or item.get("title") or ""
            out.append(str(text))
    return [t for t in out if t.strip()]

def protocol_shaped(contents: list[str]) -> list[str]:
    """The subset of ``contents`` that describes this system's procedure, not the user's work.

    Public because it is the whole decision, and a test should be able to assert on it
    directly without building an agent. Returns the offending items in order; an empty list
    means the plan is about the user.

    Two tiers, per the module docstring: any single item naming a harness internal is
    decisive, otherwise ``_PROTOCOL_ITEM_THRESHOLD`` protocol-step phrasings are required.
    """
    self_ref = [c for c in contents if any(p.search(c) for p in _HARNESS_SELF_REFERENCE)]
    steps = [c for c in contents if any(p.search(c) for p in _PROTOCOL_STEP_PATTERNS)]
    flagged = list(dict.fromkeys([*self_ref, *steps]))
    if self_ref:
        return flagged
    if len(steps) >= _PROTOCOL_ITEM_THRESHOLD:
        return flagged
    return []


def _rejections_this_turn(messages: list, current_id: str | None) -> int:
    """How many times this guard has already refused a plan on the current turn.

    Counted off the transcript via ``REJECTION_MARKER`` rather than kept in private state:
    the checkpointer persists messages per ``thread_id``, so the count survives a crash and
    resume, and the guard holds no mutable state that could leak across threads.
    """
    count = 0
    for msg in messages[_turn_start_index(messages):]:
        if not isinstance(msg, ToolMessage):
            continue
        if current_id is not None and getattr(msg, "tool_call_id", None) == current_id:
            continue
        content = getattr(msg, "content", "")
        if isinstance(content, str) and REJECTION_MARKER in content:
            count += 1
    return count


def _rejection_notice(tool_call_id: str, flagged: list[str], total: int) -> ToolMessage:
    """The correction returned in place of executing the call.

    Names the offending items back — a small model corrects far more reliably against a
    quoted mistake than against an abstract rule — then gives the two legal continuations.
    ``status="success"`` matches ``loop_breaker``'s convention: the correction is carried by
    the content, and an error status would put this on the model-error/retry paths, which
    is not what happened.
    """
    listed = "\n".join(f"  - {item.strip()[:120]}" for item in flagged[:6])
    return ToolMessage(
        tool_call_id=tool_call_id,
        status="success",
        content=(
            f"{REJECTION_MARKER} REJECTED — this plan was NOT saved. {len(flagged)} of its "
            f"{total} items describe your own operating procedure instead of the user's "
            f"work:\n{listed}\n\n"
            f"Those steps are how you work; they are never a plan. The user cannot use "
            f"them and must never see them. Do NOT call `write_todos` again with steps "
            f"like these, and do NOT describe your procedure, routing, or intent analysis "
            f"in your reply.\n\n"
            f"Do exactly one of these now:\n"
            f"1) If this turn needs no real work (a greeting, a thank-you, small talk, or "
            f"a question you can simply answer) — skip planning entirely and reply to the "
            f"user directly in one or two plain sentences.\n"
            f"2) If there IS real work — call `write_todos` once with items naming the "
            f"USER'S deliverables and their targets (e.g. \"Pull Q3 pipeline from Attio\", "
            f"\"Post the summary to #revenue\"), not the steps you take to produce them."
        ),
    )

def _decide(request: ToolCallRequest) -> ToolMessage | None:
    """Return a corrective ToolMessage to refuse the plan, or None to let it execute.

    Only ``write_todos`` is inspected; every other tool passes straight through.
    """
    tool_call = request.tool_call or {}
    if tool_call.get("name") != _GUARDED_TOOL:
        return None

    tool_call_id = tool_call.get("id")
    if not tool_call_id:
        # Without an id there is no valid ToolMessage to return — allow, don't crash.
        return None

    contents = _todo_contents(tool_call.get("args", {}) or {})
    if not contents:
        return None

    flagged = protocol_shaped(contents)
    if not flagged:
        return None

    messages = _extract_messages(request.state)
    prior = _rejections_this_turn(messages, tool_call_id)
    if prior >= _MAX_REJECTIONS_PER_TURN:
        # Degrade OPEN, never closed: a cosmetic phantom plan is a far better outcome
        # than a turn that cannot finish because a guard will not yield.
        logger.warning(
            "plan_guard: allowing protocol-shaped plan after %d refusal(s) this turn "
            "(budget exhausted; %d item(s) still protocol-shaped)",
            prior,
            len(flagged),
        )
        return None

    logger.warning(
        "plan_guard: refused protocol-shaped write_todos (%d/%d items; refusal %d of %d): %s",
        len(flagged),
        len(contents),
        prior + 1,
        _MAX_REJECTIONS_PER_TURN,
        " | ".join(item.strip()[:60] for item in flagged[:3]),
    )
    return _rejection_notice(tool_call_id, flagged, len(contents))

class ProtocolPlanGuardMiddleware(AgentMiddleware):
    """Stops a plan made of this system's own steps from ever being written.

    See the module docstring for the measured failure and why prose could not fix it.
    Stateless — every decision comes from the tool arguments plus message history in agent
    state — so it is safe on every agent instance and on both execution paths. Both hooks
    are implemented because the web API and the Slack webhook use the async path while the
    recovery worker uses the sync one.
    """

    name = "ProtocolPlanGuardMiddleware"

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        refusal = _decide(request)
        if refusal is not None:
            return refusal
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        refusal = _decide(request)
        if refusal is not None:
            return refusal
        return await handler(request)
