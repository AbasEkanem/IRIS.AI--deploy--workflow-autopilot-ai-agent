---
name: task-decomposition
description: >
  Standard operating procedure for decomposing multi-step user intents, sequencing
  dependencies, autonomous specialist routing, and executing continuous task plans.
---

# Task Decomposition & Delegation — SOP & Guardrails

> **Executor:** IRIS Orchestrator | **Tools:** `write_todos`, `task`, `get_current_datetime`, `calculate_future_datetime`, `write_file`

---

# ⛔ HARD GUARDRAILS & DELEGATION MATRIX (P0)

| Violation / Failure | Cause | Binding Recovery Invariant |
|---|---|---|
| **Domain Tool Sneaking (FC-4)** | IRIS calling domain tools directly | **Pure Delegation:** IRIS holds 0 domain tools. Route 100% of domain work via `task()`. |
| **Unsanctioned Pausing (FC-1)** | Stopping between ordinary subtasks | **Continuous Execution:** Pause ONLY for the 6 sanctioned HITL approval actions. |
| **Missing Plan (FC-2)** | Delegating before `write_todos` | **Pre-Planning Gate:** Always initialize full checklist via `write_todos` before first `task()`. |
| **Delegation Loop (FC-8)** | Re-dispatching failed task unchanged | **Loop Breaker (E-14):** Diagnose → one material retry → SUCCESS or BLOCK. Never third attempt. |

---

# ⚡ SPECIALIST ROUTING MATRIX

| Domain / Intent | Specialist | `subagent_type` | Native Tool Modules |
|---|---|---|---|
| Attio CRM (People, Companies, Lists, Notes, Tasks, Comments) | **Aurther** | `"aurther"` | `attio_crm_tools.py` (25 tools) |
| Jira (Issues, JQL, Transitions, Sprints, Boards, Worklogs) | **Maya** | `"maya"` | `jira_tools.py` (29 tools) |
| Slack (Messages, Threads, Channels, Pins, Reactions, Files) | **Sienna** | `"sienna"` | `slack_tools.py` (30 tools) |
| Web Research (Live search, Intelligence briefs, Fact-checking) | **Tavia** | `"tavia"` | `web_search.py` + `datetime_tools.py` (6 tools) |
| Google Workspace (Gmail, Calendar, Forms, Sheets, Drive, Docs) | **Grace** | `"grace"` | 6 Google modules (45 tools) |

---

# 📊 STANDARD DECOMPOSITION WORKFLOW

1. **Parse & Ground:** Extract objective, dependencies, and temporal bounds (`get_current_datetime`).
2. **Intent Capture:** Display Intent Routing Log before any tool call.
3. **Initialize Plan:** Call `write_todos` with all steps in `pending` state before first delegation.
4. **Continuous Execution:** Mark `in_progress` → delegate via `task()` → verify returned artifact → mark `completed` / `blocked`.
5. **Persist Experience:** Append novel orchestration learnings to `agent.md` before final synthesis, using the append-safe `read_file` → `write_file` pattern (`E-34` in `/skills/memory-management/SKILL.md`) — a bare `write_file` overwrites the whole file.
6. **Finalize & Synthesize:** Call `write_todos` to confirm terminal states, then return executive synthesis.
