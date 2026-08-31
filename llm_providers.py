"""
llm_providers.py — LLM Provider Templates for IRIS
====================================================
Three ready-to-use LangChain provider setups:

  1. OpenRouter  → OpenAI models   (via ChatOpenRouter)
  2. OpenRouter  → Gemini models   (via ChatOpenRouter)
  3. Anthropic   → Claude models   (via ChatAnthropic + ANTHROPIC_BASE_URL)

Usage
-----
Import any factory from this module and call it, or use the
pre-built singleton instances at the bottom of the file.

  from llm_providers import openrouter_openai_llm, openrouter_gemini_llm, anthropic_llm

All keys are read from environment variables (.env via dotenv).
Set them once in .env — no hard-coding required.

NOTE: this module is a standalone template — nothing in IRIS imports it. The live
provider router the agents actually use is `create_model_instance` in loadenv.py.
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
    from langchain_anthropic import ChatAnthropic           # pip install langchain-anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    ChatAnthropic = None                                    # type: ignore[assignment,misc]
    _ANTHROPIC_AVAILABLE = False


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
OPENROUTER_GEMINI_MODEL  = os.getenv("OPENROUTER_GEMINI_MODEL", "google/gemini-2.0-flash-001")


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
# 2. OPENROUTER  →  GOOGLE GEMINI MODELS
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
#   from llm_providers import openrouter_openai_llm
#   from llm_providers import openrouter_gemini_llm
#   from llm_providers import anthropic_llm
# ==============================================================================

def _safe_create(factory, name: str):
    """Attempt to create a model instance; return None and log on failure."""
    try:
        return factory()
    except Exception as exc:
        logger.warning("[llm_providers] Could not initialise %s: %s", name, exc)
        return None


openrouter_openai_llm  = _safe_create(create_openrouter_openai_llm,  "OpenRouter/OpenAI")
openrouter_gemini_llm  = _safe_create(create_openrouter_gemini_llm,  "OpenRouter/Gemini")


# ==============================================================================
# 3. ANTHROPIC — Claude models via a direct or proxied Anthropic API
# ==============================================================================
# ANTHROPIC_BASE_URL selects the host: api.anthropic.com, or a proxy/reseller.
# Whatever it points at receives the full prompt and the API key, so treat a
# non-Anthropic host as a data-egress decision, not just a config value.
#
# Model IDs must match what the configured host serves. To reach Claude through
# OpenRouter instead, use create_openrouter_openai_llm with an "anthropic/claude-*"
# ID — that is how IRIS's orchestrator is wired (see loadenv.py).
# API key → https://console.anthropic.com/keys (or your proxy provider)
# ==============================================================================

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
ANTHROPIC_MODEL    = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")


def create_anthropic_llm(
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    api_key: str | None = None,
    base_url: str | None = None,
    **kwargs: Any,
):
    """
    Factory: LangChain ChatAnthropic pointed at Claude via custom proxy.

    Parameters
    ----------
    model       : Anthropic model ID, e.g. "claude-opus-5".
                  Falls back to ANTHROPIC_MODEL env var.
    temperature : Sampling temperature (0.0 = deterministic).
    max_tokens  : Maximum output tokens.
    api_key     : Override ANTHROPIC_API_KEY env var.
    base_url    : Override ANTHROPIC_BASE_URL env var (proxy endpoint).
    **kwargs    : Any extra ChatAnthropic constructor args.

    Returns
    -------
    ChatAnthropic instance ready for .invoke() / LCEL chains.
    """
    if not _ANTHROPIC_AVAILABLE:
        raise ImportError(
            "langchain-anthropic is not installed.\n"
            "Run: pip install -U langchain-anthropic"
        )

    key = api_key or ANTHROPIC_API_KEY
    if not key:
        raise ValueError(
            "ANTHROPIC_API_KEY is not set. "
            "Add it to your .env file or pass api_key= explicitly."
        )

    resolved_model    = model    or ANTHROPIC_MODEL
    resolved_base_url = base_url or ANTHROPIC_BASE_URL
    logger.info("[Anthropic] Initialising model: %s (base_url: %s)", resolved_model, resolved_base_url)

    return ChatAnthropic(
        model=resolved_model,
        temperature=temperature,
        max_tokens=max_tokens,
        anthropic_api_key=key,
        anthropic_api_url=resolved_base_url,
        **kwargs,
    )


anthropic_llm = _safe_create(create_anthropic_llm, "Anthropic/Claude")


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
    test_provider(openrouter_gemini_llm,  "OpenRouter → Gemini")
    test_provider(anthropic_llm,          "Anthropic → Claude")

    # ── LCEL chain demo (OpenRouter) ───────────────────────────────────────────
    print("\n--- LCEL Chain Example (OpenRouter → OpenAI) ---")
    if openrouter_openai_llm:
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.output_parsers import StrOutputParser

        prompt = ChatPromptTemplate.from_messages([
            ("system", "You are a concise data assistant."),
            ("human",  "{question}"),
        ])
        chain = prompt | openrouter_openai_llm | StrOutputParser()
        result = chain.invoke({"question": "What is LangChain in one sentence?"})
        print(f"Chain result: {result}")

