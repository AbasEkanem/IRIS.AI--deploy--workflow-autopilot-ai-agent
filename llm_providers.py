"""
llm_providers.py — LLM Provider Templates for IRIS
====================================================
Three ready-to-use LangChain provider setups:

  1. OpenRouter  → OpenAI models   (via ChatOpenRouter)
  2. Groq        → Groq models      (via ChatGroq)
  3. OpenRouter  → Gemini models    (via ChatOpenRouter)

Usage
-----
Import any factory from this module and call it, or use the
pre-built singleton instances at the bottom of the file.

  from llm_providers import groq_llm, openrouter_openai_llm, openrouter_gemini_llm

All keys are read from environment variables (.env via dotenv).
Set them once in .env — no hard-coding required.
"""

from __future__ import annotations

import os
import logging
from typing import Any

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Optional soft-imports (fail clearly at call time, not import time) ────────

try:
    from langchain_openrouter import ChatOpenRouter          # pip install langchain-openrouter
    _OPENROUTER_AVAILABLE = True
except ImportError:
    ChatOpenRouter = None                                    # type: ignore[assignment,misc]
    _OPENROUTER_AVAILABLE = False

try:
    from langchain_groq import ChatGroq                     # pip install langchain-groq
    _GROQ_AVAILABLE = True
except ImportError:
    ChatGroq = None                                         # type: ignore[assignment,misc]
    _GROQ_AVAILABLE = False


# ==============================================================================
# 1. OPENROUTER  →  OPENAI MODELS
# ==============================================================================
# OpenRouter exposes OpenAI models under the "openai/" namespace.
# Recommended models (set OPENROUTER_OPENAI_MODEL in .env to override):
#   • openai/gpt-4o                  — flagship, best reasoning
#   • openai/gpt-4o-mini             — fast & cheap
#   • openai/o3-mini                 — reasoning specialist
#   • openai/gpt-4-turbo             — large context (128k)
# API key → https://openrouter.ai/keys
# ==============================================================================

OPENROUTER_API_KEY       = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_OPENAI_MODEL  = os.getenv("OPENROUTER_OPENAI_MODEL", "openai/gpt-4o-mini")
OPENROUTER_GEMINI_MODEL  = os.getenv("OPENROUTER_GEMINI_MODEL", "google/gemini-3.5-flash-001")


def create_openrouter_openai_llm(
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    api_key: str | None = None,
    **kwargs: Any,
):
    """
    Factory: LangChain ChatOpenRouter pointed at an OpenAI model.

    Parameters
    ----------
    model       : OpenRouter model ID, e.g. "openai/gpt-4o".
                  Falls back to OPENROUTER_OPENAI_MODEL env var.
    temperature : Sampling temperature (0.0 = deterministic).
    max_tokens  : Maximum output tokens.
    api_key     : Override OPENROUTER_API_KEY env var.
    **kwargs    : Any extra ChatOpenRouter constructor args.

    Returns
    -------
    ChatOpenRouter instance ready for .invoke() / LCEL chains.
    """
    if not _OPENROUTER_AVAILABLE:
        raise ImportError(
            "langchain-openrouter is not installed.\n"
            "Run: pip install -U langchain-openrouter"
        )

    key = api_key or OPENROUTER_API_KEY
    if not key:
        raise ValueError(
            "OPENROUTER_API_KEY is not set. "
            "Add it to your .env file or pass api_key= explicitly."
        )

    resolved_model = model or OPENROUTER_OPENAI_MODEL
    logger.info("[OpenRouter/OpenAI] Initialising model: %s", resolved_model)

    return ChatOpenRouter(
        model=resolved_model,
        temperature=temperature,
        max_tokens=max_tokens,
        openrouter_api_key=key,
        **kwargs,
    )


# ==============================================================================
# 2. GROQ — ultra-fast inference (LPU hardware)
# ==============================================================================
# Groq is a hardware-accelerated inference platform — orders of magnitude faster
# than traditional GPU clouds. Ideal for real-time / low-latency applications.
#
# Recommended models (set GROQ_MODEL in .env to override):
#   • openai/gpt-oss-120b            — flagship OSS reasoning model on Groq
#   • openai/gpt-oss-20b             — fast & cheap general purpose
#   • groq/compound                  — multi-tool (web search + code exec)
#   • groq/compound-mini             — lighter compound variant
# API key → https://console.groq.com/keys
# ==============================================================================

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


def create_groq_llm(
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    max_retries: int = 2,
    api_key: str | None = None,
    **kwargs: Any,
):
    """
    Factory: LangChain ChatGroq for fast Groq-hosted inference.

    Parameters
    ----------
    model       : Groq model ID, e.g. "openai/gpt-oss-120b".
                  Falls back to GROQ_MODEL env var.
    temperature : Sampling temperature.
    max_tokens  : Max output tokens (None = model default).
    max_retries : LangChain-level retry count on transient failures.
    api_key     : Override GROQ_API_KEY env var.
    **kwargs    : Any extra ChatGroq constructor args.

    Returns
    -------
    ChatGroq instance ready for .invoke() / LCEL chains.
    """
    if not _GROQ_AVAILABLE:
        raise ImportError(
            "langchain-groq is not installed.\n"
            "Run: pip install -U langchain-groq"
        )

    key = api_key or GROQ_API_KEY
    if not key:
        raise ValueError(
            "GROQ_API_KEY is not set. "
            "Add it to your .env file or pass api_key= explicitly."
        )

    resolved_model = model or GROQ_MODEL
    logger.info("[Groq] Initialising model: %s", resolved_model)

    return ChatGroq(
        model=resolved_model,
        temperature=temperature,
        max_tokens=max_tokens,
        max_retries=max_retries,
        groq_api_key=key,
        **kwargs,
    )


# ==============================================================================
# 3. OPENROUTER  →  GOOGLE GEMINI MODELS
# ==============================================================================
# Gemini models are available via OpenRouter under the "google/" namespace.
# Recommended models (set OPENROUTER_GEMINI_MODEL in .env to override):
#   • google/gemini-2.0-flash-001    — fast, cheap, multimodal
#   • google/gemini-2.5-pro          — strongest reasoning
#   • google/gemini-2.5-flash        — balance of speed & quality
#   • google/gemini-2.0-flash-thinking-exp — extended reasoning (experimental)
# API key → https://openrouter.ai/keys  (same key as OpenAI template above)
# ==============================================================================


def create_openrouter_gemini_llm(
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    api_key: str | None = None,
    **kwargs: Any,
):
    """
    Factory: LangChain ChatOpenRouter pointed at a Google Gemini model.

    Parameters
    ----------
    model       : OpenRouter model ID, e.g. "google/gemini-2.5-pro".
                  Falls back to OPENROUTER_GEMINI_MODEL env var.
    temperature : Sampling temperature.
    max_tokens  : Maximum output tokens.
    api_key     : Override OPENROUTER_API_KEY env var.
    **kwargs    : Any extra ChatOpenRouter constructor args.

    Returns
    -------
    ChatOpenRouter instance ready for .invoke() / LCEL chains.
    """
    if not _OPENROUTER_AVAILABLE:
        raise ImportError(
            "langchain-openrouter is not installed.\n"
            "Run: pip install -U langchain-openrouter"
        )

    key = api_key or OPENROUTER_API_KEY
    if not key:
        raise ValueError(
            "OPENROUTER_API_KEY is not set. "
            "Add it to your .env file or pass api_key= explicitly."
        )

    resolved_model = model or OPENROUTER_GEMINI_MODEL
    logger.info("[OpenRouter/Gemini] Initialising model: %s", resolved_model)

    return ChatOpenRouter(
        model=resolved_model,
        temperature=temperature,
        max_tokens=max_tokens,
        openrouter_api_key=key,
        **kwargs,
    )


# ==============================================================================
# PRE-BUILT SINGLETON INSTANCES
# ==============================================================================
# These are lazily initialised — they return None (with a warning) if the
# required API key is missing, so importing this module never crashes.
#
# Use them directly:
#   from llm_providers import groq_llm
#   from llm_providers import openrouter_openai_llm
#   from llm_providers import openrouter_gemini_llm
# ==============================================================================

def _safe_create(factory, name: str):
    """Attempt to create a model instance; return None and log on failure."""
    try:
        return factory()
    except Exception as exc:
        logger.warning("[llm_providers] Could not initialise %s: %s", name, exc)
        return None


openrouter_openai_llm  = _safe_create(create_openrouter_openai_llm,  "OpenRouter/OpenAI")
groq_llm               = _safe_create(create_groq_llm,               "Groq")
openrouter_gemini_llm  = _safe_create(create_openrouter_gemini_llm,  "OpenRouter/Gemini")


# ==============================================================================
# QUICK-START TEST  (python llm_providers.py)
# ==============================================================================
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    def test_provider(llm, label: str):
        if llm is None:
            print(f"\n⚠️  [{label}] Skipped — instance is None (check your .env keys)")
            return
        print(f"\n🔍 Testing [{label}] ...")
        try:
            response = llm.invoke("Reply with exactly three words: 'It is working'")
            print(f"✅ [{label}] Response: {response.content}")
        except Exception as exc:
            print(f"❌ [{label}] Error: {exc}", file=sys.stderr)

    test_provider(openrouter_openai_llm,  "OpenRouter → OpenAI")
    test_provider(groq_llm,               "Groq")
    test_provider(openrouter_gemini_llm,  "OpenRouter → Gemini")

    # ── LCEL chain demo (Groq) ─────────────────────────────────────────────────
    print("\n--- LCEL Chain Example (Groq) ---")
    if groq_llm:
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.output_parsers import StrOutputParser

        prompt = ChatPromptTemplate.from_messages([
            ("system", "You are a concise data assistant."),
            ("human",  "{question}"),
        ])
        chain = prompt | groq_llm | StrOutputParser()
        result = chain.invoke({"question": "What is LangChain in one sentence?"})
        print(f"Chain result: {result}")
