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

# ── Collapse ChatNVIDIA's per-call UserWarnings to once per process ───────────
# Prod's orchestrator (nvidia/nemotron-3.5-lightning-30b-a3b) is NOT in
# langchain-nvidia-ai-endpoints' static CHAT_MODEL_TABLE — it is discovered live
# from the endpoint's /models list, where `Model.supports_tools` defaults to False
# (_statics.py:70). Two warnings therefore fire:
#
#   _common.py:250   "Found <model> in available_models, but type is unknown…"
#   chat_models.py:1043  "Model '<model>' is not known to support tools."
#
# Both are about the CLIENT'S LOOKUP TABLE, not the endpoint: bind_tools proceeds
# unchanged, and a 30-call probe (tmp/three_models_out.txt) measured lightning at
# 100% valid tool_calls, 0% raw-JSON, 0% empty. So they are noise — but
# `bind_tools` is re-invoked on EVERY model call by the middleware stack, so the
# default "once per unique (message, category, module, lineno)" filter does not
# hold: each new bound instance re-warns, and Railway's log was ~70% these two
# lines. That is the actual harm — it buries the records that matter
# (blank_recovery, plan_guard, durability) in a wall of duplicates.
#
# "once" keeps ONE copy of each so the information is not lost, and drops the
# repeats. Scoped to these two exact messages, so no other warning is affected.
import warnings as _warnings

for _pattern in (
    r"Model '.*' is not known to support tools",
    r"Found .* in available_models, but type is unknown",
):
    _warnings.filterwarnings("once", message=_pattern, category=UserWarning)

try:
    from langchain_openrouter import ChatOpenRouter          # pip install langchain-openrouter
except ImportError:
    ChatOpenRouter = None

try:
    from langchain_anthropic import ChatAnthropic           # pip install langchain-anthropic
except ImportError:
    ChatAnthropic = None

# ==============================================================================
# NEMOTRON SAMPLING & THINKING DEFAULTS (one place, dashboard-tunable)
# ==============================================================================
# These are not the obvious values. Both departures are deliberate and each fixes
# a distinct half of the live production symptom cluster (loops, empty tool calls,
# stalls, raw JSON instead of a tool call, long runs that never finish):
#
#  * enable_thinking DEFAULTS OFF. NVIDIA's own function-calling documentation
#    states tool calling is supported on the Nemotron family "with detailed
#    thinking off". IRIS previously ran thinking ON for 5 of its 6 agents — every
#    one of which calls tools — i.e. the documented-unsupported combination. The
#    hosted tool/reasoning parser then emits the call as raw JSON in `content`
#    with `tool_calls` left EMPTY, or emits nothing at all and the request never
#    returns (worst on the 550b Ultra deployment). Do NOT turn this back on to
#    "improve planning": reasoning that cannot produce a parseable tool call is
#    worth nothing in a tool-using agent, and the loops / stalls / raw-JSON
#    output are all the same bug wearing different hats.
#  * temperature 1.0 / top_p 0.95 are NVIDIA's RECOMMENDED values "for all
#    modes". IRIS previously ran 0.0 / 1.0. Greedy decoding on a reasoning MoE is
#    a known repetition-loop driver, so the old setting was an INDEPENDENT second
#    source of the looping symptom. Counter-intuitive for an orchestrator, but it
#    is the vendor's documented operating point.
#
# All three are read from the environment so the configuration can be retuned —
# or fully reverted — from the Railway dashboard with no code change and no
# redeploy. The exact revert to the old behaviour is:
#   NEMOTRON_ENABLE_THINKING=1  NEMOTRON_TEMPERATURE=0.0  NEMOTRON_TOP_P=1.0
#
# ── WHAT THE MEASUREMENT ACTUALLY FOUND (tmp/probe_nemotron.py, 2026-08-26) ────
# The two rationales above are the VENDOR'S, and a 40-call matrix over
# {ultra,super} x {thinking on,off} x {greedy,spec} did NOT reproduce either of
# them. Recorded here because the confident tone above would otherwise outlive the
# evidence against it:
#
#   * thinking ON tool-called perfectly — 100% valid `tool_calls` on super with
#     thinking on, at BOTH sampling settings. Not one raw-JSON, truncated, empty or
#     prose response in 40 calls. The documented "tool calling needs detailed
#     thinking off" incompatibility did not appear at all.
#   * greedy vs spec sampling showed no difference either. No loops, no repetition.
#   * the ONE real effect was the model: hosted ultra-550b failed 30% of
#     tool-carrying calls (6/20) and super-120b failed 0% (0/20). Every ultra
#     failure was a fast HTTP 500 (1.3-1.8s), not the documented hang.
#
# So thinking-off is kept for LATENCY, which the probe did measure (super p50
# 2.6-3.5s off vs 4.4-4.5s on; ultra p95 3.8s off vs 20.0s on) — not because the
# tool-calling incompatibility was confirmed here. Two honest caveats: the probe is
# a single-turn, one-tool, short-prompt test, while the orchestrator runs a large
# system prompt, 6+ tools and 30+ super-steps, so non-reproduction at minimal load
# is not proof the parser failures never happen under real load; and planning
# QUALITY — the actual user complaint — was not measured, so if planning degrades,
# NEMOTRON_ENABLE_THINKING=1 is a supported and now evidence-backed-as-safe move
# for tool calling. `chat_template_kwargs {"medium_effort": true}` is also accepted
# by this endpoint (6/6 ok) if a middle setting is ever wanted.
def _env_flag(name: str, default: str = "0") -> bool:
    """Parse a boolean env var. Anything not clearly truthy reads as False."""
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


NEMOTRON_ENABLE_THINKING = _env_flag("NEMOTRON_ENABLE_THINKING", "0")
NEMOTRON_TEMPERATURE = float(os.getenv("NEMOTRON_TEMPERATURE", "1.0"))
NEMOTRON_TOP_P = float(os.getenv("NEMOTRON_TOP_P", "0.95"))

# ==============================================================================
# ORCHESTRATOR CONFIGURATION PLACEHOLDERS FROM .ENV
# ==============================================================================
ORCHESTRATOR_NAME = os.getenv("ORCHESTRATOR_NAME", "iris")
ORCHESTRATOR_MODEL_NAME = os.getenv("ORCHESTRATOR_MODEL_NAME") or os.getenv("ORCHESTRATOR_MODEL", "")
ORCHESTRATOR_MODEL_API_KEY = os.getenv("ORCHESTRATOR_MODEL_API_KEY") or ""

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
    temperature: float | None = None,
    api_key: str | None = None,
    enable_thinking: bool | None = None,
    top_p: float | None = None,
):
    """Factory: create a LangChain chat model from a model name + optional API key.

    Provider dispatch (checked in order):
      1. nvidia/ or nemotron  → ChatNVIDIA  (NVIDIA NIM)
      2. bare claude-*        → ChatAnthropic  (ANTHROPIC_BASE_URL, direct or proxy)
      3. any namespaced ID    → ChatOpenRouter  (openrouter.ai; when a key is set)
         (openai/, google/, anthropic/claude-*, meta-llama/, …)
      4. gemini-* (no slash)  → ChatGoogleGenerativeAI  (direct Gemini API)

    The orchestrator reaches Claude through (3), not (2): its model ID is
    "anthropic/claude-opus-5", an OpenRouter ID. Only a BARE "claude-…" name takes
    the Anthropic branch. There is no Groq path — it was removed as unused.

    temperature / top_p / enable_thinking are TRI-STATE. Pass a value to force it
    for one agent; leave it None (the default) to inherit the deployment-wide
    setting — NEMOTRON_TEMPERATURE / NEMOTRON_TOP_P / NEMOTRON_ENABLE_THINKING on
    the NVIDIA path, or a conservative temperature of 0.0 on the other providers.
    None-means-inherit is what makes the sampling and thinking configuration
    retunable from the Railway dashboard with no redeploy; see the NEMOTRON_*
    block above for why the defaults are what they are.

    Other fixed choices:
    - enable_thinking toggles Nemotron reasoning (NO token budget is sent — the
      ultra-550b V2 runner 400s on thinking_token_budget; see create body)
    - 32,768 max completion tokens so a tool call can never be truncated
      mid-JSON (a missing final `}` is a documented Nemotron tool-call failure)
    - Reasoning traces are stripped downstream by ReasoningTrimMiddleware.
    """
    if not model_name:
        return None

    key = api_key or os.getenv("NVIDIA_API_KEY") or ""
    if key.startswith("nvapi-") or model_name.startswith("nvidia/") or "nemotron" in model_name:
        if ChatNVIDIA is None:
            raise ImportError("langchain_nvidia_ai_endpoints is required for NVIDIA models.")

        # Resolve the tri-state knobs. Done INSIDE this branch so the NEMOTRON_*
        # env vars can only ever affect the NVIDIA path — an Anthropic / OpenRouter /
        # Gemini fallback keeps its own conservative default further down.
        temp = NEMOTRON_TEMPERATURE if temperature is None else float(temperature)
        tp = NEMOTRON_TOP_P if top_p is None else float(top_p)
        # Resolve to a concrete bool BEFORE model_kwargs is built: invariant 2
        # below requires an explicit True/False in chat_template_kwargs, and a
        # bare None must never reach it.
        think = NEMOTRON_ENABLE_THINKING if enable_thinking is None else bool(enable_thinking)

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
            "chat_template_kwargs": {"enable_thinking": think},
        }

        model = ChatNVIDIA(
            model=model_name,
            api_key=key,
            temperature=temp,
            top_p=tp,
            # 32,768 tokens of output runway. Raised from 16,384 because a TRUNCATED
            # tool call is a documented Nemotron failure ("the model does not produce
            # a final `}`", HuggingFace NVFP4 discussion #8) and truncation is exactly
            # what a too-small completion ceiling causes. With thinking off the
            # reasoning budget is freed anyway, so the wider runway costs nothing on
            # the normal path and removes a whole class of malformed-call failures.
            max_completion_tokens=int(os.getenv("NVIDIA_MAX_COMPLETION_TOKENS", "32768")),
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

    # ── Non-NVIDIA providers ─────────────────────────────────────────────────
    # temperature=None means "inherit", but the NEMOTRON_* defaults are
    # NVIDIA-specific and were already resolved inside the branch above. Every
    # provider below gets the conservative 0.0 that this signature used to
    # hardcode, so retuning Nemotron sampling can never silently change how a
    # Anthropic / OpenRouter / Gemini fallback behaves.
    temperature = 0.0 if temperature is None else float(temperature)

    # ── Anthropic branch (claude-* models via a direct/proxy Anthropic API) ──────
    # Detected by a BARE "claude-*" model name (no "/" namespace) plus an Anthropic
    # credential. Routes through ANTHROPIC_BASE_URL, which may be api.anthropic.com
    # or a proxy.
    #
    # Namespaced Claude IDs deliberately do NOT land here: "anthropic/claude-opus-5"
    # is an OpenRouter ID and falls through to the OpenRouter branch below. That is
    # how the orchestrator currently reaches Claude.
    #
    # The key test excludes "sk-or-" because OpenRouter keys are also "sk-"-prefixed,
    # so a bare "sk-" check would forward an OpenRouter key to the Anthropic host.
    # An explicit api_key argument is only overridden by the env var when it is
    # clearly not an Anthropic credential.
    def _looks_anthropic(k: str) -> bool:
        return k.startswith("sk-") and not k.startswith("sk-or-")

    _anthropic_key = api_key if _looks_anthropic(api_key or "") else os.getenv("ANTHROPIC_API_KEY", "")
    _anthropic_base = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    if model_name.startswith("claude-") and _anthropic_key:
        if ChatAnthropic is None:
            raise ImportError(
                "langchain-anthropic is required for Claude models.\n"
                "Run: pip install -U langchain-anthropic"
            )
        logger.info("[loadenv] Routing '%s' through Anthropic proxy: %s", model_name, _anthropic_base)
        return ChatAnthropic(
            model=model_name,
            temperature=temperature,
            max_tokens=int(os.getenv("ANTHROPIC_MAX_TOKENS", "4096")),
            anthropic_api_key=_anthropic_key,
            anthropic_api_url=_anthropic_base,
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

# ==============================================================================
# PER-AGENT MODEL INSTANCES
# ==============================================================================
# Every instance below now INHERITS sampling + thinking from the NEMOTRON_* env
# block at the top of this file. That is deliberate: these six agents are all
# tool callers, and NVIDIA's own function-calling documentation says Nemotron
# tool calling is supported "with detailed thinking off". Passing an explicit
# per-agent `temperature=` / `enable_thinking=` here is what previously locked
# the whole deployment into greedy decoding with thinking ON — the exact
# configuration the vendor documents as incompatible with tool use, and the root
# cause of the raw-JSON / empty-tool-call / stall / loop cluster.
#
# Leave these calls parameter-free unless an agent genuinely needs to differ
# from the deployment (Sienna does — see below). Retuning is a Railway dashboard
# edit, not a code change.
orchestrator_model = create_model_instance(
    ORCHESTRATOR_MODEL_NAME,
    api_key=ORCHESTRATOR_MODEL_API_KEY,
)
attio_subagent_model = create_model_instance(
    ATTIO_SUBAGENT_MODEL_NAME,
    api_key=ATTIO_SUBAGENT_MODEL_API_KEY,
)
jira_subagent_model = create_model_instance(
    JIRA_SUBAGENT_MODEL_NAME,
    api_key=JIRA_SUBAGENT_MODEL_API_KEY,
)
slack_subagent_model = create_model_instance(
    SLACK_SUBAGENT_MODEL_NAME,
    api_key=SLACK_SUBAGENT_MODEL_API_KEY,
    # thinking OFF, explicitly and unconditionally: lightning-30b (Sienna's model)
    # leaks its reasoning as untagged prose into `content` when thinking is on,
    # which no stripper catches. Slack drafting/tool calls don't need extended
    # reasoning; the orchestrator plans. This stays hardcoded rather than
    # inheriting NEMOTRON_ENABLE_THINKING so that flipping that flag back to 1
    # (to experiment on the reasoning models) can never re-break Sienna.
    enable_thinking=False,
)
tavily_subagent_model = create_model_instance(
    TAVILY_SUBAGENT_MODEL_NAME,
    api_key=TAVILY_SUBAGENT_MODEL_API_KEY,
)
google_workspace_subagent_model = create_model_instance(
    GOOGLE_WORKSPACE_SUBAGENT_MODEL_NAME,
    api_key=GOOGLE_WORKSPACE_SUBAGENT_MODEL_API_KEY,
)

# ── No fallback model ─────────────────────────────────────────────────────────
# There was a `fallback_model` here, wired into ModelFallbackMiddleware in both
# IRIS.py and subagent_config.py. Removed deliberately.
#
# A fallback only earns its place if it fails DIFFERENTLY from the primary. That
# held while the orchestrator and Maya ran ultra-550b and the fallback was a
# different model; it stopped holding once those two moved onto super-120b, at
# which point the fallback was re-rolling the same request against the same
# endpoint. Pointing it at lightning-30b instead restored the difference but
# bought it with a much weaker model answering on the primary's behalf — silently,
# mid-run, with nothing in the response to say a degraded model produced it.
#
# What remains is ModelRetryMiddleware, which is the layer that actually matches
# the measured failure: the hosted endpoint returning a bare Exception("[500] …")
# on a fraction of tool-carrying calls. That is transient, so a plain retry of the
# same model is the correct response to it — a second model was never what fixed
# it. If a genuinely differently-failing fallback is wanted later, reinstate it
# with a model this deployment does not otherwise depend on, and make the
# degradation visible in the run.

