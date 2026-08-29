"""backfill_thread_index.py — give already-existing conversations an index entry.

``thread_index.record_thread`` runs on every ``/ask``, so from now on a thread
indexes itself. Conversations that existed BEFORE the index did have no entry, and
nothing would ever create one unless the user happened to open that thread again —
which they cannot do, because finding it is exactly what the index is for. This
script closes that loop once.

It walks the checkpointer with ``alist()``: a full scan of a table that is already
282 MB on the dev box. That is why it is a one-off script and not a boot step —
run it manually after deploying the index, never on startup.

Only threads keyed ``web:{user_id}:{thread_id}`` are considered; Slack threads
(``slack-*``) are not shown in the web sidebar and are skipped. Existing index
entries are left alone, so re-running is safe and idempotent.

Run (local):
    project_venv/Scripts/python.exe backfill_thread_index.py --dry-run
    project_venv/Scripts/python.exe backfill_thread_index.py

Run (Railway, one-off shell) — the env is already loaded there:
    python backfill_thread_index.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import thread_index as ti  # noqa: E402  (must follow load_dotenv)
from agent_memory import build_async_store, close_async_store  # noqa: E402
from checkpointer import build_async_checkpointer, close_async_checkpointer  # noqa: E402

logging.basicConfig(level="INFO", format="%(message)s")
logger = logging.getLogger("backfill")


def _split_key(thread_key: str) -> tuple[str, str] | None:
    """``web:{user_id}:{thread_id}`` → ``(user_id, thread_id)``, else None.

    Split from the RIGHT once, because a user_id is an email and emails may contain
    the separator in principle; the thread_id never does (``_THREAD_ID_RE`` in
    web_api.py restricts it to ``[A-Za-z0-9_-]``).
    """
    if not thread_key.startswith("web:"):
        return None
    rest = thread_key[4:]
    user_id, sep, thread_id = rest.rpartition(":")
    if not sep or not user_id or not thread_id:
        return None
    return user_id, thread_id


def _first_human_text(checkpoint: Any) -> str:
    """The text of a thread's first human turn, for the title."""
    try:
        msgs = (checkpoint.get("channel_values") or {}).get("messages") or []
    except Exception:  # noqa: BLE001
        return ""
    for m in msgs:
        mtype = getattr(m, "type", None) or (m.get("type") if isinstance(m, dict) else None)
        name = getattr(m, "name", None) or (m.get("name") if isinstance(m, dict) else None)
        if mtype not in ("human", "user"):
            continue
        # A NAMED HumanMessage is a harness nudge (blank_recovery, todo_reconcile,
        # tool_call_repair …), not something the user typed — titling a thread with
        # "[SELF-CORRECTION] …" would be worse than leaving it untitled.
        if name:
            continue
        content = getattr(m, "content", None)
        if content is None and isinstance(m, dict):
            content = m.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):  # multimodal turn: first text part
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
                    return part["text"]
    return ""


async def main(dry_run: bool, limit: int | None) -> int:
    saver = await build_async_checkpointer()
    store = await build_async_store()
    try:
        # thread_key -> raw first-human text ("" = thread seen, still untitled).
        #
        # A checkpoint's ``channel_values`` carries only the channels WRITTEN at that
        # super-step, not the whole state — measured: the newest checkpoint of a live
        # thread typically holds just ``{__pregel_tasks, memory_contents,
        # skills_metadata, todos}`` and no ``messages`` at all, and the first one that
        # does can be 50 checkpoints back. So we cannot take the first checkpoint per
        # thread; we keep scanning that thread until one yields a human turn.
        #
        # Any checkpoint that DOES carry ``messages`` carries the whole list, whose
        # first human entry is the thread's first human turn — so whichever we hit
        # first (alist is newest-first) gives the same title.
        found: dict[str, str] = {}
        scanned = 0
        async for tup in saver.alist(None, limit=limit):
            scanned += 1
            key = ((tup.config or {}).get("configurable") or {}).get("thread_id") or ""
            if not key or _split_key(key) is None:
                continue
            if found.get(key):  # already titled — nothing left to learn
                continue
            found[key] = _first_human_text(tup.checkpoint) or found.get(key, "")

        untitled = sum(1 for v in found.values() if not v)
        logger.info(
            "scanned %d checkpoints → %d web threads (%d with no recoverable title)",
            scanned, len(found), untitled,
        )

        wrote = skipped = 0
        for key, raw in sorted(found.items()):
            parsed = _split_key(key)
            if parsed is None:
                continue
            user_id, thread_id = parsed
            title = ti.derive_title(raw)
            ns = ti.thread_namespace(user_id)
            existing = None
            try:
                existing = await store.aget(ns, thread_id)
            except Exception:  # noqa: BLE001
                pass
            if existing is not None:
                skipped += 1
                continue
            # Log the thread id and the title only — never the user_id, which is an
            # email address.
            logger.info("  %s %s — %s", "would index" if dry_run else "indexing", thread_id, title[:70])
            if not dry_run:
                await ti.record_thread(store, user_id, thread_id, title=title)
            wrote += 1

        verb = "would write" if dry_run else "wrote"
        logger.info("%s %d entries, skipped %d already indexed", verb, wrote, skipped)
        return 0
    finally:
        await close_async_store()
        await close_async_checkpointer()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report what would be written, write nothing")
    ap.add_argument(
        "--limit", type=int, default=None,
        help="stop after N checkpoints (smoke test only — a truncated scan can miss the "
             "checkpoint that carries a thread's messages, so titles degrade to "
             "'New conversation')",
    )
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.dry_run, args.limit)))
