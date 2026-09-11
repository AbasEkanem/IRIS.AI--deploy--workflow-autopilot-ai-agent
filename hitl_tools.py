"""hitl_tools.py — single source of truth for the HITL (human-in-the-loop) gate.

The structural HITL gate has THREE representations that must always agree:

  1. `IRREVERSIBLE_TOOLS` (this file) — the tuple wired into deepagents'
     `interrupt_on` in IRIS.py, which pauses the graph before any of these tools
     runs and propagates into all 5 specialist subagents.
  2. The categorical HITL clause in the orchestrator prompt
     (`prompts/iris/delegation-rules.md`, `prompts/iris/execution-protocol.md`) —
     the prose that tells IRIS which actions require approval.
  3. The UI approval surface (`ui/src/components/ApprovalCard.tsx`) — the
     per-tool copy shown to the human approving/rejecting the pending action.

When these drift, real damage follows: a tool gated in code but absent from the
prompt caused a reworded-redispatch loop that ended in a worker CancelledError
(see the HITL production-wiring post-mortem). This module is the SSOT for (1),
and `verify_hitl_sync()` is the durable guard that proves (1), (2) and (3) still
line up — run hard in `test_hitl_sync.py` (pre-push) and warn-only at IRIS boot.

Intentionally a LEAF module: it imports only the standard library, so IRIS.py can
import the SSOT and the boot guard without any circular dependency on the harness.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── The gated tools (SSOT) ────────────────────────────────────────────────────
# These perform destructive or externally-visible actions — posting Slack
# messages, sending calendar invites, emailing, sharing files, publishing forms,
# leaving outward-visible comments, transitioning tickets, and deletes. Each is
# passed to the harness's `interrupt_on` (IRIS.py) so the graph PAUSES for human
# approval before the tool runs — a structural gate that does not depend on model
# judgment, and one that propagates into every specialist subagent.
IRREVERSIBLE_TOOLS = (
    # ── Outbound email (Grace) ───────────────────────────────────────────────
    "send_research_email",
    "schedule_research_email",
    # ── Outbound Slack messages (Sienna) — reach real people in a workspace ──
    "send_slack_message",
    "reply_to_slack_thread",
    "send_slack_dm",
    "send_slack_ephemeral_message",
    "schedule_slack_message",
    "update_slack_message",              # edits an already-posted message
    "upload_slack_file",
    # ── Calendar (Grace) — create/modify/cancel emails an invite to attendees ─
    "create_calendar_event",
    "update_calendar_event",
    "cancel_calendar_event",
    "respond_to_calendar_invitation",
    # ── Externally-visible comments / publishing ─────────────────────────────
    "add_jira_comment",                  # visible to every issue watcher
    "create_attio_comment",              # visible to CRM collaborators
    "publish_google_form",               # makes the form publicly live
    # ── Drive sharing (Grace) — grants access to outside parties ─────────────
    "share_drive_file",
    "bulk_share_drive_files",
    "share_drive_file_with_anyone",
    # ── Destructive / irreversible mutations (deletes, trashes, transitions) ─
    "transition_jira_issue",
    "delete_jira_issue",
    "trash_drive_file",
    "delete_attio_record",
    "delete_attio_note",
    "delete_attio_task",
    "delete_attio_list_entry",
    "delete_slack_message",
    "delete_scheduled_slack_message",
    "delete_form_item",
)

# ── Category map ──────────────────────────────────────────────────────────────
# Every gated tool, grouped by the kind of approval-worthy action it performs.
# The union of the values MUST equal set(IRREVERSIBLE_TOOLS) exactly — that
# equality is check (1) of verify_hitl_sync(), so a tool added to the gate but
# forgotten here (or vice versa) fails the guard.
HITL_TOOL_CATEGORIES: dict[str, tuple[str, ...]] = {
    "outbound_email": (
        "send_research_email",
        "schedule_research_email",
    ),
    "slack_write": (
        "send_slack_message",
        "reply_to_slack_thread",
        "send_slack_dm",
        "send_slack_ephemeral_message",
        "schedule_slack_message",
        "update_slack_message",
        "upload_slack_file",
    ),
    "calendar_change": (
        "create_calendar_event",
        "update_calendar_event",
        "cancel_calendar_event",
        "respond_to_calendar_invitation",
    ),
    "external_comment": (
        "add_jira_comment",
        "create_attio_comment",
    ),
    "form_publish": (
        "publish_google_form",
    ),
    "drive_share": (
        "share_drive_file",
        "bulk_share_drive_files",
        "share_drive_file_with_anyone",
    ),
    "jira_transition": (
        "transition_jira_issue",
    ),
    "resource_deletion": (
        "delete_jira_issue",
        "trash_drive_file",
        "delete_attio_record",
        "delete_attio_note",
        "delete_attio_task",
        "delete_attio_list_entry",
        "delete_slack_message",
        "delete_scheduled_slack_message",
        "delete_form_item",
    ),
}

# ── Prose coverage anchors ────────────────────────────────────────────────────
# The categorical HITL clause in each prompt file must still name every category
# above. Rather than diff prose, we require these semantic keywords to survive
# (case-insensitive). The keywords that were the ACTUAL historical drift gaps —
# "rsvp" (respond_to_calendar_invitation), "ephemeral"/"upload" (the looser Slack
# writes), and "transition" (any transition, not just Done/Closed) — are included
# deliberately so that silently dropping them re-breaks the guard.
_REQUIRED_PROMPT_KEYWORDS: tuple[str, ...] = (
    "email",        # outbound_email
    "slack",        # slack_write
    "ephemeral",    # slack_write — send_slack_ephemeral_message
    "upload",       # slack_write — upload_slack_file
    "calendar",     # calendar_change
    "rsvp",         # calendar_change — respond_to_calendar_invitation
    "comment",      # external_comment
    "form",         # form_publish / delete_form_item
    "drive",        # drive_share
    "transition",   # jira_transition — ANY transition
    "delet",        # resource_deletion — matches delete/deletion/deleting
)

# Files that carry the representations checked against the SSOT. Paths are
# relative to this module's directory (the repo root), so the check is
# independent of the process working directory.
_PROMPT_FILES: tuple[str, ...] = (
    "prompts/iris/delegation-rules.md",
    "prompts/iris/execution-protocol.md",
)
_UI_APPROVAL_CARD = "ui/src/components/ApprovalCard.tsx"


class HITLSyncError(AssertionError):
    """Raised by verify_hitl_sync(strict=True) when a representation has drifted."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def verify_hitl_sync(strict: bool = False) -> list[str]:
    """Check that the three HITL representations agree. Return a list of problems.

    Checks, in order:
      1. STRUCTURAL — HITL_TOOL_CATEGORIES flattens to exactly
         set(IRREVERSIBLE_TOOLS): no gated tool is uncategorized, no category
         names a non-gated tool, and nothing is listed twice.
      2. PROSE — every keyword in _REQUIRED_PROMPT_KEYWORDS appears
         (case-insensitive) in each prompt file, so the categorical HITL clause
         provably still covers all 29 tools.
      3. UI (best-effort) — every tool name appears in ApprovalCard.tsx. If that
         file is absent (a backend-only checkout) the UI check is SKIPPED with a
         logged note rather than counted as drift.

    strict=True raises HITLSyncError on any problem — the pre-push gate
    (test_hitl_sync.py). strict=False returns the problems and logs a warning —
    the IRIS boot guard, which must NEVER raise so a guard bug cannot down prod.
    """
    problems: list[str] = []
    root = _repo_root()

    # 1. Structural: category map ⇔ gated tuple.
    flat = [t for tools in HITL_TOOL_CATEGORIES.values() for t in tools]
    cat_set = set(flat)
    gated = set(IRREVERSIBLE_TOOLS)
    if len(flat) != len(cat_set):
        dupes = sorted({t for t in flat if flat.count(t) > 1})
        problems.append(f"HITL_TOOL_CATEGORIES lists tool(s) more than once: {dupes}")
    if len(gated) != len(IRREVERSIBLE_TOOLS):
        dupes = sorted({t for t in IRREVERSIBLE_TOOLS if IRREVERSIBLE_TOOLS.count(t) > 1})
        problems.append(f"IRREVERSIBLE_TOOLS lists tool(s) more than once: {dupes}")
    missing = gated - cat_set
    extra = cat_set - gated
    if missing:
        problems.append(f"gated tools missing from HITL_TOOL_CATEGORIES: {sorted(missing)}")
    if extra:
        problems.append(f"HITL_TOOL_CATEGORIES names non-gated tool(s): {sorted(extra)}")

    # 2. Prose: required keywords present in each prompt file.
    for rel in _PROMPT_FILES:
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8").lower()
        except OSError as exc:
            problems.append(f"cannot read prompt file {rel}: {exc}")
            continue
        for keyword in _REQUIRED_PROMPT_KEYWORDS:
            if keyword not in text:
                problems.append(f"{rel}: HITL prose is missing the keyword '{keyword}'")

    # 3. UI (best-effort): every gated tool named in the approval card.
    ui_path = root / _UI_APPROVAL_CARD
    try:
        ui_text = ui_path.read_text(encoding="utf-8")
    except OSError:
        logger.info("hitl_sync: %s not found — UI coverage check skipped", _UI_APPROVAL_CARD)
    else:
        for tool in IRREVERSIBLE_TOOLS:
            if tool not in ui_text:
                problems.append(f"{_UI_APPROVAL_CARD}: gated tool '{tool}' has no approval-card entry")

    if problems:
        if strict:
            raise HITLSyncError(
                "HITL sync drift detected:\n  - " + "\n  - ".join(problems)
            )
        logger.warning("hitl_sync.drift detected (%d issue(s)): %s", len(problems), problems)
    return problems


if __name__ == "__main__":  # `python hitl_tools.py` → run the strict check by hand
    logging.basicConfig(level=logging.INFO)
    verify_hitl_sync(strict=True)
    print(f"HITL sync OK — {len(IRREVERSIBLE_TOOLS)} gated tools, "
          f"{len(HITL_TOOL_CATEGORIES)} categories, all representations agree.")
