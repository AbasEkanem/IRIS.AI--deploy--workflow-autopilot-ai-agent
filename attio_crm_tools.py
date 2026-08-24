"""
attio_crm_tools.py
==================
Full Attio CRM API v2 tool suite for a LangChain/DeepAgents subagent.

Provides full CRM capability:
  - People & Contact management (create, search, query, update, delete)
  - Company & Account management (create, search, query, update, delete)
  - List & Pipeline management (create lists, list pipelines, query entries, add/remove entries)
  - Notes & Call summaries (create rich notes on records, list, delete)
  - Follow-up Tasks (create tasks with deadlines, update status, delete)
  - Collaboration Comments (post comments on records, list comments)
  - Workspace members (needed to resolve UUIDs for task assignees / comment authors)

Fixes applied in this revision (verified against Attio's live docs/OpenAPI spec
as of Aug 2026):

1. `search_attio_records` no longer silently swallows API errors (auth
   failures, rate limits, etc.) as "no results found" — it now surfaces
   the failure so the caller/agent can tell "genuinely empty" apart from
   "the request failed".
2. `_req` now retries on HTTP 429, correctly parsing Attio's `Retry-After`
   header as an HTTP-date (per Attio's docs), not a plain number of
   seconds — the previous naive `float(retry_after)` would have crashed
   on a real rate-limit response. Also retries transient 5xx with backoff.
3. `create_attio_task` now always sends `deadline_at`, `linked_records`,
   and `assignees` — Attio's schema marks all three as required request
   fields (deadline_at may be `null`, but the key must be present).
4. `create_attio_comment` requires `author_id` (a workspace-member UUID) and
   sends the parent record as a NESTED `record: {object, record_id}` object —
   both verified LIVE against the real API (Aug 2026). The old flat
   `record_id`/`object` keys at the data root were rejected with a generic
   "data: Invalid input" error.
5. `list_attio_comments` reads `GET /v2/threads` (comments live inside
   threads) — verified live. There is NO `GET /v2/comments` list endpoint;
   the old code hit it and got HTTP 404 every time.
6. `list_attio_lists` defensively handles `parent_object` whether Attio
   returns it as a string or a list, avoiding a silent character-split
   bug from `", ".join()` on a plain string.
7. Added `list_workspace_members`, needed to resolve real UUIDs for task
   `assignees` and comment `author_id` — those fields require actual
   workspace-member IDs, which have no other lookup path in this file.

VERIFICATION STATUS (Aug 2026): every endpoint in this module — records,
objects, notes, lists, tasks, threads/comments, and workspace_members — was
verified LIVE against the connected Attio workspace (token scopes confirmed
via GET /v2/self). The create-comment and list-comments (threads) shapes were
confirmed with self-cleaning create/read/delete round-trips.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from langchain_core.tools import tool

from idempotency import idempotent

load_dotenv()

_log = logging.getLogger(__name__)

_ATTIO_BASE_URL = "https://api.attio.com/v2"
_ATTIO_ACCESS_TOKEN = os.getenv("ATTIO_ACCESS_TOKEN", "")

_MAX_RETRIES = 3


def _get_headers() -> dict[str, str]:
    token = os.getenv("ATTIO_ACCESS_TOKEN", _ATTIO_ACCESS_TOKEN)
    if not token:
        raise EnvironmentError(
            "ATTIO_ACCESS_TOKEN is missing from .env. "
            "Please generate an API key in Attio (Workspace settings > Developers > + New access token)."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _parse_retry_after(header_value: str | None) -> float:
    """
    Attio's Retry-After header is an HTTP-date (e.g. 'Tue, 23 May 2023
    14:42:01 GMT'), not a plain number of seconds. This parses that date
    and returns how many seconds to wait, falling back gracefully if the
    header is missing or in an unexpected format.
    """
    if not header_value:
        return 1.0

    # Some APIs send a plain integer number of seconds instead of a date —
    # handle both, since the standard technically allows either.
    try:
        return max(0.0, float(header_value))
    except ValueError:
        pass

    try:
        retry_dt = parsedate_to_datetime(header_value)
        if retry_dt.tzinfo is None:
            retry_dt = retry_dt.replace(tzinfo=timezone.utc)
        delta = (retry_dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return 1.0


def _req(
    method: str,
    path: str,
    json_data: dict | None = None,
    params: dict | None = None,
) -> dict[str, Any]:
    url = f"{_ATTIO_BASE_URL}{path}"
    headers = _get_headers()

    for attempt in range(_MAX_RETRIES + 1):
        resp = requests.request(
            method, url, headers=headers, json=json_data, params=params, timeout=30
        )

        if resp.status_code == 429:
            wait_s = _parse_retry_after(resp.headers.get("Retry-After"))
            if attempt < _MAX_RETRIES:
                _log.warning(
                    "Attio rate limit hit (attempt %s/%s), waiting %.2fs",
                    attempt + 1,
                    _MAX_RETRIES,
                    wait_s,
                )
                time.sleep(wait_s)
                continue
            raise RuntimeError(
                f"Attio API error (429): rate limit exceeded after {_MAX_RETRIES} retries"
            )

        if resp.status_code >= 500 and attempt < _MAX_RETRIES:
            backoff = 2 ** attempt
            _log.warning(
                "Attio server error %s (attempt %s/%s), backing off %ss",
                resp.status_code,
                attempt + 1,
                _MAX_RETRIES,
                backoff,
            )
            time.sleep(backoff)
            continue

        if resp.status_code == 204:
            return {"status": "ok"}

        try:
            data = resp.json()
        except Exception:
            data = {"raw_text": resp.text}

        if resp.status_code >= 400:
            err_msg = data.get("message") or data.get("error") or resp.text
            raise RuntimeError(f"Attio API error ({resp.status_code}): {err_msg}")

        return data

    # Should be unreachable, but keeps type-checkers happy.
    raise RuntimeError("Attio API request failed after retries with no response.")


# ==============================================================================
# 1. SEARCH & QUERY TOOLS
# ==============================================================================

@tool
def search_attio_records(query: str, limit: int = 10) -> str:
    """Search for records (people or companies) across Attio by name, email, domain, or keyword.

    Args:
        query: The search term (e.g. 'John Doe', 'stripe.com', 'Acme Corp').
        limit: Max results to return (default 10).

    Note: this searches only the most recent 50 records per object type
    (Attio has no free-text search across arbitrary fields via this
    endpoint), so very old or low-activity records may not surface here.
    For a guaranteed match, use query_attio_records or get_attio_record
    with a known ID instead.
    """
    query_clean = query.strip().lower()
    matches: list[dict[str, str]] = []
    errors: list[str] = []

    for object_slug, label in (("people", "PEOPLE"), ("companies", "COMPANIES")):
        try:
            res = _req(
                "POST", f"/objects/{object_slug}/records/query", json_data={"limit": 50}
            )
        except Exception as e:
            errors.append(f"{label}: {e}")
            continue

        for rec in res.get("data", []):
            rid = rec.get("id", {}).get("record_id", "")
            vals = rec.get("values", {})

            if object_slug == "people":
                name_list = vals.get("name", [])
                title = name_list[0].get("full_name", "") if name_list else ""
                email_list = vals.get("email_addresses", [])
                details = [e.get("email_address", "") for e in email_list]
            else:
                name_list = vals.get("name", [])
                title = name_list[0].get("value", "") if name_list else ""
                dom_list = vals.get("domains", [])
                details = [d.get("domain", "") for d in dom_list]

            combined = f"{title} {' '.join(details)}".lower()
            if query_clean in combined or not query_clean:
                matches.append(
                    {
                        "object": label,
                        "title": title or f"Unnamed {label.title()}",
                        "detail": ", ".join(details) if details else "No contact info",
                        "record_id": rid,
                        "web_url": rec.get("web_url", ""),
                    }
                )

    if not matches and errors:
        # Every query failed — this is NOT the same as "no records found".
        # Surface it clearly so the agent/caller doesn't assume a genuine
        # empty result and, e.g., go create a duplicate record.
        return f"⚠️ Search failed for all object types: {'; '.join(errors)}"

    if not matches:
        return f"No records found matching '{query}' in Attio."

    lines = [f"🔍 Found {len(matches[:limit])} record(s) matching '{query}':"]
    for m in matches[:limit]:
        lines.append(
            f"- [{m['object']}] **{m['title']}** ({m['detail']}) | ID: `{m['record_id']}` | Link: {m['web_url']}"
        )
    if errors:
        lines.append(f"⚠️ Note: some object types failed to search: {'; '.join(errors)}")
    return "\n".join(lines)


@tool
def query_attio_records(
    object_slug: str = "people",
    limit: int = 20,
    filter_json: str = "",
) -> str:
    """Query records from an Attio object collection with optional attribute-based filtering.

    Args:
        object_slug: The object collection slug ('people' or 'companies'). Default 'people'.
        limit: Max records to return (default 20, max 50).
        filter_json: Optional JSON string with an Attio filter object to narrow results.
            Attio's real filter syntax nests a "$" operator under the attribute
            slug — there is NO "filters"/"condition" wrapper key, and sending
            one will fail with "Unknown attribute slug: condition".

            Single condition:
            '{"lead_status": {"$eq": "Hot"}}'

            Multiple conditions combined:
            '{"$and": [{"lead_status": {"$eq": "Hot"}}, {"last_interaction_at": {"$lte": "2026-08-12"}}]}'

            Valid operators: $eq, $not, $gt, $gte, $lt, $lte, $contains.
            Attribute slugs are workspace-specific — if unsure, call
            GET /objects/{object_slug}/attributes first rather than guessing
            slug variants like 'lead_status', 'temperature', 'status', 'stage'.
    """
    try:
        payload: dict[str, Any] = {"limit": min(limit, 50)}
        if filter_json:
            try:
                payload["filter"] = json.loads(filter_json)
            except json.JSONDecodeError as exc:
                return f"⚠️ filter_json is not valid JSON: {exc}"

        res = _req("POST", f"/objects/{object_slug}/records/query", json_data=payload)
        data = res.get("data", [])
        if not data:
            filter_hint = " (no records matched the filter — try a different attribute slug or value)" if filter_json else ""
            return f"No records found in object collection `{object_slug}`{filter_hint}."

        lines = [f"📋 Found {len(data)} record(s) in `{object_slug}`:"]
        for r in data:
            rec_id = r.get("id", {}).get("record_id", "N/A")
            vals = r.get("values", {})
            web_url = r.get("web_url", "")

            # ── Name ──────────────────────────────────────────────────────────
            name_entries = vals.get("name", [{}])
            first_name_entry = name_entries[0] if name_entries else {}
            name_val = (
                first_name_entry.get("full_name")
                or first_name_entry.get("value")
                or "Untitled"
            )

            # ── Email (people) ────────────────────────────────────────────────
            emails = [
                e.get("email_address", "")
                for e in vals.get("email_addresses", [])
                if e.get("email_address")
            ]
            email_str = emails[0] if emails else ""

            # ── Lead status / temperature (try common slug variants) ──────────
            status_val = ""
            for slug in ("lead_status", "temperature", "status", "stage"):
                entries = vals.get(slug, [])
                if entries and isinstance(entries, list):
                    raw = entries[0]
                    status_val = (
                        raw.get("value") or raw.get("title") or ""
                        if isinstance(raw, dict) else str(raw)
                    )
                    if status_val:
                        break

            # ── Last interaction (try common slug variants) ───────────────────
            last_interact = ""
            for slug in ("last_interaction_at", "last_contacted_at",
                         "last_contact_date", "last_activity_at"):
                entries = vals.get(slug, [])
                if entries and isinstance(entries, list):
                    raw = entries[0]
                    last_interact = (
                        str(raw.get("value", "") or "")
                        if isinstance(raw, dict) else str(raw)
                    )
                    if last_interact:
                        break

            # ── Build summary line ────────────────────────────────────────────
            parts: list[str] = [f"**{name_val}**", f"ID: `{rec_id}`"]
            if email_str:
                parts.append(f"Email: {email_str}")
            if status_val:
                parts.append(f"Status: {status_val}")
            if last_interact:
                parts.append(f"Last Contact: {last_interact[:10]}")
            if web_url:
                parts.append(f"Link: {web_url}")
            lines.append("- " + " | ".join(parts))

        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Query failed: {e}"


@tool
def list_object_attributes(object_slug: str = "people") -> str:
    """List the real attribute slugs available on an Attio object collection.

    Call this BEFORE filtering on any attribute you haven't confirmed exists
    (e.g. a status/temperature/stage field) instead of guessing slug names —
    guessed slugs like 'lead_status' commonly fail with
    "Unknown attribute slug: <slug>" because the actual slug is
    workspace-specific (e.g. it might be 'status', 'stage', or a custom name).

    Args:
        object_slug: 'people' or 'companies'. Default 'people'.
    """
    try:
        res = _req("GET", f"/objects/{object_slug}/attributes")
        data = res.get("data", [])
        if not data:
            return f"No attributes found for object `{object_slug}`."

        lines = [f"🏷️ Attributes on `{object_slug}` ({len(data)}):"]
        for attr in data:
            slug = attr.get("api_slug", "N/A")
            title = attr.get("title", "")
            attr_type = attr.get("type", "")
            lines.append(f"- `{slug}` ({attr_type}) — {title}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ List attributes failed: {e}"


@tool
def find_status_attribute(status_value: str = "Hot") -> str:
    """Locate which object/list actually holds a lead-status-style value (e.g. 'Hot').

    Attio workspaces vary: a status like 'Hot' might be a select attribute on
    `people`, a select attribute on `deals`, or a STAGE on a pipeline list
    rather than an attribute at all. Guessing attribute slugs on `people`
    alone (e.g. 'lead_status', 'temperature', 'status') will silently miss
    it if it actually lives on `deals` or on a list's stage field.

    Call this ONCE at the start of any "find leads/deals with status X" task
    instead of guessing slugs or retrying the same object repeatedly. It
    checks, in order: `people` attributes, `deals` attributes, and every
    list's stage/status options — and reports exactly where (if anywhere)
    the value was found, so you can build the correct filter or list query
    directly instead of trial-and-error.

    Args:
        status_value: The status/stage value to look for (e.g. 'Hot').
    """
    findings: list[str] = []
    value_lower = status_value.strip().lower()

    # 1. Check `people` attributes for a select field with a matching option.
    for object_slug in ("people", "deals"):
        try:
            res = _req("GET", f"/objects/{object_slug}/attributes")
            for attr in res.get("data", []):
                slug = attr.get("api_slug", "")
                attr_type = attr.get("type", "")
                options = attr.get("config", {}).get("select", {}).get("options", []) if isinstance(attr.get("config"), dict) else []
                option_titles = [o.get("title", "") for o in options] if options else []
                if attr_type == "select" and any(value_lower == t.strip().lower() for t in option_titles):
                    findings.append(
                        f"✅ Found on OBJECT `{object_slug}`: attribute `{slug}` (select), "
                        f"option '{status_value}'. Filter with: "
                        f'{{"{slug}": {{"$eq": "{status_value}"}}}} against object_slug="{object_slug}"'
                    )
        except Exception as e:
            findings.append(f"⚠️ Could not check `{object_slug}` attributes: {e}")

    # 2. Check every list's stages for a matching status/stage option.
    try:
        res = _req("GET", "/lists")
        for l in res.get("data", []):
            list_slug = l.get("api_slug", "")
            list_name = l.get("name", "Untitled List")
            for attr in l.get("attributes", []) if isinstance(l.get("attributes"), list) else []:
                attr_type = attr.get("type", "")
                options = attr.get("config", {}).get("select", {}).get("options", []) if isinstance(attr.get("config"), dict) else []
                option_titles = [o.get("title", "") for o in options] if options else []
                if attr_type == "select" and any(value_lower == t.strip().lower() for t in option_titles):
                    findings.append(
                        f"✅ Found on LIST `{list_name}` (slug `{list_slug}`): stage attribute "
                        f"`{attr.get('api_slug', '')}`, option '{status_value}'. Use "
                        f"get_attio_list_entries(list_slug_or_id=\"{list_slug}\") and filter "
                        f"entries by that stage value client-side."
                    )
    except Exception as e:
        findings.append(f"⚠️ Could not check lists: {e}")

    if not findings or all(f.startswith("⚠️") for f in findings):
        return (
            f"❌ No attribute or list stage matching '{status_value}' found on "
            f"`people`, `deals`, or any list. Do not keep guessing slug names — "
            f"ask the user to confirm where this status lives, or fetch a known "
            f"'Hot' lead's record directly with get_attio_record to inspect it."
        )
    return "\n".join(findings)


@tool
def get_attio_record(object_slug: str, record_id: str) -> str:
    """Fetch full details and attributes of a single person or company record in Attio.

    Args:
        object_slug: 'people' or 'companies'.
        record_id: The UUID of the record.
    """
    try:
        res = _req("GET", f"/objects/{object_slug}/records/{record_id}")
        data = res.get("data", {})
        vals = data.get("values", {})
        web_url = data.get("web_url", "")
        created_at = data.get("created_at", "")

        lines = [
            f"📄 **Attio {object_slug[:-1].capitalize()} Record** (ID: `{record_id}`)",
            f"- **Web URL**: {web_url}",
            f"- **Created At**: {created_at}",
            "- **Attributes**:",
        ]
        for attr_slug, val_list in vals.items():
            str_vals = []
            for v in val_list:
                if isinstance(v, dict):
                    # Use explicit str() on every branch: numeric fields like
                    # revenue or score can return a float from v.get("value"),
                    # which is truthy but not a str — causing
                    # "sequence item N: expected str instance, float found".
                    raw = (
                        v.get("value")
                        if v.get("value") is not None
                        else (
                            v.get("full_name")
                            or v.get("email_address")
                            or v.get("domain")
                            or v
                        )
                    )
                    str_vals.append(str(raw))
                else:
                    str_vals.append(str(v))
            lines.append(f"  • `{attr_slug}`: {', '.join(str_vals)}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Get record failed: {e}"


@tool
def get_attio_record_interactions(object_slug: str, record_id: str) -> str:
    """Get the last interaction / contact date for a person or company record in Attio.

    Returns the most recent interaction timestamp, sourced from the record's built-in
    interaction-tracking attributes first, then falls back to the most-recent note date.

    Use this to answer 'has this lead been contacted in the last N days?'
    Pair with query_attio_records(filter_json=...) to build a stale-lead report.

    Args:
        object_slug: 'people' or 'companies'.
        record_id: The UUID of the record.
    """
    try:
        # ── 1. Check the record's own interaction-tracking attributes ──────────
        res = _req("GET", f"/objects/{object_slug}/records/{record_id}")
        vals = res.get("data", {}).get("values", {})

        for slug in (
            "last_interaction_at",
            "last_contacted_at",
            "last_contact_date",
            "last_activity_at",
            "last_modified_at",
        ):
            entries = vals.get(slug, [])
            if entries and isinstance(entries, list):
                raw = entries[0]
                val = (
                    str(raw.get("value", "") or "")
                    if isinstance(raw, dict) else str(raw)
                )
                if val:
                    return (
                        f"📅 Last interaction for `{record_id}` "
                        f"(attribute `{slug}`): **{val}**"
                    )

        # ── 2. Fall back: most recent note date ────────────────────────────────
        try:
            notes_res = _req(
                "GET",
                "/notes",
                params={
                    "parent_record_id": record_id,
                    "parent_object": object_slug,
                    "limit": 1,
                },
            )
            notes = notes_res.get("data", [])
            if notes:
                note_date = (notes[0].get("created_at") or "")[:10]
                note_title = notes[0].get("title", "Note")
                if note_date:
                    return (
                        f"📅 Last interaction for `{record_id}` "
                        f"(most recent note): **{note_date}** "
                        f"(Note: \"{note_title}\")"
                    )
        except Exception:
            pass  # notes fallback failed; fall through to no-data response

        return (
            f"⚠️ No interaction date found for `{record_id}` in `{object_slug}`. "
            "No last_interaction_at attribute and no notes attached. "
            "This record may never have been contacted."
        )
    except Exception as e:
        return f"⚠️ Get interactions failed: {e}"


# ==============================================================================
# 2. CONTACTS & COMPANIES CRUD
# ==============================================================================

@tool
@idempotent("create_attio_person", key_args=["email", "name"])
def create_attio_person(
    name: str,
    email: str,
    job_title: str = "",
    phone: str = "",
    description: str = "",
) -> str:
    """Create a new contact (person record) in Attio CRM.

    Args:
        name: Full name of the person (e.g. 'Sarah Connor').
        email: Primary email address (e.g. 'sarah@techcorp.io').
        job_title: Optional job title (e.g. 'VP Engineering').
        phone: Optional phone number (e.g. '+14155552671').
        description: Optional notes/bio for the contact card.
    """
    try:
        parts = name.strip().split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

        values: dict[str, Any] = {
            "name": [{"first_name": first_name, "last_name": last_name, "full_name": name}],
            "email_addresses": [{"email_address": email}],
        }
        if job_title:
            values["job_title"] = [{"value": job_title}]
        if phone:
            # Attio's phone_numbers attribute requires the key `original_phone_number`
            # (an E.164 string); the older `phone_number` key is rejected with a 400
            # "invalid value ... slug phone_numbers". Verified live Aug 2026.
            values["phone_numbers"] = [{"original_phone_number": phone}]
        if description:
            values["description"] = [{"value": description}]

        payload = {"data": {"values": values}}
        res = _req("POST", "/objects/people/records", json_data=payload)
        data = res.get("data", {})
        rec_id = data.get("id", {}).get("record_id", "N/A")
        web_url = data.get("web_url", "")
        return f"✅ Created person **{name}** (ID: `{rec_id}`) | Attio Link: {web_url}"
    except Exception as e:
        return f"⚠️ Create person failed: {e}"


@tool
@idempotent("create_attio_company", key_args=["name", "domain"])
def create_attio_company(
    name: str,
    domain: str = "",
    description: str = "",
) -> str:
    """Create a new company / account record in Attio CRM.

    Args:
        name: Official company name (e.g. 'Stripe, Inc.').
        domain: Primary domain without protocol (e.g. 'stripe.com').
        description: Optional company overview or bio.
    """
    try:
        values: dict[str, Any] = {
            "name": [{"value": name}],
        }
        if domain:
            clean_dom = domain.replace("https://", "").replace("http://", "").rstrip("/")
            values["domains"] = [{"domain": clean_dom}]
        if description:
            values["description"] = [{"value": description}]

        payload = {"data": {"values": values}}
        res = _req("POST", "/objects/companies/records", json_data=payload)
        data = res.get("data", {})
        rec_id = data.get("id", {}).get("record_id", "N/A")
        web_url = data.get("web_url", "")
        return f"✅ Created company **{name}** (ID: `{rec_id}`) | Attio Link: {web_url}"
    except Exception as e:
        return f"⚠️ Create company failed: {e}"


@tool
@idempotent("update_attio_record", key_args=["object_slug", "record_id", "values_json"])
def update_attio_record(
    object_slug: str,
    record_id: str,
    values_json: str,
) -> str:
    """Update specific attributes on an existing person or company in Attio.

    Read-modify-merge (safe update): the record is fetched FIRST, so a blank or
    empty incoming value is never allowed to overwrite an attribute that already
    holds real data. This prevents the "patch one field, silently wipe the
    description" corruption class. Attributes you don't mention are always left
    untouched (Attio only changes attributes present in the PATCH body).

    Args:
        object_slug: 'people' or 'companies'.
        record_id: The UUID of the record to update.
        values_json: JSON string mapping attribute slug to values list, e.g.:
                     '{"job_title": [{"value": "Chief Technology Officer"}]}'
    """
    try:
        incoming = json.loads(values_json)
    except Exception as e:
        return f"⚠️ Update record failed: values_json is not valid JSON ({e})."
    if not isinstance(incoming, dict):
        return ("⚠️ Update record failed: values_json must be a JSON object "
                "mapping attribute slugs to value lists.")

    try:
        # Read current state so we never blank a populated attribute as a side
        # effect of patching an unrelated one.
        current = _req("GET", f"/objects/{object_slug}/records/{record_id}")
        existing_vals = current.get("data", {}).get("values", {}) or {}

        def _is_blank(v: Any) -> bool:
            if v in (None, "", [], {}):
                return True
            if isinstance(v, list):
                # e.g. [{"value": ""}] / [{"value": None}] — a "clear the field" payload.
                return all(_is_blank(item) for item in v)
            if isinstance(v, dict):
                inner = v.get("value", v.get("full_name", v.get("original_phone_number")))
                return inner in (None, "")
            return False

        def _has_existing(slug: str) -> bool:
            ev = existing_vals.get(slug)
            return bool(ev) and not _is_blank(ev)

        merged: dict[str, Any] = {}
        preserved: list[str] = []
        for slug, val in incoming.items():
            if _is_blank(val) and _has_existing(slug):
                # Refuse to overwrite real data with an empty value.
                preserved.append(slug)
                _log.warning(
                    "update_attio_record: refusing to blank populated attribute '%s' on %s/%s",
                    slug, object_slug, record_id,
                )
                continue
            merged[slug] = val

        if not merged:
            note = f" (preserved from blanking: {', '.join(preserved)})" if preserved else ""
            return f"⚠️ Update record: nothing to change{note}."

        payload = {"data": {"values": merged}}
        res = _req("PATCH", f"/objects/{object_slug}/records/{record_id}", json_data=payload)
        data = res.get("data", {})
        web_url = data.get("web_url", "")
        changed = ", ".join(sorted(merged))
        suffix = f" | preserved (not blanked): {', '.join(preserved)}" if preserved else ""
        return f"✅ Updated {object_slug[:-1]} record `{record_id}` [{changed}]{suffix} | Link: {web_url}"
    except Exception as e:
        return f"⚠️ Update record failed: {e}"


@tool
def delete_attio_record(object_slug: str, record_id: str) -> str:
    """Delete a person or company record from Attio.

    Args:
        object_slug: 'people' or 'companies'.
        record_id: The UUID of the record to delete.
    """
    try:
        _req("DELETE", f"/objects/{object_slug}/records/{record_id}")
        return f"✅ Deleted {object_slug[:-1]} record `{record_id}` from Attio."
    except Exception as e:
        return f"⚠️ Delete record failed: {e}"


# ==============================================================================
# 3. LISTS & PIPELINES
# ==============================================================================

@tool
def list_attio_lists() -> str:
    """List all CRM lists, views, and sales pipelines in the Attio workspace."""
    try:
        res = _req("GET", "/lists")
        data = res.get("data", [])
        if not data:
            return "No lists/pipelines found in this Attio workspace."

        lines = [f"📊 Found {len(data)} list(s)/pipeline(s):"]
        for l in data:
            lid = l.get("id", {}).get("list_id", "N/A")
            name = l.get("name", "Untitled List")
            slug = l.get("api_slug", "N/A")

            # Defensive: parent_object may come back as a string OR a list
            # depending on the endpoint/version. Blindly ", ".join()-ing a
            # plain string silently splits it into individual characters.
            raw_parent = l.get("parent_object", ["people"])
            if isinstance(raw_parent, str):
                parent_obj = raw_parent
            elif isinstance(raw_parent, (list, tuple)):
                parent_obj = ", ".join(str(p) for p in raw_parent)
            else:
                parent_obj = str(raw_parent)

            lines.append(f"- **{name}** (Slug: `{slug}` | ID: `{lid}` | Target: `{parent_obj}`)")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ List pipelines failed: {e}"


@tool
def create_attio_list(
    name: str,
    api_slug: str = "",
    parent_object: str = "people",
) -> str:
    """Create a new pipeline or lead tracking list in Attio CRM.

    Args:
        name: Display name of the pipeline/list (e.g. 'lead_pipeline', 'Enterprise Sales').
        api_slug: Unique identifier slug (e.g. 'lead_pipeline'). Defaults to normalized name.
        parent_object: 'people' or 'companies'. Default 'people'.
    """
    try:
        slug = api_slug or name.strip().lower().replace(" ", "_").replace("-", "_")
        payload = {
            "data": {
                "name": name,
                "api_slug": slug,
                "parent_object": parent_object,
                "workspace_access": "full-access",
                "workspace_member_access": [],
            }
        }
        res = _req("POST", "/lists", json_data=payload)
        data = res.get("data", {})
        lid = data.get("id", {}).get("list_id", "N/A")
        return f"✅ Created Attio pipeline list **{name}** (Slug: `{slug}` | ID: `{lid}`)."
    except Exception as e:
        return f"⚠️ Create list failed: {e}"


@tool
def get_attio_list_entries(list_slug_or_id: str, limit: int = 20) -> str:
    """Retrieve entries (records placed on a list/pipeline) from an Attio list.

    Args:
        list_slug_or_id: The slug or ID of the list (e.g. 'sales-pipeline').
        limit: Max entries to return (default 20).
    """
    try:
        payload = {"limit": min(limit, 50)}
        res = _req("POST", f"/lists/{list_slug_or_id}/entries/query", json_data=payload)
        data = res.get("data", [])
        if not data:
            return f"No entries found in list `{list_slug_or_id}`."

        lines = [f"📋 {len(data)} entry(s) in list `{list_slug_or_id}`:"]
        for entry in data:
            eid = entry.get("id", {}).get("entry_id", "N/A")
            parent_id = entry.get("parent_record_id", "N/A")
            parent_obj = entry.get("parent_object", "record")
            lines.append(f"- Entry `{eid}` | Parent {str(parent_obj).upper()}: `{parent_id}`")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Get list entries failed: {e}"


@tool
def add_attio_list_entry(
    list_slug_or_id: str,
    parent_record_id: str,
    parent_object: str = "people",
) -> str:
    """Add an existing person or company record onto an Attio list / pipeline.

    Args:
        list_slug_or_id: The slug or ID of the list.
        parent_record_id: The UUID of the person or company record.
        parent_object: 'people' or 'companies'. Default 'people'.
    """
    try:
        payload = {
            "data": {
                "parent_record_id": parent_record_id,
                "parent_object": parent_object,
                "entry_values": {},
            }
        }
        res = _req("POST", f"/lists/{list_slug_or_id}/entries", json_data=payload)
        data = res.get("data", {})
        eid = data.get("id", {}).get("entry_id", "N/A")
        return f"✅ Added {parent_object[:-1]} `{parent_record_id}` to list `{list_slug_or_id}` (Entry ID: `{eid}`)."
    except Exception as e:
        return f"⚠️ Add list entry failed: {e}"


@tool
def delete_attio_list_entry(list_slug_or_id: str, entry_id: str) -> str:
    """Remove an entry from an Attio list / pipeline.

    Args:
        list_slug_or_id: The slug or ID of the list.
        entry_id: The entry UUID to remove.
    """
    try:
        _req("DELETE", f"/lists/{list_slug_or_id}/entries/{entry_id}")
        return f"✅ Removed entry `{entry_id}` from list `{list_slug_or_id}`."
    except Exception as e:
        return f"⚠️ Delete list entry failed: {e}"


# ==============================================================================
# 4. NOTES & CALL SUMMARIES
# ==============================================================================

@tool
def list_attio_notes(parent_record_id: str = "", parent_object: str = "people", limit: int = 10) -> str:
    """List notes attached to a person or company record in Attio.

    Args:
        parent_record_id: Optional record ID to filter notes for a specific contact/account.
        parent_object: 'people' or 'companies' (used when filtering by record ID).
        limit: Max notes to return (default 10).
    """
    try:
        params: dict[str, Any] = {"limit": min(limit, 25)}
        if parent_record_id:
            params["parent_record_id"] = parent_record_id
            params["parent_object"] = parent_object

        res = _req("GET", "/notes", params=params)
        data = res.get("data", [])
        if not data:
            return "No notes found."

        lines = [f"📝 Found {len(data)} note(s):"]
        for n in data:
            nid = n.get("id", {}).get("note_id", "N/A")
            title = n.get("title", "Untitled Note")
            created_at = n.get("created_at", "")[:10]
            lines.append(f"- **{title}** (ID: `{nid}` | Date: {created_at})")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ List notes failed: {e}"


@tool
@idempotent("create_attio_note", key_args=["parent_record_id", "title", "content"])
def create_attio_note(
    title: str,
    content: str,
    parent_record_id: str,
    parent_object: str = "people",
) -> str:
    """Create a rich note attached to a person or company record in Attio (e.g. meeting notes, call summary).

    Args:
        title: Note headline (e.g. 'Intro Call with CTO').
        content: Markdown or plaintext content of the note.
        parent_record_id: UUID of the target person or company record.
        parent_object: 'people' or 'companies'. Default 'people'.
    """
    try:
        payload = {
            "data": {
                "title": title,
                "content": content,
                "parent_record_id": parent_record_id,
                "parent_object": parent_object,
                "format": "plaintext",
            }
        }
        res = _req("POST", "/notes", json_data=payload)
        data = res.get("data", {})
        nid = data.get("id", {}).get("note_id", "N/A")
        return f"✅ Created note **{title}** on {parent_object[:-1]} `{parent_record_id}` (Note ID: `{nid}`)."
    except Exception as e:
        return f"⚠️ Create note failed: {e}"


@tool
def delete_attio_note(note_id: str) -> str:
    """Delete a note from Attio.

    Args:
        note_id: UUID of the note to delete.
    """
    try:
        _req("DELETE", f"/notes/{note_id}")
        return f"✅ Deleted note `{note_id}` from Attio."
    except Exception as e:
        return f"⚠️ Delete note failed: {e}"


# ==============================================================================
# 5. CRM TASKS & ACTION ITEMS
# ==============================================================================

@tool
def list_attio_tasks(is_completed: Optional[bool] = None, limit: int = 15) -> str:
    """List CRM tasks and action items in Attio.

    Args:
        is_completed: Filter by completion status (True for completed, False for open, None for all).
        limit: Max tasks to return (default 15).
    """
    try:
        params: dict[str, Any] = {"limit": min(limit, 50)}
        if is_completed is not None:
            params["is_completed"] = "true" if is_completed else "false"

        res = _req("GET", "/tasks", params=params)
        data = res.get("data", [])
        if not data:
            return "No tasks found in Attio."

        lines = [f"✅ Found {len(data)} task(s):"]
        for t in data:
            tid = t.get("id", {}).get("task_id", "N/A")
            content = t.get("content_plaintext", t.get("content", "Task"))
            completed = t.get("is_completed", False)
            status_icon = "☑️" if completed else "⬜"
            deadline = (t.get("deadline_at") or "")[:10]
            deadline_str = f" | Due: {deadline}" if deadline else ""
            lines.append(f"- {status_icon} **{content}** (ID: `{tid}`{deadline_str})")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ List tasks failed: {e}"


@tool
def create_attio_task(
    content: str,
    deadline: str = "",
    linked_record_id: str = "",
    linked_object: str = "people",
) -> str:
    """Create a CRM follow-up task or action item in Attio.

    Args:
        content: Description of the action item (e.g. 'Send follow-up proposal to Sarah').
        deadline: Optional ISO 8601 deadline (e.g. '2026-08-20T17:00:00Z').
        linked_record_id: Optional record ID to link the task to (person or company).
        linked_object: 'people' or 'companies' (if linking to a record). Default 'people'.
    """
    try:
        # Attio's task schema requires content, format, deadline_at,
        # linked_records, and assignees to ALL be present in the request
        # body (deadline_at may be null, but the key itself is required) —
        # so these are always included below, never conditionally omitted.
        task_data: dict[str, Any] = {
            "content": content,
            "format": "plaintext",
            "deadline_at": deadline if deadline else None,
            "is_completed": False,
            "linked_records": [],
            "assignees": [],
        }
        if linked_record_id:
            task_data["linked_records"] = [
                {"target_object": linked_object, "target_record_id": linked_record_id}
            ]

        res = _req("POST", "/tasks", json_data={"data": task_data})
        data = res.get("data", {})
        tid = data.get("id", {}).get("task_id", "N/A")
        return f"✅ Created CRM task **{content}** in Attio (Task ID: `{tid}`)."
    except Exception as e:
        return f"⚠️ Create task failed: {e}"


@tool
def update_attio_task_status(task_id: str, is_completed: bool = True) -> str:
    """Mark a CRM task as completed or open in Attio.

    Args:
        task_id: UUID of the task.
        is_completed: True to mark completed, False to mark open. Default True.
    """
    try:
        _req("PATCH", f"/tasks/{task_id}", json_data={"data": {"is_completed": is_completed}})
        status_str = "completed" if is_completed else "re-opened"
        return f"✅ Task `{task_id}` marked as {status_str} in Attio."
    except Exception as e:
        return f"⚠️ Update task failed: {e}"


@tool
def delete_attio_task(task_id: str) -> str:
    """Delete a CRM task from Attio.

    Args:
        task_id: UUID of the task to delete.
    """
    try:
        _req("DELETE", f"/tasks/{task_id}")
        return f"✅ Deleted task `{task_id}` from Attio."
    except Exception as e:
        return f"⚠️ Delete task failed: {e}"


# ==============================================================================
# 6. COMMENTS
# ==============================================================================

@tool
def list_attio_comments(record_id: str, object_slug: str = "people") -> str:
    """List internal collaboration comments on a person or company record in Attio.

    Args:
        record_id: The UUID of the record.
        object_slug: 'people' or 'companies'. Default 'people'.

    Note: Attio has NO ``GET /v2/comments`` list endpoint (it returns 404).
    Comments live inside *threads*, so this reads ``GET /v2/threads`` filtered
    by the record and flattens every thread's ``comments`` array. Verified
    live against the Attio API (Aug 2026).
    """
    try:
        # Attio requires querying threads by EITHER a record or an entry.
        params = {"record_id": record_id, "object": object_slug, "limit": 50}
        res = _req("GET", "/threads", params=params)
        threads = res.get("data", [])

        # Flatten comments out of every thread on this record.
        comments: list[dict[str, Any]] = []
        for thread in threads:
            for c in thread.get("comments", []) or []:
                comments.append(c)

        if not comments:
            return f"No comments found on {object_slug[:-1]} `{record_id}`."

        lines = [f"💬 Found {len(comments)} comment(s) on `{record_id}`:"]
        for c in comments:
            cid = c.get("id", {}).get("comment_id", "N/A")
            content = c.get("content_plaintext", c.get("content", ""))
            author = c.get("author", {}).get("type", "User")
            lines.append(f"- [{author}] {content} (ID: `{cid}`)")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ List comments failed: {e}"


@tool
@idempotent("create_attio_comment", key_args=["record_id", "content"])
def create_attio_comment(
    record_id: str,
    content: str,
    author_id: str,
    object_slug: str = "people",
) -> str:
    """Post an internal collaboration comment on a person or company record in Attio.

    Args:
        record_id: UUID of the target record.
        content: Comment text.
        author_id: UUID of the workspace member the comment should be
            posted as. Attio requires every comment to have an author —
            use list_workspace_members to find a valid ID.
        object_slug: 'people' or 'companies'. Default 'people'.
    """
    try:
        # Verified live (Aug 2026): Attio's POST /v2/comments requires the
        # parent record as a NESTED object — record: {object, record_id} —
        # NOT flat record_id/object keys at the data root (which fail with a
        # generic "data: Invalid input" validation error).
        payload = {
            "data": {
                "format": "plaintext",
                "content": content,
                "author": {"type": "workspace-member", "id": author_id},
                "record": {"object": object_slug, "record_id": record_id},
            }
        }
        res = _req("POST", "/comments", json_data=payload)
        data = res.get("data", {})
        cid = data.get("id", {}).get("comment_id", "N/A")
        return f"✅ Posted comment on {object_slug[:-1]} `{record_id}` (Comment ID: `{cid}`)."
    except Exception as e:
        return f"⚠️ Create comment failed: {e}"


# ==============================================================================
# 7. WORKSPACE MEMBERS (needed to resolve UUIDs for task assignees / comment authors)
# ==============================================================================

@tool
def list_workspace_members(limit: int = 25) -> str:
    """List workspace members in this Attio workspace, with their UUIDs.

    Use this to look up the actor ID needed for create_attio_task's
    assignees or create_attio_comment's author_id.

    Args:
        limit: Max members to return (default 25).
    """
    try:
        res = _req("GET", "/workspace_members")
        data = res.get("data", [])
        if not data:
            return "No workspace members found."

        lines = [f"👤 Found {len(data)} workspace member(s):"]
        for m in data[:limit]:
            mid = m.get("id", {}).get("workspace_member_id", "N/A")
            name = m.get("first_name", "") + " " + m.get("last_name", "")
            email = m.get("email_address", "")
            lines.append(f"- **{name.strip() or 'Unknown'}** ({email}) | ID: `{mid}`")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ List workspace members failed: {e}"


# ==============================================================================
# EXPORT TOOL REGISTRY
# ==============================================================================

ATTIO_TOOLS = [
    # Search & Query (6)
    search_attio_records,
    query_attio_records,           # ← now supports filter_json + richer output
    list_object_attributes,        # ← discover real attribute slugs on one object
    find_status_attribute,         # ← NEW: locate a status value across people/deals/lists
    get_attio_record,
    get_attio_record_interactions, # ← NEW: last-contacted / interaction date
    # Records & Companies CRUD (4)
    create_attio_person,
    create_attio_company,
    update_attio_record,
    delete_attio_record,
    # Lists & Pipelines (5)
    list_attio_lists,
    create_attio_list,
    get_attio_list_entries,
    add_attio_list_entry,
    delete_attio_list_entry,
    # Notes (3)
    list_attio_notes,
    create_attio_note,
    delete_attio_note,
    # Tasks (4)
    list_attio_tasks,
    create_attio_task,
    update_attio_task_status,
    delete_attio_task,
    # Comments (2)
    list_attio_comments,
    create_attio_comment,
    # Workspace members (1)
    list_workspace_members,
]