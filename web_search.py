"""web_search.py — Production-ready Tavily Search & Strategic Reflection Tools for IRIS & Tavia Subagent.

Features:
- Real-time web search with AI summaries + source citations via Tavily
- think_tool for strategic reflection between research steps and quality decision-making
- Connection retry logic with exponential backoff
- Anti-hallucination governor protection against empty/sparse results
"""

from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache, wraps
from typing import Any, List, Optional

from dotenv import load_dotenv
from pathlib import Path
from langchain_core.tools import BaseTool, ToolException, tool
from langchain_tavily import TavilySearch

logger = logging.getLogger(__name__)

load_dotenv()

_GOVERNOR_WARNING = (
    "[SYSTEM_GOVERNOR_WARNING] Search returned no results. "
    "No data exists for this query. "
    "Do NOT fabricate, invent, or guess any information. "
    "You MUST state that no information was found and stop."
)


def _get_api_key() -> str:
    """Retrieve Tavily API key from environment with case-insensitive fallback."""
    key = os.getenv("TAVILY_API_KEY") or os.getenv("tavily_api_key") or ""
    if not key:
        raise RuntimeError(
            "[web_search] TAVILY_API_KEY is not set. "
            "Please provide TAVILY_API_KEY or tavily_api_key in your .env file."
        )
    return key


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
        if _is_empty_result(result):
            logger.warning("[web_search] Tavily returned empty result — injecting governor warning.")
            return _GOVERNOR_WARNING
        return result

    try:
        result = str(await _call())
        if not result or result.strip() in ("None", ""):
            logger.warning("[web_search] returned empty — injecting governor warning.")
            return _GOVERNOR_WARNING
        return result
    except ToolException as e:
        logger.error("[web_search] Tavily search failed: %s", e)
        return _GOVERNOR_WARNING
    except Exception as e:
        logger.error("[web_search] Unexpected error: %s", e)
        return f"⚠️ Search error: {e}"


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


@tool
def save_research_brief(filename: str, content: str) -> str:
    """Save a research brief to a local markdown file in tmp/ for caching and downstream reuse.

    Args:
        filename: Name of the file (e.g. 'ai_agent_frameworks_2026.md').
        content: Full structured research report markdown content.
    """
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
def read_research_brief(filename: str) -> str:
    """Check and read an existing research brief from tmp/ for query caching.

    Args:
        filename: Name of the research brief file to check (e.g. 'ai_agent_frameworks_2026.md').
    """
    try:
        safe_name = Path(filename).name
        if not safe_name.endswith(".md"):
            safe_name += ".md"
        file_path = _TMP_DIR / safe_name
        
        if file_path.exists():
            content = file_path.read_text(encoding="utf-8")
            logger.info("[web_search] Read cached research brief from %s", file_path)
            return f"[CACHED_RESEARCH_BRIEF: {file_path.as_posix()}]\n\n{content}"
        return f"CACHE_MISS: No cached research brief found for '{safe_name}'. Proceed with live web search."
    except Exception as e:
        logger.error("[web_search] Failed to read research brief: %s", e)
        return f"CACHE_MISS: Error reading brief: {e}"


# ── Export ────────────────────────────────────────────────────────────────────
TAVILY_TOOLS: list[BaseTool] = [tavily_search, think_tool, save_research_brief, read_research_brief]


