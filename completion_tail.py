"""completion_tail.py — One tail taxonomy for every completion guard.

Problem this solves
-------------------
The stack has four PRIVATE predicates describing the same thing — the last
message of a run about to end:

  * `_is_final_answer` (vendored ultra profile) — text REQUIRED, so an empty
    completion is invisible to all ten shipped guards;
  * `_is_empty_completion` (blank_recovery) — text required to be ABSENT;
  * `_unparsed_call_tail` (tool_call_repair) — envelope-shaped tool-call text;
  * `gt.is_budget_guard` (web_api / guardrail_taxonomy) — the budget fallback.

Each guard can only see the tail classes its own predicate recognises — which is
exactly how the shipped guard family went blind to empty completions. This module
is the shared, TOTAL classification: every possible tail maps to exactly one
class, and every guard that consumes it can reason about all of them.

Deliberately self-contained: no cross-package import of the vendored profile's
private helpers (a pip install would wipe them), and no import from any guard
module (the documented convention is that sibling guards duplicate small helpers
rather than import each other, so they cannot break one another).

Pure functions, message objects in / class string out — trivially testable
offline. Both sync and async guards consume it identically (classification has
no I/O, so there is no async twin to write).
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

# Tail classes (string constants, not an enum: guards compare and log them and
# the strings appear in warning lines).
FINAL = "final"                    # non-empty prose, no tool calls — a real answer
EMPTY = "empty"                    # no text, no tool calls — the dead end
TOOL_CALLS = "tool_calls"          # the loop is continuing, not ending
HARNESS_REPORT = "harness_report"  # blank_recovery's give-up / strict-retry fallback
NON_TAIL = "none"                  # no messages, or the last message is not an AIMessage

#: Response-metadata keys stamped by the harness's own terminal answers. A tail
#: carrying either is the HARNESS speaking, not the model — content guards must
#: stand down on it (it already IS the honest report) and must not consume their
#: one-shot budget policing it.
_HARNESS_METADATA_KEYS = ("iris_blank_recovery_exhausted", "iris_strict_retry_exhausted")


def _text_of(message: Any) -> str:
    """Flatten message content (str or list-of-blocks) to plain text."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def is_harness_report(message: Any) -> bool:
    """True when the tail is a harness-authored terminal answer (never police it)."""
    meta = getattr(message, "response_metadata", None) or {}
    return any(bool(meta.get(key)) for key in _HARNESS_METADATA_KEYS)


def classify_tail(messages: list) -> str:
    """Classify the last message of a run — total over all possible tails.

    Ordering of the checks is load-bearing:

      1. ``not messages`` / last is not an AIMessage → NON_TAIL. A ToolMessage
         last means the loop is mid-flight (tools → model edge), not ending.
      2. Harness metadata → HARNESS_REPORT, checked BEFORE the text check: the
         give-up answer HAS text, so a text-first classifier would misfile it as
         FINAL and send content guards to police the harness's own words.
      3. ``tool_calls`` → TOOL_CALLS (loop continues; guards never fire here).
      4. Non-empty text → FINAL; else → EMPTY (the class `_is_final_answer`
         could not see, and the reason this module exists).
    """
    if not messages:
        return NON_TAIL
    last = messages[-1]
    if not isinstance(last, AIMessage):
        return NON_TAIL
    if is_harness_report(last):
        return HARNESS_REPORT
    if getattr(last, "tool_calls", None):
        return TOOL_CALLS
    if _text_of(last).strip():
        return FINAL
    return EMPTY
