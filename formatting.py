"""
formatting.py
=============
Per-surface output formatting adapters for IRIS.

The problem this solves: the model authors content in **GitHub-flavored
Markdown** (``**bold**``, ``## heading``, ``[label](url)``, ``- bullet``,
tables) out of habit — but every delivery surface speaks a *different*
formatting dialect, and none of them is GitHub Markdown:

  • Slack    → mrkdwn (``*bold*``, ``_italic_``, ``<url|label>``) + Block Kit
  • Calendar → a small HTML subset in the event ``description`` field
  • Docs     → full HTML (rendered via Drive → Google Doc conversion)

Feeding GitHub Markdown to any of them leaks literal ``**``/``##``/``[](...)``
symbols into the rendered output (the "asterisk soup"). This module converts
the model's Markdown into each surface's real dialect at the tool boundary, so
the model keeps writing the one format it is good at and the tool guarantees
valid, professional output.

HARDENING CONTRACT — every public function here is *total*: it is wrapped so
that on ANY internal error it falls back to a safe value (the original string,
or an empty block list) rather than raising. A formatting layer must never
become a new failure mode for a send/create tool. Conversions are also
idempotent on already-valid mrkdwn (e.g. an existing ``*bold*`` is left alone),
so routing a message through twice does no harm.
"""

from __future__ import annotations

import html as _html
import logging
import re
from typing import List

_log = logging.getLogger(__name__)

__all__ = [
    "to_slack_mrkdwn",
    "to_slack_blocks",
    "to_calendar_html",
    "to_docs_html",
]

# Sentinels used to park protected code spans while inline substitutions run.
# NUL-delimited so no Markdown/mrkdwn regex below can ever match them.
_PH_OPEN = "\x00"
_PH_CLOSE = "\x00"

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_LANG_HINT_RE = re.compile(r"```[ \t]*[A-Za-z0-9_+\-]+[ \t]*\r?\n")
_HR_RE = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*$")
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+")
_TABLE_SEP_RE = re.compile(r"^\|?[\s:|-]*-[\s:|-]*\|?$")


# ── shared helpers ────────────────────────────────────────────────────────────
def _protect_code(text: str) -> tuple[str, List[str]]:
    """Replace fenced + inline code with NUL placeholders so inline Markdown
    substitutions never touch code content. Returns (text, store)."""
    store: List[str] = []

    def _stash(match: re.Match) -> str:
        store.append(match.group(0))
        return f"{_PH_OPEN}{len(store) - 1}{_PH_CLOSE}"

    text = _FENCE_RE.sub(_stash, text)
    text = _INLINE_CODE_RE.sub(_stash, text)
    return text, store


def _restore_code(text: str, store: List[str]) -> str:
    for i, original in enumerate(store):
        text = text.replace(f"{_PH_OPEN}{i}{_PH_CLOSE}", original)
    return text


def _clean_table_lines(text: str) -> str:
    """Turn Markdown pipe-tables into clean readable lines (Slack/plain have no
    native table): drop the ``|---|`` separator row and strip the outer pipes,
    keeping an inner ``  ·  `` column separator."""
    out: List[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if "|" in stripped and _TABLE_SEP_RE.match(stripped):
            continue  # header/body separator row → drop
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            out.append("  ·  ".join(c for c in cells if c != ""))
        else:
            out.append(line)
    return "\n".join(out)


def _inline_to_mrkdwn(text: str) -> str:
    """Inline Markdown → Slack mrkdwn on a code-protected string.

    NOTE on the single-asterisk ambiguity: Slack uses ``*x*`` for BOLD while
    Markdown uses it for ITALIC. We deliberately do NOT rewrite single-star
    runs — only ``**x**``/``__x__`` → ``*x*``. This keeps the transform
    idempotent (an already-valid ``*bold*`` survives untouched) and never
    mangles pre-formatted mrkdwn; the only cost is that a Markdown ``*italic*``
    renders as Slack bold, which shows no stray symbols."""
    # ~~strike~~ → ~strike~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    # **bold** / __bold__ → *bold*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)
    # ![alt](url) → <url|alt>   (before links)
    text = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)",
        lambda m: f"<{m.group(2)}|{m.group(1) or m.group(2)}>",
        text,
    )
    # [label](url) → <url|label>
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)", r"<\2|\1>", text)
    return text


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _chunk(text: str, size: int) -> List[str]:
    """Split text into <=size chunks, preferring paragraph/line boundaries."""
    if len(text) <= size:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > size and current:
            chunks.append(current)
            current = ""
        # A single line longer than size is hard-split.
        while len(line) > size:
            chunks.append(line[:size])
            line = line[size:]
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


# ── Slack ─────────────────────────────────────────────────────────────────────
def to_slack_mrkdwn(text: str) -> str:
    """Convert GitHub-flavored Markdown → Slack **mrkdwn**.

    Total: on any error returns the input unchanged. Idempotent on valid mrkdwn.
    """
    if not text:
        return ""
    try:
        # Strip code-fence language hints (Slack shows them literally).
        text = _LANG_HINT_RE.sub("```\n", text)
        text, store = _protect_code(text)

        # Line-structure pass.
        text = _clean_table_lines(text)
        lines: List[str] = []
        for line in text.split("\n"):
            if _HR_RE.match(line):
                lines.append("──────────")
                continue
            heading = _HEADING_RE.match(line)
            if heading:
                lines.append(f"*{heading.group(1).strip()}*")
                continue
            lines.append(_BULLET_RE.sub(r"\1•  ", line))
        text = "\n".join(lines)

        # Inline pass.
        text = _inline_to_mrkdwn(text)

        text = _restore_code(text, store)
        return text
    except Exception:  # never let formatting break a send
        _log.warning("to_slack_mrkdwn fell back to raw text", exc_info=True)
        return text


def to_slack_blocks(text: str, header: str | None = None) -> List[dict]:
    """Build a Block Kit ``blocks`` array from Markdown for a professional,
    sectioned layout (header → sections → dividers).

    Returns [] on any error or empty input, so callers can safely do
    ``if blocks: payload["blocks"] = blocks`` and always keep a ``text``
    fallback for notifications/accessibility.
    """
    if not text or not text.strip():
        return []
    try:
        src = text.strip()
        blocks: List[dict] = []

        # Header: explicit arg wins; otherwise promote a leading ``# H1``.
        head = header.strip() if header and header.strip() else None
        if head is None:
            m = re.match(r"^#[ \t]+(.+)", src)
            if m:
                head = m.group(1).strip()
                src = src[m.end():].lstrip("\n")
        if head:
            blocks.append(
                {"type": "header",
                 "text": {"type": "plain_text", "text": _truncate(head, 150), "emoji": True}}
            )

        # Segment on blank lines; each segment becomes a section (or a divider).
        for segment in re.split(r"\n[ \t]*\n", src):
            seg = segment.strip("\n")
            if not seg.strip():
                continue
            if all(_HR_RE.match(ln) for ln in seg.split("\n") if ln.strip()):
                blocks.append({"type": "divider"})
                continue
            rendered = to_slack_mrkdwn(seg)
            if not rendered.strip():
                continue
            for chunk in _chunk(rendered, 2900):  # Slack section cap is 3000
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})

        # Slack hard-caps a message at 50 blocks.
        if len(blocks) > 50:
            blocks = blocks[:49] + [
                {"type": "section", "text": {"type": "mrkdwn", "text": "_… content truncated …_"}}
            ]
        return blocks
    except Exception:
        _log.warning("to_slack_blocks fell back to no blocks", exc_info=True)
        return []


# ── Google Calendar ───────────────────────────────────────────────────────────
_FENCE_LINE_RE = re.compile(r"^[ \t]*```")


def _cal_inline(escaped: str) -> str:
    """Inline Markdown → Calendar HTML subset on an ALREADY html-escaped string.

    The input must be pre-escaped so literal ``<``/``>``/``&`` in user text can
    never break the markup; the Markdown delimiters (``*``, ``[]()``) survive
    escaping untouched, so we insert the tags afterward.
    """
    escaped = re.sub(r"`([^`]+)`", r"\1", escaped)            # inline code → plain
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__(.+?)__", r"<b>\1</b>", escaped)
    escaped = re.sub(r"~~(.+?)~~", r"\1", escaped)            # strike unsupported → drop marks
    escaped = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)", r'<a href="\2">\1</a>', escaped)
    return escaped


def to_calendar_html(text: str) -> str:
    """Convert Markdown → the small HTML subset Google Calendar renders inside an
    event ``description`` (``<b>``, ``<i>``, ``<a>``, ``<ul>/<li>``, ``<br>``).

    Escapes literal HTML first so arbitrary user text can never break the markup.
    Total: returns the input unchanged on any error.
    """
    if not text:
        return ""
    try:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        buf: List[str] = []
        pending: List[str] = []  # accumulates consecutive bullet items into one <ul>

        def _flush_list() -> None:
            if pending:
                items = "".join(f"<li>{_cal_inline(_html.escape(i))}</li>" for i in pending)
                buf.append(f"<ul>{items}</ul>")
                pending.clear()

        for raw in lines:
            line = raw.rstrip()
            if _FENCE_LINE_RE.match(line):       # drop ``` code-fence markers
                continue
            if _HR_RE.match(line):
                _flush_list()
                buf.append("<br>")
                continue
            bullet = _BULLET_RE.match(line)
            if bullet:
                pending.append(line[bullet.end():])
                continue
            _flush_list()
            heading = _HEADING_RE.match(line)
            if heading:
                buf.append(f"<b>{_cal_inline(_html.escape(heading.group(1).strip()))}</b><br>")
            elif not line.strip():
                buf.append("<br>")
            else:
                buf.append(f"{_cal_inline(_html.escape(line))}<br>")
        _flush_list()

        html_out = "".join(buf)
        html_out = re.sub(r"(?:<br>\s*){3,}", "<br><br>", html_out)  # collapse big gaps
        html_out = re.sub(r"(?:<br>\s*)+$", "", html_out)            # trim trailing breaks
        return html_out.strip()
    except Exception:
        _log.warning("to_calendar_html fell back to raw text", exc_info=True)
        return text


# ── Google Docs ───────────────────────────────────────────────────────────────
# Drive's importer accepts full HTML, so we render Markdown with the highest
# fidelity available: markdown-it-py (CommonMark + GFM tables + strikethrough).
# A dependency-free fallback keeps to_docs_html total if the lib is ever absent.
try:
    from markdown_it import MarkdownIt as _MarkdownIt

    _MD = _MarkdownIt("commonmark").enable(["table", "strikethrough"])
except Exception:  # pragma: no cover — dependency insurance
    _MD = None

_FALLBACK_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$")


def _basic_md_to_html(text: str) -> str:
    """Minimal, dependency-free Markdown → HTML (no tables). Used only if
    markdown-it-py is unavailable at runtime."""

    def _inline(raw: str) -> str:
        s = _html.escape(raw)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"__(.+?)__", r"<strong>\1</strong>", s)
        s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
        s = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)", r'<a href="\2">\1</a>', s)
        return s

    out: List[str] = []
    pending: List[str] = []

    def _flush() -> None:
        if pending:
            out.append("<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in pending) + "</ul>")
            pending.clear()

    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        bullet = _BULLET_RE.match(line)
        if bullet:
            pending.append(line[bullet.end():])
            continue
        _flush()
        heading = _FALLBACK_HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
    _flush()
    return "\n".join(out)


def to_docs_html(text: str) -> str:
    """Convert Markdown → a standalone HTML document for Drive → Google Doc
    conversion (headings, tables, lists, bold/italic, links, code, quotes).

    Total: on any render error it falls back first to the basic converter, then
    to an escaped ``<pre>`` block, so document content is never lost.
    """
    src = text or ""
    try:
        body = _MD.render(src) if _MD is not None else _basic_md_to_html(src)
    except Exception:
        _log.warning("to_docs_html: primary render failed, using basic fallback", exc_info=True)
        try:
            body = _basic_md_to_html(src)
        except Exception:
            body = f"<pre>{_html.escape(src)}</pre>"
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        f"<body>{body}</body></html>"
    )
