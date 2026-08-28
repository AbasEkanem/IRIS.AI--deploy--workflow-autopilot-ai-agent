"""jira_tools.py — Comprehensive Native Jira Tools Suite for IRIS.

Production-grade suite of Jira REST API tools using `atlassian-python-api`.
Provides issue lifecycle management, JQL searching, transitions, workflow governance,
sprints, agile boards, links, comments, attachments, worklogs, and project metadata.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from langchain_core.tools import BaseTool, tool
from atlassian import Jira
from requests.exceptions import RequestException

from idempotency import idempotent

load_dotenv()

log = logging.getLogger(__name__)


# ── Custom Exceptions ─────────────────────────────────────────────────────────

class SubtaskUnsupportedError(Exception):
    pass


class SubtaskVerificationError(Exception):
    pass


class MissingConfigError(Exception):
    pass


# ── Client & Helpers ──────────────────────────────────────────────────────────

def _require_env(*names: str) -> None:
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise MissingConfigError(f"Missing required environment variable(s): {', '.join(missing)}")


@lru_cache(maxsize=1)
def _jira() -> Jira:
    _require_env("JIRA_URL", "JIRA_USERNAME", "JIRA_API_TOKEN")
    return Jira(
        url=os.getenv("JIRA_URL", ""),
        username=os.getenv("JIRA_USERNAME", ""),
        password=os.getenv("JIRA_API_TOKEN", ""),
        cloud=True,
        timeout=int(os.getenv("JIRA_REQUEST_TIMEOUT_S", "30")),
    )


def _default_project() -> str:
    """Return an explicitly configured default project, if one exists.

    A guessed project can produce misleading create-metadata errors and may create
    work in the wrong project.  Callers must supply ``project_key`` when no
    default has been configured.
    """
    return os.getenv("JIRA_DEFAULT_PROJECT", "").strip()


def _resolve_assignee_account_id(jira: Jira, assignee: str) -> str | None:
    """Resolve an email/name or accept a Jira Cloud account ID verbatim."""
    candidate = assignee.strip()
    if not candidate:
        return None
    # Jira Cloud account IDs commonly use the tenant-id:uuid form.  Searching
    # that value as a display name is unreliable, so preserve it directly.
    if re.fullmatch(r"[^\s:]+:[^\s:]+", candidate):
        return candidate
    users = jira.user_find_by_user_string(query=candidate, limit=1)
    if not users:
        return None
    return users[0].get("accountId")


def _jql_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _extract_text(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts: list[str] = []

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "text" and "text" in node:
                    parts.append(node["text"])
                for child in node.get("content", []) or []:
                    _walk(child)
            elif isinstance(node, list):
                for child in node:
                    _walk(child)

        _walk(value)
        return " ".join(parts).strip()
    return str(value)


def _get_issue_project_key(jira: Jira, issue_key: str) -> str:
    issue = jira.issue(issue_key, fields="project")
    return issue["fields"]["project"]["key"]


def _issue_type_values(meta: Any) -> list[dict]:
    """Normalise create-meta issue-type payloads across Jira Cloud API shapes.

    Jira Cloud returns issue types under the ``issueTypes`` key, while some
    older/self-hosted responses use ``values``. Fall back to a bare list too.
    """
    if isinstance(meta, dict):
        return meta.get("issueTypes") or meta.get("values") or []
    return meta or []


def _get_project_issue_types(jira: Jira, project_key: str) -> list[dict]:
    meta = jira.issue_createmeta_issuetypes(project_key)
    values = _issue_type_values(meta)
    return [{"id": t["id"], "name": t["name"], "subtask": bool(t.get("subtask"))} for t in values]


def _get_subtask_issue_types(jira: Jira, project_key: str) -> list[dict]:
    meta = jira.issue_createmeta_issuetypes(project_key)
    values = _issue_type_values(meta)
    return [{"id": t["id"], "name": t["name"], "subtask": bool(t.get("subtask"))} for t in values if t.get("subtask")]



def _parent_field_supported(jira: Jira, project_key: str, issue_type_id: str) -> bool:
    try:
        fields_meta = jira.issue_createmeta_fieldtypes(project_key, issue_type_id)
        values = fields_meta.get("values", []) if isinstance(fields_meta, dict) else []
        return "parent" in {f.get("fieldId", "") for f in values}
    except Exception:
        return False


def _pick_subtask_issue_type(subtask_types: list[dict], hint: Optional[str]) -> dict:
    if not subtask_types:
        raise SubtaskUnsupportedError("No subtask types available in project.")
    if not hint or hint.strip().lower() in ("", "subtask", "sub-task"):
        for t in subtask_types:
            if t["name"].lower() in ("sub-task", "subtask"):
                return t
        return subtask_types[0]
    hint_lc = hint.strip().lower()
    for t in subtask_types:
        if hint_lc == t["name"].lower():
            return t
    for t in subtask_types:
        if hint_lc in t["name"].lower() or t["name"].lower() in hint_lc:
            return t
    return subtask_types[0]


_JQL_FIELDS = {
    "project", "status", "assignee", "reporter", "priority", "issuetype",
    "key", "text", "summary", "description", "labels", "fixversion",
    "affectedversion", "created", "updated", "resolution", "resolved",
    "sprint", "component", "duedate", "comment", "parent", "watcher",
    "worklogauthor", "issuekey",
}


def _looks_like_jql(query: str) -> bool:
    q = query.strip()
    if re.search(r"\border\s+by\b", q, re.IGNORECASE):
        return True
    m = re.match(r"^(\w+)\s*(=|!=|~|!~|>=|<=|>|<|\bin\b|\bnot\s+in\b|\bis\b|\bwas\b)\s", q, re.IGNORECASE)
    return bool(m) and m.group(1).lower() in _JQL_FIELDS


def _bound_jql(jql: str) -> str:
    """Add a restriction clause to a JQL that has none.

    Jira Cloud rejects an *unbounded* search — a query that is nothing but an
    ``ORDER BY`` — with "Unbounded JQL queries are not allowed here." A model asked
    for "the latest issues" naturally writes exactly that (measured 2026-08-28:
    ``order by created DESC`` → hard error, no results, no hint at what to fix).

    So bound it: prefer the configured default project, and fall back to a very wide
    ``created`` window, which restricts the query formally without excluding anything
    a user would plausibly want. A JQL that already has a restriction is returned
    untouched.
    """
    q = (jql or "").strip()
    if not q:
        return q
    # Everything before ORDER BY is the restriction. If that is empty, there is none.
    head = re.split(r"\border\s+by\b", q, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    if head:
        return q
    project = _default_project()
    clause = f'project = "{_jql_escape(project)}"' if project else "created >= -3650d"
    return f"{clause} {q}"


def _build_search_jql(jira: Jira, query: str) -> str:
    """Build JQL from plain English, resolving person names to accountIds when detected."""
    q_lower = query.lower()

    assignee_match = re.search(r'assigned?\s+to\s+([a-z\s]+?)(?:\s+and|\s+or|$)', q_lower, re.IGNORECASE)
    reporter_match = re.search(r'reported?\s+by\s+([a-z\s]+?)(?:\s+and|\s+or|$)', q_lower, re.IGNORECASE)
    type_match = re.search(r'\b(bug|task|story|epic|sub-task|subtask)s?\b', q_lower, re.IGNORECASE)

    jql_parts = []

    if type_match:
        issue_type = type_match.group(1)
        if issue_type.lower() in ('sub-task', 'subtask'):
            jql_parts.append('issuetype = Sub-task')
        else:
            jql_parts.append(f'issuetype = {issue_type.capitalize()}')

    if assignee_match:
        name = assignee_match.group(1).strip()
        try:
            users = jira.user_find_by_user_string(query=name, limit=3, include_inactive_users=False)
            if users:
                account_id = users[0]['accountId']
                jql_parts.append(f'assignee = "{_jql_escape(account_id)}"')
            else:
                jql_parts.append(f'text ~ "{_jql_escape(query)}"')
        except Exception:
            jql_parts.append(f'text ~ "{_jql_escape(query)}"')
    elif reporter_match:
        name = reporter_match.group(1).strip()
        try:
            users = jira.user_find_by_user_string(query=name, limit=3, include_inactive_users=False)
            if users:
                account_id = users[0]['accountId']
                jql_parts.append(f'reporter = "{_jql_escape(account_id)}"')
            else:
                jql_parts.append(f'text ~ "{_jql_escape(query)}"')
        except Exception:
            jql_parts.append(f'text ~ "{_jql_escape(query)}"')

    if not jql_parts or (not assignee_match and not reporter_match and type_match):
        jql_parts.append(f'text ~ "{_jql_escape(query)}"')

    return ' AND '.join(jql_parts) + ' ORDER BY updated DESC'


# ── 1. Issue Lifecycle & Core Operations ──────────────────────────────────────

@tool
@idempotent("create_jira_issue", key_args=["summary", "description", "project_key", "parent_key"])
def create_jira_issue(
    summary: str,
    description: str,
    issue_type: Optional[str] = None,
    project_key: Optional[str] = None,
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    assignee_email: Optional[str] = None,
    labels: Optional[list[str]] = None,
    parent_key: Optional[str] = None,
) -> str:
    """Create a Jira issue. ``assignee`` accepts a Jira account ID, email, or name.

    ``assignee_email`` is retained for backwards compatibility; prefer
    ``assignee`` for all new calls.
    """
    try:
        jira = _jira()
        if parent_key:
            parent_project = _get_issue_project_key(jira, parent_key)
            project = project_key or parent_project
            if project_key and project_key.upper() != parent_project.upper():
                return f"⚠️ Can't create subtask under {parent_key}: different project."
            subtask_types = _get_subtask_issue_types(jira, project)
            if not subtask_types:
                raise SubtaskUnsupportedError(f"Project '{project}' has no Sub-task type.")
            chosen = _pick_subtask_issue_type(subtask_types, issue_type)
            if not _parent_field_supported(jira, project, chosen["id"]):
                raise SubtaskUnsupportedError(f"Issue type '{chosen['name']}' does not support parent field.")
            type_note = (
                f"\n\nOriginally requested type: {issue_type}"
                if issue_type and issue_type.strip().lower() not in chosen["name"].lower()
                else ""
            )
            fields = {
                "project": {"key": project},
                "summary": summary,
                "description": description + type_note,
                "issuetype": {"id": chosen["id"]},
                "parent": {"key": parent_key},
            }
            type_label = chosen["name"]
        else:
            effective_type = issue_type or "Task"
            project = project_key or _default_project()
            if not project:
                return "⚠️ project_key is required because JIRA_DEFAULT_PROJECT is not configured."
            available = _get_project_issue_types(jira, project)
            non_subtask = [t for t in available if not t["subtask"]]
            match = next((t for t in non_subtask if t["name"].lower() == effective_type.strip().lower()), None)
            if not match:
                names = ", ".join(t["name"] for t in non_subtask) or "none"
                return f"⚠️ Issue type '{effective_type}' not valid. Available: {names}."
            fields = {
                "project": {"key": project},
                "summary": summary,
                "description": description,
                "issuetype": {"id": match["id"]},
            }
            type_label = match["name"]

        if priority:
            fields["priority"] = {"name": priority}
        if labels:
            fields["labels"] = labels
        assignee_value = assignee or assignee_email
        if assignee_value:
            account_id = _resolve_assignee_account_id(jira, assignee_value)
            if not account_id:
                return f"⚠️ User '{assignee_value}' not found."
            fields["assignee"] = {"accountId": account_id}

        issue = jira.issue_create(fields=fields)
        key = issue.get("key", "?")
        url = f"{os.getenv('JIRA_URL', '')}/browse/{key}"
        if parent_key:
            created = jira.issue(key, fields="parent")
            if not (created.get("fields") or {}).get("parent"):
                raise SubtaskVerificationError(f"Parent link not attached for {key}.")
        parent_note = f" under {parent_key}" if parent_key else ""
        return f"✅ Created {key} ({type_label}{parent_note}): {url}"
    except (SubtaskUnsupportedError, SubtaskVerificationError, MissingConfigError) as e:
        return f"⚠️ {e}"
    except RequestException as e:
        return f"⚠️ Network error: {e}"
    except Exception as e:
        return f"⚠️ Couldn't create issue: {e}"


@tool
def get_jira_issue(issue_key: str) -> str:
    """Get full details, status, assignee, priority, and description of a Jira issue."""
    try:
        jira = _jira()
        issue = jira.issue(issue_key)
        f = issue["fields"]
        return (
            f"*{issue_key}* — {f.get('summary', 'N/A')}\n"
            f"Type: {f['issuetype']['name']} | Status: {f['status']['name']} | Priority: {(f.get('priority') or {}).get('name', 'None')}\n"
            f"Assignee: {(f.get('assignee') or {}).get('displayName', 'Unassigned')} | Reporter: {(f.get('reporter') or {}).get('displayName', 'Unknown')}\n"
            f"Description: {_extract_text(f.get('description'))[:600]}\n"
            f"Link: {os.getenv('JIRA_URL', '')}/browse/{issue_key}"
        )
    except Exception as e:
        return f"⚠️ Couldn't fetch {issue_key}: {e}"


@tool
def update_jira_issue(
    issue_key: str,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[list[str]] = None,
) -> str:
    """Update summary, description, priority, or labels on an existing Jira issue."""
    updates: dict[str, Any] = {}
    if summary:
        updates["summary"] = summary
    if description:
        updates["description"] = description
    if priority:
        updates["priority"] = {"name": priority}
    if labels is not None:
        updates["labels"] = labels
    if not updates:
        return "Nothing to update."
    try:
        _jira().update_issue_field(issue_key, updates)
        return f"✅ Updated {issue_key}"
    except Exception as e:
        return f"⚠️ Update failed: {e}"


@tool
def delete_jira_issue(issue_key: str, delete_subtasks: bool = True) -> str:
    """Delete a Jira issue (and optionally its subtasks). Requires HITL confirmation."""
    try:
        jira = _jira()
        jira.delete_issue(issue_key, delete_subtasks=delete_subtasks)
        return f"✅ Successfully deleted Jira issue {issue_key}."
    except Exception as e:
        return f"⚠️ Failed to delete issue {issue_key}: {e}"


# ── 2. Transitions & Workflow Governance ──────────────────────────────────────

@tool
def get_jira_transitions(issue_key: str) -> str:
    """Get all permissible status transitions and legal target states for a Jira issue."""
    try:
        jira = _jira()
        transitions = jira.get_issue_transitions(issue_key)
        if not transitions:
            return f"No available status transitions found for {issue_key}."
        lines = [f"Available status transitions for *{issue_key}*:"]
        for t in transitions:
            t_id = t.get("id")
            name = t.get("name")
            # `to` can be a plain string (atlassian-python-api) OR a dict
            # ({"name": ...}) depending on the Jira Cloud response shape.
            to_field = t.get("to", "")
            if isinstance(to_field, dict):
                to_status = to_field.get("name", "Unknown")
            else:
                to_status = to_field or "Unknown"
            lines.append(f"• ID `{t_id}`: *{name}* → (Target State: `{to_status}`)")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Failed to get transitions for {issue_key}: {e}"


@tool
def transition_jira_issue(issue_key: str, status: str) -> str:
    """Transition a Jira issue to a new status (e.g., 'In Progress', 'In Review', 'Done')."""
    try:
        _jira().set_issue_status(issue_key, status)
        return f"✅ Moved {issue_key} to *{status}*"
    except Exception as e:
        return f"⚠️ Transition failed for {issue_key}: {e}. (Tip: call get_jira_transitions first to see legal states)."


# ── 3. Search & JQL Querying ──────────────────────────────────────────────────

@tool
def search_jira_issues(query: str, max_results: int = 10) -> str:
    """Search Jira issues using plain English or JQL query."""
    try:
        jira = _jira()
        if _looks_like_jql(query):
            jql = _bound_jql(query)
        else:
            jql = _build_search_jql(jira, query)
        issues = jira.jql(jql, limit=max_results).get("issues", [])
        if not issues:
            return "No issues found."
        return "\n".join([
            f"• *{i['key']}* [{i['fields']['status']['name']}] {i['fields'].get('summary', '')} — _{(i['fields'].get('assignee') or {}).get('displayName', 'Unassigned')}_"
            for i in issues
        ])
    except Exception as e:
        return f"⚠️ Search error: {e}"


@tool
def list_my_jira_issues(max_results: int = 10) -> str:
    """List open unresolved Jira issues assigned to the configured user."""
    try:
        jira = _jira()
        jql = f'assignee = "{_jql_escape(os.getenv("JIRA_USERNAME", ""))}" AND resolution = Unresolved ORDER BY updated DESC'
        issues = jira.jql(jql, limit=max_results).get("issues", [])
        if not issues:
            return "No open issues assigned to you."
        lines = [f"Your {len(issues)} open issue(s):"]
        for i in issues:
            lines.append(f"• *{i['key']}* [{i['fields']['status']['name']}] {i['fields'].get('summary', '')}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Error: {e}"


@tool
def get_jira_project_issues(
    project_key: Optional[str] = None,
    status_filter: Optional[str] = None,
    max_results: int = 15,
) -> str:
    """List recent issues in a project with optional status filter."""
    try:
        jira = _jira()
        project = project_key or _default_project()
        jql = f'project = "{_jql_escape(project)}"'
        if status_filter:
            jql += f' AND status = "{_jql_escape(status_filter)}"'
        jql += " ORDER BY updated DESC"
        issues = jira.jql(jql, limit=max_results).get("issues", [])
        if not issues:
            return f"No issues in {project}."
        return "\n".join([f"Issues in *{project}*:"] + [
            f"• *{i['key']}* [{i['fields']['status']['name']}] {i['fields'].get('summary', '')}"
            for i in issues
        ])
    except Exception as e:
        return f"⚠️ Error: {e}"


# ── 4. Users & Assignment ─────────────────────────────────────────────────────

@tool
def search_jira_users(query: str, max_results: int = 10, include_inactive: bool = False) -> str:
    """Search for Jira users by email, name, or display name to obtain their accountId."""
    try:
        jira = _jira()
        users = jira.user_find_by_user_string(
            query=query,
            start=0,
            limit=max_results,
            include_inactive_users=include_inactive,
        )
        if not users:
            return f"No users found matching '{query}'."
        lines = [f"Users matching '{query}':"]
        for u in users:
            active = "✅ Active" if u.get("active") else "⚠️ Inactive"
            lines.append(f"• {u.get('displayName')} ({u.get('emailAddress', 'no-email')}) [`{u.get('accountId')}`] — {active}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ User search failed: {e}"


@tool
def assign_jira_issue(issue_key: str, assignee: str) -> str:
    """Assign a Jira issue to a user by email, name, or accountId (or 'none' to unassign)."""
    try:
        jira = _jira()
        if not assignee or str(assignee).strip().lower() in ("", "none", "unassign", "null"):
            jira.update_issue_field(issue_key, {"assignee": None})
            return f"✅ {issue_key} unassigned."
        users = jira.user_find_by_user_string(query=str(assignee).strip(), start=0, limit=5, include_inactive_users=False)
        if not users:
            return f"⚠️ No active user found for '{assignee}'. Try search_jira_users first."
        if len(users) > 1:
            suggestions = "\n".join([f"  • {u['displayName']} ({u.get('emailAddress', 'N/A')})" for u in users[:3]])
            return f"⚠️ Multiple users found for '{assignee}'. Be more specific:\n{suggestions}"
        jira.update_issue_field(issue_key, {"assignee": {"accountId": users[0]["accountId"]}})
        return f"✅ {issue_key} assigned to **{users[0]['displayName']}**"
    except Exception as e:
        return f"⚠️ Assign failed: {e}"


# ── 5. Comments & Discussions ─────────────────────────────────────────────────

@tool
@idempotent("add_jira_comment", key_args=["issue_key", "comment"])
def add_jira_comment(issue_key: str, comment: str) -> str:
    """Add a comment to a Jira issue."""
    try:
        _jira().issue_add_comment(issue_key, comment)
        return f"✅ Comment added to {issue_key}"
    except Exception as e:
        return f"⚠️ Comment failed: {e}"


_JIRA_COMMENT_CHAR_BUDGET = 4000


@tool
def get_jira_comments(issue_key: str, max_comments: int = 20) -> str:
    """Get recent comments on a Jira issue."""
    try:
        comments = _jira().issue_get_comments(issue_key).get("comments", [])
        recent = comments[-max_comments:]
        lines = [f"Last {len(recent)} comment(s) on *{issue_key}*:"]
        for c in recent:
            author = c.get("author", {}).get("displayName", "Unknown")
            body = _extract_text(c.get("body")) or ""
            if len(body) > _JIRA_COMMENT_CHAR_BUDGET:
                body = (
                    body[:_JIRA_COMMENT_CHAR_BUDGET]
                    + f" …[truncated: showing {_JIRA_COMMENT_CHAR_BUDGET} of {len(body)} chars]"
                )
            lines.append(f"_{author}_: {body}")
        return "\n".join(lines) if recent else f"No comments on {issue_key}."
    except Exception as e:
        return f"⚠️ Comments error: {e}"


# ── 6. Issue Linking & Relations ──────────────────────────────────────────────

@tool
def get_jira_issue_link_types() -> str:
    """List available issue link types (e.g. 'Blocks', 'Relates', 'Duplicate') in Jira."""
    try:
        jira = _jira()
        link_types = jira.get_issue_link_types()
        if not link_types:
            return "No issue link types configured."
        lines = ["Available Jira issue link types:"]
        for lt in link_types:
            name = lt.get("name")
            inward = lt.get("inward")
            outward = lt.get("outward")
            lines.append(f"• *{name}* (Outward: \"{outward}\" | Inward: \"{inward}\")")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Failed to get link types: {e}"


@tool
def link_jira_issues(
    inward_issue_key: str,
    outward_issue_key: str,
    link_type: str = "Relates",
    comment: Optional[str] = None,
) -> str:
    """Link two Jira issues together (e.g., inward 'is blocked by' outward, or 'Relates')."""
    try:
        jira = _jira()
        data = {
            "type": {"name": link_type},
            "inwardIssue": {"key": inward_issue_key},
            "outwardIssue": {"key": outward_issue_key},
        }
        if comment:
            data["comment"] = {"body": comment}
        jira.create_issue_link(data)
        return f"✅ Linked {inward_issue_key} and {outward_issue_key} with relation '{link_type}'."
    except Exception as e:
        return f"⚠️ Failed to link issues: {e}"


@tool
def get_jira_issue_links(issue_key: str) -> str:
    """Get all linked issues and relation dependencies for a Jira issue."""
    try:
        jira = _jira()
        issue = jira.issue(issue_key, fields="issuelinks")
        links = issue.get("fields", {}).get("issuelinks", [])
        if not links:
            return f"No linked issues found for {issue_key}."
        lines = [f"Links for *{issue_key}*:"]
        for lk in links:
            lt_name = lk.get("type", {}).get("name", "Link")
            if "outwardIssue" in lk:
                target = lk["outwardIssue"]
                rel = lk.get("type", {}).get("outward", "relates to")
                lines.append(f"• {rel} → *{target.get('key')}* ({target.get('fields', {}).get('summary', '')}) [{target.get('fields', {}).get('status', {}).get('name', '')}]")
            elif "inwardIssue" in lk:
                target = lk["inwardIssue"]
                rel = lk.get("type", {}).get("inward", "is related to")
                lines.append(f"• {rel} ← *{target.get('key')}* ({target.get('fields', {}).get('summary', '')}) [{target.get('fields', {}).get('status', {}).get('name', '')}]")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Failed to get issue links: {e}"


# ── 7. Attachments ────────────────────────────────────────────────────────────

@tool
def add_jira_attachment(issue_key: str, file_path: str) -> str:
    """Upload and attach a local file, document, or report to a Jira issue."""
    try:
        if not os.path.exists(file_path):
            return f"⚠️ Local file not found: {file_path}"
        jira = _jira()
        jira.add_attachment(issue_key=issue_key, filename=file_path)
        return f"✅ Attached '{os.path.basename(file_path)}' to {issue_key}."
    except Exception as e:
        return f"⚠️ Failed to upload attachment to {issue_key}: {e}"


# ── 8. Project Metadata, Components & Releases ────────────────────────────────

@tool
def list_jira_projects() -> str:
    """List all accessible Jira projects with their key, name, and project lead."""
    try:
        jira = _jira()
        projects = jira.projects()
        if not projects:
            return "No projects found."
        lines = ["Accessible Jira Projects:"]
        for p in projects:
            key = p.get("key")
            name = p.get("name")
            lead = p.get("lead", {}).get("displayName", "N/A")
            lines.append(f"• *{name}* (Key: `{key}`, Lead: {lead})")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Failed to list projects: {e}"


@tool
def get_jira_project_details(project_key: str) -> str:
    """Get full details of a Jira project including description, lead, and issue types."""
    try:
        jira = _jira()
        p = jira.project(project_key)
        issue_types = [t.get("name") for t in p.get("issueTypes", [])]
        return (
            f"*{p.get('name')}* (Key: `{p.get('key')}`)\n"
            f"Lead: {p.get('lead', {}).get('displayName', 'N/A')}\n"
            f"Description: {p.get('description') or 'No description'}\n"
            f"Issue Types: {', '.join(issue_types)}"
        )
    except Exception as e:
        return f"⚠️ Failed to get project details for {project_key}: {e}"


@tool
def get_jira_project_components(project_key: str) -> str:
    """List all components configured for a Jira project."""
    try:
        jira = _jira()
        components = jira.get_project_components(project_key)
        if not components:
            return f"No components found for project {project_key}."
        lines = [f"Components in *{project_key}*:"]
        for c in components:
            c_name = c.get("name")
            c_lead = c.get("lead", {}).get("displayName", "No lead")
            lines.append(f"• *{c_name}* (Lead: {c_lead})")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Failed to get components for {project_key}: {e}"


@tool
def get_jira_project_versions(project_key: str) -> str:
    """List all releases/versions for a Jira project."""
    try:
        jira = _jira()
        versions = jira.get_project_versions(project_key)
        if not versions:
            return f"No versions found for project {project_key}."
        lines = [f"Versions/Releases in *{project_key}*:"]
        for v in versions:
            name = v.get("name")
            released = "✅ Released" if v.get("released") else "⏳ Unreleased"
            archived = " [Archived]" if v.get("archived") else ""
            lines.append(f"• *{name}* — {released}{archived}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Failed to get versions for {project_key}: {e}"


# ── 9. Agile Boards & Sprints ─────────────────────────────────────────────────

@tool
def list_jira_boards(project_key_or_name: Optional[str] = None) -> str:
    """List agile Scrum and Kanban boards in Jira."""
    try:
        jira = _jira()
        res = jira.get_all_agile_boards(project_key=project_key_or_name)
        boards = res.get("values", []) if res else []
        if not boards:
            return "No agile boards found."
        lines = ["Agile Boards:"]
        for b in boards:
            b_id = b.get("id")
            name = b.get("name")
            b_type = b.get("type")
            lines.append(f"• Board ID `{b_id}`: *{name}* (Type: {b_type})")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Failed to list agile boards: {e}"


@tool
def list_jira_sprints(board_id: int, state: Optional[str] = None) -> str:
    """List sprints on an agile board. state can be 'active', 'future', or 'closed'."""
    try:
        jira = _jira()
        res = jira.get_all_sprints_from_board(board_id=board_id, state=state)
        sprints = res.get("values", []) if res else []
        if not sprints:
            return f"No sprints found on board {board_id}."
        lines = [f"Sprints on Board {board_id}:"]
        for s in sprints:
            s_id = s.get("id")
            name = s.get("name")
            s_state = s.get("state")
            start = s.get("startDate", "N/A")
            end = s.get("endDate", "N/A")
            lines.append(f"• Sprint ID `{s_id}`: *{name}* [{s_state.upper()}] (Start: {start} | End: {end})")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Failed to list sprints: {e}"


@tool
def get_jira_sprint_issues(sprint_id: int, max_results: int = 25) -> str:
    """Retrieve all issues assigned to a specific sprint."""
    try:
        jira = _jira()
        res = jira.get_sprint_issues(sprint_id=sprint_id, start=0, limit=max_results)
        issues = res.get("issues", []) if res else []
        if not issues:
            return f"No issues found in sprint {sprint_id}."
        lines = [f"Issues in Sprint {sprint_id}:"]
        for i in issues:
            key = i.get("key")
            summary = i.get("fields", {}).get("summary", "")
            status = i.get("fields", {}).get("status", {}).get("name", "")
            assignee = i.get("fields", {}).get("assignee", {}).get("displayName", "Unassigned")
            lines.append(f"• *{key}* [{status}] {summary} — _{assignee}_")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Failed to get sprint issues: {e}"


@tool
def add_jira_issues_to_sprint(sprint_id: int, issue_keys: list[str]) -> str:
    """Add or move a list of Jira issue keys into a sprint."""
    try:
        jira = _jira()
        jira.add_issues_to_sprint(sprint_id=sprint_id, issues=issue_keys)
        return f"✅ Moved issues {', '.join(issue_keys)} into sprint {sprint_id}."
    except Exception as e:
        return f"⚠️ Failed to add issues to sprint: {e}"


# ── 10. Watchers & Worklogs ───────────────────────────────────────────────────

@tool
def get_jira_watchers(issue_key: str) -> str:
    """Get all watchers on a Jira issue."""
    try:
        jira = _jira()
        res = jira.issue_get_watchers(issue_key)
        watchers = res.get("watchers", []) if isinstance(res, dict) else []
        if not watchers:
            return f"No watchers on {issue_key}."
        names = [w.get("displayName", "Unknown") for w in watchers]
        return f"Watchers on *{issue_key}* ({len(names)} total):\n" + "\n".join([f"• {n}" for n in names])
    except Exception as e:
        return f"⚠️ Failed to get watchers for {issue_key}: {e}"


@tool
def add_jira_watcher(issue_key: str, username_or_account_id: str) -> str:
    """Add a user as a watcher to a Jira issue."""
    try:
        jira = _jira()
        jira.issue_add_watcher(issue_key, username_or_account_id)
        return f"✅ Added watcher '{username_or_account_id}' to {issue_key}."
    except Exception as e:
        return f"⚠️ Failed to add watcher to {issue_key}: {e}"


@tool
def get_jira_worklogs(issue_key: str) -> str:
    """Retrieve worklog history (time spent) for a Jira issue."""
    try:
        jira = _jira()
        res = jira.issue_get_worklog(issue_key)
        worklogs = res.get("worklogs", []) if isinstance(res, dict) else []
        if not worklogs:
            return f"No worklogs recorded for {issue_key}."
        lines = [f"Worklogs for *{issue_key}*:"]
        for wl in worklogs:
            author = wl.get("author", {}).get("displayName", "Unknown")
            time_spent = wl.get("timeSpent", "N/A")
            comment = _extract_text(wl.get("comment"))
            lines.append(f"• _{author}_ logged *{time_spent}*: {comment}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Failed to get worklogs for {issue_key}: {e}"


@tool
def add_jira_worklog(issue_key: str, time_spent: str, comment: Optional[str] = None) -> str:
    """Log work time spent on an issue (e.g., '2h 30m', '1d', '45m') with optional comment."""
    try:
        jira = _jira()
        payload: dict[str, Any] = {"timeSpent": time_spent}
        if comment:
            payload["comment"] = comment
        jira.issue_add_json_worklog(issue_key, payload)
        comment_note = f" (Comment: {comment})" if comment else ""
        return f"✅ Logged *{time_spent}* on {issue_key}{comment_note}."
    except Exception as e:
        return f"⚠️ Failed to log worklog on {issue_key}: {e}"


# ── Official Exports ──────────────────────────────────────────────────────────

JIRA_TOOLS: list[BaseTool] = [
    # Issue Lifecycle
    create_jira_issue,
    get_jira_issue,
    update_jira_issue,
    delete_jira_issue,
    # Transitions & Workflow
    get_jira_transitions,
    transition_jira_issue,
    # Searching & JQL
    search_jira_issues,
    list_my_jira_issues,
    get_jira_project_issues,
    # Users & Assignment
    search_jira_users,
    assign_jira_issue,
    # Comments
    add_jira_comment,
    get_jira_comments,
    # Linking
    get_jira_issue_link_types,
    link_jira_issues,
    get_jira_issue_links,
    # Attachments
    add_jira_attachment,
    # Projects & Releases
    list_jira_projects,
    get_jira_project_details,
    get_jira_project_components,
    get_jira_project_versions,
    # Sprints & Agile Boards
    list_jira_boards,
    list_jira_sprints,
    get_jira_sprint_issues,
    add_jira_issues_to_sprint,
    # Watchers & Worklogs
    get_jira_watchers,
    add_jira_watcher,
    get_jira_worklogs,
    add_jira_worklog,
]
