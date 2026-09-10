"""revived_guards.py — Give the shipped ultra-profile guards eyes for empty tails.

Problem this solves
-------------------
deepagents' nemotron-3-ultra profile ships two answer-quality guards IRIS adopts
(FinalAnswerGuard, FollowupDiscipline). Both are gated behind `_is_final_answer`,
which requires NON-EMPTY text — so the one completion that fails their quality
bar hardest (an EMPTY one: the run ends with zero of the job done) is invisible
to both. The gate is also load-bearing for their detection logic, which parses
the answer's TEXT; opening the gate alone would hand them an empty string to
parse. Revival therefore cannot be a gate fix — each guard needs a mission-
specific behaviour for the empty class, alongside its unchanged text logic.

Design — intercept-only-on-deviation
------------------------------------
Each subclass overrides `after_agent`/`aafter_agent` at the HOOK boundary (the
public, stable surface — never the private `_nudge`, which a pip install would
wipe) and inspects the tail via completion_tail.classify_tail:

  * EMPTY        → the revival: a persisted, mission-specific nudge demanding a
                   concrete answer, with `jump_to="model"`.
  * HARNESS_REPORT (blank_recovery's give-up text) → stand down WITHOUT calling
                   super and WITHOUT consuming the one-shot flag. Without this,
                   the give-up answer (non-empty, unnamed) would pass
                   `_is_final_answer` and the guard would police the harness's
                   own honest report — one extra bounded jump per exhaustion.
  * everything else → `super()`'s hook, byte-identical shipped behaviour.

Bounds (why this cannot loop)
-----------------------------
The revival reuses each guard's OWN shipped one-shot flag
(`nemotron_final_guard_fired` / `nemotron_followup_guard_fired`), so combined
shipped+revival is at most ONE correction per thread — the shipped design's
known limitation, not a new one. Downstream, blank_recovery's strict in-call
retry (5) and its per-turn backstop (5) remain the primaries; this guard is the
last-chance backstop that fires only when an empty tail still reaches END.

Ordering
--------
IRIS.py registers these AFTER TodoReconcileMiddleware, so — after_agent hooks
running in REVERSE registration order — they fire FIRST on any tail. On an
empty tail this is the desired first responder; if the model blanks again, the
flag stands this guard down and BlankResultRecovery's Hook B takes over. Both
directions are bounded and neither guard's removal semantics conflict (the
revival leaves the empty turn in place — Hook B removes it if it ever fires).

Sync + async twins of every override, per the framework rule (hook pairs must
be implemented on both paths or ainvoke fails loud).
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware.types import AgentState, hook_config
from langchain_core.messages import AIMessage, HumanMessage

from completion_tail import EMPTY, HARNESS_REPORT, classify_tail

logger = logging.getLogger(__name__)

# History ceiling, mirroring blank_recovery's / tool_call_repair's local copies
# (kept local per the sibling-guard convention). Past it, a revival jump risks
# more churn than an honest stop is worth.
_MAX_AI_MESSAGES = 60

# The one-shot flag each parent guard sets when it fires — reused verbatim so
# shipped and revival share one budget (at most one correction of each kind per
# thread, the shipped design).
_FINAL_FLAG = "nemotron_final_guard_fired"
_FOLLOWUP_FLAG = "nemotron_followup_guard_fired"

#: Message-name tags for the revival nudges. Named (not anonymous) so
#: guardrail_taxonomy.py and ui/src/lib/corrections.ts classify them as
#: correction cards; keep both mirrors in sync when changing these.
FINAL_REVIVAL_SOURCE = "iris_final_answer_revival"
FOLLOWUP_REVIVAL_SOURCE = "iris_followup_revival"

# Mission-specific empty-tail texts. Concrete shape, not vague "continue" —
# these models handle a named output shape far better than an instruction to
# try harder (measured across plan_guard / blank_recovery nudge history).
_FINAL_REVIVAL_TEXT = (
    "You ended the run with an EMPTY response — no text and no tool call. That is "
    "not a valid completion of the user's request.\n"
    "Answer NOW, as plain prose, using the Final Response Contract:\n"
    "STATUS / SUMMARY / ARTIFACTS (with the exact IDs and literal values of what "
    "was created or sent) / BLOCKERS (anything still pending or failed, named as "
    "such).\n"
    "If a step failed or is awaiting approval, SAY so in BLOCKERS — never end "
    "silent."
)

_FOLLOWUP_REVIVAL_TEXT = (
    "You ended the run with an EMPTY response. Do not end silent.\n"
    "Answer NOW in one or two plain sentences: what you completed, and what "
    "remains (or ask the ONE smallest question you need answered to proceed). "
    "An empty message is never an answer."
)


def _state_get(state: Any, key: str) -> Any:
    """Read a state key, tolerant of dict or attribute form (fail-safe None)."""
    source = getattr(state, "state", None) or state
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _count_ai(messages: list) -> int:
    return sum(1 for m in messages if isinstance(m, AIMessage))


class _RevivalMixin:
    """Shared revival machinery; concrete guards supply the per-guard pieces."""

    _flag: str = ""
    _source: str = ""
    _text: str = ""
    _parent: type = object  # the shipped middleware this class revives

    def _revive_or_delegate(self, state: Any) -> dict[str, Any] | None:
        messages = list(_state_get(state, "messages") or [])
        if not messages:
            return None
        last = messages[-1]
        tail = classify_tail(messages)

        if tail == HARNESS_REPORT:
            # The harness already answered honestly. Stand down and leave the
            # one-shot flag UNCONSUMED — a later real tail may still need it.
            return None

        if tail != EMPTY:
            # Every other class: shipped behaviour, unchanged.
            return self._parent._nudge(state)  # type: ignore[attr-defined]

        # ── EMPTY: the class the shipped gate could not see ──────────────────
        if _state_get(state, self._flag):
            return None  # one-shot budget spent — shipped design
        if _count_ai(messages) >= _MAX_AI_MESSAGES:
            logger.warning(
                "revived_guards: empty completion with oversized history (ai=%d) — "
                "leaving it to blank_recovery",
                _count_ai(messages),
            )
            return None

        logger.warning(
            "revived_guards: %s fired on an EMPTY completion — persisting the "
            "revival nudge and jumping back to the model",
            type(self).__name__,
        )
        return {
            "messages": [HumanMessage(content=self._text, name=self._source)],
            self._flag: True,
            "jump_to": "model",
        }


try:
    # The vendored private module — import-guarded so a rename/deprecation can
    # never kill boot (same contract as IRIS.py's own import of these classes).
    from deepagents.profiles.harness._nvidia_nemotron_3_ultra import (
        FinalAnswerGuardMiddleware as _ShippedFinalAnswerGuard,
        FollowupDisciplineMiddleware as _ShippedFollowupDiscipline,
    )
except Exception:  # pragma: no cover — fail-soft, guards simply stay unrevived
    _ShippedFinalAnswerGuard = None  # type: ignore[assignment,misc]
    _ShippedFollowupDiscipline = None  # type: ignore[assignment,misc]
    logging.getLogger(__name__).warning(
        "revived_guards: shipped ultra-profile guards not importable — "
        "revivals are DISABLED this boot."
    )


if _ShippedFinalAnswerGuard is not None and _ShippedFollowupDiscipline is not None:

    class RevivedFinalAnswerGuardMiddleware(_RevivalMixin, _ShippedFinalAnswerGuard):  # type: ignore[valid-type,misc]
        """FinalAnswerGuard + an empty-completion revival path.

        Text tails behave exactly as shipped (delegate to the parent's `_nudge`).
        An EMPTY tail — invisible to the shipped gate — now fires the guard's own
        mission: the Final Response Contract is violated maximally, so demand it.
        """

        _flag = _FINAL_FLAG
        _source = FINAL_REVIVAL_SOURCE
        _text = _FINAL_REVIVAL_TEXT
        _parent = _ShippedFinalAnswerGuard

        @hook_config(can_jump_to=["model"])
        def after_agent(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
            return self._revive_or_delegate(state)

        @hook_config(can_jump_to=["model"])
        async def aafter_agent(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
            return self._revive_or_delegate(state)

    class RevivedFollowupDisciplineMiddleware(_RevivalMixin, _ShippedFollowupDiscipline):  # type: ignore[valid-type,misc]
        """FollowupDiscipline + an empty-completion revival path.

        The shipped guard polices lazy ANSWERS (redundant clarifying questions);
        its empty-class mission is the same bar at the extreme: the run must not
        end silent.
        """

        _flag = _FOLLOWUP_FLAG
        _source = FOLLOWUP_REVIVAL_SOURCE
        _text = _FOLLOWUP_REVIVAL_TEXT
        _parent = _ShippedFollowupDiscipline

        @hook_config(can_jump_to=["model"])
        def after_agent(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
            return self._revive_or_delegate(state)

        @hook_config(can_jump_to=["model"])
        async def aafter_agent(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
            return self._revive_or_delegate(state)

else:  # pragma: no cover — vendored module unavailable; boot must not break
    RevivedFinalAnswerGuardMiddleware = None  # type: ignore[assignment,misc]
    RevivedFollowupDisciplineMiddleware = None  # type: ignore[assignment,misc]


def revived_answer_quality_guards() -> tuple:
    """Fresh instances per build — both guards declare private state.

    Returns an EMPTY TUPLE when the vendored module is unavailable, so callers
    can unpack unconditionally (`*_revived_answer_quality_guards()`), matching
    the shipped guards' own fail-soft contract in IRIS.py.
    """
    classes = (RevivedFinalAnswerGuardMiddleware, RevivedFollowupDisciplineMiddleware)
    return tuple(cls() for cls in classes if cls is not None)

