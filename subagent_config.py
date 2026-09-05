"""subagent_config.py — Native Subagent Configuration Registry for IRIS.

Configures all specialized subagents (Aurther, Maya, Sienna, Tavia, Grace) with
native Python / LangChain tools, models, and system prompts.

Tool counts (verified against the exported *_TOOLS lists):
  - Aurther (Attio CRM): 25 tools (People, Companies, Lists, Entries, Notes, Tasks, Comments, Interactions, Members)
  - Maya (Jira): 29 tools
  - Sienna (Slack): 30 tools
  - Tavia (Tavily): 5 search/extract & caching + 2 datetime tools + 2 Exa tools = 9 tools
  - Grace (Google Workspace): 45 tools (Gmail 6, Calendar 7, Forms 9, Sheets 7, Drive 14, Docs 2)
  System total: 25 + 29 + 30 + 7 + 45 = 136 domain tools.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field

from datetime_tools import date_time_tools
from loadenv import (
    attio_subagent_model,
    google_workspace_subagent_model,
    jira_subagent_model,
    slack_subagent_model,
    tavily_subagent_model,
)
from PROMPTS import (
    ATTIO_SUBAGENT_DESCRIPTION,
    ATTIO_SUBAGENT_NAME,
    ATTIO_SUBAGENT_PROMPT,
    GOOGLE_WORKSPACE_SUBAGENT_DESCRIPTION,
    GOOGLE_WORKSPACE_SUBAGENT_NAME,
    GOOGLE_WORKSPACE_SUBAGENT_PROMPT,
    JIRA_SUBAGENT_DESCRIPTION,
    JIRA_SUBAGENT_NAME,
    JIRA_SUBAGENT_PROMPT,
    SLACK_SUBAGENT_DESCRIPTION,
    SLACK_SUBAGENT_NAME,
    SLACK_SUBAGENT_PROMPT,
    TAVILY_SUBAGENT_DESCRIPTION,
    TAVILY_SUBAGENT_NAME,
    TAVILY_SUBAGENT_PROMPT,
)
from attio_crm_tools import ATTIO_TOOLS
from jira_tools import JIRA_TOOLS
from slack_tools import SLACK_TOOLS
from web_search import TAVILY_TOOLS

# Defensive middleware attached to every subagent (see the post-build loop below).
from langchain.agents.middleware import ModelRetryMiddleware
from loop_breaker import ToolCallLoopBreakerMiddleware
from prompt_caching import OpenRouterPromptCachingMiddleware
from reasoning_trim import ReasoningTrimMiddleware
from resilience import is_retryable_model_error, raise_if_control_flow
from resume_context import ResumeContextMiddleware
from tool_call_repair import MalformedToolCallRepairMiddleware

# ── Google Workspace tool suites (Grace) ─────────────────────────────────────
from gmail_tools import email_tools
from google_calendar_tools import CALENDAR_TOOLS
from google_form_tools import FORM_TOOLS
from google_sheets_tools import SHEETS_TOOLS
from google_drive_tools import DRIVE_TOOLS
from google_docs_tools import DOCS_TOOLS

# Combined Google Workspace tool list for Grace (45 tools: Gmail, Calendar, Forms, Sheets, Drive, Docs)
GOOGLE_WORKSPACE_TOOLS = (
    email_tools          # 6: send_research_email, create_gmail_draft, read_inbox,
                         #    search_emails, get_sent_email_log, schedule_research_email
    + CALENDAR_TOOLS     # 7: create_calendar_event, list_calendar_events, ...
    + FORM_TOOLS         # 9: create_google_form, get_google_form_details, ...
    + SHEETS_TOOLS       # 7: create_google_spreadsheet, read_sheet_values, update, append, etc.
    + DRIVE_TOOLS        # 14: search_drive_files, upload_file_to_drive, ...
    + DOCS_TOOLS         # 2: create_google_doc, read_google_doc
)

logger = logging.getLogger(__name__)


# ── Completion contract (Q2) — machine-checkable subagent result ─────────────
# Master switch for the structured completion contract. When True, every
# subagent below is compiled with `response_format=SubagentResult`, so the
# deepagents `task` tool returns JSON ({status, summary, details, remaining})
# to IRIS instead of free text (deepagents serialises `structured_response`
# via model_dump_json). IRIS then keys delegation-completion off `status` — not
# off prose — so a PARTIAL/empty result can no longer be mistaken for "done"
# and redispatched. This is the completion half of the D-01/E-21 fix; the
# SubagentLoopBreakerMiddleware is the structural half.
#
# Flip to True to re-enable the machine-checkable SubagentResult contract across
# ALL subagents in one line. Requires a tool-calling model (both the ChatNVIDIA
# and ChatGoogleGenerativeAI subagent models qualify).
#
# Currently False (loop/JSON fix): forcing structured output on the smaller
# Nemotron subagents compiles a synthetic ToolStrategy tool that the model MUST
# call to return — an extra constraint layered on top of enable_thinking that was
# a contributor to the malformed-output / looping failure. The subagent prompts
# already emit an explicit STATUS/SUMMARY/ARTIFACTS block that IRIS's delegation
# rules parse, so plain-text results are fully supported without the schema.
USE_STRUCTURED_COMPLETION = False


class SubagentResult(BaseModel):
    """The result every subagent hands back to IRIS through the `task` tool.

    Only `status` is mandatory; the text fields default to empty so a subagent
    is never forced to fabricate content just to satisfy the schema (which keeps
    validation failures — a new failure mode — vanishingly unlikely).
    """

    status: Literal["COMPLETED", "PARTIAL", "FAILED"] = Field(
        description=(
            "COMPLETED = the delegated task is fully done and needs no further "
            "dispatch. PARTIAL = real progress made but work remains. FAILED = "
            "could not complete. IRIS MUST key completion off this field."
        )
    )
    summary: str = Field(
        default="",
        description="One or two plain-language sentences on what was accomplished, for the user.",
    )
    details: str = Field(
        default="",
        description="Concrete artifacts produced: record IDs, URLs, keys, counts. Empty if none.",
    )
    remaining: str = Field(
        default="",
        description="For PARTIAL/FAILED only: what is left to do or what blocked completion. Empty when COMPLETED.",
    )


# ── Subagent configuration list ───────────────────────────────────────────────
subagents: List[Dict[str, Any]] = [
    {
        "name": ATTIO_SUBAGENT_NAME,
        "description": ATTIO_SUBAGENT_DESCRIPTION,
        "tools": ATTIO_TOOLS,                         # 25 Attio CRM tools
        "model": attio_subagent_model,
        "system_prompt": ATTIO_SUBAGENT_PROMPT,
    },
    {
        "name": JIRA_SUBAGENT_NAME,
        "description": JIRA_SUBAGENT_DESCRIPTION,
        "tools": JIRA_TOOLS,                         # 29 Jira tools
        "model": jira_subagent_model,
        "system_prompt": JIRA_SUBAGENT_PROMPT,
    },
    {
        "name": SLACK_SUBAGENT_NAME,
        "description": SLACK_SUBAGENT_DESCRIPTION,
        "tools": SLACK_TOOLS,                         # 30 Slack tools
        "model": slack_subagent_model,
        "system_prompt": SLACK_SUBAGENT_PROMPT,
    },
    {
        "name": TAVILY_SUBAGENT_NAME,
        "description": TAVILY_SUBAGENT_DESCRIPTION,
        "tools": TAVILY_TOOLS + date_time_tools,      # 5 search/extract/think/cache + 2 datetime
        "model": tavily_subagent_model,
        "system_prompt": TAVILY_SUBAGENT_PROMPT,
    },
    {
        "name": GOOGLE_WORKSPACE_SUBAGENT_NAME,
        "description": GOOGLE_WORKSPACE_SUBAGENT_DESCRIPTION,
        "tools": GOOGLE_WORKSPACE_TOOLS,              # 45 Google Workspace tools (Gmail 6, Calendar 7, Forms 9, Sheets 7, Drive 14, Docs 2)
        "model": google_workspace_subagent_model,
        "system_prompt": GOOGLE_WORKSPACE_SUBAGENT_PROMPT,
    },
]


# Attach the completion contract to every subagent (Q2). Done as a post-build
# loop rather than inline in each dict so USE_STRUCTURED_COMPLETION is a single,
# honest on/off switch — flipping it to False reverts all subagents to plain
# text with no other edits. setdefault() means a future per-subagent
# response_format override (if one is ever added inline) still wins.
if USE_STRUCTURED_COMPLETION:
    for _spec in subagents:
        _spec.setdefault("response_format", SubagentResult)


# ── Subagent model-failure formatter ─────────────────────────────────────────
# Returned as the subagent's final AIMessage after ModelRetryMiddleware exhausts
# its retries (see the middleware loop below), so it becomes the `task` result
# IRIS reads. Shaped to the plaintext STATUS/SUMMARY/ARTIFACTS/BLOCKERS/LEARNING
# block the delegation rules already parse (D-04/FC-7), so a transient model
# outage reads as an actionable FAILED subtask — never a blank result (so
# blank_recovery Hook A correctly does NOT fire) and never a silently-garbage
# "success". Defined here, NOT imported from IRIS.py — that would be circular
# (IRIS.py imports `subagents` from this module). Mirrors IRIS.py's format_error.
def format_subagent_error(exc: Exception) -> str:
    # A HITL interrupt bubbling up through the model call must not be turned into
    # a text result — ModelRetryMiddleware's _handle_failure would convert it into
    # a normal AIMessage and lose the pending approval. Re-raise control flow;
    # format only genuine model faults. Matches IRIS.py's format_error.
    raise_if_control_flow(exc)
    return (
        "STATUS: FAILED\n"
        "SUMMARY: The model endpoint was temporarily unavailable after multiple retries.\n"
        "ARTIFACTS: none\n"
        "BLOCKERS: transient model error (upstream). Retry this step with a materially-changed "
        "brief, or mark it blocked and advance.\n"
        "LEARNING: none"
    )


# Attach the shared defensive middleware to every subagent. Subagent middleware
# is ISOLATED — the orchestrator's middleware list does NOT propagate into a
# subagent's own create_agent (deepagents builds each with middleware=spec.get(
# "middleware", [])) — so the reasoning-strip, tool-loop, and model-retry guards
# must be declared here too, or Tavia et al. would run without them. Fresh
# instances per spec (the list literal re-evaluates each iteration); all three are
# effectively stateless here, but per-agent instances keep them safe if instance
# state is ever added.
#   • ReasoningTrimMiddleware — strips the Nemotron think-trace from the
#     subagent's own turns AND from its final answer, so the task result handed
#     back to IRIS is clean. Critical now that structured output is off: tag-style
#     reasoning would otherwise ride inline in the returned content and re-pollute
#     IRIS's context (the "raw JSON thoughts" symptom, via the subagent channel).
#   • ToolCallLoopBreakerMiddleware — caps identical (name+args) domain-tool
#     calls, e.g. Tavia re-running the same tavily_search (the websearch loop).
#   • ModelRetryMiddleware — closes the gap that let a transient upstream model
#     error (a bare Exception("[404] …") from ChatNVIDIA) crash a whole multi-step
#     run: subagents previously had NO model-retry, and the orchestrator's
#     ToolRetryMiddleware.retry_on doesn't match a bare Exception. Listed LAST so
#     it is the innermost wrap_model_call layer (retries just the model call,
#     mirroring the orchestrator's ordering at IRIS.py:211). retry_on is the shared
#     is_retryable_model_error predicate → 3 attempts for transient faults only,
#     first-attempt return for permanent ones. HITL safety is NOT provided by the
#     middleware (its retry loop catches a bare `except Exception` with no interrupt
#     exclusion — verified in the installed source); it comes from the predicate
#     declining GraphBubbleUp plus format_subagent_error re-raising it. On
#     exhaustion the callable on_failure returns a FAILED block as the subagent's
#     result — no exception escapes to kill the run.
#   • MalformedToolCallRepairMiddleware — recovers a tool call the NIM parser left
#     as raw JSON in `content` with `tool_calls` empty. Needed on subagents even
#     more than on the orchestrator: a subagent's whole job is calling tools.
#     Placed directly inside ReasoningTrimMiddleware, mirroring IRIS.py.
for _spec in subagents:
    _spec.setdefault("middleware", [
        ReasoningTrimMiddleware(),
        MalformedToolCallRepairMiddleware(),
        # A subagent can be the thing mid-delegation when a crash happens; the
        # parent's resumed=True config propagates into the subagent (deepagents
        # inherits parent config via ensure_config), so it gets the same one-time
        # "you resumed — don't repeat completed actions" directive. No-op otherwise.
        ResumeContextMiddleware(),
        ToolCallLoopBreakerMiddleware(),
        # Prompt caching, and ONLY for maya in practice. Subagent middleware is
        # isolated, so the orchestrator's caching layers do not reach here.
        # Measured per-call fixed prefixes: grace 15,169 / aurther 8,656 /
        # sienna 7,716 / maya 5,589 / tavia 5,013 tokens, resent on every call a
        # subagent makes.
        #
        # Only maya benefits: she is the one subagent on anthropic/claude-opus-5
        # via OpenRouter, and this middleware self-gates to exactly that shape.
        # For the four Nemotron subagents it is a no-op by design — NVIDIA NIM
        # exposes no prompt-cache control, so grace's 15,169-token prefix (the
        # largest in the system, 12,674 of it tool schemas) stays fully billed on
        # all six of her calls. That is a real, unsolved cost, recorded here so
        # this line is not read as covering it. Trimming Grace's 45 tool schemas,
        # or splitting her into narrower subagents, is what would actually move
        # that number.
        OpenRouterPromptCachingMiddleware(),
        # max_retries=2 for the same reason as the orchestrator (see IRIS.py): a
        # subagent's retries burn the PARENT's stream ceiling, so a 5-attempt budget
        # here can exhaust the whole window inside one task call. Each attempt
        # carries its own 120s transport deadline, so 3 attempts ≈ 360s.
        #
        # retry_on=is_retryable_model_error replaces the default (Exception,), which
        # retried permanent faults too. That mattered most here: a subagent whose
        # model name or key is wrong (grace on a dead Gemini ID, sienna on the
        # degraded lightning-30b) previously spent 3 doomed attempts — each
        # re-billing its full fixed prefix, up to grace's 15,169 tokens — before
        # returning the same FAILED block. Now a 4xx/bad-config failure returns on
        # attempt 1 and only 429/5xx/timeouts retry. The predicate also declines
        # LangGraph control-flow exceptions, matching format_subagent_error's
        # raise_if_control_flow so a mid-subagent HITL interrupt survives.
        ModelRetryMiddleware(
            max_retries=2,
            retry_on=is_retryable_model_error,
            on_failure=format_subagent_error,
        ),
    ])


def build_subagent_config() -> List[Dict[str, Any]]:
    """Return the subagents configuration list synchronously."""
    return subagents
