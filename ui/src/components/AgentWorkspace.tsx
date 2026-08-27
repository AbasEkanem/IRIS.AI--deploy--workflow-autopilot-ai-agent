"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Sparkles,
  PenLine,
  Terminal,
  FileText,
  Search,
  Cpu,
  Database,
  ShieldCheck,
  CircleCheckBig,
  ChevronDown,
  ChevronRight,
  Circle,
  CircleDot,
  Copy,
  Check,
} from "lucide-react";
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
   Three properties that are the whole point, unchanged since the first
   version:

   1. It does NOT auto-dismiss. The body stays MOUNTED behind
      `maxHeight: collapsed ? 0 : N`, so reopening the chevron shows the
      full transcript with zero refetch. AgentSearchCard unmounted 1.8 s
      after the run finished, which is why it could not be reused: the
      record of a ten-minute orchestration evaporated before anyone
      could read it.
   2. It shows the harness steering IRIS — the blank-response, loop and
      tool-call-repair guards — as first-class rows, so a long run is
      legible instead of being a spinner.
   3. Nothing here reaches the chat. The chat holds one live line while
      this fills, then the parsed summary. This panel is the workspace.
   ══════════════════════════════════════════════════════════════════ */

/* ── The console card ───────────────────────────────────────────────────
   This panel used to be chromeless and painted entirely from iris.css
   tokens, so it inherited whatever surface the message bubble sat on.

   It is now a card with its own dark field, in every theme, deliberately: the
   execution rail is a terminal, and a terminal is a surface you look INTO, not a
   region of the page it happens to sit in. That is also why the palette below is
   literal hex rather than tokens — the card composites over nothing but itself,
   so there is no theme scope for these values to be wrong in, and no fifth place
   to keep in sync when the app's palette changes again.

   The one boundary: the HEADER row (the line the card collapses to) stays on
   theme tokens. It is a control belonging to the message, not part of the
   console, and a dark strip floating in a light chat with no field under it reads
   as a rendering bug. `T` is that header palette; `P` is the console. */
const P = {
  /** The card field, and the rail's own ground. */
  card: "#131316",
  /** Nested boxes — detail bodies, the verbatim quote — one step darker. */
  well: "#0f0f12",
  border: "#2c2c32",
  borderHover: "#3a3a40",
  /** Badge chips (tool names, counts). */
  chip: "#1c1c21",
  text: "#e7e7ea",
  strong: "#f0f0f2",
  dim: "#8a8f98",
  faint: "#55585f",
  /** The one accent, reserved for reasoning beats and the live row. */
  accent: "#e8825a",
  green: "#5fb96c",
  amber: "#e5c15a",
  red: "#e5716a",
  rail: "#2a2a2e",
  /** Identifier colour inside an inline-code pill. */
  code: "#f0d9a8",
};

/** Header-row palette — theme tokens, because the header is part of the message.
 *  `--text-muted`, never `--muted`: the latter exists only inside page.tsx's
 *  shell-scoped <style> and carries a stale fallback from a retired palette. */
const T = {
  text: "var(--text)",
  muted: "var(--text-muted)",
  dim: "var(--text-dim)",
  accent: "var(--accent)",
  green: "var(--green)",
  amber: "var(--amber)",
  red: "var(--red)",
};

/* ── The code theme ─────────────────────────────────────────────────────
   JetBrains Mono, loaded and self-hosted by next/font in layout.tsx.

   It has to come through the CSS variable rather than by family name: next/font
   rewrites the family to a hashed `__JetBrains_Mono_*`, so a literal
   `'JetBrains Mono'` in a stack matches nothing and falls straight through to
   Consolas. `var(--font-mono)` is the only handle that resolves, and it resolves
   here because layout.tsx puts the variable on <body>, which this panel is a
   descendant of. */
const MONO =
  "var(--font-mono), ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Monaco, " +
  "Consolas, 'Liberation Mono', monospace";

/**
 * The face plus the two font features, applied to every text node in the panel.
 *
 * **Ligatures off**, deliberately. JetBrains Mono ships coding ligatures, so
 * `->`, `=>`, `!=` and `::` fuse into single glyphs — and this panel prints tool
 * names, file paths and email addresses that a user has to be able to read back
 * and retype character for character.
 *
 * `tnum` fixes digit width so the elapsed-time readout and the row counters stop
 * reflowing as they tick.
 */
const CODE: React.CSSProperties = {
  fontFamily: MONO,
  fontVariantLigatures: "none",
  fontFeatureSettings: '"liga" 0, "calt" 0, "tnum" 1',
};

/** Height the console card scrolls inside while the run is live, and once the
 *  user has asked for the full transcript. Fixed, not content-derived: the card's
 *  own height must not change as rows land, or every step shoves the answer
 *  further down the page. `maxHeight`, so a two-step run doesn't reserve 380px of
 *  blank field. */
const CARD_MAX = 380;
const CARD_MAX_FULL = 480;

/** Beat between one line finishing and the next appearing. Without it a burst of
 *  rows reads as one continuous smear of text rather than as separate commands. */
const REVEAL_GAP = 200;

/* ── Icons ───────────────────────────────────────────────────────────────
   lucide-react, one glyph per row kind. Note two renames: this is
   lucide-react 1.x, which dropped the `*2` compatibility aliases, so
   `CheckCircle2` is `CircleCheckBig` and `Code2` is `CodeXml`. Importing the
   old names typechecks as `any` under some configs and then crashes at render
   with "type is invalid", which is a much worse failure than a build error. */
const KIND_ICON = {
  think: Sparkles,
  read: FileText,
  write: PenLine,
  search: Search,
  memory: Database,
  subagent: Cpu,
  tool: Terminal,
} as const;

/**
 * Row colour by kind. One accent, and it is spent on reasoning.
 *
 * Everything IRIS *does* is grey and everything IRIS *thinks* is coral, so
 * scanning the rail tells you where the decisions were without reading a word.
 * Spreading the accent across tool calls too would make the colour mean "a row",
 * which is to say nothing.
 */
const KIND_COLOR: Record<string, string> = {
  think: P.accent,
  read: P.dim,
  write: P.dim,
  search: P.dim,
  memory: P.dim,
  subagent: P.dim,
  tool: P.dim,
};

/**
 * Render backtick spans as inline monospace pills.
 *
 * The guardrail labels and the taxonomy's explanations name tools, state fields
 * and message names in backticks (`iris_toolcall_repair`, `write_todos`), and
 * those are the words a reader needs to pick out of the sentence. Plain text, no
 * markdown renderer: a guardrail can quote third-party content.
 */
function InlineText({ text }: { text: string }) {
  const parts = text.split(/(`[^`]+`)/g);
  return (
    <>
      {parts.map((part, i) =>
        part.startsWith("`") && part.endsWith("`") && part.length > 2 ? (
          <code
            key={i}
            style={{
              ...CODE,
              fontSize: "0.92em", fontWeight: 500, color: P.code,
              background: P.chip, border: `1px solid ${P.border}`,
              borderRadius: 5, padding: "1px 5px",
            }}
          >
            {part.slice(1, -1)}
          </code>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}

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
      if (typeof s === "number" && s <= c.seq) { at = i; depth = e.row.depth; }
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
 *  JetBrains Mono character cell (advance width is exactly 0.6em) and painted in
 *  the console foreground, so it sits in the line like a real shell cursor
 *  rather than reading as a coloured decoration. */
function Caret() {
  return (
    <span
      aria-hidden
      style={{
        display: "inline-block", width: "0.6em", height: "1.05em",
        marginLeft: 1, verticalAlign: "text-bottom",
        background: P.text,
        animation: "wsCaret 1s steps(1,end) infinite",
      }}
    />
  );
}
/* ── Rows ──────────────────────────────────────────────────────── */

/**
 * The four strings a row is built from.
 *
 * `label` is the one line the row prints; `badge` is the identifier that moves
 * out of that line into a right-aligned chip, which is the point of the new
 * layout — a row used to read `grace · send_research_email` and truncate in the
 * middle of the tool name, so the one token a reader needs to retype was the
 * first thing the ellipsis ate. `detail` is the untruncated brief that the
 * expandable well shows. `full` is label+detail for the clipboard, unchanged, so
 * a pasted transcript still reads the way it always did.
 */
function rowLabel(row: TreeRow): {
  label: string; badge: string; detail: string; full: string;
} {
  const { step, sub } = row;
  const tool = step.tool && step.tool !== "task" ? step.tool : "";
  const label = sub?.subagent_type || step.detail || step.tool || "Working…";
  const badge = sub?.subagent_type ? tool || "task" : tool;
  const detail = sub?.description || (sub ? "" : step.tool && step.detail !== step.tool ? step.tool : "");
  const fullLabel = sub?.subagent_type ? `${sub.subagent_type}${tool ? ` · ${tool}` : ""}` : label;
  return { label, badge, detail, full: detail ? `${fullLabel} — ${detail}` : fullLabel };
}

/**
 * The timeline gutter: one glyph, plus the 1px connector running down to the
 * next row.
 *
 * The connector is drawn by the ROW rather than as one absolutely-positioned
 * line behind the list, because rows enter one at a time and at varying heights
 * (an open detail well is 100px tall). A single background line would have to be
 * measured and would lag every reveal by a frame; per-row segments cannot get out
 * of sync with the content they connect.
 */
function StepRail({
  kind, active, last,
}: { kind: string; active: boolean; last: boolean }) {
  const Icon = KIND_ICON[kind as keyof typeof KIND_ICON] ?? Terminal;
  const color = active ? P.accent : KIND_COLOR[kind] ?? P.dim;
  return (
    <div
      style={{
        display: "flex", flexDirection: "column", alignItems: "center",
        flexShrink: 0, width: 16, alignSelf: "stretch",
      }}
    >
      <Icon size={13} color={color} strokeWidth={1.9} style={{ flexShrink: 0, marginTop: 3 }} />
      {!last && <span aria-hidden style={{ flex: 1, width: 1, minHeight: 4, marginTop: 3, background: P.rail }} />}
    </div>
  );
}
/** A right-aligned identifier chip — tool name, specialist name, count. */
function Chip({ text, tone = P.dim }: { text: string; tone?: string }) {
  return (
    <span
      className="ws-chip"
      style={{
        ...CODE, fontSize: 10.5, color: tone, background: P.chip,
        border: `1px solid ${P.border}`, borderRadius: 5, padding: "1px 5px",
        flexShrink: 0, maxWidth: 150, overflow: "hidden",
        textOverflow: "ellipsis", whiteSpace: "nowrap",
      }}
    >
      {text}
    </span>
  );
}

function ActivityRow({
  row, active, mode, rush, last, onDone,
}: {
  row: TreeRow;
  active: boolean;
  mode: RevealMode;
  rush: number;
  /** Suppresses the connector, so the rail ends with the last row instead of
   *  trailing a stub into empty field. */
  last: boolean;
  onDone: (key: string) => void;
}) {
  const { step, depth, sub } = row;
  const kind = classify(step);
  const { label, badge, detail, full } = rowLabel(row);
  const blank = sub?.status === "blank";
  const key = `r:${row.key}`;
  const [open, setOpen] = useState(false);

  const done = useCallback(() => onDone(key), [onDone, key]);
  /* Typed as ONE string so the reveal runs continuously across the label and its
     detail even though only the label is on the row — the gate's timing budget
     stays proportional to how much this row actually has to say. */
  const { shown, typing } = useTypewriter(full, mode === "typing", (SPEED[kind] ?? 30) / rush, done);
  const shownLabel = shown.slice(0, label.length);
  const canOpen = Boolean(detail) && !typing;

  // Not yet reached by the gate. Nothing is rendered — not even an empty line —
  // so the rail grows downward the way console output does.
  if (mode === "hidden") return null;
  return (
    <div
      style={{
        display: "flex", gap: 9,
        paddingLeft: `calc(var(--ws-indent) * ${depth})`,
        animation: "wsRise .22s ease both",
      }}
    >
      <StepRail kind={kind} active={active} last={last && !open} />
      <div style={{ flex: 1, minWidth: 0, paddingBottom: 5 }}>
        <div
          onClick={(e) => { if (canOpen) { e.stopPropagation(); setOpen((o) => !o); } }}
          role={canOpen ? "button" : undefined}
          aria-expanded={canOpen ? open : undefined}
          style={{
            display: "flex", alignItems: "center", gap: 7,
            cursor: canOpen ? "pointer" : "default",
          }}
        >
          <span
            className="ws-line"
            style={{
              ...CODE,
              flex: 1, minWidth: 0, fontSize: 12, lineHeight: 1.55,
              color: active ? P.strong : P.text,
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              transition: "color .3s",
            }}
          >
            {shownLabel}
            {/* The cursor stays on the active row after its text finishes: a shell
                leaves the block cursor blinking at the prompt while the command
                runs, and that is exactly this row's state. */}
            {(typing || active) && <Caret />}
          </span>
          {blank && (
            <span
              style={{ ...CODE, fontSize: 10.5, color: P.amber, flexShrink: 0 }}
              title="The specialist returned an empty result"
            >
              blank
            </span>
          )}
          {badge && !typing && <Chip text={badge} />}
          {!active && step.done && (
            <Check size={12} color={P.green} strokeWidth={2.6} style={{ flexShrink: 0 }} />
          )}
          {canOpen && (
            <ChevronRight
              size={12} color={P.faint} strokeWidth={2}
              style={{ flexShrink: 0, transition: "transform .18s", transform: open ? "rotate(90deg)" : "none" }}
            />
          )}
        </div>
        {/* The well. This is the one place the untruncated brief exists: a
            delegation's `description` is a paragraph, and the row is a single
            ellipsised line, so without this the instruction IRIS actually gave a
            specialist was unreadable anywhere in the UI. Inert text, never
            markdown — a brief can quote fetched content. */}
        {open && detail && (
          <div
            style={{
              ...CODE,
              marginTop: 5, padding: "7px 9px", background: P.well,
              border: `1px solid ${P.border}`, borderRadius: 7,
              fontSize: 11.5, lineHeight: 1.6, color: P.dim,
              whiteSpace: "pre-wrap", maxHeight: 160, overflowY: "auto",
              animation: "wsRise .18s ease both",
            }}
          >
            {detail}
          </div>
        )}
      </div>
    </div>
  );
}
/**
 * One self-correcting guardrail, shown where in the run it fired.
 *
 * These rows are the harness steering IRIS mid-run: the blank-response and
 * empty-completion recoveries, the loop breaker, the tool-call repair and todo
 * reconcile. They used to render as a shell comment — `# Caught an empty response
 * and kept going` — which filed IRIS correcting itself in the visual register of
 * a throwaway remark, indistinguishable at a glance from a dim command line.
 *
 * They are the opposite of a throwaway. Each one is a guardrail catching IRIS
 * mid-mistake and putting the run back on course, and that is the single most
 * reassuring thing a person watching a ten-minute orchestration can see. So a
 * guardrail is a banded row, tagged for what it is, carrying its own explanation
 * as well as the verbatim steering text.
 *
 * The tag splits on the backend's own definition of severity
 * (guardrail_taxonomy.py), not on a new one invented here:
 *
 *   • `warn` — a guard that CAUGHT something wrong (an empty answer, a loop, a
 *     vague final answer) ⇒ `SELF-CORRECTION`, in amber.
 *   • `info` — a forward-looking guard that steered IRIS before it went wrong
 *     ⇒ `GUARDRAIL`, in the accent.
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
  const color = caught ? P.amber : P.accent;
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
        paddingLeft: `calc(var(--ws-indent) * ${depth} + 25px)`,
        margin: "1px 0 6px",
        animation: "wsRise .22s ease both",
      }}
    >
      {/* The band is what separates a guardrail from the command lines around it:
          rail in the severity colour, field one step darker than the card. One
          surface treatment for "this is commentary on the run". */}
      <div
        style={{
          background: P.well, borderLeft: `2px solid ${color}`,
          border: `1px solid ${P.border}`, borderLeftWidth: 2, borderLeftColor: color,
          borderRadius: "0 7px 7px 0",
        }}
      >
        <button
          onClick={(e) => { e.stopPropagation(); if (canOpen) setOpen((o) => !o); }}
          aria-expanded={canOpen ? open : undefined}
          style={{
            display: "flex", alignItems: "center", gap: 7, width: "100%",
            background: "transparent", border: "none", padding: "4px 8px",
            cursor: canOpen ? "pointer" : "default", textAlign: "left",
            fontFamily: "inherit", color: "inherit",
          }}
        >
          <ShieldCheck size={13} color={color} strokeWidth={1.9} style={{ flexShrink: 0 }} />
          <span
            className="ws-tag"
            title={
              caught
                ? "A guardrail caught a problem in IRIS's own output and put the run back on course."
                : "A guardrail steered IRIS before it went wrong."
            }
            style={{
              ...CODE, fontSize: 10, fontWeight: 700, letterSpacing: "0.05em",
              color, flexShrink: 0, whiteSpace: "nowrap",
            }}
          >
            {caught ? "[SELF-CORRECTION]" : "[GUARDRAIL]"}
          </span>
          <span
            className="ws-line"
            style={{
              ...CODE, flex: 1, minWidth: 0, fontSize: 12, lineHeight: 1.55,
              color: P.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}
          >
            <InlineText text={shown} />
            {typing && <Caret />}
          </span>
          {c.persisted === false && (
            <span
              style={{ ...CODE, fontSize: 10.5, color: P.faint, flexShrink: 0 }}
              title="Request-only guardrail — steered this model call but was never written to thread state"
            >
              live only
            </span>
          )}
          {canOpen && (
            <ChevronRight
              size={12} color={P.faint} strokeWidth={2}
              style={{ flexShrink: 0, transition: "transform .18s", transform: open ? "rotate(90deg)" : "none" }}
            />
          )}
        </button>

        {open && (
          <div style={{ padding: "0 9px 8px 8px" }}>
            {why && (
              <div style={{ ...CODE, fontSize: 11.5, lineHeight: 1.6, color: P.dim }}>
                <InlineText text={why} />
              </div>
            )}
            {hasRaw && (
              <>
                <div
                  style={{
                    ...CODE, fontSize: 9.5, fontWeight: 700, letterSpacing: "0.06em",
                    textTransform: "uppercase", color: P.faint, margin: "7px 0 3px",
                  }}
                >
                  verbatim steering text
                </div>
                {/* Rendered as INERT pre-wrap text on purpose. A guardrail can
                    quote third-party content (an email body, a fetched page), so
                    it is never passed through the markdown renderer. */}
                <div
                  style={{
                    ...CODE, fontSize: 11.5, lineHeight: 1.6, color: P.dim,
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
function Section({
  title, badge, children,
}: { title: string; badge?: string; children: React.ReactNode }) {
  return (
    <div style={{ padding: "6px 0" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 5 }}>
        <span
          style={{
            ...CODE, fontSize: 9.5, fontWeight: 700, letterSpacing: "0.08em",
            textTransform: "uppercase", color: P.faint,
          }}
        >
          {title}
        </span>
        {badge && (
          <span style={{ ...CODE, fontSize: 10, fontWeight: 600, color: P.dim }}>{badge}</span>
        )}
        <span style={{ flex: 1, height: 1, background: P.border }} />
      </div>
      {children}
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
          style={{
            ...CODE, display: "flex", alignItems: "flex-start", gap: 8,
            padding: "2px 0", fontSize: 12, lineHeight: 1.5,
          }}
        >
          <span
            style={{
              flexShrink: 0, marginTop: 3, width: 14, height: 14,
              display: "inline-flex", alignItems: "center", justifyContent: "center",
            }}
          >
            {t.status === "completed" ? (
              <Check size={12.5} color={P.green} strokeWidth={2.8} />
            ) : t.status === "in_progress" ? (
              <svg
                width="12.5" height="12.5" viewBox="0 0 24 24" fill="none" stroke={P.accent}
                strokeWidth="2.6" strokeLinecap="round" style={{ animation: "wsSpin .9s linear infinite" }}
              >
                <path d="M21 12a9 9 0 1 1-6.219-8.56" />
              </svg>
            ) : (
              <Circle size={11} color={P.faint} strokeWidth={1.8} />
            )}
          </span>
          <span
            style={{
              ...CODE,
              color: t.status === "completed" ? P.faint : P.text,
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

/* ── Duration ──────────────────────────────────────────────────── */

/** "Worked for 1 second" / "Worked for 47 seconds". Never "0 seconds". */
function workedLabel(ms: number): string {
  const s = Math.max(1, Math.round(ms / 1000));
  return `Worked for ${s} second${s === 1 ? "" : "s"}`;
}
/** The header pill for an end that was not a clean completion. Header row, so
 *  these are theme tokens, not console hex. */
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

  /** Skip the animation for everything on screen. Clicking the card is the
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

  /** Index of the last entry the gate has actually rendered, so `StepRail` knows
   *  which row ends the timeline. Without this the connector trails off the
   *  bottom into hidden rows that are not on screen yet. */
  const lastShown = useMemo(() => {
    for (let i = view.length - 1; i >= 0; i--) if (view[i].mode !== "hidden") return i;
    return -1;
  }, [view]);

  /* ── Full transcript ──
     The card's height is FIXED and its content scrolls, so this toggle is not
     "show me the rest" — everything is already reachable by scrolling. It buys
     two things: a taller viewport, and IRIS's own prose, which is the one part of
     the record that is paragraphs rather than lines and would otherwise push
     every execution row off the visible field. */
  const [full, setFull] = useState(false);
  const hasNotes = Boolean(transcript.trim());
  /* Pin the scroller to the newest line, unless the user has scrolled up to read.
     A ResizeObserver on the content rather than an effect on `stream`: rows grow
     as they type, so height changes between events too.

     `atBottom` drives the scroll-fade affordance. It is state, not a ref, because
     unlike `stick` it has to repaint a control — and it is only ever set from a
     scroll or resize callback, so it cannot loop. */
  const railRef = useRef<HTMLDivElement | null>(null);
  const innerRef = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const measure = useCallback(() => {
    const el = railRef.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 28;
    stick.current = near;
    setAtBottom((prev) => (prev === near ? prev : near));
  }, []);
  useEffect(() => {
    const rail = railRef.current;
    const inner = innerRef.current;
    if (!rail || !inner || typeof ResizeObserver === "undefined") return;
    const pin = () => {
      if (stick.current) rail.scrollTop = rail.scrollHeight;
      measure();
    };
    pin();
    const ro = new ResizeObserver(pin);
    ro.observe(inner);
    /* The card itself resizes too — the viewport narrows, or the `full` toggle
       changes its height — and either changes whether the content overflows. */
    ro.observe(rail);
    return () => ro.disconnect();
  }, [collapsed, full, measure]);

  const toBottom = useCallback(() => {
    const el = railRef.current;
    if (!el) return;
    stick.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: reduced ? "auto" : "smooth" });
  }, [reduced]);
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

  /** The closing row: what the run amounted to, checked off. Only for a clean
   *  completion — an aborted or errored run already says so in the header pill,
   *  and a green check under it would contradict that. */
  const showSummary = !running && !paused && !pill && summaryBits.length > 0;

  /** The pulse. Only when the gate has caught up and IRIS has not yet said what
   *  she is doing next — which is the one moment the panel has nothing to show
   *  and a person starts wondering whether it is stuck. */
  const showWorking = running && backlog === 0 && !activeKey;
  /* Viewport-aware, not just pixel-aware. The card is a fixed-height scroller by
     design, but 380px of fixed height is most of a phone in landscape — so the
     ceiling is whichever is smaller, the design height or a fraction of the
     viewport. `min()` rather than a media query because there is nothing discrete
     about it: the card should shrink smoothly, and it still transitions cleanly
     from 0 when the chevron opens. */
  const bodyMax = full ? `min(${CARD_MAX_FULL}px, 78vh)` : `min(${CARD_MAX}px, 62vh)`;

  return (
    <div
      data-iris-ws
      style={{
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
      {/* Everything responsive lives here rather than in the inline styles above,
          because a media query cannot reach a `style={{}}` attribute. The nesting
          step is a CSS variable for the same reason in reverse: the rows compute
          `calc(var(--ws-indent) * depth)` inline, so one query re-tunes the
          indentation of the whole tree without React re-rendering. */}
      <style>{`
        [data-iris-ws] { --ws-indent: 15px; }
        @keyframes wsSpin   { from { transform:rotate(0deg); } to { transform:rotate(360deg); } }
        @keyframes wsFadeUp { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:translateY(0); } }
        @keyframes wsRise   { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:translateY(0); } }
        @keyframes wsCaret  { 0%,49% { opacity:.85; } 50%,100% { opacity:0; } }
        @keyframes wsPulse  { 0%,100% { opacity:.35; } 50% { opacity:1; } }
        [data-iris-ws] .ws-rail::-webkit-scrollbar { width: 8px; height: 8px; }
        [data-iris-ws] .ws-rail::-webkit-scrollbar-track { background: transparent; }
        [data-iris-ws] .ws-rail::-webkit-scrollbar-thumb {
          background: ${P.borderHover}; border-radius: 8px; border: 2px solid transparent;
          background-clip: content-box;
        }
        [data-iris-ws] .ws-rail { scrollbar-width: thin; scrollbar-color: ${P.borderHover} transparent; }
        [data-iris-ws] .ws-ghost { opacity: .55; transition: opacity .18s; }
        [data-iris-ws] .ws-ghost:hover { opacity: 1; }
        [data-iris-ws] .ws-card { padding: 16px 16px 12px; }
        [data-iris-ws] .ws-notes { overflow-wrap: anywhere; }
        /* ≤560px — a phone, or the chat pane docked beside something else. The
           nesting step halves (a 4-deep delegation tree costs 60px of gutter at
           the desktop step, which on a 360px screen is a sixth of the line), the
           identifier chips give up width before the label does, and the card's
           padding stops being generous. */
        @media (max-width: 560px) {
          [data-iris-ws] { --ws-indent: 9px; }
          [data-iris-ws] .ws-card { padding: 12px 11px 10px; }
          [data-iris-ws] .ws-line { font-size: 11.5px; }
          [data-iris-ws] .ws-chip { max-width: 92px; }
        }
        /* ≤400px — the narrowest real phone. The elapsed readout goes: the header
           already says "Working", and a ticking number is the least load-bearing
           thing competing for that line. */
        @media (max-width: 400px) {
          [data-iris-ws] { --ws-indent: 7px; }
          [data-iris-ws] .ws-card { padding: 10px 9px 9px; }
          [data-iris-ws] .ws-line { font-size: 11px; }
          [data-iris-ws] .ws-chip { max-width: 62px; }
          [data-iris-ws] .ws-elapsed { display: none; }
          [data-iris-ws] .ws-tag { font-size: 9px; letter-spacing: 0; }
        }
        @media (prefers-reduced-motion: reduce) {
          [data-iris-ws] *, [data-iris-ws] { animation: none !important; transition: none !important; }
        }
      `}</style>
      {/* ── Header — the row the panel collapses to, and the one part that is NOT
             console-dark. See the palette note: this is a control belonging to the
             message, so it stays on theme tokens and sits on the message's own
             surface. A div, not a button: the copy control is a real button and
             nesting one inside another is invalid HTML (and unreachable by
             keyboard). ── */}
      <div className="ws-head" style={{ display: "flex", alignItems: "center", gap: 8, padding: "2px 0 6px" }}>
        <button
          onClick={() => hasBody && setCollapsed((v) => !v)}
          aria-expanded={hasBody ? !collapsed : undefined}
          style={{
            display: "flex", alignItems: "center", gap: 8,
            flex: 1, minWidth: 0,
            background: "transparent", border: "none", padding: "3px 0",
            cursor: hasBody ? "pointer" : "default",
            userSelect: "none", textAlign: "left", fontFamily: "inherit",
          }}
        >
          {running ? (
            <svg
              width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={T.accent}
              strokeWidth="2.6" strokeLinecap="round"
              style={{ flexShrink: 0, animation: "wsSpin .9s linear infinite" }}
            >
              <path d="M21 12a9 9 0 1 1-6.219-8.56" />
            </svg>
          ) : pill ? (
            <Sparkles size={13} color={pill.color} strokeWidth={1.9} style={{ flexShrink: 0 }} />
          ) : (
            <CircleCheckBig size={13} color={T.green} strokeWidth={2.1} style={{ flexShrink: 0 }} />
          )}

          <span style={{ ...CODE, fontSize: 12.5, fontWeight: 500, color: T.text, flexShrink: 0 }}>
            {running ? "Working" : durationKnown ? workedLabel(elapsedMs) : "Execution record"}
          </span>

          {running && (
            <span className="ws-elapsed" style={{ ...CODE, fontSize: 11.5, color: T.muted, flexShrink: 0 }}>
              {(elapsedMs / 1000).toFixed(1)}s
            </span>
          )}
          {/* While collapsed, say what happened so the arrow is worth opening.
              Also the only detail a rebuilt record has to offer — it has no
              duration, so `summaryBits` carries the whole label there. */}
          {!running && collapsed && summaryBits.length > 0 && (
            <span
              style={{
                ...CODE, fontSize: 11.5, color: T.muted, flex: 1, minWidth: 0,
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              }}
            >
              {summaryBits.join(" · ")}
            </span>
          )}
          {running && activeLabel && (
            <span
              style={{
                ...CODE, fontSize: 11.5, color: T.muted, flex: 1, minWidth: 0,
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
                ...CODE, fontSize: 10.5, color: pill.color, background: "transparent",
                border: `1px solid ${pill.color}`, opacity: 0.85,
                borderRadius: 20, padding: "1px 8px", flexShrink: 0,
              }}
            >
              {pill.text}
            </span>
          )}

          {hasBody && (
            <ChevronDown
              size={13} color={T.dim} strokeWidth={2}
              style={{
                flexShrink: 0, transition: "transform .25s",
                transform: collapsed ? "rotate(-90deg)" : "none",
              }}
            />
          )}
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
            {copied ? <Check size={12.5} strokeWidth={2.6} /> : <Copy size={12.5} strokeWidth={1.9} />}
            {copied ? "Copied" : "Copy"}
          </button>
        )}
      </div>

      {/* ── Body — stays MOUNTED when collapsed, so reopening costs nothing ── */}
      {hasBody && (
        <div
          style={{
            maxHeight: collapsed ? 0 : `calc(${bodyMax} + 44px)`,
            overflow: "hidden",
            transition: "max-height .35s ease",
          }}
        >
          {/* The console card. Fixed field, own border, and a height that does not
              grow as rows land — the whole point of the design: the answer below
              must not be shoved down the page every time a step arrives. */}
          <div
            className="ws-card"
            onClick={running ? fastForward : hasNotes || !full ? () => setFull((v) => !v) : undefined}
            style={{
              background: P.card,
              border: `1px solid ${P.border}`,
              borderRadius: 12,
              position: "relative",
              cursor: running ? "default" : "pointer",
            }}
          >
            <div
              ref={railRef}
              className="ws-rail"
              onScroll={measure}
              style={{ maxHeight: bodyMax, overflowY: "auto", overflowX: "hidden", paddingRight: 4 }}
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
                    {view.map((v, i) =>
                      v.e.kind === "row" ? (
                        <ActivityRow
                          key={v.key}
                          row={v.e.row}
                          active={v.e.row.key === activeKey}
                          mode={v.mode}
                          rush={rush}
                          last={i === lastShown && !showWorking && !showSummary}
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

                    {showWorking && (
                      <div style={{ display: "flex", gap: 9, alignItems: "center", paddingTop: 1 }}>
                        <span style={{ width: 16, display: "flex", justifyContent: "center", flexShrink: 0 }}>
                          <CircleDot size={12} color={P.accent} strokeWidth={2} style={{ animation: "wsPulse 1.4s ease-in-out infinite" }} />
                        </span>
                        <span className="ws-line" style={{ ...CODE, fontSize: 12, color: P.dim, animation: "wsPulse 1.4s ease-in-out infinite" }}>
                          Working…
                        </span>
                      </div>
                    )}
                    {/* The closing beat: a green check and the run in one line.
                        This is the only row that summarises rather than reports,
                        so it is the only one allowed the green. */}
                    {showSummary && (
                      <div style={{ display: "flex", gap: 9, alignItems: "flex-start", paddingTop: 2 }}>
                        <span style={{ width: 16, display: "flex", justifyContent: "center", flexShrink: 0, marginTop: 2 }}>
                          <CircleCheckBig size={13} color={P.green} strokeWidth={2.1} />
                        </span>
                        <div style={{ minWidth: 0 }}>
                          <div className="ws-line" style={{ ...CODE, fontSize: 12, lineHeight: 1.55, color: P.strong, fontWeight: 500 }}>
                            Done
                          </div>
                          <div style={{ ...CODE, fontSize: 11, lineHeight: 1.5, color: P.faint, marginTop: 1 }}>
                            {summaryBits.join(" · ")}
                            {durationKnown ? ` · ${(elapsedMs / 1000).toFixed(1)}s` : ""}
                          </div>
                        </div>
                      </div>
                    )}
                  </Section>
                )}

                {/* IRIS's own prose during the run — the intent log and any interim
                    narration. Behind the toggle because it is paragraphs, and at 380px
                    of field a paragraph pushes every execution row out of view.
                    Inert pre-wrap text, never markdown: it can quote tool output
                    that carries injected instructions. */}
                {hasNotes && full && (
                  <Section title="Notes">
                    <div
                      className="ws-notes"
                      style={{
                        ...CODE, fontSize: 11.5, lineHeight: 1.65, color: P.dim,
                        whiteSpace: "pre-wrap", padding: "2px 0",
                      }}
                    >
                      {transcript}
                    </div>
                  </Section>
                )}
                {canRecover && (
                  <div style={{ paddingTop: 10, borderTop: `1px solid ${P.border}`, marginTop: 6 }}>
                    <div style={{ ...CODE, fontSize: 11.5, color: P.dim, marginBottom: 7, lineHeight: 1.55 }}>
                      The run stopped early, but IRIS may have already saved an answer.
                    </div>
                    <button
                      onClick={(e) => { e.stopPropagation(); onRecover?.(); }}
                      style={{
                        ...CODE,
                        padding: "5px 12px", borderRadius: 7, border: `1px solid ${P.accent}`,
                        background: "transparent", color: P.accent, fontSize: 11.5,
                        fontWeight: 600, cursor: "pointer",
                      }}
                    >
                      Recover answer
                    </button>
                  </div>
                )}
              </div>
            </div>

            {/* Scroll affordance. Only when there is something below the fold —
                a chevron floating over a card whose content already fits reads as
                a broken control. Sits over a gradient so it never lands on top of
                a line of text and becomes unreadable. */}
            {!atBottom && (
              <button
                onClick={(e) => { e.stopPropagation(); toBottom(); }}
                title="Jump to the newest line"
                aria-label="Jump to the newest line"
                style={{
                  position: "absolute", left: 0, right: 0, bottom: 0, height: 34,
                  display: "flex", alignItems: "flex-end", justifyContent: "center",
                  paddingBottom: 4,
                  background: `linear-gradient(to bottom, transparent, ${P.card} 72%)`,
                  border: "none", borderRadius: "0 0 12px 12px", cursor: "pointer",
                }}
              >
                <ChevronDown size={15} color={P.dim} strokeWidth={2.2} />
              </button>
            )}
          </div>
          {/* Below the card, not inside it: a control that changes the card's own
              height should not itself be part of the region that scrolls. Hidden
              while running — the height is the last thing to fiddle with when the
              content is still arriving, and the card is click-to-fast-forward
              then. */}
          {!running && (
            <div style={{ display: "flex", justifyContent: "center", paddingTop: 6 }}>
              <button
                className="ws-ghost"
                onClick={() => setFull((v) => !v)}
                style={{
                  ...CODE,
                  display: "inline-flex", alignItems: "center", gap: 5,
                  background: "transparent", border: "none", padding: "2px 6px",
                  color: T.muted, cursor: "pointer", fontSize: 10.5,
                }}
              >
                {full ? "Show less" : hasNotes ? "Show full transcript" : "Show more"}
                <ChevronDown
                  size={12} strokeWidth={2}
                  style={{ transition: "transform .2s", transform: full ? "rotate(180deg)" : "none" }}
                />
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
