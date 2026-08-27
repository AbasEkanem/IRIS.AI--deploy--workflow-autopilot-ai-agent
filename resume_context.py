"""resume_context.py — Tell a resumed run that it resumed (Part 4).

Problem this solves
-------------------
When recovery.py (Part 2) resumes a crashed thread, LangGraph replays it from the
last durable checkpoint: all completed work — including external side effects that
already succeeded (emails sent, Slack posts, calendar events, CRM writes) — is
restored into the message history and the pending step continues. But the *model*
has no idea it was interrupted. Seeing a long history that trails off mid-plan, a
model will often re-narrate what it already did or re-issue a completed action,
which at best wastes a turn and at worst duplicates a side effect.

Fix
---
A tiny orchestrator+subagent middleware. On the FIRST model call of a run whose
config carries ``resumed=True`` (set by recovery.py's resume invoke), it appends a
one-time directive to the conversation: *you resumed; completed work is already in
history; do not repeat completed external actions; continue from the next
incomplete step.* The directive is persisted into graph state (like the
blank_recovery nudges) so it keeps steering the run and is visible in the
transcript, and it is injected exactly once per resume — guarded by a private-state
field holding the resume's own token — so it never piles up across the resumed
invoke's many internal model calls, and a SECOND crash on the same thread still
gets its own directive rather than resuming silently.

This is the *context* half of the resume story; idempotency.py (Part 3) is the
*enforcement* half. Even if the model ignores the directive and retries a
completed side effect, the idempotency key absorbs it. Belt-and-braces by design.

Scope
-----
Added to BOTH the orchestrator middleware list (IRIS.py) and the subagent list
(subagent_config.py) — orchestrator middleware is not propagated to declarative
subagents, and a subagent can be the thing that resumes mid-delegation, so it
needs the same nudge. Reads the flag via ``langgraph.config.get_config()``
(``Runtime`` exposes no ``config``); a config-read failure fails safe to "not
resumed" (inject nothing). Correct on ``.invoke`` and ``.ainvoke`` and idempotent
across the run.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    PrivateStateAttr,
)
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

# Distinct message name so the directive is easy to spot in a transcript and the
# UI can render it as a system-correction note rather than a user bubble (matches
# the blank_recovery convention).
_RESUME_SOURCE = "iris_resume_context"

_RESUME_DIRECTIVE = (
    "⟳ You RESUMED after an interruption — a crash or network failure restarted this "
    "run, and LangGraph replayed it from the last saved checkpoint. Your COMPLETED "
    "work is already in the message history above, INCLUDING any external actions that "
    "already succeeded (emails sent, Slack messages posted, calendar events created, "
    "documents/CRM records written). Re-issuing any of those would DUPLICATE the side "
    "effect. So:\n"
    "1) Read your write_todos plan and the history to see exactly what already finished.\n"
    "2) Continue from the FIRST incomplete step — do NOT re-narrate or re-run completed steps.\n"
    "3) If a step's outcome is unclear from the history, VERIFY it (a read) before ever "
    "redoing it (a write).\n"
    "Then proceed normally to completion."
)


def _resume_token() -> str | None:
    """Token identifying the running invoke's resume, or None if not a resume.

    ``configurable.resumed`` and ``configurable.resume_id`` are both set by
    recovery.py's resume call. Read via ``get_config()`` — the config contextvar
    populated for the duration of graph execution (verified: a custom
    ``configurable`` key IS visible here). Any failure → None (inject nothing):
    a missed notice is harmless, a spurious one only on a genuinely-resumed run.

    The token is what makes the notice once-per-RESUME rather than once-per-thread.
    A resume with no ``resume_id`` (an older caller, or anything else that sets only
    ``resumed``) falls back to the constant ``"1"``, which reproduces the old
    once-per-thread behaviour for that caller rather than injecting on every model
    call — the failure mode worth avoiding of the two.
    """
    try:
        from langgraph.config import get_config

        configurable = (get_config() or {}).get("configurable") or {}
        if not configurable.get("resumed"):
            return None
        token = configurable.get("resume_id")
        return str(token) if token else "1"
    except Exception:
        return None


def _state_str(state: Any, key: str) -> str:
    """Read a str-valued private flag from state (dict or attribute form)."""
    value = state.get(key) if isinstance(state, dict) else getattr(state, key, None)
    return str(value) if isinstance(value, str) else ""


class ResumeContextState(AgentState):
    """State schema carrying the resume token the notice was last injected for.

    A ``str``, not the ``bool`` this used to be. ``PrivateStateAttr`` only omits the
    field from the input/output *schemas* — the value is still persisted in the
    checkpoint, so a bare "already injected" bool survived the run that set it and
    silenced the directive on every LATER resume of the same thread. Storing which
    resume it was injected for makes the guard per-resume. Old checkpoints simply
    have no value here, so the first resume after this change injects normally.
    """

    iris_resume_notice_for: NotRequired[Annotated[str, PrivateStateAttr]]


class ResumeContextMiddleware(AgentMiddleware):
    """Inject a one-time 'you resumed' directive on a crash-resumed run.

    Fires only when the invoke config carries ``resumed=True`` (recovery.py) and
    only on the first model call of THAT resume (guarded by the persisted resume
    token), so the directive steers the run without repeating — and a second crash
    on the same thread gets its own directive. No-op on normal dispatch and on
    human-driven HITL resumes (those configs carry no ``resumed`` flag and the
    model already has full context from the interrupt). Both sync and async hooks
    are implemented so the guard holds on ``.invoke`` and ``.ainvoke``.
    """

    name = "ResumeContextMiddleware"
    state_schema = ResumeContextState

    def _plan(self, state: Any) -> dict[str, Any] | None:
        token = _resume_token()
        if token is None:
            return None
        if _state_str(state, "iris_resume_notice_for") == token:
            return None  # already injected once for this resume
        logger.info(
            "resume_context: resumed run detected — injecting one-time resume directive "
            "(resume_id=%s)",
            token,
        )
        return {
            "messages": [HumanMessage(content=_RESUME_DIRECTIVE, name=_RESUME_SOURCE)],
            "iris_resume_notice_for": token,
        }

    def before_model(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        return self._plan(state)

    async def abefore_model(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        return self._plan(state)
