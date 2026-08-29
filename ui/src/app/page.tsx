"use client";
import { useState, useRef, useEffect, useCallback } from "react";
import { Sun, Moon, Sparkles, ThumbsUp, Copy, RefreshCw, Edit2, PenSquare, Search, History, Settings, Plus } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeRaw from "rehype-raw";
import { LoginScreen } from "@/components/LoginScreen";
import IrisLogo from "@/components/IrisLogo";
import { fetchThreadHistory, fetchThreads, deleteThread, uploadFile, getGoogleStatus, googleConnectUrl, mintGoogleConnectTicket, disconnectGoogle, type GoogleStatus, type HistoryErrorKind } from "@/lib/api";
import { useIrisStream } from "@/hooks/useIrisStream";

/* AgentSearchCard is deliberately no longer imported: AgentWorkspace replaces
   it. The card auto-dismissed 1.8s after finishing, which is the one behaviour
   this design cannot have — the panel has to stay openable afterwards. */
import AgentWorkspace from "@/components/AgentWorkspace";
import { SummaryStatus, SummaryDetails } from "@/components/AgentSummaryCard";
import WorkingLine from "@/components/WorkingLine";
import { renderSegments } from "@/lib/streamReducer";
import RecalibrationCard from "@/components/RecalibrationCard";
import IntegrationMarquee from "@/components/IntegrationMarquee";
import ApprovalCard from "@/components/ApprovalCard";
import SystemCorrectionCard from "@/components/SystemCorrectionCard";
import { correctionKind } from "@/lib/corrections";
import "katex/dist/katex.min.css";
import { useSession, signOut, signIn } from "next-auth/react";
import { useTheme } from "@/context/ThemeContext";

// Currency symbols that must never be interpreted as the opening/closing
// delimiter of a math span. `$` is intentionally excluded — it's the real
// math delimiter; the currency-guard below neutralises accidental `$…$`
// wrappers that only contain a monetary amount.
const CURRENCY_SYMBOLS = "₦₹₱₩₫₴₸₺₼₽€£¥₽؋฿";

function preprocessMath(text: string): string {
  if (!text) return "";
  let processed = text;

  // Convert \[ and \] to $$
  processed = processed.replace(/\\\[/g, "\n$$\n").replace(/\\\]/g, "\n$$\n");
  // Convert \( and \) to $
  processed = processed.replace(/\\\(/g, "$").replace(/\\\)/g, "$");

  // Upgrade any multi-line $...$ to $$...$$
  // Ensures we don't accidentally match existing $$...$$ blocks
  processed = processed.replace(/(^|[^$\\])\$([^$]+?)\$(?!\$)/g, (match, prefix, inner) => {
    if (inner.includes("\n")) {
      return prefix + "$$" + inner + "$$";
    }
    return match;
  });

  // ── Currency guard ──────────────────────────────────────────────────
  // Models frequently emit amounts like "$₦5,000$", "₦5,000" or "$5,000"
  // where the `$` is a US-dollar sign, not a math delimiter. Left alone,
  // remark-math parses "$…$" as an inline formula and KaTeX then warns
  // ("Unrecognized Unicode character ₦"). We escape any `$` that is acting
  // as a currency marker (adjacent to a digit or a currency glyph) so it
  // renders as a literal dollar sign instead of opening a math span.
  const currencyClass = CURRENCY_SYMBOLS.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  processed = processed.replace(
    new RegExp(`\\$(?=\\s*[${currencyClass}\\d])`, "g"),
    "\\$"
  );
  // Also escape a `$` that immediately follows a currency amount (closing
  // side of a "$5,000$" style wrapper) so the pair doesn't form a span.
  processed = processed.replace(
    new RegExp(`(?<=[${currencyClass}\\d])\\$`, "g"),
    "\\$"
  );

  return processed;
}


/* NOTE: the SSE event → message reducers (`mergeStatusStep`,
   `applyStreamEvent`, answer-segment merging) now live in
   `@/lib/streamReducer` and are driven by the `useIrisStream` hook, so the
   eight hand-copied `for await` chains this file used to carry are gone. */

/* ── CSS variable map — must match iris.css exactly ── */


const C = {
  bg:           "var(--bg)",
  sidebar:      "var(--bg-sidebar)",
  sidebarBorder:"var(--border)",
  inputBg:      "var(--input-bg)",
  inputBorder:  "var(--input-border)",
  chipBg:       "var(--surface-2)",
  chipBorder:   "var(--border)",
  text:         "var(--text)",
  muted:        "var(--text-muted)",
  accent:       "var(--accent)",
  topBar:       "var(--surface)",
  topBarBorder: "var(--border)",
  userBubble:   "var(--msg-user-bg)",
  assistantBg:  "var(--msg-agent-bg)",
  dot:          "var(--accent)",
};
/* ── sidebar icons (SVG paths) ── */
function Icon({ d, size = 18, stroke = "currentColor", strokeWidth = 1.8 }: { d: string; size?: number; stroke?: string; strokeWidth?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke={stroke} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round">
      <path d={d} />
    </svg>
  );
}

/* ── Skeleton Shimmer ── */
function SkeletonShimmer() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, width: "100%", maxWidth: 300, padding: "8px 0" }}>
      <div className="shimmer-line" style={{ width: "95%", height: 16, borderRadius: 4 }} />
      <div className="shimmer-line" style={{ width: "80%", height: 14, borderRadius: 4 }} />
      <div className="shimmer-line" style={{ width: "90%", height: 12, borderRadius: 4 }} />
    </div>
  );
}
/* ── Live plan scratchpad (streamed `write_todos`) ──
   IRIS calls write_todos to lay out her plan and again to tick items off.
   The backend streams the WHOLE current checklist on every call as a `todo`
   SSE event, so this just re-renders the latest snapshot in place: pending →
   hollow circle, in_progress → spinner, completed → checkmark + strikethrough.
   This is the visible "plan → work each step → done" feed the user asked for. */
function TodoChecklist({ todos }: { todos: any[] }) {
  if (!todos || todos.length === 0) return null;
  const norm = (t: any) => {
    // `content` FIRST — that is LangChain's real Todo key
    // (langchain/agents/middleware/todo.py:25-32). Reading only the two names
    // below is why this checklist never once rendered: every item flattened to
    // "" and the filter below dropped the whole list.
    const description = String(t?.content ?? t?.description ?? t?.task_description ?? "").trim();
    let status = String(t?.status ?? t?.task_status ?? "pending").toLowerCase().replace(/-/g, "_");
    if (!["pending", "in_progress", "completed"].includes(status)) status = "pending";
    return { description, status };
  };
  const items = todos.map(norm).filter(t => t.description);
  if (items.length === 0) return null;
  const doneCount = items.filter(t => t.status === "completed").length;
  const allDone = doneCount === items.length;
  return (
    <div style={{
      margin: "0 0 12px", padding: "12px 14px",
      background: "rgba(var(--accent-rgb),0.05)",
      border: "1px solid rgba(var(--accent-rgb),0.18)",
      borderRadius: 12, animation: "fadeUp .3s ease",
    }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        marginBottom: 8,
      }}>
        <span style={{
          fontSize: 11.5, fontWeight: 700, letterSpacing: "0.05em",
          textTransform: "uppercase", color: "var(--accent-2)",
          display: "flex", alignItems: "center", gap: 6,
        }}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
          </svg>
          {allDone ? "Plan complete" : "Plan"}
        </span>
        <span style={{ fontSize: 11.5, fontWeight: 600, color: C.muted, fontVariantNumeric: "tabular-nums" }}>
          {doneCount}/{items.length}
        </span>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {items.map((t, i) => (
          <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: 9, fontSize: 13.5, lineHeight: 1.5 }}>
            <span style={{ flexShrink: 0, marginTop: 2, width: 15, height: 15, display: "inline-flex", alignItems: "center", justifyContent: "center" }}>
              {t.status === "completed" ? (
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M20 6L9 17l-5-5" />
                </svg>
              ) : t.status === "in_progress" ? (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2.5" strokeLinecap="round" style={{ animation: "spin 0.9s linear infinite" }}>
                  <path d="M21 12a9 9 0 1 1-6.219-8.56" />
                </svg>
              ) : (
                <span style={{ width: 12, height: 12, borderRadius: "50%", border: `1.6px solid var(--border-med)`, display: "inline-block" }} />
              )}
            </span>
            <span style={{
              color: t.status === "completed" ? C.muted : C.text,
              textDecoration: t.status === "completed" ? "line-through" : "none",
              fontWeight: t.status === "in_progress" ? 600 : 400,
            }}>
              {t.description}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
/* ── Clipboard helper (safe in HTTP + HTTPS) ── */
function copyToClipboard(text: string): Promise<void> {
  if (navigator?.clipboard?.writeText) {
    return navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
  }
  return Promise.resolve(fallbackCopy(text));
}
function fallbackCopy(text: string): void {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.cssText = "position:fixed;top:-9999px;left:-9999px;opacity:0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand("copy"); } catch (_) { }
  document.body.removeChild(ta);
}

/* ── Agent Reasoning Block ── */
function AgentReasoningBlock({ node, ...props }: any) {
  return (
    <div style={{
      marginTop: 8, marginBottom: 12, padding: "12px 16px",
      borderLeft: `3px solid ${C.accent}`, background: "rgba(var(--accent-rgb),0.06)",
      color: C.muted, fontSize: 13.5, fontStyle: "italic", borderRadius: "0 8px 8px 0"
    }}>
      <div style={{ fontWeight: 600, marginBottom: 8, fontSize: 11.5, textTransform: "uppercase", letterSpacing: "0.06em", color: C.accent, fontStyle: "normal" }}>Agent Reasoning</div>
      <div {...props} />
    </div>
  );
}

/* ── Message bubble ── */
function Bubble({
  msg, onRegenerate, onRefresh, onEdit, onRecover, busy
}: {
  msg: any;
  onRegenerate?: (id: string) => void;
  onRefresh?: (id: string) => void;
  onEdit?: (id: string, newContent: string) => void;
  /** Re-attach to a run whose stream dropped; offered on terminal.resumable. */
  onRecover?: (id: string) => void;
  /**
   * A run is streaming, or an approval is waiting. Edit / refresh / regenerate are
   * withheld for the duration.
   *
   * All three REWRITE history: they tell the server to delete a turn and everything
   * after it, then re-run. Offering that mid-run invites two conflicting writes to
   * the same thread, and offering it under a pending approval is worse — that
   * approval is a suspended graph whose resume is built from the very messages the
   * rewind would delete, so the server refuses it (web.rewind_skipped_pending_
   * approval) and the edit would silently land as an extra turn instead. Better to
   * not offer the button than to offer one that quietly does the wrong thing.
   */
  busy?: boolean;
}) {
  const { theme } = useTheme();
  const isDark = theme === "dark";
  const isUser = msg.role === "user";
  // ── Toolbar state ──
  const [liked, setLiked] = useState(false);
  const [copied, setCopied] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [editText, setEditText] = useState(msg.content || "");
  // Hover state — the interaction toolbar (edit/copy/refresh · like/copy/regenerate)
  // is revealed only while the pointer is over this message row, for both user
  // and agent bubbles.
  const [hovered, setHovered] = useState(false);
  const handleCopy = (text: string) => {
    copyToClipboard(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };
  const handleSaveEdit = () => {
    // The editor can be left open while a run starts (or an approval arrives) in
    // another bubble; the pencil is gone by then but this textarea is still on
    // screen with its own Enter-to-save. Re-check rather than trusting the gate
    // that was true when it opened.
    if (busy) return;
    // Compare against the prefix-stripped original so the save reliably fires
    // even when msg.content still carries a "[SYSTEM CONTEXT: …]" prefix (the
    // textarea only ever shows/edits the stripped text).
    const stripPrefix = (s: string) => (s || "").replace(/^\[SYSTEM CONTEXT:[\s\S]*?\]\s*/i, "");
    const original = stripPrefix(msg.content).trim();
    const trimmed = stripPrefix(editText).trim();
    if (trimmed && trimmed !== original) {
      onEdit && onEdit(msg.id, trimmed);
    }
    setIsEditing(false);
  };


  /* ── Workspace derivation ──
     The panel classifies its own rows straight from `statusSteps`, because it
     needs the `ns`/`parent_id` the backend now sends to nest a specialist's
     calls under the delegation that spawned them. The flat `actions` array this
     file used to build for AgentSearchCard threw both away, so it is gone along
     with the card it fed. */
  const wsSteps: any[] = msg.statusSteps ?? [];
  const wsTodos: any[] = msg.todos ?? [];
  const wsSubagents: any[] = msg.subagents ?? [];
  const wsCorrections: any[] = msg.corrections ?? [];
  const wsTranscript = renderSegments(msg.workspaceSegments ?? []);
  const running = !isUser && Boolean(msg.isStreaming);
  // Mount as soon as the run starts, so the record exists from the first event
  // instead of appearing only once something happens to be worth classifying.
  const hasWorkspace = !isUser && (
    running || wsSteps.length > 0 || wsTodos.length > 0 ||
    wsSubagents.length > 0 || wsCorrections.length > 0 || Boolean(wsTranscript.trim())
  );

  /* A parsed Final Response Contract becomes the structured chat summary. An
     empty `status` means the parse failed and `raw` holds the whole answer, so
     the body renders plain prose instead — there is no path to an empty bubble. */
  const structured = !isUser && Boolean(msg.summary && String(msg.summary.status || "").trim());
  const bodyText: string = structured
    ? String(msg.summary.summary || msg.summary.raw || msg.content || "")
    : String(msg.content || "");

  // System self-correction (a persisted blank_recovery nudge) — render as a
  // distinct, collapsible affordance, never as a user/assistant bubble. All
  // hooks above have already run, so this early return is hook-safe.
  if (msg.correction) {
    return <SystemCorrectionCard kind={msg.correction} text={msg.content} />;
  }

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        display: "flex", gap: 14, marginBottom: 24,
        flexDirection: isUser ? "row-reverse" : "row",
        animation: "fadeUp .3s ease"
      }}>
      {/* avatar */}
      <div className="msg-avatar" style={{
        width: 46, height: 46, borderRadius: "50%", flexShrink: 0, marginTop: 2,
        background: isUser ? C.chipBg : "transparent",
        display: "flex", alignItems: "center", justifyContent: "center", overflow: "hidden"
      }}>
        {isUser
          ? <span style={{ fontSize: 15, color: C.text, fontWeight: 600 }}>{msg.userNameInitials || "U"}</span>
          : <IrisLogo size={42} />}
      </div>
      {/* minWidth:0 is what lets the bubble actually shrink: a flex item's
          default min-width:auto is its min-content width, so one long token
          inside would push the row past the viewport. The 76% cap lives in CSS
          (.msg-col) because on a phone it costs more than the avatar saves. */}
      <div className="msg-col" style={{ width: "100%", minWidth: 0 }}>
        {/* ── The execution workspace ──
            Everything IRIS does while it works lives in here: the live todo
            plan, the delegation tree, tool rows, the harness nudges that steer
            it off empty responses and loops, and its own streamed prose. The
            chat stays clean — one "IRIS is working" line, then the summary.
            On completion the panel collapses itself to "▸ Worked for N
            seconds" and stays openable; it never self-dismisses. */}
        {hasWorkspace && (
          <AgentWorkspace
            running={running}
            startedAt={msg.workspaceStartedAt}
            workedMs={msg.workedMs}
            todos={wsTodos}
            statusSteps={wsSteps}
            subagents={wsSubagents}
            corrections={wsCorrections}
            transcript={wsTranscript}
            terminal={msg.terminal ?? null}
            /* False only for a record rebuilt from /history — wall-clock duration
               is not persisted, so the header shows the run's shape instead of a
               fabricated "Worked for N seconds". Live runs leave it undefined and
               the prop defaults to true. */
            durationKnown={msg.durationKnown !== false}
            onRecover={onRecover ? () => onRecover(msg.id) : undefined}
          />
        )}
        {/* Message body / edit mode */}
        {isEditing && isUser ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <textarea
              value={editText.replace(/^\[SYSTEM CONTEXT:[\s\S]*?\]\s*/i, "")}
              onChange={e => setEditText(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSaveEdit(); } if (e.key === "Escape") setIsEditing(false); }}
              autoFocus
              rows={Math.max(2, editText.split("\n").length)}
              style={{
                width: "100%", background: C.inputBg, border: `1px solid ${C.accent}`,
                borderRadius: 10, padding: "10px 14px", color: C.text, fontSize: 14.5,
                lineHeight: 1.7, resize: "vertical", fontFamily: "inherit",
                outline: "none", boxSizing: "border-box"
              }}
            />
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button
                onClick={() => setIsEditing(false)}
                style={{
                  padding: "5px 14px", borderRadius: 7, border: `1px solid var(--border-med)`, background: "transparent",
                  color: C.muted, fontSize: 12.5, cursor: "pointer", fontFamily: "inherit"
                }}>
                Cancel
              </button>
              <button
                onClick={handleSaveEdit}
                disabled={busy}
                title={busy ? "Wait for IRIS to finish before sending an edit" : undefined}
                style={{
                  padding: "5px 14px", borderRadius: 7, border: "none",
                  background: busy ? "rgba(128,128,128,0.2)" : C.accent,
                  color: busy ? C.muted : "#fff", fontSize: 12.5,
                  cursor: busy ? "not-allowed" : "pointer", fontWeight: 600, fontFamily: "inherit"
                }}>
                Save & Send
              </button>
            </div>
          </div>
        ) : (
          <div className="msg-body" style={{
            background: isUser ? C.userBubble : C.assistantBg,
            border: isUser ? `1px solid ${C.inputBorder}` : "none",
            borderRadius: isUser ? "14px 14px 4px 14px" : "0 14px 14px 14px",
            padding: isUser ? "10px 15px" : "4px 0",
            color: C.text, fontSize: 14.5, lineHeight: 1.7,

            fontFamily: "inherit",
          }}>
            {msg.recalibrating ? (
              <RecalibrationCard resumeAt={msg.resumeAt} onRetry={() => onRegenerate && onRegenerate(msg.id)} />
            ) : !isUser && (msg.typing || (msg.isStreaming && !bodyText)) ? (
              /* One live line for the whole run. The old SkeletonShimmer implied
                 prose was about to appear here, but orchestrator tokens now go
                 to the workspace channel, so nothing streams into the chat. */
              <WorkingLine />
            ) : isUser ? (
              msg.content.replace(/^\[SYSTEM CONTEXT:[\s\S]*?\]\s*/i, "")
            ) : !bodyText ? (
              /* A run can settle with no written answer (paused for approval,
                 timed out, empty completion). Say so instead of leaving an
                 empty bubble — the workspace above holds the evidence. */
              <div style={{ fontSize: 13.5, color: C.muted, fontStyle: "italic" }}>
                {msg.terminal?.reason === "paused"
                  ? "Waiting for your approval."
                  : "This turn ended without a written answer — open the workspace above to see what ran."}
              </div>
            ) : (
              /* ── The answer ──
                 When the Final Response Contract parsed, its STATUS badge sits
                 above the prose and ARTIFACTS/BLOCKERS/LEARNING below it, while
                 the SUMMARY body itself still goes through this file's own
                 markdown pipeline — links, tables and KaTeX behave exactly as
                 they do for an ordinary answer, with no second renderer config
                 to keep in sync. */
              <>
              {structured && <SummaryStatus status={String(msg.summary.status)} />}
              <ReactMarkdown
                remarkPlugins={[remarkGfm, remarkMath]}
                rehypePlugins={[
                  [
                    rehypeKatex,
                    {
                      // Don't crash on bad input; render what we can.
                      throwOnError: false,
                      errorColor: "var(--text)",
                      // Currency symbols (₦ Naira, ₹, ₱, ₩, ₫, etc.) and other
                      // stray Unicode routinely appear when the model writes an
                      // amount that lands inside $…$. KaTeX's strict mode fires a
                      // console warning ("Unrecognized Unicode character") for
                      // each one. Downgrade `unknownSymbol` to "ignore" so the
                      // glyph is rendered verbatim instead of spamming the
                      // console; keep "warn" for every other class of issue.
                      strict: (errorCode: string) =>
                        errorCode === "unknownSymbol" ? "ignore" : "warn",
                    },
                  ],
                  rehypeRaw,
                ]}

                components={({

                  p: ({ node, ...props }: any) => <div style={{ marginBottom: 12, lineHeight: 1.75, whiteSpace: "pre-line" }} {...props} />,
                  "think": AgentReasoningBlock,
                  "thinking": AgentReasoningBlock,
                  "plan": AgentReasoningBlock,
                  "reasoning": AgentReasoningBlock,
                  "internal": AgentReasoningBlock,
                  "analysis": AgentReasoningBlock,
                  "inner": AgentReasoningBlock,
                  "state": AgentReasoningBlock,
                  ul: ({ node, ...props }: any) => <ul style={{ paddingLeft: 24, marginBottom: 12, listStyleType: "disc" }} {...props} />,
                  ol: ({ node, ...props }: any) => <ol style={{ paddingLeft: 24, marginBottom: 12, listStyleType: "decimal" }} {...props} />,
                  li: ({ node, ...props }: any) => <li style={{ marginBottom: 4, lineHeight: 1.7 }} {...props} />,
                  h1: ({ node, ...props }: any) => <h1 style={{ fontSize: "1.5rem", fontWeight: 700, margin: "20px 0 10px", color: C.text, letterSpacing: "-0.01em" }} {...props} />,
                  h2: ({ node, ...props }: any) => <h2 style={{ fontSize: "1.25rem", fontWeight: 700, margin: "18px 0 8px", color: C.text, letterSpacing: "-0.01em" }} {...props} />,
                  h3: ({ node, ...props }: any) => <h3 style={{ fontSize: "1.1rem", fontWeight: 600, margin: "14px 0 6px", color: C.text }} {...props} />,
                  h4: ({ node, ...props }: any) => <h4 style={{ fontSize: "1rem", fontWeight: 600, margin: "12px 0 4px", color: C.text }} {...props} />,
                  strong: ({ node, ...props }: any) => <strong style={{ fontWeight: 700, color: C.text }} {...props} />,
                  em: ({ node, ...props }: any) => <em style={{ fontStyle: "italic", color: "inherit" }} {...props} />,
                  a: ({ node, href, ...props }: any) => <a href={href} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)", textDecoration: "none", borderBottom: "1px solid rgba(var(--accent-rgb),0.3)", transition: "border-color 0.15s" }} {...props} />,
                  blockquote: ({ node, ...props }: any) => (
                    <blockquote style={{
                      borderLeft: "3px solid rgba(var(--accent-rgb),0.4)",
                      margin: "12px 0", padding: "8px 16px",
                      background: "rgba(var(--accent-rgb),0.04)",
                      borderRadius: "0 8px 8px 0",
                      color: C.muted, fontStyle: "italic",
                    }} {...props} />
                  ),
                  hr: () => <hr style={{ border: "none", borderTop: "1px solid var(--border-dim)", margin: "16px 0" }} />,
                  table: ({ node, ...props }: any) => (
                    <div style={{ overflowX: "auto", margin: "12px 0", borderRadius: 8, border: "1px solid var(--border-dim)" }}>
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13.5 }} {...props} />
                    </div>
                  ),
                  thead: ({ node, ...props }: any) => <thead style={{ background: "var(--surface-2)" }} {...props} />,
                  th: ({ node, ...props }: any) => <th style={{ padding: "10px 14px", textAlign: "left", fontWeight: 600, fontSize: 12.5, color: C.muted, textTransform: "uppercase", letterSpacing: "0.04em", borderBottom: "1px solid var(--border-dim)" }} {...props} />,
                  td: ({ node, ...props }: any) => <td style={{ padding: "9px 14px", borderBottom: "1px solid var(--border-dim)", verticalAlign: "top" }} {...props} />,
                  tr: ({ node, ...props }: any) => <tr style={{ transition: "background 0.1s" }} {...props} />,
                  code: ({ node, inline, className, children, ...props }: any) => {
                    if (inline) {
                      return <code style={{ background: "var(--surface-3)", padding: "2px 6px", borderRadius: 4, fontFamily: "'SF Mono', 'Fira Code', 'Consolas', monospace", fontSize: 13, color: "var(--text-2)" }} {...props}>{children}</code>;
                    }
                    const lang = className?.replace("language-", "") || "";
                    return (
                      <div style={{ position: "relative", margin: "12px 0" }}>
                        {lang && (
                          <div style={{
                            display: "flex", justifyContent: "space-between", alignItems: "center",
                            background: "var(--surface-2)", padding: "6px 14px",
                            borderRadius: "8px 8px 0 0", border: "1px solid var(--border-dim)",
                            borderBottom: "none",
                          }}>
                            <span style={{ fontSize: 11.5, color: C.muted, fontWeight: 500, textTransform: "uppercase", letterSpacing: "0.04em" }}>{lang}</span>
                          </div>
                        )}
                        <pre style={{
                          background: "var(--surface-2)", padding: "14px 16px",
                          borderRadius: lang ? "0 0 8px 8px" : 8,
                          overflowX: "auto", fontFamily: "'SF Mono', 'Fira Code', 'Consolas', monospace",
                          fontSize: 13, lineHeight: 1.6,
                          border: "1px solid var(--border-med)",
                          borderTop: lang ? "none" : undefined,
                        }}><code {...props}>{children}</code></pre>
                      </div>
                    );
                  }
                } as any)}
              >
                {preprocessMath(bodyText)}
              </ReactMarkdown>
              {structured && <SummaryDetails summary={msg.summary} />}
              {/* "The tasks and their status" — the settled plan, restated under
                  the answer. Only once the run is over: while it runs, the live
                  copy belongs in the workspace above. */}
              {!msg.isStreaming && wsTodos.length > 0 && <TodoChecklist todos={wsTodos} />}
              </>
            )}
            {/* Verification quality banner — shows after response is complete while grader runs */}
            {!isUser && msg.content && !msg.isStreaming && (msg.statusSteps || []).some((s: any) => s.phase === "verifying") && (
              <div style={{
                display: "flex", alignItems: "center", gap: 8,
                marginTop: 12, padding: "8px 14px",
                background: "rgba(var(--accent-rgb),0.08)",
                border: "1px solid rgba(var(--accent-rgb),0.2)",
                borderRadius: 10, fontSize: 12.5, color: "var(--accent-2)",
                fontFamily: "'DM Sans', sans-serif",
                animation: "fadeUp .3s ease"
              }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ animation: "pulse 1.5s ease infinite" }}>
                  <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                </svg>
                Verifying response quality…
              </div>
            )}
          </div>
        )}
        {/* ── Interaction Toolbar ── */}
        {!msg.typing && !msg.isStreaming && msg.content && !isEditing && (
          <div className="msg-actions" style={{
            display: "flex", gap: 12, marginTop: 8,
            justifyContent: isUser ? "flex-end" : "flex-start",
            color: C.muted, alignItems: "center",
            // Reveal only on hover — kept mounted (space reserved) so the layout
            // never jumps; opacity + pointerEvents gate the fade in/out.
            opacity: hovered ? 1 : 0,
            pointerEvents: hovered ? "auto" : "none",
            transition: "opacity .15s ease",
          }}>
            {isUser ? (
              /* USER toolbar: Edit | Copy | Refresh — Copy is always safe, the two
                 that rewrite the thread are withheld while `busy`. */
              <>
                {!busy && (
                <button
                  onClick={() => { setEditText(msg.content); setIsEditing(true); }}
                  title="Edit message"
                  style={{ background: "none", border: "none", cursor: "pointer", padding: 0, color: "inherit", display: "flex", alignItems: "center" }}
                  onMouseEnter={e => (e.currentTarget.style.color = C.text)}
                  onMouseLeave={e => (e.currentTarget.style.color = C.muted)}>
                  <Edit2 size={13} />
                </button>
                )}
                <button
                  onClick={() => handleCopy(msg.content)}
                  title={copied ? "Copied!" : "Copy message"}
                  style={{
                    background: "none", border: "none", cursor: "pointer", padding: 0,
                    color: copied ? "#4caf50" : "inherit", display: "flex", alignItems: "center", gap: 4, transition: "color .2s"
                  }}
                  onMouseEnter={e => { if (!copied) e.currentTarget.style.color = C.text; }}
                  onMouseLeave={e => { if (!copied) e.currentTarget.style.color = C.muted; }}>
                  <Copy size={13} />
                  {copied && <span style={{ fontSize: 11, fontWeight: 600 }}>Copied!</span>}
                </button>
                {!busy && (
                <button
                  onClick={() => onRefresh && onRefresh(msg.id)}
                  title="Regenerate response"
                  style={{ background: "none", border: "none", cursor: "pointer", padding: 0, color: "inherit", display: "flex", alignItems: "center" }}
                  onMouseEnter={e => (e.currentTarget.style.color = C.text)}
                  onMouseLeave={e => (e.currentTarget.style.color = C.muted)}>
                  <RefreshCw size={13} />
                </button>
                )}
              </>
            ) : (
              /* AGENT toolbar: Like | Copy | Regenerate */
              <>
                <button
                  onClick={() => setLiked(v => !v)}
                  title={liked ? "Unlike" : "Like response"}
                  style={{
                    background: "none", border: "none", cursor: "pointer", padding: 0,
                    color: liked ? C.accent : "inherit", display: "flex", alignItems: "center", transition: "color .2s"
                  }}
                  onMouseEnter={e => { if (!liked) e.currentTarget.style.color = C.text; }}
                  onMouseLeave={e => { if (!liked) e.currentTarget.style.color = C.muted; }}>
                  <ThumbsUp size={13} fill={liked ? C.accent : "none"} />
                </button>
                <button
                  onClick={() => handleCopy(msg.content)}
                  title={copied ? "Copied!" : "Copy response"}
                  style={{
                    background: "none", border: "none", cursor: "pointer", padding: 0,
                    color: copied ? "#4caf50" : "inherit", display: "flex", alignItems: "center", gap: 4, transition: "color .2s"
                  }}
                  onMouseEnter={e => { if (!copied) e.currentTarget.style.color = C.text; }}
                  onMouseLeave={e => { if (!copied) e.currentTarget.style.color = C.muted; }}>
                  <Copy size={13} />
                  {copied && <span style={{ fontSize: 11, fontWeight: 600 }}>Copied!</span>}
                </button>
                {!busy && (
                <button
                  onClick={() => onRegenerate && onRegenerate(msg.id)}
                  title="Regenerate response"
                  style={{ background: "none", border: "none", cursor: "pointer", padding: 0, color: "inherit", display: "flex", alignItems: "center" }}
                  onMouseEnter={e => (e.currentTarget.style.color = C.text)}
                  onMouseLeave={e => (e.currentTarget.style.color = C.muted)}>
                  <RefreshCw size={13} />
                </button>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
/* ── Google Workspace connect section ──
   Real Connect Google button + live status indicator, wired to the backend
   /google/status, /google/connect and /google/disconnect endpoints. */
function GoogleWorkspaceSection() {
  const [status, setStatus] = useState<GoogleStatus | null>(null);
  const [loadingStatus, setLoadingStatus] = useState(true);
  const [disconnecting, setDisconnecting] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState(false);

  const refresh = useCallback(async () => {
    setLoadingStatus(true);
    const s = await getGoogleStatus();
    setStatus(s);
    setLoadingStatus(false);
  }, []);

  // Fetch status on mount, and re-check when the tab regains focus (e.g. after
  // returning from the Google consent screen in another tab/redirect).
  useEffect(() => {
    refresh();
    const onFocus = () => refresh();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refresh]);

  // If we just came back from the OAuth callback (?google_connected=1), surface
  // it immediately and clean the URL so a refresh doesn't re-trigger.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.has("google_connected")) {
      refresh();
      params.delete("google_connected");
      params.delete("reason");
      const clean = window.location.pathname + (params.toString() ? `?${params}` : "");
      window.history.replaceState({}, "", clean);
    }
  }, [refresh]);

  const handleConnect = async () => {
    // Mint a single-use, Bearer-authenticated ticket first, then do the full-page
    // navigation to /connect?ticket=… (the navigation itself can't carry the auth
    // header). The backend consumes the ticket before redirecting to Google, so a
    // ticket leaked via the Referer header / history is already spent.
    setConnectError(false);
    setConnecting(true);
    try {
      const ticket = await mintGoogleConnectTicket();
      window.location.href = `${googleConnectUrl()}?ticket=${encodeURIComponent(ticket)}`;
      // Leave `connecting` true — the page is navigating away.
    } catch {
      setConnectError(true);
      setConnecting(false);
    }
  };

  const handleDisconnect = async () => {
    setDisconnecting(true);
    await disconnectGoogle();
    await refresh();
    setDisconnecting(false);
  };

  const connected = status?.connected === true;
  const available = status?.available !== false;
  // Disable the Connect button while checking status or while a connect is in flight.
  const connectBusy = !available || loadingStatus || connecting;

  // ── Status dot color ──
  const dotColor = loadingStatus ? "#b0b0b0" : !available ? "#e5a23b" : connected ? "#3fb950" : "#e5534b";
  const statusLabel = loadingStatus
    ? "Checking…"
    : !available
      ? "Unavailable"
      : connected
        ? "Connected"
        : "Not connected";

  return (
    <div style={{ marginBottom: 24 }}>
      <div style={{ fontSize: 13, fontWeight: 500, color: C.text, marginBottom: 8 }}>Google Workspace</div>
      <div style={{
        padding: "12px", background: C.inputBg, borderRadius: 8,
        border: `1px solid ${C.inputBorder}`,
      }}>
        {/* Header row: Google glyph + status indicator */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
            {/* Google 'G' logo */}
            <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">
              <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>
              <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>
              <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24s.92 7.54 2.56 10.78l7.97-6.19z"/>
              <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>
            </svg>
            <span style={{ fontSize: 13, fontWeight: 500, color: C.text }}>Google account</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{
              width: 8, height: 8, borderRadius: "50%", background: dotColor,
              boxShadow: `0 0 6px ${dotColor}`,
              animation: loadingStatus ? "pulse 1.4s ease infinite" : "none",
            }} />
            <span style={{ fontSize: 12, fontWeight: 500, color: C.muted }}>{statusLabel}</span>
          </div>
        </div>

        {/* Helper / detail text */}
        <p style={{ fontSize: 12, color: C.muted, lineHeight: 1.5, marginBottom: 12 }}>
          {!available
            ? (status?.detail || "Per-user Google connect isn't configured on the server yet.")
            : connected
              ? "IRIS can act on your Google Workspace (Docs, Drive, Calendar, Sheets, Forms) on your behalf."
              : "Connect your Google account so IRIS can create and manage Docs, Drive files, Calendar events and more for you."}
        </p>

        {/* Action button */}
        {connected ? (
          <button
            onClick={handleDisconnect}
            disabled={disconnecting}
            style={{
              width: "100%", padding: "8px 14px", borderRadius: 7, fontSize: 13, fontWeight: 500,
              background: "transparent", color: "#e5534b", border: "1px solid #e5534b",
              cursor: disconnecting ? "not-allowed" : "pointer", fontFamily: "inherit",
              opacity: disconnecting ? 0.6 : 1, transition: "background 0.15s",
            }}
            onMouseEnter={e => { if (!disconnecting) e.currentTarget.style.background = "rgba(229,83,75,0.08)"; }}
            onMouseLeave={e => (e.currentTarget.style.background = "transparent")}
          >
            {disconnecting ? "Disconnecting…" : "Disconnect Google"}
          </button>
        ) : (
          <button
            onClick={handleConnect}
            disabled={connectBusy}
            style={{
              width: "100%", padding: "8px 14px", borderRadius: 7, fontSize: 13, fontWeight: 600,
              display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
              background: connectBusy ? "rgba(128,128,128,0.2)" : "#fff",
              color: connectBusy ? C.muted : "#3c4043",
              border: "1px solid var(--input-border)",
              cursor: connectBusy ? "not-allowed" : "pointer",
              fontFamily: "inherit", transition: "box-shadow 0.15s, transform 0.05s",
            }}
            onMouseEnter={e => { if (!connectBusy) e.currentTarget.style.boxShadow = "0 1px 6px rgba(0,0,0,0.18)"; }}
            onMouseLeave={e => (e.currentTarget.style.boxShadow = "none")}
          >
            <svg width="16" height="16" viewBox="0 0 48 48" aria-hidden="true">
              <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>
              <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>
              <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24s.92 7.54 2.56 10.78l7.97-6.19z"/>
              <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>
            </svg>
            {loadingStatus ? "Checking…" : connecting ? "Connecting…" : "Connect Google"}
          </button>
        )}
        {connectError && (
          <p style={{ fontSize: 12, color: "#e5534b", lineHeight: 1.5, marginTop: 10, marginBottom: 0 }}>
            Could not start the Google connect flow. Please try again.
          </p>
        )}
      </div>
    </div>
  );
}

/* ── Settings Modal ── */
function SettingsModal({ sidebarOpen, onClose, userName, userEmail, onLogout }: { sidebarOpen: boolean, onClose: () => void, userName: string, userEmail: string, onLogout: () => void }) {
  const { preference, setPreference } = useTheme();

  return (

    <div className="modal-overlay settings-modal" style={{ "--modal-left": sidebarOpen ? "240px" : "56px" } as any} onClick={onClose}>
      <div onClick={e => e.stopPropagation()} style={{
        background: C.sidebar,
        border: `1px solid ${C.sidebarBorder}`,
        borderRadius: 14, width: "100%", maxWidth: 360, padding: "24px 24px 20px",
        boxShadow: "0 24px 60px rgba(0,0,0,0.18)",
        position: "relative",
      }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 4, color: C.text, fontFamily: "inherit", letterSpacing: "-0.01em" }}>
          Settings
        </h2>
        <p style={{ fontSize: 13, color: C.muted, marginBottom: 20, fontFamily: "inherit" }}>
          Preferences and Account
        </p>

        {/* Account Info */}
        <div style={{ marginBottom: 20, padding: "12px", background: C.inputBg, borderRadius: 8, border: `1px solid ${C.inputBorder}` }}>
          <div style={{ fontSize: 12, fontWeight: 500, color: C.muted, marginBottom: 4, textTransform: "uppercase" }}>Account</div>
          <div style={{ fontSize: 14, fontWeight: 500, color: C.text }}>{userName || "User"}</div>
          <div style={{ fontSize: 13, color: C.muted, marginBottom: 12 }}>{userEmail || "Not logged in"}</div>
          <button onClick={onLogout} style={{
            width: "100%", padding: "7px 14px", borderRadius: 7, fontSize: 13, fontWeight: 500,
            background: "transparent", color: "#e5534b", border: "1px solid #e5534b",
            cursor: "pointer", fontFamily: "inherit", transition: "background 0.15s",
          }}
            onMouseEnter={e => (e.currentTarget.style.background = "rgba(229,83,75,0.08)")}
            onMouseLeave={e => (e.currentTarget.style.background = "transparent")}
          >Sign Out</button>
        </div>

        {/* Google Workspace connect */}
        <GoogleWorkspaceSection />

        {/* Theme Selector — Dark / Light / Auto */}

        <div style={{ marginBottom: 24 }}>
          <div style={{ fontSize: 13, fontWeight: 500, color: C.text, marginBottom: 8 }}>Theme</div>
          <div style={{ display: "flex", gap: 6, background: C.inputBg, padding: 4, borderRadius: 9, border: `1px solid ${C.inputBorder}` }}>
            <button
              onClick={() => setPreference("dark")}
              style={{
                flex: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 6,
                padding: "6px 10px", borderRadius: 6, border: "none", cursor: "pointer",
                background: preference === "dark" ? C.accent : "transparent",
                color: preference === "dark" ? "#fff" : C.text,
                fontSize: 12.5, fontWeight: preference === "dark" ? 600 : 400, fontFamily: "inherit",
                transition: "all 0.15s",
              }}>
              <Moon size={13} /> Dark
            </button>
            <button
              onClick={() => setPreference("light")}
              style={{
                flex: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 6,
                padding: "6px 10px", borderRadius: 6, border: "none", cursor: "pointer",
                background: preference === "light" ? C.accent : "transparent",
                color: preference === "light" ? "#fff" : C.text,
                fontSize: 12.5, fontWeight: preference === "light" ? 600 : 400, fontFamily: "inherit",
                transition: "all 0.15s",
              }}>
              <Sun size={13} /> Light
            </button>
            <button
              onClick={() => setPreference("auto")}
              style={{
                flex: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 6,
                padding: "6px 10px", borderRadius: 6, border: "none", cursor: "pointer",
                background: preference === "auto" ? C.accent : "transparent",
                color: preference === "auto" ? "#fff" : C.text,
                fontSize: 12.5, fontWeight: preference === "auto" ? 600 : 400, fontFamily: "inherit",
                transition: "all 0.15s",
              }}>
              <Sparkles size={13} /> Auto
            </button>
          </div>
        </div>


        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button onClick={onClose} style={{
            padding: "7px 14px", borderRadius: 7, fontSize: 13, fontWeight: 500,
            background: C.accent, color: "var(--bg)", border: "none",
            cursor: "pointer", fontFamily: "inherit",
            transition: "opacity 0.15s",
          }}
            onMouseEnter={e => (e.currentTarget.style.opacity = "0.88")}
            onMouseLeave={e => (e.currentTarget.style.opacity = "1")}
          >Close</button>
        </div>
      </div>
    </div>
  );
}

/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   MAIN COMPONENT
â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */
export default function HomePage() {
  const { data: session, status } = useSession();
  const [input, setInput] = useState("");
  const [threads, setThreads] = useState<any[]>([]);
  const [activeThreadId, setActiveThreadId] = useState<string>("");
  const [attachedFiles, setAttachedFiles] = useState<any[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  /* History-read honesty: `historyError` is set only when the server could not
     answer, so an empty conversation and a failed read no longer render the same.
     `historyRetry` is a nonce the banner's Retry button bumps to re-run the load
     effect (the thread id has not changed, so nothing else would re-trigger it). */
  const [historyError, setHistoryError] = useState<HistoryErrorKind | null>(null);
  const [historyRetry, setHistoryRetry] = useState(0);

  const userId = session?.user?.email || "";
  const userEmail = session?.user?.email || "";
  const userName = session?.user?.name || "";
  const userInitials = userName ? userName.split(" ").map(n => n[0]).join("").toUpperCase() : "U";
  const isLoggedIn = status === "authenticated";

  /* ── The streaming turn ──────────────────────────────────────────────────
     Shaped after LangGraph agent-chat-ui's `useStream`: ONE hook owns
     messages / isLoading / interrupt / error and exposes submit + resume +
     stop, driving our own FastAPI SSE bridge (so per-user auth, HITL
     approvals, rate limiting and uploads all keep working). Aliased to the
     names this screen already used, so the JSX below is untouched. */
  const {
    messages, setMessages,
    isLoading: loading,
    interrupt: pendingInterrupt,
    submit, regenerate, refresh, editAndResubmit, resume,
    recover,
    stop: stopGeneration,
  } = useIrisStream({
    threadId: activeThreadId,
    userInitials,
    // First message of a thread → title it in the sidebar.
    onFirstMessage: (text: string) => {
      const title = text.length > 36 ? text.slice(0, 36) + "…" : text;
      setThreads(prev => (prev.some(t => t.id === activeThreadId)
        ? prev
        : [{ id: activeThreadId, title, timestamp: Date.now() }, ...prev]));
    },
  });

  const { theme, toggle, preference } = useTheme();
  const isDark = theme === "dark";
  const [hydrated, setHydrated] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [isSearching, setIsSearching] = useState(false);
  const [showSearchModal, setShowSearchModal] = useState(false);
  const [showRecentModal, setShowRecentModal] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  // ── Greeting: client is the SOURCE OF TRUTH (zero CLS) ──────────────────────
  // The client computes the full context/user/time-aware greeting synchronously
  // on frame 1 from session.user.name + local time, so there is NEVER a text
  // pop. The server /api/greeting response is treated as an OPTIONAL enrichment
  // (e.g. a fresher subline variant) that fades in over 300ms only when it
  // actually differs — never replacing the greeting title mid-read.
  const [serverSubline, setServerSubline] = useState<string>("");
  const taRef = useRef<HTMLTextAreaElement>(null);


  useEffect(() => {
    if (status !== "authenticated" || !userId) return;
    let active = true;
    async function fetchGreeting() {
      try {
        const threadId = localStorage.getItem(`iris_active_${userId}`) || "default";
        const res = await fetch("/api/greeting", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ thread_id: threadId })
        });
        // Guard against non-2xx responses and non-JSON bodies. When the backend
        // (or its proxy) returns a plain-text error like "Internal Server Error"
        // or a 502 Bad Gateway HTML page, res.json() throws
        // "Unexpected token 'I'…". We fall back silently to the static greeting.
        if (!res.ok) {
          console.warn(`Greeting request failed with status ${res.status}; using static greeting.`);
          return;
        }
        const contentType = res.headers.get("content-type") || "";
        if (!contentType.includes("application/json")) {
          console.warn("Greeting response was not JSON; using static greeting.");
          return;
        }
        const data = await res.json().catch(() => null);
        if (!data) return;
        // Only use greeting text from API — background image is now a permanent
        // static asset (iris-bg-dark.png / iris-bg-light.png) that
        // doesn't change per-session.
        if (active && data.subline) {
          setServerSubline(data.subline);
        }
      } catch (e) {
        console.error("Failed to fetch dynamic subline:", e);
      }
    }
    fetchGreeting();
    return () => { active = false; };
  }, [status, userId, userName]);
  const bottomRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Aborting is owned by useIrisStream (`stopGeneration` above) — it holds the
  // AbortController for whichever path is streaming.
  // Scroll to bottom on messages change
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);
  // Load session from local storage on mount
  useEffect(() => {
    if (status === "authenticated" && userId) {
      const savedThreads = localStorage.getItem(`iris_threads_${userId}`);
      const savedActive  = localStorage.getItem(`iris_active_${userId}`);
      const savedSidebar = localStorage.getItem(`iris_sidebar_${userId}`);
      if (savedThreads) {
        try { setThreads(JSON.parse(savedThreads)); } catch { }
      }
      setActiveThreadId(savedActive ?? crypto.randomUUID());
      if (savedSidebar !== null) setSidebarOpen(savedSidebar === "true");
    }
    if (status !== "loading") {
      setHydrated(true);
    }
  }, [status, userId]);
  // Persist sidebar preference
  useEffect(() => {
    if (!userId) return;
    localStorage.setItem(`iris_sidebar_${userId}`, String(sidebarOpen));
  }, [sidebarOpen, userId]);
  // Save threads to local storage
  useEffect(() => {
    if (!userId) return;
    // NOTE: no `threads.length === 0` guard. It used to be here as a "don't stomp
    // saved threads with an empty array before hydration" reflex, but the mount
    // effect above already sets `hydrated` and seeds from storage — so the only
    // thing the guard actually did was make deleting your LAST thread impossible:
    // the empty array never got written, and the deleted thread came back on
    // reload. `hydrated` is the correct gate for the stomp concern.
    if (!hydrated) return;
    localStorage.setItem(`iris_threads_${userId}`, JSON.stringify(threads));
  }, [threads, userId, hydrated]);
  // Save active thread ID
  useEffect(() => {
    if (!userId || !activeThreadId) return;
    localStorage.setItem(`iris_active_${userId}`, activeThreadId);
  }, [activeThreadId, userId]);
  /* Merge the SERVER thread list into the local one.
     ────────────────────────────────────────────────────────────────────────
     localStorage alone made every conversation reachable only from the browser
     profile that created it — a new device, cleared site data, or a private
     window showed an empty sidebar over a database full of intact threads.
     This is a merge and not a replace in both directions on purpose:
       · server-only rows are threads this browser never knew about (the actual
         fix), so they are added;
       · local-only rows are a thread created moments ago whose first message
         has not been sent yet, so they are kept rather than yanked away;
       · a fetch failure returns [] and therefore changes nothing.
     Ordered newest-first because the sidebar renders in array order. */
  useEffect(() => {
    if (!hydrated || !userId) return;
    let active = true;
    (async () => {
      const remote = await fetchThreads();
      if (!active || remote.length === 0) return;
      setThreads(prev => {
        const byId = new Map<string, any>();
        for (const t of prev) byId.set(t.id, t);
        for (const r of remote) {
          const local = byId.get(r.thread_id);
          const remoteTs = (r.updated_at ?? r.created_at ?? 0) * 1000;
          byId.set(r.thread_id, {
            id: r.thread_id,
            // The local title came from the same first message, so either is
            // right; preferring the local one keeps a rename-free UI stable.
            title: local?.title || r.title || "New conversation",
            timestamp: Math.max(local?.timestamp ?? 0, remoteTs),
          });
        }
        return Array.from(byId.values()).sort((a, b) => (b.timestamp ?? 0) - (a.timestamp ?? 0));
      });
    })();
    return () => { active = false; };
  }, [hydrated, userId]);
  // Load history when activeThreadId changes
  useEffect(() => {
    if (!activeThreadId) return;
    let active = true;
    const loadHistory = async () => {
      const result = await fetchThreadHistory(activeThreadId);
      if (!active) return;
      // A failed read is NOT an empty thread. `result.messages` is only the truth
      // when `error` is absent — otherwise we leave whatever is on screen alone and
      // raise a banner, because overwriting it with [] is precisely the bug that
      // made an expired token look like deleted history.
      if (result.error) {
        setHistoryError(result.error);
        return;
      }
      setHistoryError(null);
      const history = result.messages;

      const initials = userName ? userName.split(" ").map(n => n[0]).join("").toUpperCase() : "U";
      const formatted = history.map((m: any, i: number) => {
        // A persisted blank_recovery nudge arrives as a HumanMessage (role
        // "user") tagged with a recovery `name`. It must never read as something
        // the user typed — flag it so Bubble renders the SystemCorrectionCard.
        // When `name` is absent (e.g. the SSE bridge hasn't forwarded it yet),
        // correction is null and the message maps exactly as before.
        //
        // Note the backend now WITHHOLDS a named nudge whenever the turn's
        // workspace record is carrying it, so this path fires only for a turn
        // that has no record to show it in. That is what stopped a reloaded
        // thread reading as a column of standalone amber nudge cards.
        const correction = correctionKind(m.name);
        // The turn's rebuilt execution record, on the turn's last assistant
        // message. Spread under the SAME field names the live reducer writes, so
        // a restored message and a live one reach AgentWorkspace identically —
        // hard-zeroing `statusSteps` here is exactly what used to guarantee the
        // panel could never come back after a reload.
        const ws = m.workspace;
        return {
          id: m.id || `${m.role}-${Date.now()}-${i}`,
          role: correction ? "system" : (m.role === "user" ? "user" : "assistant"),
          content: m.content,
          ...(correction ? { correction } : {}),
          userNameInitials: initials,
          statusSteps: ws?.statusSteps ?? [],
          ...(ws ? {
            todos: ws.todos ?? [],
            subagents: ws.subagents ?? [],
            corrections: ws.corrections ?? [],
            workspaceSegments: ws.workspaceSegments ?? [],
            terminal: ws.terminal ?? null,
            // Deliberately no `workedMs`/`workspaceStartedAt`: duration is not
            // persisted, and leaving them unset with durationKnown=false is what
            // keeps the header from inventing "Worked for 1 second".
            durationKnown: ws.duration_known !== false,
          } : {}),
          ...(m.summary ? { summary: m.summary } : {}),
          isStreaming: false
        };
      });
      setMessages(formatted);
    };
    loadHistory();
    return () => { active = false; };
  }, [activeThreadId, userName, historyRetry]);
  const resize = () => {
    const t = taRef.current;
    if (t) { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 180) + "px"; }
  };
  /* ── Rich Context-Aware Greetings ── */
  const getGreeting = () => {
    const now = new Date();
    const h = now.getHours();
    const min = now.getMinutes();
    const day = now.getDay();
    const date = now.getDate();
    const month = now.getMonth(); // 0-indexed
    const decimalHour = h + min / 60;
    const firstName = userName ? userName.split(" ")[0] : "";
    const greet = (title: string, subtitle: string) => ({ title, subtitle });

    const isWeekend  = day === 0 || day === 6;
    const isMonday   = day === 1;
    const isFriday   = day === 5;
    const isSunday   = day === 0;

    // Updated time bands
    // 00:00–05:00  → late night
    // 05:00–07:00  → early morning
    // 07:00–12:00  → morning
    // 12:00–13:30  → lunch
    // 13:30–15:00  → afternoon      (ends at 3 PM)
    // 15:00–16:00  → late afternoon (3–4 PM wind-down)
    // 16:00–18:00  → evening        (4–6 PM)
    // 18:00–20:30  → late evening   (6–8:30 PM)
    // 20:30+       → night
    const isLateNight    = decimalHour >= 0    && decimalHour < 5;
    const isEarlyMorning = decimalHour >= 5    && decimalHour < 7;
    const isMorning      = decimalHour >= 7    && decimalHour < 12;
    const isLunch        = decimalHour >= 12   && decimalHour < 13.5;
    const isAfternoon    = decimalHour >= 13.5 && decimalHour < 15;
    const isLateAfternoon = decimalHour >= 15  && decimalHour < 16;
    const isEvening      = decimalHour >= 16   && decimalHour < 18;
    const isLateEvening  = decimalHour >= 18   && decimalHour < 20.5;

    // Notable days
    const isNewYearsDay  = month === 0  && date === 1;
    const isChristmas    = month === 11 && date === 25;
    const isChristmasEve = month === 11 && date === 24;
    const isNewYearsEve  = month === 11 && date === 31;
    const isValentines   = month === 1  && date === 14;

    const name = firstName ? `, ${firstName}` : "";
    const n    = firstName || "";

    // ── Special occasions ────────────────────────────────────────────
    if (isNewYearsDay)  return greet(`Happy New Year${name}! 🎆`, "New year, new run. What's first?");
    if (isChristmas)    return greet(`Merry Christmas${name}! 🎄`, "Enjoy today. I'm here if you need anything.");
    if (isChristmasEve) return greet(`Christmas Eve${name}! 🎁`, "Almost there. Anything to wrap up?");
    if (isNewYearsEve)  return greet(`New Year's Eve${name}! 🥂`, "One last push before midnight.");
    if (isValentines)   return greet(`Happy Valentine's Day${name}! 💌`, "What can I help you with today?");

    // ── Late night (00:00–05:00) ─────────────────────────────────────
    if (isLateNight) {
      if (!n) return greet("Still going?", "Tell me what you're working on.");
      return greet(`Up late, ${n}.`, "What are we tackling?");
    }

    // ── Early morning (05:00–07:00) ──────────────────────────────────
    if (isEarlyMorning) {
      if (isWeekend) return greet(`Early one${name}.`, isSunday ? "Quiet Sunday start." : "Getting ahead of the weekend.");
      return greet(`Early start${name}.`, "Good time to get ahead. What's the goal?");
    }

    // ── Weekend ──────────────────────────────────────────────────────
    if (isWeekend) {
      const dayLabel = isSunday ? "Sunday" : "Saturday";
      if (isMorning) {
        if (isSunday) return greet(`Sunday morning${name}.`, "No rush. What's on your mind?");
        return greet(`Saturday${name}.`, "Weekend mode. What do you need?");
      }
      if (isLunch)  return greet(`Midday${name}.`, isSunday ? "Quiet midday. I'm here." : "Taking a breather?");
      if (isAfternoon || isLateAfternoon) {
        if (isSunday) return greet(`Sunday afternoon${name}.`, "Recharge time. Anything before the week starts?");
        return greet(`Saturday afternoon${name}.`, "Your time, your pace. What can I do?");
      }
      if (isEvening) {
        if (isSunday) return greet(`Sunday evening${name}.`, "New week is close. Anything to prep?");
        return greet(`Saturday evening${name}.`, "Winding down or gearing up?");
      }
      if (isLateEvening) {
        if (isSunday) return greet(`Sunday night${name}.`, "Almost Monday. Rest up.");
        return greet(`Saturday night${name}.`, "Hope it's been a good one.");
      }
      if (isSunday) return greet(`Late Sunday${name}.`, "Wind down — tomorrow's a new week.");
      return greet(`Late Saturday${name}.`, "Call it a night when you're ready.");
    }

    // ── Monday ───────────────────────────────────────────────────────
    if (isMonday) {
      if (isMorning)        return greet(`Monday morning${name}.`, "Week starts now. What's the plan?");
      if (isLunch)          return greet(`Monday lunch${name}.`, "Refuel. Afternoon is yours.");
      if (isAfternoon || isLateAfternoon) return greet(`Monday afternoon${name}.`, "Still plenty of day left. What's next?");
      if (isEvening)        return greet(`Monday evening${name}.`, "Day one done. What's left to close?");
      if (isLateEvening)    return greet(`Monday night${name}.`, "Week has started. Get some rest.");
      return greet(`Late Monday${name}.`, "Rest up — long week ahead.");
    }

    // ── Friday ───────────────────────────────────────────────────────
    if (isFriday) {
      if (isMorning)        return greet(`Friday morning${name}. 🎉`, "End of the week in sight. Let's close strong.");
      if (isLunch)          return greet(`Friday lunch${name}.`, "Almost there. What's left?");
      if (isAfternoon || isLateAfternoon) return greet(`Friday afternoon${name}.`, "Final stretch. What needs wrapping up?");
      if (isEvening)        return greet(`Friday evening${name}.`, "Weekend starts now. Anything before you go?");
      if (isLateEvening)    return greet(`Friday night${name}.`, "Week's behind you. Well done.");
      return greet(`Late Friday${name}.`, "Rest well.");
    }

    // ── Mid-week (Tue–Thu) ───────────────────────────────────────────
    const dayMornings: Record<number, string> = {
      2: "Tuesday. Momentum from yesterday — keep it.",
      3: "Midweek. Downhill from here.",
      4: "Thursday. One more push to Friday."
    };
    const dayEvenings: Record<number, string> = {
      2: "Tuesday done. Stay consistent.",
      3: "Over the hump.",
      4: "Thursday evening — Friday's right there."
    };
    const dayLateEvenings: Record<number, string> = {
      2: "Call it a night.",
      3: "Good work today.",
      4: "Almost Friday. Rest up."
    };

    if (isMorning)        return greet(`Good morning${name}.`, dayMornings[day] ?? "Let's focus. What's the priority?");
    if (isLunch)          return greet(`Lunch${name}.`, "Break time. Back at it soon?");
    if (isAfternoon)      return greet(`Good afternoon${name}.`, "Deep focus window. What are we solving?");
    if (isLateAfternoon)  return greet(`3 o'clock${name}.`, "Winding down from peak hours. Anything to finish?");
    if (isEvening)        return greet(`Good evening${name}.`, dayEvenings[day] ?? "How can I help you close the day?");
    if (isLateEvening)    return greet(`Evening${name}.`, dayLateEvenings[day] ?? "Wrapping up for the night?");
    return greet(`Good night${name}.`, "Rest well. Back at it tomorrow.");
  };
  const handleFiles = async (e: any) => {
    const files = Array.from(e.target.files) as File[];
    if (!files.length) return;
    for (const file of files) {
      try {
        const res = await uploadFile(file);
        setAttachedFiles(prev => [...prev, { name: file.name, path: res.path }]);
      } catch (err) {
        alert("File upload failed: " + (err instanceof Error ? err.message : "Unknown error"));
      }
    }
    e.target.value = "";
  };
  const removeFile = (idx: number) => setAttachedFiles(prev => prev.filter((_, i) => i !== idx));
  const handleNewThread = () => {
    setActiveThreadId(crypto.randomUUID());
    if (typeof window !== "undefined" && window.innerWidth <= 768) {
      setSidebarOpen(false);
    }
  };
  const handleDeleteThread = async (id: string) => {
    // Optimistic: the row disappears at once, because waiting on a round trip to
    // remove something the user just deleted feels broken. But the delete is now
    // REAL — it destroys the checkpoint server-side — so a failure has to put the
    // row back rather than leave a thread hidden that will reappear on reload.
    const removed = threads.find(t => t.id === id);
    setThreads(prev => prev.filter(t => t.id !== id));
    if (id === activeThreadId) setActiveThreadId(crypto.randomUUID());
    const ok = await deleteThread(id);
    if (!ok && removed) {
      setThreads(prev => (prev.some(t => t.id === id) ? prev : [removed, ...prev]));
      alert("Could not delete that conversation — IRIS could not be reached. It is still here.");
    }
  };
  const handleLogout = () => {
    setThreads([]);
    setActiveThreadId("");
    setMessages([]);
    signOut();
  };
  /* ══════════════════════════════════════════════════════════════════
     Send paths — thin wrappers over useIrisStream
     ──────────────────────────────────────────────────────────────────
     Each of these used to carry its own ~60-line `for await` chain over the
     SSE stream, and they had already drifted apart (only some handled
     `stream_abort`; only some cleared `isStreaming` on `rate_limit`). The
     loop now lives once in `useIrisStream` + `streamReducer`, so what is
     left here is only what is genuinely local to this screen: the composer,
     the attachment tray and the textarea height.
     ══════════════════════════════════════════════════════════════════ */
  const send = async (text: string) => {
    if ((!text.trim() && attachedFiles.length === 0) || loading) return;
    const finalQuery = text.trim();
    // Server-side paths travel as structured attachments, never inside the text…
    const attachmentPaths = attachedFiles.map((f: any) => f.path as string);
    // …while the bubble shows the file NAMES, so the user sees what they sent.
    const displayQuery = attachmentPaths.length > 0
      ? finalQuery + (finalQuery ? "\n\n" : "") + "📎 Attached: " + attachedFiles.map((f: any) => f.name).join(", ")
      : finalQuery;
    setInput("");
    setAttachedFiles([]);
    if (taRef.current) taRef.current.style.height = "auto";
    await submit({ query: finalQuery, display: displayQuery, attachments: attachmentPaths });
  };
  // The message-action handlers ARE the hook's methods now; aliased so the JSX
  // below (and the Bubble props) keep their existing names.
  const handleRegenerate = regenerate;
  const handleRefresh = refresh;
  const handleEditSubmit = editAndResubmit;

  const isEmpty = messages.length === 0;
  if (!hydrated) return null;
  if (!isLoggedIn) return <LoginScreen />;

  /* ── Gemini-style collapsed icon strip ── */
  const CollapsedIconStrip = () => {
    const iconColor = isDark ? "#ffffff" : "#4a4d52";

    const iconBtn = (title: string, onClick: () => void, children: React.ReactNode) => (
      <button
        type="button"
        title={title}
        onClick={onClick}
        style={{
          width: 40, height: 40, borderRadius: 8,
          background: "transparent", border: "none",
          cursor: "pointer", display: "flex",
          alignItems: "center", justifyContent: "center",
          color: iconColor,
          transition: "background .15s",
        }}
        onMouseEnter={e => {
          e.currentTarget.style.background = isDark ? "rgba(255, 255, 255, 0.10)" : "rgba(0, 0, 0, 0.06)";
        }}
        onMouseLeave={e => {
          e.currentTarget.style.background = "transparent";
        }}
      >
        {children}
      </button>
    );

    const userInitials = userName ? userName.split(" ").map(n => n[0]).join("").toUpperCase() : "U";

    return (
      <div className="icon-strip" style={{
        width: 56,
        minWidth: 56,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        paddingTop: 10,
        paddingBottom: 12,
        flexShrink: 0,
        background: "transparent",
      }}>
        {/* Top group */}
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 4 }}>
          {iconBtn("Open sidebar", () => setSidebarOpen(true),
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke={iconColor} strokeWidth="2.2" strokeLinecap="round">
              <rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" />
              <rect x="14" y="14" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" />
            </svg>
          )}
          {iconBtn("New chat", handleNewThread,
            <PenSquare size={20} strokeWidth={2.2} color={iconColor} />
          )}
          {iconBtn("Search chats", () => setShowSearchModal(true),
            <Search size={20} strokeWidth={2.2} color={iconColor} />
          )}
        </div>

        {/* Spacer */}
        <div style={{ flex: 1 }} />

        {/* Bottom group */}
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 4 }}>
          {iconBtn("Recent chats", () => setShowRecentModal(true),
            <History size={20} strokeWidth={2.2} color={iconColor} />
          )}
          {iconBtn("Settings", () => setShowSettings(true),
            <Settings size={20} strokeWidth={2.2} color={iconColor} />
          )}
          {/* Avatar / logout */}
          <button
            title="Sign out"
            onClick={handleLogout}
            style={{
              width: 34, height: 34, borderRadius: "50%",
              background: isDark ? "#16233d" : "#dcdfe5",
              border: `1.5px solid ${isDark ? "rgba(255,255,255,0.15)" : "#c4c7c5"}`,
              cursor: "pointer", display: "flex",
              alignItems: "center", justifyContent: "center",
              fontSize: 13, fontWeight: 700,
              color: isDark ? "#ffffff" : "#3a3d42",
              marginTop: 4, transition: "border-color .15s",
              overflow: "hidden",
            }}
            onMouseEnter={e => (e.currentTarget.style.borderColor = C.accent)}
            onMouseLeave={e => (e.currentTarget.style.borderColor = isDark ? "rgba(255,255,255,0.15)" : "#c4c7c5")}
          >
            {userInitials}
          </button>
        </div>
      </div>
    );
  };

  return (
    <div
      data-theme={theme}
      className={`app-shell ${isDark ? "theme-dark gemini-bg" : "theme-light"}`}
      style={{
        // 100% rather than 100vw: on any platform that shows a classic
        // (non-overlay) scrollbar, 100vw is WIDER than the content box, so the
        // shell overhangs by the scrollbar's width and the drawer/backdrop sit
        // slightly off. 100% of the body is exactly the usable width.
        display: "flex", width: "100%", overflow: "hidden", position: "relative",
        backgroundColor: isDark ? "#0a0a0e" : "var(--bg)",
        backgroundImage: isDark
          ? "radial-gradient(ellipse 70% 60% at 50% 45%, #16233d 0%, #0d1520 35%, #0a0a0e 70%)"
          : undefined,
        minHeight: "100vh",
      }}
    >
      {/* Ambient glowing atmosphere */}
      <div
        aria-hidden="true"
        style={{
          position: "fixed",
          top: "15%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          // Capped to the viewport: at 800×450 fixed the glow is wider than a
          // phone, and iOS Safari lets a fixed element that wide contribute to
          // horizontal overscroll — the page rubber-bands sideways.
          width: "min(800px, 150vw)",
          height: "min(450px, 55vh)",
          background: isDark
            ? "radial-gradient(circle, rgba(143, 107, 255, 0.12) 0%, rgba(77, 127, 255, 0.06) 45%, transparent 70%)"
            : "radial-gradient(circle, rgba(143, 107, 255, 0.08) 0%, rgba(77, 127, 255, 0.04) 45%, transparent 70%)",
          filter: "blur(60px)",
          zIndex: 0,
          pointerEvents: "none",
        }}
      />

      {/* ── LEFT SIDEBAR (open) ── */}
      <aside className={`sidebar ${sidebarOpen ? "open" : "closed"}`} style={{ zIndex: 10, backgroundColor: isDark ? "#0a0a0e" : "var(--sidebar, #e3e6eb)", border: "none" }}>
        {/* Header: IRIS 1.0 + collapse button */}
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          padding: "14px 14px 8px 18px", flexShrink: 0
        }}>
          <span style={{
            fontSize: 18, fontWeight: 700, color: isDark ? "#ffffff" : C.text, letterSpacing: "-0.02em",
            fontFamily: "'Georgia', serif"
          }}>IRIS <span style={{ color: C.accent }}>1.0</span></span>
          <button onClick={() => setSidebarOpen(false)} title="Collapse sidebar"
            style={{
              width: 34, height: 34, borderRadius: 8,
              background: "transparent", border: "none",
              cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
              color: isDark ? "#ffffff" : "#4a4d52",
              transition: "background .15s, color .15s"
            }}
            onMouseEnter={e => { e.currentTarget.style.background = isDark ? "rgba(255, 255, 255, 0.08)" : "rgba(0, 0, 0, 0.06)"; }}
            onMouseLeave={e => { e.currentTarget.style.background = "transparent"; }}>
            <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" />
              <rect x="14" y="14" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" />
            </svg>
          </button>
        </div>
        {/* Nav items */}
        <nav style={{ padding: "4px 8px", flexShrink: 0, display: "flex", flexDirection: "column", gap: 4 }}>
          <button onClick={handleNewThread}
            style={{
              width: "100%", display: "flex", alignItems: "center", gap: 12,
              padding: "10px 12px", borderRadius: 10,
              background: "transparent", border: "none",
              cursor: "pointer", color: isDark ? "#ffffff" : "#3a3d42", fontSize: 14, fontFamily: "inherit",
              fontWeight: 500, textAlign: "left", transition: "background .15s"
            }}
            onMouseEnter={e => e.currentTarget.style.background = isDark ? "rgba(255, 255, 255, 0.08)" : "rgba(0, 0, 0, 0.06)"}
            onMouseLeave={e => e.currentTarget.style.background = "transparent"}>
            <PenSquare size={19} strokeWidth={2} style={{ color: isDark ? "#ffffff" : "#3a3d42", flexShrink: 0 }} />
            <span>New chat</span>
          </button>

          {isSearching ? (
            <div style={{
              display: "flex", alignItems: "center",
              background: "var(--surface-2)",
              borderRadius: 10, padding: "8px 12px"
            }}>
              <Search size={17} strokeWidth={2} style={{ color: isDark ? "#ffffff" : "#6b6f75", marginRight: 8 }} />
              <input
                autoFocus
                type="text"
                placeholder="Search chats..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                style={{
                  background: "transparent", border: "none", color: isDark ? "#ffffff" : C.text, outline: "none",
                  fontSize: 13, width: "100%", fontFamily: "inherit"
                }}
              />
              <button onClick={() => { setIsSearching(false); setSearchQuery(""); }} style={{ background: "none", border: "none", color: isDark ? "#ffffff" : "#6b6f75", cursor: "pointer", marginLeft: 4 }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
              </button>
            </div>
          ) : (
            <button onClick={() => setIsSearching(true)}
              style={{
                width: "100%", display: "flex", alignItems: "center", gap: 12,
                padding: "10px 12px", borderRadius: 10,
                background: "transparent", border: "none",
                cursor: "pointer", color: isDark ? "#ffffff" : "#3a3d42", fontSize: 14, fontFamily: "inherit",
                fontWeight: 500, textAlign: "left", transition: "background .15s"
              }}
              onMouseEnter={e => e.currentTarget.style.background = isDark ? "rgba(255, 255, 255, 0.08)" : "rgba(0, 0, 0, 0.06)"}
              onMouseLeave={e => e.currentTarget.style.background = "transparent"}>
              <Search size={19} strokeWidth={2} style={{ color: isDark ? "#ffffff" : "#3a3d42", flexShrink: 0 }} />
              <span>Search chats</span>
            </button>
          )}
        </nav>
        {/* Recents */}
        <div style={{ padding: "14px 8px 6px 18px", flexShrink: 0 }}>
          <span style={{ fontSize: 12, color: isDark ? "#9aa0a6" : "#6b6f75", fontWeight: 600, letterSpacing: "0.04em", textTransform: "uppercase" }}>Recents</span>
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "0 8px" }}>
          {threads.filter(t => t.title.toLowerCase().includes(searchQuery.toLowerCase())).length === 0 ? (
            <div style={{
              margin: "8px 4px", padding: "14px 12px", borderRadius: 10,
              border: `1px dashed ${C.sidebarBorder}`, color: "var(--text-muted)", fontSize: 13,
              textAlign: "center", lineHeight: 1.6
            }}>
              {searchQuery ? "No matches found" : "Your conversations\nwill appear here"}
            </div>
          ) : (
            threads.filter(t => t.title.toLowerCase().includes(searchQuery.toLowerCase())).map((t) => (
              <div key={t.id} style={{ display: "flex", alignItems: "center", gap: 4, width: "100%", position: "relative", marginBottom: 2 }}>
                <button
                  onClick={() => {
                    setActiveThreadId(t.id);
                    if (typeof window !== "undefined" && window.innerWidth <= 768) {
                      setSidebarOpen(false);
                    }
                  }}
                  style={{
                    flex: 1, display: "flex", alignItems: "center", justifyContent: "space-between",
                    padding: "9px 10px", borderRadius: 8,
                    background: t.id === activeThreadId ? (isDark ? "rgba(143, 107, 255, 0.22)" : "var(--accent-subtle-2)") : "transparent",
                    border: t.id === activeThreadId ? (isDark ? "1px solid rgba(143, 107, 255, 0.35)" : "none") : "none",
                    cursor: "pointer", color: t.id === activeThreadId ? "#ffffff" : (isDark ? "#e8eaed" : "#4a4d52"),
                    fontSize: 13.5, fontFamily: "inherit", textAlign: "left",
                    transition: "background .15s", gap: 6, overflow: "hidden"
                  }}
                  onMouseEnter={e => { if (t.id !== activeThreadId) e.currentTarget.style.background = "var(--surface-hover)"; }}
                  onMouseLeave={e => { if (t.id !== activeThreadId) e.currentTarget.style.background = "transparent"; }}>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
                    {t.title}
                  </span>
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); handleDeleteThread(t.id); }}
                  title="Delete chat"
                  style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", padding: "8px", display: "flex", alignItems: "center" }}
                  onMouseEnter={e => e.currentTarget.style.color = "var(--red)"}
                  onMouseLeave={e => e.currentTarget.style.color = "var(--text-muted)"}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                </button>
              </div>
            ))
          )}
        </div>
        {/* Footer */}
        <div style={{
          borderTop: `1px solid ${C.sidebarBorder}`, padding: "12px 12px",
          display: "flex", alignItems: "center", justifyContent: "space-between", flexShrink: 0
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{
              width: 34, height: 34, borderRadius: "50%",
              background: isDark ? "#16233d" : "#dcdfe5",
              border: `1.5px solid ${isDark ? "rgba(255,255,255,0.15)" : "#c4c7c5"}`,
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 14, fontWeight: 700, color: isDark ? "#ffffff" : "#3a3d42", flexShrink: 0
            }}>
              {userName ? userName.split(" ").map(n => n[0]).join("").toUpperCase() : "U"}
            </div>
            <div>
              <div style={{ fontSize: 13.5, fontWeight: 600, color: C.text }}>{userName ? userName.split(" ")[0] : "User"}</div>
              <div style={{ fontSize: 11.5, color: isDark ? "#9aa0a6" : "#6b6f75" }}>Premium workspace</div>
            </div>
          </div>
          <button onClick={() => setShowSettings(true)} title="Settings"
            style={{
              width: 34, height: 34, borderRadius: 8,
              background: "transparent", border: "none",
              cursor: "pointer", display: "flex",
              alignItems: "center", justifyContent: "center", transition: "background .15s",
              color: isDark ? "#ffffff" : "#3a3d42"
            }}
            onMouseEnter={e => e.currentTarget.style.background = isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.06)"}
            onMouseLeave={e => e.currentTarget.style.background = "transparent"}>
            <Settings size={19} strokeWidth={2} />
          </button>
        </div>
      </aside>

      <div className={`mobile-backdrop ${sidebarOpen ? "open" : ""}`} onClick={() => setSidebarOpen(false)} />

      {/* ── GEMINI-STYLE COLLAPSED ICON STRIP (shown when sidebar is closed) ── */}
      <div className="desktop-only" style={{ height: "100%", position: "relative", zIndex: 1 }}>
        {!sidebarOpen && <CollapsedIconStrip />}
      </div>

      {showSettings && <SettingsModal sidebarOpen={sidebarOpen} onClose={() => setShowSettings(false)} userName={userName} userEmail={userEmail} onLogout={handleLogout} />}

      {showSearchModal && (
        <div className="modal-overlay search-modal" style={{ "--modal-left": sidebarOpen ? "240px" : "56px" } as any} onClick={() => setShowSearchModal(false)}>
          <div style={{
            width: "100%", maxWidth: 500, background: isDark ? "rgba(30,30,30,0.75)" : "rgba(227, 230, 235, 0.92)",
            backdropFilter: "blur(24px) saturate(150%)", border: `1px solid ${C.sidebarBorder}`,
            borderRadius: 12, overflow: "hidden", display: "flex", flexDirection: "column", maxHeight: "60vh",
            boxShadow: "0 24px 60px rgba(0,0,0,0.2)"
          }} onClick={e => e.stopPropagation()}>
            <div style={{ display: "flex", alignItems: "center", padding: "16px", borderBottom: `1px solid ${C.sidebarBorder}` }}>
              <Search size={20} style={{ color: C.muted, marginRight: 12 }} />
              <input
                autoFocus
                placeholder="Search recent chats..."
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
                onKeyDown={e => {
                  if (e.key === "Enter") {
                    const filtered = threads.filter(t => t.title.toLowerCase().includes(searchQuery.toLowerCase()));
                    if (filtered.length > 0) {
                      setActiveThreadId(filtered[0].id);
                      setShowSearchModal(false);
                    }
                  }
                  if (e.key === "Escape") setShowSearchModal(false);
                }}
                style={{ background: "transparent", border: "none", outline: "none", color: C.text, fontSize: 16, width: "100%", fontFamily: "inherit" }}
              />
            </div>
            <div style={{ overflowY: "auto", padding: 8 }}>
              {threads.filter(t => t.title.toLowerCase().includes(searchQuery.toLowerCase())).map((t) => (
                <button key={t.id} onClick={() => { setActiveThreadId(t.id); setShowSearchModal(false); }}
                  style={{
                    width: "100%", padding: "12px 16px", background: "transparent", border: "none",
                    color: C.text, textAlign: "left", cursor: "pointer", borderRadius: 8,
                    fontSize: 14, fontFamily: "inherit",
                    transition: "background 0.1s"
                  }}
                  onMouseEnter={e => e.currentTarget.style.background = "rgba(120,120,120,0.15)"}
                  onMouseLeave={e => e.currentTarget.style.background = "transparent"}
                >
                  {t.title}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ── RECENT CHATS POPUP ── */}
      {showRecentModal && (
        <div className="modal-overlay search-modal" style={{ "--modal-left": sidebarOpen ? "240px" : "56px" } as any} onClick={() => setShowRecentModal(false)}>
          <div style={{
            width: "100%", maxWidth: 420, background: isDark ? "rgba(30,30,30,0.82)" : "rgba(227, 230, 235, 0.94)",
            backdropFilter: "blur(24px) saturate(150%)", border: `1px solid ${C.sidebarBorder}`,
            borderRadius: 12, overflow: "hidden", display: "flex", flexDirection: "column", maxHeight: "65vh",
            boxShadow: "0 24px 60px rgba(0,0,0,0.22)"
          }} onClick={e => e.stopPropagation()}>
            <div style={{ display: "flex", alignItems: "center", padding: "14px 16px", borderBottom: `1px solid ${C.sidebarBorder}` }}>
              <History size={18} style={{ color: C.accent, marginRight: 10, flexShrink: 0 }} />
              <span style={{ fontSize: 14, fontWeight: 600, color: C.text }}>Recent Chats</span>
              <span style={{ marginLeft: "auto", fontSize: 12, color: C.muted }}>{threads.length} thread{threads.length !== 1 ? "s" : ""}</span>
            </div>
            <div style={{ overflowY: "auto", padding: 8 }}>
              {threads.length === 0 ? (
                <div style={{ padding: "24px 16px", textAlign: "center", color: C.muted, fontSize: 13 }}>No chats yet — start a conversation!</div>
              ) : threads.map((t) => (
                <button key={t.id} onClick={() => { setActiveThreadId(t.id); setShowRecentModal(false); }}
                  style={{
                    width: "100%", padding: "10px 14px", background: activeThreadId === t.id ? `rgba(${isDark ? "255,255,255" : "0,0,0"},0.07)` : "transparent",
                    border: "none", color: C.text, textAlign: "left", cursor: "pointer", borderRadius: 8,
                    fontSize: 13.5, fontFamily: "inherit", transition: "background 0.1s",
                    display: "flex", flexDirection: "column", gap: 2,
                  }}
                  onMouseEnter={e => e.currentTarget.style.background = `rgba(${isDark ? "255,255,255" : "0,0,0"},0.07)`}
                  onMouseLeave={e => e.currentTarget.style.background = activeThreadId === t.id ? `rgba(${isDark ? "255,255,255" : "0,0,0"},0.07)` : "transparent"}
                >
                  <span style={{ fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.title}</span>
                  {t.preview && <span style={{ fontSize: 12, color: C.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.preview}</span>}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* minWidth:0 lets this flex child actually shrink — without it a long
          code block or table pushes the whole shell wider than the viewport
          and the page scrolls sideways on a phone. */}
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", overflow: "hidden", position: "relative" }}>
        {/* Universal Adaptive Topbar — always visible across Desktop, Tablet & Mobile */}
        <div className="app-topbar" style={{
          minHeight: 54, display: "flex", alignItems: "center", justifyContent: "space-between",
          padding: "0 clamp(10px, 3vw, 20px)",
          borderBottom: `1px solid ${isDark ? "rgba(255, 255, 255, 0.08)" : "rgba(0, 0, 0, 0.08)"}`,
          background: isDark ? "rgba(10, 10, 14, 0.82)" : "rgba(227, 230, 235, 0.90)",
          backdropFilter: "blur(16px) saturate(140%)",
          WebkitBackdropFilter: "blur(16px) saturate(140%)",
          flexShrink: 0, zIndex: 20
        }}>
          {/* Left section: Sidebar toggle + Logo / Title */}
          <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
            {/* Always rendered. Whether it is VISIBLE is a viewport question, so
                CSS answers it (.topbar-menu-btn rules below) — this used to be
                `window.innerWidth <= 768` read during render with no resize
                listener, which meant a window dragged across the breakpoint kept
                the stale answer until some unrelated state change re-rendered.
                The hide rule carries !important because `display: flex` is set
                inline here, and inline styles otherwise win. */}
            <button
              onClick={() => setSidebarOpen(prev => !prev)}
              aria-label={sidebarOpen ? "Close sidebar" : "Open sidebar"}
              aria-expanded={sidebarOpen}
              title={sidebarOpen ? "Close sidebar" : "Open sidebar"}
              className={`touch-target-lg topbar-menu-btn${sidebarOpen ? " is-sidebar-open" : ""}`}
              style={{
                background: "transparent", border: "none",
                color: isDark ? "#ffffff" : "#3a3d42",
                padding: 8, cursor: "pointer", display: "flex",
                alignItems: "center", justifyContent: "center", borderRadius: 8,
                transition: "background 0.15s"
              }}
              onMouseEnter={e => (e.currentTarget.style.background = isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.06)")}
              onMouseLeave={e => (e.currentTarget.style.background = "transparent")}
            >
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                <line x1="3" y1="12" x2="21" y2="12"></line>
                <line x1="3" y1="6" x2="21" y2="6"></line>
                <line x1="3" y1="18" x2="21" y2="18"></line>
              </svg>
            </button>
            
            <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
              <span className="topbar-brand" style={{
                fontSize: 17, fontWeight: 700,
                color: isDark ? "#ffffff" : C.text,
                fontFamily: "'Georgia', serif",
                letterSpacing: "-0.02em",
                flexShrink: 0
              }}>
                IRIS <span style={{ color: C.accent }}>1.0</span>
              </span>
              
              {!isEmpty && activeThreadId && (
                <span className="desktop-title-pill" style={{
                  fontSize: 12, color: C.muted,
                  padding: "2px 8px", borderRadius: 6,
                  background: isDark ? "rgba(255,255,255,0.05)" : "rgba(0,0,0,0.04)",
                  border: `1px solid ${isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.06)"}`,
                  maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                }}>
                  {threads.find(t => t.id === activeThreadId)?.title || "Active conversation"}
                </span>
              )}
            </div>
          </div>

          {/* Right section: New Chat button + Theme Switcher + Settings */}
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
            {/* ── High-Visibility New Chat Action Button ── */}
            <button
              onClick={handleNewThread}
              aria-label="Start new chat"
              title="Start new chat"
              className="touch-target-lg topbar-new-chat-btn"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "6px 12px",
                borderRadius: 9,
                background: isDark
                  ? "linear-gradient(135deg, rgba(143, 107, 255, 0.22) 0%, rgba(77, 127, 255, 0.16) 100%)"
                  : "linear-gradient(135deg, rgba(77, 127, 255, 0.14) 0%, rgba(143, 107, 255, 0.10) 100%)",
                border: isDark
                  ? "1px solid rgba(143, 107, 255, 0.38)"
                  : "1px solid rgba(77, 127, 255, 0.30)",
                color: isDark ? "#ffffff" : "var(--accent-2, #4d7fff)",
                cursor: "pointer",
                fontSize: 13,
                fontWeight: 600,
                fontFamily: "inherit",
                transition: "all 0.18s ease",
                boxShadow: isDark
                  ? "0 2px 8px rgba(143, 107, 255, 0.15)"
                  : "0 2px 8px rgba(77, 127, 255, 0.08)",
              }}
              onMouseEnter={e => {
                e.currentTarget.style.background = isDark
                  ? "linear-gradient(135deg, rgba(143, 107, 255, 0.32) 0%, rgba(77, 127, 255, 0.24) 100%)"
                  : "linear-gradient(135deg, rgba(77, 127, 255, 0.22) 0%, rgba(143, 107, 255, 0.16) 100%)";
                e.currentTarget.style.transform = "translateY(-1px)";
              }}
              onMouseLeave={e => {
                e.currentTarget.style.background = isDark
                  ? "linear-gradient(135deg, rgba(143, 107, 255, 0.22) 0%, rgba(77, 127, 255, 0.16) 100%)"
                  : "linear-gradient(135deg, rgba(77, 127, 255, 0.14) 0%, rgba(143, 107, 255, 0.10) 100%)";
                e.currentTarget.style.transform = "none";
              }}
            >
              <PenSquare size={16} strokeWidth={2.3} />
              <span className="topbar-new-chat-label">New chat</span>
            </button>

            {/* Quick Theme Toggle */}
            <button
              onClick={toggle}
              aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
              title={isDark ? "Switch to light theme" : "Switch to dark theme"}
              className="touch-target-lg topbar-icon-btn"
              style={{
                width: 36, height: 36, borderRadius: 8,
                background: "transparent", border: "none",
                cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
                color: isDark ? "#ffffff" : "#4a4d52",
                transition: "background 0.15s"
              }}
              onMouseEnter={e => (e.currentTarget.style.background = isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.06)")}
              onMouseLeave={e => (e.currentTarget.style.background = "transparent")}
            >
              {isDark ? <Sun size={18} strokeWidth={2} /> : <Moon size={18} strokeWidth={2} />}
            </button>

            {/* Settings Button */}
            <button
              onClick={() => setShowSettings(true)}
              aria-label="Settings"
              title="Settings"
              className="touch-target-lg topbar-icon-btn"
              style={{
                width: 36, height: 36, borderRadius: 8,
                background: "transparent", border: "none",
                cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
                color: isDark ? "#ffffff" : "#4a4d52",
                transition: "background 0.15s"
              }}
              onMouseEnter={e => (e.currentTarget.style.background = isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.06)")}
              onMouseLeave={e => (e.currentTarget.style.background = "transparent")}
            >
              <Settings size={18} strokeWidth={2} />
            </button>
          </div>
        </div>

        {/* ── CONTENT ── */}
        <div style={{ flex: 1, minWidth: 0, overflowY: "auto", display: "flex", flexDirection: "column" }}>
          {/* History could not be read. Shown INSTEAD of silently rendering an empty
              thread, which is what the old `return []` did — a user with an expired
              token watched their conversation disappear and had no affordance at all.
              An expired session gets a sign-in button (retrying would loop); anything
              else gets Retry, which bumps the nonce the load effect depends on. */}
          {historyError && (
            <div
              role="status"
              style={{
                margin: "12px auto 0", maxWidth: 720, width: "calc(100% - 32px)",
                display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
                padding: "10px 14px", borderRadius: 10, fontSize: 13, lineHeight: 1.5,
                background: isDark ? "rgba(255,176,32,0.10)" : "rgba(255,176,32,0.14)",
                border: `1px solid ${isDark ? "rgba(255,176,32,0.30)" : "rgba(210,140,10,0.35)"}`,
                color: isDark ? "#f0d9a8" : "#7a5200",
              }}
            >
              <span style={{ flex: 1, minWidth: 180 }}>
                {historyError === "unauthorized"
                  ? "Your session expired, so this conversation could not be loaded. Sign in again to get it back — nothing has been lost."
                  : historyError === "unreachable"
                  ? "Could not reach IRIS to load this conversation. Your messages are still saved."
                  : "IRIS could not read this conversation right now. Your messages are still saved."}
              </span>
              <button
                onClick={() => (historyError === "unauthorized" ? signIn() : setHistoryRetry(n => n + 1))}
                className="touch-target-lg"
                style={{
                  padding: "6px 12px", borderRadius: 8, cursor: "pointer", fontSize: 12,
                  fontWeight: 600, whiteSpace: "nowrap",
                  background: isDark ? "rgba(255,255,255,0.10)" : "rgba(0,0,0,0.06)",
                  border: `1px solid ${isDark ? "rgba(255,255,255,0.18)" : "rgba(0,0,0,0.14)"}`,
                  color: "inherit",
                }}
              >
                {historyError === "unauthorized" ? "Sign in again" : "Retry"}
              </button>
            </div>
          )}
          {isEmpty ? (
            /* ── EMPTY STATE ──
               Gap and padding are fluid (see `.empty-state`) so the hero + composer
               still both fit on a 360×640 phone and in landscape. */
            <div className="empty-state">
              <div style={{ display: "flex", flexDirection: "column", gap: 12, alignItems: "center", textAlign: "center" }}>
                <h1 style={{
                  fontSize: "clamp(22px, 5vw, 30px)", fontWeight: 300,
                  letterSpacing: "-0.01em", margin: 0, color: C.text,
                  fontFamily: "'Georgia', 'Times New Roman', serif",
                  lineHeight: 1.4
                }}>
                  {getGreeting().title}
                </h1>
                {/* Context-aware, user-aware subline */}
                <p style={{
                  fontSize: "clamp(13px, 3vw, 15px)",
                  color: C.muted,
                  margin: "2px 0 0",
                  maxWidth: 520,
                  lineHeight: 1.6,
                  fontWeight: 400,
                  transition: "opacity 300ms ease",
                }}>
                  {serverSubline || getGreeting().subtitle}
                </p>
                {/* Integration Showcase Marquee */}

                <div style={{ marginTop: -10, marginBottom: -10, width: "100%" }}>
                  <IntegrationMarquee />
                </div>
              </div>
              {/* input box */}
              <div style={{ width: "100%", maxWidth: 680 }} className="chat-input-wrapper">
                <div className="chat-input-container" style={{
                  background: isDark ? "rgb(28, 30, 37)" : C.inputBg,
                  border: isDark ? "1px solid rgba(255, 255, 255, 0.14)" : `1px solid ${C.inputBorder}`,
                  borderRadius: 16, padding: "clamp(12px, 3vw, 18px) clamp(12px, 3vw, 18px) clamp(10px, 2.5vw, 14px)", boxShadow: isDark ? "0 8px 30px rgba(0,0,0,0.8)" : "var(--shadow-md)"
                }}>
                  {attachedFiles.length > 0 && (
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 10 }}>
                      {attachedFiles.map((f, i) => (
                        <div key={i} style={{
                          display: "flex", alignItems: "center", gap: 6,
                          background: "var(--surface-2)", border: "1px solid var(--input-border)",
                          borderRadius: 8, padding: "4px 10px", fontSize: 12, color: C.text
                        }}>
                          <span>📄</span>
                          <span style={{ maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.name}</span>
                          <button onClick={() => removeFile(i)} style={{
                            background: "none", border: "none",
                            cursor: "pointer", color: C.muted, fontSize: 14, lineHeight: 1, padding: 0
                          }}>×</button>
                        </div>
                      ))}
                    </div>
                  )}
                  <textarea
                    ref={taRef}
                    value={input}
                    onChange={e => { setInput(e.target.value); resize(); }}
                    onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); } }}
                    placeholder="How can I help you today?"
                    rows={1}
                    style={{
                      width: "100%", background: "transparent", border: "none", outline: "none",
                      color: C.text, fontSize: 15, lineHeight: 1.6, resize: "none",
                      fontFamily: "inherit", maxHeight: 180, overflowY: "auto",
                      marginBottom: 10
                    }}
                  />
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                    <input ref={fileInputRef} type="file" multiple onChange={handleFiles}
                      style={{ display: "none" }} accept="*/*" />
                    <button
                      onClick={() => fileInputRef.current?.click()}
                      title="Attach files"
                      aria-label="Attach files"
                      className="touch-target"
                      style={{
                        width: 30, height: 30, borderRadius: "50%", background: "var(--surface-2)",
                        border: "none", cursor: "pointer", color: C.muted, fontSize: 20, lineHeight: 1,
                        display: "flex", alignItems: "center", justifyContent: "center",
                        transition: "background .15s"
                      }}
                      onMouseEnter={e => e.currentTarget.style.background = "var(--surface-3)"}
                      onMouseLeave={e => e.currentTarget.style.background = "var(--surface-2)"}>
                      +
                    </button>
                    <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
                      {loading ? (
                        <button onClick={stopGeneration} title="Stop generating" aria-label="Stop generating"
                          className="touch-target"
                          style={{
                            width: 32, height: 32, borderRadius: 8, border: "none",
                            background: C.accent, cursor: "pointer",
                            display: "flex", alignItems: "center", justifyContent: "center",
                            transition: "background .2s", boxShadow: `0 0 10px var(--accentShadow)`
                          }}>
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--bg)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                            <rect x="3" y="3" width="18" height="18" rx="2" fill="var(--bg)" stroke="none" />
                          </svg>
                        </button>
                      ) : (
                        <button onClick={() => send(input)} aria-label="Send message"
                          className="touch-target"
                          disabled={(!input.trim() && attachedFiles.length === 0) || loading}
                          style={{
                            width: 32, height: 32, borderRadius: 8, border: "none",
                            background: (input.trim() || attachedFiles.length > 0) && !loading ? C.accent : "rgba(128,128,128,0.2)",
                            cursor: (input.trim() || attachedFiles.length > 0) && !loading ? "pointer" : "not-allowed",
                            display: "flex", alignItems: "center", justifyContent: "center",
                            transition: "background .2s",
                            boxShadow: (input.trim() || attachedFiles.length > 0) && !loading ? `0 0 10px var(--accentShadow)` : "none"
                          }}>
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={((input.trim() || attachedFiles.length > 0) && !loading) ? "var(--bg)" : "var(--muted)"} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                            <line x1="22" y1="2" x2="11" y2="13" />
                            <polygon points="22 2 15 22 11 13 2 9 22 2" fill={((input.trim() || attachedFiles.length > 0) && !loading) ? "var(--bg)" : "var(--muted)"} stroke="none" />
                          </svg>
                        </button>
                      )}
                    </div>
                  </div>
                </div>

              </div>
            </div>
          ) : (
            /* ── CHAT VIEW ──
               `.chat-column` carries the fluid gutter + bottom clearance for the
               floating composer, so the column and the composer stay optically
               aligned at every width. */
            <div className="chat-column" style={{ flex: 1 }}>
              {messages.map((m, i) => (
                <Bubble
                  key={m.id ?? i}
                  msg={m}
                  onRegenerate={handleRegenerate}
                  onRefresh={handleRefresh}
                  onEdit={handleEditSubmit}
                  onRecover={recover}
                  // Withhold the three history-rewriting actions while a run is
                  // streaming or an approval is pending — see Bubble's `busy`.
                  busy={loading || !!pendingInterrupt}
                />
              ))}
              <div ref={bottomRef} />
            {/* The HITL approval card is NOT mounted here. It used to be — both
                inline in the transcript AND in the floating dock below — so a
                single pending approval rendered twice at once, under two
                different headlines. The dock is the one that matters: it is
                pinned above the composer and disables the input, so it cannot
                be scrolled away from while the whole conversation is blocked. */}
            </div>
          )}
        </div>

        {/* ── HITL APPROVAL DOCK — the single mount ── */}
        {pendingInterrupt && (
          <div className="composer-dock" style={{
            position: "absolute", bottom: 0, left: 0, right: 0,
            zIndex: 50,
            paddingTop: 0,
            background: `linear-gradient(transparent, ${C.bg} 20%)`,
            display: "flex", flexDirection: "column", alignItems: "center",
          }}>
            <div className="composer-col">
              {/* No separate attention bar. It said "IRIS needs your approval
                  before proceeding" directly above a card whose own header said
                  "IRIS wants to take an action" — two headlines competing to
                  introduce the same thing. The card states it once. */}
              <ApprovalCard
                interrupt={pendingInterrupt}
                disabled={loading}
                /* Both decisions go through the hook's `resume`, which streams
                   what follows into a fresh bubble. It clears the pending
                   interrupt UP FRONT, so a run that pauses AGAIN right after
                   approval (an approval chain across a long multi-step task)
                   still gets its next card — the old `finally` here wiped it. */
                onApprove={(editedArgs) => resume("approve", editedArgs)}
                onReject={() => resume("reject")}
              />
            </div>
          </div>
        )}

        {/* ── FLOATING INPUT (chat mode) ── */}

        {!isEmpty && (
          <div style={{
            position: "absolute", bottom: 0, left: 0, right: 0,
            background: pendingInterrupt ? "transparent" : `linear-gradient(transparent, ${C.bg} 38%)`,
            opacity: pendingInterrupt ? 0.35 : 1,
            pointerEvents: pendingInterrupt ? "none" : "auto",
            transition: "opacity 0.2s ease",
          }} className="chat-input-wrapper composer-dock">
            <div className="composer-col">
              <div className="chat-input-container" style={{
                background: isDark ? "rgb(28, 30, 37)" : C.inputBg,
                border: isDark ? "1px solid rgba(255, 255, 255, 0.14)" : `1px solid ${C.inputBorder}`,
                borderRadius: 16, padding: "clamp(11px, 3vw, 14px) clamp(12px, 3.5vw, 16px) clamp(10px, 2.5vw, 12px)",
                boxShadow: isDark ? "0 12px 40px rgba(0,0,0,0.85)" : "var(--shadow-lg)"
              }}>
                {attachedFiles.length > 0 && (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 10 }}>
                    {attachedFiles.map((f, i) => (
                      <div key={i} style={{
                        display: "flex", alignItems: "center", gap: 6,
                        background: "var(--surface-2)", border: "1px solid var(--input-border)",
                        borderRadius: 8, padding: "4px 10px", fontSize: 12, color: C.text
                      }}>
                        <span>📄</span>
                        <span style={{ maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.name}</span>
                        <button onClick={() => removeFile(i)} style={{
                          background: "none", border: "none",
                          cursor: "pointer", color: C.muted, fontSize: 14, lineHeight: 1, padding: 0
                        }}>×</button>
                      </div>
                    ))}
                  </div>
                )}
                <textarea
                  ref={taRef}
                  value={input}
                  onChange={e => { setInput(e.target.value); resize(); }}
                  onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && !pendingInterrupt) { e.preventDefault(); send(input); } }}
                  placeholder={pendingInterrupt ? "Waiting for your approval above…" : "Reply to IRIS…"}
                  rows={1}
                  disabled={!!pendingInterrupt}
                  style={{
                    width: "100%", background: "transparent", border: "none", outline: "none",
                    color: pendingInterrupt ? "var(--text-dim)" : C.text,
                    fontSize: 15, lineHeight: 1.6, resize: "none",
                    fontFamily: "inherit", maxHeight: 160, overflowY: "auto", marginBottom: 10,
                    cursor: pendingInterrupt ? "not-allowed" : "text",
                    opacity: pendingInterrupt ? 0.45 : 1,
                    transition: "opacity 0.2s ease, color 0.2s ease",
                  }} />
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <button onClick={() => fileInputRef.current?.click()} title="Attach files" aria-label="Attach files"
                    className="touch-target"
                    style={{
                      width: 28, height: 28, borderRadius: "50%",
                      background: "var(--surface-2)", border: "none", cursor: "pointer",
                      color: C.muted, fontSize: 18, display: "flex", alignItems: "center", justifyContent: "center",
                      transition: "background .15s"
                    }}
                    onMouseEnter={e => e.currentTarget.style.background = "var(--surface-3)"}
                    onMouseLeave={e => e.currentTarget.style.background = "var(--surface-2)"}>
                    +
                  </button>
                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    {loading ? (
                      <button onClick={stopGeneration} title="Stop generating" aria-label="Stop generating"
                        className="touch-target"
                        style={{
                          width: 32, height: 32, borderRadius: 8, border: "none",
                          background: C.accent, cursor: "pointer",
                          display: "flex", alignItems: "center", justifyContent: "center",
                          transition: "background .2s", boxShadow: `0 0 10px var(--accentShadow)`
                        }}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--bg)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                          <rect x="3" y="3" width="18" height="18" rx="2" fill="var(--bg)" stroke="none" />
                        </svg>
                      </button>
                    ) : (
                      <button onClick={() => send(input)} aria-label="Send message"
                        className="touch-target"
                        disabled={(!input.trim() && attachedFiles.length === 0) || loading || !!pendingInterrupt}
                        style={{
                          width: 32, height: 32, borderRadius: 8, border: "none",
                          background: (input.trim() || attachedFiles.length > 0) && !loading && !pendingInterrupt ? C.accent : "rgba(128,128,128,0.2)",
                          cursor: (input.trim() || attachedFiles.length > 0) && !loading && !pendingInterrupt ? "pointer" : "not-allowed",
                          display: "flex", alignItems: "center", justifyContent: "center",
                          transition: "background .2s",
                          boxShadow: (input.trim() || attachedFiles.length > 0) && !loading && !pendingInterrupt ? `0 0 10px var(--accentShadow)` : "none"
                        }}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={((input.trim() || attachedFiles.length > 0) && !loading && !pendingInterrupt) ? "var(--bg)" : "var(--muted)"} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                          <line x1="22" y1="2" x2="11" y2="13" />
                          <polygon points="22 2 15 22 11 13 2 9 22 2" fill={((input.trim() || attachedFiles.length > 0) && !loading && !pendingInterrupt) ? "var(--bg)" : "var(--muted)"} stroke="none" />
                        </svg>
                      </button>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

      </div>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&display=swap');
        /* ── GEMINI DARK THEME (Matching LoginScreen) ──
           #0a0a0e dark canvas with radial navy glow, rgb(28 30 37) inputs, and #8f6bff accent */
        .theme-dark {
          --bg: #0a0a0e;
          --sidebar: #0a0a0e;
          --sidebarBorder: #1c263d;
          --inputBg: rgb(28, 30, 37);
          --inputBorder: rgba(255, 255, 255, 0.14);
          --input-bg: rgb(28, 30, 37);
          --input-border: rgba(255, 255, 255, 0.14);
          --chipBg: #121929;
          --chipBorder: #1c263d;
          --text: #e8eaed;
          --text-muted: #9aa0a6;
          --muted: #9aa0a6;
          --accent: #8f6bff;
          --accentShadow: rgba(143,107,255,0.35);
          --topBar: #0a0a0e;
          --topBarBorder: #1c263d;
          --userBubble: #16233d;
          --assistantBg: transparent;
          --dot: #8f6bff;
        }

        /* ── NEURAL EXPRESSIVE LIGHT THEME ──
           Dimmed, comfortable low-glare daylight palette: soft matte slate-gray canvas (#e3e6eb)
           instead of blazing white, crisp charcoal text (#1e2229) for effortless reading.
           Keep these in lockstep with iris.css's :root / [data-theme="light"] scopes —
           this block is shell-scoped, so it WINS for everything inside .app-shell. */
        .theme-light {
          --bg: #e3e6eb;
          --sidebar: #e3e6eb;
          --sidebarBorder: #c2c8d2;
          --inputBg: #d8dce3;
          --inputBorder: #c2c8d2;
          --chipBg: #edf0f4;
          --chipBorder: #c2c8d2;
          --text: #1e2229;
          --muted: #4b515d;
          --accent: #4d7fff;
          --accentShadow: rgba(77,127,255,0.18);
          --topBar: #e3e6eb;
          --topBarBorder: #c2c8d2;
          --userBubble: #d8dce3;
          --assistantBg: transparent;
          --dot: #4d7fff;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: var(--bg); }
        /* Gemini dark ambiance: radial navy glow over near-black matching LoginScreen */
        .theme-dark::before {
          content: "";
          position: fixed; inset: 0; z-index: 0; pointer-events: none;
          background:
            radial-gradient(ellipse 70% 60% at 50% 45%, #16233d 0%, #0d1520 35%, #0a0a0e 70%);
          opacity: 0.95;
        }
        /* Neural Expressive daytime ambiance: soft low-glare energy wash */
        .theme-light::before {
          content: "";
          position: fixed; inset: 0; z-index: 0; pointer-events: none;
          background:
            radial-gradient(1100px 620px at 82% -12%, rgba(77,127,255,0.05), transparent 60%),
            radial-gradient(900px 560px at 8% 112%, rgba(143,107,255,0.04), transparent 62%),
            radial-gradient(500px 350px at 85% 85%, rgba(255,111,176,0.03), transparent 60%);
        }

        .sidebar {
          background: var(--bg);
          border: none;
          display: flex; flex-direction: column; flex-shrink: 0;
          transition: width 0.25s ease, min-width 0.25s ease;
        }
        .modal-overlay {
          position: fixed; inset: 0; z-index: 1100;
          background: rgba(0,0,0,0.55); backdrop-filter: blur(8px);
          display: flex;
        }
        /* ── Layout primitives ──
           dvh with a vh fallback: mobile Safari/Chrome report 100vh as the
           LARGEST viewport, so the composer used to sit underneath the browser
           toolbar until you scrolled. Declared here rather than inline because a
           React style object can't hold two values for one property. */
        .app-shell { height: 100vh; height: 100dvh; }
        .icon-strip { height: 100vh; height: 100dvh; }

        /* ── Universal Adaptive Topbar ── */
        .app-topbar {
          position: sticky;
          top: 0;
          padding-top: env(safe-area-inset-top, 0px);
          min-height: 54px;
        }
        @media (min-width: 769px) {
          /* The only case that hides the sidebar toggle: a desktop viewport where
             the sidebar is already open, so its own close control is on screen.
             Below 769px the drawer is closed by default and this button is the
             only way to reach the thread list — never hide it there. */
          .topbar-menu-btn.is-sidebar-open { display: none !important; }
        }
        /* The "New chat" label was hidden below 520px, leaving an unlabelled pen
           glyph as the only escape from a long conversation. Keep the words at
           every width — the conversation-title pill is what yields the room. */
        .topbar-new-chat-label { display: inline; }
        @media (max-width: 520px) {
          .topbar-new-chat-btn { padding: 8px 10px !important; }
          .desktop-title-pill { display: none !important; }
        }
        @media (max-width: 400px) {
          /* 360px-wide phones: shrink the wordmark and the button's internal gap
             rather than dropping either control. */
          .topbar-brand { font-size: 15px !important; }
          .topbar-new-chat-btn { gap: 5px !important; padding: 8px 9px !important; font-size: 12px !important; }
        }
        /* Keyboard and switch-control users had only hover feedback to go on. */
        .app-topbar button:focus-visible {
          outline: 2px solid var(--accent, #8f6bff);
          outline-offset: 2px;
          border-radius: 9px;
        }

        /* Message column + composer share one fluid gutter so they stay
           optically aligned at every width instead of only at ≥768px. */
        .chat-column {
          width: 100%;
          max-width: 720px;
          margin: 0 auto;
          padding: 24px clamp(12px, 3.5vw, 24px) 170px;
        }
        .composer-dock {
          padding: 10px clamp(12px, 3.5vw, 24px) calc(16px + env(safe-area-inset-bottom, 0px));
        }
        .composer-col { width: 100%; max-width: 720px; margin: 0 auto; }
        .empty-state {
          flex: 1;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          gap: clamp(18px, 4vh, 32px);
          padding: clamp(20px, 5vh, 48px) clamp(14px, 4vw, 20px) clamp(80px, 15vh, 140px);
        }

        /* ── Message row ──
           .msg-col caps the bubble so a wide answer doesn't run the full column
           on desktop; .msg-body is where untrusted text lands, so both the wrap
           rules and the overflow guard live here rather than on any one child.
           A signed URL, thread id or base64 blob has no break opportunity in
           it, and without break-word one of those sets the column's min-content
           width and scrolls the whole chat sideways on a phone. */
        .msg-col { max-width: 76%; }
        .msg-body {
          min-width: 0;
          overflow-wrap: break-word;
          word-break: break-word;
        }
        /* Display math is laid out at its natural width and ignores the column,
           so it is the one thing break-word cannot rescue — let it scroll
           inside itself. overflow-y stays hidden so tall fractions don't gain a
           pointless second scrollbar. */
        .msg-body .katex-display { max-width: 100%; overflow-x: auto; overflow-y: hidden; }

        /* ── Shell breakpoint: ≤768 drawer, ≥769 rail ──
           iris.css flips .desktop-only / .mobile-only at the SAME 769px now.
           They used to disagree (640 there, 768 here), so between 640 and 768px
           the sidebar toggle AND the hamburger were both hidden — close the
           sidebar in that band and nothing on screen could reopen it. */
        @media (min-width: 769px) {
          .sidebar.open { width: 240px; min-width: 240px; }
          .sidebar.closed { width: 0px; min-width: 0px; border-right: none; overflow: hidden; }
          .mobile-only { display: none !important; }
          .mobile-backdrop { display: none !important; }
          .modal-overlay.settings-modal {
            align-items: flex-end; justify-content: flex-start;
            padding: 0 0 24px calc(var(--modal-left) + 24px);
          }
          .modal-overlay.search-modal {
            align-items: flex-start; justify-content: flex-start;
            padding: 24px 0 0 calc(var(--modal-left) + 24px);
          }
        }
        @media (max-width: 768px) {
          /* min(280px, 84vw) keeps the backdrop tappable on a 320px phone. */
          .sidebar { position: fixed; top: 0; left: 0; bottom: 0; z-index: 1000; width: min(280px, 84vw); min-width: min(280px, 84vw); transform: translateX(-100%); transition: transform 0.3s ease; }
          .sidebar.open { transform: translateX(0); }
          .sidebar.closed { transform: translateX(-100%); }
          .desktop-only { display: none !important; }
          .chat-column { padding-top: 16px; padding-bottom: 144px; padding-left: 12px; padding-right: 12px; }
          /* The 76% cap costs more than the avatar saves at phone widths: with a
             46px avatar and a 14px gap, 76% of a 360px screen leaves the bubble
             ~250px. Give the column the full remaining width and shrink the
             avatar instead — !important because both are set inline. */
          .msg-col { max-width: 100% !important; }
          .msg-avatar { width: 34px !important; height: 34px !important; }
          /* IrisLogo takes its size as a prop, so the wrapper and <Image> are
             still sized 42px inline — without this they'd be cropped by the
             avatar's overflow:hidden instead of scaling with it. */
          .msg-avatar > div, .msg-avatar img { width: 100% !important; height: 100% !important; }
          .composer-dock { padding: 8px 12px calc(12px + env(safe-area-inset-bottom, 0px)) !important; }
          /* The dock owns its own padding above; only the empty-state composer
             (same class, no dock) takes this one. */
          .chat-input-wrapper:not(.composer-dock) { padding: 8px 12px 12px !important; }
          .chat-input-container { padding: 10px 12px 8px !important; }
          .modal-container { padding: 18px !important; margin: 12px !important; max-width: calc(100vw - 24px) !important; }
          .modal-overlay.settings-modal {
            align-items: center; justify-content: center; padding: 16px;
          }
          .modal-overlay.search-modal {
            align-items: flex-start; justify-content: center; padding: 8vh 16px 0;
          }
        }
        /* Phone tier — reclaim the vertical space the composer reserves. */
        @media (max-width: 480px) {
          .chat-column { padding-top: 12px; padding-bottom: 136px; padding-left: 8px; padding-right: 8px; }
          .empty-state { gap: 14px; padding: 16px 10px 88px; }
        }
        /* Landscape phones / short windows: the hero can't afford 18vh of
           bottom padding or the composer falls off the screen. */
        @media (max-height: 560px) {
          .empty-state { gap: 14px; padding-top: 14px; padding-bottom: 80px; }
        }
        /* ── Touch targets ──
           The 28–32px icon buttons are fine for a mouse and too small for a
           thumb. Grow them only where the pointer is actually coarse, so the
           desktop density is untouched. */
        @media (hover: none) and (pointer: coarse) {
          .touch-target { min-width: 40px; min-height: 40px; }
          .touch-target-lg { min-width: 44px; min-height: 44px; }
          /* The message toolbar is hover-revealed. A touch device never fires
             mouseenter, so Copy / Edit / Regenerate were unreachable on every
             phone and tablet — permanently opacity:0 and pointer-events:none.
             Show it unconditionally where there is no hover to reveal it with,
             and grow the 13px icons into real thumb targets. !important because
             all three properties are set inline. */
          .msg-actions { opacity: 1 !important; pointer-events: auto !important; gap: 6px !important; }
          .msg-actions > button {
            min-width: 36px; min-height: 36px;
            justify-content: center;
          }
        }
        .mobile-backdrop {
          position: fixed; inset: 0;
          background: rgba(0,0,0,0.5);
          z-index: 999;
          opacity: 0; pointer-events: none;
          transition: opacity 0.3s ease;
        }
        .mobile-backdrop.open {
          opacity: 1; pointer-events: auto;
        }
        .shimmer-line {
          background: linear-gradient(90deg, rgba(130,130,130,0.12) 25%, rgba(130,130,130,0.35) 50%, rgba(130,130,130,0.12) 75%);
          background-size: 200% 100%;
          animation: shimmer 1.5s infinite linear;
        }
        @keyframes shimmer {
          0% { background-position: 200% 0; }
          100% { background-position: -200% 0; }
        }
        ::-webkit-scrollbar { width: 3px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: var(--scroll-thumb, #333); border-radius: 2px; }
        textarea::placeholder { color: var(--text-dim, #555); }
        @keyframes fadeUp { from { opacity:0; transform:translateY(10px); } to { opacity:1; transform:translateY(0); } }
        @keyframes slideIn { from { opacity:0; transform:translateX(-8px); } to { opacity:1; transform:translateX(0); } }
        @keyframes spin { from { transform:rotate(0deg); } to { transform:rotate(360deg); } }
        @keyframes dotBounce { 0%,60%,100% { transform:translateY(0); opacity:.4; } 30% { transform:translateY(-5px); opacity:1; } }
        @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.4; } }
      `}</style>
    </div>
  );
}
