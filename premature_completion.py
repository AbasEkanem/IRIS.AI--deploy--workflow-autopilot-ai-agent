"""premature_completion.py — Stop IRIS ending a work turn without finishing it.

Problem this solves
-------------------
A turn that does real domain work can still end prematurely on a NON-empty
message. The model dispatches a ``task()``, reads the result, and then answers
with a short prose acknowledgement — no further dispatch, no ``write_todos``
plan, and no Final Response Contract — while the user's objective is only
partly done. To the user the loop "completed" early.

Why the existing guards miss it (verified, not assumed):

* ``blank_recovery.py`` (``_is_empty_completion``) requires an AIMessage with no
  text AND no tool calls. A premature stop HAS text, so it is invisible there.
* ``todo_reconcile.py`` only fires when the model called ``write_todos`` THIS
  turn (``_wrote_todos_this_turn``) and left entries open. A work turn that
  never planned — exactly the skipped-plan case FC-2 forbids — has no todos in
  state, so that guard stands down.

So a work turn that skipped planning and stopped early is structurally
unguarded. This module closes that gap, and ONLY that gap: it defers to
``todo_reconcile`` whenever a plan exists (they are mutually exclusive by an
explicit check, not by list position).

Behaviour
---------
One bounded ``after_agent`` gate. It fires when a turn that dispatched at least
``_MIN_TASK_DISPATCHES`` ``task()`` call(s) is about to end on a genuine prose
answer that is neither a plan (no ``write_todos`` this turn) nor a Final Response
Contract (no ``STATUS``/``SUMMARY``). It appends a persisted nudge — continue the
outstanding work, or close with the contract — and jumps back to the model once.

Scope decisions, each deliberately narrow so this can never nag:

* **Only a turn that actually delegated counts.** IRIS owns zero domain tools,
  so a turn with no ``task()`` did no work — there is nothing to finish. The
  ``_MIN_TASK_DISPATCHES`` floor (env ``IRIS_MIN_TASK_DISPATCHES_FOR_FINALIZE``,
  default 1) also lets an operator require *multiple* dispatches before this
  guard is willing to act, if a single-step lookup answered in plain prose
  should be allowed to stand.
* **Only when NO plan was written this turn.** If the model planned, this is
  ``todo_reconcile``'s turn to reconcile the list; acting here too would
  double-nudge. The check is explicit (``_wrote_todos_this_turn``), so the two
  guards never both fire regardless of registration order.
* **Only a real prose answer counts.** An empty completion is
  ``blank_recovery`` Hook B's; an unparsed tool-call blob is
  ``tool_call_repair``'s; ``blank_recovery``'s exhausted-budget give-up answer
  is a deliberate stop. All three are excluded by ``_is_finalizable_answer``,
  so this guard stands down for them rather than racing them.
* **Only when the answer is not already a Final Response Contract.** A turn that
  closed with STATUS/SUMMARY finalized correctly (§7) and is left alone.
* **Once per user turn, hard.** A model that will not finalize after one nudge
  would otherwise be nudged forever — the same per-turn budget fence
  ``blank_recovery`` and ``todo_reconcile`` use. Past the budget the prose
  answer is accepted as-is (it has real text, so there is no blank-bubble
  problem — unlike ``blank_recovery``, this guard never needs a give-up answer).

Ordering note (mirrors ``todo_reconcile``): ``after_agent`` hooks run in REVERSE
registration order. Deference to the sibling guards is implemented as explicit
checks, never left to list position, so re-ordering the middleware list cannot
silently change behaviour.

Orchestrator-only by design — subagents do not plan or finalize, and middleware
lists are not propagated into them (see subagent_config.py). Declares private
state, so build a FRESH instance per agent — never share.
"""

from __future__ import annotations

import logging
import os
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
# correction card instead of something the user appears to have typed. MUST stay
# in sync with guardrail_taxonomy.PREMATURE_COMPLETION_SOURCE and its UI mirror
# ui/src/lib/corrections.ts.
PREMATURE_SOURCE = "iris_premature_completion"

# The delegation tool added by deepagents' SubAgentMiddleware. A turn that never
# called it did no domain work, so there is nothing to finish.
_TASK = "task"

# The planning tool. If it was called this turn, todo_reconcile owns the
# reconciliation — this guard defers.
_WRITE_TODOS = "write_todos"

# Minimum task() dispatches this turn before the guard is willing to act.
# Default 1 (catch every premature stop); raise to 2+ to ignore single-step
# work turns that answered in plain prose. Env-tunable so it can be adjusted
# from the Railway dashboard without a redeploy.
try:
    _MIN_TASK_DISPATCHES = max(1, int(os.getenv("IRIS_MIN_TASK_DISPATCHES_FOR_FINALIZE", "1")))
except ValueError:
    _MIN_TASK_DISPATCHES = 1

# Hard cap per USER TURN. One is enough — the nudge is explicit, and a model that
# ignores it once will ignore it twice while spending the run's remaining
# super-steps. PER TURN, not per thread (a thread-lifetime counter silently
# switches a guard off forever once spent — see blank_recovery._MAX_EMPTY_RECOVERIES).
_MAX_PREMATURE_NUDGES = 1

# Never jump back on an already-huge history: at that size an early stop is
# accepted rather than risking more churn. Mirrors the sibling guards' ceiling.
_MAX_AI_MESSAGES = 60


# ─────────────────────────────────────────────────────────────────────────────
# State access helpers (dict-or-attribute tolerant, fail-safe — a schema change
# must never turn a read into a hard failure inside a hook). Kept LOCAL rather
# than imported from the sibling guards on purpose: the guards in this repo are
# deliberately independent, so a change to one cannot silently alter another.
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
    ``blank_recovery._real_user_turn_key`` and ``todo_reconcile._real_user_turn_key``.
    Constant for the whole turn — including across a HITL approval and a crash
    resume, neither of which may hand back a fresh budget (the same premature stop
    would then be nudged indefinitely).
    """
    n = 0
    last_id = ""
    for msg in messages:
        if isinstance(msg, HumanMessage) and not getattr(msg, "name", None):
            n += 1
            last_id = str(getattr(msg, "id", "") or "")
    return f"{n}:{last_id}"


def _turn_start_index(messages: list) -> int:
    """Index of the message that opened the current user turn (0 if none)."""
    start = 0
    for index, msg in enumerate(messages):
        if isinstance(msg, HumanMessage) and not getattr(msg, "name", None):
            start = index
    return start


# ─────────────────────────────────────────────────────────────────────────────
# Turn inspection (pure functions — unit-testable offline).
# ─────────────────────────────────────────────────────────────────────────────
def _count_calls_this_turn(messages: list, tool_name: str) -> int:
    """How many times the model called ``tool_name`` during the current turn."""
    count = 0
    for msg in messages[_turn_start_index(messages):]:
        if not isinstance(msg, AIMessage):
            continue
        for call in getattr(msg, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name == tool_name:
                count += 1
    return count


def _wrote_todos_this_turn(messages: list) -> bool:
    """True if the model called ``write_todos`` during the current turn.

    When true this guard defers entirely to ``todo_reconcile`` — the two are
    mutually exclusive, so a planned turn is never nudged from here.
    """
    return _count_calls_this_turn(messages, _WRITE_TODOS) > 0


def _has_final_contract(text: str) -> bool:
    """True when the answer reads like a Final Response Contract (§7).

    Requires the two mandatory contract lines, ``STATUS`` and ``SUMMARY``. Matched
    case-insensitively: a model that finalized with slightly different casing
    should be left alone, and a false *negative* here just makes the guard stand
    down — the safe direction (no nag).
    """
    low = (text or "").lower()
    return "status" in low and "summary" in low


def _is_finalizable_answer(message: Any) -> bool:
    """True when ``message`` is a genuine prose final answer this guard may act on.

    Excludes every tail another guard owns:

    * not an ``AIMessage``, or it still has ``tool_calls`` — the run is not ending.
    * empty text — ``blank_recovery`` Hook B's job.
    * ``blank_recovery``'s exhausted-budget answer (stamped in ``response_metadata``)
      — a deliberate stop that must not be re-opened.
    * a tail that is really an unparsed tool-call blob — ``tool_call_repair``
      re-issues it as a real call. Detected with that module's own public helper
      so the two cannot drift apart.
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


def _nudge_text(task_count: int) -> str:
    """The finalize-or-continue nudge."""
    return (
        f"You dispatched {task_count} subtask(s) this turn, then ended on a plain "
        "prose message with no plan (write_todos) behind it and no Final Response "
        "Contract. On a turn that did domain work that is a premature stop "
        "(FC-2 / FC-5): work may still be outstanding, and even if it is complete "
        "the run was never finalized. Do NOT stop here. Take ONE of these actions now:\n"
        "1) If any part of the user's objective is still outstanding, continue it — "
        "dispatch the next step with task(...), or call the tool it needs.\n"
        "2) If the work is genuinely and fully complete, close with the Final "
        "Response Contract (STATUS / SUMMARY / ARTIFACTS / BLOCKERS / LEARNING) — "
        "state any blocked step in BLOCKERS. Never end a work turn on a bare "
        "acknowledgement.\n"
        "Continue until the objective is finished AND finalized."
    )


class PrematureCompletionState(AgentState):
    """Private state for the per-turn premature-completion budget.

    ``todos`` is owned by ``TodoListMiddleware`` and merged into graph state
    across all middleware (factory.py), so it is readable here without being
    redeclared — though this guard only reads whether ``write_todos`` was CALLED,
    not the list contents.
    """

    # How many finalize nudges have been issued during the CURRENT user turn.
    iris_premature_completions: NotRequired[Annotated[int, PrivateStateAttr]]
    # Which turn that counter belongs to (see _real_user_turn_key). A different
    # live key means the counter is stale and the budget resets — without this the
    # counter becomes a thread-lifetime total and the guard dies after one use.
    iris_premature_completion_turn: NotRequired[Annotated[str, PrivateStateAttr]]


class PrematureCompletionGuardMiddleware(AgentMiddleware):
    """Require a work turn to finish (or finalize) before it ends.

    See the module docstring. One ``after_agent`` hook, bounded to a single
    jump-back per user turn, that fires only when the turn dispatched real work,
    wrote no plan (so ``todo_reconcile`` is not the owner), and is now ending on a
    prose answer that is not a Final Response Contract.

    Both sync and async variants are implemented so the guard holds on ``.invoke``
    and ``.ainvoke`` (the Slack webhook uses the async path). Declares private
    state, so build a FRESH instance per agent — never share.
    """

    name = "PrematureCompletionGuardMiddleware"
    state_schema = PrematureCompletionState

    def _guard(self, state: AgentState[Any]) -> dict[str, Any] | None:
        """Append the finalize nudge and jump back to the model, or no-op."""
        messages = _messages(state)
        if not messages or not _is_finalizable_answer(messages[-1]):
            return None  # common case — another guard's tail, or not ending

        task_count = _count_calls_this_turn(messages, _TASK)
        if task_count < _MIN_TASK_DISPATCHES:
            return None  # not a work turn (or below the operator's floor)
        if _wrote_todos_this_turn(messages):
            # A plan exists — todo_reconcile owns reconciliation. Defer.
            return None
        if _has_final_contract(_message_text(messages[-1])):
            return None  # already finalized per §7

        # ── Per-TURN budget fence ────────────────────────────────────────────
        # A stored key from an earlier turn (or none at all, which is every thread
        # written before this guard existed) means this turn has spent nothing, so
        # existing threads need no checkpoint migration.
        turn_key = _real_user_turn_key(messages)
        used = _state_int(state, "iris_premature_completions")
        if _state_value(state, "iris_premature_completion_turn") != turn_key:
            used = 0

        if used >= _MAX_PREMATURE_NUDGES:
            logger.warning(
                "premature_completion: turn %s ending after %d task(s) with no plan "
                "and no contract, but budget spent (%d nudge(s)) — accepting the answer",
                turn_key, task_count, used,
            )
            return None
        ai_count = sum(1 for m in messages if isinstance(m, AIMessage))
        if ai_count >= _MAX_AI_MESSAGES:
            logger.warning(
                "premature_completion: %d task(s), no plan/contract, oversized history "
                "(ai=%d) — accepting the answer as final",
                task_count, ai_count,
            )
            return None

        logger.warning(
            "premature_completion: work turn ending on unfinalized prose after %d "
            "task(s) with no plan (nudge %d/%d this turn) — jumping back to the model",
            task_count, used + 1, _MAX_PREMATURE_NUDGES,
        )

        # The premature answer is LEFT IN PLACE (mirrors todo_reconcile): it has
        # real text whose tokens were already streamed to the UI, so removing it
        # would make what the user watched disappear on reload. The nudge follows it.
        return {
            "messages": [HumanMessage(content=_nudge_text(task_count), name=PREMATURE_SOURCE)],
            "iris_premature_completions": used + 1,
            "iris_premature_completion_turn": turn_key,
            "jump_to": "model",
        }

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        """Guard once (bounded) when a work turn would end unfinalized."""
        return self._guard(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        """Async variant of `after_agent`."""
        return self._guard(state)
