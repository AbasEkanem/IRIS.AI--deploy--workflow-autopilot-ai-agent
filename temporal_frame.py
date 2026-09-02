"""temporal_frame.py — Give the model the date, instead of asking it to fetch one.

Why this exists
---------------
IRIS hallucinated the date in production. The obvious explanation — "nothing in
the harness computes a date, so the model has to remember to call
get_current_datetime()" — is only half true, and the missing half is the half
that decides the fix.

Measured (tmp/probe_loaded_orchestrator.py, 24 live calls, 2026-09-02): asked
"What is today's date?" with the real rule from prompts/iris/execution-protocol.md:31
present in EVERY cell, `nemotron-3.5-lightning-30b-a3b` called the tool on

    the rule alone, one user turn ................... 100%
    + the real 18,540-char ORCHESTRATOR_PROMPT ...... 100%
    + 20 messages of completed multi-step history ..... 0%   <- 4/4 WRONG dates

`nemotron-3-super-120b-a12b` degrades identically (75% -> 50% -> 25%). So the
instruction is not being diluted by the system prompt — it survives that intact.
It is displaced by CONVERSATION DEPTH, which is the state every real IRIS run is
in from step 2 onward. That is why instructing harder cannot fix this: the rule
was present, verbatim, in the cell that failed 4 times out of 4.

The hallucinated dates are the dangerous kind — near-miss and confident:

    lightning:  "Today's date is 2026-09-09 (Nigeria / UTC+1)."   (a week ahead)
    super:      "Today is 28 August 2026."                        (5 days behind)

Nothing downstream can tell those from the truth, so they land in real Jira due
dates, real calendar invites and real emails.

The fix
-------
Stop asking. Compute the frame in Python and put it in the request at the
position the measurement says is load-bearing: LAST, after the user turn and the
tool results, where nothing outranks it for recency. The model no longer has to
remember a rule — it cannot look at a request without seeing today's date.

Three properties this deliberately has:

* REQUEST-ONLY (``request.override``, never graph state). A timestamp written
  into history is a SECOND anchor that goes stale, and the probe above proves
  these models will confidently anchor on a stale number. Every model call gets
  a freshly computed frame instead.
* NEVER TOUCHES THE SYSTEM MESSAGE. Both prompt-cache breakpoints
  (prompt_caching.py) end inside it, so a minute-resolution timestamp there
  would invalidate the whole ~17k-token cached prefix on every single call — the
  exact bill 46adc45 removed. A trailing message leaves the prefix
  byte-identical.
* UNCONDITIONAL — no keyword gate on "today"/"deadline"/"tomorrow". The measured
  failure is the model not REALISING it needs a date, so any gate keyed on the
  model's own phrasing inherits the bug it is meant to fix ("when is the report
  due?" carries no keyword). The frame is ~90 tokens against a ~17k prefix.

Ground truth comes from ``get_current_datetime`` itself rather than a second
``datetime.now()``, so the injected frame and the tool can never disagree —
whatever timezone default the tool is later given.

Appended at the END rather than spliced in before the final turn: appending is
the only position that can never split an ``AIMessage(tool_calls=...)`` from its
``ToolMessage``, which inserting mid-history would do on every tool loop. The
frame closes by naming itself as context, because the cost of trailing-message
injection is a model that answers the injection instead of the user. Same
request-only pattern loop_breaker.py:473 already runs against these models.

Delivery
--------
NOT wired into ``IRIS.py`` or ``subagent_config.py``. It is registered as the
harness profile's ``extra_middleware`` (harness_profile.py), which reaches all
SEVEN agent stacks from one place: the orchestrator, the five declarative
subagents, and the auto-added ``general-purpose`` subagent — that last one being
unreachable from either middleware list (graph.py:765 is the only channel).
Verified by tmp/verify_harness_profile.py: frame x1 in every stack, zero
duplicates. Grace writes calendar events and Maya writes Jira due dates, so the
subagents need this anchor as much as the orchestrator does.

Position within a stack is irrelevant to this middleware, which is why it is safe
in the profile at all: profile middleware lands INNERMOST, and appending a
trailing message is order-independent. Being innermost is in fact mildly better —
nothing downstream can strip the frame before the request leaves.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import HumanMessage

from datetime_tools import get_current_datetime

logger = logging.getLogger(__name__)

#: Distinct message name so guardrail_taxonomy can classify the frame and the UI
#: never renders it as a user bubble. Request-only — REQUEST_ONLY_SOURCES.
TEMPORAL_FRAME_SOURCE = "iris_temporal_frame"

#: Nigeria / WAT — matches get_current_datetime's own default offset.
_DEFAULT_OFFSET_HOURS = 1.0

def _frame_text(offset_hours: float = _DEFAULT_OFFSET_HOURS) -> str | None:
    """The temporal frame as one short message, or None if the clock read fails.

    Routed through the tool so the frame is byte-identical to what the model
    would have received had it called ``get_current_datetime`` itself. A failure
    here must never break a turn: None simply restores the previous behaviour,
    where the model may still call the tool on its own.
    """
    try:
        now = get_current_datetime.invoke({"timezone_offset_hours": offset_hours})
    except Exception:  # noqa: BLE001 — a clock read must never fail a run
        logger.warning("temporal_frame: clock read failed; no frame injected", exc_info=True)
        return None
    if not isinstance(now, dict) or not now.get("date"):
        logger.warning("temporal_frame: unexpected tool payload %r; no frame injected", type(now))
        return None
    return (
        f"\U0001f550 CURRENT TIME — computed by the harness, authoritative: today is "
        f"{now['date']}, {now['time_local']} {now['timezone']} "
        f"(ISO {now['datetime_iso']}).\n"
        "Anchor EVERY date you write on this value — deadlines, due dates, event times, "
        'and any relative reference ("today", "tomorrow", "next week", "in 3 days"). '
        "Never state a date that is not derived from it. For date arithmetic call "
        "calculate_future_datetime; call get_current_datetime only if you need a "
        "different timezone.\n"
        "This block is CONTEXT, not a request — do not reply to it. Continue with the "
        "user's task."
    )


class TemporalFrameMiddleware(AgentMiddleware):
    """Append an authoritative current-time frame to every model request.

    Request-only, unconditional, and last in the message list — see the module
    docstring for why each of those is deliberate rather than incidental.

    Stateless, so an instance would be safe to share; IRIS still builds it fresh
    per agent, matching the convention the stateful guards require.
    """

    name = "TemporalFrameMiddleware"

    def __init__(self, *, timezone_offset_hours: float = _DEFAULT_OFFSET_HOURS) -> None:
        super().__init__()
        self._offset = timezone_offset_hours

    def _framed(self, request: ModelRequest) -> ModelRequest:
        """`request` with the frame appended, or unchanged if the clock is unreadable."""
        text = _frame_text(self._offset)
        if text is None:
            return request
        messages = list(getattr(request, "messages", None) or [])
        return request.override(
            messages=[*messages, HumanMessage(content=text, name=TEMPORAL_FRAME_SOURCE)]
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        return handler(self._framed(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        return await handler(self._framed(request))



