"""web_search.py — Production-ready Tavily Search & Strategic Reflection Tools for IRIS & Tavia Subagent.

Features:
- Real-time web search with AI summaries + source citations via Tavily
- URL fetching via tavily_extract, which renders JavaScript (see its docstring)
- think_tool for strategic reflection between research steps and quality decision-making
- Connection retry logic with exponential backoff
- Anti-hallucination governor protection against empty/sparse results
- Outage bookkeeping that makes a provider rejection un-cacheable (see _provider_state)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from functools import lru_cache, wraps
from typing import Any, List

from dotenv import load_dotenv
from pathlib import Path
from langchain_core.tools import BaseTool, ToolException, tool
from langchain_tavily import TavilyExtract, TavilySearch

logger = logging.getLogger(__name__)

load_dotenv()

_GOVERNOR_WARNING = (
    "[SYSTEM_GOVERNOR_WARNING] Search returned no results. "
    "No data exists for this query. "
    "Do NOT fabricate, invent, or guess any information. "
    "You MUST state that no information was found and stop."
)

# A FAILED search and an EMPTY search are different facts and must not share a
# message. `_GOVERNOR_WARNING` asserts "no data exists for this query" — telling the
# model that when the API actually rejected the request makes IRIS state, with
# confidence, that nothing exists about (measured 2026-08-28) "OpenAI". The provider
# was returning `{"error": Exception("Error 432: ")}` — a plan/quota rejection — and
# every query in the suite came back as the governor warning, so the entire Web
# Research specialist looked like it was working and was answering from nothing.
#
# `_OUTAGE_MARKER` is the machine-checkable half of that fix. The prose below is for
# the model; the marker is for the prompt contract (Tavia keys `STATUS: BLOCKED` off
# it) and for `save_research_brief`, which refuses to cache anything produced while
# it is the live provider state. Never reword the marker without updating
# `prompts/agents/tavia.md` and `skills/web-research-protocol/SKILL.md`.
_OUTAGE_MARKER = "[TOOL_OUTAGE]"

_UNAVAILABLE = (
    _OUTAGE_MARKER + " ⚠️ Web search is currently UNAVAILABLE — the search provider "
    "rejected the request ({reason}). This is a tool outage, NOT a statement about the "
    "query: do not conclude that no information exists, and do not answer from memory as "
    "though you had searched. Report STATUS: BLOCKED, cite nothing, save no research "
    "brief, and tell the user web search is down — if the answer matters, ask them to "
    "retry later or supply the source themselves."
)


# ── Provider outage state (cache-poisoning guard) ────────────────────────────
# The 2026-08-30 incident was not one bad answer, it was a bad answer that STUCK: a
# fabricated brief got written to `tmp/<slug>.md` during a hard quota outage, and the
# cache-first protocol then replayed it verbatim every time the user pushed back
# ("that's not true, I just opened the page"). Prompt rules alone cannot close that —
# the model that ignores the outage warning is the same model asked to honour the
# don't-save rule.
#
# So the refusal lives in code. Every provider call stamps this dict; a save is
# rejected whenever the most recent stamp is an outage. Module-level (process-wide) is
# correct rather than per-request: the quota belongs to the API key, so an outage is a
# genuinely global fact, and the next successful call clears it on its own.
_provider_state: dict[str, Any] = {"ok_at": None, "outage_at": None, "reason": ""}


def _mark_provider_ok() -> None:
    _provider_state["ok_at"] = time.time()


def _mark_provider_outage(reason: str) -> None:
    _provider_state["outage_at"] = time.time()
    _provider_state["reason"] = reason


def _provider_is_down() -> bool:
    """True when the last observed provider call was a rejection.

    Both-None (no call attempted yet) is NOT down — a think-only brief with no search
    behind it is legitimate and must stay saveable.
    """
    outage_at = _provider_state["outage_at"]
    if outage_at is None:
        return False
    ok_at = _provider_state["ok_at"]
    return ok_at is None or ok_at < outage_at



def _get_api_key() -> str:
    """Retrieve Tavily API key from environment with case-insensitive fallback."""
    key = os.getenv("TAVILY_API_KEY") or os.getenv("tavily_api_key") or ""
    if not key:
        raise RuntimeError(
            "[web_search] TAVILY_API_KEY is not set. "
            "Please provide TAVILY_API_KEY or tavily_api_key in your .env file."
        )
    return key


def _error_of(result: Any) -> str | None:
    """Extract a provider error from a search result, or ``None`` if there is none.

    ``TavilySearch._arun`` does not raise on an HTTP rejection — it returns
    ``{"error": <Exception>}``. That dict has no ``results`` and no ``answer``, so
    ``_is_empty_result`` calls it empty and the caller used to answer with the
    anti-hallucination governor warning. Checked BEFORE emptiness for that reason.
    """
    if isinstance(result, dict):
        err = result.get("error")
        if err:
            text = str(err).strip()
            return text or type(err).__name__
    return None


def _is_empty_result(result: Any) -> bool:
    """Detect if a search result is empty, sparse, or meaningless."""
    if result is None:
        return True
    if isinstance(result, str):
        stripped = result.strip()
        if not stripped or stripped.lower() in ("none", "[]", "{}"):
            return True
    if isinstance(result, dict):
        results = result.get("results", result.get("answer", None))
        if not results:
            return True
        if isinstance(results, list) and len(results) == 0:
            return True
    if isinstance(result, list) and len(result) == 0:
        return True
    return False


def _with_retry(max_attempts: int = 2, backoff_seconds: float = 0.5):
    """Decorator to retry async tool execution on transient failures."""
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    result = await fn(*args, **kwargs)
                    if result is not None:
                        return result
                    last_exc = ToolException(f"{fn.__qualname__}: engine returned None")
                except ToolException as e:
                    last_exc = e
                except Exception as e:
                    last_exc = ToolException(f"{fn.__qualname__} failed: {e}")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(backoff_seconds * (attempt + 1))
                    logger.warning(
                        "[web_search] %s attempt %d/%d failed (%s), retrying...",
                        fn.__qualname__, attempt + 1, max_attempts, last_exc,
                    )
            assert last_exc is not None
            raise last_exc
        return wrapper
    return decorator


@lru_cache(maxsize=1)
def _tavily_engine() -> TavilySearch:
    """Lazy-initialize and cache the underlying TavilySearch engine."""
    api_key = _get_api_key()
    t = TavilySearch(
        tavily_api_key=api_key,
        max_results=5,
        search_depth="advanced",
        include_answer=True,
        include_raw_content=False,
        include_images=False,
        description=(
            "Search the web for current, real-time information. "
            "Use for breaking news, live data, recent events, and factual lookups. "
            "Returns an AI summary plus the top source URLs."
        ),
    )
    t.name = "tavily_search_engine"
    logger.debug("[web_search] Tavily engine initialised")
    return t


@tool
async def tavily_search(query: str) -> str:
    """Real-time web search via Tavily Search API.

    Best for: breaking news, live data, market research, technical documentation,
    and factual inquiries. Returns an AI-generated synthesis plus ranked source URLs.

    Args:
        query: The search query string (e.g. 'latest OpenAI announcements 2026').
    """
    engine = _tavily_engine()

    @_with_retry(max_attempts=2, backoff_seconds=0.5)
    async def _call():
        result = await engine._arun(query)
        # Provider rejection first: it looks "empty" but means something else.
        error = _error_of(result)
        if error:
            logger.error("[web_search] Tavily rejected the request: %s", error)
            _mark_provider_outage(error)
            return _UNAVAILABLE.format(reason=error)
        if _is_empty_result(result):
            logger.warning("[web_search] Tavily returned empty result — injecting governor warning.")
            # An empty result is still a WORKING provider — stamp OK so a legitimate
            # "nothing found" brief stays saveable.
            _mark_provider_ok()
            return _GOVERNOR_WARNING
        _mark_provider_ok()
        return result

    try:
        result = str(await _call())
        if not result or result.strip() in ("None", ""):
            logger.warning("[web_search] returned empty — injecting governor warning.")
            return _GOVERNOR_WARNING
        return result
    except ToolException as e:
        # Retries exhausted — a transport/API failure, NOT an empty result. Same
        # distinction as above: never tell the model "no data exists" because the
        # call did not complete.
        logger.error("[web_search] Tavily search failed: %s", e)
        _mark_provider_outage(str(e) or "request failed after retries")
        return _UNAVAILABLE.format(reason=str(e) or "request failed after retries")
    except Exception as e:
        logger.error("[web_search] Unexpected error: %s", e)
        _mark_provider_outage(f"{type(e).__name__}: {e}")
        return f"{_OUTAGE_MARKER} ⚠️ Search error: {e}"


# ── URL fetching ─────────────────────────────────────────────────────────────
# Added 2026-08-30. Until then IRIS had NO way to read a URL: the only web tool was
# `tavily_search(query: str)`, so "check this link and follow it" was unimplementable
# and the model answered as if it had visited the page. It also silently corrupted the
# URL it was given (`/vote/leaderboard` → `/voting/leaderboard`) — hence the explicit
# verbatim-URL rule in the docstring.
#
# extract_depth: "basic" is the default deliberately. It is the cheaper tier AND it is
# the one measured working on the JS-rendered target page (the Hack-AI-thon leaderboard,
# whose rows arrive via a browser-side Supabase query — raw HTML contains zero data, so
# a plain HTTP fetcher would have "confirmed" an empty page). "advanced" is tried once
# as a fallback only when basic comes back suspiciously thin, since it burns more credit
# per call and the quota is what broke in the first place.
_THIN_CONTENT_CHARS = 400


def _extracted_text(result: Any) -> str:
    """Flatten a TavilyExtract payload into cited page text, or '' if it has none."""
    if not isinstance(result, dict):
        return ""
    chunks: list[str] = []
    for item in result.get("results") or []:
        if not isinstance(item, dict):
            continue
        body = (item.get("raw_content") or "").strip()
        if not body:
            continue
        title = item.get("title") or "(untitled)"
        chunks.append(f"### [{title}]({item.get('url', '')})\n\n{body}")
    return "\n\n---\n\n".join(chunks)


@lru_cache(maxsize=2)
def _tavily_extractor(depth: str) -> TavilyExtract:
    """Lazy-initialize and cache one extractor per depth tier."""
    return TavilyExtract(tavily_api_key=_get_api_key(), extract_depth=depth)


@tool
async def tavily_extract(urls: List[str]) -> str:
    """Read the actual content of specific web pages, rendering JavaScript.

    Use this — NOT `tavily_search` — whenever the user supplies a URL or says
    "check/open/follow this link". `tavily_search` takes a query string and cannot
    visit a page; it will return search results ABOUT the link instead of its content.

    This renders client-side JavaScript, so it works on dashboards, leaderboards, and
    single-page apps whose data is loaded by the browser and is absent from the raw
    HTML. Returns the page text as markdown (tables preserved) with its title and URL.

    Args:
        urls: One or more page URLs to read. Pass each URL EXACTLY as given — never
            repair, shorten, or guess a path. If a URL 404s, report that; do not
            substitute a path you think is more likely.
    """
    if isinstance(urls, str):  # tolerate a bare string from a loose tool call
        urls = [urls]
    targets = [u.strip() for u in (urls or []) if isinstance(u, str) and u.strip()]
    if not targets:
        return "⚠️ No URL supplied. Pass at least one page URL to read."

    async def _attempt(depth: str) -> tuple[str, str | None]:
        """Return (text, error). Both empty/None means the page had no content."""
        extractor = _tavily_extractor(depth)
        try:
            result = await extractor.ainvoke({"urls": targets})
        except Exception as e:  # transport/client-side failure
            return "", f"{type(e).__name__}: {e}"
        error = _error_of(result)
        if error:
            return "", error
        return _extracted_text(result), None

    text, error = await _attempt("basic")
    # Thin (but successful) basic extraction — retry once at the pricier tier before
    # concluding the page is genuinely empty.
    if not error and len(text) < _THIN_CONTENT_CHARS:
        deep_text, deep_error = await _attempt("advanced")
        if not deep_error and len(deep_text) > len(text):
            text = deep_text

    if error:
        logger.error("[web_search] Tavily extract rejected the request: %s", error)
        _mark_provider_outage(error)
        return _UNAVAILABLE.format(reason=error)

    _mark_provider_ok()
    if not text:
        logger.warning("[web_search] extract returned no content for %s", targets)
        # A reachable page with no extractable text is a real finding, not an outage —
        # but it must never become "the data does not exist". Say which it is.
        return (
            "The page(s) were reached but contained no extractable text: "
            + ", ".join(targets)
            + ". Report this literally — the page may require a login, or its content "
            "may be behind an interaction. Do NOT infer what the page would have said."
        )

    logger.info("[web_search] extracted %d chars from %d URL(s)", len(text), len(targets))
    return text




@tool(description="Strategic reflection tool for research planning and multi-step reasoning")
def think_tool(reflection: str) -> str:
    """Tool for strategic reflection on research progress and decision-making.

    Use this tool after each search to analyze results and plan next steps systematically.
    This creates a deliberate pause in the research workflow for quality decision-making.

    When to use:
    - After receiving search results: What key information did I find?
    - Before deciding next steps: Do I have enough to answer comprehensively?
    - When assessing research gaps: What specific information am I still missing?
    - Before concluding research: Can I provide a complete answer now?

    Reflection should address:
    1. Analysis of current findings - What concrete information have I gathered?
    2. Gap assessment - What crucial information is still missing?
    3. Quality evaluation - Do I have sufficient evidence/examples for a good answer?
    4. Strategic decision - Should I continue searching or provide my answer?

    Args:
        reflection: Your detailed reflection on research progress, findings, gaps, and next steps.

    Returns:
        Confirmation that reflection was recorded for decision-making.
    """
    logger.info("[think_tool] Reflection: %s", reflection)
    return f"Reflection recorded: {reflection}"


_TMP_DIR = Path(__file__).parent / "tmp"
_TMP_DIR.mkdir(parents=True, exist_ok=True)

# How long a saved brief may be served as a cache HIT. Before this existed the cache
# was permanent, which is what made the 2026-08-30 fabrication survive: "re-verify the
# leaderboard" read back the same wrong brief and reported it as a fresh confirmation.
# Research about a live leaderboard, a share price, or "today's news" is stale within
# hours; 24h is the compromise between that and re-burning quota on a repeated ask.
_BRIEF_TTL_HOURS = float(os.getenv("IRIS_BRIEF_TTL_HOURS", "24"))


@tool
def save_research_brief(filename: str, content: str) -> str:
    """Save a research brief to a local markdown file in tmp/ for caching and downstream reuse.

    Do NOT call this when a search or extract returned a tool-outage notice — there is
    nothing verified to cache, and a brief written from an outage poisons every later
    lookup of the same topic. The call is refused in that state.

    Args:
        filename: Name of the file (e.g. 'ai_agent_frameworks_2026.md').
        content: Full structured research report markdown content.
    """
    # Hard guard, not advice. See `_provider_state`: the model that ignored the outage
    # warning cannot be trusted to honour a don't-save instruction about it either.
    if _provider_is_down():
        reason = _provider_state["reason"] or "provider rejected the request"
        logger.error("[web_search] REFUSED to save brief %r during provider outage (%s)", filename, reason)
        return (
            f"{_OUTAGE_MARKER} ⚠️ REFUSED to save this brief: the search provider is "
            f"currently failing ({reason}), so nothing in it was verified against a live "
            "source. Do not retry the save. Report STATUS: BLOCKED with no citations and "
            "tell the user web research is down."
        )
    if _OUTAGE_MARKER in (content or ""):
        logger.error("[web_search] REFUSED to save brief %r — content carries the outage marker", filename)
        return (
            f"{_OUTAGE_MARKER} ⚠️ REFUSED to save this brief: its content is a tool-outage "
            "notice, not research. Report STATUS: BLOCKED instead."
        )
    try:
        safe_name = Path(filename).name
        if not safe_name.endswith(".md"):
            safe_name += ".md"
        file_path = _TMP_DIR / safe_name
        file_path.write_text(content, encoding="utf-8")
        logger.info("[web_search] Saved research brief to %s", file_path)
        return f"Research brief saved successfully to {file_path.as_posix()}"
    except Exception as e:
        logger.error("[web_search] Failed to save research brief: %s", e)
        return f"Error saving research brief: {e}"


@tool
def read_research_brief(filename: str, force_refresh: bool = False) -> str:
    """Check and read an existing research brief from tmp/ for query caching.

    Args:
        filename: Name of the research brief file to check (e.g. 'ai_agent_frameworks_2026.md').
        force_refresh: Set True to bypass the cache and force a live search. Use this
            whenever the user disputes or contradicts an earlier answer ("that's not
            true", "I just checked it myself"), or asks to re-verify, re-check, or
            confirm something. A cached brief can only ever repeat the answer they are
            disputing.
    """
    try:
        safe_name = Path(filename).name
        if not safe_name.endswith(".md"):
            safe_name += ".md"
        file_path = _TMP_DIR / safe_name

        if force_refresh:
            logger.info("[web_search] cache BYPASSED for %s (force_refresh)", safe_name)
            return (
                f"CACHE_BYPASS: force_refresh was requested for '{safe_name}'. Ignore any "
                "cached brief and perform a live search or extract now."
            )

        if file_path.exists():
            age_hours = (time.time() - file_path.stat().st_mtime) / 3600.0
            if age_hours > _BRIEF_TTL_HOURS:
                logger.info(
                    "[web_search] cache STALE for %s (%.1fh > %.1fh TTL)",
                    safe_name, age_hours, _BRIEF_TTL_HOURS,
                )
                return (
                    f"CACHE_MISS: a brief for '{safe_name}' exists but is STALE "
                    f"({age_hours:.1f}h old, TTL {_BRIEF_TTL_HOURS:.0f}h). Do not use it. "
                    "Perform a live search or extract and overwrite it."
                )
            content = file_path.read_text(encoding="utf-8")
            logger.info("[web_search] Read cached research brief from %s", file_path)
            return (
                f"[CACHED_RESEARCH_BRIEF: {file_path.as_posix()} | age {age_hours:.1f}h]\n\n"
                f"{content}"
            )
        return f"CACHE_MISS: No cached research brief found for '{safe_name}'. Proceed with live web search."
    except Exception as e:
        logger.error("[web_search] Failed to read research brief: %s", e)
        return f"CACHE_MISS: Error reading brief: {e}"


# ── Export ────────────────────────────────────────────────────────────────────
TAVILY_TOOLS: list[BaseTool] = [
    tavily_search,
    tavily_extract,
    think_tool,
    save_research_brief,
    read_research_brief,
]



