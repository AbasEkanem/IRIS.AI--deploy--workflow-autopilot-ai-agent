"""
google_calendar_tools.py
==========================
Full Calendar CRUD for IRIS.AI (Grace Subagent): create events, list schedule, update
details, respond to invitations, cancel events, and check free/busy schedules.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional, Union

from langchain_core.tools import tool

from google_auth import get_service, execute_with_retry
from formatting import to_calendar_html
from idempotency import idempotent

_log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[^\s,;\[\]\"']+@[^\s,;\[\]\"']+")


def _svc():
    return get_service("calendar")


def _parse_emails(value: Union[str, list, None]) -> list[str]:
    """Best-effort parse of attendee emails from whatever the model passes.

    Accepts a native list, a JSON array string, a Python-repr list string,
    a comma/semicolon-separated string, or a single bare email. Falls back to
    a regex scan so malformed quoting/escaping can never stall the tool.
    """
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        text = str(value).strip()
        items = None
        # Try strict JSON first.
        try:
            parsed = json.loads(text)
            items = parsed if isinstance(parsed, (list, tuple)) else [parsed]
        except (ValueError, TypeError):
            items = None
        # Fall back to a plain regex scan (handles bad quoting, brackets, commas).
        if items is None:
            items = _EMAIL_RE.findall(text)
    emails = []
    for item in items:
        for match in _EMAIL_RE.findall(str(item)):
            if match not in emails:
                emails.append(match)
    return emails



@tool
@idempotent(
    "create_calendar_event",
    key_args=["calendar_id", "summary", "start_time_iso", "end_time_iso", "attendees_emails_json"],
)
def create_calendar_event(
    summary: str,
    start_time_iso: str,
    end_time_iso: str,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees_emails_json: Optional[Union[str, list]] = None,
    timezone_str: str = "UTC",
    calendar_id: str = "primary"
) -> str:
    """Create a new event on Google Calendar.

    Args:
        summary: Title/Summary of the event.
        start_time_iso: Start date-time in ISO format (e.g. '2026-08-01T14:00:00Z' or '2026-08-01T14:00:00').
        end_time_iso: End date-time in ISO format.
        description: Optional event description. Write it in Markdown — it is
            auto-converted to Calendar-safe rich text (bold, bullets, links).
        location: Optional location string or meeting link.
        attendees_emails_json: Optional attendee emails. Accepts a plain list
            (e.g. ["a@x.com", "b@y.com"]), a JSON array string, a single email,
            or a comma-separated string.
        timezone_str: Timezone name (default 'UTC').
        calendar_id: Target calendar ID (default 'primary').
    """
    try:
        event: dict = {
            "summary": summary,
            "start": {"dateTime": start_time_iso, "timeZone": timezone_str},
            "end": {"dateTime": end_time_iso, "timeZone": timezone_str},
        }
        if description:
            event["description"] = to_calendar_html(description)
        if location:
            event["location"] = location

        attendee_list = [{"email": e} for e in _parse_emails(attendees_emails_json)]
        if attendee_list:
            event["attendees"] = attendee_list


        created_event = execute_with_retry(lambda: (
            _svc()
            .events()
            .insert(calendarId=calendar_id, body=event, sendUpdates="all" if attendee_list else "none")
        ))
        event_id = created_event["id"]
        link = created_event.get("htmlLink", "N/A")
        return f"✅ Calendar event created: **{summary}** (ID: `{event_id}`)\nStart: `{start_time_iso}` | End: `{end_time_iso}`\nLink: {link}"
    except Exception as e:
        return f"⚠️ Create calendar event failed: {e}"


@tool
def list_calendar_events(
    time_min_iso: Optional[str] = None,
    time_max_iso: Optional[str] = None,
    max_results: int = 20,
    calendar_id: str = "primary"
) -> str:
    """List upcoming events from Google Calendar.

    Args:
        time_min_iso: Minimum event start time in ISO format (defaults to current time if omitted).
        time_max_iso: Maximum event start time in ISO format.
        max_results: Max events to return (default 20).
        calendar_id: Target calendar ID (default 'primary').
    """
    try:
        min_time = time_min_iso or datetime.now(timezone.utc).isoformat()
        kwargs: dict = {
            "calendarId": calendar_id,
            "timeMin": min_time,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime"
        }
        if time_max_iso:
            kwargs["timeMax"] = time_max_iso

        res = execute_with_retry(lambda: _svc().events().list(**kwargs))
        events = res.get("items", [])
        if not events:
            return "No upcoming calendar events found."

        out = [f"📅 Upcoming Events ({len(events)}):"]
        for event in events:
            event_id = event["id"]
            summary = event.get("summary", "No Title")
            start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
            end = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")
            out.append(f"- **{summary}** (ID: `{event_id}`)\n  Start: `{start}` | End: `{end}`\n  Link: {event.get('htmlLink', 'N/A')}")
        return "\n".join(out)
    except Exception as e:
        return f"⚠️ List calendar events failed: {e}"


@tool
def get_calendar_event_details(event_id: str, calendar_id: str = "primary") -> str:
    """Get complete metadata, description, location, and attendee statuses for a specific calendar event.

    Args:
        event_id: The ID of the event.
        calendar_id: Target calendar ID (default 'primary').
    """
    try:
        event = execute_with_retry(lambda: _svc().events().get(calendarId=calendar_id, eventId=event_id))
        summary = event.get("summary", "Untitled Event")
        start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
        end = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")
        attendees = event.get("attendees", [])

        out = [f"📅 **{summary}** (`{event_id}`):\nStart: `{start}` | End: `{end}`"]
        if event.get("location"):
            out.append(f"Location: {event['location']}")
        if event.get("description"):
            out.append(f"Description: {event['description']}")
        if attendees:
            out.append(f"Attendees ({len(attendees)}):")
            for a in attendees:
                out.append(f"  - `{a.get('email')}` (Status: `{a.get('responseStatus', 'needsAction')}`)")
        out.append(f"Link: {event.get('htmlLink', 'N/A')}")
        return "\n".join(out)
    except Exception as e:
        return f"⚠️ Get event details failed: {e}"


@tool
def update_calendar_event(
    event_id: str,
    summary: Optional[str] = None,
    start_time_iso: Optional[str] = None,
    end_time_iso: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    calendar_id: str = "primary"
) -> str:
    """Update details of an existing Google Calendar event.

    Args:
        event_id: The ID of the event to update.
        summary: Optional updated summary.
        start_time_iso: Optional updated start time in ISO format.
        end_time_iso: Optional updated end time in ISO format.
        description: Optional updated description. Markdown is auto-converted to
            Calendar-safe rich text (bold, bullets, links).
        location: Optional updated location.
        calendar_id: Target calendar ID (default 'primary').
    """
    try:
        fields: dict = {}
        if summary:
            fields["summary"] = summary
        if start_time_iso:
            fields["start"] = {"dateTime": start_time_iso}
        if end_time_iso:
            fields["end"] = {"dateTime": end_time_iso}
        if description:
            fields["description"] = to_calendar_html(description)
        if location:
            fields["location"] = location

        updated = execute_with_retry(lambda: _svc().events().patch(calendarId=calendar_id, eventId=event_id, body=fields, sendUpdates="all"))
        return f"✅ Calendar event `{event_id}` updated successfully. Summary: **{updated.get('summary')}**"
    except Exception as e:
        return f"⚠️ Update event failed: {e}"


@tool
def cancel_calendar_event(event_id: str, notify_attendees: bool = True, calendar_id: str = "primary") -> str:
    """Cancel and delete an event from Google Calendar with optional attendee notifications.

    Args:
        event_id: The ID of the event to cancel.
        notify_attendees: Whether to send cancellation emails to attendees (default True).
        calendar_id: Target calendar ID (default 'primary').
    """
    try:
        execute_with_retry(lambda: _svc().events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendUpdates="all" if notify_attendees else "none",
        ))
        return f"✅ Cancelled calendar event `{event_id}` (Notified attendees: {notify_attendees})."
    except Exception as e:
        return f"⚠️ Cancel event failed: {e}"


# Alias for backward compatibility
delete_calendar_event = cancel_calendar_event


@tool
def respond_to_calendar_invitation(
    event_id: str,
    response_status: str,
    self_email: str,
    calendar_id: str = "primary"
) -> str:
    """Respond to an event invitation on Google Calendar.

    Args:
        event_id: The ID of the event.
        response_status: Response choice: 'accepted', 'declined', or 'tentative'.
        self_email: Your Google account email address.
        calendar_id: Target calendar ID (default 'primary').
    """
    try:
        response_lc = response_status.lower().strip()
        if response_lc not in ("accepted", "declined", "tentative"):
            return "⚠️ response_status must be one of 'accepted', 'declined', or 'tentative'."

        event = execute_with_retry(lambda: _svc().events().get(calendarId=calendar_id, eventId=event_id))
        attendees = event.get("attendees", [])
        matched = False
        for attendee in attendees:
            if attendee.get("email", "").lower() == self_email.lower():
                attendee["responseStatus"] = response_lc
                matched = True
                break

        if not matched:
            attendees.append({"email": self_email, "responseStatus": response_lc})

        execute_with_retry(lambda: _svc().events().patch(
            calendarId=calendar_id, eventId=event_id, body={"attendees": attendees}, sendUpdates="all"
        ))
        return f"✅ Responded `{response_lc}` to event invitation `{event_id}`"
    except Exception as e:
        return f"⚠️ Respond to invitation failed: {e}"


@tool
def check_calendar_freebusy(
    time_min_iso: str,
    time_max_iso: str,
    calendar_ids_json: Optional[str] = None
) -> str:
    """Check free/busy availability across one or more Google Calendars for a specified time window.

    Args:
        time_min_iso: Window start time in ISO format (e.g. '2026-08-01T09:00:00Z').
        time_max_iso: Window end time in ISO format (e.g. '2026-08-01T17:00:00Z').
        calendar_ids_json: Optional JSON array of calendar IDs/emails, defaults to '["primary"]'.
    """
    try:
        cids = json.loads(calendar_ids_json) if calendar_ids_json else ["primary"]
        body = {
            "timeMin": time_min_iso,
            "timeMax": time_max_iso,
            "items": [{"id": cid} for cid in cids]
        }
        res = execute_with_retry(lambda: _svc().freebusy().query(body=body))
        calendars = res.get("calendars", {})

        out = [f"⏳ **Free/Busy Schedule** ({time_min_iso} to {time_max_iso}):"]
        for cid, info in calendars.items():
            busy_slots = info.get("busy", [])
            if not busy_slots:
                out.append(f"- Calendar `{cid}`: Entirely FREE during window ✅")
            else:
                out.append(f"- Calendar `{cid}`: BUSY during {len(busy_slots)} slot(s):")
                for slot in busy_slots:
                    out.append(f"   • {slot['start']} -> {slot['end']}")
        return "\n".join(out)
    except Exception as e:
        return f"⚠️ Free/busy query failed: {e}"


# Tool registry — imported by subagent_config.py for Grace
CALENDAR_TOOLS = [
    create_calendar_event,
    list_calendar_events,
    get_calendar_event_details,
    update_calendar_event,
    cancel_calendar_event,
    respond_to_calendar_invitation,
    check_calendar_freebusy,
]
