"""reasoning_trim.py — Strip Nemotron reasoning traces from message state.

Problem this solves
-------------------
The Nemotron models run with ``enable_thinking=True`` + a ``reasoning_budget``.
Every assistant turn therefore carries a chain-of-thought trace, which
``langchain_nvidia_ai_endpoints`` parks in two places on the returned message:

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
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

# additional_kwargs keys ChatNVIDIA uses to carry the reasoning trace.
# (chat_models._custom_postprocess: reasoning_content is always set; reasoning
# and _reasoning_api_fields are set when the API returned separate fields.)
_REASONING_KEYS = ("reasoning_content", "reasoning", "_reasoning_api_fields")

# Complete <think>…</think> pair (tolerant of attributes / whitespace / case).
_THINK_PAIR_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)


def _strip_think_tags(text: str) -> str:
    """Remove inline ``<think>…</think>`` reasoning from a content string.

    Handles the three shapes seen in the wild:
      * complete pairs ``<think>…</think>answer`` → ``answer``;
      * a stray closing tag (reasoning emitted as a prefix, no opening tag) →
        keep only the tail after the last ``</think>``;
      * an unclosed opening tag (truncated reasoning at the end) → drop from the
        tag onward.
    """
    cleaned = _THINK_PAIR_RE.sub("", text)

    low = cleaned.lower()
    if "</think>" in low:  # stray closing tag → reasoning was a prefix
        idx = low.rfind("</think>")
        cleaned = cleaned[idx + len("</think>") :]
        low = cleaned.lower()
    if "<think>" in low:  # unclosed opening tag → truncated reasoning tail
        cleaned = cleaned[: low.find("<think>")]

    return cleaned.strip()


def _clean_content(content: Any) -> Any:
    """Return content with reasoning removed, or the SAME object if unchanged."""
    if isinstance(content, str):
        if "<think>" in content.lower() or "</think>" in content.lower():
            return _strip_think_tags(content)
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
