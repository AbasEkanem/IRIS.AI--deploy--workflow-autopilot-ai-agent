"""
IRIS.AI — Email API (Grace Subagent)
Supports:
  - Sending via Resend (primary, production-grade)
  - Sending via Gmail SMTP (fallback)
  - Reading inbox via IMAP (Gmail)
  - Logging all activity to Supabase
"""

from __future__ import annotations

import os
import time
import socket
import imaplib
import email
import email.header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.utils import make_msgid, formatdate
from email import encoders
from datetime import datetime, timezone, timedelta
import smtplib
import base64

import resend
from supabase import create_client, Client
from langchain_core.tools import tool
from dotenv import load_dotenv

from idempotency import idempotent


load_dotenv()

#Credentials 
RESEND_API_KEY     = os.getenv("RESEND_API_KEY", "")

GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# Gmail REST API credentials (fallback for Railway where SMTP ports 465/587 are blocked)
GOOGLE_OAUTH_CLIENT_ID     = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN       = os.getenv("GOOGLE_REFRESH_TOKEN", "")

# Part 6 (recipient verification): addresses considered "internal". A
# client-facing send to any of these is almost always a misfire — seen live in
# thread …0416a9b5dd69, where a client update email was routed to the
# operator's OWN gmail instead of the client. Two distinct internal addresses
# exist and BOTH must be covered:
#   * GMAIL_ADDRESS  — the bot's own send-from inbox (e.g. iiriissai@gmail.com)
#   * OPERATOR_EMAIL — the human operator's personal inbox (set this in .env to
#                      the address that owns the workspace, e.g. the same handle
#                      as the Atlassian site). Empty by default.
# A send to any internal address is refused unless allow_self=True. The set is
# recomputed per call (via _internal_addresses) so tests/config can override the
# globals at runtime.
OPERATOR_EMAIL = os.getenv("OPERATOR_EMAIL", "").strip().lower()


def _internal_addresses() -> set[str]:
    """The set of addresses a client-facing send must never target by accident."""
    return {a for a in (GMAIL_ADDRESS.strip().lower(), OPERATOR_EMAIL) if a}

_default_from = f"IRIS.AI <{GMAIL_ADDRESS}>" if GMAIL_ADDRESS else "IRIS.AI <[EMAIL_ADDRESS]>"
RESEND_FROM        = os.getenv("RESEND_FROM_EMAIL", _default_from)

SUPABASE_URL       = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY       = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")

# Supabase client (optional — gracefully degrades if not set)
_supabase: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception:
        _supabase = None


SAFE_LOG_BODY_CHARS = 1000
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

# ── IMAP (Gmail) connection settings ──────────────────────────────────────────
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
# Per-socket timeout: caps any single blocking IMAP round-trip. Previously
# imaplib.IMAP4_SSL() was created with NO timeout, so a slow or half-open Gmail
# connection could block forever — which is exactly what let a single inbox
# search run past the agent's own CONVERSATION_TIMEOUT_S wall.
IMAP_TIMEOUT_S = float(os.getenv("IMAP_TIMEOUT_S", "15"))
# Hard wall-clock budget for ONE inbox read/search. Sits well below the agent
# timeout so the tool returns a clean (possibly partial) result instead of being
# killed by the outer agent timeout mid-fetch.
IMAP_DEADLINE_S = float(os.getenv("IMAP_DEADLINE_S", "45"))
# Bytes of BODY TEXT fetched per message for the list/preview view. Full RFC822
# bodies can be megabytes each and we only render ~300 chars, so we cap the body
# download with a partial fetch (BODY.PEEK[TEXT]<0.N>).
#
# This bounds the BODY ONLY — never the headers. It used to bound the whole
# message (BODY.PEEK[]<0.N>), on the assumption that "headers are at the top so a
# 4 KB prefix still yields From/Subject/Date". That assumption is false for Gmail:
# Google prepends Delivered-To, three Received hops, ARC-Seal,
# ARC-Message-Signature, ARC-Authentication-Results and two DKIM signatures above
# the originator headers, and those base64 blobs alone exceed 4 KB. Measured on
# this mailbox, header blocks run 5.1-6.9 KB with `From:` at byte ~5371 — so the
# 4096-byte window cut off before From/Subject/Date were ever reached and EVERY
# email came back with empty fields. The headers are now fetched in full.
IMAP_PREVIEW_BYTES = int(os.getenv("IMAP_PREVIEW_BYTES", "4096"))


def _imap_connect() -> imaplib.IMAP4_SSL:
    """Open an authenticated Gmail IMAP connection with a socket timeout.

    The timeout is the critical fix: imaplib.IMAP4_SSL() with no timeout uses a
    blocking socket that can hang indefinitely if Gmail stalls mid-handshake or
    mid-response.
    """
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT_S)
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    return mail


def _imap_logout_quietly(mail) -> None:
    """Best-effort logout so a connection is never leaked, even on error."""
    try:
        mail.logout()
    except Exception:
        pass


def _fetch_message_summary(mail, msg_id, preview_bytes: int = IMAP_PREVIEW_BYTES):
    """Fetch a message's full headers plus a bounded prefix of its body.

    One FETCH, two items: BODY.PEEK[HEADER] for the complete header block and
    BODY.PEEK[TEXT]<0.N> for the body prefix. PEEK means we do NOT flag the
    message as \\Seen (the old "(RFC822)" fetch silently marked every previewed
    email as read), and bounding TEXT means we never pull a multi-megabyte body
    just to render a 300-character preview.

    Headers are fetched in FULL, deliberately. The previous BODY.PEEK[]<0.N>
    capped the whole message, which truncated inside Gmail's own ARC/DKIM header
    blobs and returned a message with no From, Subject or Date at all — see the
    note on IMAP_PREVIEW_BYTES.

    The two literals are matched by their IMAP response prefix rather than by
    position: the server may return the items in either order, and concatenating
    body-before-header produces a stream the email parser reads as one giant
    header-less body — silently reproducing the very bug this replaces.
    """
    _, msg_data = mail.fetch(
        msg_id, f"(BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.{preview_bytes}>)"
    )
    header_bytes = b""
    text_bytes = b""
    for part in (msg_data or []):
        if not (isinstance(part, tuple) and len(part) >= 2
                and isinstance(part[1], (bytes, bytearray))):
            continue
        prefix = bytes(part[0] or b"").upper()
        if b"HEADER" in prefix:
            header_bytes = bytes(part[1])
        elif b"TEXT" in prefix:
            text_bytes = bytes(part[1])

    if not header_bytes:
        return None
    # BODY[HEADER] already ends with the blank line that terminates the header
    # block, so plain concatenation yields a well-formed RFC822 stream. The body
    # is cut mid-part; the parser is lenient about that and still surfaces the
    # first text/plain part, which is all the preview needs.
    return email.message_from_bytes(header_bytes + text_bytes)


# Supabase helpers
def _log_email_to_supabase(
    direction: str,        
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    status: str = "ok",
    error: str = "",
):
    """Persist email event to Supabase `iris_emails` table."""
    if not _supabase:
        return
    try:
        _supabase.table("iris_emails").insert({
            "direction":  direction,
            "from_email": from_addr,
            "to_email":   to_addr,
            "subject":    subject,
            "body":       body[:SAFE_LOG_BODY_CHARS],
            "status":     status,
            "error":      error,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        pass                                    


def _md_to_html(text: str) -> str:
    """Convert markdown to clean HTML for email rendering."""
    import re
    lines = text.split("\n")
    html_lines = []
    in_ul = False
    in_ol = False

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            html_lines.append("</ul>")
            in_ul = False
        if in_ol:
            html_lines.append("</ol>")
            in_ol = False

    def fmt_inline(s: str) -> str:
        # Bold+italic
        s = re.sub(r'\*\*\*(.+?)\*\*\*', r'<strong><em>\1</em></strong>', s)
        # Bold
        s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
        # Italic
        s = re.sub(r'\*(.+?)\*', r'<em>\1</em>', s)
        # Inline code — light grey background, dark text for light theme
        s = re.sub(r'`(.+?)`',
                   r'<code style="background:#F1F5F9;color:#0F172A;padding:2px 6px;'
                   r'border-radius:4px;font-size:12px;font-family:Consolas,monospace;">\1</code>', s)
        return s

    for line in lines:
        stripped = line.rstrip()

        # Horizontal rule
        if re.match(r'^[-*_]{3,}$', stripped):
            close_lists()
            html_lines.append('<hr style="border:none;border-top:1px solid #E2E8F0;margin:20px 0;">')
            continue

        # Headings — deep navy text for maximum contrast on white
        h_match = re.match(r'^(#{1,4})\s+(.*)', stripped)
        if h_match:
            close_lists()
            level = len(h_match.group(1))
            sizes  = {1: "22px", 2: "18px", 3: "16px", 4: "15px"}
            weights = {1: "800", 2: "700", 3: "700", 4: "600"}
            colors  = {1: "#0F172A", 2: "#1E293B", 3: "#1E293B", 4: "#334155"}
            html_lines.append(
                f'<h{level} style="margin:22px 0 8px;font-size:{sizes.get(level,"16px")};'
                f'font-weight:{weights.get(level,"600")};color:{colors.get(level,"#1E293B")};'
                f'line-height:1.3;font-family:\'Segoe UI\',Arial,Helvetica,sans-serif;">'
                f'{fmt_inline(h_match.group(2))}</h{level}>'
            )
            continue

        # Unordered list — dark text
        ul_match = re.match(r'^[\-\*\+]\s+(.*)', stripped)
        if ul_match:
            if in_ol:
                close_lists()
            if not in_ul:
                html_lines.append('<ul style="margin:8px 0 12px 20px;padding:0;color:#334155;">')
                in_ul = True
            html_lines.append(f'<li style="margin:5px 0;line-height:1.7;">{fmt_inline(ul_match.group(1))}</li>')
            continue

        # Ordered list — dark text
        ol_match = re.match(r'^\d+\.\s+(.*)', stripped)
        if ol_match:
            if in_ul:
                close_lists()
            if not in_ol:
                html_lines.append('<ol style="margin:8px 0 12px 20px;padding:0;color:#334155;">')
                in_ol = True
            html_lines.append(f'<li style="margin:5px 0;line-height:1.7;">{fmt_inline(ol_match.group(1))}</li>')
            continue

        # Blank line
        if not stripped:
            close_lists()
            html_lines.append('<div style="height:12px;"></div>')
            continue

        # Regular paragraph — dark charcoal text, high contrast
        close_lists()
        html_lines.append(
            f'<p style="margin:0 0 10px;color:#1A1A2E;line-height:1.85;'
            f'font-size:15px;font-family:\'Segoe UI\',Arial,Helvetica,sans-serif;">'
            f'{fmt_inline(stripped)}</p>'
        )

    close_lists()
    return "\n".join(html_lines)


# HTML email template
def _build_html_email(subject: str, body: str) -> str:
    """Wrap body in IRIS.AI-branded professional HTML email template.

    Design principles:
    - White background (#FFFFFF) with light grey outer wrapper for depth
    - Dark charcoal body text (#1A1A2E) for WCAG AA contrast on white
    - Web-safe font stack (Segoe UI / Arial) — Google Fonts are stripped by Gmail
    - Blue/indigo accent gradient bar for brand identity
    - Clean, minimal layout matching top-tier SaaS transactional email style
    """
    rendered_body = _md_to_html(body)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
</head>
<body style="margin:0;padding:0;background-color:#F4F6F9;
             font-family:'Segoe UI',Arial,Helvetica,sans-serif;">

  <!-- Outer wrapper: light grey page background -->
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         bgcolor="#F4F6F9"
         style="background-color:#F4F6F9;padding:40px 16px;">
    <tr><td align="center">

      <!-- Card container: white background, subtle border -->
      <table role="presentation" width="620" cellpadding="0" cellspacing="0"
             bgcolor="#FFFFFF"
             style="background-color:#FFFFFF;border-radius:12px;overflow:hidden;
                    border:1px solid #DDE3ED;
                    box-shadow:0 4px 24px rgba(15,23,42,0.08);">

        <!-- Top accent gradient bar -->
        <tr>
          <td bgcolor="#2563EB"
              style="height:5px;
                     background:linear-gradient(90deg,#1D4ED8,#2563EB,#4F46E5,#7C3AED);
                     background-color:#2563EB;">
          </td>
        </tr>

        <!-- Header: logo + badge -->
        <tr>
          <td bgcolor="#FFFFFF"
              style="padding:28px 36px 22px;background-color:#FFFFFF;
                     border-bottom:1px solid #E8EDF5;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
              <!-- Logo mark + wordmark -->
              <td style="vertical-align:middle;">
                <table role="presentation" cellpadding="0" cellspacing="0"><tr>
                  <td style="vertical-align:middle;padding-right:12px;">
                    <div style="width:40px;height:40px;border-radius:10px;
                                background:linear-gradient(135deg,#2563EB,#7C3AED);
                                text-align:center;line-height:40px;
                                font-size:18px;font-weight:900;color:#FFFFFF;
                                font-family:'Segoe UI',Arial,sans-serif;">
                      I
                    </div>
                  </td>
                  <td style="vertical-align:middle;">
                    <div style="font-size:20px;font-weight:800;letter-spacing:-0.4px;
                                color:#1D4ED8;
                                font-family:'Segoe UI',Arial,Helvetica,sans-serif;">
                      IRIS.AI
                    </div>
                    <div style="font-size:11px;color:#64748B;font-weight:500;margin-top:2px;
                                font-family:'Segoe UI',Arial,Helvetica,sans-serif;">
                      Intelligent Reasoning &amp; Integration System
                    </div>
                  </td>
                </tr></table>
              </td>
              <!-- Badge -->
              <td align="right" style="vertical-align:middle;">
                <span style="display:inline-block;
                             background-color:#EFF6FF;
                             color:#1D4ED8;
                             font-size:11px;font-weight:700;
                             padding:6px 14px;border-radius:20px;
                             border:1px solid #BFDBFE;
                             letter-spacing:0.4px;
                             font-family:'Segoe UI',Arial,Helvetica,sans-serif;">
                  &#9993; IRIS Mail
                </span>
              </td>
            </tr></table>
          </td>
        </tr>

        <!-- Subject banner -->
        <tr>
          <td bgcolor="#FFFFFF"
              style="padding:24px 36px 16px;background-color:#FFFFFF;">
            <div style="background-color:#EFF6FF;border-left:4px solid #2563EB;
                        border-radius:0 8px 8px 0;padding:16px 20px;">
              <div style="font-size:10px;font-weight:700;color:#2563EB;
                          letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;
                          font-family:'Segoe UI',Arial,Helvetica,sans-serif;">
                Subject
              </div>
              <h1 style="margin:0;font-size:19px;font-weight:700;color:#0F172A;
                         line-height:1.4;
                         font-family:'Segoe UI',Arial,Helvetica,sans-serif;">
                {subject}
              </h1>
            </div>
          </td>
        </tr>

        <!-- Thin divider -->
        <tr>
          <td style="padding:0 36px;">
            <div style="height:1px;background-color:#E8EDF5;"></div>
          </td>
        </tr>

        <!-- Body content -->
        <tr>
          <td bgcolor="#FFFFFF"
              style="padding:28px 36px 36px;background-color:#FFFFFF;">
            <div style="font-size:15px;line-height:1.85;color:#1A1A2E;
                        font-family:'Segoe UI',Arial,Helvetica,sans-serif;">
              {rendered_body}
            </div>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td bgcolor="#F8FAFC"
              style="padding:20px 36px;background-color:#F8FAFC;
                     border-top:1px solid #E8EDF5;border-radius:0 0 12px 12px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
              <td>
                <p style="margin:0;font-size:12px;color:#64748B;
                          font-family:'Segoe UI',Arial,Helvetica,sans-serif;">
                  Sent by <strong style="color:#475569;">IRIS.AI v2.0</strong>
                  &mdash; Enterprise Productivity Agent
                </p>
              </td>
              <td align="right">
                <p style="margin:0;font-size:11px;color:#94A3B8;
                          font-family:'Segoe UI',Arial,Helvetica,sans-serif;">
                  Google Workspace &middot; Tavily &middot; Jira
                </p>
              </td>
            </tr></table>
          </td>
        </tr>

        <!-- Bottom accent bar -->
        <tr>
          <td bgcolor="#4F46E5"
              style="height:4px;
                     background:linear-gradient(90deg,#7C3AED,#4F46E5,#2563EB);
                     background-color:#4F46E5;">
          </td>
        </tr>

      </table>
    </td></tr>
  </table>

</body>
</html>"""


#  TOOL: send_research_email


@tool
@idempotent("send_research_email", key_args=["to_email", "subject", "research_content"])
def send_research_email(
    to_email: str,
    subject: str,
    research_content: str,
    attachment_paths: list[str] = None,
    allow_self: bool = False,
) -> str:
    """
    Send a comprehensive research report or analysis via email with professional HTML formatting.

    Use this tool when the user requests to email, send, or share deep research findings,
    competitive analysis, market insights, or investigation results with stakeholders.

    Call it directly. Delivery is approval-gated by the harness: this call suspends the
    run and shows the user an approve/reject card carrying these exact arguments, so no
    approval has to be obtained beforehand and none can be. Never substitute
    ``create_gmail_draft`` to avoid the gate — a draft delivers to nobody, so the send
    the user asked for silently never happens.

    Delivery priority:
      1. Resend API  — primary (production-grade, ~99% deliverability)
      2. Gmail SMTP  — fallback if Resend key is not configured

    All emails are logged to Supabase `IRIS_emails` table for audit and retrieval.

    Args:
        to_email: Recipient's email address (e.g. 'analyst@company.com')
        subject: Descriptive subject line summarising the research topic
        research_content: Full research findings, analysis, citations, and conclusions
        attachment_paths: Optional list of absolute paths to files to attach to the email.
        allow_self: Set True ONLY to intentionally email the operator's own inbox.
            Left False, a send to the operator's own address is refused as a
            likely mis-addressed client email.

    Returns:
        Confirmation message with delivery method and status
    """
    if not to_email or "@" not in to_email:
        return f"Email delivery failed: '{to_email}' is not a valid email address."

    # Part 6 — recipient verification: refuse a send to any internal address
    # (the bot's own inbox OR the human operator's) unless explicitly allowed.
    # Catches the live "client email → own gmail" misfire. Returns a correction
    # string (a failure prefix, so it is never idempotency-cached and the model
    # can self-correct and resend).
    if not allow_self and to_email.strip().lower() in _internal_addresses():
        return (
            f"Email delivery failed: refusing to send to an internal address "
            f"({to_email}) — this is the operator's own inbox, not a client. "
            f"This is almost always a mis-addressed client email. Confirm the "
            f"intended external recipient and resend; if you truly intend to "
            f"email the operator, set allow_self=True."
        )

    html_body = _build_html_email(subject, research_content)
    method = "unknown"

    resend_attachments = []
    if attachment_paths:
        for path in attachment_paths:
            if os.path.isfile(path) and os.path.getsize(path) <= MAX_ATTACHMENT_BYTES:
                filename = os.path.basename(path)
                with open(path, "rb") as f:
                    file_data = f.read()
                resend_attachments.append({
                    "filename": filename,
                    "content": list(file_data)
                })

    # ── 1. Try Resend ──
    if RESEND_API_KEY:
        try:
            resend.api_key = RESEND_API_KEY
            payload = {
                "from":    RESEND_FROM,
                "to":      [to_email],
                "subject": subject,
                "text":    research_content,
                "html":    html_body,
            }
            if resend_attachments:
                payload["attachments"] = resend_attachments
            resend.Emails.send(payload)
            method = "Resend"
            _log_email_to_supabase("sent", RESEND_FROM, to_email, subject, research_content[:SAFE_LOG_BODY_CHARS])
            return (
                f"✓ Email delivered via Resend\n"
                f"Recipient : {to_email}\n"
                f"Subject   : {subject}\n"
                f"Logged    : Supabase iris_emails ✓"
            )
        except Exception as e:
            method = "Resend (failed)"
            _log_email_to_supabase("sent", RESEND_FROM, to_email, subject, research_content[:SAFE_LOG_BODY_CHARS], "error", str(e))
            # Fall through to Gmail

    # ── 2. Fallback: Gmail REST API (HTTPS — works on Railway; SMTP ports are blocked) ──
    def _send_via_gmail_api(to: str, subject: str, plain_body: str, html_body: str, attachments: list) -> str:
        """Send via Gmail REST API using the stored OAuth refresh token."""
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            if not (GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN):
                return "Gmail API credentials not configured (missing CLIENT_ID/SECRET/REFRESH_TOKEN)"

            creds = Credentials(
                token=None,
                refresh_token=GOOGLE_REFRESH_TOKEN,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=GOOGLE_OAUTH_CLIENT_ID,
                client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
                scopes=["https://www.googleapis.com/auth/gmail.send"],
            )
            service = build("gmail", "v1", credentials=creds)

            msg = MIMEMultipart("mixed")
            msg["From"]       = f"IRIS.AI <{GMAIL_ADDRESS}>" if GMAIL_ADDRESS else "IRIS.AI"
            msg["To"]         = to
            msg["Subject"]    = subject
            msg["Date"]       = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid(domain="iris.ai")

            body_part = MIMEMultipart("alternative")
            body_part.attach(MIMEText(plain_body, "plain", "utf-8"))
            body_part.attach(MIMEText(html_body,  "html",  "utf-8"))
            msg.attach(body_part)

            if attachments:
                for path in attachments:
                    if os.path.isfile(path) and os.path.getsize(path) <= MAX_ATTACHMENT_BYTES:
                        filename = os.path.basename(path)
                        with open(path, "rb") as f:
                            part = MIMEBase("application", "octet-stream")
                            part.set_payload(f.read())
                        encoders.encode_base64(part)
                        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
                        msg.attach(part)

            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            service.users().messages().send(userId="me", body={"raw": raw}).execute()
            return "ok"
        except Exception as exc:
            return f"Gmail API error: {exc}"

    if not GMAIL_ADDRESS:
        return (
            "Email delivery failed: Neither Resend API key nor Gmail credentials are configured.\n"
            "Set RESEND_API_KEY (recommended) or GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET "
            "+ GOOGLE_REFRESH_TOKEN + GMAIL_ADDRESS in your environment."
        )

    gmail_api_result = _send_via_gmail_api(
        to=to_email,
        subject=subject,
        plain_body=research_content,
        html_body=html_body,
        attachments=attachment_paths or [],
    )
    if gmail_api_result == "ok":
        method = "Gmail API"
        _log_email_to_supabase("sent", GMAIL_ADDRESS, to_email, subject, research_content[:SAFE_LOG_BODY_CHARS])
        return (
            f"✓ Email delivered via Gmail API\n"
            f"Recipient : {to_email}\n"
            f"Subject   : {subject}\n"
            f"Logged    : Supabase iris_emails ✓"
        )

    # ── 3. Last resort: Gmail SMTP (may be blocked on some hosts) ──
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return (
            "Email delivery failed: Neither Resend API key nor Gmail credentials are configured.\n"
            "Set RESEND_API_KEY (recommended) or GMAIL_ADDRESS + GMAIL_APP_PASSWORD in .env"
        )

    try:
        msg = MIMEMultipart("mixed")
        msg["From"]    = f"IRIS.AI <{GMAIL_ADDRESS}>"
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg["Date"]    = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="iris.ai")

        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(research_content, "plain", "utf-8"))
        body_part.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(body_part)

        if attachment_paths:
            for path in attachment_paths:
                if os.path.isfile(path) and os.path.getsize(path) <= MAX_ATTACHMENT_BYTES:
                    filename = os.path.basename(path)
                    with open(path, "rb") as f:
                        part = MIMEBase("application", "octet-stream")
                        part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
                    msg.attach(part)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())

        method = "Gmail SMTP"
        _log_email_to_supabase("sent", GMAIL_ADDRESS, to_email, subject, research_content[:SAFE_LOG_BODY_CHARS])
        return (
            f"✓ Email delivered via Gmail SMTP\n"
            f"Recipient : {to_email}\n"
            f"Subject   : {subject}\n"
            f"Logged    : Supabase IRIS_emails ✓"
        )

    except smtplib.SMTPAuthenticationError:
        return "Email delivery failed: Gmail authentication error. Use a Gmail App Password, not your account password."
    except Exception as e:
        _log_email_to_supabase("sent", GMAIL_ADDRESS, to_email, subject, research_content[:SAFE_LOG_BODY_CHARS], "error", str(e))
        return f"Email delivery failed: {str(e)}"



# TOOL: create_gmail_draft


def _find_drafts_mailbox(mail) -> str:
    """Resolve the Drafts mailbox name (IMAP-quoted) for an APPEND.

    Gmail localizes folder names, so we resolve by the ``\\Drafts`` special-use
    attribute advertised in the LIST response rather than hardcoding
    "[Gmail]/Drafts", then fall back to the canonical English name if the
    attribute isn't present. Each LIST line has the shape
    ``(flags) "delimiter" name`` where ``name`` may be quoted or a bare atom, so
    we strip the flag group, skip the delimiter token, and take the remainder as
    the name. The returned value is IMAP-quoted so it can be handed straight to
    ``IMAP4.append`` (which does not quote for us).
    """
    try:
        typ, data = mail.list()
        if typ == "OK" and data:
            for raw in data:
                line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
                if "\\Drafts" not in line:
                    continue
                rest = line.strip()
                # 1. strip the leading "(flags)" group
                if rest.startswith("(") and ")" in rest:
                    rest = rest[rest.index(")") + 1:].strip()
                # 2. skip the delimiter token (quoted like "/" or a bare atom/NIL)
                if rest.startswith('"'):
                    end = rest.find('"', 1)
                    rest = rest[end + 1:].strip() if end != -1 else ""
                else:
                    parts = rest.split(None, 1)
                    rest = parts[1].strip() if len(parts) > 1 else ""
                # 3. remainder is the mailbox name — unquote if quoted
                name = rest[1:-1] if len(rest) >= 2 and rest.startswith('"') and rest.endswith('"') else rest
                if name:
                    return '"%s"' % name
    except Exception:
        pass
    return '"[Gmail]/Drafts"'


@tool
@idempotent("create_gmail_draft", key_args=["to_email", "subject", "body"])
def create_gmail_draft(
    to_email: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
) -> str:
    """Create a real Gmail **draft** (saved in Gmail, NOT sent) for later human review.

    Use this whenever the user asks you to *draft*, *prepare*, *write up*, or
    *hold for review* an email instead of sending it. The draft is saved to the
    operator's Gmail Drafts folder exactly as if composed by hand — they can
    open, edit, and send it themselves. Nothing is delivered to the recipient
    until a human sends it.

    This is the correct tool for "draft an email for review". Do NOT fake a
    draft by writing a local ``*_draft.md`` file, and do NOT use
    ``send_research_email`` for something that should only be drafted — that
    delivers immediately and cannot be recalled.

    The converse is equally wrong and much easier to do by accident: **do NOT
    reach for this tool when the task says *send*.** A draft delivers to nobody,
    so answering a send request with a draft reports success while the recipient
    never hears from you. Sending is approval-gated by the harness, not by you —
    calling ``send_research_email`` is what raises the approval card, so there is
    no approval to secure first and no reason to substitute a draft for it.

    Args:
        to_email: Intended recipient address (stored in the draft's To: header).
        subject: Subject line.
        body: Email body (Markdown supported; rendered to branded HTML).
        cc: Optional comma-separated Cc addresses.
        bcc: Optional comma-separated Bcc addresses.

    Returns:
        Confirmation that the draft was saved to Gmail Drafts, or a ``⚠️``
        correction string the model can act on.
    """
    if not to_email or "@" not in to_email:
        return f"⚠️ Draft not created: '{to_email}' is not a valid recipient address."

    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return (
            "⚠️ Draft not created: Gmail IMAP credentials are not configured. "
            "Set GMAIL_ADDRESS + GMAIL_APP_PASSWORD in .env (a Gmail App Password, "
            "not the account password)."
        )

    html_body = _build_html_email(subject, body)

    msg = MIMEMultipart("alternative")
    msg["From"]       = f"IRIS.AI <{GMAIL_ADDRESS}>"
    msg["To"]         = to_email
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"]    = subject
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="iris.ai")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    mail = None
    try:
        mail = _imap_connect()
        drafts = _find_drafts_mailbox(mail)
        typ, resp = mail.append(
            drafts,
            r"(\Draft)",
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes(),
        )
        if typ != "OK":
            return f"⚠️ Draft not created: Gmail rejected the APPEND ({typ}: {resp})."
    except imaplib.IMAP4.error as e:
        return f"⚠️ Draft not created: Gmail IMAP error: {e}"
    except (socket.timeout, OSError) as e:
        return f"⚠️ Draft not created: network error talking to Gmail: {e}"
    except Exception as e:
        return f"⚠️ Draft not created: {e}"
    finally:
        if mail is not None:
            _imap_logout_quietly(mail)

    _log_email_to_supabase("draft", GMAIL_ADDRESS, to_email, subject, body[:SAFE_LOG_BODY_CHARS])
    return (
        f"✓ Gmail draft saved (NOT sent)\n"
        f"Recipient : {to_email}\n"
        f"Subject   : {subject}\n"
        f"Location  : Gmail → Drafts (open it to review, edit, and send)\n"
        f"Logged    : Supabase IRIS_emails ✓"
    )



# TOOL: read_inbox


@tool
def read_inbox(
    max_emails: int = 10,
    folder: str = "INBOX",
) -> str:
    """
    Read the most recent emails from Gmail inbox via IMAP.

    Use this tool when the user asks to check email, read messages, find replies,
    or retrieve incoming messages sent to IRIS's configured Gmail address.

    All retrieved emails are logged to Supabase `iris_emails` for audit.

    Args:
        max_emails: Maximum number of emails to retrieve (default 10, max 50)
        folder: IMAP mailbox folder to read from (default 'INBOX')

    Returns:
        Formatted list of recent emails with sender, subject, date, and preview
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return "Inbox read failed: Gmail credentials not configured in .env"

    max_emails = min(max_emails, 50)
    deadline = time.monotonic() + IMAP_DEADLINE_S

    mail = None
    try:
        mail = _imap_connect()
        mail.select(folder)

        _, message_ids = mail.search(None, "ALL")
        ids = message_ids[0].split()
        recent_ids = ids[-max_emails:][::-1]   # newest first

        results = []
        truncated = False
        for msg_id in recent_ids:
            # Wall-clock cap: stop cleanly with a partial result instead of
            # running past the agent timeout on a large/slow mailbox.
            if time.monotonic() > deadline:
                truncated = True
                break

            msg = _fetch_message_summary(mail, msg_id)
            if msg is None:
                continue

            # `or` rather than get()'s default: a present-but-blank `Subject:`
            # header returns "", which skips the default and renders a bare label.
            subject   = _decode_header(msg.get("Subject", "")).strip() or "(no subject)"
            from_addr = _decode_header(msg.get("From", ""))
            date_str  = msg.get("Date", "")
            body      = _extract_body(msg)[:600]

            results.append({
                "id":      msg_id.decode(),
                "from":    from_addr,
                "subject": subject,
                "date":    date_str,
                # Whitespace-collapsed, not just stripped. The output below is a
                # line-oriented block format, and the preview is untrusted
                # third-party text: a body containing newlines can forge its own
                # "[4] From    : ..." block and spoof an email that does not
                # exist in the mailbox. Without newlines it cannot start a line.
                "preview": " ".join(body.split()),
            })
            _log_email_to_supabase("received", from_addr, GMAIL_ADDRESS, subject, body)

        if not results:
            return f"No emails found in {folder}."

        header = f"📬 {len(results)} emails retrieved from {folder}:"
        if truncated:
            header += " (partial — stopped early to stay within the time budget)"
        lines = [header + "\n"]
        for i, e in enumerate(results, 1):
            lines.append(
                f"{'─'*55}\n"
                f"[{i}] From    : {e['from']}\n"
                f"    Subject : {e['subject']}\n"
                f"    Date    : {e['date']}\n"
                f"    Preview : {e['preview'][:300]}…\n"
            )
        return "\n".join(lines)

    except (socket.timeout, TimeoutError):
        return (
            "Inbox read timed out talking to Gmail (IMAP). This is usually a slow "
            "connection or a very large mailbox — please try again, or narrow the "
            "request (e.g. fewer emails)."
        )
    except Exception as err:
        return f"Inbox read failed: {err}"
    finally:
        if mail is not None:
            _imap_logout_quietly(mail)


# TOOL: search_emails


@tool
def search_emails(
    query: str,
    folder: str = "INBOX",
    max_results: int = 10,
) -> str:
    """
    Search IRIS's Gmail inbox for emails matching a keyword, sender, or subject.

    Use this tool when the user asks to find a specific email, look for replies
    from a certain person, or retrieve emails about a particular topic.

    Results are also cross-referenced with the Supabase iris_emails log.

    Args:
        query: Search term — searches subject and body (e.g. 'quantum computing', 'from:alice@example.com')
        folder: IMAP mailbox folder (default 'INBOX')
        max_results: Maximum emails to return (default 10)

    Returns:
        Matching emails with sender, subject, date, and content preview
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return "Email search failed: Gmail credentials not configured."

    deadline = time.monotonic() + IMAP_DEADLINE_S

    mail = None
    try:
        mail = _imap_connect()
        mail.select(folder)

        # Build the IMAP search criterion.
        #
        # Prefer Gmail's server-side search index (X-GM-RAW) for free-text
        # queries. The old `TEXT "query"` performs a brute-force full-body scan
        # across the ENTIRE mailbox, which Gmail's IMAP handles very slowly and
        # was the main driver of the search hang. X-GM-RAW runs the SAME fast
        # index that the Gmail UI uses, so it returns in a fraction of the time.
        # from:/subject: prefixes still map to the targeted IMAP keys.
        used_gm_raw = False
        if query.startswith("from:"):
            typ, message_ids = mail.search(None, "FROM", f'"{query[5:].strip()}"')
        elif query.startswith("subject:"):
            typ, message_ids = mail.search(None, "SUBJECT", f'"{query[8:].strip()}"')
        else:
            # Escape embedded double quotes so the quoted X-GM-RAW argument
            # cannot be broken out of by the query string.
            safe_query = query.replace('"', '\\"')
            try:
                typ, message_ids = mail.search(None, "X-GM-RAW", f'"{safe_query}"')
                used_gm_raw = True
            except Exception:
                # Fallback for non-Gmail IMAP servers that lack X-GM-RAW.
                typ, message_ids = mail.search(None, "TEXT", f'"{safe_query}"')

        ids = (message_ids[0].split() if message_ids and message_ids[0] else [])
        recent = ids[-max_results:][::-1]

        if not recent:
            return f"No emails found matching '{query}' in {folder}."

        results = []
        truncated = False
        for msg_id in recent:
            # Wall-clock cap: return what we have rather than exceeding the
            # agent timeout when many/large messages match.
            if time.monotonic() > deadline:
                truncated = True
                break

            msg = _fetch_message_summary(mail, msg_id)
            if msg is None:
                continue
            subject   = _decode_header(msg.get("Subject", "(no subject)"))
            from_addr = _decode_header(msg.get("From", ""))
            date_str  = msg.get("Date", "")
            body      = _extract_body(msg)[:800]
            results.append({"from": from_addr, "subject": subject, "date": date_str, "body": body})

        header = f"🔍 {len(results)} email(s) matching '{query}':"
        if truncated:
            header += " (partial — stopped early to stay within the time budget)"
        lines = [header + "\n"]
        for i, e in enumerate(results, 1):
            lines.append(
                f"{'─'*55}\n"
                f"[{i}] From    : {e['from']}\n"
                f"    Subject : {e['subject']}\n"
                f"    Date    : {e['date']}\n"
                f"    Content : {e['body'][:400]}…\n"
            )
        return "\n".join(lines)

    except (socket.timeout, TimeoutError):
        return (
            "Email search timed out talking to Gmail (IMAP). Try a more specific "
            "query, use a 'from:' or 'subject:' prefix to narrow it, or try again."
        )
    except Exception as err:
        return f"Email search failed: {err}"
    finally:
        if mail is not None:
            _imap_logout_quietly(mail)


# TOOL: get_sent_email_log

@tool
def get_sent_email_log(
    limit: int = 20,
) -> str:
    """
    Retrieve IRIS's sent and received email history from Supabase.

    Use this tool when the user asks 'what emails did you send?', 'show my email history',
    or wants to review past communications that IRIS has handled.

    Args:
        limit: Number of records to retrieve (default 20, max 100)

    Returns:
        Chronological log of all emails sent or received by IRIS
    """
    if not _supabase:
        return "Supabase not configured — email log unavailable. Set SUPABASE_URL and SUPABASE_KEY in .env"

    try:
        limit = min(limit, 100)
        resp = (
            _supabase.table("iris_emails")
            .select("direction,from_email,to_email,subject,status,created_at")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return "No email history found in Supabase."

        lines = [f"📋 Email log — {len(rows)} record(s):\n"]
        for r in rows:
            arrow = "→" if r["direction"] == "sent" else "←"
            lines.append(
                f"{'─'*50}\n"
                f"[{r['direction'].upper()}] {arrow}  {r.get('created_at','')[:16]}\n"
                f"  From   : {r.get('from_email','')}\n"
                f"  To     : {r.get('to_email','')}\n"
                f"  Subject: {r.get('subject','')}\n"
                f"  Status : {r.get('status','')}\n"
            )
        return "\n".join(lines)

    except Exception as e:
        return f"Failed to retrieve email log: {e}"


# TOOL: schedule_research_email
# Imported from schedule_email_tool.py — that module has the correct timezone-aware
# parsing logic. Do NOT redefine this tool here.


# Private helpers

def _decode_header(raw: str) -> str:
    parts = email.header.decode_header(raw)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return " ".join(decoded)


def _extract_body(msg: email.message.Message) -> str:
    """Extract plain text body from a MIME message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:
                    return ""
    else:
        try:
            return msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )
        except Exception:
            return ""
    return ""


# Tool registry
from schedule_email import schedule_research_email

email_tools = [
    send_research_email,
    create_gmail_draft,
    read_inbox,
    search_emails,
    get_sent_email_log,
    schedule_research_email,  # imported from schedule_email_tool.py
]