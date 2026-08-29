"""thread_index.py — the server-side list of a user's conversations.

Why this exists
---------------
Every message of every thread has always been durable: the checkpointer holds it
under ``web:{email}:{uuid}``. What was missing was any way to *enumerate* those
threads. The sidebar was built entirely from ``localStorage["iris_threads_{email}"]``,
so a conversation was reachable only through a ``crypto.randomUUID()`` that lived
in one browser profile. Clear site data, switch device, open a private window, or
sign in from a different origin and every past conversation became unreachable —
still stored, still intact, simply unaddressable. That is what users experienced
as "IRIS has no chat history".

Why the LangGraph store rather than a table
-------------------------------------------
``agent_memory.build_async_store()`` already gives us durable Supabase Postgres,
per-user namespacing, and indexed namespace lookup, with a sync SQLite fallback
for local dev. A dedicated ``iris_threads`` SQL table would mean a schema, a
migration, and a third database access pattern for a payload of four fields. The
alternative — scanning ``checkpointer.alist()`` — is a full walk of a table that
is already 282 MB locally, carries no titles, and gets slower every week; it is
right for the one-off backfill and wrong for the read path.

Namespace is ``("threads", _safe_label(user_id))``. ``_safe_label`` is mandatory,
not cosmetic: LangGraph rejects namespace labels containing ``.``, and every email
domain has one — passing a raw email here raises ``InvalidNamespaceError`` and
would break per-user isolation. See ``agent_memory._safe_label``.

Every function here is best-effort by design. The index is a convenience layer
over the checkpointer, which remains the source of truth for content, so an index
write that fails must never fail the user's message.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agent_memory import _safe_label

logger = logging.getLogger(__name__)

# Longest title we keep. The UI truncates for display anyway; this bounds what we
# write so a pasted wall of text cannot bloat every row of the index.
_TITLE_MAX = 120

# Hard ceiling on a single list_threads read, whatever the caller asks for.
_LIST_CAP = 500


def thread_namespace(user_id: str) -> tuple[str, ...]:
    """The store namespace holding one user's thread index."""
    return ("threads", _safe_label(user_id))


def derive_title(text: str) -> str:
    """A thread title from its first user message.

    First non-blank line, collapsed and clipped. Deliberately dumb: the title is
    a recognition aid in a sidebar, and an LLM call to name a thread would add
    latency and cost to the first message of every conversation.
    """
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if line:
            return line[:_TITLE_MAX] if len(line) <= _TITLE_MAX else line[: _TITLE_MAX - 1] + "…"
    return "New conversation"


async def record_thread(
    store: Any,
    user_id: str,
    thread_id: str,
    *,
    title: str | None = None,
) -> None:
    """Upsert one thread into the user's index. Never raises.

    Called on every ``/ask``, not only the first: an existing entry keeps its
    ``created_at`` and its original ``title`` and only moves ``updated_at``, so
    the sidebar orders by recency without the title churning under the user as the
    conversation goes on. Writing on every turn also means a thread that predates
    the index self-heals the moment it is used again — the backfill script exists
    for threads that never are.
    """
    if store is None or not user_id or not thread_id:
        return
    ns = thread_namespace(user_id)
    now = time.time()
    try:
        existing = await store.aget(ns, thread_id)
        prev = (getattr(existing, "value", None) or {}) if existing is not None else {}
        record = {
            "thread_id": thread_id,
            "title": prev.get("title") or title or "New conversation",
            "created_at": prev.get("created_at") or now,
            "updated_at": now,
        }
        await store.aput(ns, thread_id, record)
    except Exception:  # noqa: BLE001 — the index must never break a conversation
        logger.warning("thread_index: record failed thread=%s", thread_id, exc_info=True)


async def list_threads(
    store: Any,
    user_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """A user's threads, most recently used first. Never raises; [] on failure.

    ``asearch`` has no ordering guarantee, so recency is applied here after the
    read. The cap keeps that in-process sort bounded.
    """
    if store is None or not user_id:
        return []
    ns = thread_namespace(user_id)
    try:
        items = await store.asearch(ns, limit=max(1, min(limit, _LIST_CAP)), offset=max(0, offset))
    except Exception:  # noqa: BLE001
        logger.warning("thread_index: list failed user=%s", _safe_label(user_id), exc_info=True)
        return []
    out: list[dict] = []
    for item in items or []:
        val = getattr(item, "value", None) or {}
        tid = val.get("thread_id") or getattr(item, "key", None)
        if not tid:
            continue
        out.append({
            "thread_id": tid,
            "title": val.get("title") or "New conversation",
            "created_at": val.get("created_at"),
            "updated_at": val.get("updated_at") or val.get("created_at") or 0,
        })
    out.sort(key=lambda r: r.get("updated_at") or 0, reverse=True)
    return out


async def delete_thread(store: Any, user_id: str, thread_id: str) -> bool:
    """Drop one thread from the index. Returns False if the delete failed.

    Index-only: the checkpoint is destroyed by the caller (``web_api``), because
    that is an irreversible content deletion and belongs where it can be reported
    to the user, not buried in a convenience layer.
    """
    if store is None or not user_id or not thread_id:
        return False
    try:
        await store.adelete(thread_namespace(user_id), thread_id)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("thread_index: delete failed thread=%s", thread_id, exc_info=True)
        return False
