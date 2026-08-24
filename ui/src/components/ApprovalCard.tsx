"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { InterruptState } from "@/types/chat";

/* ══════════════════════════════════════════════════════════════════════════
   ApprovalCard — the human-in-the-loop gate

   This is the only thing standing between IRIS and an irreversible action:
   a sent email, a posted Slack message, a shared Drive file, a deleted CRM
   record. `interrupt_on` (IRIS.py:_IRREVERSIBLE_TOOLS) pauses the graph BEFORE
   the tool runs and the whole conversation blocks here until a human decides.
   The design follows from that, not from decoration:

   1. **The approver is not a developer.** The person whose name goes on that
      email must never be asked to read or edit JSON to check it. So the card
      leads with a plain-language sentence built from the real args — "Send an
      email to abasi@10alytics.com — subject: World News Brief" — and editing
      is per-field: labelled To / Cc / Subject / Body inputs, a Channel and a
      Message, a title and a time. Raw JSON still exists for the developer who
      needs it, demoted into "Advanced — raw payload".
   2. **Who it goes to is the thing people actually check.** Recipients get
      their own block directly under the verb, one row per recipient, so a
      stray address cannot hide in the middle of a comma-separated mush.
   3. **The reviewer must be able to read what they are approving.** The old
      card clipped every value at `maxHeight: 72` inside a nested scroller
      inside a collapsed disclosure — so you could approve an email body you
      had never seen. Values are never clipped or truncated.
   4. **The primary button names the actual consequence** — "Send email",
      "Delete record", "Make public" — never a generic "Approve & Execute".
   5. **Reject carries equal visual weight.** Both decisions are equal width
      and Reject is fully legible rather than de-emphasised.
   6. **Theme tokens only** (`var(--token, fallback)`), and `--text-muted`
      rather than `--muted`, which is not defined outside a shell-scoped block.

   SECURITY: every arg value is untrusted. An email body can be harvested from
   a web page or an inbox and carry indirect prompt injection. Every value on
   this card — headline, chips, read view, inputs — is rendered as inert plain
   text: React children or a `value=` prop. Nothing here goes through the
   markdown renderer and nothing is ever interpolated into HTML.

   Mounted ONCE, in page.tsx's composer dock. It used to render twice
   simultaneously — inline in the transcript and again in the floating overlay
   — which is why the same approval appeared under two different headlines.
   ══════════════════════════════════════════════════════════════════════════ */

const C = {
  cardBg: "var(--bg-card, #16233d)",
  border: "var(--border, #1E2740)",
  borderMed: "var(--border-med, #222d47)",
  text: "var(--text, #E8EBF7)",
  muted: "var(--text-muted, #7E88A8)",
  accent: "var(--accent, #8AB4F8)",
  surface2: "var(--surface-2, rgba(255,255,255,0.05))",
  green: "var(--green, #81c995)",
  amber: "var(--amber, #fdd663)",
  red: "var(--red, #f28b82)",
};

/* ── Icons ──────────────────────────────────────────────────────────────
   SVG, not emoji. Emoji render at the mercy of the platform font, ignore
   `currentColor`, and were a large part of why this card read as dated. */

type IconName =
  | "mail" | "clock" | "chat" | "dm" | "calendar" | "trash"
  | "share" | "ticket" | "comment" | "upload" | "globe" | "edit" | "bolt"
  | "person" | "hash" | "plus" | "close" | "code" | "target";

const ICON_PATHS: Record<IconName, string> = {
  mail: "M4 4h16v16H4zM4 7l8 6 8-6",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3.5 2",
  chat: "M21 12a8 8 0 0 1-8 8H8l-5 3 1.5-5A8 8 0 1 1 21 12z",
  dm: "M22 3 11 14M22 3l-7 18-4-7-7-4z",
  calendar: "M4 5h16v15H4zM4 10h16M9 3v4M15 3v4",
  trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6",
  share: "M18 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 22a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM8.6 13.5l6.8 4M15.4 6.5l-6.8 4",
  ticket: "M4 6h16v5a2 2 0 0 0 0 4v3H4v-3a2 2 0 0 0 0-4zM12 6v12",
  comment: "M4 4h16v12H8l-4 4z",
  upload: "M12 16V4M7 9l5-5 5 5M4 20h16",
  globe: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c2.5 2.4 2.5 15.6 0 18M12 3c-2.5 2.4-2.5 15.6 0 18",
  edit: "M12 20h9M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4z",
  bolt: "M13 2 4 14h6l-1 8 9-12h-6z",
  person: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4.5 20.5c0-3.6 3.4-5.5 7.5-5.5s7.5 1.9 7.5 5.5",
  hash: "M6.5 3 5 21M19 3l-1.5 18M3.5 8.5h17M2.5 15.5h17",
  plus: "M12 5v14M5 12h14",
  close: "M6 6l12 12M18 6 6 18",
  code: "M8.5 5.5 2.5 12l6 6.5M15.5 5.5l6 6.5-6 6.5",
  target: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z",
};

function Icon({ name, size = 16, color, strokeWidth = 1.7 }: {
  name: IconName; size?: number; color?: string; strokeWidth?: number;
}) {
  return (
    <svg
      aria-hidden width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke={color ?? "currentColor"} strokeWidth={strokeWidth}
      strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}
    >
      <path d={ICON_PATHS[name]} />
    </svg>
  );
}

/* ── Tool registry ──────────────────────────────────────────────────────
   Every tool in IRIS.py's `_IRREVERSIBLE_TOOLS` is listed. The old registry
   covered 8 of 30, so calendar invites, Drive sharing and every delete fell
   through to a bare "⚡ delete_attio_record" — the reviewer had to decode a
   function name to understand what they were authorising.

   These keys are BACKEND TOOL NAMES and are byte-identical to the ones that
   drive `interrupt_on`. Never rename one to make a label read better — the
   label is the `label` field.

   `verb` is what the primary button says: it must describe the consequence,
   in the imperative, short enough to fit a button.
   `owner` is the specialist that runs it (the "real names" convention used
   throughout the workspace). `destructive` means it cannot be walked back. */
interface ToolMeta {
  icon: IconName;
  label: string;
  description: string;
  verb: string;
  owner?: string;
  destructive?: boolean;
}

const TOOL_META: Record<string, ToolMeta> = {
  /* Outbound email — Grace */
  send_research_email:           { icon: "mail",     label: "Send email",                   description: "Delivers this message to the recipients below.",                 verb: "Send email",       owner: "grace" },
  schedule_research_email:       { icon: "clock",    label: "Schedule email",               description: "Queues this message for automatic delivery later.",              verb: "Schedule email",   owner: "grace" },
  /* Outbound Slack — Sienna */
  send_slack_message:            { icon: "chat",     label: "Post to Slack channel",        description: "Posts this message where everyone in the channel sees it.",       verb: "Post message",     owner: "sienna" },
  reply_to_slack_thread:         { icon: "chat",     label: "Reply in Slack thread",        description: "Posts a reply visible to everyone following the thread.",         verb: "Post reply",       owner: "sienna" },
  send_slack_dm:                 { icon: "dm",       label: "Send Slack DM",                description: "Sends a direct message to this person.",                          verb: "Send DM",          owner: "sienna" },
  send_slack_ephemeral_message:  { icon: "chat",     label: "Send ephemeral message",       description: "Shows a message only this one Slack user can see.",               verb: "Send message",     owner: "sienna" },
  schedule_slack_message:        { icon: "clock",    label: "Schedule Slack message",       description: "Queues this message to post at a later time.",                    verb: "Schedule message", owner: "sienna" },
  update_slack_message:          { icon: "edit",     label: "Edit a posted message",        description: "Rewrites a message that people may have already read.",           verb: "Save edit",        owner: "sienna" },
  upload_slack_file:             { icon: "upload",   label: "Upload file to Slack",         description: "Shares this file with the channel's members.",                    verb: "Upload file",      owner: "sienna" },
  delete_slack_message:          { icon: "trash",    label: "Delete Slack message",         description: "Permanently removes a posted message.",                           verb: "Delete message",   owner: "sienna", destructive: true },
  delete_scheduled_slack_message:{ icon: "trash",    label: "Cancel scheduled message",     description: "Stops a queued message from ever being sent.",                    verb: "Cancel message",   owner: "sienna", destructive: true },
  /* Calendar — Grace. Every one of these emails the attendees. */
  create_calendar_event:         { icon: "calendar", label: "Create calendar event",        description: "Creates the event and emails an invitation to every attendee.",   verb: "Create event",     owner: "grace" },
  update_calendar_event:         { icon: "calendar", label: "Update calendar event",        description: "Changes the event and notifies every attendee.",                  verb: "Save changes",     owner: "grace" },
  cancel_calendar_event:         { icon: "trash",    label: "Cancel calendar event",        description: "Cancels the event and tells every attendee it is off.",           verb: "Cancel event",     owner: "grace", destructive: true },
  respond_to_calendar_invitation:{ icon: "calendar", label: "Respond to invitation",        description: "Sends your RSVP to the organiser.",                               verb: "Send response",    owner: "grace" },
  /* Externally visible comments / publishing */
  add_jira_comment:              { icon: "comment",  label: "Comment on Jira issue",        description: "Visible to every watcher on the issue, and notifies them.",       verb: "Post comment",     owner: "maya" },
  create_attio_comment:          { icon: "comment",  label: "Comment on CRM record",        description: "Visible to everyone collaborating on the record.",                verb: "Post comment",     owner: "aurther" },
  publish_google_form:           { icon: "globe",    label: "Publish form publicly",        description: "Makes the form live and answerable by anyone with the link.",     verb: "Publish form",     owner: "grace" },
  /* Drive sharing — grants access to outside parties */
  share_drive_file:              { icon: "share",    label: "Share Drive file",             description: "Grants the named people access to this file.",                    verb: "Share file",       owner: "grace" },
  bulk_share_drive_files:        { icon: "share",    label: "Share several Drive files",    description: "Grants access to every file listed below at once.",               verb: "Share files",      owner: "grace" },
  share_drive_file_with_anyone:  { icon: "globe",    label: "Make file public",             description: "Anyone with the link will be able to open this file.",            verb: "Make public",      owner: "grace" },
  /* Destructive mutations */
  transition_jira_issue:         { icon: "ticket",   label: "Move Jira issue",              description: "Changes the issue's status and notifies its watchers.",           verb: "Move issue",       owner: "maya" },
  delete_jira_issue:             { icon: "trash",    label: "Delete Jira issue",            description: "Permanently deletes the ticket and its history.",                 verb: "Delete issue",     owner: "maya",    destructive: true },
  trash_drive_file:              { icon: "trash",    label: "Move Drive file to trash",     description: "Removes the file from its current location.",                     verb: "Move to trash",    owner: "grace",   destructive: true },
  delete_attio_record:           { icon: "trash",    label: "Delete CRM record",            description: "Permanently deletes the record and everything attached to it.",   verb: "Delete record",    owner: "aurther", destructive: true },
  delete_attio_note:            { icon: "trash",    label: "Delete CRM note",              description: "Permanently deletes this note.",                                  verb: "Delete note",      owner: "aurther", destructive: true },
  delete_attio_task:            { icon: "trash",    label: "Delete CRM task",              description: "Permanently deletes this task.",                                  verb: "Delete task",      owner: "aurther", destructive: true },
  delete_attio_list_entry:      { icon: "trash",    label: "Remove from CRM list",         description: "Removes this entry from the list.",                                verb: "Remove entry",     owner: "aurther", destructive: true },
  delete_form_item:             { icon: "trash",    label: "Delete form item",             description: "Permanently removes this question from the form.",                 verb: "Delete item",      owner: "grace",   destructive: true },
  /* Not gated today, but the card is reused for them if that changes */
  create_jira_issue:            { icon: "ticket",    label: "Create Jira issue",            description: "Opens a new ticket in the project.",                               verb: "Create issue",     owner: "maya" },
  update_jira_issue:            { icon: "edit",      label: "Update Jira issue",            description: "Modifies an existing ticket.",                                     verb: "Save changes",     owner: "maya" },
};

/** The headline sentence's opening clause, per tool.
 *
 *  A generic transformation of `label` cannot produce readable English — "Send
 *  email" + "to" + recipient is fine, but "Post to Slack channel" + "to" is
 *  not, and a delete reads best when the target is the grammatical object
 *  ("Delete the CRM record …") rather than something appended after a
 *  preposition. So the connector is baked into the phrase and the target is
 *  appended verbatim. `trimDangling` removes the connector when the payload
 *  turns out to carry no target at all, so the sentence never trails off.
 *
 *  Keys are the same backend tool names as TOOL_META — never renamed. */
const TOOL_PHRASE: Record<string, string> = {
  send_research_email:            "Send an email to",
  schedule_research_email:        "Schedule an email to",
  send_slack_message:             "Post a message to",
  reply_to_slack_thread:          "Reply to a thread in",
  send_slack_dm:                  "Send a direct message to",
  send_slack_ephemeral_message:   "Show a private message to",
  schedule_slack_message:         "Schedule a message to",
  update_slack_message:           "Rewrite a message already posted in",
  upload_slack_file:              "Upload a file to",
  delete_slack_message:           "Delete a message from",
  delete_scheduled_slack_message: "Cancel a scheduled message in",
  create_calendar_event:          "Create a calendar event and invite",
  update_calendar_event:          "Change a calendar event and notify",
  cancel_calendar_event:          "Cancel a calendar event and notify",
  respond_to_calendar_invitation: "Send an RSVP for",
  add_jira_comment:               "Post a public comment on",
  create_attio_comment:           "Post a comment on",
  publish_google_form:            "Publish the form",
  share_drive_file:               "Share a Drive file with",
  bulk_share_drive_files:         "Share several Drive files with",
  share_drive_file_with_anyone:   "Make public the Drive file",
  transition_jira_issue:          "Move the Jira issue",
  delete_jira_issue:              "Delete the Jira issue",
  trash_drive_file:               "Move to trash the Drive file",
  delete_attio_record:            "Delete the CRM record",
  delete_attio_note:              "Delete the CRM note",
  delete_attio_task:              "Delete the CRM task",
  delete_attio_list_entry:        "Remove from its CRM list the entry",
  delete_form_item:               "Delete the form question",
  create_jira_issue:              "Create a Jira issue in",
  update_jira_issue:              "Update the Jira issue",
};

/** `delete_attio_list_entry` → "Delete attio list entry". Last-resort label for
 *  a gated tool nobody added to the registry — still far better than the raw
 *  identifier, and it flags itself as unrecognised in the description. */
function fallbackMeta(tool: string): ToolMeta {
  const words = tool.replace(/_/g, " ").trim();
  const label = words.charAt(0).toUpperCase() + words.slice(1);
  const destructive = /(^|_)(delete|remove|trash|cancel|purge|revoke)/.test(tool);
  return {
    icon: destructive ? "trash" : "bolt",
    label: label || tool,
    description: "Review the details below before allowing this.",
    verb: destructive ? "Delete" : "Allow",
    destructive,
  };
}

const RISK: Record<string, { badge: string; tone: string }> = {
  high:   { badge: "High risk", tone: C.red },
  medium: { badge: "Review",    tone: C.amber },
  low:    { badge: "Routine",   tone: C.accent },
};

/* ── Field schema ───────────────────────────────────────────────────────
   The schema is derived from the args ACTUALLY PRESENT on the interrupt, not
   declared per tool. Two reasons: the backend signatures drift (an optional
   `cc` appears the day someone adds it), and a per-tool table would silently
   drop any arg it had not heard of — the one failure mode an approval gate
   cannot have. So the hints below are keyed by ARG NAME, shared across every
   tool that uses that name, and anything unrecognised still gets a labelled
   input with its kind inferred from the value.

   `role` drives prominence and the headline sentence; `kind` drives the input.
   `mono` marks opaque machine identifiers, which get a monospace field so an
   approver can see they are not meant to be prose. */

type FieldKind = "line" | "prose" | "list" | "bool" | "number" | "locked";
type FieldRole = "recipient" | "channel" | "subject" | "when" | "body" | "detail";

interface FieldHint {
  label: string;
  kind: FieldKind;
  role: FieldRole;
  mono?: boolean;
  hint?: string;
}

const FIELD_HINTS: Record<string, FieldHint> = {
  /* Who it goes to — the block the approver actually checks.
     `kind` encodes the BACKEND's arity, not a display preference. Email's
     `to_email`, Slack's `user_email` and Drive's `email` are all singular `str`
     parameters, so they stay single-valued: turning one into a list editor
     would let an approver add a second address that then gets joined into one
     string and silently mis-delivered. Only genuinely multi-valued args get
     the add/remove list editor. */
  to_email:        { label: "To",          kind: "line", role: "recipient" },
  to:              { label: "To",          kind: "list", role: "recipient" },
  recipient:       { label: "To",          kind: "line", role: "recipient" },
  recipients:      { label: "To",          kind: "list", role: "recipient" },
  email:           { label: "To",          kind: "line", role: "recipient" },
  emails:          { label: "To",          kind: "list", role: "recipient" },
  email_address:   { label: "To",          kind: "line", role: "recipient" },
  email_addresses: { label: "To",          kind: "list", role: "recipient" },
  cc:              { label: "Cc",          kind: "list", role: "recipient" },
  bcc:             { label: "Bcc",         kind: "list", role: "recipient" },
  attendees:            { label: "Attendees", kind: "list", role: "recipient" },
  attendee_emails:      { label: "Attendees", kind: "list", role: "recipient" },
  attendees_emails_json:{ label: "Attendees", kind: "list", role: "recipient" },
  guests:          { label: "Guests",      kind: "list", role: "recipient" },
  shared_with:     { label: "Share with",  kind: "list", role: "recipient" },
  user:            { label: "To",          kind: "line", role: "recipient" },
  user_id:         { label: "To",          kind: "line", role: "recipient", mono: true, hint: "Slack user identifier" },
  user_email:      { label: "To",          kind: "line", role: "recipient" },
  users:           { label: "To",          kind: "list", role: "recipient" },
  /* NOT a recipient: `respond_to_calendar_invitation` uses this for the person
     doing the replying. Treating it as the destination made the headline read
     "Send an RSVP for you@example.com" — the RSVP goes to the organiser. */
  self_email:      { label: "Replying as", kind: "line", role: "detail" },

  /* Where it gets posted. */
  channel:         { label: "Channel",     kind: "line", role: "channel" },
  channel_id:      { label: "Channel",     kind: "line", role: "channel" },
  channel_name:    { label: "Channel",     kind: "line", role: "channel" },
  conversation_id: { label: "Conversation", kind: "line", role: "channel", mono: true },

  /* The one-line gist. */
  subject:         { label: "Subject",     kind: "line", role: "subject" },
  title:           { label: "Title",       kind: "line", role: "subject" },
  summary:         { label: "Title",       kind: "line", role: "subject" },
  name:            { label: "Name",        kind: "line", role: "subject" },
  event_summary:   { label: "Title",       kind: "line", role: "subject" },

  /* When. */
  start:           { label: "Starts",      kind: "line", role: "when" },
  end:             { label: "Ends",        kind: "line", role: "when" },
  start_time:      { label: "Starts",      kind: "line", role: "when" },
  end_time:        { label: "Ends",        kind: "line", role: "when" },
  start_time_iso:  { label: "Starts",      kind: "line", role: "when" },
  end_time_iso:    { label: "Ends",        kind: "line", role: "when" },
  start_datetime:  { label: "Starts",      kind: "line", role: "when" },
  end_datetime:    { label: "Ends",        kind: "line", role: "when" },
  when:            { label: "When",        kind: "line", role: "when" },
  date:            { label: "Date",        kind: "line", role: "when" },
  time:            { label: "Time",        kind: "line", role: "when" },
  send_at:         { label: "Send at",     kind: "line", role: "when" },
  schedule_at:     { label: "Send at",     kind: "line", role: "when" },
  post_at:         { label: "Post at",     kind: "line", role: "when", hint: "Unix timestamp, in seconds" },
  scheduled_time:  { label: "Send at",     kind: "line", role: "when" },
  schedule_time:   { label: "Send at",     kind: "line", role: "when" },
  timezone:        { label: "Time zone",   kind: "line", role: "when" },
  timezone_str:    { label: "Time zone",   kind: "line", role: "when" },
  duration_minutes:{ label: "Length (min)", kind: "number", role: "when" },

  /* The prose. A textarea sized for real writing, not a one-line input. */
  body:            { label: "Message",     kind: "prose", role: "body" },
  message:         { label: "Message",     kind: "prose", role: "body" },
  text:            { label: "Message",     kind: "prose", role: "body" },
  content:         { label: "Message",     kind: "prose", role: "body" },
  research_content:{ label: "Message",     kind: "prose", role: "body" },
  comment:         { label: "Comment",     kind: "prose", role: "body" },
  initial_comment: { label: "Comment",     kind: "prose", role: "body" },
  body_text:       { label: "Message",     kind: "prose", role: "body" },
  html_body:       { label: "Message (HTML)", kind: "prose", role: "body" },
  message_body:    { label: "Message",     kind: "prose", role: "body" },
  description:     { label: "Description", kind: "prose", role: "body" },
  agenda:          { label: "Agenda",      kind: "prose", role: "body" },
  notes:           { label: "Notes",       kind: "prose", role: "body" },

  /* Everything else worth naming properly. */
  location:        { label: "Location",    kind: "line", role: "detail" },
  role:            { label: "Access level", kind: "line", role: "detail" },
  permission:      { label: "Access level", kind: "line", role: "detail" },
  status:          { label: "New status",  kind: "line", role: "detail" },
  response:        { label: "Your reply",  kind: "line", role: "detail" },
  response_status: { label: "Your reply",  kind: "line", role: "detail" },
  rsvp:            { label: "Your reply",  kind: "line", role: "detail" },
  file_path:       { label: "File",        kind: "line", role: "detail" },
  filename:        { label: "File",        kind: "line", role: "detail" },
  file_name:       { label: "File",        kind: "line", role: "detail" },
  attachment_paths:{ label: "Attachments", kind: "list", role: "detail" },
  query:           { label: "Search query", kind: "line", role: "detail" },
  labels:          { label: "Labels",      kind: "list", role: "detail" },
  priority:        { label: "Priority",    kind: "line", role: "detail" },
  issue_type:      { label: "Issue type",  kind: "line", role: "detail" },
  assignee:        { label: "Assignee",    kind: "line", role: "detail" },
  assignee_email:  { label: "Assignee",    kind: "line", role: "detail" },
  notify:          { label: "Notify people", kind: "bool", role: "detail" },
  notify_attendees:{ label: "Notify attendees", kind: "bool", role: "detail" },
  send_notification: { label: "Notify people", kind: "bool", role: "detail" },
  send_updates:    { label: "Notify people", kind: "line", role: "detail" },
  delete_subtasks: { label: "Delete subtasks too", kind: "bool", role: "detail" },
  allow_self:      { label: "Allow sending to yourself", kind: "bool", role: "detail" },
  index:           { label: "Question",    kind: "number", role: "detail", hint: "Position in the form, counting from 0" },
  max_files:       { label: "Limit",       kind: "number", role: "detail" },

  /* Opaque identifiers. Shown, editable, but never dressed up as prose. */
  thread_ts:       { label: "Thread",          kind: "line", role: "detail", mono: true, hint: "Slack thread identifier" },
  message_ts:      { label: "Message",         kind: "line", role: "detail", mono: true, hint: "Slack message identifier" },
  timestamp:       { label: "Message",         kind: "line", role: "detail", mono: true, hint: "Slack message identifier" },
  ts:              { label: "Message",         kind: "line", role: "detail", mono: true, hint: "Slack message identifier" },
  scheduled_message_id: { label: "Scheduled message", kind: "line", role: "detail", mono: true },
  event_id:        { label: "Event",           kind: "line", role: "detail", mono: true },
  calendar_id:     { label: "Calendar",        kind: "line", role: "detail", mono: true },
  file_id:         { label: "File",            kind: "line", role: "detail", mono: true },
  file_ids:        { label: "Files",           kind: "list", role: "detail", mono: true },
  record_id:       { label: "Record",          kind: "line", role: "detail", mono: true },
  note_id:         { label: "Note",            kind: "line", role: "detail", mono: true },
  task_id:         { label: "Task",            kind: "line", role: "detail", mono: true },
  entry_id:        { label: "Entry",           kind: "line", role: "detail", mono: true },
  list_id:         { label: "List",            kind: "line", role: "detail", mono: true },
  list_slug_or_id: { label: "List",            kind: "line", role: "detail" },
  item_id:         { label: "Question",        kind: "line", role: "detail", mono: true },
  form_id:         { label: "Form",            kind: "line", role: "detail", mono: true },
  issue_key:       { label: "Issue",           kind: "line", role: "detail" },
  issue_id:        { label: "Issue",           kind: "line", role: "detail", mono: true },
  object:          { label: "Record type",     kind: "line", role: "detail" },
  object_id:       { label: "Record type",     kind: "line", role: "detail", mono: true },
  object_slug:     { label: "Record type",     kind: "line", role: "detail" },
  author_id:       { label: "Author",          kind: "line", role: "detail", mono: true },
  parent_key:      { label: "Parent issue",    kind: "line", role: "detail" },
  project_key:     { label: "Project",         kind: "line", role: "detail" },
  transition:      { label: "Move to",         kind: "line", role: "detail" },
  parent_id:       { label: "Parent",          kind: "line", role: "detail", mono: true },
};

/** `html_body` → "Html body". Used when an arg has no hint entry. */
function humanize(key: string): string {
  const spaced = key
    .replace(/[_\-.]+/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .trim();
  if (!spaced) return key;
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function isPrimitive(v: unknown): v is string | number | boolean {
  return typeof v === "string" || typeof v === "number" || typeof v === "boolean";
}

/** An array we can safely round-trip through a list editor. An array of objects
 *  is NOT one — flattening it to text would destroy structure the tool needs,
 *  so it stays read-only and points at the raw payload editor. */
function isFlatList(v: unknown): v is (string | number)[] {
  return Array.isArray(v) && v.every((x) => typeof x === "string" || typeof x === "number");
}

/** Kind inferred from the value, for args with no hint entry. `locked` means
 *  "structured — show it, do not pretend a single input can edit it". */
function inferKind(v: unknown): FieldKind {
  if (typeof v === "boolean") return "bool";
  if (typeof v === "number") return "number";
  if (typeof v === "string") return v.includes("\n") || v.length > 140 ? "prose" : "line";
  if (isFlatList(v)) return "list";
  if (v === null || v === undefined) return "line";
  return "locked";
}

interface Field {
  /** The EXACT arg key. Submitted back under this name, never renamed. */
  key: string;
  label: string;
  kind: FieldKind;
  role: FieldRole;
  mono?: boolean;
  hint?: string;
  /** True when the incoming value was a real array. Drives serialisation: a
   *  `to` that arrived as "a@x, b@y" must go back as a string, not an array,
   *  or the tool's `str` parameter gets a list and fails after approval. */
  wasArray: boolean;
}

const ROLE_RANK: Record<FieldRole, number> = {
  recipient: 0, channel: 1, subject: 2, when: 3, body: 4, detail: 5,
};

function buildFields(args: Record<string, unknown>): Field[] {
  const fields = Object.entries(args).map(([key, value], index) => {
    const hint = FIELD_HINTS[key.toLowerCase()];
    const inferred = inferKind(value);
    // A hint's `kind` is a strong signal, but the value wins when it is
    // structured: a `body` that arrived as an object cannot go in a textarea.
    const kind: FieldKind = inferred === "locked"
      ? "locked"
      : hint
        ? (hint.kind === "list" && !isFlatList(value) && typeof value !== "string" ? inferred : hint.kind)
        : inferred;
    return {
      key,
      label: hint?.label ?? humanize(key),
      kind,
      role: hint?.role ?? "detail",
      mono: hint?.mono,
      hint: hint?.hint,
      wasArray: Array.isArray(value),
      index,
    };
  });
  // Stable sort: role priority first, original arg order within a role.
  return fields
    .sort((a, b) => ROLE_RANK[a.role] - ROLE_RANK[b.role] || a.index - b.index)
    .map(({ index: _index, ...f }) => f);
}

/* ── Value coercion ─────────────────────────────────────────────────────
   `toList` is for RENDERING only, and `fromList` runs only inside an onChange.
   That asymmetry is deliberate: a field the approver never touched keeps its
   incoming value byte-for-byte, so "approved without editing" never sends a
   payload that differs from the one the model proposed by a stray space. */

/** `toList` is for RENDERING only, and `fromList` runs only inside an onChange.
 *  That asymmetry is deliberate: a field the approver never touched keeps its
 *  incoming value byte-for-byte, so "approved without editing" never sends a
 *  payload that differs from the one the model proposed by a stray space.
 *
 *  `keepBlanks` is what makes a freshly added row typeable. A multi-value arg
 *  backed by a STRING round-trips through join+split on every keystroke, so a
 *  new empty row would be filtered straight back out and the input would
 *  disappear as it was added. The editors pass true; display paths and
 *  `normalizeArgs` pass false, so blank rows never reach a chip or the wire. */
function toList(v: unknown, keepBlanks = false): string[] {
  if (Array.isArray(v)) {
    const mapped = v.map((x) => (typeof x === "string" ? x : String(x)));
    return keepBlanks ? mapped : mapped.filter((s) => s.trim());
  }
  if (typeof v === "string") {
    const t = v.trim();
    // A present-but-empty arg shows one blank row to fill while editing, and
    // nothing at all while reading. Returning [] to an editor here would also
    // strand the FIRST added row: `[""]` joins back to `""`, so the round-trip
    // is lossy at zero and the input could never appear.
    if (!t) return keepBlanks ? [""] : [];
    // The calendar attendees arg (`attendees_emails_json`) can arrive as a
    // JSON-array STRING — '["a@x.com","b@y.com"]'. Splitting that on its commas
    // would produce broken fragments, so parse it as JSON first.
    if (t.startsWith("[") && t.endsWith("]")) {
      try {
        const parsed = JSON.parse(t);
        if (isFlatList(parsed)) {
          const mapped = parsed.map(String);
          return keepBlanks ? mapped : mapped.map((s) => s.trim()).filter(Boolean);
        }
      } catch { /* not JSON — fall through to delimiter split */ }
    }
    if (!/[,;]/.test(t)) return [t];
    const parts = v.split(/[,;]/).map((s) => s.trim());
    return keepBlanks ? parts : parts.filter(Boolean);
  }
  if (v === null || v === undefined) return [];
  return [String(v)];
}

function fromList(list: string[], wasArray: boolean): string[] | string {
  // Empty rows are kept here so a freshly added recipient can be typed into;
  // `normalizeArgs` strips them at submit time.
  return wasArray ? list : list.join(", ");
}

function asText(v: unknown): string {
  if (typeof v === "string") return v;
  if (v === null || v === undefined) return "";
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return renderValue(v);
}

/** A Unix timestamp is not something a human can check, and `post_at` on a
 *  scheduled Slack message is exactly the fact an approver needs to verify.
 *
 *  Rendered in UTC via the getUTC* accessors rather than `toLocaleString`: the
 *  output is then identical on the server and in the browser, so it cannot
 *  produce a hydration mismatch, and it states its zone instead of leaving the
 *  reader to guess. ISO strings are deliberately NOT reformatted — a calendar
 *  event sends a local wall time with the zone in a separate `timezone_str`
 *  arg, so restating it in UTC would misreport the meeting time. Display only;
 *  the submitted value is never touched. */
function epochLabel(v: unknown): string | undefined {
  const raw =
    typeof v === "number" ? v
    : typeof v === "string" && /^\d{10,13}$/.test(v.trim()) ? Number(v.trim())
    : NaN;
  if (!Number.isFinite(raw)) return undefined;
  // 10-digit values are seconds, 13-digit are milliseconds.
  const ms = raw >= 1e12 ? raw : raw >= 1e9 ? raw * 1000 : NaN;
  if (!Number.isFinite(ms)) return undefined;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return undefined;
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`;
}

/** Drop the blank rows the list editor tolerates while typing. Runs once, on
 *  approve, so the tool never receives `to: ["real@x", ""]`. Routes through
 *  `toList` so a JSON-array string is not mangled, and preserves each value's
 *  CURRENT container shape — a string arg stays a string, an array stays an
 *  array — because the backend signatures are typed and a list handed to a
 *  `str` parameter fails only after the human has already approved. */
function normalizeArgs(values: Record<string, unknown>, fields: Field[]): Record<string, unknown> {
  const listKeys = new Set(fields.filter((f) => f.kind === "list").map((f) => f.key));
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(values)) {
    if (listKeys.has(key) && (Array.isArray(value) || typeof value === "string")) {
      const cleaned = toList(value).map((s) => s.trim()).filter(Boolean);
      out[key] = Array.isArray(value) ? cleaned : cleaned.join(", ");
      continue;
    }
    out[key] = value;
  }
  return out;
}

/** Render a value for review. Objects are pretty-printed; nothing is truncated
 *  — the whole point of this card is that the reviewer sees what they approve. */
function renderValue(v: unknown): string {
  if (typeof v === "string") return v;
  if (v === null || v === undefined) return String(v);
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

/* ── The headline sentence ──────────────────────────────────────────────
   "Send an email to abasi@10alytics.com — subject: World News Brief".
   Assembled from the tool's phrase plus the values already in the payload, so
   it can never describe an action other than the one about to run.

   Two classes of trailing connector, because they accept different things:
   a PEOPLE connector ("… to", "… and notify") can only be followed by an
   addressee, so when the payload has none it is trimmed off rather than
   filled with a record id — `update_calendar_event` takes no attendees
   parameter at all, and "Change a calendar event and notify 3f9a2c" is worse
   than saying nothing. A PLACE connector ("… in", "… on") and a phrase that
   simply ends in a noun both accept an identifier. */
const PEOPLE_CONNECTOR = /\s+(?:to|with|and invite|and notify)$/i;
const PLACE_CONNECTOR = /\s+(?:in|on|for)$/i;

/** Args that identify WHAT is being acted on, best first. Deliberately excludes
 *  `summary`/`title`/`subject`: those read better as the qualifier after the
 *  dash, and using one as the target produces "Create a Jira issue in Fix the
 *  login bug" where the connector wanted a project. */
const IDENTIFIER_PRIORITY = [
  "record_id", "note_id", "task_id", "entry_id", "issue_key", "issue_id",
  "file_id", "file_ids", "event_id", "index", "item_id", "form_id",
  "scheduled_message_id", "timestamp", "message_ts", "ts",
  "list_slug_or_id", "list_id", "project_key", "parent_key",
];

/** Outcome args worth putting in the headline when there is no subject or time
 *  — the new status of an issue, the access level being granted, the RSVP. */
const QUALIFIER_KEYS = new Set([
  "status", "transition", "response_status", "response", "rsvp",
  "role", "permission", "location", "object_slug", "object",
]);

function hasText(v: unknown): boolean {
  return isPrimitive(v) && String(v).trim().length > 0;
}

/** The single field that best names the thing being acted on. */
function primaryTarget(fields: Field[], values: Record<string, unknown>): Field | undefined {
  for (const key of IDENTIFIER_PRIORITY) {
    const f = fields.find((x) => x.key === key && hasText(values[x.key]));
    if (f) return f;
  }
  return fields.find((f) => f.role === "detail" && hasText(values[f.key]));
}

/** Slack channels read as "#social" even when the arg is bare "social". Purely
 *  a display prefix — the submitted value is never rewritten. */
function channelLabel(v: string): string {
  const t = v.trim();
  if (!t || t.startsWith("#") || t.startsWith("@")) return t;
  // C0XXXX / D0XXXX ids are not names; do not pretend they are.
  if (/^[CDGU][A-Z0-9]{6,}$/.test(t)) return t;
  return `#${t}`;
}

interface Sentence {
  /** Opening clause — "Send an email to". Never contains payload text. */
  lead: string;
  /** Individually rendered target chips, or a "3 recipients" stand-in. */
  targets: string[];
  /** Set when `targets` was collapsed to a count, so the block below carries
   *  the real list and the headline stays one readable line. */
  collapsed: boolean;
  /** "subject: World News Brief" — the one qualifier worth the headline. */
  qualifier?: { label: string; value: string };
}

function buildSentence(
  tool: string,
  meta: ToolMeta,
  fields: Field[],
  values: Record<string, unknown>,
): Sentence {
  const phrase = TOOL_PHRASE[tool] ?? meta.label;

  // Who/where it goes. Recipients beat channels (ROLE_RANK already ordered
  // them), and the first populated one wins so Cc never displaces To.
  const addressFields = fields.filter((f) => f.role === "recipient" || f.role === "channel");
  let targets: string[] = [];
  let isChannel = false;
  for (const f of addressFields) {
    const list = toList(values[f.key]);
    if (list.length) {
      targets = list;
      isChannel = f.role === "channel";
      break;
    }
  }
  if (isChannel) targets = targets.map(channelLabel);

  let lead = phrase;
  let identifier: Field | undefined;

  if (!targets.length) {
    identifier = primaryTarget(fields, values);
    if (PEOPLE_CONNECTOR.test(phrase)) {
      // Nobody to address. Drop the connector; the identifier becomes the
      // qualifier below rather than pretending to be a person.
      lead = phrase.replace(PEOPLE_CONNECTOR, "");
    } else if (identifier) {
      targets = [String(values[identifier.key]).trim()];
      identifier = undefined; // consumed as the target
    } else if (PLACE_CONNECTOR.test(phrase)) {
      lead = phrase.replace(PLACE_CONNECTOR, "");
    }
  }

  const collapsed = targets.length > 2;
  const shown = collapsed
    ? [`${targets.length} ${isChannel ? "channels" : "recipients"}`]
    : targets;

  /* Qualifier: the subject line, else the start time, else an outcome like the
     new issue status — and failing all of those, whatever identifies the
     record, so a sentence with no addressee still says what it is acting on. */
  const usedAsTarget = new Set(shown.map((s) => s.trim().toLowerCase()));
  const unused = (f: Field) =>
    hasText(values[f.key]) && !usedAsTarget.has(String(values[f.key]).trim().toLowerCase());

  const qualField =
    fields.find((f) => f.role === "subject" && unused(f)) ??
    fields.find((f) => f.role === "when" && unused(f)) ??
    fields.find((f) => QUALIFIER_KEYS.has(f.key) && unused(f)) ??
    (identifier && unused(identifier) ? identifier : undefined);

  return {
    lead,
    targets: shown,
    collapsed,
    qualifier: qualField
      ? {
          label: qualField.label.toLowerCase(),
          value:
            (qualField.role === "when" ? epochLabel(values[qualField.key]) : undefined) ??
            String(values[qualField.key]).trim(),
        }
      : undefined,
  };
}

export interface ApprovalCardProps {
  interrupt: InterruptState;
  onApprove: (editedArgs?: Record<string, unknown>) => Promise<void>;
  onReject: () => Promise<void>;
  disabled?: boolean;
}

export default function ApprovalCard({
  interrupt,
  onApprove,
  onReject,
  disabled = false,
}: ApprovalCardProps) {
  const meta = TOOL_META[interrupt.tool] ?? fallbackMeta(interrupt.tool);
  const risk = RISK[interrupt.risk] ?? RISK.medium;
  // A destructive tool is treated as high-risk regardless of what the backend
  // graded it — the classifier is heuristic, the irreversibility is a fact.
  const tone = meta.destructive ? C.red : risk.tone;
  const badge = meta.destructive ? "Irreversible" : risk.badge;

  const originalArgs = useMemo(
    () => (interrupt.args ?? {}) as Record<string, unknown>,
    [interrupt.args],
  );
  const fields = useMemo(() => buildFields(originalArgs), [originalArgs]);
  const hasArgs = fields.length > 0;

  const [phase, setPhase] = useState<"pending" | "approving" | "rejecting">("pending");
  const [editMode, setEditMode] = useState(false);
  /* `values` is the single source of truth for what will be submitted. Both
     editors — the per-field inputs and the raw JSON textarea — write into it,
     so the two views can never disagree about what is about to happen. */
  const [values, setValues] = useState<Record<string, unknown>>(() => ({ ...originalArgs }));
  const [rawText, setRawText] = useState(() => JSON.stringify(originalArgs, null, 2));
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [rawOpen, setRawOpen] = useState(false);

  const isActing = phase !== "pending";
  const locked = disabled || isActing;

  /* A different tool can pause the run while this card is on screen (an
     approval chain across a long multi-step task reuses this component). Reset
     every editor so the previous action's edits are never carried over into
     the next decision. */
  const toolKey = `${interrupt.agentMsgId}:${interrupt.tool}`;
  useEffect(() => {
    setPhase("pending");
    setEditMode(false);
    setJsonError(null);
    setValues({ ...originalArgs });
    setRawText(JSON.stringify(originalArgs, null, 2));
    setRawOpen(false);
    // `originalArgs` is derived from the same interrupt identity as toolKey.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toolKey]);

  /* The card blocks the entire conversation, so move focus to it and announce
     it. Focus lands on the container, never on a decision button — nothing
     here should be approvable or rejectable by a stray Enter keypress. */
  const cardRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    cardRef.current?.focus({ preventScroll: false });
  }, [toolKey]);

  /** Write one arg. Keeps the raw JSON view in step unless it currently holds
   *  text that does not parse — that text is the developer's, and silently
   *  overwriting it would discard work. Approve stays blocked until it parses. */
  const setArg = useCallback((key: string, value: unknown) => {
    setValues((prev) => {
      const next = { ...prev, [key]: value };
      if (!jsonError) setRawText(JSON.stringify(next, null, 2));
      return next;
    });
  }, [jsonError]);

  /* Raw JSON edits flow straight into `values` whenever they parse, which is
     what keeps one source of truth across both editors. */
  const onRawChange = useCallback((text: string) => {
    setRawText(text);
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      setJsonError("That is not valid JSON. Fix it, or press Reset, before approving.");
      return;
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      // The tool is called with **args, so anything but an object would fail
      // server-side after the approval had already been given.
      setJsonError("The payload must be a JSON object, e.g. { \"to\": \"…\" }.");
      return;
    }
    setJsonError(null);
    setValues(parsed as Record<string, unknown>);
  }, []);

  const resetRaw = useCallback(() => {
    setValues({ ...originalArgs });
    setRawText(JSON.stringify(originalArgs, null, 2));
    setJsonError(null);
  }, [originalArgs]);

  /* What actually gets submitted, and whether it differs from what the model
     proposed. Both sides are normalised before the comparison, so neither a
     blank recipient row left empty nor a purely cosmetic whitespace difference
     in a comma-joined list counts as an edit — "approved without touching
     anything" must always send `undefined`, never a reformatted payload. */
  const submission = useMemo(() => {
    const normalized = normalizeArgs(values, fields);
    const baseline = normalizeArgs(originalArgs, fields);
    const dirty = JSON.stringify(normalized) !== JSON.stringify(baseline);
    return { normalized, dirty };
  }, [values, fields, originalArgs]);

  const handleApprove = useCallback(async () => {
    if (locked) return;
    if (jsonError) {
      // Raw payload is mid-edit and unparseable; approving would send the last
      // good state and quietly drop what is on screen.
      setRawOpen(true);
      return;
    }
    /* CONTRACT: a FLAT args object, same keys as `interrupt.args`, which
       /resume wraps into `edited_action.args` (web_api.py:1226). Nothing
       nested, nothing restructured. `undefined` when nothing was edited —
       identical to what an untouched approval has always sent. */
    const finalArgs = submission.dirty ? submission.normalized : undefined;
    setPhase("approving");
    try {
      await onApprove(finalArgs);
    } catch {
      setPhase("pending");
    }
  }, [locked, jsonError, submission, onApprove]);

  const handleReject = useCallback(async () => {
    if (locked) return;
    setPhase("rejecting");
    try {
      await onReject();
    } catch {
      setPhase("pending");
    }
  }, [locked, onReject]);

  /* No resolved ("approved" / "rejected") render. `resume` clears the pending
     interrupt UP FRONT (useIrisStream.ts:232), which unmounts this component
     before either promise settles — the old card's 25-line resolved-state
     branch was unreachable and only looked live. The in-flight spinner below
     is what the user actually sees, and the catch above restores the buttons
     if the resume call itself fails. */

  const sentence = useMemo(
    () => buildSentence(interrupt.tool, meta, fields, values),
    [interrupt.tool, meta, fields, values],
  );

  const addressFields = fields.filter((f) => f.role === "recipient" || f.role === "channel");
  const detailFields = fields.filter((f) => f.role !== "recipient" && f.role !== "channel");

  /* With no addressee, the action still has a subject: the record it deletes,
     the file it publishes. It gets the same prominence recipients get, because
     it is the thing the approver has to recognise before saying yes. Suppressed
     when the headline already shows that exact value, so the card does not say
     the same id twice in a row. */
  const targetField = !addressFields.length
    ? (() => {
        const f = primaryTarget(fields, values);
        if (!f) return undefined;
        const shown = String(values[f.key]).trim().toLowerCase();
        const inHeadline = sentence.targets.some((t) => t.trim().toLowerCase() === shown);
        return inHeadline ? undefined : f;
      })()
    : undefined;

  const btnBase: React.CSSProperties = {
    flex: 1,
    minHeight: 44, // pointer-target floor; the old buttons were ~33px
    padding: "0 14px",
    borderRadius: 10,
    fontSize: 13.5,
    fontWeight: 600,
    fontFamily: "inherit",
    cursor: locked ? "not-allowed" : "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    gap: 7,
    transition: "background .16s ease, border-color .16s ease, color .16s ease",
  };

  const inputBase: React.CSSProperties = {
    width: "100%",
    boxSizing: "border-box",
    background: C.cardBg,
    border: `1px solid ${C.borderMed}`,
    borderRadius: 8,
    padding: "8px 10px",
    color: C.text,
    fontFamily: "inherit",
    fontSize: 13,
    lineHeight: 1.5,
    outline: "none",
  };

  return (
    <div
      data-hitl
      ref={cardRef}
      tabIndex={-1}
      role="group"
      aria-labelledby="hitl-title"
      aria-describedby="hitl-desc"
      style={{
        width: "100%",
        maxWidth: 620,
        background: C.cardBg,
        border: `1px solid ${C.border}`,
        borderTop: `2px solid ${tone}`,
        borderRadius: 14,
        overflow: "hidden",
        fontFamily: "'DM Sans', system-ui, sans-serif",
        boxShadow: "0 10px 34px rgba(0,0,0,0.28)",
        animation: "hitlIn .32s var(--spring, cubic-bezier(0.34,1.56,0.64,1)) both",
        opacity: isActing ? 0.8 : 1,
        transition: "opacity .2s ease",
        outline: "none",
        // Consumed by the tint rules below. Kept as custom properties rather
        // than inline colors so the @supports fallback can override them.
        ["--hitl-tone" as string]: tone,
        ["--hitl-danger" as string]: C.red,
      } as React.CSSProperties}
    >
      <style>{`
        @keyframes hitlIn   { from { opacity:0; transform:translateY(12px); } to { opacity:1; transform:translateY(0); } }
        @keyframes hitlSpin { to { transform:rotate(360deg); } }
        @keyframes hitlBeacon { 0%,100% { opacity:1; } 50% { opacity:.35; } }
        [data-hitl] button:focus-visible,
        [data-hitl] textarea:focus-visible,
        [data-hitl] input:focus-visible {
          outline: 2px solid ${C.accent};
          outline-offset: 2px;
        }
        [data-hitl] input:focus, [data-hitl] textarea:focus { border-color: ${C.accent}; }
        [data-hitl] button:not(:disabled):active { transform: translateY(1px); }
        [data-hitl] button:disabled { opacity: .55; cursor: not-allowed; }
        [data-hitl] input::placeholder, [data-hitl] textarea::placeholder { color: ${C.muted}; opacity: .8; }

        /* ── Tinted surfaces ──────────────────────────────────────────────
           Next 16 supports Firefox 111+, but color-mix() only landed in
           Firefox 113 — on 111/112 a color-mix background parses as invalid
           and the element would render with NO background at all. So the
           neutral token version ships first and color-mix upgrades it only
           where it is actually supported. */
        [data-hitl] .hitl-tint      { background: ${C.surface2}; border-color: ${C.borderMed}; }
        [data-hitl] .hitl-tint-edge { border-color: ${C.borderMed}; }
        @supports (background: color-mix(in srgb, red 10%, transparent)) {
          [data-hitl] .hitl-badge   { background: color-mix(in srgb, var(--hitl-tone) 12%, transparent);
                                      border-color: color-mix(in srgb, var(--hitl-tone) 38%, transparent); }
          [data-hitl] .hitl-tile    { background: color-mix(in srgb, var(--hitl-tone) 10%, transparent);
                                      border-color: color-mix(in srgb, var(--hitl-tone) 30%, transparent); }
          [data-hitl] .hitl-approve { background: color-mix(in srgb, var(--hitl-tone) 14%, transparent);
                                      border-color: color-mix(in srgb, var(--hitl-tone) 55%, transparent); }
          [data-hitl] .hitl-danger  { background: color-mix(in srgb, var(--hitl-danger) 8%, transparent);
                                      border-color: color-mix(in srgb, var(--hitl-danger) 22%, transparent); }
          [data-hitl] .hitl-reject  { border-color: color-mix(in srgb, var(--hitl-danger) 45%, transparent); }
          [data-hitl] .hitl-who     { background: color-mix(in srgb, var(--hitl-tone) 7%, transparent);
                                      border-color: color-mix(in srgb, var(--hitl-tone) 26%, transparent); }
        }

        .hitl-approve:not(:disabled):hover { filter: brightness(1.14); }
        .hitl-reject:not(:disabled):hover  { background: ${C.surface2}; }
        .hitl-ghost:not(:disabled):hover   { background: ${C.surface2}; }
        .hitl-disclose:hover               { background: ${C.surface2}; }
        .hitl-chipbtn:not(:disabled):hover { background: ${C.surface2}; color: ${C.red}; }
        @media (prefers-reduced-motion: reduce) {
          [data-hitl], [data-hitl] * { animation: none !important; transition: none !important; }
        }
      `}</style>

      {/* Screen readers get told the conversation is blocked, not just that a
          card appeared. Assertive is right here: nothing else can proceed. */}
      <span
        aria-live="assertive"
        style={{
          position: "absolute", width: 1, height: 1, padding: 0, margin: -1,
          overflow: "hidden", clip: "rect(0 0 0 0)", whiteSpace: "nowrap", border: 0,
        }}
      >
        {`IRIS is waiting for your approval to ${meta.label.toLowerCase()}. The conversation is paused.`}
      </span>

      {/* ── Header ────────────────────────────────────────────────────── */}
      <div
        style={{
          display: "flex", alignItems: "center", gap: 8,
          padding: "10px 14px",
          background: C.surface2,
          borderBottom: `1px solid ${C.border}`,
        }}
      >
        <span
          aria-hidden
          style={{
            width: 7, height: 7, borderRadius: "50%", background: tone,
            flexShrink: 0, animation: "hitlBeacon 2s ease-in-out infinite",
          }}
        />
        <span
          style={{
            fontSize: 11, fontWeight: 700, letterSpacing: "0.07em",
            textTransform: "uppercase", color: C.muted,
          }}
        >
          Approval required
        </span>
        <span style={{ flex: 1 }} />
        <span
          className="hitl-tint hitl-badge"
          style={{
            fontSize: 10.5, fontWeight: 700, letterSpacing: "0.04em",
            color: tone,
            borderWidth: 1, borderStyle: "solid",
            borderRadius: 20, padding: "2px 9px", flexShrink: 0,
          }}
        >
          {badge}
        </span>
      </div>

      {/* ── What is about to happen, as a sentence ─────────────────────
          The headline the approver reads. Built from the live payload, so it
          updates as they edit, and every value inside it is inert text. */}
      <div style={{ display: "flex", gap: 12, padding: "14px 14px 10px" }}>
        <div
          className="hitl-tint hitl-tile"
          style={{
            width: 38, height: 38, borderRadius: 10, flexShrink: 0,
            borderWidth: 1, borderStyle: "solid",
            display: "flex", alignItems: "center", justifyContent: "center",
            color: tone,
          }}
        >
          <Icon name={meta.icon} size={18} />
        </div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div
            id="hitl-title"
            style={{ fontSize: 15.5, fontWeight: 600, color: C.text, lineHeight: 1.4 }}
          >
            {sentence.lead}
            {sentence.targets.map((t, i) => (
              <span key={`${t}-${i}`}>
                {i === 0 ? " " : ", "}
                <span style={{ color: tone, wordBreak: "break-word" }}>{t}</span>
              </span>
            ))}
            {sentence.qualifier && (
              <span style={{ color: C.muted, fontWeight: 400 }}>
                {" — "}
                {sentence.qualifier.label}:{" "}
                <span style={{ color: C.text }}>{sentence.qualifier.value}</span>
              </span>
            )}
          </div>
          <div id="hitl-desc" style={{ fontSize: 12.5, color: C.muted, lineHeight: 1.5, marginTop: 4 }}>
            {meta.description}
          </div>
          {meta.owner && (
            <div
              style={{ fontSize: 11, color: C.muted, marginTop: 4, opacity: 0.85 }}
              title={`Requested by the ${meta.owner} specialist`}
            >
              {meta.owner} · {interrupt.tool}
            </div>
          )}
        </div>
      </div>

      {/* ── Who it goes to ─────────────────────────────────────────────
          The most-checked fact on the card, so it sits directly under the
          verb and gives every addressee its own row. A fifth recipient
          nobody noticed is the failure this exists to prevent. */}
      {addressFields.length > 0 && (
        <div style={{ padding: "0 14px 10px" }}>
          {addressFields.map((f) => (
            <AddressBlock
              key={f.key}
              field={f}
              value={values[f.key]}
              tone={tone}
              editable={editMode && !locked}
              onChange={(next) => setArg(f.key, next)}
              inputBase={inputBase}
            />
          ))}
        </div>
      )}

      {/* A delete has no addressee — the target takes the same slot. */}
      {targetField && (
        <div style={{ padding: "0 14px 10px" }}>
          <div
            className="hitl-tint hitl-who"
            style={{
              display: "flex", alignItems: "center", gap: 9,
              borderWidth: 1, borderStyle: "solid", borderRadius: 10,
              padding: "9px 11px",
            }}
          >
            <Icon name="target" size={15} color={tone} />
            <span style={{ fontSize: 11.5, color: C.muted, flexShrink: 0 }}>
              {targetField.label}
            </span>
            <span
              style={{
                fontSize: 13, color: C.text, fontWeight: 500,
                wordBreak: "break-word", minWidth: 0,
                fontFamily: targetField.mono
                  ? "'JetBrains Mono', ui-monospace, monospace"
                  : "inherit",
              }}
            >
              {asText(values[targetField.key])}
            </span>
          </div>
        </div>
      )}

      {/* ── The rest of the action, field by field ─────────────────────
          Always visible: an approval you have to expand a disclosure to read
          is an approval given blind. One scroller, values never clipped. */}
      {detailFields.length > 0 && (
        <div style={{ padding: "0 14px 12px" }}>
          <div
            style={{
              border: `1px solid ${C.border}`, borderRadius: 10,
              maxHeight: 320, overflowY: "auto",
              background: C.surface2,
            }}
          >
            {detailFields.map((f, i) => (
              <FieldRow
                key={f.key}
                field={f}
                value={values[f.key]}
                editable={editMode && !locked}
                last={i === detailFields.length - 1}
                onChange={(next) => setArg(f.key, next)}
                inputBase={inputBase}
              />
            ))}
          </div>
        </div>
      )}

      {!hasArgs && (
        <div style={{ padding: "0 14px 12px", fontSize: 12.5, color: C.muted, lineHeight: 1.5 }}>
          This action carries no details to review.
        </div>
      )}

      {/* ── Advanced — raw payload ─────────────────────────────────────
          Kept, deliberately, and deliberately demoted. A developer sometimes
          has to add a key the form cannot express, or fix a structured value.
          It writes into the same state the fields do, so the two never drift. */}
      {hasArgs && (
        <div style={{ padding: "0 14px 12px" }}>
          <button
            className="hitl-disclose"
            onClick={() => setRawOpen((v) => !v)}
            aria-expanded={rawOpen}
            aria-controls="hitl-raw"
            style={{
              display: "flex", alignItems: "center", gap: 7, width: "100%",
              minHeight: 32, padding: "0 9px",
              background: "transparent",
              border: `1px solid ${jsonError ? C.red : C.border}`,
              borderRadius: 9,
              cursor: "pointer", fontSize: 11.5, color: C.muted,
              fontFamily: "inherit", textAlign: "left",
              transition: "background .15s ease",
            }}
          >
            <svg
              aria-hidden width="11" height="11" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"
              style={{
                transform: rawOpen ? "rotate(90deg)" : "none",
                transition: "transform .2s ease", flexShrink: 0,
              }}
            >
              <polyline points="9 6 15 12 9 18" />
            </svg>
            <Icon name="code" size={12} />
            <span>Advanced — raw payload</span>
            {jsonError && (
              <span style={{ marginLeft: "auto", color: C.red, fontWeight: 600 }}>
                invalid JSON
              </span>
            )}
          </button>

          {rawOpen && (
            <div id="hitl-raw" style={{ marginTop: 8 }}>
              <textarea
                value={rawText}
                onChange={(e) => onRawChange(e.target.value)}
                disabled={locked}
                spellCheck={false}
                aria-label="Edit the action payload as JSON"
                aria-invalid={Boolean(jsonError)}
                style={{
                  width: "100%", minHeight: 150, boxSizing: "border-box",
                  background: C.cardBg,
                  border: `1px solid ${jsonError ? C.red : C.borderMed}`,
                  borderRadius: 10, padding: "10px 12px",
                  color: C.text,
                  fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                  fontSize: 12, lineHeight: 1.6, resize: "vertical", outline: "none",
                }}
              />
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 5 }}>
                {jsonError ? (
                  <div role="alert" style={{ color: C.red, fontSize: 11.5, lineHeight: 1.5, flex: 1 }}>
                    {jsonError}
                  </div>
                ) : (
                  <div style={{ color: C.muted, fontSize: 11.5, lineHeight: 1.5, flex: 1 }}>
                    Exact arguments sent to <code style={{ fontFamily: "'JetBrains Mono', ui-monospace, monospace" }}>{interrupt.tool}</code>.
                  </div>
                )}
                <button
                  className="hitl-ghost"
                  onClick={resetRaw}
                  disabled={locked || !submission.dirty}
                  style={{
                    minHeight: 28, padding: "0 10px", borderRadius: 8,
                    background: "transparent", border: `1px solid ${C.border}`,
                    color: C.muted, fontSize: 11.5, fontFamily: "inherit",
                    cursor: locked ? "not-allowed" : "pointer", flexShrink: 0,
                  }}
                >
                  Reset
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {meta.destructive && (
        <div
          className="hitl-tint hitl-danger"
          style={{
            display: "flex", alignItems: "center", gap: 7,
            margin: "0 14px 12px", padding: "8px 10px",
            borderWidth: 1, borderStyle: "solid",
            borderRadius: 9, fontSize: 12, color: C.text, lineHeight: 1.5,
          }}
        >
          <Icon name="trash" size={14} color={C.red} />
          <span>This cannot be undone.</span>
        </div>
      )}

      {submission.dirty && !jsonError && (
        <div
          style={{
            display: "flex", alignItems: "center", gap: 7,
            margin: "0 14px 12px", padding: "7px 10px",
            border: `1px solid ${C.borderMed}`, background: C.surface2,
            borderRadius: 9, fontSize: 11.5, color: C.muted, lineHeight: 1.5,
          }}
        >
          <Icon name="edit" size={13} color={C.accent} />
          <span>You changed this action. <strong style={{ color: C.text, fontWeight: 600 }}>{meta.verb}</strong> will use your version.</span>
        </div>
      )}

      {/* ── Decision ──────────────────────────────────────────────────── */}
      <div
        style={{
          display: "flex", gap: 8, padding: "11px 14px",
          borderTop: `1px solid ${C.border}`, background: C.surface2,
        }}
      >
        {hasArgs && (
          <button
            className="hitl-ghost"
            onClick={() => setEditMode((v) => !v)}
            disabled={locked}
            aria-pressed={editMode}
            style={{
              ...btnBase,
              flex: "0 0 auto",
              background: editMode ? C.surface2 : "transparent",
              border: `1px solid ${editMode ? C.accent : C.border}`,
              color: editMode ? C.accent : C.muted,
              fontWeight: 500,
            }}
          >
            <Icon name="edit" size={14} />
            {editMode ? "Done editing" : "Edit"}
          </button>
        )}

        {/* Equal width with Approve, and fully legible. Rejecting is the safe
            choice — it must never be the harder button to find or press. */}
        <button
          className="hitl-tint-edge hitl-reject"
          onClick={handleReject}
          disabled={locked}
          style={{
            ...btnBase,
            background: "transparent",
            borderWidth: 1, borderStyle: "solid",
            color: C.red,
          }}
        >
          {phase === "rejecting" ? <Spin color={C.red} /> : "Reject"}
        </button>

        <button
          className="hitl-tint hitl-approve"
          onClick={handleApprove}
          disabled={locked}
          style={{
            ...btnBase,
            borderWidth: 1, borderStyle: "solid",
            color: tone,
          }}
        >
          {phase === "approving" ? <Spin color={tone} /> : (
            <>
              <Icon name={meta.icon} size={14} strokeWidth={2} />
              {meta.verb}
            </>
          )}
        </button>
      </div>
    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════════════
   Field renderers
   ══════════════════════════════════════════════════════════════════════════ */

/** Recipients and channels. Every addressee is its own row — the whole point.
 *  In edit mode each row is its own input with its own remove button, so
 *  correcting the third of four addresses does not mean retyping the line. */
function AddressBlock({
  field, value, tone, editable, onChange, inputBase,
}: {
  field: Field;
  value: unknown;
  tone: string;
  editable: boolean;
  onChange: (next: unknown) => void;
  inputBase: React.CSSProperties;
}) {
  // `list` drives the read view and the count (no blanks); `rows` drives the
  // editor, where a blank row is a row the approver is about to type into.
  const list = toList(value);
  const rows = toList(value, true);
  const isChannel = field.role === "channel";
  const icon: IconName = isChannel ? "hash" : "person";
  // A single-valued arg (`channel`, `user`) must never become an array.
  const singleValued = field.kind !== "list";

  const commit = (next: string[]) => {
    onChange(singleValued ? (next[0] ?? "") : fromList(next, field.wasArray));
  };

  return (
    <div style={{ marginBottom: 8 }}>
      <div
        style={{
          display: "flex", alignItems: "center", gap: 6,
          fontSize: 10.5, fontWeight: 700, letterSpacing: "0.06em",
          textTransform: "uppercase", color: "var(--text-muted, #7E88A8)",
          marginBottom: 5,
        }}
      >
        <span>{field.label}</span>
        {list.length > 1 && (
          <span style={{ fontWeight: 500, letterSpacing: 0, textTransform: "none", opacity: 0.85 }}>
            {list.length} {isChannel ? "channels" : "people"}
          </span>
        )}
      </div>

      {!editable && list.length === 0 && (
        <div style={{ fontSize: 12.5, color: "var(--text-muted, #7E88A8)", fontStyle: "italic" }}>
          none
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
        {editable && singleValued ? (
          /* A single-valued arg always gets exactly ONE input, driven by the
             value itself rather than by a derived list. Deriving it would make
             an empty `to_email` unfixable: the blank row round-trips to a
             zero-length list and the input would disappear as it was added. */
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <Icon name={icon} size={14} color={tone} />
            <input
              value={asText(value)}
              onChange={(e) => onChange(e.target.value)}
              spellCheck={false}
              aria-label={field.label}
              style={{ ...inputBase, fontSize: 12.5, padding: "6px 9px" }}
            />
          </div>
        ) : editable ? (
          rows.map((entry, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <Icon name={icon} size={14} color={tone} />
              <input
                value={entry}
                onChange={(e) => {
                  const next = [...rows];
                  next[i] = e.target.value;
                  commit(next);
                }}
                spellCheck={false}
                aria-label={`${field.label} ${i + 1}`}
                style={{ ...inputBase, fontSize: 12.5, padding: "6px 9px" }}
              />
              <button
                className="hitl-chipbtn"
                onClick={() => commit(rows.filter((_, j) => j !== i))}
                aria-label={`Remove ${field.label} ${i + 1}`}
                style={{
                  width: 26, height: 26, borderRadius: 7, flexShrink: 0,
                  display: "flex", alignItems: "center", justifyContent: "center",
                  background: "transparent",
                  border: `1px solid var(--border, #1E2740)`,
                  color: "var(--text-muted, #7E88A8)", cursor: "pointer",
                }}
              >
                <Icon name="close" size={11} strokeWidth={2.2} />
              </button>
            </div>
          ))
        ) : (
          list.map((entry, i) => (
            <div
              key={i}
              className="hitl-tint hitl-who"
              style={{
                display: "flex", alignItems: "center", gap: 8,
                borderWidth: 1, borderStyle: "solid", borderRadius: 9,
                padding: "7px 10px", minWidth: 0,
              }}
            >
              <Icon name={icon} size={14} color={tone} />
              <span
                style={{
                  fontSize: 13, color: "var(--text, #E8EBF7)", fontWeight: 500,
                  wordBreak: "break-word", minWidth: 0,
                }}
              >
                {isChannel ? channelLabel(entry) : entry}
              </span>
            </div>
          ))
        )}
      </div>

      {editable && !singleValued && (
        <button
          className="hitl-ghost"
          onClick={() => commit([...rows, ""])}
          style={{
            display: "flex", alignItems: "center", gap: 5,
            marginTop: 6, minHeight: 28, padding: "0 9px",
            background: "transparent",
            border: `1px dashed var(--border-med, #222d47)`,
            borderRadius: 8, cursor: "pointer",
            fontSize: 11.5, fontFamily: "inherit",
            color: "var(--text-muted, #7E88A8)",
          }}
        >
          <Icon name="plus" size={12} strokeWidth={2.2} />
          Add {field.label.toLowerCase()}
        </button>
      )}
    </div>
  );
}

/** One non-address arg. Read mode shows the value in full as inert text; edit
 *  mode swaps in the input its kind calls for — a textarea sized for prose,
 *  a checkbox for a flag, a per-row list editor, a monospace box for an id. */
function FieldRow({
  field, value, editable, last, onChange, inputBase,
}: {
  field: Field;
  value: unknown;
  editable: boolean;
  last: boolean;
  onChange: (next: unknown) => void;
  inputBase: React.CSSProperties;
}) {
  const muted = "var(--text-muted, #7E88A8)";
  const text = asText(value);
  const mono = field.mono
    ? "'JetBrains Mono', ui-monospace, monospace"
    : "inherit";

  const label = (
    <label
      htmlFor={editable ? `hitl-f-${field.key}` : undefined}
      style={{
        fontSize: 11, color: muted, lineHeight: 1.6,
        wordBreak: "break-word", fontWeight: 600,
      }}
    >
      {field.label}
      {field.hint && (
        <span style={{ display: "block", fontWeight: 400, opacity: 0.8, fontSize: 10.5 }}>
          {field.hint}
        </span>
      )}
    </label>
  );

  let control: React.ReactNode;

  if (!editable) {
    /* A timestamp gets its readable form FIRST and the exact stored value
       beside it — the approver can check the time, and can still see precisely
       what will be sent. */
    const friendly = field.role === "when" ? epochLabel(value) : undefined;
    control = (
      <span
        style={{
          fontSize: 12.5, color: "var(--text, #E8EBF7)", lineHeight: 1.55,
          whiteSpace: "pre-wrap", wordBreak: "break-word", minWidth: 0,
          fontFamily: friendly ? "inherit" : mono,
        }}
      >
        {friendly ? (
          <>
            {friendly}
            <span style={{ color: muted, fontSize: 11, marginLeft: 6 }}>({text})</span>
          </>
        ) : (
          text || <span style={{ color: muted, fontStyle: "italic" }}>empty</span>
        )}
      </span>
    );
  } else if (field.kind === "locked") {
    /* Structured value. A single input cannot round-trip it without risking
       silent corruption of something the tool depends on, so it stays
       read-only here and the raw editor is named as the way to change it. */
    control = (
      <div style={{ minWidth: 0 }}>
        <span
          style={{
            display: "block", fontSize: 12, color: "var(--text, #E8EBF7)",
            lineHeight: 1.55, whiteSpace: "pre-wrap", wordBreak: "break-word",
            fontFamily: "'JetBrains Mono', ui-monospace, monospace",
          }}
        >
          {text}
        </span>
        <span style={{ display: "block", fontSize: 11, color: muted, marginTop: 4 }}>
          Structured value — change it under “Advanced — raw payload”.
        </span>
      </div>
    );
  } else if (field.kind === "prose") {
    control = (
      <textarea
        id={`hitl-f-${field.key}`}
        value={text}
        onChange={(e) => onChange(e.target.value)}
        spellCheck
        style={{
          ...inputBase,
          minHeight: 170,
          lineHeight: 1.6,
          resize: "vertical",
          fontFamily: field.mono ? mono : "inherit",
        }}
      />
    );
  } else if (field.kind === "bool") {
    control = (
      <label style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12.5, color: "var(--text, #E8EBF7)", cursor: "pointer" }}>
        <input
          id={`hitl-f-${field.key}`}
          type="checkbox"
          checked={value === true}
          onChange={(e) => onChange(e.target.checked)}
          style={{ width: 15, height: 15, accentColor: "var(--accent, #8AB4F8)", cursor: "pointer" }}
        />
        {value === true ? "Yes" : "No"}
      </label>
    );
  } else if (field.kind === "number") {
    control = (
      <input
        id={`hitl-f-${field.key}`}
        type="number"
        value={typeof value === "number" ? value : text}
        onChange={(e) => {
          const n = e.target.value;
          onChange(n === "" ? "" : Number(n));
        }}
        style={{ ...inputBase, maxWidth: 140 }}
      />
    );
  } else if (field.kind === "list") {
    // Edit-only branch, so blanks are kept: a row being typed into is a row.
    const list = toList(value, true);
    control = (
      <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
        {list.map((entry, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input
              value={entry}
              onChange={(e) => {
                const next = [...list];
                next[i] = e.target.value;
                onChange(fromList(next, field.wasArray));
              }}
              spellCheck={false}
              aria-label={`${field.label} ${i + 1}`}
              style={{ ...inputBase, fontSize: 12.5, padding: "6px 9px", fontFamily: mono }}
            />
            <button
              className="hitl-chipbtn"
              onClick={() => onChange(fromList(list.filter((_, j) => j !== i), field.wasArray))}
              aria-label={`Remove ${field.label} ${i + 1}`}
              style={{
                width: 26, height: 26, borderRadius: 7, flexShrink: 0,
                display: "flex", alignItems: "center", justifyContent: "center",
                background: "transparent", border: `1px solid var(--border, #1E2740)`,
                color: muted, cursor: "pointer",
              }}
            >
              <Icon name="close" size={11} strokeWidth={2.2} />
            </button>
          </div>
        ))}
        <button
          className="hitl-ghost"
          onClick={() => onChange(fromList([...list, ""], field.wasArray))}
          style={{
            display: "flex", alignItems: "center", gap: 5, alignSelf: "flex-start",
            minHeight: 28, padding: "0 9px", background: "transparent",
            border: `1px dashed var(--border-med, #222d47)`, borderRadius: 8,
            cursor: "pointer", fontSize: 11.5, fontFamily: "inherit", color: muted,
          }}
        >
          <Icon name="plus" size={12} strokeWidth={2.2} />
          Add
        </button>
      </div>
    );
  } else {
    control = (
      <input
        id={`hitl-f-${field.key}`}
        value={text}
        onChange={(e) => onChange(e.target.value)}
        spellCheck={false}
        style={{ ...inputBase, fontFamily: mono }}
      />
    );
  }

  const stacked = editable && (field.kind === "prose" || field.kind === "locked");

  return (
    <div
      style={{
        display: stacked ? "block" : "grid",
        gridTemplateColumns: stacked ? undefined : "minmax(78px, 104px) 1fr",
        gap: 10,
        padding: "9px 12px",
        borderBottom: last ? "none" : `1px solid var(--border, #1E2740)`,
      }}
    >
      {label}
      {stacked ? <div style={{ marginTop: 5 }}>{control}</div> : control}
    </div>
  );
}

function Spin({ color }: { color: string }) {
  return (
    <span
      aria-hidden
      style={{
        width: 14, height: 14, borderRadius: "50%",
        border: `2px solid ${color}`, borderTopColor: "transparent",
        display: "inline-block", animation: "hitlSpin .7s linear infinite",
      }}
    />
  );
}
