---
name: jira-ticket-lifecycle
description: >
  Standard operating procedure for Jira issue lifecycle, status transitions,
  JQL querying, issue linking, agile sprint management, and worklogs.
---

# Jira Issue, Agile & Workflow Lifecycle — SOP & Guardrails

> **Executor:** Maya | **Tools:** 29 native Jira tools in `jira_tools.py`

---

# ⛔ HARD GUARDRAILS & ERROR RECOVERY MATRIX (P0)

| Error / Edge Case | Cause | Binding Recovery Invariant |
|---|---|---|
| **Invalid Transition** | Transitioning without checking legal next states | **Pre-flight Check (E-02):** Always call `get_jira_transitions(issue_key)` before `transition_jira_issue`. |
| **`'str' object has no attribute 'get'`** | Indexing `.get("name")` on plain string `to` field | **Payload Shape Safety (E-18):** Check `isinstance(to_field, dict)` before sub-key access. |
| **Kanban Sprint Error** | Calling sprint tools on simple/Kanban board | **Kanban Fact:** Simple/Kanban boards do not support sprints. Use board/backlog issue queries instead. Do not retry as tool bug. |
| **Invalid Assignee** | Passing raw user email directly as accountId | **AccountId Resolution:** Use `search_jira_users` to resolve names/emails to valid `accountId` strings. |
| **Issue Deletion** | Irreversible operation | **HITL Required:** `delete_jira_issue` requires explicit human approval. |

---

# ⚡ CORE OPERATIONAL RULES

1. **Transition Check:** Query legal transitions first via `get_jira_transitions(issue_key)`.
2. **Issue Creation (E-16):** Always supply `project_key`. Use `parent_key` for subtasks.
3. **Issue Linking:** Use `link_jira_issues(inward, outward, link_type="Blocks")` for dependencies (`Blocks`, `Relates`, `Duplicate`).
4. **Agile Sprints:** Query active sprints via `list_jira_sprints(board_id, state="active")` before batching issues via `add_jira_issues_to_sprint`.
5. **Worklogs:** Log work time via `add_jira_worklog(issue_key, time_spent="2h 30m")`.

---

# 🛠️ NATIVE TOOL MODULE REFERENCE

Tool definitions live in `jira_tools.py` (29 tools):
- **Lifecycle (4):** `create_jira_issue`, `get_jira_issue`, `update_jira_issue`, `delete_jira_issue` (HITL)
- **Transitions (2):** `get_jira_transitions`, `transition_jira_issue` (HITL for Done/Closed)
- **Search & Users (5):** `search_jira_issues`, `list_my_jira_issues`, `get_jira_project_issues`, `search_jira_users`, `assign_jira_issue`
- **Comments, Links & Files (6):** `add_jira_comment`, `get_jira_comments`, `get_jira_issue_link_types`, `link_jira_issues`, `get_jira_issue_links`, `add_jira_attachment`
- **Projects, Boards & Sprints (8):** `list_jira_projects`, `get_jira_project_details`, `get_jira_project_components`, `get_jira_project_versions`, `list_jira_boards`, `list_jira_sprints`, `get_jira_sprint_issues`, `add_jira_issues_to_sprint`
- **Watchers & Worklogs (4):** `get_jira_watchers`, `add_jira_watcher`, `get_jira_worklogs`, `add_jira_worklog`

---

# 💡 ACTIVE GUARDRAIL REGISTRY

[GUARDRAIL E-18] Jira Cloud returns transition `to` field as string, not dict. Check isinstance(to_field, dict) before calling .get("name").
