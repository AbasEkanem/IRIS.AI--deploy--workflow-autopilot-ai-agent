"""tool_call_repair.py — Recover a tool call the provider parser left in `content`.

Problem this solves
-------------------
NVIDIA's hosted NIM endpoint has a documented tool-calling failure mode: instead
of populating the OpenAI-style ``tool_calls`` field, the parser emits the call as
raw JSON *text* inside ``message.content`` and leaves ``tool_calls`` empty. Three
independent sources describe it:

  * NVIDIA's own function-calling documentation states Nemotron tool calling is
    supported "with detailed thinking off" — IRIS previously ran thinking ON for
    five of its six agents, all tool callers (fixed in loadenv.py).
  * Stack Overflow for Agents, on nemotron-3-ultra-550b: the parser will "emit the
    tool call as raw JSON/text in the content field instead of the ``tool_calls``
    field, or not produce one at all."
  * HuggingFace NVFP4 discussion #8: tool calling fails because "the model does
    not produce a final ``}``" — a TRUNCATED call, which is what a too-small
    completion ceiling causes (also addressed in loadenv.py, 16k → 32k).

To LangGraph an ``AIMessage`` with no ``tool_calls`` is a finished answer. So the
observed symptoms are all one bug:

  raw JSON reaches the user  ← the run ends on the unparsed blob
  "empty tool calls"         ← truncated JSON, no closing brace
  loops                      ← no valid call → the model re-emits → loop breaker
  long tasks fail            ← per-step failure rate compounds over 30+ steps

Fix
---
Two hooks, deliberately split by what each can see:

  1. ``wrap_model_call`` — THE REPAIR. If the response already has ``tool_calls``,
     only their NAMES are checked (see ``_recover_tool_name``: a measured shape
     where the parser leaks template text into the name field); a response whose
     names are all valid is returned byte-identical. Otherwise ``content`` is
     scanned for a tool call, truncation is repaired by brace-balancing, and the
     name is validated against ``request.tools`` — the tool list is only available
     here, which is why the repair lives in this hook.

  2. ``after_agent`` — THE NUDGE, for what repair could not fix (unparseable, or a
     name the model invented). The run is about to end with a tool-call blob as
     its "answer", so the blob is removed, a persisted nudge is appended, and the
     graph jumps back to the model. Bounded per user turn so it can never loop.

SAFETY: the middleware never synthesises a call to a tool the model was not
offered. A parsed name absent from ``request.tools`` is refused and falls through
to the nudge path. Without that check a hallucinated name would be promoted from
harmless text into an actual dispatch attempt.

Modelled on ``blank_recovery.py`` (the ``after_agent`` + ``can_jump_to`` idiom and
the per-turn fence) and ``reasoning_trim.py`` (the ``wrap_model_call`` idiom).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from typing import Annotated, Any, Callable, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from reasoning_trim import strip_think_tags

logger = logging.getLogger(__name__)

# Message-name tag for the injected nudge. Named (not anonymous) so the guardrail
# taxonomy files it as a correction and the UI renders it as a collapsed
# correction card rather than as something the user typed. Must stay in sync with
# guardrail_taxonomy.py and its UI mirror ui/src/lib/corrections.ts.
REPAIR_SOURCE = "iris_toolcall_repair"

# At most this many nudges per USER TURN. Per turn, not per thread — a
# thread-lifetime counter silently switches the guard off forever once spent (the
# bug blank_recovery._MAX_EMPTY_RECOVERIES documents at length). Two covers a
# transient parser miss plus one follow-up.
_MAX_REPAIR_NUDGES = 2

# Never jump back on an already-huge history: at that size a malformed tail is
# treated as a genuine stop rather than risking more churn. Mirrors
# blank_recovery's ceilings.
_MAX_AI_MESSAGES = 60

# ── Envelope patterns, most specific first ────────────────────────────────────
# <TOOLCALL>[{...}]</TOOLCALL> is Nemotron's native envelope; <tool_call>{...}
# </tool_call> is the Hermes/Qwen style some NIM builds emit. Both are
# UNAMBIGUOUS — no legitimate prose answer contains them — which is what lets the
# after_agent detector fire on them without needing the tool list.
_TOOLCALL_ENVELOPE_RE = re.compile(
    r"<\s*TOOLCALL\s*>(?P<body>.*?)<\s*/\s*TOOLCALL\s*>", re.DOTALL | re.IGNORECASE
)
_TOOL_CALL_TAG_RE = re.compile(
    r"<\s*tool_call\s*>(?P<body>.*?)<\s*/\s*tool_call\s*>", re.DOTALL | re.IGNORECASE
)
# An unclosed envelope — the truncation case. Matches to end-of-string.
_TOOLCALL_OPEN_RE = re.compile(
    r"<\s*(?:TOOLCALL|tool_call)\s*>(?P<body>.*)\Z", re.DOTALL | re.IGNORECASE
)
# A ```json … ``` (or bare ```) fence.
_FENCE_RE = re.compile(
    r"```(?:json|JSON)?\s*(?P<body>[\[{].*?)(?:```|\Z)", re.DOTALL
)

# Keys a Nemotron/OpenAI-shaped call may use for its arguments.
_ARG_KEYS = ("arguments", "args", "parameters", "input")


def _messages(state: Any) -> list:
    """Pull the message list out of agent state (dict or attribute form)."""
    if isinstance(state, dict):
        return state.get("messages") or []
    return getattr(state, "messages", None) or []


def _state_value(source: Any, key: str) -> Any:
    """Read a private counter from state, tolerant of dict or attribute form."""
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
    ``blank_recovery._real_user_turn_key`` — kept local rather than imported so
    the two guards cannot break each other.
    """
    n = 0
    last_id = ""
    for msg in messages:
        if isinstance(msg, HumanMessage) and not getattr(msg, "name", None):
            n += 1
            last_id = str(getattr(msg, "id", "") or "")
    return f"{n}:{last_id}"


# ─────────────────────────────────────────────────────────────────────────────
# JSON recovery (pure functions — unit-testable offline, see
# tmp/test_tool_call_repair.py)
# ─────────────────────────────────────────────────────────────────────────────
def _balance_json(text: str) -> str:
    """Append the closing brackets a truncated JSON blob is missing.

    Walks the text tracking string/escape state so braces INSIDE string literals
    are not counted, then closes whatever is still open, innermost first. Also
    trims a dangling ``"key":`` / trailing comma so the result can actually parse.

    This is the fix for the documented "the model does not produce a final ``}``"
    failure. If the text is already balanced it is returned unchanged.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and stack[-1] == ("{" if ch == "}" else "["):
                stack.pop()

    if not stack and not in_string:
        return text

    out = text
    if in_string:
        out += '"'  # close the truncated string literal
    # Drop a trailing comma or a key with no value — both make json.loads fail.
    out = re.sub(r",\s*$", "", out)
    out = re.sub(r',?\s*"[^"]*"\s*:\s*$', "", out)
    for opener in reversed(stack):
        out += "}" if opener == "{" else "]"
    return out


def _loads(text: str) -> Any:
    """json.loads with a brace-balancing retry for truncated blobs."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    repaired = _balance_json(text)
    if repaired == text:
        return None
    try:
        return json.loads(repaired)
    except (json.JSONDecodeError, ValueError):
        return None


def _as_call_dicts(parsed: Any) -> list[dict]:
    """Normalise a parsed blob to a list of candidate call dicts."""
    if isinstance(parsed, dict):
        # OpenAI wire shape: {"function": {"name": ..., "arguments": "..."}}
        fn = parsed.get("function")
        return [fn if isinstance(fn, dict) else parsed]
    if isinstance(parsed, list):
        out: list[dict] = []
        for item in parsed:
            if isinstance(item, dict):
                fn = item.get("function")
                out.append(fn if isinstance(fn, dict) else item)
        return out
    return []


def _extract_args(candidate: dict) -> dict | None:
    """Pull the arguments dict out of a candidate call.

    ``arguments`` may be a dict OR a JSON-encoded string (the OpenAI wire format);
    both are accepted. A call with a name and no arguments at all is legal — a
    zero-arg tool — and yields ``{}``.
    """
    for key in _ARG_KEYS:
        if key not in candidate:
            continue
        raw = candidate[key]
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            parsed = _loads(raw)
            if isinstance(parsed, dict):
                return parsed
            if raw.strip() in ("", "{}"):
                return {}
            return None
        if raw is None:
            return {}
        return None
    return {}


def find_tool_call_blob(text: str) -> tuple[str, list[dict], bool] | None:
    """Find a tool-call blob in a completion's text.

    Returns ``(matched_substring, candidate_dicts, enveloped)`` or ``None``.
    ``enveloped`` is True when the blob came from an explicit ``<TOOLCALL>`` /
    ``<tool_call>`` tag or a code fence — an unambiguous signal, which is what the
    ``after_agent`` detector keys on since it has no tool list to validate against.

    Reasoning is stripped FIRST: think-prose routinely contains example JSON, and
    extracting that would fire a tool call the model never intended.
    """
    cleaned = strip_think_tags(text or "")
    if not cleaned:
        return None

    for pattern, enveloped in (
        (_TOOLCALL_ENVELOPE_RE, True),
        (_TOOL_CALL_TAG_RE, True),
        (_TOOLCALL_OPEN_RE, True),
        (_FENCE_RE, True),
    ):
        match = pattern.search(cleaned)
        if not match:
            continue
        candidates = _as_call_dicts(_loads(match.group("body")))
        if candidates:
            return match.group(0), candidates, enveloped

    # Bare JSON: only when the WHOLE completion is the blob. A JSON object
    # mentioned inside prose is far more likely to be a legitimate answer (IRIS
    # reporting an API payload) than a misplaced tool call, and the cost of a
    # false positive here is firing a tool the user never asked for.
    stripped = cleaned.strip()
    if stripped[:1] in ("{", "["):
        candidates = _as_call_dicts(_loads(stripped))
        if candidates:
            return stripped, candidates, False
    return None


def _looks_like_call(candidates: list[dict]) -> bool:
    """True if any candidate has the (name, arguments) shape of a tool call."""
    return any(
        isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"].strip()
        for c in candidates
    )


# ─────────────────────────────────────────────────────────────────────────────
# Repair (wrap_model_call side)
# ─────────────────────────────────────────────────────────────────────────────
def _tool_name_of(tool: Any) -> str | None:
    """Best-effort tool name from a BaseTool instance or an OpenAI-style dict.

    Same contract as ``loop_breaker._tool_name_of``; kept local so this module has
    no import dependency on the loop breaker.
    """
    name = getattr(tool, "name", None)
    if name:
        return name
    if isinstance(tool, dict):
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return fn["name"]
        return tool.get("name")
    return None


def _offered_tool_names(request: ModelRequest) -> set[str]:
    """The set of tool names this model call actually offered."""
    names: set[str] = set()
    for tool in getattr(request, "tools", None) or []:
        name = _tool_name_of(tool)
        if name:
            names.add(str(name))
    return names


def _repair_message(message: AIMessage, offered: set[str]) -> AIMessage | None:
    """Return a copy of `message` with recovered ``tool_calls``, or None.

    None means "not repairable" — no blob, unparseable, no name, or a name the
    model was never offered. Every one of those falls through to the nudge path
    rather than being guessed at.
    """
    text = _message_text(message)
    if not text.strip():
        return None

    found = find_tool_call_blob(text)
    if found is None:
        return None
    matched, candidates, _enveloped = found

    tool_calls: list[dict] = []
    for index, candidate in enumerate(candidates):
        name = candidate.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if name not in offered:
            # THE safety property: never synthesise a call to a tool the model was
            # not offered. Refuse the whole repair — a partially-repaired message
            # would fire some calls and silently drop others.
            logger.warning(
                "tool_call_repair: refusing repair — tool %r is not in the offered "
                "tool set (%d tools offered)",
                name,
                len(offered),
            )
            return None
        args = _extract_args(candidate)
        if args is None:
            return None
        call_id = candidate.get("id") or f"repair_{getattr(message, 'id', 'x')}_{index}"
        tool_calls.append(
            {"name": name, "args": args, "id": str(call_id), "type": "tool_call"}
        )

    if not tool_calls:
        return None

    # Remove the blob from content so the raw JSON never reaches the transcript or
    # the UI. Whatever prose surrounded it is preserved.
    remaining = text.replace(matched, "").strip() if matched else ""

    logger.warning(
        "tool_call_repair: recovered %d tool call(s) from content (%s) — the "
        "provider parser left the call in `content` with tool_calls empty",
        len(tool_calls),
        ", ".join(c["name"] for c in tool_calls),
    )
    try:
        return message.model_copy(update={"content": remaining, "tool_calls": tool_calls})
    except Exception:  # pragma: no cover — defensive
        logger.exception("tool_call_repair: model_copy failed; leaving message unchanged")
        return None


_IDENT_CHAR = re.compile(r"[A-Za-z0-9_]")


def _recover_tool_name(broken: str, offered: set[str]) -> str | None:
    """The offered tool the model MEANT, when `broken` is a corrupted tool name.

    A third failure shape, measured rather than documented. In
    ``tmp/e2e_multispecialist.py`` (2026-08-26) the hosted parser produced a
    ``tool_call`` whose *name* was::

        tavily_search\\nfunction=tavily_search({'query': '…'})\\nfunction=tavily_search()\\n</tool_call

    — the intended name followed by the raw template text that should have been
    consumed by the parser. So ``tool_calls`` was NON-empty and the repair path
    above never looked at it (its fast path returns any response with tool_calls
    unchanged), while LangGraph rejected the call as an unknown tool. The run did
    recover — the model re-issued it correctly on the next step — so this costs a
    wasted super-step rather than the run, which is why it is repaired here and
    not escalated to a nudge.

    Recovery is deliberately narrow on two axes. The corrupted name must START with
    an offered name AND the very next character must be a non-identifier one — the
    boundary the leaked template always supplies (a newline, or ``(``). That second
    condition is what stops a hallucinated *variant* from being silently redirected
    to a real tool: ``send_slack_message_v2_beta`` does not match
    ``send_slack_message`` (the boundary character is ``_``), so it is refused
    rather than quietly promoted into a real outbound send. Among names that do
    match we take the LONGEST, so ``search_drive_files`` is never mistaken for a
    shorter ``search_drive``. A name already in `offered` never reaches here.
    Anything ambiguous returns None and falls through to LangGraph's own
    invalid-tool error, which is the behaviour that already recovered the run.
    """
    cleaned = (broken or "").strip()
    if not cleaned:
        return None
    matches = [
        name
        for name in offered
        if cleaned.startswith(name) and not _IDENT_CHAR.match(cleaned[len(name) : len(name) + 1])
    ]
    if not matches:
        return None
    return max(matches, key=len)


def _repair_call_names(message: AIMessage, offered: set[str]) -> AIMessage | None:
    """Copy of `message` with corrupted tool-call NAMES rewritten, or None.

    Only names absent from `offered` are touched, and only when
    ``_recover_tool_name`` finds an unambiguous match — so the module's central
    safety property holds unchanged: a call is never redirected to a tool the
    model was not offered.
    """
    calls = list(getattr(message, "tool_calls", None) or [])
    if not calls:
        return None

    fixed: list[dict] = []
    changed = False
    for call in calls:
        name = str(call.get("name") or "")
        if name in offered:
            fixed.append(call)
            continue
        recovered = _recover_tool_name(name, offered)
        if recovered is None:
            fixed.append(call)
            continue
        logger.warning(
            "tool_call_repair: rewrote corrupted tool name %r -> %r "
            "(parser leaked template text into the name field)",
            name[:120],
            recovered,
        )
        fixed.append({**call, "name": recovered})
        changed = True

    if not changed:
        return None
    try:
        return message.model_copy(update={"tool_calls": fixed})
    except Exception:  # noqa: BLE001 - never let a repair attempt break the run
        logger.exception("tool_call_repair: name-repair model_copy failed; leaving unchanged")
        return None


def _repair_response(response: ModelResponse, request: ModelRequest) -> ModelResponse:
    """Repair the AI message in `response`, or return it unchanged."""
    result = getattr(response, "result", None)
    if not result:
        return response

    ai_indexes = [i for i, m in enumerate(result) if isinstance(m, AIMessage)]
    if not ai_indexes:
        return response

    offered = _offered_tool_names(request)
    if not offered:
        return response  # nothing to validate against — refuse to guess

    # A response that ALREADY has tool_calls needs no content-blob repair, but its
    # names may still be corrupted (see _recover_tool_name). That check is the only
    # work done on this path, and it is a no-op when every name is valid — so a
    # healthy response is still returned byte-identical.
    if any(getattr(result[i], "tool_calls", None) for i in ai_indexes):
        renamed_any = False
        new_result = list(result)
        for i in ai_indexes:
            renamed = _repair_call_names(new_result[i], offered)
            if renamed is not None:
                new_result[i] = renamed
                renamed_any = True
        return replace(response, result=new_result) if renamed_any else response

    repaired_any = False
    new_result = list(result)
    for i in ai_indexes:
        repaired = _repair_message(new_result[i], offered)
        if repaired is not None:
            new_result[i] = repaired
            repaired_any = True

    return replace(response, result=new_result) if repaired_any else response


# ─────────────────────────────────────────────────────────────────────────────
# Nudge (after_agent side)
# ─────────────────────────────────────────────────────────────────────────────
_NUDGE_TEXT = (
    "Your last turn printed a tool call as TEXT instead of issuing it. It was not "
    "executed — printing JSON does nothing.\n\n"
    "Re-issue it as a real tool call now, using the tool-calling mechanism, not "
    "message content. Do not wrap it in <TOOLCALL> tags, a code fence, or any "
    "other markup.\n\n"
    "If the tool you named does not exist, pick the correct tool from the ones you "
    "were given. If you have no tool call to make, write your answer as plain "
    "prose instead."
)


def _unparsed_call_tail(messages: list) -> AIMessage | None:
    """The final AI message, if the run is ending on an unparsed tool-call blob.

    Deliberately STRICTER than the repair path, because this hook has no tool list
    to validate against: it fires only on an explicit ``<TOOLCALL>`` /
    ``<tool_call>`` / fenced envelope, or on a completion that is ENTIRELY a JSON
    object with a ``name``. A JSON payload quoted inside a prose answer is left
    alone — that is far more likely to be a legitimate answer than a lost call.
    """
    if not messages:
        return None
    last = messages[-1]
    if not isinstance(last, AIMessage) or getattr(last, "tool_calls", None):
        return None

    found = find_tool_call_blob(_message_text(last))
    if found is None:
        return None
    _matched, candidates, enveloped = found
    if not _looks_like_call(candidates):
        return None
    if not enveloped and _message_text(last).strip()[:1] not in ("{", "["):
        return None
    return last


class ToolCallRepairState(AgentState):
    """Private state for the per-turn nudge budget."""

    # How many repair nudges have been issued during the CURRENT user turn.
    iris_toolcall_repairs: NotRequired[Annotated[int, PrivateStateAttr]]
    # Which turn that counter belongs to (see _real_user_turn_key). A different
    # live key means the counter is stale and the budget resets — without this the
    # counter becomes a thread-lifetime total and the guard dies after two uses.
    iris_toolcall_repair_turn: NotRequired[Annotated[str, PrivateStateAttr]]


class MalformedToolCallRepairMiddleware(AgentMiddleware):
    """Recover tool calls the provider parser left as text in ``content``.

    See the module docstring. ``wrap_model_call`` does the repair (it is the only
    hook with access to ``request.tools``, which the safety check needs);
    ``after_agent`` supplies a bounded nudge for what repair could not fix.

    Both sync and async variants of every hook are implemented so the guard holds
    on ``.invoke`` and ``.ainvoke`` (the Slack webhook uses the async path).
    Declares private state, so build a FRESH instance per agent — never share.
    """

    name = "MalformedToolCallRepairMiddleware"
    state_schema = ToolCallRepairState

    # ── Repair ───────────────────────────────────────────────────────────────
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return _repair_response(handler(request), request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> ModelResponse:
        return _repair_response(await handler(request), request)

    # ── Nudge ────────────────────────────────────────────────────────────────
    def _nudge(self, state: AgentState[Any]) -> dict[str, Any] | None:
        """Remove an unparsed tool-call tail, nudge, and jump back to the model."""
        messages = _messages(state)
        tail = _unparsed_call_tail(messages)
        if tail is None:
            return None  # common case — pure no-op

        turn_key = _real_user_turn_key(messages)
        used = _state_int(state, "iris_toolcall_repairs")
        if _state_value(state, "iris_toolcall_repair_turn") != turn_key:
            used = 0

        if used >= _MAX_REPAIR_NUDGES:
            logger.warning(
                "tool_call_repair: unparsed tool call after %d nudges this turn (%s) "
                "— letting the turn end rather than looping",
                used,
                turn_key,
            )
            return None
        ai_count = sum(1 for m in messages if isinstance(m, AIMessage))
        if ai_count >= _MAX_AI_MESSAGES:
            logger.warning(
                "tool_call_repair: unparsed tool call with oversized history "
                "(ai=%d) — treating it as a genuine stop",
                ai_count,
            )
            return None

        logger.warning(
            "tool_call_repair: run ending on an unparsed tool call "
            "(nudge %d/%d this turn) — jumping back to the model",
            used + 1,
            _MAX_REPAIR_NUDGES,
        )

        updates: list[Any] = []
        tail_id = getattr(tail, "id", None)
        if tail_id:
            # Drop the blob so the model does not echo it back and the user never
            # sees raw JSON in the transcript.
            updates.append(RemoveMessage(id=tail_id))
        updates.append(HumanMessage(content=_NUDGE_TEXT, name=REPAIR_SOURCE))

        return {
            "messages": updates,
            "iris_toolcall_repairs": used + 1,
            "iris_toolcall_repair_turn": turn_key,
            "jump_to": "model",
        }

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        """Nudge once (bounded) when a run is about to end on an unparsed call."""
        return self._nudge(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: AgentState[Any], runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        """Async variant of `after_agent`."""
        return self._nudge(state)
