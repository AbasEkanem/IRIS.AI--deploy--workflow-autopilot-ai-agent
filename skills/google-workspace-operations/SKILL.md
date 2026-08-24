---
name: google-workspace-operations
description: >
  Prerequisites and failure recovery for Google Workspace tools — Gmail, Calendar,
  Forms, Sheets, Drive, Docs — including OAuth refresh-token setup.
---

# Google Workspace Operations — SOP & Guardrails

> **Executor:** Grace | **Tools:** 45 Google Workspace tools (Gmail 6, Calendar 7, Forms 9, Sheets 7, Drive 14, Docs 2)

---

# ⛔ HARD GUARDRAILS & ERROR RECOVERY MATRIX (P0)

Each entry below is a real failure and the invariant that prevents it.

[GUARDRAIL E-25] Failure: Google Doc creation failed due to missing GOOGLE_REFRESH_TOKEN in .env | Root Cause: OAuth2 credentials not configured for the Google Workspace integration | Binding Invariant: Must run get_google_refresh_token.py one-time OAuth flow and add GOOGLE_REFRESH_TOKEN to .env before any Drive/Docs/Sheets/Forms/Calendar/Gmail tools can execute
