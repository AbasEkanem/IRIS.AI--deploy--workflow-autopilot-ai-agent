"use client";

/* ══════════════════════════════════════════════════════════════════
   useIrisStream — one hook that owns the whole streaming turn
   ──────────────────────────────────────────────────────────────────
   Shaped after LangGraph agent-chat-ui's `useStream`: a single object that
   owns messages / isLoading / interrupt / error and exposes submit + stop.
   The transport underneath stays our own FastAPI SSE bridge, so per-user
   auth, HITL approvals, rate limiting and uploads all keep working.

   Everything funnels through `consume()`, so the four send paths (send,
   regenerate, refresh, edit) and the four HITL paths (approve/reject in the
   inline card and in the floating overlay) share one event loop instead of
   the eight hand-copied `for await` chains this replaces.
   ══════════════════════════════════════════════════════════════════ */

import { useCallback, useEffect, useRef, useState } from "react";

import { fetchThreadStatus, resumeAgent, streamChat } from "@/lib/api";
import { appendLocalText, applyStreamEvent } from "@/lib/streamReducer";
import type { InterruptPayload, InterruptState, StreamEvent } from "@/types/chat";

/** Factory so each attempt gets a fresh generator bound to a fresh signal. */
type StreamSource = (signal: AbortSignal) => AsyncGenerator<StreamEvent>;

export interface UseIrisStreamOptions {
  /** Thread the run belongs to. Read at call time, so switching threads is safe. */
  threadId: string;
  /** Initials shown on the user avatar. */
  userInitials?: string;
  /** Fired when a thread's FIRST user message is sent (thread-list bookkeeping). */
  onFirstMessage?: (text: string) => void;
}

export interface SubmitOptions {
  /** Text sent to the backend. */
  query: string;
  /** Text shown in the user bubble. Defaults to `query`. */
  display?: string;
  /** Server-side paths from /api/upload. */
  attachments?: string[];
}

let seq = 0;
/** Unique even when two messages are created in the same millisecond. */
const newId = (role: string) => `${role}-${Date.now()}-${seq++}`;

/**
 * Which genuine user turn is the bubble at `upToIdx`? — zero-based, counted the way
 * the server segments turns.
 *
 * A resubmit (edit / regenerate / refresh) has to tell the server WHICH turn it
 * replaces. `/ask` appends into a channel with an `add_messages` reducer, so without
 * it a rewrite lands as a brand-new turn: the model then answers with the original
 * AND the rewrite in context, and a reload replays the message the user thought they
 * had edited away. The local array slice only ever hid that from view.
 *
 * It must be an ordinal rather than an id — `/ask` sends content with no id and
 * LangGraph assigns its own, so not one id in this array exists server-side.
 *
 * Named user-role bubbles are skipped: a `name` marks a persisted guardrail nudge
 * wearing the user's role, which is precisely what the server's segmentation refuses
 * to count as a turn. Returns -1 when no user turn sits at or before `upToIdx`, which
 * api.ts then omits from the body — degrading to a plain append rather than guessing.
 */
const userTurnOrdinal = (list: any[], upToIdx: number) =>
  list.slice(0, upToIdx + 1).filter(m => m.role === "user" && !m.name).length - 1;

const freshAssistant = (id: string) => ({
  id,
  role: "assistant" as const,
  content: "",
  segments: [] as { id: string; text: string }[],
  typing: true,
  statusSteps: [] as any[],
  isStreaming: true,
});

export function useIrisStream({ threadId, userInitials = "U", onFirstMessage }: UseIrisStreamOptions) {
  const [messages, setMessages] = useState<any[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [interrupt, setInterrupt] = useState<InterruptState | null>(null);
  const [error, setError] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  // Handlers are called from event callbacks that captured an older render, so
  // read the live array through a ref rather than the closed-over `messages`.
  const messagesRef = useRef<any[]>(messages);
  useEffect(() => { messagesRef.current = messages; }, [messages]);

  const patch = useCallback((id: string, fn: (m: any) => any) => {
    setMessages(prev => prev.map(m => (m.id === id ? fn(m) : m)));
  }, []);

  /** Abort the in-flight run. The reader's `finally` releases the connection. */
  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  /* ── The one event loop ── */
  const consume = useCallback(async (source: StreamSource, targetId: string) => {
    setError(null);
    setIsLoading(true);
    const controller = new AbortController();
    abortRef.current = controller;
    // A pause hands control to the approval card; the run is NOT finished, so
    // we must not clear its streaming flags the way a completed run does.
    let paused = false;

    try {
      for await (const event of source(controller.signal)) {
        if (event.type === "interrupt") {
          const payload = event.data as InterruptPayload;
          setInterrupt({ ...payload, agentMsgId: targetId });
          // statusSteps used to be zeroed here. It must NOT be: the workspace
          // panel IS the record of what led up to the approval, and wiping it
          // erased the delegation and tool history the moment IRIS asked.
          // `isStreaming:false` already stops the live spinner rows.
          //
          // `terminal` is stamped HERE rather than taken from the backend's
          // `terminal {reason:"paused"}`: that event is emitted after the
          // reconciliation step, and this `break` abandons the stream before it
          // arrives. Without it the bubble reads "ended without an answer" while
          // an approval card is sitting right underneath it.
          patch(targetId, m => ({
            ...m,
            typing: false,
            isStreaming: false,
            terminal: { reason: "paused", resumable: true },
            workedMs: typeof m.workspaceStartedAt === "number"
              ? Math.max(0, Date.now() - m.workspaceStartedAt)
              : (m.workedMs ?? 0),
          }));
          paused = true;
          break;
        }
        patch(targetId, m => applyStreamEvent(m, event));
      }
      if (!paused) patch(targetId, m => ({ ...m, isStreaming: false, typing: false }));
    } catch (err) {
      const isAbort = err instanceof DOMException && err.name === "AbortError";
      if (isAbort) {
        patch(targetId, m => appendLocalText(m, "*Generation stopped by user.*"));
      } else {
        const detail = err instanceof Error ? err.message : "Unknown error";
        setError(detail);
        patch(targetId, m => appendLocalText(m, `⚠️ Error communicating with agent: ${detail}`));
      }
    } finally {
      abortRef.current = null;
      setIsLoading(false);
    }
  }, [patch]);

  /* ── Send a new message ── */
  const submit = useCallback(async ({ query, display, attachments }: SubmitOptions) => {
    if (isLoading) return;
    const finalQuery = query.trim();
    if (!finalQuery && !(attachments && attachments.length > 0)) return;

    if (messagesRef.current.length === 0) onFirstMessage?.(finalQuery);

    const userMsg = {
      id: newId("user"),
      role: "user" as const,
      content: display ?? finalQuery,
      userNameInitials: userInitials,
    };
    const agentId = newId("agent");
    setMessages(prev => [...prev, userMsg, freshAssistant(agentId)]);

    const tid = threadId;
    const paths = attachments && attachments.length > 0 ? attachments : undefined;
    await consume(signal => streamChat(finalQuery, tid, signal, paths), agentId);
  }, [consume, isLoading, onFirstMessage, threadId, userInitials]);

  /**
   * Re-run the user message that produced `agentMsgId`.
   *
   * Drops the stale answer. The old code sliced `0..idx` INCLUSIVE of the
   * assistant being regenerated, so a regenerate left the previous answer
   * sitting above the new one.
   */
  const regenerate = useCallback(async (agentMsgId: string) => {
    if (isLoading) return;
    const list = messagesRef.current;
    const idx = list.findIndex(m => m.id === agentMsgId);
    if (idx === -1) return;
    const userMsg = list[idx - 1];
    if (!userMsg || userMsg.role !== "user") return;

    // Strip any [SYSTEM CONTEXT:...] prefix injected by the backend before
    // resending, so internal annotations never re-enter as user input.
    const finalQuery = String(userMsg.content ?? "").replace(/^\[SYSTEM CONTEXT:[\s\S]*?\]\s*/i, "");
    // Re-running a turn REPLACES it. Without this the same request was appended a
    // second time and the model answered it twice over.
    const turn = userTurnOrdinal(list, idx - 1);
    const agentId = newId("agent");
    setMessages(prev => [...prev.slice(0, idx), freshAssistant(agentId)]);

    const tid = threadId;
    await consume(signal => streamChat(finalQuery, tid, signal, undefined, turn), agentId);
  }, [consume, isLoading, threadId]);

  /** Retry from a user message — regenerates its answer if one already exists. */
  const refresh = useCallback(async (userMsgId: string) => {
    if (isLoading) return;
    const list = messagesRef.current;
    const idx = list.findIndex(m => m.id === userMsgId);
    if (idx === -1) return;

    const next = list[idx + 1];
    if (next && next.role === "assistant") {
      await regenerate(next.id);
      return;
    }

    // Same prefix strip as regenerate. This path was missing it, so a refresh on a
    // message carrying a backend-injected [SYSTEM CONTEXT:...] header re-sent that
    // header back as if the user had typed it.
    const finalQuery = String(list[idx].content ?? "").replace(/^\[SYSTEM CONTEXT:[\s\S]*?\]\s*/i, "");
    const turn = userTurnOrdinal(list, idx);
    const agentId = newId("agent");
    setMessages(prev => [...prev.slice(0, idx + 1), freshAssistant(agentId)]);

    const tid = threadId;
    await consume(signal => streamChat(finalQuery, tid, signal, undefined, turn), agentId);
  }, [consume, isLoading, regenerate, threadId]);

  /**
   * Rewrite a user message and re-run from there.
   *
   * The local slice below is only half the job — it changes what the browser shows.
   * `replace_from_turn` is the other half: it tells the server to delete that turn
   * and everything after it from the checkpoint before the edited text is appended.
   * Without it the edit stacked behind the original, so IRIS answered with both
   * requests in context and a reload brought the replaced message back.
   */
  const editAndResubmit = useCallback(async (userMsgId: string, newContent: string) => {
    if (isLoading) return;
    const list = messagesRef.current;
    const idx = list.findIndex(m => m.id === userMsgId);
    if (idx === -1) return;

    const turn = userTurnOrdinal(list, idx);
    const updatedUser = { ...list[idx], content: newContent };
    const agentId = newId("agent");
    setMessages(prev => [...prev.slice(0, idx), updatedUser, freshAssistant(agentId)]);

    const tid = threadId;
    await consume(signal => streamChat(newContent, tid, signal, undefined, turn), agentId);
  }, [consume, isLoading, threadId]);

  /**
   * Answer a HITL approval and stream what follows.
   *
   * The pending interrupt is cleared UP FRONT, not in a `finally`. A resumed run
   * frequently pauses again (an approval chain across a long multi-step task), and
   * clearing afterwards wiped the interrupt `consume` had just set — stranding the
   * run with no card to approve. Clearing first lets the next pause survive.
   *
   * It resumes into the SAME assistant message that paused (`pending.agentMsgId`),
   * rather than appending a fresh one. Appending split one logical task across two
   * workspace panels: everything up to the approval — the todos, the delegations,
   * the harness nudges — stayed behind in the first bubble while the work that the
   * approval unblocked streamed into a second, empty one. An approval is a pause in
   * a run, not a new run, so there is one panel and it keeps filling.
   *
   * Re-baselining `workspaceStartedAt` to `now - workedMs` is what makes the
   * counter continue from where the pause froze it AND excludes the time the human
   * spent deciding — that wait is not IRIS working. `terminal` must be cleared too:
   * the pause stamped `{reason:"paused"}`, and leaving it there would show the
   * paused pill and a Recover button on a run that is actively streaming again.
   */
  const resume = useCallback(async (
    decision: "approve" | "reject",
    editedArgs?: Record<string, unknown>
  ) => {
    const pending = interrupt;
    if (!pending) return;
    setInterrupt(null);

    // The paused message is normally still mounted. It is not if the user switched
    // threads while the card was open, so fall back to a fresh bubble rather than
    // streaming into an id that no longer exists and rendering nothing.
    const target = messagesRef.current.find(m => m.id === pending.agentMsgId);
    let agentId: string;
    if (target) {
      agentId = pending.agentMsgId;
      patch(agentId, m => {
        const worked = typeof m.workedMs === "number" ? m.workedMs : 0;
        const { terminal: _paused, ...rest } = m;
        return {
          ...rest,
          typing: true,
          isStreaming: true,
          workspaceStartedAt: Date.now() - worked,
          workedMs: undefined,
        };
      });
    } else {
      agentId = newId("agent");
      setMessages(prev => [...prev, freshAssistant(agentId)]);
    }

    await consume(
      signal => resumeAgent(pending.thread_id, decision, editedArgs, signal),
      agentId
    );
  }, [consume, interrupt, patch]);

  /**
   * Re-attach to a run whose stream ended without an answer.
   *
   * Offered by the workspace panel on `terminal {resumable:true}`. The answer is
   * frequently already persisted — durability is `"async"`, so a completed
   * super-step is flushed before the stream ceiling fires — but nothing in the UI
   * ever read `/api/threads/{id}/status`, so it was unreachable and the request
   * had to be retyped.
   *
   * The recovered answer is folded in as `response_complete`, so it renders as
   * ordinary prose; `/status` returns the answer text only, not the parsed Final
   * Response Contract, so a recovered turn shows no structured summary card.
   * `terminal` is set directly rather than replayed through the reducer, which
   * would recompute "worked for N seconds" against the current clock and inflate
   * it by however long the panel sat idle.
   */
  const recover = useCallback(async (agentMsgId: string) => {
    const st = await fetchThreadStatus(threadId);

    if (st.pending_interrupt) {
      setInterrupt({ ...st.pending_interrupt, agentMsgId });
      return;
    }
    if (st.has_answer && st.answer) {
      const answer = st.answer;
      patch(agentMsgId, m => ({
        ...applyStreamEvent(m, { type: "response_complete", data: answer }),
        terminal: { reason: "complete", resumable: false },
      }));
      return;
    }
    patch(agentMsgId, m => appendLocalText(
      m,
      st.state === "running"
        ? "*This run is still going on the server — try recovering again in a moment.*"
        : "*No saved answer was found for this run.*",
    ));
  }, [patch, threadId]);

  return {
    messages,
    setMessages,
    isLoading,
    interrupt,
    setInterrupt,
    error,
    submit,
    regenerate,
    refresh,
    editAndResubmit,
    resume,
    recover,
    stop,
  };
}
