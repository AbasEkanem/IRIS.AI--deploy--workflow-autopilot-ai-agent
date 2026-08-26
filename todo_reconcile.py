"""todo_reconcile.py — Don't let a long run end with its plan left half-open.

Problem this solves
-------------------
On a long multi-step task IRIS lays out a plan with ``write_todos``, works through
part of it, and then answers — leaving todos sitting at ``pending`` /
``in_progress`` that were in fact finished, blocked, or abandoned. The plan the
user is shown goes stale, so completed work looks unfinished and dropped work
looks like it is still coming.

Nothing structural prevented it. ``TodoListMiddleware`` registers the
``write_todos`` tool and stores the list in ``state["todos"]``
(langchain/agents/middleware/todo.py), but *calling it again is voluntary* — the
tool description asks the model to keep the list current and no code checks that it
did. The failure rate compounds with run length: the longer the run, the further
the final answer is from the planning call, and the likelier the model treats the
plan as scratch paper it has finished with.

Fix
---
One bounded ``after_agent`` gate. When the agent is about to end a turn in which it
DID write a plan, and that plan still has non-``completed`` entries, the guard
appends a persisted nudge naming the unfinished steps and jumps back to the model
once. The model then either does the outstanding work or reconciles the list.

Scope decisions, each one deliberately narrow so this can never nag:

* **Only a plan written in THIS turn counts.** The guard requires a ``write_todos``
  call after the current turn boundary. Todos left over from an earlier turn are
  ignored — otherwise a short follow-up question ("what was that ticket ID
  again?") would be nudged about a plan from ten minutes ago that the user has
  moved on from. This also matches the reported symptom exactly: the list goes
  stale *within* the long run that created it.
* **Only a real prose answer counts.** An EMPTY completion is
  ``blank_recovery.py``'s Hook B, and a completion that is really an unparsed
  tool-call blob is ``tool_call_repair.py``'s. Both of those already jump back to
  the model with a better-targeted nudge, so this guard stands down for them
  rather than racing them.
* **Once per user turn, hard.** A model that will not reconcile its list would
  otherwise be nudged forever — the same trap ``blank_recovery``'s per-turn
  counter exists to close.

Ordering note (load-bearing, and NOT obvious): ``after_agent`` hooks run in
REVERSE registration order — LangChain makes the LAST-listed such middleware the
entry to the chain and walks back to the first before ``END``
(langchain/agents/factory.py:1805-1826). This middleware is registered *after*
``BlankResultRecoveryMiddleware`` and ``MalformedToolCallRepairMiddleware``, so its
hook runs BEFORE theirs, and a ``jump_to`` here would short-circuit them. That is
why the deference above is implemented as explicit checks in
``_pending_summary`` rather than left to list position: position is easy to change
by accident, and a check is not.

Orchestrator-only by design — subagents do not plan, and middleware lists are not
propagated into them (see subagent_config.py).
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
from langchain_core.messages import AIMessage, HumanMessage

from tool_call_repair import find_tool_call_blob

logger = logging.getLogger(__name__)

# Message-name tag for the injected nudge. Named (not anonymous) so the guardrail
# taxonomy files it as a correction and the UI renders it as a collapsed
# correction card instead of something the user appears to have typed. Must stay in
# sync with guardrail_taxonomy.py and its UI mirror ui/src/lib/corrections.ts.
RECONCILE_SOURCE = "iris_todo_reconcile"

# The planning tool whose state field this guard watches.
_WRITE_TODOS = "write_todos"

# The ONLY status that counts as closed. langchain's Todo TypedDict declares
# status as Literal["pending", "in_progress", "completed"] (todo.py:26-33), and
# WriteTodosInput is a pydantic model, so pydantic rejects any other value. That is
# why the nudge tells the model to record a blocked/dropped outcome in the todo's
# free-text `content` and still set status to "completed" — instructing it to write
# status="blocked" would just trade a stale plan for a tool-validation error.
_DONE = "completed"

# Hard cap per USER TURN. One is enough: the nudge names the exact unfinished
# steps, so a model that ignores it once will ignore it twice, and jumping again
# would spend the run's remaining super-steps arguing with itself.
#
# PER TURN, not per thread — a thread-lifetime counter silently switches a guard
# off forever once spent, the bug documented at length in
# blank_recovery._MAX_EMPTY_RECOVERIES.
_MAX_RECONCILE_NUDGES = 1

# Never jump back on an already-huge history: at that size an unreconciled plan is
# accepted as-is rather than risking more churn. Mirrors blank_recovery's ceiling.
_MAX_AI_MESSAGES = 60

# How many unfinished todos to name in the nudge before summarising the rest.
# Enough to be actionable, short enough not to crowd the model's context.
_MAX_LISTED = 8


# ─────────────────────────────────────────────────────────────────────────────
# State access helpers (dict-or-attribute tolerant, fail-safe — a schema change
# must never turn a read into a hard failure inside a hook).
# ─────────────────────────────────────────────────────────────────────────────
def _messages(state: Any) -> list:
    """Pull the message list out of agent state (dict or attribute form)."""
    if isinstance(state, dict):
        return state.get("messages") or []
    return getattr(state, "messages", None) or []


def _state_value(source: Any, key: str) -> Any:
    """Read a field from state, tolerant of dict or attribute form."""
    state = source.state if hasattr(source, "state") else source
    if isinstance(state, dict):
        return state.get(key)
    return getattr(state, key, None) if state is not None else None


def _state_int(source: Any, key: str) -> int:
    """Read an int-valued private counter (fail-safe 0)."""
    try:
        return int(_state_value(source, key) or 0)
    except (TypeError, ValueError):
        return 0


def _message_text(message: Any) -> str:
    """Flatten message content (str or list-of-blocks) to plain text."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _real_user_turn_key(messages: list) -> str:
    """Stable identifier for the CURRENT user turn.

    Every guardrail nudge in this codebase is persisted as
    ``HumanMessage(name=<source>)``, so a ``HumanMessage`` with NO ``name`` is the
    only marker of a genuine new user turn. Same contract as
    ``blank_recovery._real_user_turn_key`` and
    ``tool_call_repair._real_user_turn_key`` — kept local rather than imported so
    the guards cannot break each other.
    """
    n = 0
    last_id = ""
    for msg in messages:
        if isinstance(msg, HumanMessage) and not getattr(msg, "name", None):
            n += 1
            last_id = str(getattr(msg, "id", "") or "")
    return f"{n}:{last_id}"


def _turn_start_index(messages: list) -> int:
    """Index of the message that opened the current user turn (0 if none).

    A ``HumanMessage`` with no ``name`` is the turn marker; everything at or after
    the LAST one belongs to the live turn.
    """
    start = 0
    for index, msg in enumerate(messages):
        if isinstance(msg, HumanMessage) and not getattr(msg, "name", None):
            start = index
    return start


# ─────────────────────────────────────────────────────────────────────────────
# Todo inspection (pure functions — unit-testable offline, see
# tmp/test_todo_reconcile.py)
# ─────────────────────────────────────────────────────────────────────────────
def _todo_fields(item: Any) -> tuple[str, str]:
    """Return ``(content, status)`` for one todo, tolerant of dict or object form.

    ``TodoListMiddleware`` stores plain dicts (a ``TypedDict`` at runtime), but a
    future model_dump or pydantic wrapper would hand back objects; both read the
    same here so a shape change degrades to "no unfinished todos" instead of
    raising inside a hook.
    """
    if isinstance(item, dict):
        content, status = item.get("content"), item.get("status")
    else:
        content, status = getattr(item, "content", None), getattr(item, "status", None)
    return (
        str(content) if isinstance(content, str) else "",
        str(status).strip().lower() if isinstance(status, str) else "",
    )


def _unfinished(todos: Any) -> list[tuple[str, str]]:
    """The ``(content, status)`` pairs that are not yet ``completed``.

    An entry with an unrecognised or missing status counts as unfinished: the
    honest reading of "I cannot tell whether this is done" is "not done".
    """
    if not isinstance(todos, list):
        return []
    out: list[tuple[str, str]] = []
    for item in todos:
        content, status = _todo_fields(item)
        if status != _DONE:
            out.append((content, status or "unknown"))
    return out


def _wrote_todos_this_turn(messages: list) -> bool:
    """True if the model called ``write_todos`` during the CURRENT user turn.

    This is what scopes the guard to a plan the model itself just made, so a stale
    list from an earlier turn is never nudged about.
    """
    for msg in messages[_turn_start_index(messages) :]:
        if not isinstance(msg, AIMessage):
            continue
        for call in getattr(msg, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name == _WRITE_TODOS:
                return True
    return False


def _is_reconcilable_answer(message: Any) -> bool:
    """True when `message` is a genuine prose final answer this guard may act on.

    Excludes, in order, every tail another guard owns:

    * not an ``AIMessage``, or it still has ``tool_calls`` — the run is not ending.
    * empty text — that is an empty completion, ``blank_recovery.py`` Hook B's job.
    * ``blank_recovery``'s exhausted-budget answer (stamped in
      ``response_metadata``) — that guard has DELIBERATELY decided to stop the
      turn, and re-opening it here would undo the decision and could ping-pong.
    * a tail that is really an unparsed tool-call blob — ``tool_call_repair.py``
      re-issues it as a real call, which is strictly better than nudging about
      todos. Detected with that module's own public helper rather than a local
      copy of the patterns, so the two cannot drift apart.
    """
    if not isinstance(message, AIMessage) or getattr(message, "tool_calls", None):
        return False
    text = _message_text(message).strip()
    if not text:
        return False
    metadata = getattr(message, "response_metadata", None) or {}
    if isinstance(metadata, dict) and metadata.get("iris_blank_recovery_exhausted"):
        return False
    return find_tool_call_blob(text) is None


def _nudge_text(unfinished: list[tuple[str, str]], total: int) -> str:
    """The reconciliation nudge, naming the actual unfinished steps."""
    shown = unfinished[:_MAX_LISTED]
    listing = "\n".join(f"  - [{status}] {content}" for content, status in shown)
    if len(unfinished) > len(shown):
        listing += f"\n  - …and {len(unfinished) - len(shown)} more"
    return (
        f"You are ending this turn with {len(unfinished)} of {total} planned todo(s) "
        f"still unfinished:\n{listing}\n\n"
        "Do NOT stop with the plan half-open. Take ONE of these actions now:\n"
        "1) If a listed step is genuinely still outstanding and you can do it, do it "
        "now — dispatch it with task(...) or call the tool it needs.\n"
        f'2) If a step is actually finished, blocked, or no longer needed, call '
        f'{_WRITE_TODOS} and set its status to "{_DONE}", editing that todo\'s '
        "`content` to say what really happened — e.g. \"Post the summary to #ops — "
        "BLOCKED: no channel access\". The status field accepts ONLY \"pending\", "
        '"in_progress" or "completed", so record a blocked or dropped outcome in the '
        "content text, never as a status value.\n"
        f'3) Once every todo reads "{_DONE}", give your final answer using the Final '
        "Response Contract (STATUS / SUMMARY / ARTIFACTS / BLOCKERS / LEARNING) — and "
        "state any blocked step in BLOCKERS.\n\n"
        "This list is the record the user sees. Left stale it makes finished work look "
        "abandoned and abandoned work look like it is still coming."
    )


class TodoReconcileState(AgentState):
    """Private state for the per-turn reconcile budget.

    ``todos`` itself is NOT redeclared here — ``TodoListMiddleware`` owns that
    field, and LangChain merges every middleware's ``state_schema`` into one graph
    state (factory.py:1176), so it is readable from this hook regardless of
    registration order.
    """

    # How many reconcile nudges have been issued during the CURRENT user turn.
    iris_todo_reconciles: NotRequired[Annotated[int, PrivateStateAttr]]
    # Which turn that counter belongs to (see _real_user_turn_key). A different
    # live key means the counter is stale and the budget resets — without this the
    # counter becomes a thread-lifetime total and the guard dies after one use.
    iris_todo_reconcile_turn: NotRequired[Annotated[str, PrivateStateAttr]]


class TodoReconcileMiddleware(AgentMiddleware):
    """Require a run to close out its own plan before it ends.

    See the module docstring. One ``after_agent`` hook, bounded to a single
    jump-back per user turn, that fires only when the model wrote a plan this turn
    and is now ending on a real prose answer with entries still open.

    Both sync and async variants are implemented so the guard holds on ``.invoke``
    and ``.ainvoke`` (the Slack webhook uses the async path). Declares private
    state, so build a FRESH instance per agent — never share.
    """

    name = "TodoReconcileMiddleware"
    state_schema = TodoReconcileState

    def _reconcile(self, state: AgentState[Any]) -> dict[str, Any] | None:
        """Append the reconcile nudge and jump back to the model, or no-op."""
        messages = _messages(state)
        if not messages or not _is_reconcilable_answer(messages[-1]):
            return None  # common case — another guard's tail, or not ending

        todos = _state_value(state, "todos")
        unfinished = _unfinished(todos)
        if not unfinished:
            return None  # common case — no plan, or the plan is closed out
        if not _wrote_todos_this_turn(messages):
            # A plan from an earlier turn. Not this turn's business.
            return None

        # ── Per-TURN budget fence ────────────────────────────────────────────
        # A stored key from an earlier turn (or none at all, which is every thread
        # written before this guard existed) means this turn has spent nothing, so
        # existing threads need no checkpoint migration.
        turn_key = _real_user_turn_key(messages)
        used = _state_int(state, "iris_todo_reconciles")
        if _state_value(state, "iris_todo_reconcile_turn") != turn_key:
            used = 0

        if used >= _MAX_RECONCILE_NUDGES:
            logger.warning(
                "todo_reconcile: turn %s ending with %d unfinished todo(s) after %d "
                "nudge(s) — accepting the answer rather than looping",
                turn_key,
                len(unfinished),
                used,
            )
            return None
        ai_count = sum(1 for m in messages if isinstance(m, AIMessage))
        if ai_count >= _MAX_AI_MESSAGES:
            logger.warning(
                "todo_reconcile: %d unfinished todo(s) with oversized history (ai=%d) "
                "— accepting the answer as final",
                len(unfinished),
                ai_count,
            )
            return None

        total = len(todos) if isinstance(todos, list) else len(unfinished)
        logger.warning(
            "todo_reconcile: run ending with %d/%d todo(s) unfinished (nudge %d/%d "
            "this turn) — jumping back to the model",
            len(unfinished),
            total,
            used + 1,
            _MAX_RECONCILE_NUDGES,
        )

        # The premature answer is deliberately LEFT IN PLACE, unlike
        # blank_recovery's Hook B which removes the empty turn it recovers from.
        # That message has real text, which means its tokens were already streamed
        # to the UI; deleting it from state would make what the user watched
        # disappear on the next page load, and would throw away a summary the model
        # would then have to regenerate. The nudge simply follows it.
        return {
            "messages": [HumanMessage(content=_nudge_text(unfinished, total), name=RECONCILE_SOURCE)],
            "iris_todo_reconciles": used + 1,
            "iris_todo_reconcile_turn": turn_key,
            "jump_to": "model",
        }

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        """Reconcile once (bounded) when a turn would end on an open plan."""
        return self._reconcile(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        """Async variant of `after_agent`."""
        return self._reconcile(state)
