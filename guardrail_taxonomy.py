"""guardrail_taxonomy.py — one place that knows every steering message IRIS can receive.

The harness steers IRIS with injected messages: IRIS's own recovery/resume nudges
(blank_recovery.py, resume_context.py, loop_breaker.py, tool_call_repair.py,
todo_reconcile.py) plus the deepagents Nemotron profile's ten internal guards. They
are deliberately PERSISTED into graph state — that is what keeps a run
self-correcting across turns — which means they also flow out through /history and
the SSE bridge and, without a classifier, render as though the USER typed them.

This module is the single source of truth for recognising them. It is mirrored by
``ui/src/lib/corrections.ts``; keep the two in sync.

Three distinct classes, because the mechanism differs and so does what is possible:

  1. NAME-CLASSIFIABLE + PERSISTED — carry ``name=<source>`` and land in
     ``aget_state().values["messages"]``. Fully observable, live and on reload.
  2. CONTENT-PREFIX ONLY — loop_breaker's short-circuit ToolMessages carry no usable
     ``name``, and all three use ``status="success"``, so neither field discriminates
     them. Only the content prefix does.
  3. REQUEST-ONLY — injected via ``request.override(messages=[...])`` inside
     wrap_model_call. These NEVER reach graph state, so they cannot appear in
     ``stream_mode="updates"`` or in /history at any cost. Listed here so the
     classifier can label one if a state marker is ever added, and so the omission is
     documented rather than silently mysterious.

Non-ASCII markers we match on byte-for-byte are written as explicit escapes, so no
encoding step between here and loop_breaker.py can quietly break a prefix test.
"""

from __future__ import annotations

from typing import Any

# ── 1. IRIS's own sources — mirror the constants in the middleware modules ───
BLANK_TASK_SOURCE = "iris_blank_result_recovery"            # blank_recovery.py:80
EMPTY_COMPLETION_SOURCE = "iris_empty_completion_recovery"  # blank_recovery.py:81
LOOP_TERMINATION_SOURCE = "iris_loop_terminator"            # loop_breaker.py:266
RESUME_SOURCE = "iris_resume_context"                       # resume_context.py:56
TOOLCALL_REPAIR_SOURCE = "iris_toolcall_repair"             # tool_call_repair.py:78
TODO_RECONCILE_SOURCE = "iris_todo_reconcile"               # todo_reconcile.py:74

# ── 2. The Nemotron profile's ten internal names ─────────────────────────────
# Read from the profile's own frozenset so the two can never drift. The literal
# fallback keeps the classifier working if the profile is absent or renamed — note
# that switching the orchestrator to another model REMOVES these names rather than
# replacing them: the nemotron profile is the only one in deepagents that injects
# named guards at all.
_NEMOTRON_FALLBACK = frozenset({
    "nemotron_final_answer_guard",
    "nemotron_transition_nudge",
    "nemotron_followup_guard",
    "nemotron_entity_guard",
    "nemotron_action_commit_nudge",
    "nemotron_tool_chain_nudge",
    "nemotron_filesystem_request_nudge",
    "nemotron_domain_tool_preference",
    "nemotron_domain_tool_nudge",
    "nemotron_progress_budget",
})

try:  # pragma: no cover — import path depends on the installed deepagents build
    from deepagents.profiles.harness._nvidia_nemotron_3_ultra import (  # type: ignore
        _INTERNAL_MESSAGE_NAMES as _PROFILE_NAMES,
    )
except Exception:  # noqa: BLE001 — a missing/renamed profile must never break the app
    _PROFILE_NAMES = frozenset()

NEMOTRON_SOURCES = frozenset(_PROFILE_NAMES) | _NEMOTRON_FALLBACK

# The budget guard is the ONLY nudge delivered as an AIMessage rather than a
# HumanMessage: NemotronProgressBudget returns it from wrap_model_call in place of a
# real model call (_nvidia_nemotron_3_ultra.py:1027-1033), so it persists as an
# assistant turn. It must never be served as IRIS's final answer — the profile's own
# _is_final_answer excludes it at :795, and _final_answer_from_state now does too.
BUDGET_GUARD_SOURCE = "nemotron_progress_budget"
BUDGET_GUARD_METADATA_KEY = "nemotron_progress_budget_reason"

IRIS_SOURCES = frozenset({
    BLANK_TASK_SOURCE,
    EMPTY_COMPLETION_SOURCE,
    LOOP_TERMINATION_SOURCE,
    RESUME_SOURCE,
    TOOLCALL_REPAIR_SOURCE,
    TODO_RECONCILE_SOURCE,
})

#: Every name-classifiable source, whether or not it reaches graph state.
ALL_NAMED_SOURCES = IRIS_SOURCES | NEMOTRON_SOURCES

#: Injected via request.override — never in graph state, never in /history.
REQUEST_ONLY_SOURCES = frozenset({
    LOOP_TERMINATION_SOURCE,              # loop_breaker.py:473
    "nemotron_domain_tool_preference",    # _nvidia_nemotron_3_ultra.py:1106
    "nemotron_filesystem_request_nudge",  # _nvidia_nemotron_3_ultra.py:1135
})

#: Sources that reach graph state, and so can be surfaced live and after reload.
PERSISTED_SOURCES = ALL_NAMED_SOURCES - REQUEST_ONLY_SOURCES

# ── 3. Content markers for the sources that carry no usable name ──────────────
# U+26A0 U+FE0F (warning sign + emoji variation selector) + " LOOP GUARD " + U+2014
# (em dash) + " ". loop_breaker.py:161, :177, :348.
LOOP_GUARD_PREFIX = "⚠️ LOOP GUARD — "
#: Written by the loop terminator's state marker (see loop_breaker.py).
LOOP_TERMINATOR_PREFIX = "⛔ LOOP TERMINATOR — "
#: U+26A0 U+FE0F + " DISPATCH BUDGET " + U+2014 + " ". loop_breaker.py:195 — the
#: per-turn cap on total `task` dispatches. Same shape as the LOOP GUARD messages
#: (unnamed ToolMessage, status="success"), so only this prefix discriminates it.
#: It went unregistered until now, which meant classify() returned None for it and
#: the workspace rendered a harness instruction as though the USER had typed
#: "you have already dispatched N subtasks…".
DISPATCH_BUDGET_PREFIX = "⚠️ DISPATCH BUDGET — "

# Harness bookkeeping that either duplicates a dedicated panel or carries no signal.
# Suppressed from the workspace rather than labelled, so the feed stays readable.
NOISE_PREFIXES = (
    "Updated todo list to ",   # langchain todo.py:161 — the todo panel already shows this
    "(empty tool result)",     # blank_recovery.py:75 / profile :65
    "Updated file ",           # filesystem backend write confirmation
)

#: Ceiling for any verbatim text crossing to the browser. The workspace renders
#: third-party content (email bodies, web pages, Slack messages) as inert text; a cap
#: keeps one large tool result from dominating the panel or the wire.
MAX_RAW_CHARS = 1200

_WARN = "warn"
_INFO = "info"

# label + severity per source. `severity` drives the panel's colour band and its tag:
# "warn" for a guard that CAUGHT something wrong (shown as SELF-CORRECTION), "info" for
# a forward-looking guard that steered IRIS before it went wrong (shown as GUARDRAIL).
#
# These labels are the headline text of a guardrail row in the workspace, so they are
# written from the guard's point of view — "Steered …", "Held …", "Caught …" — never
# "Nudged …". A nudge sounds like a suggestion IRIS was free to ignore; what actually
# happened is that the harness corrected the run.
_LABELS: dict[str, tuple[str, str]] = {
    BLANK_TASK_SOURCE: ("Caught a blank subtask result and kept going", _WARN),
    EMPTY_COMPLETION_SOURCE: ("Caught an empty response and kept going", _WARN),
    RESUME_SOURCE: ("Resumed after an interruption — told not to repeat completed work", _INFO),
    LOOP_TERMINATION_SOURCE: ("Disabled a looping tool and asked for a summary", _WARN),
    TOOLCALL_REPAIR_SOURCE: ("Caught a tool call printed as text and re-issued it", _WARN),
    TODO_RECONCILE_SOURCE: ("Caught an unfinished plan and required it be closed out", _WARN),
    "nemotron_transition_nudge": ("Steered to treat this as a new task", _INFO),
    "nemotron_action_commit_nudge": ("Held to performing the action now, not describing it", _INFO),
    "nemotron_tool_chain_nudge": ("Steered to finish the chained follow-on action", _INFO),
    "nemotron_domain_tool_nudge": ("Steered to a domain tool after an empty file search", _INFO),
    "nemotron_domain_tool_preference": ("Steered away from filesystem tools", _INFO),
    "nemotron_filesystem_request_nudge": ("Steered toward filesystem tools", _INFO),
    "nemotron_followup_guard": ("Rewrote a vague follow-up question", _WARN),
    "nemotron_entity_guard": ("Told to resolve each entity with its own lookup", _INFO),
    "nemotron_final_answer_guard": ("Caught a problem in the final answer", _WARN),
    BUDGET_GUARD_SOURCE: ("Hit the harness step budget and fell back to a summary", _WARN),
}

# Two sources emit two DIFFERENT texts under one name, so the name alone cannot label
# the reason. Each entry maps a content prefix to a more precise label.
_SECONDARY: dict[str, tuple[tuple[str, str], ...]] = {
    "nemotron_entity_guard": (
        ("Your final answer is using or mixing opaque entity IDs",
         "Caught unresolved entity IDs in the final answer"),
        ("Before answering, resolve each current-entity",
         "Told to resolve each entity with its own lookup"),
    ),
    "nemotron_final_answer_guard": (
        ("Your final answer omitted exact literal value",
         "Caught missing literal values in the final answer"),
        ("Your final answer should communicate the concrete outcome",
         "Caught a vague result for a state-changing action"),
    ),
}

# Sub-kinds of the LOOP GUARD ToolMessage, discriminated by what follows the prefix.
_LOOP_GUARD_VARIANTS: tuple[tuple[str, str], ...] = (
    ("this exact subtask was already delegated to",
     "Blocked a duplicate delegation and reused the earlier result"),
    ("this exact subtask has already been attempted",
     "Stopped retrying a subtask that keeps failing"),
    ("the tool ",
     "Blocked a repeated tool call with identical arguments"),
)


def truncate(text: str, limit: int = MAX_RAW_CHARS) -> str:
    """Cap verbatim text for the wire, marking the cut so it never reads as complete."""
    t = text or ""
    if len(t) <= limit:
        return t
    return t[:limit] + "\n…[truncated]"


def is_budget_guard(m: Any) -> bool:
    """True for the NemotronProgressBudget fallback AIMessage.

    Checked by name AND by response_metadata: the metadata key is the more robust
    discriminator (it survives a name change), while the name is what already-persisted
    turns carry.
    """
    if getattr(m, "name", None) == BUDGET_GUARD_SOURCE:
        return True
    meta = getattr(m, "response_metadata", None) or {}
    return isinstance(meta, dict) and BUDGET_GUARD_METADATA_KEY in meta


def is_noise(text: str) -> bool:
    """True for harness bookkeeping the workspace should drop rather than label."""
    t = (text or "").lstrip()
    return any(t.startswith(p) for p in NOISE_PREFIXES)


def _label_for(source: str, text: str) -> tuple[str, str]:
    """Resolve (label, severity), refining by content where one name has two texts."""
    label, severity = _LABELS.get(source, ("Internal steering message", _INFO))
    stripped = (text or "").lstrip()
    for prefix, refined in _SECONDARY.get(source, ()):
        if stripped.startswith(prefix):
            return refined, severity
    return label, severity


def classify(m: Any, text: str | None = None) -> dict | None:
    """Classify one graph message as a guardrail/steering message.

    Returns ``{source, label, severity, raw, persisted}`` for anything the harness
    injected to steer IRIS, or ``None`` for an ordinary message. ``raw`` is truncated
    and is the ONLY verbatim content that crosses to the browser.

    Pass ``text`` when the caller has already flattened the content, to avoid
    re-flattening a multimodal content list.
    """
    body = text if text is not None else str(getattr(m, "content", "") or "")

    source = getattr(m, "name", None)
    if source in ALL_NAMED_SOURCES:
        label, severity = _label_for(source, body)
        return {
            "source": source,
            "label": label,
            "severity": severity,
            "raw": truncate(body),
            "persisted": source not in REQUEST_ONLY_SOURCES,
        }

    # The budget guard may arrive carrying metadata but no name.
    if is_budget_guard(m):
        label, _ = _label_for(BUDGET_GUARD_SOURCE, body)
        meta = getattr(m, "response_metadata", None) or {}
        reason = meta.get(BUDGET_GUARD_METADATA_KEY) if isinstance(meta, dict) else None
        return {
            "source": BUDGET_GUARD_SOURCE,
            "label": f"{label} ({reason})" if reason else label,
            "severity": _WARN,
            "raw": truncate(body),
            "persisted": True,
        }

    # loop_breaker's short-circuit ToolMessages: no name, and status="success" on all
    # three, so the content prefix is the only discriminator. Each entry carries its
    # own default label — the LOOP GUARD variants below refine only that marker, and
    # applying them to DISPATCH BUDGET would mislabel it as "Blocked a repeating call".
    stripped = body.lstrip()
    for marker, src, default_label in (
        (LOOP_GUARD_PREFIX, "loop_guard", "Blocked a repeating call"),
        (LOOP_TERMINATOR_PREFIX, LOOP_TERMINATION_SOURCE, "Blocked a repeating call"),
        (DISPATCH_BUDGET_PREFIX, "dispatch_budget",
         "Hit the delegation budget for this turn and required a final answer"),
    ):
        if stripped.startswith(marker):
            rest = stripped[len(marker):]
            label = default_label
            for prefix, variant_label in _LOOP_GUARD_VARIANTS:
                if rest.startswith(prefix):
                    label = variant_label
                    break
            return {
                "source": src,
                "label": label,
                "severity": _WARN,
                "raw": truncate(body),
                "persisted": True,
            }

    return None
