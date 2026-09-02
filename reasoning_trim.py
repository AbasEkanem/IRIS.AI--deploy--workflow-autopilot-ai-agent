"""reasoning_trim.py — Strip Nemotron reasoning traces from message state.

Problem this solves
-------------------
When Nemotron runs with ``enable_thinking=True``, every assistant turn carries a
chain-of-thought trace, which ``langchain_nvidia_ai_endpoints`` parks in two
places on the returned message:

(Thinking is now OFF by default — ``NEMOTRON_ENABLE_THINKING`` in loadenv.py, set
to 0 because NVIDIA document tool calling as supported only "with detailed
thinking off". This middleware is still required, for three reasons: threads
checkpointed BEFORE that change still carry dirty history that is re-sent to the
model; ``NEMOTRON_ENABLE_THINKING=1`` is the supported one-var revert; and a
model may emit ``<think>`` tags regardless of the flag.)

  * ``additional_kwargs["reasoning_content"]`` (always), plus
    ``additional_kwargs["reasoning"]`` / ``["_reasoning_api_fields"]`` when the
    API returned them as separate fields; and
  * inline ``<think>…</think>`` tags left *inside* ``message.content`` when the
    model emits tag-style reasoning in NON-structured mode
    (``final_content = content_with_tags`` — see chat_models._custom_postprocess).

LangChain's ``AIMessage.content_blocks`` then synthesises a ``{"type":
"reasoning"}`` block from that ``reasoning_content``. Nothing in the stack ever
removes any of it, so the trace:

  1. is rendered verbatim by LangGraph Studio ("raw JSON thoughts"), and
  2. is checkpointed and re-sent to the model every turn, bloating context and
     confusing the smaller orchestrator into loops / half-finished runs.

Fix
---
A ``wrap_model_call`` middleware that strips the reasoning from messages on BOTH
sides of the model call:

  * OUTBOUND (``response.result``) — the messages the model just produced, before
    they are appended to graph state. This is the load-bearing half: it keeps the
    trace out of persisted state, so Studio renders clean and later turns never
    see it. (``content_blocks`` is a computed property, so removing the
    ``additional_kwargs`` reasoning keys removes the reasoning block too.)
  * INBOUND (``request.messages``) — defence for threads whose checkpointed
    history is already dirty (e.g. a run from before this middleware existed):
    the dirty history is scrubbed before it is re-sent to the model.

The middleware keeps no state and never fabricates content: a message with
nothing to strip is returned unchanged (same object identity), so it is cheap and
safe to attach to IRIS and to every subagent. Reasoning only ever appears on
assistant messages, so only ``AIMessage`` instances are touched.

The blank-turn hazard
---------------------
Stripping can legitimately empty a turn — if the model spent its whole completion
budget inside ``<think>`` it produced no answer, and ``""`` is the honest result.
That matters because this middleware runs on EVERY subagent
(subagent_config.py:244) while ``BlankResultRecoveryMiddleware`` is
orchestrator-only, so an emptied subagent turn is forwarded to IRIS as a blank
``task`` result and costs a whole re-dispatch.

Two consequences are built in above:

* ``strip_think_tags(…, salvage=True)`` no longer treats a stray ``</think>`` as
  proof that everything before it was reasoning. These models emit a spurious
  closing tag after a FINISHED answer, and the old rule deleted that answer
  outright — a self-inflicted blank response rather than a model one. The salvage
  is opt-in, and ``tool_call_repair`` deliberately does not take it: that caller
  fires the result as a tool call, so on an ambiguous tail it must prefer nothing
  over think-prose. A blank response is recoverable; a fabricated tool call is not.
* When stripping does empty a tool-call-less turn, ``_clean_message`` logs a
  WARNING. The recovery guards already handle the blank; the log is what
  separates "the trim emptied it" from "the model returned nothing", which are
  different bugs with different fixes and were previously indistinguishable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

# additional_kwargs keys ChatNVIDIA uses to carry the reasoning trace.
# (chat_models._custom_postprocess: reasoning_content is always set; reasoning
# and _reasoning_api_fields are set when the API returned separate fields.)
_REASONING_KEYS = ("reasoning_content", "reasoning", "_reasoning_api_fields")

# Complete <think>…</think> pair (tolerant of attributes / whitespace / case).
_THINK_PAIR_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)


def strip_think_tags(text: str, *, salvage: bool = False) -> str:
    """Remove inline ``<think>…</think>`` reasoning from a content string.

    Handles the four shapes seen in the wild:
      * complete pairs ``<think>…</think>answer`` → ``answer``;
      * a stray closing tag (reasoning emitted as a prefix, no opening tag) →
        keep only the tail after the last ``</think>``;
      * an unclosed opening tag (truncated reasoning at the end) → drop from the
        tag onward. If that leaves nothing, the model spent its whole completion
        budget on reasoning and genuinely produced no answer — ``""`` is the
        honest result, and the empty-completion guards downstream exist for it.
      * **a stray closing tag AFTER a finished answer** (``answer</think>``),
        which only ``salvage=True`` recovers — see below.

    ``salvage`` exists because the two callers want opposite things from the same
    ambiguous input. ``answer</think>`` may be an answer followed by a spurious
    closing tag, or reasoning followed by a correct one whose answer was
    truncated away; nothing in the text settles it.

      * ``salvage=True`` (``_clean_content``, the render/persist path) keeps the
        text before the tag when keeping the tail would empty the message.
        Without it, an answer followed by a spurious tag was stripped to ``""``
        — a blank final turn, which on a subagent becomes a blank ``task``
        result and on the orchestrator ends the run. The fallback is applied
        AFTER the unclosed-tag pass, because that tail is sometimes itself
        reasoning (``answer</think>\\n<think>truncated…``) and checking it any
        earlier still threw the answer away.
      * ``salvage=False`` (the default, and what ``tool_call_repair`` passes)
        keeps the old strict behaviour. That caller scans the result for JSON and
        FIRES it as a real tool call, and think-prose routinely contains *example*
        JSON — so for it, returning ``""`` on an ambiguous tail is the safe
        answer and salvaging would risk a tool call the model never intended.
        A blank response is recoverable; a fabricated ``send_email`` is not.

    PUBLIC because ``tool_call_repair`` needs it: before scanning a completion for
    a tool call that the provider parser left in ``content``, the reasoning must
    come off first.
    """
    cleaned = _THINK_PAIR_RE.sub("", text)

    low = cleaned.lower()
    head = ""
    if "</think>" in low:  # stray closing tag → reasoning was a prefix
        idx = low.rfind("</think>")
        head = cleaned[:idx]  # …but keep what preceded it, as a fallback
        cleaned = cleaned[idx + len("</think>") :]
        low = cleaned.lower()
    if "<think>" in low:  # unclosed opening tag → truncated reasoning tail
        cleaned = cleaned[: low.find("<think>")]

    cleaned = cleaned.strip()
    if cleaned or not salvage or not head.strip():
        return cleaned
    # Everything after the stray closing tag turned out to be reasoning too, so
    # taking it emptied the message. Fall back to what preceded the tag —
    # recursively, since the head may carry tags of its own. This terminates:
    # `head` always excludes at least the `</think>` that produced it.
    return strip_think_tags(head, salvage=True)


def _clean_content(content: Any) -> Any:
    """Return content with reasoning removed, or the SAME object if unchanged."""
    if isinstance(content, str):
        if "<think>" in content.lower() or "</think>" in content.lower():
            # salvage=True: this is the path whose output becomes the message the
            # user sees and the history the model re-reads, so an ambiguous tail
            # must not be allowed to blank the turn. See strip_think_tags.
            return strip_think_tags(content, salvage=True)
        return content
    if isinstance(content, list):
        # Defensive: some providers express content as typed blocks; drop any
        # reasoning/thinking block. Nemotron normally uses a plain string here.
        cleaned = [
            b
            for b in content
            if not (isinstance(b, dict) and b.get("type") in ("reasoning", "thinking"))
        ]
        return cleaned if len(cleaned) != len(content) else content
    return content


def _is_blank(content: Any) -> bool:
    """True when content carries no usable text, in str or block-list form."""
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        return not "".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        ).strip()
    return not content


def _clean_message(msg: Any) -> Any:
    """Strip reasoning from one assistant message; non-AI messages pass through.

    Returns the SAME object when there is nothing to strip so callers can detect
    "unchanged" by identity and avoid needless copies / request overrides.
    """
    if not isinstance(msg, AIMessage):
        return msg

    ak = getattr(msg, "additional_kwargs", None)
    ak_has = isinstance(ak, dict) and any(k in ak for k in _REASONING_KEYS)

    content = getattr(msg, "content", None)
    new_content = _clean_content(content)
    content_changed = new_content is not content

    # Observability floor. Stripping is ALLOWED to empty a turn — a model that
    # spent its whole completion budget on reasoning genuinely produced no answer
    # — but it must never do so silently, because this is the exact shape that
    # reaches a user as a "blank response". On a subagent the empty final turn is
    # forwarded as a blank `task` result; on the orchestrator it is the
    # tool-call-less AIMessage LangGraph ends the run on. blank_recovery.py
    # recovers both, but only this line distinguishes "the trim emptied it" from
    # "the model returned nothing", which are different bugs with different fixes.
    if content_changed and not getattr(msg, "tool_calls", None):
        if _is_blank(new_content) and not _is_blank(content):
            logger.warning(
                "reasoning_trim: stripping reasoning emptied a tool-call-less turn "
                "(%d chars in, 0 out) — downstream will see a blank completion",
                len(str(content or "")),
            )

    if not ak_has and not content_changed:
        return msg  # nothing to do — preserve identity

    update: dict[str, Any] = {}
    if ak_has:
        update["additional_kwargs"] = {k: v for k, v in ak.items() if k not in _REASONING_KEYS}
    if content_changed:
        update["content"] = new_content

    try:
        return msg.model_copy(update=update)
    except Exception:  # pragma: no cover — fall back to in-place mutation
        if ak_has:
            for k in _REASONING_KEYS:
                ak.pop(k, None)
        if content_changed:
            try:
                msg.content = new_content
            except Exception:
                pass
        return msg


def _clean_messages(messages: list) -> tuple[list, bool]:
    """Clean a message list; return ``(messages, changed)`` (identity-preserving)."""
    if not messages:
        return messages, False
    cleaned = [_clean_message(m) for m in messages]
    changed = any(c is not o for c, o in zip(cleaned, messages))
    return (cleaned if changed else messages), changed


def _clean_request(request: ModelRequest) -> ModelRequest:
    """Scrub already-persisted reasoning out of the history sent to the model."""
    cleaned, changed = _clean_messages(getattr(request, "messages", None) or [])
    return request.override(messages=cleaned) if changed else request


def _clean_response(response: ModelResponse) -> ModelResponse:
    """Scrub reasoning out of freshly produced messages before they hit state."""
    result = getattr(response, "result", None)
    if result is None:
        # Defensive: contract allows a bare AIMessage return.
        return _clean_message(response) if isinstance(response, AIMessage) else response
    cleaned, changed = _clean_messages(result)
    return replace(response, result=cleaned) if changed else response


class ReasoningTrimMiddleware(AgentMiddleware):
    """Strip Nemotron reasoning traces from inbound and outbound messages.

    Stateless — safe to share across the sync and async agents and to attach to
    IRIS and every subagent alike. Both hooks are implemented so the guard holds
    on ``.invoke()`` and ``.ainvoke()`` paths.
    """

    name = "ReasoningTrimMiddleware"

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return _clean_response(handler(_clean_request(request)))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> ModelResponse:
        return _clean_response(await handler(_clean_request(request)))
