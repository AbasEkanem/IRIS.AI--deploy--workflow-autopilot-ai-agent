"""prompt_caching.py — Anthropic prompt caching for IRIS's OpenRouter-routed agents.

WHY THIS EXISTS INSTEAD OF THE STOCK MIDDLEWARE
------------------------------------------------
deepagents ships `append_prompt_caching_middleware`, which appends
`AnthropicPromptCachingMiddleware` from langchain-anthropic. That middleware is
useless to IRIS as wired, and silently so — its gate is:

    if not isinstance(request.model, ChatAnthropic):   # prompt_caching.py:104
        ...
        return False                                   # no caching, no error

IRIS's orchestrator (and maya) reach Claude through `ChatOpenRouter`, not
`ChatAnthropic` — see the provider dispatch in loadenv.py:296, where a namespaced
ID like "anthropic/claude-opus-5" takes the OpenRouter branch by design. So the
stock middleware fails the isinstance check on every call and, with deepagents'
`unsupported_model_behavior="ignore"`, does nothing at all. Dropping it in would
have looked like a fix and changed zero tokens.

WHAT THIS CACHES, AND WHY THAT PREFIX
--------------------------------------
Measured (tmp/token_budget.py), the orchestrator resends a 16,993-token prefix on
EVERY model call: the 4,512-token system prompt, the three memory files
MemoryMiddleware splices in (IRIS.md 1,130 / agent.md 4,320 / security.md 1,041),
deepagents' own MEMORY_SYSTEM_PROMPT (1,102), the skills frontmatter listing
(371), and 4,517 tokens of tool schemas. On a 9-call orchestrator turn that is
~153k tokens of byte-identical prefix re-billed nine times — about 85% of the
task's whole input bill.

Anthropic orders a request as [tools][system][messages], and a cache breakpoint
caches everything BEFORE it. So a single breakpoint on the last system content
block covers tools AND system together — the entire 16,993-token prefix — which
is why this middleware tags only the system message and deliberately does NOT
tag tools. Tool-level tagging would need OpenRouter to forward a per-tool
`cache_control` extras field, which is not a contract it documents; the system
breakpoint gets the same coverage through a path that is verifiable here.

WHY TAGGING THE SYSTEM MESSAGE IS ENOUGH ON THIS TRANSPORT
-----------------------------------------------------------
Verified by reading langchain_openrouter/chat_models.py, not assumed:

  * `_convert_message_to_dict` (:1333) maps SystemMessage to
    `{"role": "system", "content": message.content}` — content passed through
    VERBATIM, no normalisation. A list of content blocks carrying
    `cache_control` therefore reaches OpenRouter exactly as written, and
    OpenRouter forwards it to Anthropic. (Contrast HumanMessage at :1290, which
    goes through `_format_message_content` first.)
  * The reverse direction already exists: `cached_tokens` / `cache_write_tokens`
    are parsed off the usage payload at :1560-1564, so cache hits are
    observable in `response_metadata` without any change here.

SCOPE — deliberately narrow
----------------------------
Applies only to ChatOpenRouter instances whose model ID is an Anthropic one.
  * Nemotron subagents (ChatNVIDIA): NIM exposes no prompt-cache control, so
    aurther/sienna/tavia/grace are untouched. Their prefixes (grace's is 15,169,
    the largest in the system) stay fully billed — a real remaining cost, called
    out here so it is not mistaken for solved.
  * openai/* and google/* on OpenRouter cache automatically with no breakpoint,
    and an unexpected `cache_control` block is a needless wire-format risk, so
    they are skipped too.
A miss is always a no-op that returns the request untouched; nothing here can
fail a model call that would otherwise have succeeded.

TTL
---
Default 5m. A cache write costs ~25% more than a normal input token and a hit
costs ~90% less, so the break-even is roughly two hits — trivially cleared by a
9-call turn. IRIS_PROMPT_CACHE_TTL=1h buys cross-turn hits within a session at a
2x write premium; worth it only if turns land more than 5 minutes apart.
Set IRIS_PROMPT_CACHE=0 to disable the whole thing without a code change.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from deepagents.middleware.memory import MemoryMiddleware
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)

# Anthropic will not create a cache entry below a model-dependent floor (1024
# tokens for Opus/Sonnet). Our prefix is ~17k so this never binds in practice;
# it exists so a stripped-down deployment can't quietly pay write premiums for
# entries too small to be created.
_MIN_CACHEABLE_CHARS = 4096  # ~1k tokens at ~4 chars/token


def _prompt_caching_enabled() -> bool:
    return os.getenv("IRIS_PROMPT_CACHE", "1").strip().lower() in ("1", "true", "yes", "on")


def _cache_ttl() -> str:
    ttl = os.getenv("IRIS_PROMPT_CACHE_TTL", "5m").strip()
    return ttl if ttl in ("5m", "1h") else "5m"


def _is_openrouter_anthropic(model: Any) -> bool:
    """True only for a ChatOpenRouter pointed at an anthropic/* model ID.

    Duck-typed rather than isinstance-checked so this module does not hard-import
    langchain_openrouter (which loadenv.py treats as an optional dependency) and
    so it keeps working if the class is ever re-exported from another path.
    """
    if type(model).__name__ != "ChatOpenRouter":
        return False
    model_id = str(getattr(model, "model", "") or getattr(model, "model_name", "") or "")
    return model_id.startswith("anthropic/")


def _tag_last_block(sysmsg: Any, cache_control: dict[str, str]) -> Any:
    """Return a SystemMessage with `cache_control` on its final content block.

    Returns the input unchanged when there is nothing taggable, so every caller
    can treat a failure to tag as a plain no-op.
    """
    if sysmsg is None:
        return sysmsg
    content = sysmsg.content
    if isinstance(content, str):
        if not content:
            return sysmsg
        return SystemMessage(content=[{"type": "text", "text": content, "cache_control": cache_control}])
    if isinstance(content, list) and content:
        blocks = list(content)
        last = blocks[-1]
        if isinstance(last, str):
            blocks[-1] = {"type": "text", "text": last, "cache_control": cache_control}
        elif isinstance(last, dict):
            blocks[-1] = {**last, "cache_control": cache_control}
        else:
            return sysmsg
        return SystemMessage(content=blocks)
    return sysmsg


class OpenRouterPromptCachingMiddleware(AgentMiddleware):
    """Tag the system prefix with `cache_control` for Anthropic-via-OpenRouter.

    One breakpoint on the last system content block, which on Anthropic's
    [tools][system][messages] ordering caches the tool schemas too. No-op for
    every other provider. See the module docstring for the measurements and the
    transport verification behind those choices.
    """

    def __init__(self, ttl: str | None = None) -> None:
        self.ttl = ttl or _cache_ttl()
        self._logged = False

    @property
    def _cache_control(self) -> dict[str, str]:
        return {"type": "ephemeral", "ttl": self.ttl}

    def _should_apply(self, request: ModelRequest) -> bool:
        if not _prompt_caching_enabled():
            return False
        if not _is_openrouter_anthropic(request.model):
            return False
        sysmsg = getattr(request, "system_message", None)
        if sysmsg is None:
            return False
        content = sysmsg.content
        size = len(content) if isinstance(content, str) else sum(
            len(b.get("text", "")) if isinstance(b, dict) else len(str(b)) for b in content
        )
        return size >= _MIN_CACHEABLE_CHARS

    def _apply(self, request: ModelRequest) -> ModelRequest:
        cc = self._cache_control
        tagged = _tag_last_block(request.system_message, cc)
        if tagged is request.system_message:
            return request

        if not self._logged:
            logger.info(
                "prompt_caching: cache_control(ttl=%s) on system prefix for %s",
                self.ttl,
                getattr(request.model, "model", "?"),
            )
            self._logged = True

        return request.override(system_message=tagged)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        if not self._should_apply(request):
            return handler(request)
        return handler(self._apply(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        if not self._should_apply(request):
            return await handler(request)
        return await handler(self._apply(request))


class CachingMemoryMiddleware(MemoryMiddleware):
    """deepagents' MemoryMiddleware, with its cache breakpoint un-gated for OpenRouter.

    THE SECOND BREAKPOINT, AND WHY IT IS NOT OPTIONAL HERE
    -------------------------------------------------------
    Upstream already intends a TWO-breakpoint layout, and says why
    (MemoryMiddleware.__init__ docstring on `add_cache_control`): a breakpoint on
    the static system prompt, plus a second at the memory-block boundary, because
    "memory content would otherwise shift after every update and invalidate the
    prefix cache". deepagents even passes `add_cache_control=True` unconditionally
    at graph.py:865. The layout is already wired — it simply never fires for IRIS,
    because the breakpoint sits behind the same dead
    `isinstance(request.model, ChatAnthropic)` test described in the module
    docstring, and IRIS's Claude arrives as ChatOpenRouter.

    Position is the whole reason this subclass exists rather than a second copy of
    OpenRouterPromptCachingMiddleware. Read the documented stack order at
    deepagents/graph.py:362-390: user middleware is inserted BEFORE the
    profile / prompt-caching / memory tail, and for `wrap_model_call` the earlier
    entry is the OUTER one. So the stock MemoryMiddleware modifies the system
    message AFTER any user middleware — it appends ~7.6k tokens of IRIS.md +
    agent.md + security.md + MEMORY_SYSTEM_PROMPT past wherever a user-level
    breakpoint was placed, leaving that text outside the cached prefix. Tagging
    from inside memory injection is the only way to land a breakpoint at the true
    end of the prefix.

    USAGE — this replaces the `memory=` kwarg, it does not supplement it
    ------------------------------------------------------------------
    Pass an instance in `middleware=[...]` and do NOT also pass `memory=[...]` to
    create_deep_agent, or memory is injected twice and the prompt carries two
    copies of every file. Because this sits at user position, nothing downstream
    appends to the system message afterwards (the tail's stock caching middleware
    no-ops on ChatOpenRouter, and HumanInTheLoopMiddleware does not touch the
    system message), so the tag really is final.

    Everything else — loading sources through the backend, `memory_contents`
    state, the `{agent_memory}` fragment — is inherited untouched: this override
    calls super() first and only adds the tag its parent skipped.
    """

    def modify_request(self, request: ModelRequest) -> ModelRequest:
        request = super().modify_request(request)
        if not _prompt_caching_enabled() or not _is_openrouter_anthropic(request.model):
            return request
        tagged = _tag_last_block(request.system_message, {"type": "ephemeral", "ttl": _cache_ttl()})
        if tagged is request.system_message:
            return request
        return request.override(system_message=tagged)
