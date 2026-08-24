"use client";
import { useState } from "react";
import type { SummaryEvent } from "@/types/chat";

/* ══════════════════════════════════════════════════════════════════
   AgentSummaryCard — the chat's end-of-run answer
   ──────────────────────────────────────────────────────────────────
   IRIS is already prompted to close every turn with the Final Response
   Contract (execution-protocol.md:40-51): STATUS, SUMMARY, ARTIFACTS,
   BLOCKERS, LEARNING, fenced by ━×50. The backend parses it server-side
   (`_parse_final_contract`) and ships it as a `summary` event, so the chat
   can show the outcome as structure instead of a wall of fenced text.

   The SUMMARY body is passed in as `children` so it renders through the
   page's own markdown pipeline — links, tables, and KaTeX keep working
   exactly as they do for an ordinary answer.

   If the contract did not parse the backend leaves `status` empty and puts
   the whole answer in `raw`; the caller then renders plain prose and this
   card never mounts. There is no path that yields an empty bubble.
   ══════════════════════════════════════════════════════════════════ */

const MUTED = "var(--text-muted, #A09890)";
const TEXT = "var(--text, #EDE8E0)";
const BORDER = "var(--border, #1E2740)";
const AMBER = "#d99a4e";
const GREEN = "#4caf50";
const RED = "#e06c6c";

/** STATUS is free text from the model, so match on substring, not equality. */
function statusTone(status: string): { color: string; label: string } {
  const s = (status || "").toUpperCase();
  const label = status.trim().replace(/\s+/g, " ");
  if (/FAIL|ERROR|ABORT/.test(s)) return { color: RED, label };
  if (/BLOCK|PARTIAL|INCOMPLETE|PENDING|AWAIT/.test(s)) return { color: AMBER, label };
  if (/COMPLETE|SUCCESS|DONE|DELIVERED|SENT/.test(s)) return { color: GREEN, label };
  return { color: "var(--accent, #8AB4F8)", label };
}

const URL_RE = /^https?:\/\/\S+$/i;

/** One artifact line. A bare URL becomes a link; anything else stays inert text. */
function ArtifactRow({ text }: { text: string }) {
  const trimmed = text.trim();
  // An artifact can be "Doc: https://…" — link the trailing URL if there is one.
  const m = trimmed.match(/(https?:\/\/\S+)$/i);
  const head = m ? trimmed.slice(0, m.index).trim() : trimmed;
  const url = m ? m[1] : URL_RE.test(trimmed) ? trimmed : "";

  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 8, fontSize: 13, lineHeight: 1.55, padding: "2px 0" }}>
      <svg
        width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={MUTED}
        strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
        style={{ flexShrink: 0, marginTop: 3 }}
      >
        <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
        <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
      </svg>
      <span style={{ minWidth: 0, wordBreak: "break-word", color: TEXT }}>
        {head && <span>{head}{url ? " " : ""}</span>}
        {url && (
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              color: "var(--accent)", textDecoration: "none",
              borderBottom: "1px solid rgba(var(--accent-rgb),0.3)",
            }}
          >
            {url}
          </a>
        )}
      </span>
    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════
   The card is exported in three pieces so the SUMMARY prose can render
   through the page's OWN markdown pipeline instead of a second, thinner
   copy of it. page.tsx puts <SummaryStatus> above its existing
   <ReactMarkdown> block and <SummaryDetails> below — same visual result,
   no duplicated renderer config, and links/tables/KaTeX keep working.
   The default export composes all three for anywhere that has a renderer
   to hand.
   ══════════════════════════════════════════════════════════════════ */

/** The STATUS badge row. Sits directly above the summary prose. */
export function SummaryStatus({ status }: { status: string }) {
  const tone = statusTone(status);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10, animation: "fadeUp .3s ease" }}>
      <span
        style={{
          display: "inline-flex", alignItems: "center", gap: 6,
          fontSize: 11, fontWeight: 700, letterSpacing: "0.06em", textTransform: "uppercase",
          color: tone.color,
          background: `${tone.color}14`,
          border: `1px solid ${tone.color}55`,
          borderRadius: 20, padding: "3px 10px",
        }}
      >
        <span style={{ width: 6, height: 6, borderRadius: "50%", background: tone.color, flexShrink: 0 }} />
        {tone.label || "status"}
      </span>
      <span style={{ flex: 1, height: 1, background: BORDER, opacity: 0.6 }} />
    </div>
  );
}

/** ARTIFACTS, BLOCKERS, LEARNING, and the verbatim-answer escape hatch. */
export function SummaryDetails({ summary }: { summary: SummaryEvent }) {
  const [showRaw, setShowRaw] = useState(false);
  const artifacts = (summary.artifacts || []).filter((a) => a && a.trim());
  const blockers = (summary.blockers || "").trim();
  const learning = (summary.learning || "").trim();

  return (
    <>
      {/* ── ARTIFACTS ── */}
      {artifacts.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div
            style={{
              fontSize: 10.5, fontWeight: 700, letterSpacing: "0.07em",
              textTransform: "uppercase", color: MUTED, marginBottom: 5,
            }}
          >
            Artifacts
          </div>
          {artifacts.map((a, i) => (
            <ArtifactRow key={i} text={a} />
          ))}
        </div>
      )}

      {/* ── BLOCKERS ── */}
      {blockers && (
        <div
          style={{
            marginTop: 12, padding: "9px 12px",
            background: `${AMBER}12`,
            border: `1px solid ${AMBER}38`,
            borderRadius: 10,
            fontSize: 13, lineHeight: 1.6, color: TEXT,
            whiteSpace: "pre-wrap",
          }}
        >
          <span style={{ fontWeight: 700, color: AMBER }}>Blockers — </span>
          {blockers}
        </div>
      )}

      {/* ── LEARNING ── */}
      {learning && (
        <div style={{ marginTop: 10, fontSize: 12.5, lineHeight: 1.6, color: MUTED, whiteSpace: "pre-wrap" }}>
          <span style={{ fontWeight: 600 }}>Learned: </span>
          {learning}
        </div>
      )}

      {/* ── Escape hatch: the unparsed contract, verbatim ── */}
      {summary.raw && summary.raw.trim() && (
        <div style={{ marginTop: 10 }}>
          <button
            onClick={() => setShowRaw((v) => !v)}
            aria-expanded={showRaw}
            style={{
              background: "none", border: "none", padding: 0, cursor: "pointer",
              fontSize: 11.5, color: MUTED, fontFamily: "inherit",
            }}
          >
            {showRaw ? "Hide full response" : "Show full response"}
          </button>
          {showRaw && (
            // Inert pre-wrap text on purpose — the raw answer can quote third-party
            // content, and this view exists to inspect it, not to execute its markup.
            <div
              style={{
                marginTop: 8, padding: "9px 11px",
                background: "var(--surface-2, rgba(255,255,255,0.05))",
                border: `1px solid ${BORDER}`, borderRadius: 8,
                fontSize: 12, lineHeight: 1.6, color: MUTED,
                whiteSpace: "pre-wrap", maxHeight: 320, overflowY: "auto",
              }}
            >
              {summary.raw}
            </div>
          )}
        </div>
      )}
    </>
  );
}

export default function AgentSummaryCard({
  summary,
  children,
}: {
  summary: SummaryEvent;
  /** The SUMMARY field, pre-rendered by the caller's markdown pipeline. */
  children?: React.ReactNode;
}) {
  return (
    <div style={{ animation: "fadeUp .3s ease" }}>
      <SummaryStatus status={summary.status} />
      {children}
      <SummaryDetails summary={summary} />
    </div>
  );
}
