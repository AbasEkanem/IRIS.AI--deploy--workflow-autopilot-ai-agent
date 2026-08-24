---
name: slack-communication-protocol
description: >
  Binding execution invariants for Slack operations — channel/user ID resolution, thread
  reply parentage, and loop-guard limits on repeated identical calls — derived from
  observed runtime failures.
---

# Slack Communication Protocol — SOP & Guardrails

> **Executor:** Sienna | **Tools:** 30 Slack tools (Messaging, Threads, Channels, Pins, Reactions, Files)

---

# ⛔ HARD GUARDRAILS & ERROR RECOVERY MATRIX (P0)

Each entry below is a real failure and the invariant that prevents it.

[GUARDRAIL E-01] Failure: Repeated calls to get_slack_message_permalink on same target (timestamp 1787520128.980649 in C0BPDAG4MU4) triggered loop guard and disabled the tool. Root Cause: Tool was called 3+ times with identical arguments without progress. Binding Invariant: Message permalink retrieval must complete in a single call; repeated calls trigger loop guard and disable the tool for the remainder of the turn.
