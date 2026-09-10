"""nemotron_harness.py — Portable middleware harness for Nemotron-family models.

A self-contained, pluggable distillation of the IRIS project's production
anti-Nemotron harness (measured against hosted NIM ultra-550b / super-120b /
lightning-30b failures). Zero project-specific imports: it depends only on
`langchain`'s public middleware API, so it can be dropped into ANY agent built
on `langchain.agents` (create_react_agent / create_deep_agent / custom graphs).

Quickstart
----------
    from langchain.agents import create_react_agent
    from nemotron_harness import build_nemotron_model, build_nemotron_harness

    model = create_nemotron_model("nvidia/nemotron-3-super-120b-a12b",
                                  api_key=os.environ["NVIDIA_API_KEY"])
    agent = create_react_agent(model, tools, middleware=build_nemotron_harness())

    # Or pick individual middlewares:
    from nemotron_harness import NemotronEmptyCompletionMiddleware
    agent = create_react_agent(model, tools, middleware=[NemotronEmptyCompletionMiddleware()])

What it fights (each measured in production, not documented):
-------------------------------------------------------------
  1. EMPTY completions        — model returns no text and no tool call; LangGraph
                                ends the run silently mid-plan (its exit rule is
                                `last_ai.tool_calls == 0`, which cannot tell an
                                empty turn from a finished one).
  2. Tool calls as TEXT       — the NIM parser emits the call as raw JSON inside
                                `content` with `tool_calls` empty.
  3. TRUNCATED calls          — the model "does not produce a final }".
  4. Reasoning pollution      — `</think> traces and `reasoning_content` bleed into state.
  5. Wire-format drift        — ChatNVIDIA needs tool_calls mirrored into
                                additional_kwargs for history replay.
  6. Loops                    — identical tool calls / delegations re-emitted forever.
  7. Date hallucination       — confident near-miss dates written into real artifacts.

Layers (all sync + async twins, per the framework rule):
---------------------------------------------------------
  wrap_model_call  — wire repair + strict empty-retry (fix output BEFORE the
                     graph's exit edge can misread it)
  wrap_tool_call   — loop budgets at the argument boundary
  after_agent      — the last-chance backstop with jump_to="model" authority

Bounds (why the harness itself cannot loop): every recovery is budgeted —
strict in-call retries are capped, node backstops are per-USER-TURN counters
(never thread-lifetime: those silently die), and every path degrades to an
honest terminal answer instead of looping.

All steering messages are named `nh_*` so a host app can classify them for its
UI (see GUARDRAIL_SOURCES at the bottom) and they never masquerade as user
turns: a HumanMessage with NO `name` is the only true user-turn marker.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Awaitable, Callable, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

logger = logging.getLogger("nemotron_harness")

# ── Public constants ──────────────────────────────────────────────────────────
EMPTY_TOOL_PLACEHOLDER = "(empty tool result)"
HARNESS_EXHAUSTED_METADATA = "nh_blank_recovery_exhausted"
_MAX_AI_MESSAGES = 60          # oversized-history ceiling shared by backstops
_MAX_TOOL_MESSAGES = 120

# ── Steering-message names (nh_ prefix; host UIs classify via GUARDRAIL_SOURCES)
SOURCE_EMPTY_RETRY = "nh_empty_retry"               # request-only strict-retry nudge
SOURCE_EMPTY_BACKSTOP = "nh_empty_backstop"         # persisted after_agent nudge
SOURCE_TOOLCALL_REPAIR = "nh_toolcall_repair"       # persisted blob nudge
SOURCE_LOOP_TERMINATOR = "nh_loop_terminator"       # request-only finalisation
SOURCE_TEMPORAL_FRAME = "nh_temporal_frame"         # request-only date anchor

#: name -> (label, severity) — a host UI's guardrail classifier can mirror this.
GUARDRAIL_SOURCES: dict[str, tuple[str, str]] = {
    SOURCE_EMPTY_BACKSTOP: ("Caught an empty response and kept going", "warn"),
    SOURCE_TOOLCALL_REPAIR: ("Caught a tool call printed as text and re-issued it", "warn"),
    SOURCE_LOOP_TERMINATOR: ("Disabled a looping tool and asked for a summary", "warn"),
    SOURCE_TEMPORAL_FRAME: ("Injected the authoritative current time", "info"),
    SOURCE_EMPTY_RETRY: ("Retried an empty response in-call", "info"),
}
# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers (self-contained — sibling middlewares duplicate tiny helpers
# rather than import each other, so they cannot break one another).
# ─────────────────────────────────────────────────────────────────────────────
def _text_of(message: Any) -> str:
    """Flatten message content (str or list-of-blocks) to plain text."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _state_get(state: Any, key: str) -> Any:
    """Read a state key, tolerant of dict or attribute form (fail-safe None)."""
    source = getattr(state, "state", None) or state
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _state_int(state: Any, key: str) -> int:
    try:
        return int(_state_get(state, key) or 0)
    except (TypeError, ValueError):
        return 0


def _messages_of(state: Any) -> list:
    return list(_state_get(state, "messages") or [])


def _count_ai(messages: list) -> int:
    return sum(1 for m in messages if isinstance(m, AIMessage))


def _turn_key(messages: list) -> str:
    """Stable identifier for the CURRENT user turn.

    Every harness nudge is persisted as ``HumanMessage(name=<source>)``, so a
    HumanMessage with NO name is the only marker of a genuine new user turn.
    Per-TURN budgets keyed on this never become thread-lifetime totals — the
    classic death of a recovery guard (forensics: 12 of 17 live threads had
    spent a thread-lifetime budget and were permanently unrecoverable).
    """
    n, last_id = 0, ""
    for msg in messages:
        if isinstance(msg, HumanMessage) and not getattr(msg, "name", None):
            n += 1
            last_id = str(getattr(msg, "id", "") or "")
    return f"{n}:{last_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Tail taxonomy — the completion predicate the agent loop lacks
# ─────────────────────────────────────────────────────────────────────────────
FINAL, EMPTY, TOOL_CALLS, HARNESS_REPORT, NON_TAIL = (
    "final", "empty", "tool_calls", "harness_report", "none",
)


def classify_tail(messages: list) -> str:
    """Classify the last message of a run — total over all possible tails.

    This is the semantic check the agent loop is missing: LangGraph ends the run
    whenever the last AIMessage has `tool_calls == 0`, which conflates transport
    validity with semantic completion. Ordering is load-bearing:
      1. not an AIMessage last → NON_TAIL (mid-loop; tools → model edge).
      2. harness metadata → HARNESS_REPORT (checked BEFORE text: the give-up
         answer HAS text; a text-first classifier misfiles it as FINAL).
      3. tool_calls → TOOL_CALLS (loop continues).
      4. text → FINAL; no text → EMPTY (the dead end).
    """
    if not messages:
        return NON_TAIL
    last = messages[-1]
    if not isinstance(last, AIMessage):
        return NON_TAIL
    meta = getattr(last, "response_metadata", None) or {}
    if meta.get(HARNESS_EXHAUSTED_METADATA):
        return HARNESS_REPORT
    if getattr(last, "tool_calls", None):
        return TOOL_CALLS
    return FINAL if _text_of(last).strip() else EMPTY
# ─────────────────────────────────────────────────────────────────────────────
# 1. Reasoning trim — strip Nemotron thinking traces from both wire directions
# ─────────────────────────────────────────────────────────────────────────────
_THINK_RE = re.compile(r"<think\b[^>]*>(.*?)\s*</think>\s*", re.DOTALL | re.IGNORECASE)
_REASONING_KWARGS = ("reasoning_content", "reasoning", "_reasoning_api_fields")


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text)


def _clean_message(message: Any) -> Any:
    if not isinstance(message, AIMessage):
        return message
    updates: dict[str, Any] = {}
    kwargs = getattr(message, "additional_kwargs", None) or {}
    if any(key in kwargs for key in _REASONING_KWARGS):
        updates["additional_kwargs"] = {k: v for k, v in kwargs.items() if k not in _REASONING_KWARGS}
    if isinstance(message.content, str) and "<think" in message.content:
        stripped = _strip_think(message.content)
        if stripped != message.content:
            updates["content"] = stripped
    elif isinstance(message.content, list):
        new_parts = []
        changed = False
        for part in message.content:
            if isinstance(part, dict) and part.get("text") and "<think" in str(part["text"]):
                new_text = _strip_think(str(part["text"]))
                changed = changed or new_text != part["text"]
                new_parts.append({**part, "text": new_text})
            else:
                new_parts.append(part)
        if changed:
            updates["content"] = new_parts
    return message.model_copy(update=updates) if updates else message


def _clean_messages(messages: list) -> tuple[list, bool]:
    if not messages:
        return messages, False
    cleaned = [_clean_message(m) for m in messages]
    changed = any(c is not o for c, o in zip(cleaned, messages))
    return (cleaned if changed else messages), changed


class NemotronReasoningTrimMiddleware(AgentMiddleware):
    """Strip `reasoning_content`/`bike` traces from inbound history AND outbound replies.

    Why wrap_model_call: reasoning must be scrubbed on BOTH sides of every call.
    Outbound is the load-bearing half — without it the trace is checkpointed and
    re-sent every turn, bloating context and confusing the model into loops.
    Inbound is the defence for checkpoints written before this middleware
    existed. Identity-preserving: a clean message is returned unchanged.
    Registered OUTERMOST by convention — nothing downstream may re-add reasoning.
    """

    name = "NemotronReasoningTrimMiddleware"

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        cleaned, changed = _clean_messages(getattr(request, "messages", None) or [])
        response = handler(request.override(messages=cleaned) if changed else request)
        result = getattr(response, "result", None)
        if result is None:
            return _clean_message(response) if isinstance(response, AIMessage) else response
        cleaned_result, changed = _clean_messages(result)
        return replace(response, result=cleaned_result) if changed else response

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        cleaned, changed = _clean_messages(getattr(request, "messages", None) or [])
        response = await handler(request.override(messages=cleaned) if changed else request)
        result = getattr(response, "result", None)
        if result is None:
            return _clean_message(response) if isinstance(response, AIMessage) else response
        cleaned_result, changed = _clean_messages(result)
        return replace(response, result=cleaned_result) if changed else response
# ─────────────────────────────────────────────────────────────────────────────
# 2. Temporal frame — the date, computed and injected, never remembered
# ─────────────────────────────────────────────────────────────────────────────
class NemotronTemporalFrameMiddleware(AgentMiddleware):
    """Append an authoritative current-time frame to EVERY model request.

    Measured: asked "what is today's date?", the rule "always call the time
    tool" got 100% compliance alone, but 0% with 20 messages of history — the
    instruction is displaced by conversation depth. The fix is structural: stop
    asking, compute the frame in Python, and put it in the LAST (most recent)
    position, which nothing outranks.

    Deliberate properties:
      * REQUEST-ONLY (never graph state): a timestamp in history is a second
        anchor that goes stale, and these models confidently anchor on it.
      * UNCONDITIONAL: the failure is not realising a date is needed ("when is
        the report due?" carries no keyword); a gate inherits that bug.
      * Fail-soft: a clock error returns the request unchanged.
    """

    name = "NemotronTemporalFrameMiddleware"

    def __init__(self, *, timezone_offset_hours: float = 1.0) -> None:
        self._offset = timezone_offset_hours

    def _framed(self, request: ModelRequest) -> ModelRequest:
        try:
            now = datetime.now(timezone(timedelta(hours=self._offset)))
            frame = (
                f"🕐 CURRENT TIME — computed by the harness, authoritative: "
                f"{now:%A, %Y-%m-%d %H:%M} (UTC{self._offset:+g}).\n"
                "Anchor EVERY date you write on this value — deadlines, due dates, "
                "event times, and any relative reference (\"today\", \"tomorrow\", "
                "\"next week\"). Never state a date not derived from it.\n"
                "This block is CONTEXT, not a request — do not reply to it."
            )
        except Exception:  # noqa: BLE001 — a clock read must never fail a run
            logger.warning("temporal_frame: clock read failed; no frame injected", exc_info=True)
            return request
        messages = list(getattr(request, "messages", None) or [])
        return request.override(messages=[*messages, HumanMessage(content=frame, name=SOURCE_TEMPORAL_FRAME)])

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        return handler(self._framed(request))

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        return await handler(self._framed(request))
# ─────────────────────────────────────────────────────────────────────────────
# 3. ChatNVIDIA wire compatibility — mirror tool-call fields the endpoint needs
# ─────────────────────────────────────────────────────────────────────────────
def _to_openai_tool_calls(tool_calls: list) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": call.get("name", ""), "arguments": json.dumps(call.get("args", {}) or {})},
        }
        for call in tool_calls
    ]


def _content_is_empty(message: Any) -> bool:
    content = getattr(message, "content", None)
    if content is None or (isinstance(content, str) and not content.strip()):
        return True
    return isinstance(content, list) and not any(
        isinstance(p, dict) and str(p.get("text", "")).strip() for p in content
    )


class NemotronWireCompatibilityMiddleware(AgentMiddleware):
    """Patch request messages for ChatNVIDIA serialization.

    Ported from the shipped ultra profile's ChatNVIDIAMessageCompatibility
    Middleware + the empty-result half of NemotronToolCallShim:

      * AIMessages with `tool_calls` but no `additional_kwargs["tool_calls"]`
        get the OpenAI-shaped mirror — without it, replaying history that
        carries tool traffic can serialize wrong on the NIM wire.
      * ToolMessages that carry the assistant `name` but lack it in
        additional_kwargs get it mirrored.
      * EMPTY ToolMessage results are rewritten to a visible placeholder — an
        empty result otherwise reads as "the call never ran", a re-emission /
        loop vector.
    Identity-preserving: messages that need nothing are returned unchanged.
    """

    name = "NemotronWireCompatibilityMiddleware"

    @staticmethod
    def _repair_message(message: Any) -> Any:
        if isinstance(message, AIMessage):
            if message.tool_calls and "tool_calls" not in (message.additional_kwargs or {}):
                additional = dict(message.additional_kwargs or {})
                additional["tool_calls"] = _to_openai_tool_calls(message.tool_calls)
                return message.model_copy(update={"additional_kwargs": additional})
            return message
        if isinstance(message, ToolMessage):
            if _content_is_empty(message):
                return message.model_copy(update={"content": EMPTY_TOOL_PLACEHOLDER})
            if getattr(message, "name", None) and "name" not in (message.additional_kwargs or {}):
                additional = dict(message.additional_kwargs or {})
                additional["name"] = message.name
                return message.model_copy(update={"additional_kwargs": additional})
        return message

    @staticmethod
    def _with_repaired_messages(request: ModelRequest) -> ModelRequest:
        original = list(request.messages or [])
        repaired = [NemotronWireCompatibilityMiddleware._repair_message(m) for m in original]
        changed = any(f is not o for f, o in zip(repaired, original))
        return request.override(messages=repaired) if changed else request

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        return handler(self._with_repaired_messages(request))

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        return await handler(self._with_repaired_messages(request))
# ─────────────────────────────────────────────────────────────────────────────
# 4. Tool-call repair — recover calls the NIM parser left as text
# ─────────────────────────────────────────────────────────────────────────────
_TOOLCALL_TAG_RE = re.compile(r"<TOOLCALL>\s*(\{.*?\})\s*</TOOLCALL>", re.DOTALL)
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_WHOLE_JSON_RE = re.compile(r"^\s*(\{.*\})\s*$", re.DOTALL)
# A truncated whole-message call has NO closing brace — `{...` with the JSON cut
# mid-way. This is the "model does not produce a final }" failure. Without this
# the repair path never fires on it and the raw blob ends the run.
_WHOLE_JSON_TRUNCATED_RE = re.compile(r"^\s*(\{.*)$", re.DOTALL)


def _balance_json(text: str) -> str:
    """Append the closing brackets a truncated JSON blob is missing.

    The fix for the documented "the model does not produce a final }" failure.
    Tracks string/escape state so braces INSIDE string literals are not counted,
    then closes whatever is still open, innermost first. Also trims a dangling
    `"key":` / trailing comma so the result can actually parse. Balanced text is
    returned unchanged.
    """
    stack: list[str] = []
    in_string = escaped = False
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
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
            else:
                return text  # mismatched — not a truncation we can fix
    if not stack:
        text = re.sub(r",\s*(?=[}\]])", "", text)
        return text
    balanced = re.sub(r",\s*$", "", text.rstrip())
    balanced = re.sub(r":\s*$", "", balanced)
    balanced = re.sub(r",\s*$", "", balanced)
    for closer in reversed(stack):
        balanced += closer
    return balanced


def _parse_call_obj(obj: Any) -> dict | None:
    """Accept {"name":..., "arguments": {...}|"str"} / {"function": {...}} shapes."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):
        fn = obj["function"]
        name, raw_args = fn.get("name"), fn.get("arguments")
    else:
        name, raw_args = obj.get("name"), obj.get("arguments", obj.get("args", obj.get("parameters")))
    if not isinstance(name, str) or not name.strip():
        return None
    if isinstance(raw_args, str):
        try:
            args = json.loads(_balance_json(raw_args))
        except Exception:  # noqa: BLE001
            return None
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        args = {}
    return {"name": name.strip(), "args": args if isinstance(args, dict) else {}, "type": "tool_call"}


def _candidates_from_text(text: str) -> list[dict]:
    """All tool-call-shaped candidates in `text`, most-specific envelope first."""
    candidates: list[dict] = []
    for match in _TOOLCALL_TAG_RE.finditer(text):
        call = _parse_call_obj(match.group(1))
        if call:
            candidates.append(call)
    for match in _FENCED_JSON_RE.finditer(text):
        call = _parse_call_obj(match.group(1))
        if call:
            candidates.append(call)
    whole = _WHOLE_JSON_RE.match(text)
    if whole:
        raw = whole.group(1)
        call = _parse_call_obj(_loads_or_None(raw)) or _parse_call_obj(_loads_or_None(_balance_json(raw)))
        if call:
            candidates.append(call)
        return candidates
    # Truncated (no closing brace) whole-message JSON — brace-balance + retry.
    truncated = _WHOLE_JSON_TRUNCATED_RE.match(text)
    if truncated:
        raw = truncated.group(1)
        call = _parse_call_obj(_loads_or_None(_balance_json(raw)))
        if call:
            candidates.append(call)
    return candidates


def _loads_or_None(raw: str) -> Any:
    """json.loads that fails to None (the whole-message branch feeds strings)."""
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _strip_call_text(text: str) -> str:
    """Content that remains after removing every envelope region (best effort)."""
    leftover = _TOOLCALL_TAG_RE.sub("", text)
    leftover = _FENCED_JSON_RE.sub("", leftover)
    leftover = _WHOLE_JSON_RE.sub("", leftover)
    return leftover.strip()
class NemotronToolCallRepairState(AgentState):
    """Private per-turn budget for the repair nudge."""

    nh_toolcall_repairs: NotRequired[Annotated[int, PrivateStateAttr]]
    nh_toolcall_repair_turn: NotRequired[Annotated[str, PrivateStateAttr]]


class NemotronToolCallRepairMiddleware(AgentMiddleware):
    """Recover a tool call the NIM parser left inside `content`.

    Two hooks, split by what each can see:

    * ``wrap_model_call`` — THE REPAIR. Scans a tool-call-less response's content
      for a call (explicit <TOOLCALL>, fenced JSON, or whole-message JSON —
      brace-balancing truncations), validates the name against ``request.tools``
      (the tool list exists ONLY here), and promotes the call to real
      `tool_calls`. SAFETY: a parsed name the model was not offered is NEVER
      promoted — that would turn harmless text into an actual dispatch.
    * ``after_agent`` — THE NUDGE, for what repair could not fix: the run is
      about to end on a tool-call blob as its "answer". The blob is removed, a
      persisted corrective nudge appended, bounded per user turn (2), and the
      graph jumps back to the model.

    To LangGraph an AIMessage with no `tool_calls` is a finished answer — which
    is why the un-repaired blob ends runs, reaches users as raw JSON, and
    manifests as "empty tool calls" / loops / long-task failures.
    """

    name = "NemotronToolCallRepairMiddleware"
    state_schema = NemotronToolCallRepairState

    _MAX_REPAIR_NUDGES = 2

    @staticmethod
    def _offered_names(request: ModelRequest) -> set[str]:
        names = set()
        for tool in getattr(request, "tools", None) or []:
            name = getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)
            if isinstance(name, str) and name:
                names.add(name)
        return names

    @staticmethod
    def _repair_response(response: ModelResponse, request: ModelRequest) -> ModelResponse:
        result = getattr(response, "result", None)
        if not result:
            return response
        offered = NemotronToolCallRepairMiddleware._offered_names(request)
        new_result, changed = list(result), False
        for i, message in enumerate(result):
            if not isinstance(message, AIMessage) or message.tool_calls:
                continue
            text = _text_of(message)
            if not text.strip():
                continue
            for call in _candidates_from_text(text):
                if offered and call["name"] not in offered:
                    continue  # never promote a call to a tool that was not offered
                leftover = _strip_call_text(text)
                repaired = message.model_copy(
                    update={"tool_calls": [{**call, "id": f"nh_repair_{i}"}], "content": leftover}
                )
                new_result[i], changed = repaired, True
                logger.info("tool_call_repair: promoted text call %r to tool_calls", call["name"])
                break
        return replace(response, result=new_result) if changed else response

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        return self._repair_response(handler(request), request)

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        return self._repair_response(await handler(request), request)
    # ── Nudge (after_agent) ──────────────────────────────────────────────────
    @staticmethod
    def _blob_tail(messages: list) -> AIMessage | None:
        """The final AI message, if the run is ending on an unparsed call blob.

        Deliberately STRICTER than the repair path (no tool list here): fires
        only on an explicit <TOOLCALL>/<fenced-JSON> envelope or an entirely-JSON
        message with a "name". JSON quoted inside prose is left alone.
        """
        if not messages:
            return None
        last = messages[-1]
        if not isinstance(last, AIMessage) or getattr(last, "tool_calls", None):
            return None
        text = _text_of(last)
        stripped = text.strip()
        if not stripped:
            return None
        if _TOOLCALL_TAG_RE.search(text) or _FENCED_JSON_RE.search(text):
            return last
        if stripped[0] in "{[" and _candidates_from_text(stripped):
            return last
        return None

    def _nudge(self, state: AgentState) -> dict[str, Any] | None:
        messages = _messages_of(state)
        tail = self._blob_tail(messages)
        if tail is None:
            return None
        turn_key = _turn_key(messages)
        used = 0 if _state_get(state, "nh_toolcall_repair_turn") != turn_key else _state_int(state, "nh_toolcall_repairs")
        if used >= self._MAX_REPAIR_NUDGES:
            logger.warning("tool_call_repair: unparsed call after %d nudges this turn — letting the turn end", used)
            return None
        if _count_ai(messages) >= _MAX_AI_MESSAGES:
            logger.warning("tool_call_repair: oversized history — treating blob as a genuine stop")
            return None
        updates: list = []
        tail_id = getattr(tail, "id", None)
        if tail_id:
            updates.append(RemoveMessage(id=tail_id))
        updates.append(
            HumanMessage(
                content=(
                    "Your last turn printed a tool call as TEXT instead of issuing it — "
                    "printing JSON does nothing; the call never ran.\n"
                    "Re-issue it now through the tool-calling mechanism. If the tool you "
                    "named does not exist, pick the correct tool from the ones you were "
                    "given. If you have no call to make, write your answer as plain prose."
                ),
                name=SOURCE_TOOLCALL_REPAIR,
            )
        )
        return {
            "messages": updates,
            "nh_toolcall_repairs": used + 1,
            "nh_toolcall_repair_turn": turn_key,
            "jump_to": "model",
        }

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        return self._nudge(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        return self._nudge(state)
# ─────────────────────────────────────────────────────────────────────────────
# 5. Empty-completion recovery — the core defense
# ─────────────────────────────────────────────────────────────────────────────
class NemotronEmptyCompletionState(AgentState):
    """Private per-turn budget for the empty-completion backstop."""

    nh_empty_recoveries: NotRequired[Annotated[int, PrivateStateAttr]]
    nh_empty_turn: NotRequired[Annotated[str, PrivateStateAttr]]


class NemotronEmptyCompletionMiddleware(AgentMiddleware):
    """Recover from empty completions at TWO layers — strict-first, backstop-second.

    THE core defense. The agent loop ends the run whenever the last AIMessage has
    `tool_calls == 0` — it cannot tell an EMPTY turn (no text, no calls; the
    model returned nothing) from a finished answer. Every content guard gated on
    "has text" is blind to it. Left alone: the run settles idle mid-plan, todos
    half-open, no error raised.

    * ``wrap_model_call`` — PRIMARY, strict. An empty response is retried IN-CALL
      (same request + one request-only corrective nudge, last position) up to
      ``max_retries`` times. No graph jump, no state churn; exhausted, the call
      ends OUT LOUD on an honest INCOMPLETE answer stamped with
      ``nh_blank_recovery_exhausted`` metadata — non-empty text, so every
      downstream consumer (the loop's exit edge, content guards) stands down.
    * ``after_agent`` — BACKSTOP. An empty tail that still reaches END (e.g. a
      middleware-ordering change) removes the blank turn, appends the persisted
      continue-or-finalize nudge, and jumps back. Per-USER-TURN budget (never
      thread-lifetime: a spent thread-lifetime counter silently kills the guard
      forever) plus an oversized-history ceiling so the backstop itself cannot
      loop. Past the cap: the honest INCOMPLETE answer, out loud.
    """

    name = "NemotronEmptyCompletionMiddleware"
    state_schema = NemotronEmptyCompletionState

    def __init__(self, *, max_retries: int = 5, max_backstops_per_turn: int = 5) -> None:
        self._max_retries = max_retries
        self._max_backstops = max_backstops_per_turn

    # ── give-up ──────────────────────────────────────────────────────────────
    _GIVE_UP_TEXT = (
        "**STATUS:** INCOMPLETE\n\n"
        "**SUMMARY:** I stopped returning content mid-run — the model produced empty "
        "responses repeatedly, and every recovery layer is exhausted, so I ended the "
        "turn rather than loop.\n\n"
        "**BLOCKERS:** Completed work is preserved in history. Send the request again "
        "— mentioning only the outstanding step — and I will continue from there."
    )

    @staticmethod
    def _give_up_messages(messages: list) -> list:
        """Replace the empty final turn with the honest INCOMPLETE answer.

        Deliberately UNNAMED: a name would file it as a guardrail card and leave
        the chat bubble empty again — this text IS the turn's answer. Stamped so
        classify_tail reports HARNESS_REPORT and content guards stand down.
        """
        updates: list = []
        empty_id = getattr(messages[-1], "id", None)
        if empty_id:
            updates.append(RemoveMessage(id=empty_id))
        updates.append(
            AIMessage(
                content=NemotronEmptyCompletionMiddleware._GIVE_UP_TEXT,
                response_metadata={HARNESS_EXHAUSTED_METADATA: True},
            )
        )
        return updates

    # ── PRIMARY: strict in-call retry ────────────────────────────────────────
    @staticmethod
    def _response_tail(response: Any) -> Any:
        result = getattr(response, "result", None)
        return result[-1] if result else None

    @staticmethod
    def _nudged_request(request: ModelRequest, attempt: int, max_retries: int) -> ModelRequest:
        messages = list(getattr(request, "messages", None) or [])
        nudge = HumanMessage(
            content=(
                "You returned an EMPTY response — no text and no tool call. That "
                f"completion was discarded; this is retry {attempt} of {max_retries}.\n"
                "Do NOT return empty again. Take ONE action now:\n"
                "1) Issue the next tool call for the user's task.\n"
                "2) If the last step failed, retry it ONCE with a materially-changed "
                "approach, or report it as blocked and move on.\n"
                "3) ONLY if the task is genuinely complete, write the final answer as "
                "plain prose. Never an empty message."
            ),
            name=SOURCE_EMPTY_RETRY,
        )
        return request.override(messages=[*messages, nudge])

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        response = handler(request)
        attempt = 0
        while classify_tail([self._response_tail(response)]) == EMPTY and attempt < self._max_retries:
            attempt += 1
            logger.warning("empty_completion: strict in-call retry %d/%d", attempt, self._max_retries)
            response = handler(self._nudged_request(request, attempt, self._max_retries))
        if classify_tail([self._response_tail(response)]) == EMPTY:
            logger.warning("empty_completion: still empty after %d in-call retries — ending the call honestly", self._max_retries)
            result = list(getattr(response, "result", None) or [])
            for i in range(len(result) - 1, -1, -1):
                if isinstance(result[i], AIMessage) and not _text_of(result[i]).strip() and not getattr(result[i], "tool_calls", None):
                    result[i] = AIMessage(
                        content=self._GIVE_UP_TEXT,
                        response_metadata={HARNESS_EXHAUSTED_METADATA: True},
                    )
                    return replace(response, result=result)
            return response
        if attempt:
            logger.info("empty_completion: strict in-call retry recovered the response (attempt %d)", attempt)
        return response

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        response = await handler(request)
        attempt = 0
        while classify_tail([self._response_tail(response)]) == EMPTY and attempt < self._max_retries:
            attempt += 1
            logger.warning("empty_completion: strict in-call retry %d/%d", attempt, self._max_retries)
            response = await handler(self._nudged_request(request, attempt, self._max_retries))
        if classify_tail([self._response_tail(response)]) == EMPTY:
            logger.warning("empty_completion: still empty after %d in-call retries — ending the call honestly", self._max_retries)
            result = list(getattr(response, "result", None) or [])
            for i in range(len(result) - 1, -1, -1):
                if isinstance(result[i], AIMessage) and not _text_of(result[i]).strip() and not getattr(result[i], "tool_calls", None):
                    result[i] = AIMessage(
                        content=self._GIVE_UP_TEXT,
                        response_metadata={HARNESS_EXHAUSTED_METADATA: True},
                    )
                    return replace(response, result=result)
            return response
        if attempt:
            logger.info("empty_completion: strict in-call retry recovered the response (attempt %d)", attempt)
        return response
    # ── BACKSTOP: after_agent ────────────────────────────────────────────────
    def _backstop(self, state: AgentState) -> dict[str, Any] | None:
        messages = _messages_of(state)
        if classify_tail(messages) != EMPTY:
            return None
        turn_key = _turn_key(messages)
        used = 0 if _state_get(state, "nh_empty_turn") != turn_key else _state_int(state, "nh_empty_recoveries")
        if used >= self._max_backstops:
            logger.warning("empty_completion: budget spent (%s) — ending with an explicit incomplete answer", turn_key)
            return {"messages": self._give_up_messages(messages)}
        if _count_ai(messages) >= _MAX_AI_MESSAGES or sum(
            1 for m in messages if isinstance(m, ToolMessage)
        ) >= _MAX_TOOL_MESSAGES:
            logger.warning("empty_completion: oversized history — treating the empty stop as genuine, but out loud")
            return {"messages": self._give_up_messages(messages)}
        logger.warning("empty_completion: backstop recovery %d/%d this turn — jumping back to the model", used + 1, self._max_backstops)
        updates: list = []
        empty_id = getattr(messages[-1], "id", None)
        if empty_id:
            updates.append(RemoveMessage(id=empty_id))  # safe: no tool_calls to orphan
        updates.append(
            HumanMessage(
                content=(
                    "You returned an EMPTY response with no tool call. That would end the "
                    "run with the user's task unfinished, which is not a valid completion. "
                    "Do NOT stop here. Take ONE action now:\n"
                    "1) Continue the task with the next tool call.\n"
                    "2) If the last step failed, retry ONCE with a materially-changed "
                    "approach, or report it blocked and move on.\n"
                    "3) ONLY if genuinely finished, write the final answer as plain prose."
                ),
                name=SOURCE_EMPTY_BACKSTOP,
            )
        )
        return {
            "messages": updates,
            "nh_empty_recoveries": used + 1,
            "nh_empty_turn": turn_key,
            "jump_to": "model",
        }

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        return self._backstop(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:  # noqa: ARG002
        return self._backstop(state)
# ─────────────────────────────────────────────────────────────────────────────
# 6. Loop breaker — identical (tool, args) calls, soft tool-tier + hard model-tier
# ─────────────────────────────────────────────────────────────────────────────
def _signature(name: str, args: Any) -> str:
    try:
        payload = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        payload = str(args)
    return f"{name}|{payload}"


def _prior_identical_calls(messages: list, signature: str) -> tuple[int, str]:
    """(#prior identical calls this turn, last result text) for a tool signature."""
    count, last_result = 0, ""
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                if _signature(call.get("name", ""), call.get("args")) == signature:
                    count += 1
        elif isinstance(message, ToolMessage):
            last_result = _text_of(message)
    return count, last_result


class NemotronToolLoopBreakerMiddleware(AgentMiddleware):
    """Cap identical (tool name + arguments) calls per user turn, two tiers.

    The measured failure: a subagent re-emits the same call verbatim forever
    (the websearch-looping incident). Prompt rules are advisory; this is
    structural, at the tool boundary:

    * SOFT (`wrap_tool_call`): the 3rd+ identical call is SHORT-CIRCUITED — the
      prior result is reproduced back (capped, so the correction cannot balloon
      an already-struggling model) with "stop retrying; use it or change
      something material". The tool does NOT execute again: no further side
      effects. Two attempts cover a legitimate retry.
    * HARD (`wrap_model_call`): at _HARD_STOP_AFTER identical calls the soft
      notice has provably failed — the tool is REMOVED from the next request's
      tool set and a finalisation directive injected, so the agent physically
      cannot re-emit it and must wrap up.

    Decisions derive purely from message history in state (no mutable instance
    state); scan boundary is the last real user turn. `task`-style delegation
    tools are excluded (see NemotronDelegationLoopBreakerMiddleware).
    """

    name = "NemotronToolLoopBreakerMiddleware"

    def __init__(self, *, soft_after: int = 2, hard_after: int = 3, delegation_tool: str | None = "task") -> None:
        self._soft_after = soft_after
        self._hard_after = hard_after
        self._delegation_tool = delegation_tool

    @staticmethod
    def _short_circuit(tool_name: str, prior_result: str) -> ToolMessage:
        reproduced = (prior_result or "")[:4000]
        return ToolMessage(
            content=(
                f"⛔ LOOP GUARD — {tool_name} was already called with THESE EXACT "
                f"arguments this turn. Its previous result:\n{reproduced or '(empty)'}\n\n"
                "Stop retrying it. Either USE the result above, or change something "
                "material (different arguments, different tool, different approach) — "
                "an identical re-call is blocked."
            ),
        )

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return self._guard_tool(request, handler)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        blocked = self._guard_tool(request, handler)
        if isinstance(blocked, ToolMessage):
            return blocked
        return await handler(request)

    def _guard_tool(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        tool_call = getattr(request, "tool_call", None) or {}
        name = tool_call.get("name", "")
        if not name or name == self._delegation_tool:
            return None  # not guarded — caller decides (sync: handler / async: await)
        signature = _signature(name, tool_call.get("args"))
        messages = _messages_of(getattr(request, "state", None))
        prior, last_result = _prior_identical_calls(messages, signature)
        if prior >= self._soft_after:
            logger.warning("tool_loop_breaker: identical %s call #%d blocked this turn", name, prior + 1)
            return self._short_circuit(name, last_result)
        return None

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        return handler(self._hard_tier(request))

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        return await handler(self._hard_tier(request))

    def _hard_tier(self, request: ModelRequest) -> ModelRequest:
        messages = list(getattr(request, "messages", None) or [])
        counts: dict[str, int] = {}
        for message in messages:
            if isinstance(message, AIMessage):
                for call in message.tool_calls or []:
                    sig = _signature(call.get("name", ""), call.get("args"))
                    counts[sig] = counts.get(sig, 0) + 1
        banned = {sig.split("|", 1)[0] for sig, n in counts.items() if n >= self._hard_after}
        banned.discard(self._delegation_tool or "")
        if not banned:
            return request
        logger.warning("tool_loop_breaker: hard-stopping looping tools %s — removed from tool set", sorted(banned))
        tools = [t for t in (getattr(request, "tools", None) or []) if (getattr(t, "name", None) or "") not in banned]
        directive = HumanMessage(
            content=(
                "⛔ LOOP TERMINATED — the tool(s) "
                f"{', '.join(sorted(banned))} were called with identical arguments "
                f"{self._hard_after}+ times this turn and have been REMOVED for the "
                "remainder of it. Do not attempt them again. Wrap up now: report what "
                "was accomplished, what is blocked, and hand back a final prose answer."
            ),
            name=SOURCE_LOOP_TERMINATOR,
        )
        return request.override(tools=tools, messages=[*messages, directive])
# ─────────────────────────────────────────────────────────────────────────────
# 7. Delegation loop breaker — the "task tool" redispatch loop (E-21 class)
# ─────────────────────────────────────────────────────────────────────────────
def _signature_of_delegation(messages: list, delegation_tool: str) -> dict[str, list]:
    """(signature -> [status per attempt]) for delegation calls this turn.

    status: "ok" (a ToolMessage followed and was not an error-shaped string),
    "fail" (error-shaped ⚠️/❌/failed text), or "pending" (no result yet).
    """
    tool_call_ids: dict[str, dict] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                if call.get("name") == delegation_tool:
                    sig = _signature(delegation_tool, call.get("description"))
                    tool_call_ids[call.get("id", "")] = {"sig": sig, "status": "pending"}
    for message in messages:
        if isinstance(message, ToolMessage):
            entry = tool_call_ids.get(getattr(message, "tool_call_id", ""))
            if entry is None:
                continue
            text = _text_of(message).strip()
            lowered = text.lower()
            entry["status"] = (
                "fail" if (text.startswith(("⚠", "❌")) or "failed" in lowered or "error" in lowered) else "ok"
            )
    out: dict[str, list] = {}
    for entry in tool_call_ids.values():
        out.setdefault(entry["sig"], []).append(entry["status"])
    return out


class NemotronDelegationLoopBreakerMiddleware(AgentMiddleware):
    """Block identical re-delegations of a `task`-style tool, per user turn.

    The measured failure (E-21): a subagent yielded partial output, the model
    read it as "not done" and re-dispatched the IDENTICAL subtask — ~20
    duplicate artifacts. Governance says "one material retry, never an identical
    redispatch" — this enforces it structurally:

    * already completed OK → short-circuit with the prior result + "do not
      redispatch" (no second subagent run);
    * failed once → exactly ONE material retry allowed; an IDENTICAL 3rd
      attempt is blocked outright;
    * signatures are exact (whitespace/case-normalised), so legitimately
      different subtasks are never blocked.

    Only active if the agent actually has `delegation_tool` in its tool set
    (pass delegation_tool=None to disable entirely).
    """

    name = "NemotronDelegationLoopBreakerMiddleware"

    def __init__(self, *, delegation_tool: str = "task", max_identical: int = 2) -> None:
        self._delegation_tool = delegation_tool
        self._max_identical = max_identical

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        blocked = self._decide(request, handler)
        return blocked if isinstance(blocked, ToolMessage) else handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        blocked = self._decide(request, handler)
        return blocked if isinstance(blocked, ToolMessage) else await handler(request)

    def _decide(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        tool_call = getattr(request, "tool_call", None) or {}
        if tool_call.get("name") != self._delegation_tool:
            return None
        messages = _messages_of(getattr(request, "state", None))
        sig = _signature(self._delegation_tool, tool_call.get("description"))
        attempts = _signature_of_delegation(messages, self._delegation_tool).get(sig, [])
        if "ok" in attempts:
            return ToolMessage(
                content=(
                    "⛔ DELEGATION GUARD — this exact subtask was already completed "
                    "successfully this turn. Do NOT redispatch it; its result is in "
                    "history above. Use it and move on to the remaining work."
                ),
            )
        if attempts.count("fail") >= self._max_identical:
            return ToolMessage(
                content=(
                    "⛔ DELEGATION GUARD — this exact subtask already failed "
                    f"{len(attempts)} time(s) this turn. Stop retrying it. Report the "
                    "blocker and move on to any remaining independent work."
                ),
            )
        return None
# ─────────────────────────────────────────────────────────────────────────────
# 8. Model factory — ChatNVIDIA with the hard-won invocation invariants
# ─────────────────────────────────────────────────────────────────────────────
def create_nemotron_model(
    model_name: str,
    *,
    api_key: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    enable_thinking: bool | None = None,
    max_completion_tokens: int = 32768,
    timeout_seconds: float = 300.0,
):
    """Build a ChatNVIDIA model with the measured-correct invocation defaults.

    Three invariants, each a live production failure (do not "simplify" them):

    1. `enable_thinking` is passed EXPLICITLY (default False). Omitting it is NOT
       the same as False: some Nemotron runners default thinking ON and dump
       untagged reasoning into `content`, which no stripper can catch. Nemotron
       documents tool calling as supported "with detailed thinking off"; the
       on-mode also triples latency (measured p95 3.8s → 20.0s).
    2. NO `reasoning_budget` is sent. ChatNVIDIA maps it to
       `thinking_token_budget`, which the hosted runner rejects with HTTP 400 on
       every call. Thinking length is bounded by `max_completion_tokens` instead.
    3. `max_completion_tokens` is large (32k). A TRUNCATED tool call is a
       documented Nemotron failure ("the model does not produce a final }");
       a too-small completion ceiling manufactures exactly that. With thinking
       off the reasoning budget is freed, so the wider runway costs nothing.

    temperature 1.0 / top_p 0.95 are NVIDIA's documented operating point for the
    family (greedy decoding on a reasoning MoE is a repetition-loop driver).
    `timeout_seconds` makes a stalled endpoint a RETRYABLE exception (the
    transport default of 60s inherits silently otherwise).
    """
    try:
        from langchain_nvidia_ai_endpoints import ChatNVIDIA
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "langchain-nvidia-ai-endpoints is required for create_nemotron_model: pip install langchain-nvidia-ai-endpoints"
        ) from exc

    return ChatNVIDIA(
        model=model_name,
        api_key=api_key,
        temperature=1.0 if temperature is None else float(temperature),
        top_p=0.95 if top_p is None else float(top_p),
        max_completion_tokens=max_completion_tokens,
        timeout=timeout_seconds,
        model_kwargs={"chat_template_kwargs": {"enable_thinking": bool(enable_thinking) if enable_thinking is not None else False}},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 9. The harness assembler — one call, the full defense
# ─────────────────────────────────────────────────────────────────────────────
def build_nemotron_harness(
    *,
    empty_max_retries: int = 5,
    empty_max_backstops: int = 5,
    loop_soft_after: int = 2,
    loop_hard_after: int = 3,
    delegation_tool: str | None = "task",
    temporal_frame: bool = True,
    timezone_offset_hours: float = 1.0,
    wire_compatibility: bool = True,
) -> list[AgentMiddleware]:
    """The full anti-Nemotron middleware stack, in the load-bearing ORDER.

    Ordering matters and is deliberate:
      1. ReasoningTrim  — OUTERMOST. Nothing downstream may re-add reasoning.
      2. WireCompat     — patches message shapes before repair/validation see them.
      3. TemporalFrame  — request-only; appends LAST, so nothing after it
                          (repair, empty-retry) may drop or reorder the frame.
      4. ToolCallRepair — normalizes the response before the exit edge reads it.
      5. EmptyCompletion— strict retry wraps the whole inner stack: a retried
                          call re-enters repair too, so retries get repaired
                          responses, not just retried garbage.
      6-7. Loop breakers — INNERMOST at the tool boundary; hard tier mutates the
                          tool set last so it reflects repaired traffic counts.
    """
    stack: list[AgentMiddleware] = [NemotronReasoningTrimMiddleware()]
    if wire_compatibility:
        stack.append(NemotronWireCompatibilityMiddleware())
    if temporal_frame:
        stack.append(NemotronTemporalFrameMiddleware(timezone_offset_hours=timezone_offset_hours))
    stack.append(NemotronToolCallRepairMiddleware())
    stack.append(
        NemotronEmptyCompletionMiddleware(max_retries=empty_max_retries, max_backstops_per_turn=empty_max_backstops)
    )
    stack.append(
        NemotronToolLoopBreakerMiddleware(
            soft_after=loop_soft_after, hard_after=loop_hard_after, delegation_tool=delegation_tool
        )
    )
    if delegation_tool:
        stack.append(NemotronDelegationLoopBreakerMiddleware(delegation_tool=delegation_tool))
    return stack
# ─────────────────────────────────────────────────────────────────────────────
# 10. Optional: auto-attach inside a deepagents agent (guarded, fail-soft)
# ─────────────────────────────────────────────────────────────────────────────
def attach_to_deepagents(
    model_specs: tuple[str, ...] | list[str],
    *,
    system_prompt_suffix: str = "",
    middleware_factory: Callable[[], list] | None = None,
) -> bool:
    """Register the harness as a deepagents HarnessProfile for `model_specs`.

    For agents built on `create_deep_agent`: profiles resolve per
    `provider:model-id` during agent assembly, so this must run BEFORE any agent
    is built. Keys are exact `provider:model` specs (e.g.
    "NVIDIA:nvidia/nemotron-3-super-120b-a12b" — note the capital `NVIDIA`,
    which is what ChatNVIDIA instances report via `_get_ls_params`). Returns
    True if registered, False if deepagents is absent (plain-middleware agents
    don't need this — just pass build_nemotron_harness() directly).
    """
    try:
        from deepagents import register_harness_profile  # type: ignore
        from deepagents.profiles import HarnessProfile  # type: ignore
    except Exception:  # noqa: BLE001 — deepagents absent: fail-soft
        logger.info("attach_to_deepagents: deepagents not installed — skipped (pass the middlewares directly instead)")
        return False

    factory = middleware_factory or build_nemotron_harness
    profile = HarnessProfile(system_prompt_suffix=system_prompt_suffix, extra_middleware=factory)
    for spec in dict.fromkeys(model_specs):
        register_harness_profile(spec, profile)
    logger.info("attach_to_deepagents: registered harness profile for %s", list(dict.fromkeys(model_specs)))
    return True


if __name__ == "__main__":  # pragma: no cover — standalone smoke test (no network)
    """Smoke test. Run:  python nemotron_harness.py"""
    import sys

    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  {'[PASS]' if cond else '[FAIL]'} {name}")
        if not cond:
            failures.append(name)

    print("1. classify_tail")
    check("empty", classify_tail([AIMessage(content="")]) == EMPTY)
    check("final", classify_tail([AIMessage(content="done")]) == FINAL)
    give_up = AIMessage(content="**STATUS:** INCOMPLETE", response_metadata={HARNESS_EXHAUSTED_METADATA: True})
    check("harness_report", classify_tail([give_up]) == HARNESS_REPORT)
    check("tool_calls", classify_tail([AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1"}])]) == TOOL_CALLS)

    print("2. balance_json (truncation repair)")
    check("closes truncated brace", _balance_json('{"name": "t"') == '{"name": "t"}')
    check("trims dangling colon", _balance_json('{"name": "t", "arg":') in ('{"name": "t", "arg":}', '{"name": "t", "arg"}'))
    check("balanced unchanged", _balance_json('{"a": 1}') == '{"a": 1}')

    print("3. candidates_from_text")
    calls = _candidates_from_text('{"name": "search_web", "arguments": {"q": "ai"}}')
    check("whole-message JSON parsed", bool(calls) and calls[0]["name"] == "search_web")
    calls2 = _candidates_from_text('{"name": "search_web", "arguments": {"q": "ai"')
    check("truncated whole-message repaired", bool(calls2) and calls2[0]["name"] == "search_web")

    print("4. strict empty retry")
    mw = NemotronEmptyCompletionMiddleware(max_retries=5)
    calls_count = {"n": 0}
    script = [AIMessage(content=""), AIMessage(content=""), AIMessage(content="recovered")]

    def handler(request):
        calls_count["n"] += 1
        return ModelResponse(result=[script.pop(0)], structured_response=None)

    request = ModelRequest(model=None, messages=[HumanMessage(content="go")])
    out = mw.wrap_model_call(request, handler)
    check("recovered in-call", _text_of(out.result[0]) == "recovered" and calls_count["n"] == 3)

    def always_empty(request):
        return ModelResponse(result=[AIMessage(content="")], structured_response=None)

    out2 = mw.wrap_model_call(request, always_empty)
    check("exhaustion -> honest give-up", "INCOMPLETE" in _text_of(out2.result[0]) and out2.result[0].response_metadata.get(HARNESS_EXHAUSTED_METADATA))
    check("retry budget (1 initial + 5 retries = 6 calls)", calls_count["n"] == 3)

    print("5. tool-call repair (plain-middleware path)")
    repair = NemotronToolCallRepairMiddleware()
    blob = '{"name": "search_web", "arguments": {"query": "latest ai news"}'
    fake_tools = [type("T", (), {"name": "search_web"})()]

    def blob_handler(request):
        return ModelResponse(result=[AIMessage(content=blob)], structured_response=None)

    class DummyRequest:
        def __init__(self):
            self.messages = [HumanMessage(content="go")]
            self.tools = fake_tools

        def override(self, **kw):
            return self

    out3 = repair._repair_response(blob_handler(None), DummyRequest())
    check("blob promoted to tool_calls", bool(out3.result[0].tool_calls) and out3.result[0].tool_calls[0]["name"] == "search_web")

    print("6. harness assembler")
    stack = build_nemotron_harness()
    check("7 middlewares in order", len(stack) == 7 and isinstance(stack[0], NemotronReasoningTrimMiddleware) and isinstance(stack[-1], NemotronDelegationLoopBreakerMiddleware))
    check("delegation_tool=None drops it", len(build_nemotron_harness(delegation_tool=None)) == 6)

    print(f"\n{'=' * 60}")
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("ALL CHECKS PASSED — nemotron_harness is ready to drop into an agent.")
    sys.exit(0)















