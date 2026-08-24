---
name: runtime-reliability
description: >
  Operational guardrails for system stability, anti-hallucination, payload shape safety,
  positional index clamping and graceful failure recovery.
---

# Runtime Reliability & Payload Safety — SOP & Guardrails

> **Target:** System-wide runtime reliability & tool interaction safety

---

# ⛔ HARD GUARDRAILS & PAYLOAD SAFETY MATRIX (P0)

| ID | Issue / Danger | Binding Invariant & Code Pattern |
|---|---|---|
| **E-08** | Fabricated IDs, keys, URLs | Ground every resource ID, URL, timestamp, and status strictly in actual tool return values. Never guess or extrapolate. |
| **E-14** | Delegation loop recycling | If a specialist reports missing tools or capability errors, terminate delegation for that domain immediately. Never re-dispatch unchanged. |
| **E-25** | Host socket resets (WinError 10053) | Transient socket drops during Drive uploads or API calls. Use bounded exponential backoff retries via `ToolRetryMiddleware` & `_retry_media`. |

---

# ⚡ CORE RELIABILITY DIRECTIVES

1. **Action-First Discipline:** Invoke tools immediately when needed. Avoid verbose conversational filler or speculative chain-of-thought dumps.
2. **Task Result Validation (E-16):** Accept delegated worker output only when it contains `STATUS`, `SUMMARY`, and verified `ARTIFACTS`. Treat blank or un-structured output as `FAILED`.
3. **HITL Outbound Safety:** Require explicit user approval before executing high-stakes irreversible actions (email send, public file sharing, Jira status close, Attio/Drive record deletion).
4. **Live Reliability Harness:** The round-trip test harness `scratch/test_google_jira_full.py` validates the Google Workspace (45) + Jira (29) tools end-to-end. Every failure recorded is a binding avoidance directive.
