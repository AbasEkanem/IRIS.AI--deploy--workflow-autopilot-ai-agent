"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { correctionCopy, correctionKind } from "@/lib/corrections";
import type {
  StatusStep,
  SubagentEvent,
  CorrectionEvent,
  TerminalEvent,
} from "@/types/chat";

/* ══════════════════════════════════════════════════════════════════
   AgentWorkspace — IRIS's execution environment, in the agent card
   ──────────────────────────────────────────────────────────────────
   Replaces AgentSearchCard. Same visual family, three differences that
   are the whole point:

   1. It does NOT auto-dismiss. AgentSearchCard unmounts 1.8 s after the
      run finishes (:270-285), which is exactly why it couldn't be reused:
      the record of a ten-minute orchestration evaporated before anyone
      could read it. Here the body stays MOUNTED behind
      `maxHeight: collapsed ? 0 : N`, so reopening the chevron shows the
      full transcript with zero refetch.
   2. It shows the harness steering IRIS — the blank-response and loop
      guards — as first-class rows, so a long run is legible instead of
      being a spinner.
   3. Nothing here reaches the chat. The chat holds one live line while
      this fills, then the parsed summary. This panel is the workspace.

   Live-only by design: a reload does not rebuild it (page.tsx zeroes
   statusSteps and /history drops every ToolMessage), so there is nothing
   on the wire to restore. Stated, not hidden.
   ══════════════════════════════════════════════════════════════════ */

/* ── Chromeless by instruction ──────────────────────────────────────────
   No border, no card field, no blur, no title bar. The panel is a region of
   the message it belongs to, not an object floating on top of it — it reads
   as part of the reply, the way a fenced code block does.

   That has one hard consequence: with the dark glass field gone, colour can
   no longer be hardcoded. The old palette was deliberately theme-INDEPENDENT
   (near-white text, console green/cyan) because it always composited over its
   own dark field; painted straight onto the chat surface those literals are
   invisible in light mode. So every colour here is an iris.css token, each of
   which is defined in all four theme scopes (`:root`, the
   prefers-color-scheme dark block, and both explicit `[data-theme]` blocks).

   `--text-muted`, never `--muted`: the latter exists only inside page.tsx's
   shell-scoped <style> and carries a stale fallback from a retired palette.

   The console character survives in the things that are actually console — the
   code face and the `PS>` prompt on each command line. Every text node in the
   panel is set in that face now, headers and section labels included. That is a
   deliberate reversal of the earlier rule ("headers use the app face so the
   panel sits in the page"): a workspace IS a code surface, and typesetting half
   of it in the page's prose font read as an accident rather than as a choice.
   See `MONO` and `CODE` below. */
const T = {
  text: "var(--text)",
  muted: "var(--text-muted)",
  dim: "var(--text-dim)",
  accent: "var(--accent)",
  green: "var(--green)",
  amber: "var(--amber)",
  red: "var(--red)",
  /** Hairline for section rules and the recover divider. */
  hair: "var(--border)",
  /** The guardrail field — the band behind a self-correction row and the quote
   *  inside it. Tinted from the accent so it reads as commentary on the run in
   *  both themes without introducing a second surface colour. */
  quote: "rgba(var(--accent-rgb), 0.07)",
};

/* ── The code theme ─────────────────────────────────────────────────────
   JetBrains Mono, loaded and self-hosted by next/font in layout.tsx.

   It has to come through the CSS variable rather than by family name: next/font
   rewrites the family to a hashed `__JetBrains_Mono_*`, so a literal
   `'JetBrains Mono'` in a stack matches nothing and falls straight through to
   Consolas — which is exactly what `iris.css:1185` and ApprovalCard's identifier
   fields have silently been doing all along. `var(--font-mono)` is the only
   handle that resolves, and it resolves here because layout.tsx puts the
   variable on <body>, which this panel is a descendant of.

   The tail is the code-block stack GPT itself falls back to, so a font that
   fails to load degrades to the same faces rather than to the page's prose
   font. */
const MONO =
  "var(--font-mono), ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Monaco, " +
  "Consolas, 'Liberation Mono', monospace";

/**
 * The face plus the two font features, applied to every text node in the panel.
 *
 * **Ligatures off**, deliberately. JetBrains Mono ships coding ligatures, so
 * `->`, `=>`, `!=` and `::` fuse into single glyphs — and this panel prints tool
 * names, file paths and email addresses that a user has to be able to read back
 * and retype character for character. A fused glyph makes that impossible, and
 * GPT's own code face carries no ligatures either, so switching them off is part
 * of matching the theme rather than a compromise against it.
 *
 * `tnum` fixes digit width so the elapsed-time readout and the row counters stop
 * reflowing as they tick.
 */
const CODE: React.CSSProperties = {
  fontFamily: MONO,
  fontVariantLigatures: "none",
  fontFeatureSettings: '"liga" 0, "calt" 0, "tnum" 1',
};

/** Height the execution rail scrolls inside. A run of forty steps must not push
 *  the answer off the screen, so the rail scrolls internally and pins itself to
 *  the newest line — `maxHeight`, not `height`, so a two-step run doesn't
 *  reserve 340px of blank space. */
const RAIL_MAX = 340;

/** Beat between one line finishing and the next appearing. Without it a burst of
 *  rows reads as one continuous smear of text rather than as separate commands. */
const REVEAL_GAP = 200;

/* ── Icons ─────────────────────────────────────────────────────── */

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="13" height="13" viewBox="0 0 24 24" fill="none"
      stroke={T.muted} strokeWidth="2.2" strokeLinecap="round"
      style={{
        transition: "transform .25s ease",
        transform: open ? "rotate(180deg)" : "rotate(-90deg)",
        flexShrink: 0,
      }}
    >
      <polyline points="6 9 12 15 18 9" />
    </svg>
  );
}

function CheckIcon({ color = T.green, size = 12 }: { color?: string; size?: number }) {
  return (
    <svg
      width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke={color} strokeWidth="2.5" strokeLinecap="round" style={{ flexShrink: 0 }}
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

function CopyIcon({ size = 12.5 }: { size?: number }) {
  return (
    <svg
      width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0 }}
    >
      <rect x="9" y="9" width="12" height="12" rx="2" />
      <path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1" />
    </svg>
  );
}

function Spinner({ size = 15 }: { size?: number }) {
  return (
    <svg
      width={size} height={size} viewBox="0 0 100 100"
      style={{ animation: "wsSpin 1.8s linear infinite", flexShrink: 0 }}
    >
      {Array.from({ length: 8 }).map((_, i) => {
        const rad = (i * 45 * Math.PI) / 180;
        return (
          <line
            key={i}
            x1="50" y1="50"
            x2={50 + 40 * Math.sin(rad)} y2={50 - 40 * Math.cos(rad)}
            stroke={T.accent} strokeWidth="10" strokeLinecap="round"
            opacity={0.25 + (i / 8) * 0.75}
          />
        );
      })}
    </svg>
  );
}

/** Row glyphs, keyed by the classifier below. `subagent` is the chip/cpu mark. */
const ROW_ICONS: Record<string, string> = {
  think: "M12 8v4l3 3M12 2a10 10 0 1 0 0 20A10 10 0 0 0 12 2z",
  read: "M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2zM22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z",
  write: "M12 20h9M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z",
  search: "M11 3a8 8 0 1 0 0 16 8 8 0 0 0 0-16zM21 21l-4.35-4.35",
  memory: "M12 3a4 4 0 0 0-4 4 3 3 0 0 0-1 5.83V17a3 3 0 0 0 6 0V3zM12 3a4 4 0 0 1 4 4 3 3 0 0 1 1 5.83V17a3 3 0 0 1-6 0",
  subagent: "M9 3v2M15 3v2M9 19v2M15 19v2M3 9h2M3 15h2M19 9h2M19 15h2M6 6h12v12H6zM9 9h6v6H9z",
  tool:
    "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z",
};

function RowIcon({ kind, active }: { kind: string; active: boolean }) {
  const d = ROW_ICONS[kind] ?? ROW_ICONS.think;
  return (
    <svg
      width="13" height="13" viewBox="0 0 24 24" fill="none"
      stroke={active ? T.accent : T.muted} strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}
    >
      <path d={d} />
    </svg>
  );
}

/* `Dots` lived here — the three bouncing dots on the active row. Removed with
   its only call site: the console block cursor now holds that position, and its
   `wsDot` keyframes went with it. */

/* ── Typewriter ─────────────────────────────────────────────────────────
   Steps and guardrails type themselves in as they arrive, so a long
   orchestration reads like IRIS working rather than a list appearing fully
   formed. Two things this must not do: retype a row that is already on
   screen, and animate at all for someone who asked the OS not to. */

/** Live `prefers-reduced-motion`. False during SSR and the first paint, then
 *  corrected in an effect — so the animation can only ever be *removed*, never
 *  introduced, by hydration. */
function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return reduced;
}

/**
 * Per-kind reveal rate, ms per character.
 *
 * Not one global speed: a thought should land slower than a tool invocation,
 * the way a shell prints a comment slower than it echoes a command. 55ms/char
 * is ~18 cps, 30ms/char is ~33 cps. The word and clause rests in `charCost`
 * push the effective rate about 15% below these numbers.
 *
 * Rows type SEQUENTIALLY — one row at a time, in stream order, line 1 finishes
 * before line 2 appears. That makes these rates a real budget rather than a
 * per-row cost, which is what `rush` below absorbs on a busy run.
 */
const SPEED: Record<string, number> = {
  think: 55,
  subagent: 40,
  memory: 34,
  read: 30,
  write: 30,
  search: 30,
  tool: 30,
  guardrail: 40,
};

/**
 * Cost in ms of revealing the next character, given the one just revealed.
 *
 * A flat rate reads as a ticker, not as typing. Real typing lands in bursts and
 * rests at boundaries, so this rests after a clause, rests a little at word
 * ends, and jitters everywhere else — which also stops two rows typing the same
 * word from marching in visible lockstep.
 */
function charCost(prev: string, msPerChar: number): number {
  if (/[.,;:!?]/.test(prev)) return msPerChar * 3.4;
  if (prev === " " || prev === "·" || prev === "—") return msPerChar * 1.7;
  return msPerChar * (0.75 + Math.random() * 0.6);
}

/**
 * Reveal `text` one character at a time, then report completion exactly once.
 *
 * Driven by requestAnimationFrame rather than setInterval: a backgrounded tab
 * suspends rAF, so a run left in another tab does not accumulate a queue of
 * timer callbacks, and the reveal is computed from real elapsed time so it
 * cannot drift.
 *
 * A label that GREW (a status row gains its tool name a moment after its
 * phase) keeps its cursor and carries on. A label that was REPLACED restarts,
 * so the reader never sees a spliced hybrid of two different strings.
 *
 * `onDone` is held in a ref, not taken as a dependency: the parent's callback
 * is stable, but keying the rAF effect on a function would make any future
 * inline callback restart the animation mid-line.
 */
function useTypewriter(
  text: string,
  animate: boolean,
  msPerChar: number,
  onDone?: () => void,
): { shown: string; typing: boolean } {
  const countRef = useRef(animate ? 0 : text.length);
  const prevText = useRef(text);
  const wasAnimating = useRef(animate);
  const doneRef = useRef(false);
  const doneCb = useRef(onDone);
  useEffect(() => { doneCb.current = onDone; }, [onDone]);
  const [, bump] = useState(0);

  useEffect(() => {
    const grew = text.startsWith(prevText.current);
    prevText.current = text;
    const became = animate && !wasAnimating.current;
    wasAnimating.current = animate;

    /* Not this row's turn to animate — either it is already part of the record,
       or the whole panel is in snap-to-full mode (finished, collapsed, or
       reduced motion). Park at the full string; the parent owns the gate. */
    if (!animate) {
      countRef.current = text.length;
      bump((n) => n + 1);
      return;
    }
    if (!grew || became) { countRef.current = 0; doneRef.current = false; }

    /* Fires the gate. Guarded by `doneRef` because StrictMode double-invokes
       this effect on mount and a growing label re-enters it: the parent's Set
       add is idempotent too, so this is belt and braces on purpose. */
    const settle = () => {
      countRef.current = text.length;
      bump((n) => n + 1);
      if (!doneRef.current) { doneRef.current = true; doneCb.current?.(); }
    };

    // Empty label, or a row re-entered after completing: release immediately
    // rather than stalling every line behind it.
    if (countRef.current >= text.length) { settle(); return; }

    let raf = 0;
    let last = performance.now();
    let budget = 0;
    const step = (now: number) => {
      budget += now - last;
      last = now;
      let advanced = false;
      // A loop, not a single step: at 30ms/char one character is shorter than a
      // frame, while a backgrounded tab that suspended rAF comes back with a
      // large budget and should catch up in one go rather than crawl. Bounded
      // by text.length either way.
      while (countRef.current < text.length) {
        const cost = charCost(countRef.current > 0 ? text[countRef.current - 1] : "", msPerChar);
        if (budget < cost) break;
        budget -= cost;
        countRef.current += 1;
        advanced = true;
      }
      if (countRef.current >= text.length) { settle(); return; }
      if (advanced) bump((n) => n + 1);
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [text, animate, msPerChar]);

  const n = Math.min(countRef.current, text.length);
  return { shown: text.slice(0, n), typing: animate && n < text.length };
}

/* ── The reveal gate ────────────────────────────────────────────────────
   One line at a time, in stream order, across the whole panel.

   This replaces a slot queue that broke twice, both times for the same shape of
   reason: the turn was held in an effect and RELEASED IN THAT EFFECT'S CLEANUP.
   (1) The queue object was a fresh literal every render and the header ticks at
   100ms, so the cleanup fired ten times a second and advanced the turn past the
   row that was still typing. (2) With that fixed, StrictMode's mount-time
   effect → cleanup → effect double-invoke fired the same cleanup once on mount,
   re-creating the identical failure.

   So the gate holds no turn and owns no cleanup. It is a monotonically growing
   Set of finished row keys in the PARENT:

     • a row whose key is in the set renders in full, forever;
     • the FIRST row whose key is not in the set types;
     • every row after that renders nothing at all.

   Adding a key twice is a no-op, nothing is ever released, and no cleanup path
   can advance the gate — which makes it immune to both failure modes above, and
   to a re-render at any frequency. Keys, not indices: mergeStream re-splices
   guardrails on every event, so a row's position moves under it while a key
   doesn't.
   Monotonic on purpose — a HITL resume must not rewind the gate and retype the
   record of everything that happened before the approval. */

type RevealMode = "full" | "typing" | "hidden";

/* ── Classification ────────────────────────────────────────────── */

/** Map a status row onto a glyph. Mirrors page.tsx's classifyStep. */
function classify(s: StatusStep): string {
  const tool = (s.tool || "").toLowerCase();
  if (s.phase === "subagent" || s.phase === "delegating" || s.phase === "researching" || tool === "task")
    return "subagent";
  if (s.phase === "memory" || tool.includes("memory")) return "memory";
  if (s.phase === "searching" || tool.includes("search")) return "search";
  if (s.phase === "reading" || tool.includes("read") || tool.startsWith("get_") || tool.startsWith("list_"))
    return "read";
  if (s.phase === "writing" || tool.includes("write") || tool.includes("create") || tool.includes("send"))
    return "write";
  if (s.phase === "tool" || s.tool) return "tool";
  return "think";
}

interface TreeRow {
  step: StatusStep;
  depth: number;
  sub?: SubagentEvent;
  key: string;
}

/**
 * Build the delegation tree from the `parent_id` the backend now sends.
 *
 * Rows produced inside a subagent's namespace carry the `tool_call_id` of the
 * `task` dispatch that spawned them, so a specialist's tool calls nest under
 * their delegation instead of being flattened into one list. A row whose parent
 * is unknown (its `task` row was filtered, or attribution was ambiguous) falls
 * back to the root rather than disappearing.
 */
function buildTree(statusSteps: StatusStep[], subagents: SubagentEvent[]): TreeRow[] {
  // `tool_done` rows carry only a done flag and no label — they were already
  // merged into their start row by the reducer.
  const steps = statusSteps.filter((s) => s.phase !== "tool_done");

  const subById = new Map<string, SubagentEvent>();
  for (const s of subagents) if (s.id) subById.set(s.id, s);

  const ids = new Set<string>();
  for (const s of steps) if (s.id) ids.add(s.id);

  const children = new Map<string, StatusStep[]>();
  const roots: StatusStep[] = [];
  for (const s of steps) {
    const p = s.parent_id;
    if (p && p !== s.id && ids.has(p)) {
      const arr = children.get(p) ?? [];
      arr.push(s);
      children.set(p, arr);
    } else {
      roots.push(s);
    }
  }

  const out: TreeRow[] = [];
  const seen = new Set<StatusStep>(); // cycle guard — cheap, and a malformed parent chain must not hang the UI
  const walk = (s: StatusStep, depth: number, path: string) => {
    if (seen.has(s)) return;
    seen.add(s);
    out.push({ step: s, depth, sub: s.id ? subById.get(s.id) : undefined, key: path });
    if (s.id) (children.get(s.id) ?? []).forEach((c, i) => walk(c, depth + 1, `${path}.${i}`));
  };
  roots.forEach((r, i) => walk(r, 0, String(i)));

  // Any step orphaned by the cycle guard still gets shown, at the root.
  steps.forEach((s, i) => {
    if (!seen.has(s)) out.push({ step: s, depth: 0, sub: undefined, key: `o${i}` });
  });
  return out;
}

/**
 * One entry in the execution stream: either something IRIS did, or a guardrail
 * that corrected it.
 */
type StreamEntry =
  | { kind: "row"; row: TreeRow }
  | { kind: "guardrail"; c: CorrectionEvent; index: number; depth: number };

/** The gate's identity for an entry. Stable across re-splices; see the gate note. */
function entryKey(e: StreamEntry): string {
  return e.kind === "row" ? `r:${e.row.key}` : `c:${e.c.source}:${e.index}`;
}

/**
 * Interleave the guardrails into the activity tree.
 *
 * The workspace is IRIS's execution environment, so a guardrail belongs at the
 * point in the run where it fired — attached to the action it corrected, not
 * filed in a separate list underneath everything. The reducer stamps both arrays
 * with a shared arrival counter (`seq`); each guardrail lands after the last row
 * that had already arrived when it did, and inherits that row's indentation, so a
 * guardrail aimed at a specialist sits inside the specialist's branch.
 *
 * The tree is walked in TREE order, not `seq` order, and that is the deliberate
 * trade: nesting is what makes a multi-agent run legible, and a nested branch
 * still running means a guardrail can land one or two rows off. Approximate
 * placement inside the right branch beats exact placement in a flat list.
 *
 * A guardrail with no `seq` (an older backend, or a correction restored from
 * history) appends at the end — which is precisely the previous behaviour, so
 * nothing is lost when the stamp is missing.
 */
function mergeStream(rows: TreeRow[], corrections: CorrectionEvent[]): StreamEntry[] {
  const out: StreamEntry[] = rows.map((row) => ({ kind: "row" as const, row }));
  if (corrections.length === 0) return out;

  corrections.forEach((c, index) => {
    if (typeof c.seq !== "number") {
      out.push({ kind: "guardrail", c, index, depth: 0 });
      return;
    }
    // Last row at or before this guardrail. `<=` rather than `<` so a tie resolves
    // to the row: the row's seq was claimed first, and a guardrail reads as a
    // reaction to an action, never as its cause.
    let at = -1;
    let depth = 0;
    for (let i = 0; i < out.length; i++) {
      const e = out[i];
      if (e.kind !== "row") continue;
      const s = e.row.step.seq;
      if (typeof s === "number" && s <= c.seq) {
        at = i;
        depth = e.row.depth;
      }
    }
    out.splice(at + 1, 0, { kind: "guardrail", c, index, depth });
  });
  return out;
}

/** Plain-text form of one entry, for the Copy button. */
function entryText(e: StreamEntry): string {
  if (e.kind === "guardrail") {
    const pad = "  ".repeat(e.depth);
    const raw = e.c.raw && e.c.raw.trim() ? `\n${pad}#   ${e.c.raw.trim().replace(/\n/g, `\n${pad}#   `)}` : "";
    /* The tag the row carries on screen goes into the copied text too, so a
       pasted transcript still says which lines were IRIS correcting itself. */
    const tag = e.c.severity === "warn" ? "SELF-CORRECTION" : "GUARDRAIL";
    return `${pad}# [${tag}] ${e.c.label ?? e.c.source}${raw}`;
  }
  return `${"  ".repeat(e.row.depth)}PS> ${rowLabel(e.row).full}`;
}

/** The console block cursor that trails the text being typed. Sized to one
 *  JetBrains Mono character cell (advance width is exactly 0.6em — it was 0.56em
 *  while the rail was set in Consolas) and painted in the body foreground, so it
 *  sits in the line like a real shell cursor rather than reading as a coloured
 *  decoration. */
function Caret() {
  return (
    <span
      aria-hidden
      style={{
        display: "inline-block", width: "0.6em", height: "1.05em",
        marginLeft: 1, verticalAlign: "text-bottom",
        background: T.text,
        animation: "wsCaret 1s steps(1,end) infinite",
      }}
    />
  );
}

/* ── Rows ──────────────────────────────────────────────────────── */

/** Real names, not abstracted domain labels: "grace · send_research_email".
 *  Split out so the Copy button and the row render the same string. */
function rowLabel(row: TreeRow): { label: string; detail: string; full: string } {
  const { step, sub } = row;
  const label =
    sub?.subagent_type
      ? `${sub.subagent_type}${step.tool && step.tool !== "task" ? ` · ${step.tool}` : ""}`
      : step.detail || (step.tool ? step.tool : "Working…");
  const detail = sub?.description || (sub ? "" : step.tool && step.detail !== step.tool ? step.tool : "");
  return { label, detail, full: detail ? `${label} — ${detail}` : label };
}

function ActivityRow({
  row, active, mode, rush, onDone,
}: {
  row: TreeRow;
  active: boolean;
  mode: RevealMode;
  rush: number;
  onDone: (key: string) => void;
}) {
  const { step, depth, sub } = row;
  const kind = classify(step);
  const { label, full } = rowLabel(row);
  const blank = sub?.status === "blank";
  const key = `r:${row.key}`;

  const done = useCallback(() => onDone(key), [onDone, key]);
  /* Typed as ONE string so the reveal runs continuously across the label and
     its detail, then split again for rendering — the detail keeps its muted
     tone instead of the row typing in two visibly separate bursts. */
  const { shown, typing } = useTypewriter(
    full,
    mode === "typing",
    (SPEED[kind] ?? 30) / rush,
    done,
  );
  const shownLabel = shown.slice(0, label.length);
  const shownDetail = shown.length > label.length ? shown.slice(label.length) : "";

  // Not yet reached by the gate. Nothing is rendered — not even an empty line —
  // so the rail grows downward the way console output does.
  if (mode === "hidden") return null;

  return (
    <div
      style={{
        display: "flex", alignItems: "center", gap: 8,
        padding: "3px 2px",
        paddingLeft: 2 + depth * 16,
        animation: "wsFadeUp .2s ease",
      }}
    >
      {depth > 0 && (
        <span aria-hidden style={{ color: T.muted, fontSize: 11, flexShrink: 0, opacity: 0.7 }}>↳</span>
      )}
      <RowIcon kind={kind} active={active} />
      <span
        style={{
          ...CODE,
          flex: 1, minWidth: 0, fontSize: 12, lineHeight: 1.5,
          color: active ? T.text : T.muted,
          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          transition: "color .3s",
        }}
      >
        {/* The prompt on every command line, which is what makes the rail read
            as a console transcript rather than a bullet list. Accented on the
            row executing right now, dim on the record behind it. `PS>` rather
            than a full `PS C:\…>` — a fabricated path would be decoration, and
            this panel is not pretending to be a shell it isn't. */}
        <span style={{ color: active ? T.accent : T.dim }}>{"PS> "}</span>
        {shownLabel}
        {shownDetail ? (
          <span style={{ color: T.muted, opacity: 0.75 }}>{shownDetail}</span>
        ) : null}
        {/* The cursor stays on the active row after its text finishes: a shell
            leaves the block cursor blinking at the prompt while the command
            runs, and that is exactly this row's state. */}
        {(typing || active) && <Caret />}
      </span>
      {blank && (
        <span style={{ ...CODE, fontSize: 10.5, color: T.amber, flexShrink: 0 }} title="The specialist returned an empty result">
          blank
        </span>
      )}
      {!active && step.done && <CheckIcon />}
    </div>
  );
}

/**
 * One self-correcting guardrail, shown where in the run it fired.
 *
 * These rows are the harness steering IRIS mid-run: the blank-response and
 * empty-completion recoveries, the loop breaker, and the model profile's
 * commit / entity / final-answer guards. They used to render as a shell comment —
 * `# Caught an empty response and kept going` — which filed IRIS correcting
 * itself in the visual register of a throwaway remark, indistinguishable at a
 * glance from a dim command line.
 *
 * They are the opposite of a throwaway. Each one is a guardrail catching IRIS
 * mid-mistake and putting the run back on course, and that is the single most
 * reassuring thing a person watching a ten-minute orchestration can see. So a
 * guardrail is now a banded row, tagged for what it is, and it carries its own
 * explanation instead of only the verbatim steering text.
 *
 * The tag splits on the backend's own definition of severity
 * (guardrail_taxonomy.py:119-120), not on a new one invented here:
 *
 *   • `warn` — a guard that CAUGHT something wrong (an empty answer, a loop, a
 *     vague final answer) ⇒ `SELF-CORRECTION`, in amber.
 *   • `info` — a forward-looking guard that steered IRIS before it went wrong
 *     ⇒ `GUARDRAIL`, in the accent.
 *
 * Bracketed uppercase because that is how a console prints a level, and this
 * panel is a console.
 */
function CorrectionRow({
  c, index, depth, mode, rush, onDone,
}: {
  c: CorrectionEvent;
  index: number;
  /** Indentation of the row this guardrail follows, so it attaches to that action. */
  depth: number;
  mode: RevealMode;
  rush: number;
  onDone: (key: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const caught = c.severity === "warn";
  const color = caught ? T.amber : T.accent;
  const hasRaw = Boolean(c.raw && c.raw.trim());

  /* The plain-language "why", from the taxonomy shared with the backend.
     Only for a RECOGNISED source: correctionCopy falls back to the
     empty-completion copy for an unknown kind, and captioning a loop guard with
     the wrong explanation is worse than showing no caption at all. */
  const kind = correctionKind(c.source);
  const why = kind ? correctionCopy(kind).sub : "";
  const canOpen = hasRaw || Boolean(why);

  const key = `c:${c.source}:${index}`;
  const done = useCallback(() => onDone(key), [onDone, key]);
  const { shown, typing } = useTypewriter(c.label ?? "", mode === "typing", SPEED.guardrail / rush, done);

  if (mode === "hidden") return null;
  return (
    <div
      style={{
        // Matches ActivityRow's indent step, so the guardrail lines up with the
        // action it curated rather than starting a new column.
        paddingLeft: depth * 16,
        margin: "3px 0",
        animation: "wsFadeUp .2s ease",
      }}
    >
      {/* The band is what separates a guardrail from the command lines around it.
          Rail in the severity colour, field in the same faint accent tint the
          verbatim quote uses — one surface treatment for "this is commentary on
          the run", not a second one invented for this row. */}
      <div
        style={{
          background: T.quote,
          borderLeft: `2px solid ${color}`,
          borderRadius: "0 6px 6px 0",
        }}
      >
        <button
          onClick={(e) => { e.stopPropagation(); if (canOpen) setOpen((o) => !o); }}
          aria-expanded={canOpen ? open : undefined}
          style={{
            display: "flex", alignItems: "center", gap: 8, width: "100%",
            background: "transparent", border: "none", padding: "4px 8px",
            cursor: canOpen ? "pointer" : "default", textAlign: "left",
            fontFamily: "inherit", color: "inherit",
          }}
        >
          {/* shield-with-check — the same guardrail mark SystemCorrectionCard uses */}
          <svg
            width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={color}
            strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}
          >
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            <polyline points="9 12 11 14 15 10" />
          </svg>
          <span
            title={
              caught
                ? "A guardrail caught a problem in IRIS's own output and put the run back on course."
                : "A guardrail steered IRIS before it went wrong."
            }
            style={{
              ...CODE, fontSize: 10.5, fontWeight: 700, letterSpacing: "0.05em",
              color, flexShrink: 0, whiteSpace: "nowrap",
            }}
          >
            {caught ? "[SELF-CORRECTION]" : "[GUARDRAIL]"}
          </span>
          <span
            style={{
              ...CODE,
              flex: 1, minWidth: 0, fontSize: 12, lineHeight: 1.5, color: T.text,
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}
          >
            {shown}
            {typing && <Caret />}
          </span>
          {c.persisted === false && (
            <span
              style={{ ...CODE, fontSize: 10.5, color: T.muted, flexShrink: 0 }}
              title="Request-only guardrail — steered this model call but was never written to thread state"
            >
              live only
            </span>
          )}
          {canOpen && <Chevron open={open} />}
        </button>

        {open && (
          <div style={{ padding: "0 9px 8px 8px" }}>
            {why && (
              <div style={{ ...CODE, fontSize: 11.5, lineHeight: 1.55, color: T.muted }}>
                {why}
              </div>
            )}
            {hasRaw && (
              <>
                <div
                  style={{
                    ...CODE, fontSize: 10, fontWeight: 700, letterSpacing: "0.06em",
                    textTransform: "uppercase", color: T.dim, margin: "7px 0 3px",
                  }}
                >
                  verbatim steering text
                </div>
                {/* Rendered as INERT pre-wrap text on purpose. A guardrail can
                    quote third-party content (an email body, a fetched page), so
                    it is never passed through the markdown renderer. */}
                <div
                  style={{
                    ...CODE,
                    fontSize: 11.5, lineHeight: 1.55, color: T.muted,
                    whiteSpace: "pre-wrap", maxHeight: 180, overflowY: "auto",
                  }}
                >
                  {c.raw}
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** The live plan. Dense variant of page.tsx's TodoChecklist, sized for the panel. */
function PlanRows({ todos }: { todos: any[] }) {
  const items = todos
    .map((t) => {
      // `content` is LangChain's real Todo key; the other two are the backend's
      // compatibility spellings.
      const text = String(t?.content ?? t?.description ?? t?.task_description ?? "").trim();
      let status = String(t?.status ?? t?.task_status ?? "pending").toLowerCase().replace(/-/g, "_");
      if (!["pending", "in_progress", "completed"].includes(status)) status = "pending";
      return { text, status };
    })
    .filter((t) => t.text);
  if (items.length === 0) return null;
  const done = items.filter((t) => t.status === "completed").length;

  return (
    <Section title="Plan" badge={`${done}/${items.length}`}>
      {items.map((t, i) => (
        <div
          key={i}
          style={{ ...CODE, display: "flex", alignItems: "flex-start", gap: 8, padding: "3px 2px", fontSize: 12, lineHeight: 1.45 }}
        >
          <span style={{ flexShrink: 0, marginTop: 2, width: 14, height: 14, display: "inline-flex", alignItems: "center", justifyContent: "center" }}>
            {t.status === "completed" ? (
              <CheckIcon color={T.accent} size={13} />
            ) : t.status === "in_progress" ? (
              <svg
                width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={T.accent}
                strokeWidth="2.5" strokeLinecap="round" style={{ animation: "wsSpin .9s linear infinite" }}
              >
                <path d="M21 12a9 9 0 1 1-6.219-8.56" />
              </svg>
            ) : (
              <span style={{ width: 11, height: 11, borderRadius: "50%", border: `1.6px solid ${T.hair}`, display: "inline-block" }} />
            )}
          </span>
          <span
            style={{
              ...CODE,
              color: t.status === "completed" ? T.muted : T.text,
              textDecoration: t.status === "completed" ? "line-through" : "none",
              fontWeight: t.status === "in_progress" ? 600 : 400,
            }}
          >
            {t.text}
          </span>
        </div>
      ))}
    </Section>
  );
}

function Section({
  title, badge, children,
}: {
  title: string;
  badge?: string;
  children: React.ReactNode;
}) {
  return (
    <div style={{ padding: "8px 0" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
        <span
          style={{
            ...CODE,
            fontSize: 10.5, fontWeight: 700, letterSpacing: "0.07em",
            textTransform: "uppercase", color: T.muted,
          }}
        >
          {title}
        </span>
        {badge && (
          <span style={{ ...CODE, fontSize: 10.5, fontWeight: 600, color: T.muted }}>
            {badge}
          </span>
        )}
        <span style={{ flex: 1, height: 1, background: T.hair, opacity: 0.7 }} />
      </div>
      {children}
    </div>
  );
}

/* ── Duration ──────────────────────────────────────────────────── */

/** "Worked for 1 second" / "Worked for 47 seconds". Never "0 seconds". */
function workedLabel(ms: number): string {
  const s = Math.max(1, Math.round(ms / 1000));
  return `Worked for ${s} second${s === 1 ? "" : "s"}`;
}

const TERMINAL_PILL: Record<string, { text: string; color: string }> = {
  timeout: { text: "timed out", color: T.amber },
  error: { text: "error", color: T.red },
  paused: { text: "awaiting approval", color: T.amber },
  empty: { text: "no answer", color: T.amber },
  rate_limit: { text: "rate limited", color: T.amber },
};

/* ══════════════════════════════════════
   MAIN EXPORT — AgentWorkspace
══════════════════════════════════════ */
export default function AgentWorkspace({
  running,
  startedAt,
  workedMs,
  todos = [],
  statusSteps = [],
  subagents = [],
  corrections = [],
  transcript = "",
  terminal = null,
  durationKnown = true,
  onRecover,
}: {
  /** True while the run is streaming. Drives the ticker and the auto-collapse. */
  running: boolean;
  /** ms epoch of the first workspace event, stamped by the reducer. */
  startedAt?: number;
  /** Frozen elapsed time, set by the reducer on `terminal`. */
  workedMs?: number;
  todos?: any[];
  statusSteps?: StatusStep[];
  subagents?: SubagentEvent[];
  corrections?: CorrectionEvent[];
  /** Streamed orchestrator prose, already flattened. Rendered as inert text. */
  transcript?: string;
  terminal?: TerminalEvent | null;
  /**
   * False for a record rebuilt from the checkpointer after a reload. Wall-clock
   * duration is not persisted anywhere in graph state, so there is no honest
   * "Worked for N seconds" to print — and `workedLabel` floors at 1, so a missing
   * duration would confidently render "Worked for 1 second" for a ten-minute run.
   * The header shows the record's shape instead.
   */
  durationKnown?: boolean;
  /** Offered when the run ended resumably (abort/timeout with a persisted answer). */
  onRecover?: () => void;
}) {
  /* Collapse exactly once, on the running → DONE transition. After that the
     user owns the state — the panel never collapses or expands under them,
     and it never unmounts itself.

     A HITL pause is explicitly not "done". `running` goes false while the
     approval card is up, which used to collapse the panel and latch `settled`,
     so the resumed half of the run then streamed into a panel that was shut and
     would never reopen — the record of the approval's own consequences hidden
     behind a chevron. A pause holds the panel open, and if the user did collapse
     it themselves, resuming re-opens it: they are being shown new work. */
  const paused = terminal?.reason === "paused";
  /* A resumable end is also not "done". The run stopped with its answer sitting in
     the checkpoint, and the only affordance that gets it back — "Recover answer" —
     is rendered inside the panel BODY, so collapsing here would hide the button in
     exactly the case it exists for. Computed above the effect because the effect
     reads it; `canRecover` below is the same predicate. */
  const recoverable = Boolean(!running && !paused && terminal?.resumable && onRecover);
  const [collapsed, setCollapsed] = useState(false);
  const settled = useRef(false);
  const wasRunning = useRef(running);
  useEffect(() => {
    if (running && !wasRunning.current) {
      // Resumed after an approval: reopen and re-arm the one-shot collapse so
      // the real end of the run still collapses the panel.
      settled.current = false;
      setCollapsed(false);
    }
    wasRunning.current = running;

    if (!running && !paused && !recoverable && !settled.current) {
      settled.current = true;
      setCollapsed(true);
    }
  }, [running, paused, recoverable]);

  /* Ticker: re-render at 100 ms while running so the header counts up. The
     elapsed value is derived from `startedAt`, not accumulated, so a dropped
     frame or a backgrounded tab can't make it drift. */
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!running) return;
    const iv = setInterval(() => setTick((t) => t + 1), 100);
    return () => clearInterval(iv);
  }, [running]);

  const elapsedMs = useMemo(() => {
    void tick; // re-read the clock on every tick
    if (!running) return workedMs ?? (typeof startedAt === "number" ? Date.now() - startedAt : 0);
    return typeof startedAt === "number" ? Math.max(0, Date.now() - startedAt) : 0;
  }, [running, startedAt, workedMs, tick]);

  const rows = useMemo(() => buildTree(statusSteps, subagents), [statusSteps, subagents]);

  /* Activity and guardrail rows, interleaved into the single stream the panel
     renders. Memoized because mergeStream splices per correction and this runs on
     every SSE event. */
  const stream = useMemo(() => mergeStream(rows, corrections), [rows, corrections]);
  const keys = useMemo(() => stream.map(entryKey), [stream]);
  /* "guardrail", never "nudge". What the user is being shown is IRIS's harness
     catching IRIS mid-mistake, and "nudge" undersells that to the point of
     misdescribing it. The word is gone from every user-visible string in this
     panel; the backend event type stays `correction` and is not renamed. */
  const guardrailBadge =
    corrections.length > 0
      ? `${corrections.length} guardrail${corrections.length === 1 ? "" : "s"}`
      : undefined;

  // The active row is the first unfinished one — the same "what is IRIS doing
  // right now" signal AgentSearchCard derived, but tree-aware.
  const activeKey = running ? rows.find((r) => !r.step.done)?.key : undefined;
  const activeLabel = rows.find((r) => r.key === activeKey)?.step.detail ?? "";

  const toolCount = rows.filter((r) => !r.sub).length;
  const delegations = subagents.length;
  const hasBody =
    rows.length > 0 || corrections.length > 0 || todos.length > 0 || Boolean(transcript.trim());

  const pill = terminal && terminal.reason !== "complete" ? TERMINAL_PILL[terminal.reason] : undefined;
  /* Not while paused: the run is not lost, it is waiting on the approval card
     sitting right underneath. Offering "Recover answer" there invites the user to
     go looking for a saved answer instead of making the decision that unblocks
     the run they are watching. Same predicate as `recoverable` above, which also
     holds the panel open so this button is actually reachable. */
  const canRecover = recoverable;

  /* Rows type themselves in only while the run is LIVE and the panel is open,
     and never for someone who asked the OS not to animate. Reopening a finished
     run shows the record instantly — replaying an animation over a transcript
     you are trying to read would be theatre, and a collapsed panel would animate
     to nobody. */
  const reduced = useReducedMotion();
  const typewriter = running && !collapsed && !reduced;

  /* ── The reveal gate (see the note above `RevealMode`) ── */
  const [typedKeys, setTypedKeys] = useState<Set<string>>(() => new Set());
  const [holding, setHolding] = useState(false);
  const holdTimer = useRef<number | null>(null);

  /** A row finished typing. Monotonic, idempotent, and never released. */
  const markTyped = useCallback((key: string) => {
    setTypedKeys((prev) => {
      if (prev.has(key)) return prev;
      const next = new Set(prev);
      next.add(key);
      return next;
    });
    setHolding(true);
    if (holdTimer.current !== null) window.clearTimeout(holdTimer.current);
    holdTimer.current = window.setTimeout(() => setHolding(false), REVEAL_GAP);
  }, []);

  useEffect(() => () => {
    if (holdTimer.current !== null) window.clearTimeout(holdTimer.current);
  }, []);

  /** Skip the animation for everything on screen. Clicking the rail is the
   *  console idiom for "stop typing at me, I want to read the output". */
  const fastForward = useCallback(() => {
    setTypedKeys((prev) => {
      let changed = false;
      const next = new Set(prev);
      for (const k of keys) if (!next.has(k)) { next.add(k); changed = true; }
      return changed ? next : prev;
    });
    if (holdTimer.current !== null) window.clearTimeout(holdTimer.current);
    setHolding(false);
  }, [keys]);

  /* Snap-to-full mode marks every current row as typed, so the gate's idea of
     the record matches what is on screen. Without this, collapsing mid-run and
     reopening would hide everything that arrived while it was shut and retype
     the backlog line by line. Purely additive, so a StrictMode double-invoke
     changes nothing. */
  useEffect(() => {
    if (typewriter) return;
    setTypedKeys((prev) => {
      let changed = false;
      const next = new Set(prev);
      for (const k of keys) if (!next.has(k)) { next.add(k); changed = true; }
      return changed ? next : prev;
    });
  }, [typewriter, keys]);

  /* One row types at a time: the first key the gate has not seen. Everything
     after it renders nothing, so the rail grows one line at a time. */
  const view = useMemo(() => {
    let pendingTaken = false;
    return stream.map((e, i) => {
      const key = keys[i];
      if (!typewriter || typedKeys.has(key)) return { e, key, mode: "full" as RevealMode };
      if (!pendingTaken) {
        pendingTaken = true;
        return { e, key, mode: (holding ? "hidden" : "typing") as RevealMode };
      }
      return { e, key, mode: "hidden" as RevealMode };
    });
  }, [stream, keys, typewriter, typedKeys, holding]);

  /* Backlog rush. Steps can land faster than 33 cps can print them, and without
     this the rail drifts further behind reality the longer a busy run goes —
     "live" has to mean current, not merely animated. A single queued line types
     at full speed; a pile-up types progressively faster, capped at 4x so it
     never becomes an invisible flash. Quantised so the rate doesn't churn the
     rAF effect on every SSE event. */
  const backlog = view.reduce((n, v) => n + (v.mode === "hidden" ? 1 : 0), 0);
  const rush = backlog > 2 ? Math.round(Math.min(4, 1 + backlog / 3) * 2) / 2 : 1;

  /* Pin the rail to the newest line, unless the user has scrolled up to read.
     A ResizeObserver on the content rather than an effect on `stream`: rows grow
     as they type, so height changes between events too. */
  const railRef = useRef<HTMLDivElement | null>(null);
  const innerRef = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);
  const onRailScroll = useCallback(() => {
    const el = railRef.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 28;
  }, []);
  useEffect(() => {
    const rail = railRef.current;
    const inner = innerRef.current;
    if (!rail || !inner || typeof ResizeObserver === "undefined") return;
    const pin = () => { if (stick.current) rail.scrollTop = rail.scrollHeight; };
    pin();
    const ro = new ResizeObserver(pin);
    ro.observe(inner);
    return () => ro.disconnect();
  }, [collapsed]);

  /* Copy the run as plain text. The guardrails' verbatim `raw` is included as
     shell comments — it is the part of the record you actually want to paste into
     a bug report — and it is only ever text on a clipboard, never markup. */
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<number | null>(null);
  useEffect(() => () => {
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
  }, []);
  const copyTranscript = useCallback(() => {
    const lines = stream.map(entryText);
    if (transcript.trim()) lines.push("", "# --- notes ---", transcript.trim());
    const text = lines.join("\n");
    const ok = () => {
      setCopied(true);
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopied(false), 1500);
    };
    navigator.clipboard?.writeText(text).then(ok).catch(() => {
      /* Clipboard denied (insecure context, or the user refused the permission).
         Silent: a failed copy must not push an error into a panel whose job is
         to report on someone else's run. */
    });
  }, [stream, transcript]);

  const summaryBits = [
    toolCount > 0 ? `${toolCount} step${toolCount === 1 ? "" : "s"}` : "",
    delegations > 0 ? `${delegations} delegation${delegations === 1 ? "" : "s"}` : "",
    corrections.length > 0 ? `${corrections.length} guardrail${corrections.length === 1 ? "" : "s"}` : "",
  ].filter(Boolean);

  return (
    <div
      data-iris-ws
      style={{
        /* Chromeless: no border, no field, no blur, no radius. The panel is part
           of the message, so it inherits whatever surface the bubble sits on. */
        background: "transparent",
        border: "none",
        width: "100%",
        marginBottom: 10,
        animation: "wsFadeUp .35s ease",
        /* The code theme, set once at the root so every descendant inherits it —
           including the header buttons, which take `fontFamily: "inherit"`. Each
           text node below still spreads CODE explicitly, because a `style={{}}`
           that names a different `fontFamily` would otherwise win locally; the
           root is the guarantee, not the only application. */
        ...CODE,
      }}
    >
      <style>{`
        @keyframes wsSpin   { from { transform:rotate(0deg); } to { transform:rotate(360deg); } }
        @keyframes wsFadeUp { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:translateY(0); } }
        @keyframes wsCaret  { 0%,49% { opacity:.85; } 50%,100% { opacity:0; } }
        [data-iris-ws] .ws-rail::-webkit-scrollbar { width: 8px; }
        [data-iris-ws] .ws-rail::-webkit-scrollbar-track { background: transparent; }
        [data-iris-ws] .ws-rail::-webkit-scrollbar-thumb {
          background: var(--scroll-thumb); border-radius: 8px; border: 2px solid transparent;
          background-clip: content-box;
        }
        [data-iris-ws] .ws-ghost { opacity: .55; transition: opacity .18s; }
        [data-iris-ws] .ws-ghost:hover { opacity: 1; }
        @media (prefers-reduced-motion: reduce) {
          [data-iris-ws] *, [data-iris-ws] { animation: none !important; transition: none !important; }
        }
      `}</style>

      {/* ── Header — the row the panel collapses to.
             A div, not a button: the copy control is a real button and nesting
             one inside another is invalid HTML (and unreachable by keyboard). ── */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "2px 0 4px" }}>
        <button
          onClick={() => hasBody && setCollapsed((v) => !v)}
          aria-expanded={hasBody ? !collapsed : undefined}
          style={{
            display: "flex", alignItems: "center", gap: 9,
            flex: 1, minWidth: 0,
            background: "transparent", border: "none", padding: "3px 0",
            cursor: hasBody ? "pointer" : "default",
            userSelect: "none", textAlign: "left", fontFamily: "inherit",
          }}
        >
          {running ? <Spinner size={15} /> : pill ? <RowIcon kind="think" active={false} /> : <CheckIcon size={13} />}

          <span style={{ ...CODE, fontSize: 12.5, fontWeight: 500, color: T.text, flexShrink: 0 }}>
            {running ? "Working" : durationKnown ? workedLabel(elapsedMs) : "Execution record"}
          </span>

          {running && (
            <span style={{ ...CODE, fontSize: 11.5, color: T.muted, flexShrink: 0 }}>
              {(elapsedMs / 1000).toFixed(1)}s
            </span>
          )}

          {/* While collapsed, say what happened so the arrow is worth opening.
              Also the only detail a rebuilt record has to offer — it has no
              duration, so `summaryBits` carries the whole label there. */}
          {!running && collapsed && summaryBits.length > 0 && (
            <span
              style={{
                ...CODE,
                fontSize: 11.5, color: T.muted, flex: 1, minWidth: 0,
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              }}
            >
              {summaryBits.join(" · ")}
            </span>
          )}
          {running && activeLabel && (
            <span
              style={{
                ...CODE,
                fontSize: 11.5, color: T.muted, flex: 1, minWidth: 0,
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              }}
            >
              {activeLabel}
            </span>
          )}
          {!(running && activeLabel) && !(!running && collapsed && summaryBits.length > 0) && (
            <span style={{ flex: 1 }} />
          )}

          {pill && (
            <span
              style={{
                ...CODE,
                fontSize: 10.5, color: pill.color, background: "transparent",
                border: `1px solid ${pill.color}`, opacity: 0.85,
                borderRadius: 20, padding: "1px 8px", flexShrink: 0,
              }}
            >
              {pill.text}
            </span>
          )}

          {hasBody && <Chevron open={!collapsed} />}
        </button>

        {hasBody && !collapsed && (
          <button
            className="ws-ghost"
            onClick={copyTranscript}
            title="Copy the run as text"
            style={{
              display: "inline-flex", alignItems: "center", gap: 5,
              background: "transparent", border: "none", padding: "3px 2px",
              color: copied ? T.green : T.muted, cursor: "pointer",
              fontFamily: "inherit", fontSize: 10.5, flexShrink: 0,
            }}
          >
            {copied ? <CheckIcon size={12.5} color={T.green} /> : <CopyIcon />}
            {copied ? "Copied" : "Copy"}
          </button>
        )}
      </div>

      {/* ── Body — stays MOUNTED when collapsed, so reopening costs nothing ── */}
      {hasBody && (
        <div
          style={{
            maxHeight: collapsed ? 0 : RAIL_MAX + 60,
            overflow: "hidden",
            transition: "max-height .35s ease",
          }}
        >
          <div
            ref={railRef}
            className="ws-rail"
            onScroll={onRailScroll}
            onClick={fastForward}
            style={{ maxHeight: RAIL_MAX + 60, overflowY: "auto", paddingRight: 4 }}
          >
            <div ref={innerRef}>
              {todos.length > 0 && <PlanRows todos={todos} />}

              {stream.length > 0 && (
                /* ONE stream, not an activity list plus a guardrail footer. The
                   panel is the environment IRIS executes in, and the guardrails are
                   part of that execution — reading them in place is what shows WHY
                   the next action changed. The badge keeps the intervention count
                   visible while the panel is collapsed-adjacent, since that was the
                   only thing the separate section header carried. */
                <Section title="Execution" badge={guardrailBadge}>
                  {view.map((v) =>
                    v.e.kind === "row" ? (
                      <ActivityRow
                        key={v.key}
                        row={v.e.row}
                        active={v.e.row.key === activeKey}
                        mode={v.mode}
                        rush={rush}
                        onDone={markTyped}
                      />
                    ) : (
                      <CorrectionRow
                        key={v.key}
                        c={v.e.c}
                        index={v.e.index}
                        depth={v.e.depth}
                        mode={v.mode}
                        rush={rush}
                        onDone={markTyped}
                      />
                    ),
                  )}
                </Section>
              )}

              {transcript.trim() && (
                <Section title="Notes">
                  {/* IRIS's own prose during the run — the intent log and any
                      interim narration. Inert pre-wrap text, never markdown:
                      it can quote tool output that carries injected instructions. */}
                  <div
                    style={{
                      ...CODE,
                      fontSize: 11.5, lineHeight: 1.6, color: T.muted,
                      whiteSpace: "pre-wrap", padding: "2px 0",
                    }}
                  >
                    {transcript}
                  </div>
                </Section>
              )}

              {canRecover && (
                <div style={{ paddingTop: 10, borderTop: `1px solid ${T.hair}`, marginTop: 6 }}>
                  <div style={{ ...CODE, fontSize: 11.5, color: T.muted, marginBottom: 7, lineHeight: 1.5 }}>
                    The run stopped early, but IRIS may have already saved an answer.
                  </div>
                  <button
                    onClick={(e) => { e.stopPropagation(); onRecover?.(); }}
                    style={{
                      ...CODE,
                      padding: "5px 12px", borderRadius: 7, border: `1px solid ${T.accent}`,
                      background: "transparent", color: T.accent, fontSize: 11.5,
                      fontWeight: 600, cursor: "pointer",
                    }}
                  >
                    Recover answer
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
