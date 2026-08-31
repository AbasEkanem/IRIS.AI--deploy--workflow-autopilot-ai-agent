"""
PROMPTS.py — Backward-Compatible Shim for IRIS System Prompts

ARCHITECTURE NOTE (2026-08-16):
    System prompts have been migrated to a hierarchical .md file structure
    under prompts/ for separation of concerns, authority layering, and
    structured experience persistence. This file is now a THIN SHIM that
    composes prompts at runtime via prompt_builder.py.

    Prompt files:
        prompts/iris/role.md                ← IRIS identity + subagent registry
        prompts/iris/execution-protocol.md  ← STEP 0-7, todo tracking
        prompts/iris/delegation-rules.md    ← task() rules, loop breaking
        prompts/agents/aurther.md           ← Attio CRM specialist rules
        prompts/agents/maya.md              ← Jira specialist rules
        prompts/agents/sienna.md            ← Slack specialist rules
        prompts/agents/tavia.md             ← Web research specialist rules
        prompts/agents/grace.md             ← Google Workspace specialist rules
        prompts/shared/security-boundaries.md ← S-01/S-02/S-03 injection +
            disclosure defenses. Composed LAST into the orchestrator prompt AND
            into every specialist prompt, so its CRITICAL FINAL INSTRUCTION
            recency anchor is the tail of all 6 composed prompts.

    There is no prompts/iris/domain-protocols.md. Forms chaining and the HITL
    gates it once described now live in execution-protocol.md (STEP 4) and
    delegation-rules.md (Must Always Hold → HITL gate).

    All imports from IRIS.py and subagent_config.py remain unchanged.

    SUBAGENT NAME CONVENTION:
        The `name` field for every subagent MUST be the lowercase persona name.
        These names are the exact strings used in task(subagent_type=<name>).
        They must match the valid_agents set in prompt_builder.py exactly:
            {"aurther", "maya", "sienna", "tavia", "grace"}

Usage (unchanged):
    from PROMPTS import (
        ORCHESTRATOR_NAME, ORCHESTRATOR_DESCRIPTION, ORCHESTRATOR_PROMPT,
        ATTIO_SUBAGENT_NAME, ATTIO_SUBAGENT_DESCRIPTION, ATTIO_SUBAGENT_PROMPT,
        JIRA_SUBAGENT_NAME, JIRA_SUBAGENT_DESCRIPTION, JIRA_SUBAGENT_PROMPT,
        SLACK_SUBAGENT_NAME, SLACK_SUBAGENT_DESCRIPTION, SLACK_SUBAGENT_PROMPT,
        TAVILY_SUBAGENT_NAME, TAVILY_SUBAGENT_DESCRIPTION, TAVILY_SUBAGENT_PROMPT,
        GOOGLE_WORKSPACE_SUBAGENT_NAME, GOOGLE_WORKSPACE_SUBAGENT_DESCRIPTION, GOOGLE_WORKSPACE_SUBAGENT_PROMPT
    )
"""

from __future__ import annotations

from prompt_builder import build_iris_prompt, build_subagent_prompt

# ==============================================================================
# 0. MAIN ORCHESTRATOR: IRIS
# ==============================================================================
ORCHESTRATOR_NAME = "iris"

ORCHESTRATOR_DESCRIPTION = (
    "Main multi-agent supervisor for enterprise productivity orchestration. "
    "Decomposes user intent, dispatches domain subtasks to specialist subagents "
    "(Aurther, Maya, Sienna, Tavia, Grace), synthesizes results, and enforces execution safety."
)

ORCHESTRATOR_PROMPT = build_iris_prompt()


# ==============================================================================
# 1. ATTIO CRM SUBAGENT: AURTHER
# ==============================================================================
# IMPORTANT: The name MUST be the lowercase persona name "aurther".
# This is the exact string used in task(subagent_type="aurther") and must match
# the valid_agents set in prompt_builder.py: {"aurther", "maya", "sienna", "tavia", "grace"}
ATTIO_SUBAGENT_NAME = "aurther"

ATTIO_SUBAGENT_DESCRIPTION = (
    "Attio CRM execution specialist. Use Aurther whenever the task "
    "requires reading, searching, creating, updating, or verifying "
    "companies, people, leads, lists, pipeline entries, notes, tasks, "
    "comments, or CRM relationships in Attio. Aurther is the ONLY "
    "specialist authorized to execute Attio operations."
)

ATTIO_SUBAGENT_PROMPT = build_subagent_prompt("aurther")


# ==============================================================================
# 2. JIRA SUBAGENT: MAYA
# ==============================================================================
# IMPORTANT: The name MUST be the lowercase persona name "maya".
JIRA_SUBAGENT_NAME = "maya"

JIRA_SUBAGENT_DESCRIPTION = (
    "Jira execution specialist. Use Maya whenever the task requires "
    "searching, reading, creating, updating, transitioning, linking, "
    "commenting on, or verifying Jira issues, projects, boards, "
    "users, sprints, or other Jira resources. Maya is the ONLY "
    "specialist authorized to execute Jira operations."
)

JIRA_SUBAGENT_PROMPT = build_subagent_prompt("maya")


# ==============================================================================
# 3. SLACK SUBAGENT: SIENNA
# ==============================================================================
# IMPORTANT: The name MUST be the lowercase persona name "sienna".
SLACK_SUBAGENT_NAME = "sienna"

SLACK_SUBAGENT_DESCRIPTION = (
    "Slack execution specialist. Use Sienna whenever the task requires "
    "searching channels or messages, posting messages, replying to "
    "threads, scheduling messages, managing channels, or verifying "
    "Slack operations. Sienna is the ONLY specialist authorized to "
    "execute Slack operations."
)

SLACK_SUBAGENT_PROMPT = build_subagent_prompt("sienna")


# ==============================================================================
# 4. TAVILY SUBAGENT: TAVIA
# ==============================================================================
# IMPORTANT: The name MUST be the lowercase persona name "tavia".
TAVILY_SUBAGENT_NAME = "tavia"

TAVILY_SUBAGENT_DESCRIPTION = (
    "Web research execution specialist. Use Tavia whenever the task "
    "requires live web search, current information retrieval, "
    "reading a specific URL the user supplied (\"check/open/follow this link\"), "
    "source verification, technical research, fact-checking, or "
    "evidence-based web research. Tavia is the ONLY specialist "
    "authorized to perform live web research, and the only one that can "
    "fetch a web page."
)

TAVILY_SUBAGENT_PROMPT = build_subagent_prompt("tavia")


# ==============================================================================
# 5. GOOGLE WORKSPACE SUBAGENT: GRACE
# ==============================================================================
# IMPORTANT: The name MUST be the lowercase persona name "grace".
GOOGLE_WORKSPACE_SUBAGENT_NAME = "grace"

GOOGLE_WORKSPACE_SUBAGENT_DESCRIPTION = (
    "Google Workspace execution specialist. Use Grace whenever the "
    "task requires Gmail, Google Calendar, Google Forms, Google Sheets, or Google Drive "
    "operations. Grace is the ONLY specialist authorized to execute "
    "Google Workspace operations."
)

GOOGLE_WORKSPACE_SUBAGENT_PROMPT = build_subagent_prompt("grace")
