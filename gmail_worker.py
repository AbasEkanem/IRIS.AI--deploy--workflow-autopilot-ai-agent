"""
gmail_worker.py
===============
Background worker for processing scheduled emails for IRIS.AI.

Polls Supabase (`iris_scheduled_emails` table) for pending emails whose
`scheduled_at` timestamp has passed, claims them atomically, and dispatches
them via Resend API (primary) or Gmail SMTP (fallback).
"""

import asyncio
import os
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
from supabase import create_client
import resend

from gmail_tools import (
    _build_html_email,
    _log_email_to_supabase,
    RESEND_API_KEY,
    RESEND_FROM,
    GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD,
)

load_dotenv()


def _claim_job(supabase, job_id: str, now_iso: str) -> bool:
    resp = (
        supabase.table("iris_scheduled_emails")
        .update({"status": "processing", "processing_at": now_iso})
        .eq("id", job_id)
        .eq("status", "pending")
        .execute()
    )
    return bool(getattr(resp, "data", None))


def _poll_and_send_due_emails() -> None:
    """Run ONE polling cycle: fetch due jobs, claim and send them."""
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")

    if not supabase_url or not supabase_key:
        print("[!] Error: SUPABASE_URL and SUPABASE_KEY must be set in .env")
        return

    # Fresh client each cycle — avoids stale SSL connections dropping
    supabase = create_client(supabase_url, supabase_key)
    now = datetime.now(timezone.utc).isoformat()

    # Fetch pending emails that are due to be sent
    resp = supabase.table("iris_scheduled_emails") \
        .select("*") \
        .eq("status", "pending") \
        .lte("scheduled_at", now) \
        .execute()

    pending_jobs = resp.data or []

    if pending_jobs:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Found {len(pending_jobs)} scheduled email(s) ready to send.")

    for job in pending_jobs:
        job_id = job['id']
        if not _claim_job(supabase, job_id, now):
            continue

        to_email = job['to_email']
        subject = job['subject']
        body = job['body']

        print(f"  -> Sending to {to_email} (Subject: '{subject}')")

        try:
            html_body = _build_html_email(subject, body)

            resend_success = False
            if RESEND_API_KEY:
                try:
                    resend.api_key = RESEND_API_KEY
                    resend.Emails.send({
                        "from": RESEND_FROM,
                        "to": [to_email],
                        "subject": subject,
                        "text": body,
                        "html": html_body,
                    })
                    _log_email_to_supabase("sent", RESEND_FROM, to_email, subject, body)
                    resend_success = True
                except Exception as e:
                    print(f"  [WARN] Resend failed, falling back to Gmail SMTP: {e}")
                    _log_email_to_supabase("sent", RESEND_FROM, to_email, subject, body, "error", str(e))

            if not resend_success:
                if GMAIL_ADDRESS and GMAIL_APP_PASSWORD:
                    msg = MIMEMultipart("alternative")
                    msg["From"] = f"IRIS.AI Operations <{GMAIL_ADDRESS}>"
                    msg["To"] = to_email
                    msg["Subject"] = subject
                    msg.attach(MIMEText(body, "plain", "utf-8"))
                    msg.attach(MIMEText(html_body, "html", "utf-8"))

                    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                        server.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())
                    _log_email_to_supabase("sent", GMAIL_ADDRESS, to_email, subject, body)
                else:
                    raise Exception("Both Resend and Gmail failed or are unconfigured.")

            # Mark as sent
            supabase.table("iris_scheduled_emails") \
                .update({"status": "sent", "sent_at": now}) \
                .eq("id", job_id) \
                .execute()

            print(f"  [OK] Successfully sent and marked as 'sent' in iris_scheduled_emails.")

        except Exception as e:
            print(f"  [FAIL] Error sending email: {e}")
            # Mark as failed
            supabase.table("iris_scheduled_emails") \
                .update({"status": "failed", "error": str(e)}) \
                .eq("id", job_id) \
                .execute()


def start_worker():
    """Synchronous standalone entrypoint (e.g. `python gmail_worker.py`)."""
    print("[*] Starting IRIS.AI Scheduled Email Worker...")
    print("[+] Email worker started. Polling Supabase every 30s...")
    while True:
        try:
            _poll_and_send_due_emails()
        except Exception as e:
            print(f"[x] Worker encountered an error querying the database: {e}")
        time.sleep(30)


async def _email_poll_loop() -> None:
    """Cancellation-aware async email poll loop."""
    print("[*] Starting IRIS.AI Scheduled Email Worker (async loop)...")
    print("[+] Email worker started. Polling Supabase every 30s...")
    while True:
        try:
            await asyncio.to_thread(_poll_and_send_due_emails)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[x] Worker encountered an error querying the database: {e}")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break


async def async_start_worker():
    await _email_poll_loop()


if __name__ == "__main__":
    start_worker()
