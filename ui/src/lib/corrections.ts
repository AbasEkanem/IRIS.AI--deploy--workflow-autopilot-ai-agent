/* ══════════════════════════════════════════════════════════════════
   corrections — identity and display copy for every harness steering message
   ──────────────────────────────────────────────────────────────────
   Mirrors guardrail_taxonomy.py. Keep the two in sync.

   IRIS's guardrails steer the orchestrator by injecting messages into graph
   state. They MUST be persisted — that is what keeps a run self-correcting
   across turns — but persistence means they also come back out through
   /history and the SSE bridge, and most of them are `HumanMessage`s. The
   backend maps any HumanMessage to role:"user", so an unrecognised nudge
   renders as the user's own `EE` bubble. This module is what tells them apart.

   Previously only 2 of the ~15 were recognised, so eleven kinds of internal
   steering appeared in the transcript as things the user had typed.
   ══════════════════════════════════════════════════════════════════ */

/* Legacy named exports — still referenced by tmp/test_corrections.ts and by
   blank_recovery.py's own constants. Do not rename. */
export const BLANK_TASK_SOURCE = "iris_blank_result_recovery";
export const EMPTY_COMPLETION_SOURCE = "iris_empty_completion_recovery";

/** U+26A0 U+FE0F + " LOOP GUARD " + U+2014 — loop_breaker.py:161, :177, :348. */
export const LOOP_GUARD_PREFIX = "⚠️ LOOP GUARD — ";
/** Written by the loop terminator's state marker — loop_breaker.py. */
export const LOOP_TERMINATOR_PREFIX = "⛔ LOOP TERMINATOR — ";
/** The per-turn cap on total `task` dispatches — loop_breaker.py:195. Same shape as
    the LOOP GUARD messages (unnamed ToolMessage, status:"success"), so only this
    prefix tells it apart. It was unregistered on both sides until now, so a harness
    instruction ("you have already dispatched N subtasks…") rendered as a user bubble. */
export const DISPATCH_BUDGET_PREFIX = "⚠️ DISPATCH BUDGET — ";

export type CorrectionSeverity = "info" | "warn";

export interface CorrectionCopy {
  /** Headline shown collapsed. Reads as something IRIS did, not an error code. */
  title: string;
  /** One-line explanation shown under the title while collapsed. */
  sub: string;
  severity: CorrectionSeverity;
}

/* Source `name` → display copy. The keys are the exact strings the middleware
   sets, so this table doubles as the recognition set. Ordered as in
   guardrail_taxonomy.py: IRIS's own guards first, then the profile's. */
const BY_SOURCE = {
  iris_blank_result_recovery: {
    kind: "blank_result",
    title: "IRIS caught a blank subtask result and kept going",
    sub: "Self-correction — retried or advanced instead of treating the blank as done.",
    severity: "warn",
  },
  iris_empty_completion_recovery: {
    kind: "empty_completion",
    title: "IRIS caught an empty response and kept going",
    sub: "Self-correction — steered back to finish the task instead of stopping.",
    severity: "warn",
  },
  iris_resume_context: {
    kind: "resumed",
    title: "IRIS resumed after an interruption",
    sub: "Told to continue from the next incomplete step, not repeat completed work.",
    severity: "info",
  },
  iris_loop_terminator: {
    kind: "loop_terminated",
    title: "IRIS disabled a looping tool and wrapped up",
    sub: "A tool was called repeatedly without progress, so it was taken away for this turn.",
    severity: "warn",
  },
  iris_toolcall_repair: {
    kind: "toolcall_repaired",
    title: "IRIS caught a tool call printed as text and re-issued it",
    sub: "The model wrote the call out as JSON instead of running it, so it was sent back to be issued properly.",
    severity: "warn",
  },
  iris_todo_reconcile: {
    kind: "todo_reconciled",
    title: "IRIS caught an unfinished plan and closed it out",
    sub: "The run was ending with planned steps still open, so it had to finish them or mark them done.",
    severity: "warn",
  },
  nemotron_transition_nudge: {
    kind: "new_task",
    title: "IRIS was nudged to treat this as a new task",
    sub: "Stops the previous turn's plan bleeding into an unrelated request.",
    severity: "info",
  },
  nemotron_action_commit_nudge: {
    kind: "act_now",
    title: "IRIS was nudged to perform the action, not describe it",
    sub: "Caught an answer that narrated the work instead of doing it.",
    severity: "info",
  },
  nemotron_tool_chain_nudge: {
    kind: "finish_chain",
    title: "IRIS was nudged to finish the follow-on action",
    sub: "A chained step was left undone after the first tool succeeded.",
    severity: "info",
  },
  nemotron_domain_tool_nudge: {
    kind: "prefer_domain_tool",
    title: "IRIS was steered toward a domain tool",
    sub: "A file search came back empty, so the real integration was suggested instead.",
    severity: "info",
  },
  nemotron_domain_tool_preference: {
    kind: "avoid_filesystem",
    title: "IRIS was steered away from filesystem tools",
    sub: "Request-only nudge — never persisted, so it appears live only.",
    severity: "info",
  },
  nemotron_filesystem_request_nudge: {
    kind: "use_filesystem",
    title: "IRIS was steered toward filesystem tools",
    sub: "Request-only nudge — never persisted, so it appears live only.",
    severity: "info",
  },
  nemotron_followup_guard: {
    kind: "followup_rewritten",
    title: "IRIS rewrote a vague follow-up",
    sub: "An ambiguous reference was resolved before acting on it.",
    severity: "warn",
  },
  nemotron_entity_guard: {
    kind: "entity_resolution",
    title: "IRIS was told to resolve each entity properly",
    sub: "Guards against answering with opaque record IDs instead of real names.",
    severity: "info",
  },
  nemotron_final_answer_guard: {
    kind: "final_answer_checked",
    title: "IRIS caught a problem in its final answer",
    sub: "The answer was missing concrete values or a real outcome, and was redone.",
    severity: "warn",
  },
  nemotron_progress_budget: {
    kind: "budget_exhausted",
    title: "IRIS hit the harness step budget",
    sub: "The run was stopped short and summarised — this text is never used as the answer.",
    severity: "warn",
  },
  /* Not a `name` — classified from the content prefix. Present here so it shares
     one copy table with the rest. */
  loop_guard: {
    kind: "loop_blocked",
    title: "IRIS blocked a repeating call",
    sub: "An identical call was short-circuited and the earlier result reused.",
    severity: "warn",
  },
  dispatch_budget: {
    kind: "dispatch_budget",
    title: "IRIS hit its delegation budget for this turn",
    sub: "The cap on subtasks per turn was reached, so it had to answer from the results it already had.",
    severity: "warn",
  },
} as const;

export type CorrectionSource = keyof typeof BY_SOURCE;
export type CorrectionKind = (typeof BY_SOURCE)[CorrectionSource]["kind"];

/** kind → copy, so a caller holding only a kind (e.g. a rehydrated message) can render. */
const BY_KIND = Object.fromEntries(
  Object.values(BY_SOURCE).map((v) => [v.kind, v]),
) as Record<CorrectionKind, (typeof BY_SOURCE)[CorrectionSource]>;

/**
 * Classify a message by its `name` tag.
 *
 * Returns the correction kind for any persisted harness nudge, or `null` for an
 * ordinary message — so callers keep treating "not a correction" as the falsy
 * default and normal rendering is untouched. The two original names still map to
 * `"blank_result"` / `"empty_completion"` exactly as before.
 */
export function correctionKind(name?: string | null): CorrectionKind | null {
  if (!name) return null;
  const hit = BY_SOURCE[name as CorrectionSource];
  return hit ? hit.kind : null;
}

/**
 * Classify a message by its CONTENT, for the guardrails that carry no usable
 * `name`. loop_breaker's short-circuit ToolMessages are the case that matters:
 * all three variants use `status:"success"`, so neither the name nor the status
 * discriminates them — only the prefix does.
 */
export function correctionKindFromText(text?: string | null): CorrectionKind | null {
  const t = (text ?? "").trimStart();
  if (!t) return null;
  if (t.startsWith(LOOP_TERMINATOR_PREFIX)) return "loop_terminated";
  if (t.startsWith(LOOP_GUARD_PREFIX)) return "loop_blocked";
  if (t.startsWith(DISPATCH_BUDGET_PREFIX)) return "dispatch_budget";
  return null;
}

/** Display copy for a kind. Falls back to the empty-completion copy, never throws. */
export function correctionCopy(kind?: CorrectionKind | null): CorrectionCopy {
  const hit = (kind && BY_KIND[kind]) || BY_KIND.empty_completion;
  return { title: hit.title, sub: hit.sub, severity: hit.severity };
}

/** Every recognised source name — useful for tests and for asserting parity with Python. */
export const GUARDRAIL_SOURCES = Object.keys(BY_SOURCE) as CorrectionSource[];
