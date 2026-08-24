# API Endpoint Verification Report

**Date:** 2026-08-15
**Scope:** Verified the real API endpoints used by IRIS tool suites (Jira, Slack,
Attio, Google) against the **live APIs** using the tokens in `.env`, plus the
installed client-library source. Read-only / self-cleaning probes were used
throughout (any test data created was deleted immediately).

---

## Summary

| Suite  | Status | Action |
|--------|--------|--------|
| **Attio** | 🔴 2 real bugs found | ✅ **FIXED & re-verified live** |
| **Jira**  | 🟡 legacy endpoint removed upstream | ✅ Confirmed the code path still works (library auto-migrated) |
| **Slack** | 🟢 endpoints correct | ⚠️ Some OAuth scopes missing (config, not code) |
| **Google**| 🟢 endpoints correct | No change needed |

---

## 1. Attio (`attio_crm_tools.py`) — FIXED

Token validated via `GET /v2/self` (scopes include `comment:read-write`,
`note:read-write`, `task:read-write`, etc.). Objects confirmed: `people`,
`companies`. Attributes, lists, tasks, notes, and workspace_members payload
shapes all matched the code.

### Bug 1 — `list_attio_comments` hit a non-existent endpoint
- **Before:** `GET /v2/comments` → **HTTP 404** `Could not find endpoint "GET /v2/comments"`.
- **Root cause:** Attio has no comments *list* endpoint; comments live inside **threads**.
- **Fix:** now calls `GET /v2/threads?record_id=&object=` and flattens each
  thread's `comments[]` array. Parses `author.type` (was the wrong
  `created_by_actor.type`).
- **Re-verified live:** returns real comments. ✅

### Bug 2 — `create_attio_comment` used the wrong payload shape
- **Before:** flat `data.record_id` + `data.object` → **HTTP 400** `data: Invalid input`.
- **Fix (confirmed by self-cleaning create/delete round-trip):** parent must be a
  **nested** object:
  ```json
  {"data": {
    "format": "plaintext",
    "content": "...",
    "author": {"type": "workspace-member", "id": "<member_uuid>"},
    "record": {"object": "people", "record_id": "<record_uuid>"}
  }}
  ```
- **Re-verified live:** comment created (HTTP 200) and cleaned up. ✅

### Verified correct (no change needed)
- `POST /v2/objects/{people|companies}/records/query` (search/query)
- `POST /v2/objects/{slug}/records` (create person/company)
- `PATCH`/`DELETE /v2/objects/{slug}/records/{id}`
- `GET`/`POST /v2/lists`, `POST /v2/lists/{id}/entries[/query]`
- `GET`/`POST`/`DELETE /v2/notes`
- `GET`/`POST`/`PATCH`/`DELETE /v2/tasks` — required fields
  (`content`, `format`, `deadline_at`, `is_completed`, `linked_records`,
  `assignees`) confirmed by the validator. ✅
- `GET /v2/workspace_members`

---

## 2. Jira (`jira_tools.py`) — OK (upstream endpoint migration handled)

- `GET /rest/api/3/myself` → 200.
- ⚠️ **`GET /rest/api/3/search` → HTTP 410 GONE**: *"The requested API has been
  removed. Please migrate to the /rest/api/3/search/jql API."*
- ✅ `GET /rest/api/3/search/jql` (the replacement) → 200.
- **Why the code still works:** the installed `atlassian-python-api` `Jira.jql()`
  detects Jira Cloud and **auto-delegates to `enhanced_jql()`**, which calls the
  new `/search/jql` endpoint (verified by reading the library source and by a
  live `.jql()` call that returned issue `AAET-1`).
- **End-to-end:** `search_jira_issues` tool returned a real result. ✅
- `GET /rest/api/3/issue/createmeta/{project}/issuetypes` → 200 (used by
  create/subtask logic). ✅

> Recommendation: keep `atlassian-python-api` reasonably current — the working
> behaviour depends on the library's built-in migration to `enhanced_jql`. If
> you ever pin an older version, `.jql()` would hit the now-removed endpoint and
> return 410. Avoid passing `start>0` to `.jql()` on Cloud (the library raises).

---

## 3. Slack (`slack_tools.py`) — OK (verify workspace scopes)

- `auth.test` → 200 (bot `iris_ai`, team `Agentic AI software ltd`).
- All tools use `slack_sdk` `AsyncWebClient` with **correct official method
  names** (`chat_postMessage`, `conversations_*`, `reactions_*`, `pins_*`,
  `files_upload_v2`, etc.).
- **Granted scopes:** `chat:write`, `channels:history/read/join`,
  `groups:history`, `im:*`, `reactions:*`, `users:read[.email]`, `files:read/write`,
  `channels:write.topic`, `channels:write.invites`, `chat:write.public`, …
- ⚠️ **Missing scopes** for some tools (add in Slack app config if those tools
  are needed):
  - `pins:write` → `pin_slack_message` / `unpin_slack_message`
  - `pins:read` → `list_slack_pins`
  - `channels:manage` / `groups:write` → `create_slack_channel`,
    `set_*_topic/purpose` on private channels
  - `chat:write.customize` is present (good for custom username/icon).

---

## 4. Google (`google_*_tools.py`, `gmail_tools.py`) — OK

- Auth via `google-api-python-client` discovery build with correct API
  versions: **gmail v1, calendar v3, forms v1, drive v3**
  (`google_auth.py`). Because the client resolves endpoints from Google's
  discovery documents, method/param names are the surface to verify — and they
  match the official APIs:
  - Calendar: `events().insert/list/get/patch/delete`, `freebusy().query` ✅
  - Forms: `forms().create/get/batchUpdate`, `forms().responses().list/get` ✅
  - Drive: `files().list/create/get/update/delete/export_media/get_media`,
    `permissions().create/list/delete` with `supportsAllDrives` ✅
  - Gmail address: sending via Resend/SMTP + reading via IMAP (not the Gmail REST
    API) — credentials-based, endpoints are standard `smtp.gmail.com:465` /
    `imap.gmail.com:993`. ✅

> Note: Google send/read here intentionally uses Resend + SMTP/IMAP rather than
> the Gmail REST API. That's a valid design choice; the OAuth `gmail.*` scopes in
> `google_auth.py` are only needed if you later switch to the Gmail REST API.

---

## Reusable diagnostic

`scratch/verify_apis.py` was kept — run it any time to re-check live
connectivity and token scopes:

```
python scratch/verify_apis.py all      # attio + jira + slack
python scratch/verify_apis.py attio    # single suite
```
