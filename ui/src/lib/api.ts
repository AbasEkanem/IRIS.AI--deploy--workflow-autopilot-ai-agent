import { StreamEvent, SummaryEvent, WorkspaceRecord } from "@/types/chat";
import { getSession } from "next-auth/react";

// In production (static export served by FastAPI), use relative URLs (same origin).
// For local dev with separate frontend/backend, set NEXT_PUBLIC_API_URL=http://localhost:8000
const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

/**
 * Bearer auth header for the FastAPI backend. NextAuth's `session` callback mints
 * a short-lived HS256 token (`session.backendToken`, see the [...nextauth] route);
 * `getSession()` re-fetches — and thereby re-mints — it on demand. Returns `{}`
 * when signed out, so the request still goes out and the backend replies 401
 * (the UI already handles a failed call) rather than throwing here.
 */
async function authHeaders(): Promise<Record<string, string>> {
  const session = await getSession();
  const token = session?.backendToken;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Read one SSE body as parsed events.
 *
 * The backend's protocol is `data: {json}\n\n` per event, terminated by
 * `data: [DONE]`. /ask and /resume speak it identically, so they share this
 * reader — they used to carry byte-for-byte copies that could drift.
 */
async function* readSSE(response: Response): AsyncGenerator<StreamEvent> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      // The trailing fragment may be half an event — hold it for the next chunk.
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const raw = line.slice(6).trim();
        if (!raw) continue;
        if (raw === "[DONE]") return;
        try {
          yield JSON.parse(raw) as StreamEvent;
        } catch {
          // skip malformed lines
        }
      }
    }
  } finally {
    // Releases the connection on abort as well as on normal completion.
    try { await reader.cancel(); } catch {}
  }
}

/** POST a JSON body and stream the SSE reply. */
async function* postSSE(
  path: string,
  body: Record<string, unknown>,
  label: string,
  signal?: AbortSignal
): AsyncGenerator<StreamEvent> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${label} failed ${response.status}: ${text}`);
  }

  yield* readSSE(response);
}

export function streamChat(
  message: string,
  threadId: string,
  signal?: AbortSignal,
  attachments?: string[],  // server-side file paths from /api/upload
  // Edit / regenerate / refresh: "this message REPLACES user turn N" (zero-based).
  // The server rewinds the persisted thread to that turn before appending, so a
  // rewrite replaces the original instead of stacking behind it. Omitted entirely
  // for a normal send — a plain append is still the default.
  replaceFromTurn?: number
): AsyncGenerator<StreamEvent> {
  return postSSE(
    "/ask",
    {
      message,
      thread_id: threadId,
      ...(attachments && attachments.length > 0 ? { attachments } : {}),
      ...(typeof replaceFromTurn === "number" && replaceFromTurn >= 0
        ? { replace_from_turn: replaceFromTurn }
        : {}),
    },
    "API error",
    signal
  );
}

/**
 * One turn's persisted messages, plus — on the turn's LAST assistant message —
 * the rebuilt execution record and the parsed Final Response Contract. Both are
 * optional: a plain conversational turn ran no tools and produced no contract, so
 * it carries neither and renders exactly as it always did.
 */
export interface HistoryMessage {
  role: string;
  content: string;
  name?: string;
  id?: string;
  /** Rebuilt server-side from the checkpointer — see `WorkspaceRecord`. */
  workspace?: WorkspaceRecord;
  /** Parsed Final Response Contract, so a reloaded answer keeps its summary card. */
  summary?: SummaryEvent;
}

/**
 * Why this isn't just `HistoryMessage[]`
 * --------------------------------------
 * This function used to `return []` for every failure — expired JWT, backend
 * down, 500 — which is byte-identical to the answer for a brand-new empty
 * thread. So a signed-out-but-still-rendered session showed the user's whole
 * conversation as gone, and the UI had no way to tell "nothing here" from "I
 * could not find out". The caller needs that distinction to choose between
 * rendering an empty composer, offering a retry, or asking the user to sign in
 * again, so the failure now travels with the result.
 */
export type HistoryErrorKind = "unauthorized" | "unreachable" | "server";

export interface ThreadHistoryResult {
  messages: HistoryMessage[];
  /** Absent when the read genuinely succeeded — `messages` is then the truth. */
  error?: HistoryErrorKind;
  /** HTTP status, when there was a response at all. */
  status?: number;
}

export async function fetchThreadHistory(threadId: string): Promise<ThreadHistoryResult> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/threads/${threadId}/history`, {
      headers: await authHeaders(),
      credentials: "include",
    });
  } catch (error) {
    // Network-level: DNS, offline, CORS preflight rejection. Retryable.
    console.warn("Backend unreachable for history fetch:", error);
    return { messages: [], error: "unreachable" };
  }
  if (!res.ok) {
    // 401/403 are the ones worth naming: NextAuth can still hold a session while
    // the short-lived backendToken it mints has expired, and the only fix is a
    // fresh sign-in — a retry button would loop forever.
    const kind: HistoryErrorKind =
      res.status === 401 || res.status === 403 ? "unauthorized" : "server";
    return { messages: [], error: kind, status: res.status };
  }
  try {
    const data = await res.json();
    return { messages: data.messages ?? [] };
  } catch {
    return { messages: [], error: "server", status: res.status };
  }
}

/* ── Thread status (re-attach after a dropped stream) ── */

export interface ThreadStatus {
  thread_id: string;
  /** "running" = unfinished steps queued, "paused" = HITL, "complete" = settled. */
  state: "running" | "paused" | "complete" | "unknown";
  has_answer: boolean;
  answer?: string;
  pending_interrupt?: {
    thread_id: string;
    tool: string;
    args: Record<string, unknown>;
    risk: "low" | "medium" | "high";
  };
}

/**
 * Cheap unstreamed snapshot of a thread, read from the persisted checkpoint.
 *
 * This closes the re-attach gap: the backend has exposed
 * `/api/threads/{id}/status` all along but nothing in the UI ever called it, so
 * a run that hit the stream ceiling left its answer sitting in the checkpoint
 * with no way to collect it — the user had to retype the request. Durability is
 * `"async"`, so a completed super-step is already flushed when the ceiling
 * fires, which is exactly why the answer is usually there.
 *
 * Never throws: a probe that cannot read state returns `state:"unknown"` rather
 * than claiming the run finished, because lying "complete" would strand a run
 * that is still going.
 */
export async function fetchThreadStatus(threadId: string): Promise<ThreadStatus> {
  const unknown: ThreadStatus = { thread_id: threadId, state: "unknown", has_answer: false };
  try {
    const res = await fetch(`${API_BASE}/api/threads/${threadId}/status`, {
      headers: await authHeaders(),
      credentials: "include",
    });
    if (!res.ok) return unknown;
    const data = await res.json();
    return {
      thread_id: data.thread_id ?? threadId,
      state: data.state ?? "unknown",
      has_answer: Boolean(data.has_answer),
      answer: typeof data.answer === "string" ? data.answer : undefined,
      pending_interrupt: data.pending_interrupt,
    };
  } catch (error) {
    console.warn("Backend unreachable for thread status:", error);
    return unknown;
  }
}

/* ── Google Workspace connect (per-user OAuth) ── */

export interface GoogleStatus {
  /** true when the current user has a stored, valid Google refresh token */
  connected: boolean;
  /** false when the server isn't configured for per-user connect at all */
  available: boolean;
  detail?: string;
}

/**
 * Fetch whether the signed-in user has connected their Google account.
 * Never throws — returns a safe "not connected / unavailable" shape on failure
 * so the Settings UI can render a sensible fallback.
 */
export async function getGoogleStatus(): Promise<GoogleStatus> {
  try {
    const res = await fetch(`${API_BASE}/google/status`, {
      headers: await authHeaders(),
      credentials: "include",
    });
    if (!res.ok) {
      return { connected: false, available: false, detail: `status ${res.status}` };
    }
    return (await res.json()) as GoogleStatus;
  } catch {
    return { connected: false, available: false, detail: "Backend unreachable" };
  }
}

/**
 * The backend endpoint that kicks off Google's OAuth consent flow. This must be
 * a full-page browser navigation (not fetch) so Google can redirect the user
 * and the backend can set/read the CSRF state cookie. A full-page nav can't
 * carry a Bearer header, so the caller first mints a single-use ticket
 * (mintGoogleConnectTicket) and appends it as ?ticket=… to this URL.
 */
export function googleConnectUrl(): string {
  return `${API_BASE}/google/connect`;
}

/**
 * Mint a single-use "connect ticket" that authorizes ONE /google/connect
 * navigation (the backend consumes it before redirecting to Google, so a leaked
 * ticket is already spent). Sent as a Bearer-authenticated POST — unlike
 * /connect itself, which is a full-page navigation that can't carry the header.
 * Throws on failure so the caller can surface a connect error rather than
 * navigating to a dead end.
 */
export async function mintGoogleConnectTicket(): Promise<string> {
  const res = await fetch(`${API_BASE}/google/connect-ticket`, {
    method: "POST",
    headers: await authHeaders(),
    credentials: "include",
  });
  if (!res.ok) {
    throw new Error(`connect-ticket failed: ${res.status}`);
  }
  const data = (await res.json()) as { ticket?: string };
  if (!data.ticket) {
    throw new Error("connect-ticket response missing ticket");
  }
  return data.ticket;
}

/** Delete the current user's stored Google token. Returns true on success. */
export async function disconnectGoogle(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/google/disconnect`, {
      method: "POST",
      headers: await authHeaders(),
      credentials: "include",
    });
    return res.ok;
  } catch {
    return false;
  }
}


export async function uploadFile(file: File): Promise<{ filename: string; path: string }> {
  const formData = new FormData();
  formData.append("file", file);

  const res = await fetch(`${API_BASE}/api/upload`, {
    method: "POST",
    // No Content-Type: the browser sets multipart/form-data + boundary itself.
    headers: await authHeaders(),
    body: formData,
    credentials: "include",
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Upload failed ${res.status}: ${text}`);
  }

  return res.json();
}

export function resumeAgent(
  threadId: string,
  decision: "approve" | "reject",
  editedArgs?: Record<string, unknown>,
  signal?: AbortSignal
): AsyncGenerator<StreamEvent> {
  return postSSE(
    "/resume",
    {
      thread_id: threadId,
      decision,
      ...(editedArgs ? { edited_args: editedArgs } : {}),
    },
    "Resume",
    signal
  );
}
