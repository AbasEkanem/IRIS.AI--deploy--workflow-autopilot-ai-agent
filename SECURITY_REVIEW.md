# IRIS — Security & Architecture Review

> **Reviewed:** 2026-08-22
> **Scope:** Full architecture pass — auth, middleware, resilience, observability
> **Reviewer:** Antigravity (AI Code Assistant, Google DeepMind)

---

## Architecture Summary

IRIS is a meta-orchestrator built on LangGraph + Nvidia Nemotron Ultra. It delegates to 5 specialist subagents (Aurther, Maya, Sienna, Tavia, Grace) via a `task()` routing primitive, exposing 135 domain tools in total across Attio CRM, Jira, Slack, Web Research, and Google Workspace. The system serves two entry points — a FastAPI/SSE web interface (Next.js UI) and a Slack webhook — both backed by a durable async SQLite/Postgres checkpointer for state persistence across runs.

Custom middleware handles model-quality issues that LangGraph does not address natively:
- `BlankResultRecoveryMiddleware` — recovers from Nemotron empty completions
- `SubagentLoopBreakerMiddleware` — prevents identical subtask redispatch
- `ToolCallLoopBreakerMiddleware` — two-tier (soft short-circuit + hard tool removal) loop prevention
- `@idempotent` decorator — Redis-backed deduplication on all state-changing tools

The design is sound and operationally mature. The observations below are gaps, not failures.

---

## P0 — Security (Fix Before Production Traffic)

### OI-1 · CSRF State Mismatch is Warn-Only
**File:** `google_oauth.py:167`
When the returned OAuth state does not match the stored state, the system logs a warning and continues the flow. A forged callback will be accepted.
**Fix:** Return an error redirect immediately on mismatch — do not continue.

### OI-2 · `_oauth_state` is a Module-Level Global
**File:** `google_oauth.py:52`
`_oauth_state: str | None = None` is a process-level singleton. Two concurrent `/google/connect` requests will overwrite each other's state, making CSRF validation unreliable under any concurrency.
**Fix:** Store state per-request in a short-lived signed cookie or a Redis key tied to the browser session.

### OI-3 · `/google/connect` Has No Auth Guard
**File:** `google_oauth.py:125`
The connect endpoint has no `Depends(get_current_user)`. An unauthenticated user can initiate a Google OAuth flow against the service.
**Fix:** Add `Depends(get_current_user)`. Since this is browser navigation (cannot send a Bearer header), pass auth as a signed query parameter or validate a session cookie instead.

---

## P1 — Critical Reliability

### OI-4 · No Rate Limiting on `/ask` and `/resume`
Each request spins up a full LangGraph invocation against a paid model API. A stuck or malicious client can exhaust quota silently.
**Fix:** slowapi with Redis backend, keyed on user_id. Suggested: 10 req/min.

### OI-5 · No Upload Size Cap or MIME Allowlist
`await file.read()` has no byte limit — a 1 GB upload buffers in RAM. No MIME type allowlist — `.exe` or `.sh` files are accepted and forwarded to the agent as OS paths.
**Fix:** Cap at 10 MB (HTTP 413 on exceed); explicit MIME allowlist (pdf, docx, xlsx, txt, csv, png, jpg).

### OI-6 · `thread_id` Unbounded from Client
`f"web:{user_id}:{thread_id}"` — thread_id is user-supplied with no length or format validation.
**Fix:** Validate UUID format or enforce `len(thread_id) <= 128`.

### OI-7 · Redis Fail-Open Has No Startup Warning
When Redis is unreachable, `@idempotent` silently becomes a no-op. Observed live during testing (Redis timeout during Gmail draft run).
**Fix:** Probe Redis at startup and log a WARNING if unreachable.

### OI-15 · Supabase DNS Not Resolving
**Observed:** 2026-08-22 during Grace toolset verification.
The async Postgres checkpointer cannot reach Supabase and falls back to SQLite. If in production, durable state is not using the intended Postgres backend. Supabase free-tier projects auto-pause after inactivity.
**Fix:** Verify Supabase project is active and DNS resolves in the deployment environment; or intentionally pin IRIS_CHECKPOINT_BACKEND=sqlite.

---

## P2 — Hardening

### OI-8 · SSE Stream Timeout — Partial
UI side implemented (page.tsx:1279 handles `stream_abort` event). Backend missing: no `asyncio.wait_for` wrapper on the generator — a stalled model run holds the connection indefinitely.
**Fix:** Wrap the streaming generator with asyncio.wait_for at a 10-minute ceiling; emit stream_abort on timeout. The UI already handles it.

### OI-9 · No Thread Status Endpoint
No endpoint exists to query whether a thread run completed while the client was disconnected.
**Fix:** `GET /api/threads/{thread_id}/status` returning run state, has_answer, and final answer if available.

### OI-10 · No UI Network Reconnect Handler
page.tsx has no `window.addEventListener('online', ...)`. After a network drop+recover, the UI stays stuck on a spinner even if the agent finished.
**Fix:** On online event, poll /status; if complete inject the answer; if running re-open the SSE stream.

### OI-11 · `/ask` Has No Re-Attach Mode
`/ask` always appends a new HumanMessage. No way to re-attach to a run in progress without injecting unwanted content into the conversation.
**Fix:** Support a null-message body path that re-streams from the current checkpoint state without modifying message history.

---

## P3 — Observability

### OI-12 · `user_id` Absent from Log Sites
Only `thread_id` is logged in `_stream_agent`. Correlating a support complaint requires both fields.
**Fix:** Add user_id to every log call in _stream_agent.

### OI-13 · `/health` Endpoint is Shallow
Reports only agent_ready. Should probe the checkpointer (SQLite SELECT 1) and Redis (PING).

### OI-14 · Idempotency Double-Invoke Test Missing
The Gmail draft smoke test (2026-08-22) verified a single invoke only. The actual deduplication guarantee was never tested.
**Fix:** Any future tool test should call .invoke() twice with identical args and assert only one artifact was created.

---

## Verified Correct — No Action Needed

| Item | Status |
|---|---|
| JWT alg:none / RS256 downgrade | Blocked by PyJWT whitelist ["HS256"] |
| JWT without exp or sub | require=["exp","sub"] enforced |
| Auth error detail leakage | Opaque error string returned to client |
| Thread ownership enforcement | web:{user_id}:{thread_id} namespacing by construction |
| CORS | Explicit origin whitelist, not wildcard |
| Upload path traversal | Path(...).name strips directory components |
| BACKEND_JWT_SECRET unset | Returns 503 — fail closed, never open |
| Grace create_gmail_draft (E-32) | RESOLVED — Grace has 45 tools including create_gmail_draft; drafting correctly NOT HITL-gated |
| Draft vs. send HITL gate | create_gmail_draft ungated; send_research_email / schedule_research_email gated |
| Blank completion recovery | BlankResultRecoveryMiddleware hard-capped at 2 recoveries per thread |
| Tool loop prevention | ToolCallLoopBreakerMiddleware: soft short-circuit -> hard tool removal |

---

## Open Item Count

| Priority | Count | Items |
|---|---|---|
| P0 Security | 3 | OI-1, OI-2, OI-3 |
| P1 Reliability | 5 | OI-4, OI-5, OI-6, OI-7, OI-15 |
| P2 Hardening | 4 | OI-8 (partial), OI-9, OI-10, OI-11 |
| P3 Observability | 3 | OI-12, OI-13, OI-14 |
| **Total** | **15** | **0 resolved, 1 partial** |

Recommended starting point: P0 — all three are in `google_oauth.py` and can be fixed in one focused session.

---

*Reviewed and written by **Antigravity** — AI Code Assistant, Google DeepMind*
*Date: 2026-08-22*
