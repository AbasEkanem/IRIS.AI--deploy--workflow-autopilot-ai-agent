/* ══════════════════════════════════════════════════════════════════
   streamReducer — the single place an SSE event becomes a message patch
   ──────────────────────────────────────────────────────────────────
   page.tsx used to carry EIGHT copies of the same `if (event.type === …)`
   chain (send / regenerate / refresh / edit, plus approve+reject in both
   the inline and the floating HITL card). They had already drifted apart
   — some handled `stream_abort`, some didn't; some cleared `isStreaming`
   on rate_limit, some left it spinning. This module owns the logic once
   and `useIrisStream` drives it, so every entry point behaves identically.
   ══════════════════════════════════════════════════════════════════ */

import type {
  StreamEvent,
  SubagentEvent,
  CorrectionEvent,
  SummaryEvent,
  TerminalEvent,
} from "@/types/chat";

/* ══════════════════════════════════════════════════════════════════
   mergeStatusStep — real-time activity feed reducer
   ──────────────────────────────────────────────────────────────────
   The backend streams one `status` event per tool / subagent / memory
   call on start (done:false) and a matching `tool_done` event on finish,
   both carrying the same run-id (`id`). We merge by id so each live call
   is a single row that flips from spinner → checkmark, exactly like
   Claude's activity feed — instead of piling up duplicate rows.
   ══════════════════════════════════════════════════════════════════ */
export function mergeStatusStep(steps: any[] = [], d: any): any[] {
  const list = steps || [];

  // ── Completion event: flip the matching in-progress row to done ──
  if (d.phase === "tool_done" || d.done) {
    if (!d.id) return list;
    let found = false;
    const next = list.map((s) => {
      if (s.id && s.id === d.id) { found = true; return { ...s, done: true }; }
      return s;
    });
    return found ? next : list; // ignore a stray done with no start row
  }

  // ── Start / update event with a stable id: update in place or append ──
  if (d.id) {
    const idx = list.findIndex((s) => s.id === d.id);
    // ns/parent_id are carried so the workspace can nest a specialist's calls
    // under the `task` delegation that spawned them.
    const row = {
      phase: d.phase, detail: d.detail, tool: d.tool, id: d.id, done: false,
      ns: d.ns, parent_id: d.parent_id,
    };
    if (idx !== -1) {
      const next = [...list];
      // `seq` is NOT in `row`, so an update never restamps a row that is already
      // placed in the stream — a long-running tool keeps the position where it
      // started rather than jumping past the nudges that fired while it ran.
      next[idx] = { ...next[idx], ...row };
      return next;
    }
    return [...list, { ...row, seq: d.seq }];
  }

  // ── No id (e.g. "thinking", "verifying"): append as a transient row ──
  return [...list, { phase: d.phase, detail: d.detail, tool: d.tool, ns: d.ns, parent_id: d.parent_id, seq: d.seq }];
}

/* ══════════════════════════════════════════════════════════════════
   Answer segments — LangGraph messages-tuple semantics
   ──────────────────────────────────────────────────────────────────
   One IRIS turn routinely produces SEVERAL assistant messages: an answer,
   then a blank-recovery nudge makes the model speak again, then a final
   wrap-up. The backend now tags every `token` with its AIMessageChunk id,
   so we accumulate per id instead of concatenating the whole turn into one
   flat buffer. `content` is just the segments joined with a blank line —
   which is what finally separates those replies visually instead of fusing
   them mid-sentence.
   ══════════════════════════════════════════════════════════════════ */
export interface AnswerSegment {
  /** AIMessageChunk id from the backend (or `local:*` for client-side text). */
  id: string;
  text: string;
}

/** Flatten segments into the string the markdown renderer consumes. */
export function renderSegments(segments: AnswerSegment[]): string {
  return segments
    .map((s) => s.text.trim())
    .filter(Boolean)
    .join("\n\n");
}

/** Append `text` to the segment with this id, creating it if new. */
function upsertSegment(segments: AnswerSegment[], id: string, text: string): AnswerSegment[] {
  const idx = segments.findIndex((s) => s.id === id);
  if (idx === -1) {
    // Trim the LEADING whitespace run of a message's first chunk. The backend
    // deliberately forwards whitespace-only chunks now (dropping them fused
    // words together), so the very first one would otherwise indent the answer.
    return [...segments, { id, text: text.replace(/^\s+/, "") }];
  }
  const next = [...segments];
  next[idx] = { ...next[idx], text: next[idx].text + text };
  return next;
}

/**
 * Fold a `response_complete` payload into the streamed segments.
 *
 * `response_complete` carries the authoritative text of the LAST assistant
 * message — the sanitized version of what just streamed. Replacing the matching
 * segment (rather than appending) is what stops the answer being printed twice.
 * A single missing space used to defeat this comparison, which is exactly the
 * duplicate-reply bug; the backend keeps whitespace now, so it matches.
 */
export function applyFinalSegment(segments: AnswerSegment[], incoming: string): AnswerSegment[] {
  const final = (incoming || "").trim();
  if (!final) return segments;
  if (segments.length === 0) return [{ id: "final", text: final }];

  const last = segments[segments.length - 1];
  const lastText = last.text.trim();

  // Same text, or the sanitized superset of what streamed → replace in place.
  if (lastText && (final === lastText || final.startsWith(lastText))) {
    const next = [...segments];
    next[next.length - 1] = { ...last, text: final };
    return next;
  }
  // Already shown somewhere in this turn → nothing to add.
  if (segments.some((s) => s.text.trim() === final || s.text.includes(final))) {
    return segments;
  }
  // Genuinely new text (e.g. only the tail message streamed) → append.
  return [...segments, { id: "final", text: final }];
}

/** Replace a message's segments and keep `content` in sync. */
function withSegments(msg: any, segments: AnswerSegment[], extra?: Record<string, unknown>) {
  return { ...msg, segments, content: renderSegments(segments), ...extra };
}

/**
 * Stamp the workspace start time on the first workspace-bound event.
 *
 * The panel's "Worked for N seconds" needs an origin, and the message's own
 * createdAt is too early — it is set when the optimistic bubble is pushed, before
 * the request is even sent. Stamping here means the clock starts when IRIS
 * actually did something. Idempotent, so every later event is a no-op.
 */
function touchWorkspace(msg: any): any {
  return msg.workspaceStartedAt ? msg : { ...msg, workspaceStartedAt: Date.now() };
}

/**
 * Arrival order for the next workspace row of this message.
 *
 * The workspace wants ONE execution stream — a harness nudge shown at the point
 * in the run where it fired, not in a footer under everything. But status steps
 * and corrections accumulate in two separate arrays, so neither array's own index
 * says anything about their order relative to each other. Summing both lengths
 * gives a shared monotonic clock that does.
 *
 * Read off the arrays instead of a module-level counter deliberately: this keeps
 * `applyStreamEvent` a pure function of (msg, event), so two messages streaming
 * concurrently cannot interleave each other's stamps and a replayed event is
 * idempotent.
 *
 * Collisions are possible and harmless — a `status` UPDATE (an existing id, or a
 * `tool_done`) does not grow either array, so the next append reuses that number.
 * The consumer only ever asks "which rows came before this correction", and a tie
 * resolves to the row, since a correction is spliced in AFTER the last row whose
 * seq is <= its own.
 */
function arrivalSeq(msg: any): number {
  return (msg.statusSteps?.length ?? 0) + (msg.corrections?.length ?? 0);
}

/**
 * Apply one stream event to one assistant message. Pure — returns a new object.
 *
 * `interrupt` is NOT handled here: pausing for approval is a conversation-level
 * transition, so `useIrisStream` owns it.
 */
export function applyStreamEvent(msg: any, event: StreamEvent): any {
  const segments: AnswerSegment[] = msg.segments ?? (msg.content ? [{ id: "seed", text: msg.content }] : []);

  switch (event.type) {
    case "status":
      // `seq` is the arrival stamp the workspace uses to place harness nudges at
      // the point in the run where they fired. Derived from the two array
      // lengths rather than a module counter, so this stays a pure function of
      // (msg, event) and two messages streaming at once cannot interleave stamps.
      return touchWorkspace({
        ...msg,
        statusSteps: mergeStatusStep(msg.statusSteps, { ...(event.data as any), seq: arrivalSeq(msg) }),
      });

    case "todo":
      // Each event carries the WHOLE checklist, so replace it wholesale;
      // ticking an item off just re-renders in place. The backend now emits on
      // empty too, so clearing the plan clears the panel.
      return touchWorkspace({ ...msg, todos: (event.data as any[]) ?? [] });

    case "subagent": {
      // One row per `task` delegation, synthesized backend-side from the tool
      // call and paired to its completion ToolMessage by tool_call_id. The
      // completion event repeats only `id`/`status`, so MERGE rather than
      // replace — otherwise finishing a delegation erases its description.
      const s = event.data as SubagentEvent;
      if (!s?.subagent_type && !s?.id) return msg;
      const list: SubagentEvent[] = msg.subagents ?? [];
      const idx = s.id ? list.findIndex((x) => x.id === s.id) : -1;
      if (idx === -1) return touchWorkspace({ ...msg, subagents: [...list, s] });
      const next = [...list];
      next[idx] = { ...next[idx], ...s };
      return touchWorkspace({ ...msg, subagents: next });
    }

    case "correction": {
      // A harness guardrail steering IRIS mid-run. Append-only: the ORDER of
      // interventions is the story the workspace tells, and the backend already
      // de-dupes by message id, so each one arrives exactly once.
      const c = event.data as CorrectionEvent;
      if (!c?.label) return msg;
      const list: CorrectionEvent[] = msg.corrections ?? [];
      return touchWorkspace({ ...msg, corrections: [...list, { ...c, seq: arrivalSeq(msg) }] });
    }

    case "summary": {
      // IRIS's parsed Final Response Contract — this is what the chat shows.
      // `raw` always carries the whole answer, so a contract that failed to
      // parse still renders as prose instead of an empty bubble.
      const sum = event.data as SummaryEvent;
      if (!sum) return msg;
      return { ...msg, summary: sum, typing: false };
    }

    case "terminal": {
      // Emitted on EVERY exit path, which is what lets the live "IRIS is
      // working…" line always resolve. Freeze the elapsed time here so the
      // collapsed panel's label stops reading the wall clock.
      const t = (event.data ?? {}) as TerminalEvent;
      const startedAt = msg.workspaceStartedAt;
      return {
        ...msg,
        terminal: t,
        workedMs:
          typeof startedAt === "number"
            ? Math.max(0, Date.now() - startedAt)
            : (msg.workedMs ?? 0),
        typing: false,
        isStreaming: false,
      };
    }

    case "token": {
      const { id, text, channel } = normalizeToken(event.data);
      if (!text) return msg;
      // Prose belongs in the workspace transcript, not the chat. Keeping the
      // chat's `segments` empty is precisely what makes applyFinalSegment's
      // "no segments yet" branch turn the summary into the single chat bubble.
      if (channel === "workspace") {
        return touchWorkspace({
          ...msg,
          workspaceSegments: upsertSegment(msg.workspaceSegments ?? [], id, text),
          typing: false,
        });
      }
      return withSegments(msg, upsertSegment(segments, id, text), { typing: false });
    }

    case "response":
    case "response_complete": {
      const text = typeof event.data === "string" ? event.data : "";
      if (!text.trim()) return { ...msg, typing: false, isStreaming: false };
      return withSegments(msg, applyFinalSegment(segments, text), { typing: false, isStreaming: false });
    }

    case "refinement": {
      // A refined answer supersedes the whole turn.
      const text = typeof event.data === "string" ? event.data : "";
      if (!text.trim()) return msg;
      return withSegments(msg, [{ id: "refinement", text }], { typing: false, isStreaming: false });
    }

    case "rate_limit": {
      const { resume_at } = (event.data ?? {}) as { resume_at: number };
      return {
        ...msg,
        segments: [],
        content: "",
        recalibrating: true,
        resumeAt: new Date(resume_at * 1000),
        typing: false,
        isStreaming: false,
      };
    }

    case "stream_abort":
      // Hard timeout or server-side exception — stop the generation indicator
      // immediately rather than leaving it spinning forever.
      return { ...msg, typing: false, isStreaming: false };

    case "error": {
      const errMsg = typeof event.data === "string" ? event.data : "Unknown error";
      return withSegments(msg, [...segments, { id: `error:${segments.length}`, text: `⚠️ **${errMsg}**` }], {
        typing: false,
        isStreaming: false,
      });
    }

    default:
      return msg;
  }
}

/**
 * Normalize a `token` payload. The backend sends `{id, text, channel}` so chunks
 * can be grouped per assistant message and routed to the workspace transcript
 * rather than the chat; a bare string is still accepted so a rolled-back or older
 * backend keeps streaming into one CHAT segment instead of breaking — which is
 * also why the default channel is "chat", not "workspace".
 */
export function normalizeToken(data: unknown): {
  id: string;
  text: string;
  channel: "workspace" | "chat";
} {
  if (typeof data === "string") return { id: "orch", text: data, channel: "chat" };
  if (data && typeof data === "object") {
    const d = data as { id?: unknown; text?: unknown; channel?: unknown };
    return {
      id: typeof d.id === "string" ? d.id : "orch",
      text: typeof d.text === "string" ? d.text : "",
      channel: d.channel === "workspace" ? "workspace" : "chat",
    };
  }
  return { id: "orch", text: "", channel: "chat" };
}

/** Append plain client-side text (stop notice, transport error) to a message. */
export function appendLocalText(msg: any, text: string): any {
  const segments: AnswerSegment[] = msg.segments ?? (msg.content ? [{ id: "seed", text: msg.content }] : []);
  return withSegments(msg, [...segments, { id: `local:${segments.length}`, text }], {
    typing: false,
    isStreaming: false,
  });
}
