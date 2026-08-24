# agent.md — IRIS Self-Improvement & Experience Log

> **IRIS writes here. Subagents do not.** Append `[GUARDRAIL E-XX]` entries only — one per novel failure/invariant.
> **Read order = recency:** most recent, highest-priority guardrails are at the **BOTTOM**. Read bottom-up before every synthesis; the last entries have the strongest claim on attention.

---

## ⛔ Write Rules
- IRIS writes ONLY via `write_file("agent.md", ...)`. Subagents write to `skills/<domain>/SKILL.md` — never here.
- Write BEFORE synthesis, never batched. Each entry = an executable decision rule, not a reminder.
- Append newest at the BOTTOM (recency order). Keep entries compressed & high-signal (no transcripts/HTML/tokens).
- Schema per entry: `Failure → Root Cause → Invariant (MUST/MUST NOT) → Recovery → Scope`.

---

## Active Guardrails — OLDEST FIRST, NEWEST/HOTTEST LAST ⬇

`E-21` · Compound task → ~20 duplicate Forms. **Cause:** worker yielded partial; IRIS redispatched whole task. **MUST:** own sequencing — one atomic op per task, pass verified IDs forward; NEVER redispatch a compound task on partial output. **Scope:** delegation engine / resource creation.

`E-26` · IRIS claimed it "can't" do a supported op, offered local `write_file`, then reached for an unauthorized general-purpose subagent. **Cause:** confused own tool boundary with system capability. **MUST:** IRIS tool boundary ≠ system capability — if Grace/Maya/Sienna/Aurther/Tavia supports it, route via `task()`; NEVER claim incapability or write local fallbacks for cloud ops. **Scope:** intent routing.

`E-01` · `#sales-updates` channel not found. **Cause:** channel absent/renamed in workspace. **MUST:** resolve channel via `list_slack_channels` before any send; fall back to an existing public channel and flag the mismatch (never silently post to nearest name). **Scope:** Slack channel governance.

`E-27` · Empty first turn after a multi-step request → tripped FC-5 before any work. **Cause:** IRIS "thought" silently instead of acting. **MUST:** first turn on a multi-step request is ALWAYS an action — `get_current_datetime()` + `write_todos()`; silent reasoning is not a turn. On any FC-5 nudge, immediately emit the next pending step — do NOT apologize/narrate. **Scope:** execution loop / first-turn.

`E-28` · HITL preview was faked via an extra "show me for approval" `task()`, then "yes" needed a SECOND `task()` to actually create the event — 2 delegations, 1 gated action. **Cause:** simulated the approval in a worker instead of the structural `interrupt_on` pause; risks preview↔execution drift. **MUST:** delegate the real gated tool (create/send/cancel) EXACTLY ONCE and let `interrupt_on` surface the true pending args; NEVER fabricate a preview or re-dispatch to obtain one (FC-8). On approval, resume the same call — don't issue a new creation task. **Scope:** HITL enforcement — calendar/email/Slack/Drive gates.

`E-29` · Multi-domain research synthesis without a unified architecture document → fragmented output. **Cause:** completed research subtasks but didn't produce a single cohesive implementation guide. **MUST:** after all research subtasks complete, synthesize a unified architecture document covering: component diagram, data flow, tech stack, API contracts, deployment model, and code scaffolding — before marking the overall task complete. **Scope:** research synthesis / final deliverable.

`E-30` · Offered Google Drive upload via Grace without verifying actual Drive tool availability in the specialist's toolkit. **Cause:** assumed "Google Workspace" = full Drive API including file upload/share; didn't check the skill's actual tool surface. **MUST:** verify specialist tool capabilities against the skill file before offering cloud operations; NEVER assume broader capability than documented. **Scope:** intent routing / capability verification.

`E-31` · **Hallucinated model currency** — confidently claimed "Opus and Sonnet are current flagship models" without verifying. **Cause:** treated research output (survey citing "Opus and Sonnet") as ground truth for model versions, rather than as "what sources said at that time." In fast-moving domains (AI models), survey citations ≠ current reality. **MUST:** timestamp every model/tool version in research outputs; NEVER defend unverified claims — "let me check" is the only correct first response to a challenge; proactively re-verify time-sensitive facts (model releases, pricing, versions) before synthesizing. **Scope:** research verification / technical accuracy / trust.

`E-32` · **[RESOLVED 2026-08-22] Grace CAN create Gmail drafts** — `create_gmail_draft` is a real tool in Grace's toolset (verified: `email_tools` in `gmail_tools.py`; Grace built with 45 Google Workspace tools). Route "create/draft a Gmail" requests to Grace directly; do NOT fall back to copy-paste text. The tool writes to the operator's own Drafts via IMAP APPEND — it delivers to no one, so it is intentionally NOT a HITL-gated action (only `send_research_email` / `schedule_research_email` are gated). **Original (now-false) claim:** "Grace cannot create Gmail drafts." **Standing MUST:** the E-30/E-32 lesson still holds in spirit — verify a specialist's actual tool surface before promising a capability; the tool surface simply changed. **Scope:** intent routing / capability verification / email workflows.

---
### 🔥 MOST RECENT — apply these first (highest attention)

`E-32` · **[RESOLVED 2026-08-22] Grace CAN create Gmail drafts** — `create_gmail_draft` is a real tool in Grace's toolset (45 Google Workspace tools). Route draft requests to Grace; drafting is not HITL-gated (send/schedule are). Supersedes the earlier "Grace cannot create Gmail drafts" note. **Scope:** intent routing / capability verification / email workflows.

`E-31` · **Hallucinated model currency** — confidently claimed "Opus and Sonnet are current flagship models" without verifying. **Cause:** treated research output (survey citing "Opus and Sonnet") as ground truth for model versions, rather than as "what sources said at that time." In fast-moving domains (AI models), survey citations ≠ current reality. **MUST:** timestamp every model/tool version in research outputs; NEVER defend unverified claims — "let me check" is the only correct first response to a challenge; proactively re-verify time-sensitive facts (model releases, pricing, versions) before synthesizing. **Scope:** research verification / technical accuracy / trust.

---
### 📝 USER CONTEXT (for future reference)

**User Email:** emryzekanem@gmail.com
**Saved:** 2026-08-20
**Purpose:** Future communications, buildfest notifications, project updates

**Friend Contact — Jennifer:** jenniferudo3@gmail.com
**Saved:** 2026-08-22
**Purpose:** Personal contact for future reference

---
### 📋 ANTHROPIC PROSPECTING WORKFLOW — 2026-08-22

**Completed end-to-end prospecting workflow for Anthropic as potential 10alytics client:**

**1. Research (Tavia):**
- Latest funding: Series H, May 2026, $65B at $965B post-money (Altimeter, Dragoneer, Greenoaks, Sequoia); Series G Feb 2026: $30B at $380B (GIC, Coatue)
- Headcount: ~5,000 (Tracxn, Mar 2026) / ~2,300 (GetLatka, 2026)
- 3 notable developments (Feb-Aug 2026):
  1. Jun 11: TCS Global Premier Partnership — deploying Claude to 50K TCS associates
  2. Jun 9: Launched Claude 3.5 Sonnet, rival to GPT-4o
  3. Apr 17: Released Claude Design, collaborative visual creation tool powered by Claude Opus 4.7
- Sources: Tracxn, Intellizence, SaaStr, GetLatka, TCS press release, Lucidity Insights, Wikipedia

**2. Key Leader Identified (Tavia):**
- Eric Boyd, Head of Infrastructure (since Apr 2026, ex-Microsoft President)
- Sources: Crunchbase, GeekWire, Pulse 2.0, Datacenter Dynamics
- No higher-ranking VP Data/Analytics/ML-Infrastructure leader found publicly; company actively hiring for such roles

**3. Attio Records (Aurther):**
- Company: Anthropic (UUID: 821fc32a-c051-4d39-986e-887bfbfe6850) — FOUND existing
- Person: Eric Boyd (UUID: 964a478e-233d-4ddc-b9a7-674394a135e8) — CREATED, linked to Anthropic
- Research note logged on company record (Note ID: d50d5503-9920-421d-8bff-daa153a0517f)

**4. Jira Issue (Maya):**
- Issue: AAET-81 — "Outreach: Anthropic — analytics partnership"
- Type: Task, Priority: High, Labels: outreach, prospecting, anthropic
- URL: https://emryzekanem-1786405145451.atlassian.net/browse/AAET-81

**5. Email Draft (Grace — NOT SENT):**
- Drafted intro email to Eric Boyd proposing 20-min call next week
- Content provided for manual copy-paste (Grace cannot create Gmail drafts — E-32)

**6. Slack Announcement (Sienna):**
- Posted 3-line summary to #announcements channel
- Permalink: https://slack.com/archives/C0BPDAG4MU4/p/1787387010835479

**PENDING APPROVAL:** None — all actions completed without HITL gates (email was drafted only, not sent)

**BLOCKED:** None

**LEARNING:** [CORRECTED 2026-08-22] The original "E-32 confirmed — Grace cannot create Gmail drafts" conclusion was FALSE. Grace exposes `create_gmail_draft` (verified in `email_tools`/`gmail_tools.py`; 45 Google Workspace tools). Draft requests should be routed to Grace; drafting is not HITL-gated. See revised E-32.

---
### 📋 EDTECH COMPETITIVE INTEL WORKFLOW — 2026-08-23

**Completed end-to-end competitive intelligence workflow for EdTech sector (Q3 2026):**

**1. Research (Tavia):**
- Top 3 competitors by revenue: Duolingo, Coursera, Pearson
- Duolingo: $30M funding (Jul 2026, unicorn), Duolingo Max AI tier ($30/mo with Video Call/Roleplay), Animade acquisition (Aug 13, 2026)
- Coursera: Udemy all-stock merger (May 11, 2026), $100M LearnVector AI investment (Jul 28, 2026), $50k median ACV
- Pearson: $4.5B revenue, no verifiable Jul-Aug 2026 product/pricing/funding updates
- Industry trends: $205B market (CAGR 12-14%), AI as table stakes, PE consolidation, LMS consolidation (Canvas ~50%), emerging markets (India/Africa), corporate training tech shift ($100B), tiered AI pricing standard
- 9 verified sources with inline citations

**2. Google Sheets (Grace):**
- Spreadsheet: "Competitive Analysis — Q3 2026"
- ID: 1ZkCV4Q_AAD2rGZv9Etc3qbsRZt0-s8ZOfaSwv5bfbKg
- URL: https://docs.google.com/spreadsheets/d/1ZkCV4Q_AAD2rGZv9Etc3qbsRZt0-s8ZOfaSwv5bfbKg/edit
- 4 tabs: Duolingo, Coursera, Pearson, Industry Trends — all populated per spec

**3. Google Doc (Grace):**
- Doc: "Competitive Intel Brief — August 2026"
- ID: 125PGNVnroXVobjKLIFQyNAt88avjg_9iRr4W6zSIzNQ
- URL: https://docs.google.com/document/d/125PGNVnroXVobjKLIFQyNAt88avjg_9iRr4W6zSIzNQ/edit?usp=drivesdk
- Full executive summary with competitor deep dives, trend implications, recommended actions, source references

**4. Slack Announcement (Sienna):**
- Channel: #uyo-activities (C0BP3DWGBKM)
- Permalink: https://aabass-ai001.slack.com/archives/C0BP3DWGBKM/p1787440220034439
- Posted doc + sheet links with key highlights

**5. Jira Task (Maya):**
- Issue: AAET-86 — "Review Competitive Intel Brief"
- Type: Task, Assignee: Abasi-ikpongke Ekanem, Due: 2026-08-26
- Labels: competitive-intel, edtech, q3-2026, review
- URL: https://emryzekanem-1786405145451.atlassian.net/browse/AAET-86

**PENDING APPROVAL:** None — all actions completed without HITL gates

**BLOCKED:** None

**LEARNING:** Multi-domain workflow (Research → Sheets → Doc → Slack → Jira) executed cleanly with atomic subtasks, verified artifact handoffs, and no loops. Pattern confirmed: plan → ground time → delegate sequentially → persist artifacts → synthesize.

---
### 📋 LANGCHAIN STREAMING RESEARCH — 2026-08-23

**Research completed (Tavia):**
- 5 core streaming patterns identified with code examples and official source URLs
- Version evolution notes for LangChain 0.1.x → 0.2.x → 0.3.x
- 7 production best practices

**Google Workspace operations BLOCKED:**
- Grace requires `GOOGLE_REFRESH_TOKEN` in `.env` (OAuth setup via `get_google_refresh_token.py`)
- Document creation and email send could not proceed without authenticated session
- Research findings delivered directly to user instead

**LEARNING:** Verify Google Workspace auth state before promising cloud document/email operations. If auth is missing, complete research and deliver findings inline rather than blocking on unavailable cloud ops.

---
### 📋 AI CODING EDITORS RESEARCH — 2026-08-23

**Research completed (Tavia):**
- 12 AI coding editors/IDEs researched as of August 2026: Cursor, GitHub Copilot, Windsurf, Zed, Codeium, Continue.dev, Google Antigravity, Amazon Q Developer, Tabnine, Replit, Sourcegraph Cody
- Structured comparison table with pricing, features, differentiators
- 6 verified sources with inline citations

**Google Sheets creation BLOCKED:**
- Grace requires `GOOGLE_REFRESH_TOKEN` in `.env` (OAuth setup via `get_google_refresh_token.py`)
- Spreadsheet creation failed due to missing OAuth credentials
- Research findings posted directly to Slack #general channel instead

**Slack delivery COMPLETED:**
- Channel: #general (C0AUV4L0GKV)
- Permalink: https://app.slack.com/client/T03R7MC6Q/C0AUV4L0GKV/p1787499522.039559
- Full research summary with table and source URLs delivered

**LEARNING:** When Google Workspace auth is missing, complete research and deliver findings via available channels (Slack) rather than leaving the user with nothing. Note the auth requirement for future Google Sheet operations.

---
### 👤 CREATOR / DEVELOPER CONTEXT

**Built by:** Ekanem, Abasi-ikpongke (AI Agent Engineer)
**Framework:** LangChain (agent framework)
**Harness:** deepagents
**Runtime:** LangGraph
**Language:** Python
**Organization:** 10alytics
**Saved:** 2026-08-24
**Purpose:** Authoritative provenance record for IRIS orchestrator

---
### 🛡️ GUARDRAIL E-33 — TOOL FAILURE HANDLING & LOOP PREVENTION — 2026-08-24

**Failure:** Tool call cancelled/interrupted mid-execution (Slack post cancelled by incoming message); user perceived this as a "loop" or extended failure.

**Root Cause:** Platform-level interruption (incoming message cancels in-flight tool call) is not a tool failure, not a retry, and not a loop — but without explicit handling, it can be misread as one.

**Invariant (MUST/MUST NOT):**
- **MUST:** On any tool call that returns an error, cancellation, or blank result → mark the corresponding todo as `blocked` (or `failed` if terminal) via `write_todos` immediately. Do NOT re-dispatch the same brief.
- **MUST:** If a tool call is cancelled by the platform (interrupted), treat it as `blocked` — not a failure to retry. Surface the cancellation to the user and ask for direction (retry? different channel? skip?).
- **MUST NOT:** Ever re-dispatch an identical brief after a failure/cancellation (FC-8 / D-01 loop guard). One material retry with a changed brief is the maximum.
- **MUST:** If a loop is detected (same brief, same failure, second attempt), treat as **system failure** — stop, surface to user, do not continue autonomously.

**Recovery:**
1. Tool error/cancellation → `write_todos` update to `blocked`/`failed`
2. Report exact error/cancellation to user
3. Ask for direction (retry with modified params? different target? skip?)
4. Only proceed on explicit user confirmation

**Scope:** All tool delegation — `task()` calls, any domain tool execution, HITL-gated actions.

---
### 🛡️ GUARDRAIL E-34 — GOOGLE WORKSPACE AUTH VERIFICATION BEFORE PROMISING CLOUD OPS — 2026-08-24

**Failure:** Dispatched Google Doc creation and email send to Grace without verifying `GOOGLE_REFRESH_TOKEN` was configured. Grace returned blank results (no output) on both attempts — the OAuth credential is missing, so the tools cannot execute.

**Root Cause:** Assumed Google Workspace tools were available based on prior successful runs (EdTech workflow), but the auth token may have expired or the environment differs. Did not verify auth state before promising cloud document/email delivery.

**Invariant (MUST/MUST NOT):**
- **MUST:** Before any Google Workspace delegation (Docs, Sheets, Drive, Gmail send/schedule), verify Grace can actually execute by checking for a recent successful Grace operation in the current session, or explicitly ask Grace to confirm auth status.
- **MUST NOT:** Promise Google Doc/Sheet creation or email delivery without confirmed auth.
- **MUST:** If auth is missing, complete the research/upstream work and deliver findings directly to the user (inline, Slack, or formatted text) — do not leave the user with nothing.
- **MUST:** Document the auth requirement clearly so the user knows what setup is needed for future cloud ops.

**Recovery:**
1. On blank Grace result → treat as auth failure, not tool failure
2. Deliver research/content directly to user in structured format
3. Explain the OAuth setup requirement: `GOOGLE_REFRESH_TOKEN` in `.env` via `get_google_refresh_token.py`
4. Offer Slack delivery as alternative (Sienna) if workspace is connected

**Scope:** All Google Workspace delegations — Doc, Sheet, Drive, Gmail send/schedule.