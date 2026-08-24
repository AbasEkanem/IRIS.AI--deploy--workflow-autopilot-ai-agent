// Shared types for the IRIS 1.0 chat UI

export interface StatusStep {
  phase:
    | "thinking"
    | "subagent"
    | "tool"
    | "tool_done"
    | "reading"
    | "writing"
    | "searching"
    | "memory"
    | "emailing"
    | "researching"
    | "delegating"
    | "verifying";
  detail: string;
  tool?: string;
  /** Stable run-id from the backend — used to merge start/done events for the same call. */
  id?: string;
  /** True once the underlying tool/subagent call has finished. */
  done?: boolean;
  timestamp?: number;
  /**
   * LangGraph namespace key ("" for the orchestrator, non-empty inside a
   * subagent). The backend used to compute this and throw it away, which is why
   * the workspace could not nest a specialist's calls under its delegation.
   */
  ns?: string;
  /** `tool_call_id` of the `task` delegation that owns this row, when nested. */
  parent_id?: string | null;
  /**
   * Arrival order within this message, stamped client-side by the reducer.
   *
   * Exists so the workspace can place a harness nudge at the point in the run
   * where it actually fired, instead of in a footer below everything. Status
   * steps and corrections are separate arrays, so neither one's own index says
   * anything about their order relative to each other — this does. Derived from
   * array lengths, not a counter, so the reducer stays pure.
   */
  seq?: number;
}

/** One `task` delegation, synthesized from its tool call + completion message. */
export interface SubagentEvent {
  /** The `task` call's tool_call_id — also the parent_id of its nested rows. */
  id?: string | null;
  ns?: string;
  parent_id?: string | null;
  /** Real specialist name, e.g. "grace" — not an abstracted domain label. */
  subagent_type: string;
  /** The brief IRIS handed the specialist (truncated by the backend). */
  description?: string;
  /** "blank" is a real outcome: subagents.py defaults a completion result to "". */
  status: "running" | "done" | "blank";
}

/** A harness guardrail steering IRIS mid-run. See @/lib/corrections. */
export interface CorrectionEvent {
  /** Source `name`, or "loop_guard" when classified from the content prefix. */
  source: string;
  /** Human sentence for what the guardrail did. */
  label: string;
  severity: "info" | "warn";
  /** Truncated verbatim steering text. Render as INERT text — it can quote
   *  third-party content (emails, web pages) carrying indirect injection. */
  raw: string;
  /** False for a request-only nudge, which never reaches graph state. */
  persisted?: boolean;
  ns?: string;
  parent_id?: string | null;
  /** Arrival order within the message — see the note on `StatusStep.seq`. */
  seq?: number;
}

/** IRIS's parsed Final Response Contract — the chat's summary card. */
export interface SummaryEvent {
  status: string;
  summary: string;
  artifacts: string[];
  blockers: string;
  learning: string;
  /** The whole answer. Used verbatim when the contract did not parse. */
  raw: string;
}

/** Why a run ended. Always emitted, on every exit path. */
export interface TerminalEvent {
  reason: "complete" | "paused" | "timeout" | "error" | "rate_limit" | "empty";
  /** True when the thread can be re-attached to recover a persisted answer. */
  resumable: boolean;
}

/** A streamed token. `channel:"workspace"` routes prose to the panel, not the chat. */
export interface TokenEvent {
  id?: string;
  text: string;
  channel?: "workspace" | "chat";
}

/**
 * A finished turn's execution record, rebuilt server-side from the checkpointer
 * and delivered by `GET /api/threads/{id}/history`.
 *
 * The panel used to be live-only, so a reload lost it and the reloaded thread
 * degraded into a column of bare nudge cards — the guardrail HumanMessages were
 * the only things `/history` did not filter out. The backend now mirrors this
 * reducer's own accumulation (`_workspace_record` in web_api.py) and ships the
 * result under these exact field names, so a restored message hydrates through
 * the same path a live one takes.
 *
 * Two limits are declared rather than papered over:
 * `duration_known` is false because wall-clock time is not persisted in graph
 * state; and a specialist's internal tool calls live in their own checkpoint
 * namespace, so `statusSteps` holds the orchestrator's rows and the `task`
 * delegation rows, not the specialists' internals.
 */
export interface WorkspaceRecord {
  statusSteps: StatusStep[];
  todos: TodoTask[];
  subagents: SubagentEvent[];
  corrections: CorrectionEvent[];
  workspaceSegments: { id: string; text: string }[];
  terminal: TerminalEvent;
  duration_known: boolean;
}


export interface StreamEvent {
  type: "status" | "response" | "response_complete" | "token" | "done" | "error" | "todo" | "subagent" | "terminal" | "rate_limit" | "refinement" | "stream_abort" | "interrupt" | "correction" | "summary";
  data: string | StatusStep | TodoTask[] | SubagentEvent | CorrectionEvent | SummaryEvent | TerminalEvent | TokenEvent | { tool: string; output: string; timestamp?: number } | { phase: string; detail: string; tool?: string } | { resume_at: number } | InterruptPayload | any;
}

export interface TodoTask {
  /**
   * LangChain's `Todo` TypedDict is `{content, status}`
   * (langchain/agents/middleware/todo.py:25-32) — `content` is the REAL key and
   * its absence here is why the live checklist never rendered: the normalizer
   * only looked for the two names below, every item flattened to "", and the
   * whole list was filtered away. The backend now emits all three spellings.
   */
  content?: string;
  task_description?: string;
  description?: string;
  task_status?: "pending" | "in-progress" | "in_progress" | "completed";
  status?: "pending" | "in-progress" | "in_progress" | "completed";
}

export interface InterruptPayload {
  thread_id: string;
  tool: string;
  args: Record<string, unknown>;
  risk: "low" | "medium" | "high";
}

export interface InterruptState extends InterruptPayload {
  agentMsgId: string;
}
