---
name: memory-management
description: >
  Operational rules for memory partitioning, experience persistence, namespace isolation,
  and incident post-mortem recording across IRIS and specialist subagents.
---

# Memory Management & Persistence — SOP & Guardrails

> **Target:** System memory architecture & experience persistence rules

---

# ⛔ HARD MEMORY PARTITIONING RULES (P0)

| Owner | Memory Target | Permitted Writes | Forbidden Writes |
|---|---|---|---|
| **IRIS Orchestrator** | `IRIS.md` | Master architecture, workflow design | Specialist domain API quirks |
| **IRIS Orchestrator** | `agent.md` | System-wide orchestration errors, delegation failures, runtime guardrails | Domain-specific API quirks |
| **Specialist Subagents** | `/skills/<domain>/SKILL.md` | Domain API quirks, status slug mismatches, recovery rules | `agent.md` or `IRIS.md` |

---

# ⚡ CORE OPERATIONAL RULES

1. **Write Target Isolation:** Subagents MUST write experience notes strictly to their own `/skills/<domain>/SKILL.md`. Subagents MUST NOT write to `agent.md` or `IRIS.md`.
2. **Persist Before Synthesis — append-safe, two steps:** Newly discovered learnings MUST be persisted **before** concluding the task or synthesizing final output, and MUST be *appended*, never overwritten:
   1. `read_file(<target>)` — get the CURRENT full contents of the memory file.
   2. `write_file(<target>, <those exact contents> + "\n" + <new entry>)`.

   `write_file` REPLACES the entire file. Calling it with only the new entry **deletes everything else in that file**, including the `---` YAML frontmatter block a `SKILL.md` needs in order to be loaded at all — a skill whose frontmatter is gone is silently deactivated with no error (`E-34` below). If you have not just read the file, you may not write it. The harness memory guidelines suggest `edit_file` for persistence; **this two-step pattern outranks them** and is the only sanctioned path here.
3. **Guardrail Schema:** Format all operational learnings using:
   ```
   [GUARDRAIL E-XX] Failure: ... | Root Cause: ... | Binding Invariant: ...
   ```
4. **Information Hygiene:** Store only structured, high-signal information. Never write raw HTML, verbose transcripts, or ephemeral tokens into memory.

---

# ⛔ HARD GUARDRAILS (P0)

[GUARDRAIL E-34] Failure: A `SKILL.md` lost its `---` YAML frontmatter block and stopped loading — silently, with no error — so the skill's guardrails were absent from every later run. Root Cause: an experience note was persisted with a bare `write_file(<skill path>, <just the new entry>)`; `write_file` has replace-whole-file semantics, so the frontmatter and every prior entry were discarded. Binding Invariant: NEVER call `write_file` on a memory or `SKILL.md` target without immediately preceding it with `read_file` on that same path and re-writing the full prior contents plus the appended entry. The `---` frontmatter block (`name`, `description`) is load-bearing — a `SKILL.md` missing it is deactivated, not degraded.
