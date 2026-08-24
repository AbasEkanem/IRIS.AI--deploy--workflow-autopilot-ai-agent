"""
google_docs_tools.py
=====================
Google Docs authoring for IRIS.AI (Grace Subagent).

Grace previously had no way to *create* a Google Doc — so when asked to "write a
document" IRIS fell back to dumping a Markdown file into ``tmp/``. These tools
close that gap by creating real Google Docs the user can open, share and edit.

Implementation note — why Drive, not the Docs API:
    Google Docs can be authored two ways: the native Docs API
    (``documents.batchUpdate``) or by uploading HTML to Drive and letting Drive
    *convert* it to a Google Doc on import. We use the Drive route deliberately:
      • The ``drive`` scope is already authorized on the refresh token, so this
        needs NO OAuth re-mint (the native Docs API needs the extra ``documents``
        scope — see the OAuth-scope note in project memory).
      • Drive's HTML importer gives high-fidelity conversion (headings, tables,
        lists, bold/italic, links, blockquotes) for free — exactly what
        ``formatting.to_docs_html`` produces from the model's Markdown.

    The model writes ordinary Markdown; ``to_docs_html`` converts it to a
    standalone HTML document at the tool boundary; Drive turns that into a
    proper Google Doc. This mirrors the per-surface adapter pattern already used
    for Slack (mrkdwn) and Calendar (HTML-subset).

Tools:
    • create_google_doc(title, content_markdown, folder_id=None)
    • read_google_doc(document_id)
"""

from __future__ import annotations

import io
import logging
from typing import Optional

from googleapiclient.http import MediaIoBaseUpload
from langchain_core.tools import tool

from google_auth import get_service, execute_with_retry
from formatting import to_docs_html
from idempotency import idempotent

_log = logging.getLogger(__name__)

# Google-native Doc MIME type: setting this as the created file's mimeType while
# uploading ``text/html`` media tells Drive to CONVERT the HTML into a real
# editable Google Doc (rather than storing a raw .html file).
_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

# Defensive cap on read_google_doc output. A Doc can be arbitrarily large; the
# result feeds straight back into the model's context, so an unbounded dump
# could blow the window. 40k chars is generous for reasoning while bounded.
_READ_CHAR_CAP = 40_000


def _drive():
    # Docs-via-Drive: reuse the already-authorized Drive service (no separate
    # 'docs' client, no extra scope). Cached by get_service's lru_cache.
    return get_service("drive")


@tool
@idempotent("create_google_doc", key_args=["title", "content_markdown", "folder_id"])
def create_google_doc(
    title: str,
    content_markdown: str,
    folder_id: Optional[str] = None,
) -> str:
    """Create a new, professionally formatted Google Doc from Markdown content.

    Use this whenever the user wants an actual Google Document (a report, brief,
    proposal, meeting notes, spec, etc.) — do NOT write a local Markdown file as
    a substitute. The Doc is created in the connected account's Drive and can
    then be shared with `share_drive_file` / `share_drive_file_with_anyone`.

    Args:
        title: The document title (also its Drive filename).
        content_markdown: The body, written in ordinary Markdown. Headings
            (`#`, `##`), **bold**, *italic*, bullet/numbered lists, `tables`,
            > blockquotes, `inline code`, and [links](url) are all converted to
            native Google Docs formatting automatically — write natural Markdown,
            not raw HTML.
        folder_id: Optional Drive folder ID to create the Doc inside. Defaults
            to the Drive root ("My Drive").
    """
    try:
        if not title or not title.strip():
            return "⚠️ Create Google Doc failed: a non-empty `title` is required."

        html_bytes = to_docs_html(content_markdown or "").encode("utf-8")

        metadata: dict[str, object] = {"name": title.strip(), "mimeType": _GOOGLE_DOC_MIME}
        if folder_id and folder_id.strip():
            metadata["parents"] = [folder_id.strip()]

        # Build the media INSIDE the callable: execute_with_retry re-invokes it
        # on a transient socket drop, and a BytesIO is single-use — rebuilding
        # per attempt guarantees each retry uploads the full content, never an
        # exhausted/empty stream.
        def _do_create():
            media = MediaIoBaseUpload(io.BytesIO(html_bytes), mimetype="text/html", resumable=False)
            return (
                _drive()
                .files()
                .create(
                    body=metadata,
                    media_body=media,
                    fields="id, name, webViewLink",
                    supportsAllDrives=True,
                )
            )

        created = execute_with_retry(_do_create)
        doc_id = created["id"]
        link = created.get("webViewLink", "N/A")
        return (
            f"✅ Google Doc created: **{created.get('name', title)}** (ID: `{doc_id}`)\n"
            f"Link: {link}"
        )
    except Exception as e:
        return f"⚠️ Create Google Doc failed: {e}"


@tool
def read_google_doc(document_id: str) -> str:
    """Read the full text content of an existing Google Doc.

    Exports the Doc as plain text so IRIS can review, summarize, or fact-check
    its contents. Use the document/file ID (as returned by `create_google_doc`
    or `search_drive_files`).

    Args:
        document_id: The Google Doc's file ID.
    """
    try:
        if not document_id or not document_id.strip():
            return "⚠️ Read Google Doc failed: a non-empty `document_id` is required."

        # files().export(...).execute() returns the exported bytes directly;
        # execute_with_retry handles transient socket drops.
        raw = execute_with_retry(
            lambda: _drive().files().export(fileId=document_id.strip(), mimeType="text/plain")
        )
        # Google's text/plain export prepends a UTF-8 BOM that str.strip() does
        # NOT treat as whitespace; decode with utf-8-sig so a leading BOM is
        # dropped and the model never receives a stray leading character.
        if isinstance(raw, (bytes, bytearray)):
            text = bytes(raw).decode("utf-8-sig", errors="replace")
        else:
            text = str(raw)
            if text[:1] == "﻿":  # defensive: strip a leading BOM on the str path too
                text = text[1:]
        text = text.replace("\r\n", "\n").strip()

        if not text:
            return f"📄 Google Doc `{document_id}` is empty."
        if len(text) > _READ_CHAR_CAP:
            text = text[:_READ_CHAR_CAP].rstrip() + f"\n\n… [truncated at {_READ_CHAR_CAP} characters]"
        return f"📄 Content of Google Doc `{document_id}`:\n\n{text}"
    except Exception as e:
        return f"⚠️ Read Google Doc failed: {e}"


# Tool registry — imported by subagent_config.py for Grace
DOCS_TOOLS = [
    create_google_doc,
    read_google_doc,
]
