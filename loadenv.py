from __future__ import annotations
"""
loadenv.py - Environment & Subagent Loader for IRIS

Loads environment variables from .env and exposes names, model names, model API keys,
and model instances for the orchestrator and all subagents.
"""

import os
import logging
from typing import Any

logger = logging.getLogger(__name__)

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Model Provider Imports
from langchain_google_genai import ChatGoogleGenerativeAI

try:
    from langchain_nvidia_ai_endpoints import ChatNVIDIA
except ImportError:
    ChatNVIDIA = None

try:
    from langchain_openrouter import ChatOpenRouter          # pip install langchain-openrouter
except ImportError:
    ChatOpenRouter = None

try:
    from langchain_groq import ChatGroq                     # pip install langchain-groq
except ImportError:
    ChatGroq = None

# ==============================================================================
# ORCHESTRATOR CONFIGURATION PLACEHOLDERS FROM .ENV
# ==============================================================================
ORCHESTRATOR_NAME = os.getenv("ORCHESTRATOR_NAME", "iris")
ORCHESTRATOR_MODEL_NAME = os.getenv("ORCHESTRATOR_MODEL_NAME") or os.getenv("ORCHESTRATOR_MODEL", "")
ORCHESTRATOR_MODEL_API_KEY = os.getenv("ORCHESTRATOR_MODEL_API_KEY") or os.getenv("", "")

# ==============================================================================
# SUBAGENT NAME, MODEL NAME & MODEL API KEY PLACEHOLDERS FROM .ENV
# ==============================================================================
ATTIO_SUBAGENT_NAME_ENV = os.getenv("ATTIO_SUBAGENT_NAME", "Aurther")
ATTIO_SUBAGENT_MODEL_NAME = os.getenv("ATTIO_SUBAGENT_MODEL_NAME") or os.getenv("ATTIO_SUBAGENT_MODEL", "")
ATTIO_SUBAGENT_MODEL_API_KEY = os.getenv("ATTIO_SUBAGENT_MODEL_API_KEY", "")

JIRA_SUBAGENT_NAME_ENV = os.getenv("JIRA_SUBAGENT_NAME", "maya")
JIRA_SUBAGENT_MODEL_NAME = os.getenv("JIRA_SUBAGENT_MODEL_NAME") or os.getenv("JIRA_SUBAGENT_MODEL", "")
JIRA_SUBAGENT_MODEL_API_KEY = os.getenv("JIRA_SUBAGENT_MODEL_API_KEY", "")

SLACK_SUBAGENT_NAME_ENV = os.getenv("SLACK_SUBAGENT_NAME", "sienna")
SLACK_SUBAGENT_MODEL_NAME = os.getenv("SLACK_SUBAGENT_MODEL_NAME") or os.getenv("SLACK_SUBAGENT_MODEL", "")
SLACK_SUBAGENT_MODEL_API_KEY = os.getenv("SLACK_SUBAGENT_MODEL_API_KEY", "")

TAVILY_SUBAGENT_NAME_ENV = os.getenv("TAVILY_SUBAGENT_NAME", "tavia")
TAVILY_SUBAGENT_MODEL_NAME = os.getenv("TAVILY_SUBAGENT_MODEL_NAME") or os.getenv("TAVILY_SUBAGENT_MODEL", "")
TAVILY_SUBAGENT_MODEL_API_KEY = os.getenv("TAVILY_SUBAGENT_MODEL_API_KEY", "")

GOOGLE_WORKSPACE_SUBAGENT_NAME_ENV = os.getenv("GOOGLE_WORKSPACE_SUBAGENT_NAME", "grace")
GOOGLE_WORKSPACE_SUBAGENT_MODEL_NAME = os.getenv("GOOGLE_WORKSPACE_SUBAGENT_MODEL_NAME") or os.getenv("GOOGLE_WORKSPACE_SUBAGENT_MODEL", "")
GOOGLE_WORKSPACE_SUBAGENT_MODEL_API_KEY = os.getenv("GOOGLE_WORKSPACE_SUBAGENT_MODEL_API_KEY")

# ==============================================================================
# MODEL INITIALIZATION FACTORY & INSTANCES
# ==============================================================================
def create_model_instance(
    model_name: str,
    temperature: float = 0.0,
    api_key: str | None = None,
    enable_thinking: bool = True,
):
    """Factory: create a LangChain chat model from a model name + optional API key.

    Provider dispatch (checked in order):
      1. nvidia/ or nemotron  → ChatNVIDIA  (NVIDIA NIM)
      2. openai/, google/,
         anthropic/, etc.     → ChatOpenRouter  (when OPENROUTER_API_KEY is set)
         (any namespaced ID)  → ChatOpenRouter  (via openrouter.ai)
      3. gemini-* (no slash)  → ChatGoogleGenerativeAI  (direct Gemini API)

    Configured for Profile B (Agent Orchestrator & Tool Calling Reasoning):
    - Strict low temperature (0.0 - 0.2) for deterministic logic and schema adherence
    - enable_thinking toggles Nemotron reasoning (NO token budget is sent — the
      ultra-550b V2 runner 400s on thinking_token_budget; see create body)
    - 16,384 max completion tokens to prevent thinking/tool-call truncation
    - Reasoning traces are stripped downstream by ReasoningTrimMiddleware.
    """
    if not model_name:
        return None

    key = api_key or os.getenv("NVIDIA_API_KEY") or ""
    if key.startswith("nvapi-") or model_name.startswith("nvidia/") or "nemotron" in model_name:
        if ChatNVIDIA is None:
            raise ImportError("langchain_nvidia_ai_endpoints is required for NVIDIA models.")
        
        # Nemotron thinking configuration — three hard-won invariants (each was a
        # live production failure; see tmp/ probes 2026-08-19):
        #  1. Do NOT send `reasoning_budget` (ChatNVIDIA maps it to
        #     `thinking_token_budget`). The ultra-550b *V2 model runner* rejects it
        #     with HTTP 400 — which 400s EVERY orchestrator/maya/grace call, so the
        #     ModelRetryMiddleware fallback ("Model temporarily unavailable") is all
        #     the user ever sees. `enable_thinking` alone still produces reasoning
        #     (delivered in additional_kwargs["reasoning_content"] on ultra/super and
        #     stripped downstream by ReasoningTrimMiddleware); think length is bounded
        #     by max_completion_tokens instead of an explicit budget.
        #  2. Pass `enable_thinking` EXPLICITLY — True and False alike. Omitting it is
        #     NOT the same as False: lightning-30b defaults thinking ON and dumps its
        #     (untagged) reasoning straight into `content`, which no reasoning-tag
        #     stripper can catch. Agents that must stay clean (the Slack drafter) pass
        #     an explicit False.
        #  3. model_kwargs must ALWAYS be a dict. This ChatNVIDIA build raises
        #     "argument of type 'NoneType' is not a container" at invoke time if it is
        #     None (which the old `extra_kwargs if extra_kwargs else None` produced
        #     whenever thinking was disabled).
        model_kwargs: dict[str, Any] = {
            "chat_template_kwargs": {"enable_thinking": bool(enable_thinking)},
        }

        model = ChatNVIDIA(
            model=model_name,
            api_key=key,
            temperature=temperature,
            top_p=1.0,
            # 16,384 tokens ensures thinking tokens never exhaust the output runway
            max_completion_tokens=int(os.getenv("NVIDIA_MAX_COMPLETION_TOKENS", "16384")),
            # Explicit, tunable client-side deadline so a stalled NIM becomes a
            # RETRYABLE exception rather than a slow inheritance of whatever the
            # transport happens to default to. The LangChain ChatNVIDIA wrapper
            # exposes no timeout of its own; passing one SETS the underlying
            # _NVIDIAClient deadline (which otherwise silently defaults to 60s) to a
            # documented value we control. ChatNVIDIA pops `timeout` and hands it to
            # that client:
            #   • async path (production/ainvoke): aiohttp
            #     ClientTimeout(connect=sock_connect=sock_read=t). sock_read is an
            #     INACTIVITY timeout — it fires only after `t`s of TOTAL silence (no
            #     bytes received), so a long but actively-streaming completion is NOT
            #     killed; only a genuine stall (no traffic for `t`s) trips it.
            #   • sync path: requests session.post(timeout=t).
            # A trip raises requests.Timeout / aiohttp ServerTimeoutError — types the
            # ToolRetryMiddleware + ModelRetryMiddleware already retry, and that
            # resilience.ainvoke_with_retry retries at the invoke boundary. So a
            # stall (or a dropped connection, which raises immediately) converts into
            # a retry that replays the SAME thread from its last durable checkpoint
            # ("restart exactly from where it left off") instead of wedging the
            # super-step. 120s > the 60s default: more tolerant of long completions,
            # still bounded. Tune without a code change via NVIDIA_REQUEST_TIMEOUT.
            timeout=float(os.getenv("NVIDIA_REQUEST_TIMEOUT", "120")),
            model_kwargs=model_kwargs,
        )
        return model

    # ── Groq branch ──────────────────────────────────────────────────────────
    # Detected by api_key prefix: Groq keys always start with "gsk_".
    # This check runs BEFORE OpenRouter so a Groq key is never accidentally
    # forwarded to openrouter.ai.
    # enable_thinking is ignored — Groq handles reasoning internally.
    _groq_key = api_key if (api_key or "").startswith("gsk_") else None
    if _groq_key:
        if ChatGroq is None:
            raise ImportError(
                "langchain-groq is required for Groq models.\n"
                "Run: pip install -U langchain-groq"
            )
        logger.info("[loadenv] Routing '%s' through Groq (gsk_ key detected).", model_name)
        return ChatGroq(
            model=model_name,
            temperature=temperature,
            groq_api_key=_groq_key,
            max_retries=2,
        )

    # ── OpenRouter branch ────────────────────────────────────────────────────
    # Any namespaced model ID (contains "/") that is NOT an NVIDIA model is
    # assumed to be an OpenRouter model (e.g. openai/gpt-4o, google/gemini-2.5-pro,
    # anthropic/claude-3.5-sonnet, meta-llama/llama-3.1-70b-instruct, etc.).
    # Key resolution: explicit api_key arg → OPENROUTER_API_KEY env var.
    # enable_thinking is IGNORED on this path (OpenRouter models handle reasoning
    # internally and don't need a provider-specific thinking flag).
    _or_key = api_key or os.getenv("OPENROUTER_API_KEY", "")
    if "/" in model_name and _or_key:
        if ChatOpenRouter is None:
            raise ImportError(
                "langchain-openrouter is required for OpenRouter models.\n"
                "Run: pip install -U langchain-openrouter"
            )
        logger.info("[loadenv] Routing '%s' through OpenRouter.", model_name)
        return ChatOpenRouter(
            model=model_name,
            temperature=temperature,
            max_tokens=int(os.getenv("OPENROUTER_MAX_TOKENS", "4096")),
            openrouter_api_key=_or_key,
        )

    # ── Direct Gemini fallback ───────────────────────────────────────────────
    key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    return ChatGoogleGenerativeAI(
        model=model_name,
        temperature=temperature,
        google_api_key=key,
        streaming=False,
        # Parity with the NVIDIA path: a client-side deadline so a stalled Gemini
        # call fails fast into the retry layers instead of hanging a super-step.
        timeout=float(os.getenv("GEMINI_REQUEST_TIMEOUT", "120")),
    )

# Individual model instances loaded per subagent & orchestrator (Profile B: Optimized Reasoning)
orchestrator_model = create_model_instance(
    ORCHESTRATOR_MODEL_NAME,
    temperature=0.0,
    api_key=ORCHESTRATOR_MODEL_API_KEY,
    enable_thinking=True,
)
attio_subagent_model = create_model_instance(
    ATTIO_SUBAGENT_MODEL_NAME,
    temperature=0.0,
    api_key=ATTIO_SUBAGENT_MODEL_API_KEY,
    enable_thinking=True,
)
jira_subagent_model = create_model_instance(
    JIRA_SUBAGENT_MODEL_NAME,
    temperature=0.0,
    api_key=JIRA_SUBAGENT_MODEL_API_KEY,
    enable_thinking=True,
)
slack_subagent_model = create_model_instance(
    SLACK_SUBAGENT_MODEL_NAME,
    temperature=0.0,
    api_key=SLACK_SUBAGENT_MODEL_API_KEY,
    # thinking OFF: lightning-30b (Sienna's model) leaks its reasoning as untagged
    # prose into `content` when thinking is on, which no stripper catches. Slack
    # drafting/tool calls don't need extended reasoning; the orchestrator plans.
    enable_thinking=False,
)
tavily_subagent_model = create_model_instance(
    TAVILY_SUBAGENT_MODEL_NAME,
    temperature=0.2,
    api_key=TAVILY_SUBAGENT_MODEL_API_KEY,
    enable_thinking=True,
)
google_workspace_subagent_model = create_model_instance(
    GOOGLE_WORKSPACE_SUBAGENT_MODEL_NAME,
    temperature=0.0,
    api_key=GOOGLE_WORKSPACE_SUBAGENT_MODEL_API_KEY,
    enable_thinking=True,
)
