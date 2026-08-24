"""
schedule_email.py
==================
Scheduled email delivery tool for IRIS.AI (Grace Subagent).

Provides `schedule_research_email` — a LangChain tool that inserts scheduled email
jobs into Supabase (`iris_scheduled_emails` table). The background worker in
`gmail_worker.py` polls this table and dispatches due emails automatically.

Imported by gmail_tools.py and added to the `email_tools` registry.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from langchain_core.tools import tool

from gmail_tools import _supabase

NIGERIA_TZ = timezone(timedelta(hours=1))


@tool
def schedule_research_email(
    to_email: str,
    subject: str,
    research_content: str,
    schedule_at: str,
) -> str:
    """Schedule a research report or email to be sent at a specific future date and time.

    Args:
        to_email: Recipient's email address.
        subject: Email subject line.
        research_content: Full email body or research findings (markdown supported).
        schedule_at: Future date/time string (e.g. 'tomorrow 9am', '2026-08-15T09:00:00+01:00').
    """
    if not _supabase:
        return "Scheduling failed: Supabase is not configured. Please set SUPABASE_URL and SUPABASE_KEY in .env"

    if not to_email or "@" not in to_email:
        return f"Scheduling failed: '{to_email}' is not a valid email address."

    try:
        text = (schedule_at or "").strip()
        now_utc = datetime.now(timezone.utc)

        if text.lower().startswith("tomorrow"):
            target_local = (datetime.now(NIGERIA_TZ) + timedelta(days=1)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            target_time = target_local.astimezone(timezone.utc).isoformat()
        else:
            normalized = text.replace("Z", "+00:00")
            scheduled_dt = datetime.fromisoformat(normalized)
            if scheduled_dt.tzinfo is None:
                scheduled_dt = scheduled_dt.replace(tzinfo=NIGERIA_TZ)
            target_time = scheduled_dt.astimezone(timezone.utc).isoformat()

        if datetime.fromisoformat(target_time.replace("Z", "+00:00")) <= now_utc:
            return "Scheduling failed: scheduled time must be in the future."

        # Insert job into Supabase scheduled emails table
        _supabase.table("iris_scheduled_emails").insert({
            "to_email": to_email,
            "subject": subject,
            "body": research_content,
            "scheduled_at": target_time,
            "status": "pending",
            "created_at": now_utc.isoformat(),
        }).execute()

        return (
            f"📅 Email scheduled successfully!\n"
            f"  Recipient : {to_email}\n"
            f"  Subject   : {subject}\n"
            f"  Scheduled : {target_time}\n"
            f"  Status    : Pending in Supabase iris_scheduled_emails queue"
        )
    except Exception as e:
        return f"Scheduling failed: {str(e)}"
