"""slack_tools.py - Comprehensive Slack Tools Suite for IRIS & Sienna Subagent.

Built on official Slack Web API via slack_sdk.web.async_client.AsyncWebClient with
connection and rate-limit retry handlers.
"""

from __future__ import annotations

import os
from typing import Optional, List
from dotenv import load_dotenv
from langchain.tools import tool
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_async_handlers import (
    AsyncConnectionErrorRetryHandler,
    AsyncRateLimitErrorRetryHandler,
)
from slack_sdk.web.async_client import AsyncWebClient

# Local: per-surface output formatting (Markdown → Slack mrkdwn + Block Kit).
from formatting import to_slack_mrkdwn, to_slack_blocks

from idempotency import idempotent

load_dotenv()

_client: AsyncWebClient | None = None


def _get_client() -> AsyncWebClient:
    """Return an AsyncWebClient for Slack Web API operations."""
    token = os.getenv("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("[slack_tools] SLACK_BOT_TOKEN is not set.")

    return AsyncWebClient(
        token=token,
        timeout=15,
    )


def _formatted_payload(message: str) -> dict:
    """Render model-authored Markdown into a Slack-native message payload.

    Returns kwargs for chat_postMessage / chat_update / chat_scheduleMessage /
    chat_postEphemeral: a mrkdwn ``text`` (always present, for notifications and
    accessibility) plus Block Kit ``blocks`` for a clean, sectioned layout.

    Total by construction — the underlying converters never raise, and ``blocks``
    is omitted (text-only) when the message has no block-worthy structure. This
    is the single choke point that kills the "asterisk soup": every content-
    bearing send tool routes through here instead of passing raw model text.
    """
    payload: dict = {"text": to_slack_mrkdwn(message) or " "}
    blocks = to_slack_blocks(message)
    if blocks:
        payload["blocks"] = blocks
    return payload


# ── 1. Message Sending & Replying ─────────────────────────────────────────────

@tool
@idempotent("send_slack_message", key_args=["channel", "message"])
async def send_slack_message(channel: str, message: str) -> str:
    """Send a message to a Slack channel or user ID.

    Args:
        channel: Channel ID (e.g., 'C0123456789') or user ID to send the message to.
        message: Message content in Markdown or Slack mrkdwn — it is auto-formatted
            into a clean Slack layout (bold, bullets, links, headers/sections).
    """
    try:
        result = await _get_client().chat_postMessage(channel=channel, **_formatted_payload(message))
        ts = result.get("ts", "")
        return f"✅ Sent to {channel} (ts: {ts})"
    except SlackApiError as e:
        return f"⚠️ Send failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Send error: {e}"


@tool
@idempotent("reply_to_slack_thread", key_args=["channel", "thread_ts", "message"])
async def reply_to_slack_thread(channel: str, thread_ts: str, message: str) -> str:
    """Reply within an existing message thread in a Slack channel.

    Args:
        channel: Channel ID (e.g., 'C0123456789') where the thread exists.
        thread_ts: The timestamp (ts) of the parent message to reply to.
        message: Reply content in Markdown or Slack mrkdwn — auto-formatted into
            a clean Slack layout (bold, bullets, links, sections).
    """
    try:
        result = await _get_client().chat_postMessage(channel=channel, thread_ts=thread_ts, **_formatted_payload(message))
        ts = result.get("ts", "")
        return f"✅ Replied to thread {thread_ts} in {channel} (ts: {ts})"
    except SlackApiError as e:
        return f"⚠️ Reply failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Reply error: {e}"


@tool
@idempotent("send_slack_dm", key_args=["user_email", "message"])
async def send_slack_dm(user_email: str, message: str) -> str:
    """Send a direct message (DM) to a Slack user by their email address.

    Args:
        user_email: The email address of the Slack user to look up and message.
        message: DM content in Markdown or Slack mrkdwn — auto-formatted into a
            clean Slack layout (bold, bullets, links, sections).
    """
    try:
        client = _get_client()
        lookup = await client.users_lookupByEmail(email=user_email)
        user_id = lookup["user"]["id"]
        conv = await client.conversations_open(users=user_id)
        channel_id = conv["channel"]["id"]
        result = await client.chat_postMessage(channel=channel_id, **_formatted_payload(message))
        ts = result.get("ts", "")
        return f"✅ DM sent to {user_email} (<@{user_id}>, ts: {ts})"
    except SlackApiError as e:
        return f"⚠️ DM failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ DM error: {e}"


@tool
@idempotent("send_slack_ephemeral_message", key_args=["channel", "user_id", "message"])
async def send_slack_ephemeral_message(channel: str, user_id: str, message: str) -> str:
    """Send an ephemeral message visible ONLY to a specific user in a channel.

    Args:
        channel: Channel ID (e.g., 'C0123456789') where the ephemeral message appears.
        user_id: The Slack User ID (e.g., 'U0123456789') of the recipient.
        message: Ephemeral content in Markdown or Slack mrkdwn — auto-formatted
            into a clean Slack layout.
    """
    try:
        await _get_client().chat_postEphemeral(channel=channel, user=user_id, **_formatted_payload(message))
        return f"✅ Ephemeral message sent to <@{user_id}> in channel {channel}"
    except SlackApiError as e:
        return f"⚠️ Ephemeral send failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Ephemeral send error: {e}"


# ── 2. Message Modification, Deletion & Permalinks ─────────────────────────────

@tool
async def update_slack_message(channel: str, timestamp: str, message: str) -> str:
    """Update/edit an existing message previously posted by the bot.

    Args:
        channel: Channel ID where the message was posted.
        timestamp: The timestamp (ts) of the message to update.
        message: New content in Markdown or Slack mrkdwn — auto-formatted into a
            clean Slack layout (bold, bullets, links, sections).
    """
    try:
        await _get_client().chat_update(channel=channel, ts=timestamp, **_formatted_payload(message))
        return f"✅ Message {timestamp} in {channel} updated successfully."
    except SlackApiError as e:
        return f"⚠️ Message update failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Message update error: {e}"


@tool
async def delete_slack_message(channel: str, timestamp: str) -> str:
    """Delete a message from a Slack channel.

    Args:
        channel: Channel ID where the message is located.
        timestamp: The timestamp (ts) of the message to delete.
    """
    try:
        await _get_client().chat_delete(channel=channel, ts=timestamp)
        return f"✅ Message {timestamp} in {channel} deleted successfully."
    except SlackApiError as e:
        return f"⚠️ Message deletion failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Message deletion error: {e}"


@tool
async def get_slack_message_permalink(channel: str, timestamp: str) -> str:
    """Retrieve the permanent URL link (permalink) to a specific Slack message.

    Args:
        channel: Channel ID where the message exists.
        timestamp: The timestamp (ts) of the message.
    """
    try:
        result = await _get_client().chat_getPermalink(channel=channel, message_ts=timestamp)
        permalink = result.get("permalink", "")
        return f"🔗 Permalink: {permalink}"
    except SlackApiError as e:
        return f"⚠️ Permalink retrieval failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Permalink error: {e}"


@tool
@idempotent("schedule_slack_message", key_args=["channel", "message", "post_at"])
async def schedule_slack_message(channel: str, message: str, post_at: int) -> str:
    """Schedule a message to be posted to a Slack channel at a specific future Unix timestamp.

    Args:
        channel: Channel ID where the scheduled message should be posted.
        message: Content in Markdown or Slack mrkdwn — auto-formatted into a clean
            Slack layout (bold, bullets, links, sections).
        post_at: Unix timestamp (integer seconds) when the message should be sent (must be in the future).
    """
    try:
        result = await _get_client().chat_scheduleMessage(channel=channel, post_at=post_at, **_formatted_payload(message))
        sched_id = result.get("scheduled_message_id", "")
        return f"✅ Message scheduled for {post_at} (ID: {sched_id}) in {channel}"
    except SlackApiError as e:
        return f"⚠️ Schedule message failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Schedule message error: {e}"


@tool
async def delete_scheduled_slack_message(channel: str, scheduled_message_id: str) -> str:
    """Cancel and delete a previously scheduled Slack message before it posts.

    Args:
        channel: Channel ID where the message was scheduled to post.
        scheduled_message_id: The ID of the scheduled message (from schedule_slack_message).
    """
    try:
        await _get_client().chat_deleteScheduledMessage(channel=channel, scheduled_message_id=scheduled_message_id)
        return f"✅ Scheduled message {scheduled_message_id} in {channel} cancelled."
    except SlackApiError as e:
        return f"⚠️ Delete scheduled message failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Delete scheduled message error: {e}"


# ── 3. Pinning & Unpinning Messages ───────────────────────────────────────────

@tool
async def pin_slack_message(channel: str, timestamp: str) -> str:
    """Pin a message or file to a Slack channel.

    Args:
        channel: Channel ID where the item is located.
        timestamp: The timestamp (ts) of the message to pin.
    """
    try:
        await _get_client().pins_add(channel=channel, timestamp=timestamp)
        return f"✅ Pinned message {timestamp} to channel {channel}"
    except SlackApiError as e:
        err = e.response.get("error", "unknown")
        if err == "already_pinned":
            return f"ℹ️ Message {timestamp} is already pinned in {channel}."
        return f"⚠️ Pin failed: {err}"
    except Exception as e:
        return f"⚠️ Pin error: {e}"


@tool
async def unpin_slack_message(channel: str, timestamp: str) -> str:
    """Unpin a previously pinned message from a Slack channel.

    Args:
        channel: Channel ID where the pinned item is located.
        timestamp: The timestamp (ts) of the message to unpin.
    """
    try:
        await _get_client().pins_remove(channel=channel, timestamp=timestamp)
        return f"✅ Unpinned message {timestamp} from channel {channel}"
    except SlackApiError as e:
        err = e.response.get("error", "unknown")
        if err == "no_pin":
            return f"ℹ️ Message {timestamp} is not pinned in {channel}."
        return f"⚠️ Unpin failed: {err}"
    except Exception as e:
        return f"⚠️ Unpin error: {e}"


@tool
async def list_slack_pins(channel: str) -> str:
    """List all pinned items (messages, files) in a Slack channel.

    Args:
        channel: Channel ID (e.g., 'C0123456789') to get pinned items from.
    """
    try:
        result = await _get_client().pins_list(channel=channel)
        items = result.get("items", [])
        if not items:
            return f"No pinned items found in channel {channel}."
        lines = [f"Pinned items in {channel} ({len(items)}):"]
        for idx, item in enumerate(items, 1):
            item_type = item.get("type", "message")
            if item_type == "message":
                msg = item.get("message", {})
                lines.append(
                    f"{idx}. [Message ts: {msg.get('ts')}] <@{msg.get('user', 'Unknown')}>: "
                    f"{msg.get('text', '')[:200]}"
                )
            elif item_type == "file":
                f = item.get("file", {})
                lines.append(f"{idx}. [File] {f.get('name', 'Unnamed')} (ID: {f.get('id')})")
            else:
                lines.append(f"{idx}. [{item_type}] {item.get('created', '')}")
        return "\n".join(lines)
    except SlackApiError as e:
        return f"⚠️ List pins failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ List pins error: {e}"


# ── 4. History & Conversation Replies ─────────────────────────────────────────

async def _fetch_history(client: AsyncWebClient, channel: str, max_messages: int):
    """Fetch history, auto-joining a public channel once if the bot isn't in it."""
    try:
        return await client.conversations_history(channel=channel, limit=max_messages)
    except SlackApiError as e:
        if e.response.get("error") == "not_in_channel":
            try:
                await client.conversations_join(channel=channel)
                return await client.conversations_history(channel=channel, limit=max_messages)
            except SlackApiError:
                raise e
        raise


@tool
async def get_slack_channel_history(channel: str, max_messages: int = 10) -> str:
    """Get recent messages from a Slack channel. Auto-joins public channels if
    the bot is not yet a member.

    Args:
        channel: The channel ID (e.g. 'C0123ABC') to read history from.
        max_messages: How many recent messages to fetch (default 10, max 100).
    """
    try:
        result = await _fetch_history(_get_client(), channel, min(max_messages, 100))
        messages = result.get("messages", [])
        if not messages:
            return f"No messages in channel {channel}."
        lines = [f"Last {len(messages)} messages in {channel}:"]
        for m in reversed(messages):
            ts = m.get("ts", "")
            user = m.get("user") or m.get("bot_id", "Unknown")
            text = m.get("text", "").replace("\n", " ")[:200]
            lines.append(f"• [{ts}] <@{user}>: {text}")
        return "\n".join(lines)
    except SlackApiError as e:
        err = e.response.get("error", "unknown")
        if err == "not_in_channel":
            return (
                f"⚠️ Not a member of channel {channel} and it could not be auto-joined "
                f"(it is likely private). Join or invite the bot to the channel first."
            )
        return f"⚠️ History failed: {err}"
    except Exception as e:
        return f"⚠️ History error: {e}"


@tool
async def get_slack_thread_replies(channel: str, thread_ts: str) -> str:
    """Retrieve all replies within a specific Slack message thread.

    Args:
        channel: Channel ID where the thread exists.
        thread_ts: The timestamp (ts) of the parent message of the thread.
    """
    try:
        result = await _get_client().conversations_replies(channel=channel, ts=thread_ts)
        messages = result.get("messages", [])
        if not messages:
            return f"No replies in thread {thread_ts}."
        lines = [f"Thread replies ({len(messages)}) in {channel}:"]
        for m in messages:
            ts = m.get("ts", "")
            user = m.get("user") or m.get("bot_id", "Unknown")
            text = m.get("text", "").replace("\n", " ")[:200]
            lines.append(f"• [{ts}] <@{user}>: {text}")
        return "\n".join(lines)
    except SlackApiError as e:
        return f"⚠️ Thread failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Thread error: {e}"


# ── 5. Channel Management & Operations ────────────────────────────────────────

@tool
async def list_slack_channels(exclude_archived: bool = True, limit: int = 50, member_only: bool = False) -> str:
    """List public and private channels in the workspace with membership status.

    Args:
        exclude_archived: Skip archived channels (default True).
        limit: Max channels to return (default 50).
        member_only: If True, return ONLY channels the bot has already joined.
    """
    try:
        try:
            result = await _get_client().conversations_list(
                types="public_channel,private_channel",
                exclude_archived=exclude_archived,
                limit=limit,
            )
        except SlackApiError as e:
            if e.response.get("error") == "missing_scope":
                # Fallback to public channels only if groups:read scope is not present
                result = await _get_client().conversations_list(
                    types="public_channel",
                    exclude_archived=exclude_archived,
                    limit=limit,
                )
            else:
                raise e
        channels = result.get("channels", [])
        if member_only:
            channels = [c for c in channels if c.get("is_member")]
        if not channels:
            return "No member channels found." if member_only else "No channels found."
        label = "Member channels" if member_only else "Channels"
        lines = [f"{label} ({len(channels)}):"]
        for ch in channels:
            flag = "✅ member" if ch.get("is_member") else "➕ not a member"
            privacy = "🔒 private" if ch.get("is_private") else "🌐 public"
            lines.append(
                f"• #{ch.get('name')} (ID: {ch.get('id')}) — {privacy} — "
                f"{ch.get('num_members', 0)} members — {flag}"
            )
        if not member_only and result.get("response_metadata", {}).get("next_cursor"):
            lines.append("\n(More channels exist — increase limit to see them.)")
        return "\n".join(lines)
    except SlackApiError as e:
        return f"⚠️ List channels failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ List channels error: {e}"


@tool
async def get_slack_channel_info(channel: str) -> str:
    """Get detailed metadata and settings for a specific Slack channel (topic, purpose, members count, privacy).

    Args:
        channel: Channel ID (e.g., 'C0123456789') to inspect.
    """
    try:
        result = await _get_client().conversations_info(channel=channel, include_num_members=True)
        ch = result.get("channel", {})
        topic = ch.get("topic", {}).get("value") or "None"
        purpose = ch.get("purpose", {}).get("value") or "None"
        is_private = ch.get("is_private", False)
        is_archived = ch.get("is_archived", False)
        num_members = ch.get("num_members", 0)
        return (
            f"Channel #{ch.get('name', 'unknown')} (ID: {ch.get('id')}):\n"
            f"• Type: {'Private 🔒' if is_private else 'Public 🌐'}\n"
            f"• Members: {num_members}\n"
            f"• Archived: {is_archived}\n"
            f"• Topic: {topic}\n"
            f"• Purpose: {purpose}"
        )
    except SlackApiError as e:
        return f"⚠️ Channel info failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Channel info error: {e}"


@tool
async def join_slack_channel(channel: str) -> str:
    """Explicitly join a public Slack channel.

    Args:
        channel: Channel ID (e.g., 'C0123456789') to join.
    """
    try:
        await _get_client().conversations_join(channel=channel)
        return f"✅ Successfully joined channel {channel}"
    except SlackApiError as e:
        return f"⚠️ Join channel failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Join channel error: {e}"


@tool
async def leave_slack_channel(channel: str) -> str:
    """Leave a Slack channel.

    Args:
        channel: Channel ID (e.g., 'C0123456789') to leave.
    """
    try:
        await _get_client().conversations_leave(channel=channel)
        return f"✅ Left channel {channel}"
    except SlackApiError as e:
        return f"⚠️ Leave channel failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Leave channel error: {e}"


@tool
async def set_slack_channel_topic(channel: str, topic: str) -> str:
    """Set or update the topic of a Slack channel.

    Args:
        channel: Channel ID (e.g., 'C0123456789') where topic should be set.
        topic: The new topic string for the channel.
    """
    try:
        result = await _get_client().conversations_setTopic(channel=channel, topic=topic)
        new_topic = result.get("topic", topic)
        return f"✅ Channel {channel} topic set to: \"{new_topic}\""
    except SlackApiError as e:
        return f"⚠️ Set topic failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Set topic error: {e}"


@tool
async def set_slack_channel_purpose(channel: str, purpose: str) -> str:
    """Set or update the purpose/description of a Slack channel.

    Args:
        channel: Channel ID (e.g., 'C0123456789') where purpose should be set.
        purpose: The new purpose description.
    """
    try:
        result = await _get_client().conversations_setPurpose(channel=channel, purpose=purpose)
        new_purpose = result.get("purpose", purpose)
        return f"✅ Channel {channel} purpose set to: \"{new_purpose}\""
    except SlackApiError as e:
        return f"⚠️ Set purpose failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Set purpose error: {e}"


@tool
async def get_slack_channel_members(channel: str, limit: int = 100) -> str:
    """Retrieve the list of user IDs who are members of a Slack channel.

    Args:
        channel: Channel ID (e.g., 'C0123456789') to get members from.
        limit: Max member IDs to fetch (default 100).
    """
    try:
        result = await _get_client().conversations_members(channel=channel, limit=min(limit, 1000))
        members = result.get("members", [])
        if not members:
            return f"No members found in channel {channel}."
        members_formatted = ", ".join(f"<@{m}>" for m in members[:50])
        extra = f" (and {len(members) - 50} more)" if len(members) > 50 else ""
        return f"Channel {channel} has {len(members)} members:\n{members_formatted}{extra}"
    except SlackApiError as e:
        return f"⚠️ Channel members failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Channel members error: {e}"


@tool
@idempotent("create_slack_channel", key_args=["name", "is_private"])
async def create_slack_channel(name: str, is_private: bool = False) -> str:
    """Create a new public or private Slack channel.

    Args:
        name: Name of the channel to create (lowercase, no spaces, e.g. 'proj-announcements').
        is_private: True for private channel, False for public channel (default False).
    """
    try:
        result = await _get_client().conversations_create(name=name, is_private=is_private)
        ch = result.get("channel", {})
        return f"✅ Created channel #{ch.get('name')} (ID: {ch.get('id')}, Private: {is_private})"
    except SlackApiError as e:
        return f"⚠️ Channel creation failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Channel creation error: {e}"


@tool
async def invite_to_slack_channel(channel: str, users: str) -> str:
    """Invite one or more users to a Slack channel.

    Args:
        channel: Channel ID (e.g., 'C0123456789') to invite users into.
        users: Comma-separated list of Slack User IDs (e.g., 'U12345,U67890').
    """
    try:
        await _get_client().conversations_invite(channel=channel, users=users)
        return f"✅ Invited users ({users}) to channel {channel}"
    except SlackApiError as e:
        return f"⚠️ Channel invite failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Channel invite error: {e}"


# ── 6. Reactions (Add, Remove, Get) ───────────────────────────────────────────

@tool
async def add_slack_reaction(channel: str, timestamp: str, emoji: str) -> str:
    """Add an emoji reaction to a message.

    Args:
        channel: Channel ID where the message is located.
        timestamp: The timestamp (ts) of the message to react to.
        emoji: Emoji name without colons (e.g., 'thumbsup', 'rocket', 'eyes').
    """
    try:
        clean_emoji = emoji.strip(":")
        await _get_client().reactions_add(channel=channel, timestamp=timestamp, name=clean_emoji)
        return f"✅ Added :{clean_emoji}: to message {timestamp}"
    except SlackApiError as e:
        err = e.response.get("error", "unknown")
        if err == "already_reacted":
            return f"ℹ️ Already reacted with :{emoji}: on message {timestamp}."
        return f"⚠️ Reaction add failed: {err}"
    except Exception as e:
        return f"⚠️ Reaction add error: {e}"


@tool
async def remove_slack_reaction(channel: str, timestamp: str, emoji: str) -> str:
    """Remove an emoji reaction from a message.

    Args:
        channel: Channel ID where the message is located.
        timestamp: The timestamp (ts) of the message.
        emoji: Emoji name without colons (e.g., 'thumbsup').
    """
    try:
        clean_emoji = emoji.strip(":")
        await _get_client().reactions_remove(channel=channel, timestamp=timestamp, name=clean_emoji)
        return f"✅ Removed :{clean_emoji}: from message {timestamp}"
    except SlackApiError as e:
        err = e.response.get("error", "unknown")
        if err == "no_reaction":
            return f"ℹ️ No :{emoji}: reaction found on message {timestamp}."
        return f"⚠️ Reaction remove failed: {err}"
    except Exception as e:
        return f"⚠️ Reaction remove error: {e}"


@tool
async def get_slack_reactions(channel: str, timestamp: str) -> str:
    """Get all emoji reactions on a specific Slack message.

    Args:
        channel: Channel ID where the message is located.
        timestamp: The timestamp (ts) of the message.
    """
    try:
        result = await _get_client().reactions_get(channel=channel, timestamp=timestamp)
        msg = result.get("message", {})
        reactions = msg.get("reactions", [])
        if not reactions:
            return f"No reactions on message {timestamp} in {channel}."
        lines = [f"Reactions on message {timestamp}:"]
        for r in reactions:
            users = ", ".join(f"<@{u}>" for u in r.get("users", []))
            lines.append(f"• :{r.get('name')}: x{r.get('count')} (by {users})")
        return "\n".join(lines)
    except SlackApiError as e:
        return f"⚠️ Get reactions failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ Get reactions error: {e}"


# ── 7. Users & Directory ──────────────────────────────────────────────────────

@tool
async def lookup_slack_user(email: str) -> str:
    """Lookup a Slack user profile and User ID by their email address.

    Args:
        email: The corporate/workspace email address of the user.
    """
    try:
        user = (await _get_client().users_lookupByEmail(email=email))["user"]
        profile = user.get("profile", {})
        return (
            f"User found for {email}:\n"
            f"• Real Name: {user.get('real_name', 'Unknown')}\n"
            f"• Display Name: @{profile.get('display_name') or user.get('name')}\n"
            f"• User ID: {user.get('id')}\n"
            f"• Title/Role: {profile.get('title', 'N/A')}\n"
            f"• Timezone: {user.get('tz', 'Unknown')}\n"
            f"• Admin: {user.get('is_admin', False)}"
        )
    except SlackApiError as e:
        return f"⚠️ User lookup failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ User lookup error: {e}"


@tool
async def get_slack_user_info(user_id: str) -> str:
    """Retrieve full profile and status information for a Slack user by their User ID.

    Args:
        user_id: The Slack User ID (e.g., 'U0123456789').
    """
    try:
        result = await _get_client().users_info(user=user_id)
        user = result.get("user", {})
        profile = user.get("profile", {})
        return (
            f"Slack User <@{user_id}>:\n"
            f"• Name: {user.get('real_name', 'Unknown')}\n"
            f"• Display Name: @{profile.get('display_name') or user.get('name')}\n"
            f"• Email: {profile.get('email', 'Hidden/None')}\n"
            f"• Title: {profile.get('title', 'N/A')}\n"
            f"• Status: {profile.get('status_emoji', '')} {profile.get('status_text', '')}\n"
            f"• Timezone: {user.get('tz', 'Unknown')}\n"
            f"• Bot: {user.get('is_bot', False)} | Admin: {user.get('is_admin', False)}"
        )
    except SlackApiError as e:
        return f"⚠️ User info failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ User info error: {e}"


@tool
async def list_slack_users(limit: int = 50) -> str:
    """List members and users in the Slack workspace.

    Args:
        limit: Max users to list (default 50).
    """
    try:
        result = await _get_client().users_list(limit=limit)
        members = result.get("members", [])
        # Filter out deleted members
        active_members = [m for m in members if not m.get("deleted")]
        lines = [f"Workspace Users ({len(active_members)} active shown):"]
        for m in active_members:
            bot_tag = " [BOT]" if m.get("is_bot") else ""
            lines.append(
                f"• {m.get('real_name', 'Unknown')} (@{m.get('name')}){bot_tag} — ID: {m.get('id')}"
            )
        return "\n".join(lines)
    except SlackApiError as e:
        return f"⚠️ List users failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ List users error: {e}"


# ── 8. File Sharing & Uploads ─────────────────────────────────────────────────

@tool
@idempotent("upload_slack_file", key_args=["channel", "content", "file_path", "filename", "thread_ts"])
async def upload_slack_file(
    channel: str,
    content: Optional[str] = None,
    file_path: Optional[str] = None,
    filename: Optional[str] = None,
    title: Optional[str] = None,
    initial_comment: Optional[str] = None,
    thread_ts: Optional[str] = None,
) -> str:
    """Upload a file or text content/snippet to a Slack channel or thread using files.upload_v2.

    Args:
        channel: Channel ID (e.g., 'C0123456789') to upload to.
        content: Raw text content to upload as a file/snippet (optional if file_path provided).
        file_path: Absolute or relative local path to a file to upload (optional if content provided).
        filename: Optional name for the uploaded file (e.g., 'report.txt' or 'summary.md').
        title: Optional title of the file.
        initial_comment: Optional message text accompanying the file.
        thread_ts: Optional thread timestamp to post the file into a thread.
    """
    try:
        if not content and not file_path:
            return "⚠️ Either 'content' or 'file_path' must be provided."

        client = _get_client()
        result = await client.files_upload_v2(
            channel=channel,
            content=content,
            file=file_path,
            filename=filename or "upload.txt",
            title=title or filename or "Uploaded File",
            initial_comment=initial_comment,
            thread_ts=thread_ts,
        )
        files = result.get("files", [])
        file_title = files[0].get("title", title or "File") if files else (title or "File")
        return f"✅ File '{file_title}' uploaded to channel {channel}"
    except SlackApiError as e:
        return f"⚠️ File upload failed: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"⚠️ File upload error: {e}"


# ── Complete Export List for Sienna Subagent ──────────────────────────────────

SLACK_TOOLS = [
    # Message Sending & Replying
    send_slack_message,
    reply_to_slack_thread,
    send_slack_dm,
    send_slack_ephemeral_message,
    # Message Lifecycle & Updates
    update_slack_message,
    delete_slack_message,
    get_slack_message_permalink,
    schedule_slack_message,
    delete_scheduled_slack_message,
    # Pinning
    pin_slack_message,
    unpin_slack_message,
    list_slack_pins,
    # History & Threads
    get_slack_channel_history,
    get_slack_thread_replies,
    # Channels
    list_slack_channels,
    get_slack_channel_info,
    get_slack_channel_members,
    join_slack_channel,
    leave_slack_channel,
    set_slack_channel_topic,
    set_slack_channel_purpose,
    create_slack_channel,
    invite_to_slack_channel,
    # Reactions
    add_slack_reaction,
    remove_slack_reaction,
    get_slack_reactions,
    # Users
    lookup_slack_user,
    get_slack_user_info,
    list_slack_users,
    # Files
    upload_slack_file,
]