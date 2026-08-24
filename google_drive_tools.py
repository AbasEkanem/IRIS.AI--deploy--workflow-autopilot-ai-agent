"""
google_drive_tools.py
======================
Full Drive CRUD for IRIS.AI (Grace Subagent): search, upload, download, export, move, rename,
share, permissions management, trash, and permanent delete.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from typing import Optional


from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from langchain_core.tools import tool

from google_auth import get_service, execute_with_retry
from idempotency import idempotent

_log = logging.getLogger(__name__)

# Google's media (download/export) endpoints intermittently drop the socket
# mid-transfer — on Windows this surfaces as `[WinError 10053] An established
# connection was aborted`, on other platforms as ConnectionResetError / SSLError
# / BrokenPipeError. These are transient: the very next attempt usually
# succeeds. Without a retry the whole subagent task dies on a single blip
# (observed: a Slides export failing twice with WinError 10053 before the drive
# agent finally recovered). Retry transient transport faults with short backoff.
_TRANSIENT_NET_ERRORS = (
    ConnectionError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
    OSError,  # WinError 10053/10054 arrive as OSError subclasses
)
_MEDIA_MAX_ATTEMPTS = 4

# Shared-Drive support flags. Drive v3's files().list() / get() / update() /
# permissions().* default to My-Drive-ONLY. When the connected account is a
# Google Workspace account that owns or is a member of Shared Drives (Team
# Drives) — or holds many cross-domain "Shared with me" items — omitting these
# flags makes the server silently skip those corpora AND stall while resolving
# cross-domain permissions, which is the primary cause of the drive_agent's
# "fetching takes too long → timeout" failure. They are harmless for a plain
# personal My-Drive account, so we pass them on every call that accepts them.
_SHARED_DRIVE_FLAGS = {
    "supportsAllDrives": True,
    "includeItemsFromAllDrives": True,
}


def _is_transient_net_error(exc: Exception) -> bool:

    if isinstance(exc, _TRANSIENT_NET_ERRORS):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in ("10053", "10054", "aborted", "reset by peer", "broken pipe", "connection")
    )


def _retry_media(fn, *, what: str):
    """Run a Drive media call, retrying transient connection drops with backoff."""
    last_exc: Exception | None = None
    for attempt in range(1, _MEDIA_MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — decide retry vs re-raise below
            last_exc = exc
            if attempt >= _MEDIA_MAX_ATTEMPTS or not _is_transient_net_error(exc):
                raise
            backoff = 0.5 * (2 ** (attempt - 1))  # 0.5s, 1s, 2s
            _log.warning(
                "[drive] %s transient error (attempt %d/%d): %s — retrying in %.1fs",
                what, attempt, _MEDIA_MAX_ATTEMPTS, exc, backoff,
            )
            time.sleep(backoff)
    if last_exc:
        raise last_exc


def _svc():
    return get_service("drive")



@tool
def search_drive_files(query: str = "", max_results: int = 20) -> str:
    """Search Google Drive for files and folders matching a query string or Drive JQL filter.

    Examples:
        query: "name contains 'Q3 Report'"
        query: "mimeType = 'application/vnd.google-apps.presentation'"
    """
    try:
        q_str = f"name contains '{query}' and trashed = false" if query and "trashed" not in query and "=" not in query else (query or "trashed = false")
        # Cap pageSize defensively: an unbounded/huge page forces Drive to
        # resolve permissions across every corpus before returning, which is
        # what pushes a large shared account past the socket timeout.
        page_size = max(1, min(int(max_results or 20), 100))
        results = _retry_media(
            lambda: _svc()
            .files()
            .list(
                q=q_str,
                pageSize=page_size,
                # Return the most recently touched files first so a bounded page
                # surfaces the relevant results even on a very large Drive.
                orderBy="modifiedTime desc",
                fields="files(id, name, mimeType, modifiedTime, webViewLink, parents)",
                # Include Shared Drives / cross-domain items (see _SHARED_DRIVE_FLAGS).
                **_SHARED_DRIVE_FLAGS,
                corpora="allDrives",
            )
            .execute(),
            what="search_drive_files",
        )
        files = results.get("files", [])

        if not files:
            return f"No Drive files found matching query: '{query}'"

        out = [f"Found {len(files)} file(s):"]
        for f in files:
            out.append(f"- **{f['name']}** (ID: `{f['id']}` | Type: `{f['mimeType']}`)\n  Link: {f.get('webViewLink', 'N/A')}")
        return "\n".join(out)
    except Exception as e:
        return f"⚠️ Drive search failed: {e}"


@tool
@idempotent("upload_file_to_drive", key_args=["local_path", "name", "parent_folder_id"])
def upload_file_to_drive(
    local_path: str,
    name: Optional[str] = None,
    parent_folder_id: Optional[str] = None,
    mime_type: Optional[str] = None
) -> str:
    """Upload a local file to Google Drive.

    Args:
        local_path: Local path to the file to upload.
        name: Optional custom filename in Drive.
        parent_folder_id: Optional parent Google Drive folder ID.
        mime_type: Optional MIME type (e.g. 'application/pdf').
    """
    try:
        filename = name or local_path.split("/")[-1].split("\\")[-1]
        file_metadata: dict[str, str | list[str]] = {"name": filename}
        if parent_folder_id:
            file_metadata["parents"] = [parent_folder_id]

        media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
        created = (
            _svc()
            .files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )

        return f"✅ File uploaded to Drive: **{created['name']}** (ID: `{created['id']}`)\nLink: {created.get('webViewLink')}"
    except Exception as e:
        return f"⚠️ Upload failed: {e}"


@tool
def download_file_from_drive(file_id: str, destination_path: str) -> str:
    """Download a raw file from Google Drive to a local filesystem path.

    Args:
        file_id: The ID of the file in Google Drive.
        destination_path: Destination local path to write the downloaded file.
    """
    try:
        request = _svc().files().get_media(fileId=file_id)
        with io.FileIO(destination_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                # Retry each chunk on a transient connection drop (WinError
                # 10053 / reset) instead of failing the whole download.
                _, done = _retry_media(downloader.next_chunk, what="download_file")
        return f"✅ File downloaded successfully to `{destination_path}`"
    except Exception as e:
        return f"⚠️ Download failed: {e}"



@tool
def export_drive_file(file_id: str, destination_path: str, mime_type: str = "application/pdf") -> str:
    """Export a Google-native file (Doc/Sheet/Slide) to another format (e.g. PDF, PNG, CSV).

    Args:
        file_id: The ID of the Google Doc/Sheet/Slide.
        destination_path: Destination local path (e.g. 'report.pdf').
        mime_type: Target export format (e.g. 'application/pdf', 'image/png', 'text/csv').
    """
    try:
        request = _svc().files().export_media(fileId=file_id, mimeType=mime_type)
        with io.FileIO(destination_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                # Retry each chunk on a transient connection drop (WinError
                # 10053 / reset) instead of failing the whole export.
                _, done = _retry_media(downloader.next_chunk, what="export_file")
        return f"✅ Exported file `{file_id}` to `{destination_path}` as `{mime_type}`"

    except Exception as e:
        return f"⚠️ Export file failed: {e}"


@tool
@idempotent("create_drive_folder", key_args=["name", "parent_folder_id"])
def create_drive_folder(name: str, parent_folder_id: Optional[str] = None) -> str:
    """Create a new folder in Google Drive.

    Args:
        name: Name for the new folder.
        parent_folder_id: Optional parent folder ID to nest inside.
    """
    try:
        metadata: dict[str, str | list[str]] = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_folder_id:
            metadata["parents"] = [parent_folder_id]
        created = _svc().files().create(
            body=metadata, fields="id, name, webViewLink", supportsAllDrives=True
        ).execute()
        return f"✅ Folder created: **{created['name']}** (ID: `{created['id']}`)\nLink: {created.get('webViewLink')}"

    except Exception as e:
        return f"⚠️ Create folder failed: {e}"


@tool
def move_drive_file(file_id: str, new_parent_folder_id: str) -> str:
    """Move a file to a different folder in Google Drive.

    Args:
        file_id: The ID of the file to move.
        new_parent_folder_id: Destination parent folder ID.
    """
    try:
        file = _svc().files().get(
            fileId=file_id, fields="parents", supportsAllDrives=True
        ).execute()
        previous_parents = ",".join(file.get("parents", []))
        _svc().files().update(
            fileId=file_id,
            addParents=new_parent_folder_id,
            removeParents=previous_parents,
            fields="id, parents",
            supportsAllDrives=True,
        ).execute()

        return f"✅ Moved file `{file_id}` to folder `{new_parent_folder_id}`"
    except Exception as e:
        return f"⚠️ Move file failed: {e}"


@tool
def rename_drive_file(file_id: str, new_name: str) -> str:
    """Rename a file or folder in Google Drive.

    Args:
        file_id: The ID of the file or folder.
        new_name: New name string.
    """
    try:
        updated = _svc().files().update(
            fileId=file_id, body={"name": new_name}, fields="id, name", supportsAllDrives=True
        ).execute()
        return f"✅ Renamed file `{file_id}` to **{updated['name']}**"

    except Exception as e:
        return f"⚠️ Rename file failed: {e}"


@tool
@idempotent("share_drive_file", key_args=["file_id", "email", "role"])
async def share_drive_file(file_id: str, email: str, role: str = "writer", notify: bool = True) -> str:
    """Share a Google Drive file or folder with a specific user email.

    Args:
        file_id: The ID of the file/folder.
        email: Recipient email address.
        role: One of 'reader', 'commenter', 'writer', 'owner'. Default 'writer'.
        notify: Whether to send an email notification (default True).
    """
    try:
        permission = {"type": "user", "role": role, "emailAddress": email}
        def _exec_share():
            return (
                _svc()
                .permissions()
                .create(
                    fileId=file_id,
                    body=permission,
                    sendNotificationEmail=notify,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
        perm = await asyncio.to_thread(_exec_share)
        return f"✅ Shared file `{file_id}` with `{email}` as `{role}` (Perm ID: `{perm['id']}`)."

    except Exception as e:
        return f"⚠️ Share file failed: {e}"


@tool
@idempotent("bulk_share_drive_files", key_args=["email", "query", "file_ids", "role"])
def bulk_share_drive_files(
    email: str,
    query: str = "",
    file_ids: str = "",
    role: str = "writer",
    notify: bool = False,
    max_files: int = 200,
) -> str:
    """Share MANY Google Drive files with one user in a SINGLE call.

    Use this instead of calling share_drive_file repeatedly. It is the correct
    tool for requests like "share all my Google Docs with X" — sharing files
    one at a time exhausts the step budget and the task dies partway through.

    You must provide `email` plus EITHER `query` OR `file_ids` (not both):
      - query: a Drive search filter selecting which files to share, e.g.
          "mimeType='application/vnd.google-apps.document'" (all Google Docs)
          "mimeType='application/vnd.google-apps.spreadsheet'" (all Sheets)
          "name contains 'Q3'" (files whose name contains Q3)
        When query is used, this tool discovers the matching files itself.
      - file_ids: a JSON array OR comma-separated string of explicit file IDs,
          e.g. ["idA","idB"] or "idA,idB". Use this when you already know the IDs.

    Args:
        email: Recipient email address to grant access to.
        query: Drive search filter selecting files to share (mutually exclusive with file_ids).
        file_ids: JSON array or comma-separated list of file IDs (mutually exclusive with query).
        role: One of 'reader', 'commenter', 'writer', 'owner'. Default 'writer'.
        notify: Whether to send an email notification per file (default False for bulk).
        max_files: Safety cap on how many files to share in one call (default 200).
    """
    try:
        # ── Resolve the target file IDs from either an explicit list or a query.
        ids: list[str] = []
        if file_ids and file_ids.strip():
            raw = file_ids.strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                ids = [str(x).strip() for x in parsed if str(x).strip()]
            else:
                ids = [part.strip() for part in raw.split(",") if part.strip()]
        elif query and query.strip():
            q = query.strip()
            if "trashed" not in q:
                q = f"({q}) and trashed = false"
            page_token = None
            while True:
                resp = _retry_media(
                    lambda pt=page_token: _svc()
                    .files()
                    .list(
                        q=q,
                        pageSize=100,
                        fields="nextPageToken, files(id, name)",
                        pageToken=pt,
                        # Include Shared Drives / cross-domain items so the bulk
                        # share discovers everything the account can reach.
                        **_SHARED_DRIVE_FLAGS,
                        corpora="allDrives",
                    )
                    .execute(),
                    what="bulk_share_discover",
                )
                for f in resp.get("files", []):

                    ids.append(f["id"])
                page_token = resp.get("nextPageToken")
                if not page_token or len(ids) >= max_files:
                    break
        else:
            return "⚠️ bulk_share_drive_files needs either `query` or `file_ids`."

        if not ids:
            return f"No files matched — nothing shared with `{email}`."

        ids = ids[:max_files]

        shared = 0
        failures: list[str] = []
        for fid in ids:
            try:
                _svc().permissions().create(
                    fileId=fid,
                    body={"type": "user", "role": role, "emailAddress": email},
                    sendNotificationEmail=notify,
                    fields="id",
                ).execute()
                shared += 1
            except Exception as e:  # noqa: BLE001 — collect per-file errors, keep going
                failures.append(f"{fid}: {e}")

        summary = [f"✅ Shared {shared}/{len(ids)} file(s) with `{email}` as `{role}`."]
        if failures:
            summary.append(f"⚠️ {len(failures)} failed:")
            summary.extend(f"  - {f}" for f in failures[:10])
            if len(failures) > 10:
                summary.append(f"  …and {len(failures) - 10} more.")
        return "\n".join(summary)
    except Exception as e:
        return f"⚠️ Bulk share failed: {e}"


@tool
@idempotent("share_drive_file_with_anyone", key_args=["file_id", "role"])
def share_drive_file_with_anyone(file_id: str, role: str = "reader") -> str:
    """Make a file viewable/commentable/editable by anyone with the link.


    Args:
        file_id: The ID of the file.
        role: One of 'reader', 'commenter', 'writer'. Default 'reader'.
    """
    try:
        permission = {"type": "anyone", "role": role}
        perm = _svc().permissions().create(
            fileId=file_id, body=permission, fields="id", supportsAllDrives=True
        ).execute()
        return f"✅ File `{file_id}` is now accessible by anyone with the link as `{role}` (Perm ID: `{perm['id']}`)."

    except Exception as e:
        return f"⚠️ Share with anyone failed: {e}"


@tool
def list_drive_file_permissions(file_id: str) -> str:
    """List all user permissions and access rules on a Google Drive file/folder.

    Args:
        file_id: The ID of the file or folder.
    """
    try:
        res = _svc().permissions().list(
            fileId=file_id,
            fields="permissions(id, type, role, emailAddress)",
            supportsAllDrives=True,
        ).execute()

        perms = res.get("permissions", [])
        if not perms:
            return f"No explicit permissions found for file `{file_id}`."

        out = [f"🔒 Permissions for File `{file_id}` ({len(perms)}):"]
        for p in perms:
            email = p.get("emailAddress", "Anyone / Public")
            out.append(f"- ID: `{p['id']}` | User: `{email}` | Role: `{p['role']}` | Type: `{p['type']}`")
        return "\n".join(out)
    except Exception as e:
        return f"⚠️ List permissions failed: {e}"


@tool
def revoke_drive_file_permission(file_id: str, permission_id: str) -> str:
    """Revoke/Delete a user's permission from a Google Drive file or folder.

    Args:
        file_id: The ID of the file.
        permission_id: The ID of the permission rule to delete.
    """
    try:
        _svc().permissions().delete(
            fileId=file_id, permissionId=permission_id, supportsAllDrives=True
        ).execute()
        return f"✅ Revoked permission `{permission_id}` from file `{file_id}`."

    except Exception as e:
        return f"⚠️ Revoke permission failed: {e}"


@tool
def trash_drive_file(file_id: str) -> str:
    """Move a file to Google Drive trash (recoverable).

    Args:
        file_id: The ID of the file to move to trash.
    """
    try:
        _svc().files().update(
            fileId=file_id, body={"trashed": True}, fields="id, trashed", supportsAllDrives=True
        ).execute()
        return f"✅ Moved file `{file_id}` to trash."

    except Exception as e:
        return f"⚠️ Trash file failed: {e}"


@tool
def delete_drive_file(file_id: str) -> str:
    """Permanently delete a file or folder from Google Drive (bypasses trash).

    Args:
        file_id: The ID of the file or folder to permanently delete.
    """
    try:
        _svc().files().delete(fileId=file_id, supportsAllDrives=True).execute()
        return f"✅ Permanently deleted file `{file_id}` from Google Drive."

    except Exception as e:
        return f"⚠️ Delete file failed: {e}"


# Tool registry — imported by subagent_config.py for Grace
DRIVE_TOOLS = [
    search_drive_files,
    upload_file_to_drive,
    download_file_from_drive,
    export_drive_file,
    create_drive_folder,
    move_drive_file,
    rename_drive_file,
    share_drive_file,
    bulk_share_drive_files,
    share_drive_file_with_anyone,
    list_drive_file_permissions,
    revoke_drive_file_permission,
    trash_drive_file,
    delete_drive_file,
]
