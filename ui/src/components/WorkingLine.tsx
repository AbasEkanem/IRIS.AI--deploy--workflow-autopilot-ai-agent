"use client";

/* ══════════════════════════════════════════════════════════════════
   WorkingLine — the ONE line the chat shows while IRIS works
   ──────────────────────────────────────────────────────────────────
   Everything IRIS does during a run — todos, delegations, tool calls, the
   harness nudges that steer it off empty responses and loops — renders in
   the workspace panel above. The chat itself stays clean: this single line,
   then the summary.

   It replaces SkeletonShimmer for assistant turns. The shimmer implied text
   was about to appear inline, which is no longer true: orchestrator prose is
   routed to the workspace (`token.channel === "workspace"`), so the chat has
   nothing to stream until the run ends.
   ══════════════════════════════════════════════════════════════════ */

export default function WorkingLine({ label = "IRIS is working" }: { label?: string }) {
  return (
    <div
      style={{
        display: "flex", alignItems: "center", gap: 9,
        padding: "6px 0", fontSize: 14,
        color: "var(--text-muted, #A09890)",
        fontFamily: "inherit",
      }}
      role="status"
      aria-live="polite"
    >
      <style>{`
        @keyframes wlPulse { 0%,100% { opacity:.35; } 50% { opacity:1; } }
        @keyframes wlDot   { 0%,60%,100% { transform:translateY(0); opacity:.4; } 30% { transform:translateY(-3px); opacity:1; } }
        @media (prefers-reduced-motion: reduce) {
          [data-iris-working] * { animation: none !important; }
        }
      `}</style>
      <span data-iris-working style={{ display: "inline-flex", alignItems: "center", gap: 9 }}>
        <span
          style={{
            width: 7, height: 7, borderRadius: "50%",
            background: "var(--accent)", flexShrink: 0,
            animation: "wlPulse 1.4s ease-in-out infinite",
          }}
        />
        <span style={{ fontStyle: "italic" }}>{label}</span>
        <span style={{ display: "inline-flex", gap: 3 }}>
          {[0, 1, 2].map((i) => (
            <span
              key={i}
              style={{
                width: 3, height: 3, borderRadius: "50%",
                background: "var(--text-muted, #A09890)",
                animation: `wlDot 1s ease-in-out ${i * 0.15}s infinite`,
              }}
            />
          ))}
        </span>
      </span>
    </div>
  );
}
