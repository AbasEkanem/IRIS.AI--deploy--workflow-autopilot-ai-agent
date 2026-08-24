"use client";
import { useState } from "react";
import { correctionCopy, type CorrectionKind } from "@/lib/corrections";

/**
 * Renders a persisted harness nudge as a distinct, understated system
 * self-correction — never a user bubble. Collapsed by default (a one-line
 * "IRIS caught this and kept going"); click to reveal the full steering text
 * that the orchestrator actually received. Styled to sit quietly in the
 * transcript, theme-aware via the same CSS vars the rest of the chat uses.
 *
 * The copy lives in @/lib/corrections alongside the source-name table, so
 * recognising a new guardrail and describing it are one edit, not two. This
 * component used to carry its own exhaustive Record of the (then two) kinds.
 */

const AMBER = "#d99a4e";

export default function SystemCorrectionCard({
  kind,
  text,
}: {
  kind: CorrectionKind;
  text?: string;
}) {
  const [open, setOpen] = useState(false);
  const copy = correctionCopy(kind);
  const hasText = Boolean(text && text.trim());

  return (
    <div
      style={{
        background: "linear-gradient(135deg,rgba(217,154,78,.08),rgba(217,154,78,.02))",
        border: "1px solid rgba(217,154,78,.22)",
        borderRadius: 12,
        padding: "12px 14px",
        maxWidth: 520,
        margin: "4px auto 20px",
        animation: "fadeUp .3s ease both",
      }}
    >
      <button
        onClick={() => hasText && setOpen((o) => !o)}
        aria-expanded={open}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          width: "100%",
          background: "transparent",
          border: "none",
          cursor: hasText ? "pointer" : "default",
          padding: 0,
          textAlign: "left",
          fontFamily: "inherit",
        }}
      >
        {/* shield-with-check — the self-correction/guardrail mark */}
        <span
          style={{
            width: 26,
            height: 26,
            borderRadius: "50%",
            flexShrink: 0,
            background: "rgba(217,154,78,.12)",
            border: "1px solid rgba(217,154,78,.28)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={AMBER} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            <polyline points="9 12 11 14 15 10" />
          </svg>
        </span>
        <span style={{ flex: 1, minWidth: 0 }}>
          <span style={{ display: "block", fontSize: 13, fontWeight: 600, color: "var(--text,#EDE8E0)", lineHeight: 1.4 }}>
            {copy.title}
          </span>
          {!open && (
            <span style={{ display: "block", fontSize: 12, color: "var(--text-muted,#A09890)", lineHeight: 1.5, marginTop: 2 }}>
              {copy.sub}
            </span>
          )}
        </span>
        {hasText && (
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="var(--text-muted,#A09890)"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            style={{ flexShrink: 0, transform: open ? "rotate(180deg)" : "none", transition: "transform .2s ease" }}
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        )}
      </button>

      {open && hasText && (
        <div
          style={{
            marginTop: 10,
            paddingTop: 10,
            borderTop: "1px solid rgba(217,154,78,.16)",
            fontSize: 12.5,
            color: "var(--text-muted,#A09890)",
            lineHeight: 1.6,
            whiteSpace: "pre-wrap",
          }}
        >
          {text}
        </div>
      )}
    </div>
  );
}
