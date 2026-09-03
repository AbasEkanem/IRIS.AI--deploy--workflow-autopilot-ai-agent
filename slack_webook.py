"""slack_webhook.py — Production Slack event listener + Human-in-the-Loop approval for IRIS / Jessica.

Features:
- Webhook signature verification (HMAC SHA-256 with timestamp tolerance)
- Redis-backed event deduplication and pending approval storage
- Interactive Slack Block Kit approval cards (Approve / Reject)
- Fail-closed security guardrails with optional auto-send bypass
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from pydantic import BaseModel

# Command(resume=...) is how we un-pause a run that stopped on an interrupt_on
# gate (an irreversible specialist tool awaiting human approval). See
# _resume_and_continue / _process_agent_result below.
from langgraph.types import Command

# Both ainvoke sites below are wrapped in ainvoke_with_retry so a transient
# network/DB/checkpointer blip retries the SAME thread_id — LangGraph replays
# from the last durable checkpoint (completed work is not re-run), which is how a
# network-interrupted run "restarts exactly from where it left off". HITL-safe:
# an interrupt is returned as result["__interrupt__"], not raised, so it is never
# retried away (resilience.py excludes GraphInterrupt/GraphBubbleUp regardless).
from resilience import ainvoke_with_retry

logger = structlog.get_logger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
APPROVAL_CHANNEL = os.getenv("SLACK_APPROVAL_CHANNEL", "")

# Explicit opt-in required to bypass approval (fail-closed by default)
ALLOW_AUTO_SEND_WITHOUT_APPROVAL = os.getenv(
    "SLACK_ALLOW_AUTO_SEND_WITHOUT_APPROVAL", "false"
).lower() == "true"

DEDUPE_TTL = 3600      # 1 hour
APPROVAL_TTL = 600     # 10 minutes
RETRY_WINDOW = 300     # 5 minutes

# Graph recursion limit re-asserted on every ainvoke below. The agent is already
# built with this limit via .with_config in IRIS._build_iris, so this is explicit
# insurance rather than the sole source. Reads the SAME IRIS_RECURSION_LIMIT env
# var (same 1000 default) as IRIS.py, so the Slack path and the Studio/Platform
# path never diverge — bump the env var once and both follow. This bounds the
# ORCHESTRATOR's own super-steps per Slack turn (a backstop below the deepagents
# harness's 9_999 default); each subagent `task` delegation runs on a fresh nested
# budget and is unaffected. See IRIS.py for the sizing rationale (~30 super-steps
# per orchestration step; 150 ran dry on a 6-step task, hence the 1000 default).
RECURSION_LIMIT = int(os.getenv("IRIS_RECURSION_LIMIT", "1000"))

# Per-user memory namespace identity — same env var and default as web_api.py, so
# the web and Slack paths land in the SAME per-user namespace ("memory", IRIS_ID,
# user_id) for a given user. Passed as context= at both invoke sites below so
# create_memory_namespace (agent_memory.py) isolates memory per Slack user.
IRIS_ID = os.getenv("IRIS_ID", "iris_default")

# ── Lazy singletons ───────────────────────────────────────────────────────────
_redis: aioredis.Redis | None = None
_http_client: httpx.AsyncClient | None = None


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True,
        )
    return _redis


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=15)
    return _http_client


async def _slack_post(method: str, payload: dict) -> dict:
    """Execute a POST request against the Slack Web API."""
    client = await _get_http_client()
    resp = await client.post(
        f"https://slack.com/api/{method}",
        headers={
            "Authorization": f"Bearer {BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json=payload,
    )
    data = resp.json()
    if not data.get("ok"):
        logger.warning("slack.api_failed", method=method, error=data.get("error"))
    return data


# ── Signature Verification ────────────────────────────────────────────────────
# FAIL CLOSED. This is the ONLY authentication on /slack/events and
# /slack/interactions. Neither carries a session, and /interactions is the HITL
# Approve/Reject path — so an unsigned request there can approve a pending
# irreversible action (an outbound email, a Slack post, a calendar invite) that no
# human approved, defeating the whole `interrupt_on` gate IRIS.py declares over 29
# tools. /events is a free agent-run trigger on the model budget.
#
# The previous behaviour logged a warning and RETURNED when SLACK_SIGNING_SECRET was
# unset, on the assumption that only a dev box would be missing it. The live Railway
# deployment was missing it: an unsigned POST /slack/events was answered HTTP 200 in
# production. "Dev mode" was being inferred from the ABSENCE of configuration, which
# is precisely how it ends up switched on in prod.
#
# So a missing secret is now a refusal, not a bypass. A developer who genuinely wants
# the unauthenticated path has to ask for it by name with IRIS_ALLOW_UNSIGNED_SLACK=1
# — which doubles as the one-variable rollback if closing this breaks a live
# integration, since it takes effect without a redeploy.
_ALLOW_UNSIGNED = os.getenv("IRIS_ALLOW_UNSIGNED_SLACK", "").strip().lower() in ("1", "true", "yes", "on")


def _verify_signature(body: bytes, timestamp: str | None, signature: str | None) -> None:
    """Verify Slack's request signature. Raises HTTPException on any failure.

    Returns None only when the request is genuinely authenticated, or when an
    operator has explicitly opted into the unsigned path (see _ALLOW_UNSIGNED).
    """
    if not SIGNING_SECRET:
        if _ALLOW_UNSIGNED:
            logger.warning(
                "slack.signature_check_disabled",
                msg="SLACK_SIGNING_SECRET unset and IRIS_ALLOW_UNSIGNED_SLACK=1 — accepting "
                    "UNAUTHENTICATED Slack requests, including HITL approvals. Never set "
                    "this outside local development.",
            )
            return
        logger.error(
            "slack.signing_secret_missing",
            msg="SLACK_SIGNING_SECRET is not set — refusing the request. Set it to the "
                "Signing Secret on the Slack app's Basic Information page. To run without "
                "verification on a dev box, set IRIS_ALLOW_UNSIGNED_SLACK=1.",
        )
        raise HTTPException(status_code=401, detail="Slack signature verification is not configured")

    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Missing Slack signature headers")
    # int() on an attacker-supplied header: a non-numeric X-Slack-Request-Timestamp
    # raised ValueError out of an unauthenticated code path, answering 500 instead of
    # 401. Rejected as unauthenticated, which is what it is.
    try:
        skew = abs(int(time.time()) - int(timestamp))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Malformed Slack timestamp") from None
    if skew > RETRY_WINDOW:
        raise HTTPException(status_code=401, detail="Stale Slack request")

    basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    digest = hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    expected = f"v0={digest}"
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")


# ── Deduplication ─────────────────────────────────────────────────────────────
def _dedupe_key(event_id: str) -> str:
    return f"jessica:slack:event:{event_id}"


async def _dedupe(event_id: str) -> bool:
    """Return True if this is a new event."""
    try:
        r = await _get_redis()
        key = _dedupe_key(event_id)
        return bool(await r.set(key, "1", nx=True, ex=DEDUPE_TTL))
    except Exception as exc:
        logger.warning("slack.dedupe_redis_error", error=str(exc))
        # Fail open for dedupe check if Redis is unavailable
        return True


async def _release_dedupe(event_id: str) -> None:
    """Release dedup claim on failure so retries can succeed."""
    try:
        r = await _get_redis()
        await r.delete(_dedupe_key(event_id))
        logger.info("slack.dedupe_released", event_id=event_id)
    except Exception as exc:
        logger.error("slack.dedupe_release_failed", event_id=event_id, error=str(exc))


# ── Active-run registry (crash-recovery support; see recovery.py) ─────────────
# On dispatch (and again on every resume) we record the run's ctx under
# iris:run:active:{thread_id}. recovery.py's startup sweep reads these to learn
# which slack-* threads were executing when a previous process died, and rebuilds
# the ctx to deliver a resumed run's result to the right channel/thread.
#
# Cleared only when the run reaches a TERMINAL graph state (send card posted, or
# no draft produced). Deliberately LEFT set through a HITL pause (the run is
# genuinely still in flight, awaiting a human) and after an unhandled error (so
# the thread stays a recovery candidate). aget_state is the authoritative
# classifier in recovery.py — this registry only prioritises candidates — so a
# stale marker is harmless; a long TTL self-cleans anything the clears miss.
# Every operation is fail-open: a registry hiccup must never break the live path.
RUN_ACTIVE_PREFIX = "iris:run:active:"
RUN_ACTIVE_TTL = int(os.getenv("IRIS_RUN_ACTIVE_TTL", "86400"))  # 24h


def _run_active_key(thread_id: str) -> str:
    return f"{RUN_ACTIVE_PREFIX}{thread_id}"


async def _mark_run_active(ctx: dict) -> None:
    """Record a dispatched/resumed run so recovery.py can find it after a crash."""
    tid = ctx.get("thread_id")
    if not tid:
        return
    try:
        r = await _get_redis()
        await r.set(_run_active_key(tid), json.dumps(ctx, default=str), ex=RUN_ACTIVE_TTL)
    except Exception as exc:
        logger.warning("slack.run_active_mark_failed", thread_id=tid, error=str(exc))


async def _clear_run_active(thread_id: str) -> None:
    """Clear the active-run marker once a run reaches a terminal graph state."""
    if not thread_id:
        return
    try:
        r = await _get_redis()
        await r.delete(_run_active_key(thread_id))
    except Exception as exc:
        logger.warning("slack.run_active_clear_failed", thread_id=thread_id, error=str(exc))


# ── Models ────────────────────────────────────────────────────────────────────
class SlackEventEnvelope(BaseModel):
    type: str
    event_id: str | None = None
    event_time: int | None = None
    challenge: str | None = None
    event: dict[str, Any] | None = None


@dataclass(frozen=True)
class SlackEvent:
    event_id: str
    event_type: str
    channel_id: str
    user_id: str | None
    text: str
    ts: str
    thread_ts: str | None
    raw: dict[str, Any]


def _normalise(envelope: SlackEventEnvelope) -> SlackEvent:
    if not envelope.event_id:
        raise HTTPException(400, "Missing event_id")
    ev = envelope.event or {}
    return SlackEvent(
        event_id=envelope.event_id,
        event_type=ev.get("type", envelope.type),
        channel_id=ev.get("channel", ""),
        user_id=ev.get("user"),
        text=ev.get("text", "").strip(),
        ts=ev.get("ts", ""),
        thread_ts=ev.get("thread_ts"),
        raw=envelope.model_dump(),
    )


def _should_ignore(event: SlackEvent) -> bool:
    """Determine whether an incoming Slack event should be ignored."""
    if not event.text or event.user_id is None:
        return True

    # Ignore bot messages and automated subtypes to avoid infinite loops
    if event.event_type == "bot_message":
        return True

    raw_event = (event.raw or {}).get("event", {})
    if raw_event.get("bot_id"):
        return True

    ignored_subtypes = {
        "bot_message",
        "message_changed",
        "message_deleted",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
    }
    if raw_event.get("subtype") in ignored_subtypes:
        return True

    return False


# ── Core Approval Flow ────────────────────────────────────────────────────────
async def _draft_and_request_approval(event: SlackEvent, app_state: Any) -> None:
    try:
        await _draft_and_request_approval_inner(event, app_state)
    except Exception as exc:
        logger.error("slack.unhandled_error", event_id=event.event_id, exc_info=True)
        await _release_dedupe(event.event_id)


async def _draft_and_request_approval_inner(event: SlackEvent, app_state: Any) -> None:
    # Resolve the active agent instance
    agent = (
        getattr(app_state, "conversation_agent", None)
        or getattr(app_state, "iris_agent", None)
        or getattr(app_state, "agent", None)
    )

    if not agent:
        logger.warning("slack.agent_not_ready", event_id=event.event_id)
        await _release_dedupe(event.event_id)
        return

    prompt = (
        f"[SLACK MESSAGE FROM <@{event.user_id}> IN #{event.channel_id}]\n"
        f"{event.text}\n\n"
        "Draft a concise, professional Slack reply. Use Slack markdown formatting (*bold*, _italic_, `code`, • bullets). "
        "Reply ONLY with the reply text — no meta preamble or commentary."
    )

    thread_identifier = f"slack-{event.channel_id}-{event.thread_ts or event.ts}"

    # Context that stays constant for the whole run — the initial draft, every
    # mid-run irreversible-action approval, and the final send card. It is
    # persisted into each pending Redis record so a stateless interaction
    # callback (and the background resume) can rebuild it without the event.
    ctx = {
        "thread_id": thread_identifier,
        "channel_id": event.channel_id,
        "thread_ts": event.thread_ts or event.ts,
        "user_id": event.user_id,
        "event_id": event.event_id,
        "event_text": event.text,
        "ts": event.ts,
    }

    # Record the run as active BEFORE the first invoke so a crash mid-run leaves a
    # marker recovery.py can find (fail-open — never blocks the dispatch).
    await _mark_run_active(ctx)

    result = await ainvoke_with_retry(
        agent,
        {"messages": [{"role": "user", "content": prompt}]},
        config={
            "configurable": {"thread_id": thread_identifier},
            "recursion_limit": RECURSION_LIMIT,
        },
        context={"iris_id": IRIS_ID, "user_id": ctx["user_id"]},
    )
    await _process_agent_result(agent, result, ctx)


# ── Result router: interrupt → action card, finished → send card ─────────────
def _pending_actions(result: Any) -> list[dict]:
    """Flatten every pending action_request across all interrupts in `result`.

    Returns [] when the run did not pause. Each entry is a dict with keys
    name/args/description, exactly as HumanInTheLoopMiddleware emits it (shape
    confirmed empirically in scratch/mech_test_hitl.py). A single interrupt can
    carry more than one action_request (the model emitted several irreversible
    calls in one step); the resume contract needs one decision per request, so
    we keep the full flattened list and size the decisions to match.
    """
    if not isinstance(result, dict):
        return []
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return []
    actions: list[dict] = []
    for itr in interrupts:
        val = getattr(itr, "value", itr)
        if isinstance(val, dict):
            actions.extend(val.get("action_requests", []) or [])
    return actions


async def _process_agent_result(agent: Any, result: Any, ctx: dict) -> None:
    """Route one ainvoke result.

    Called after the FIRST invoke and again after EVERY Command(resume=...), so:
      • paused on an irreversible tool  → post an action-approval card and wait;
      • re-interrupts after a resume    → post the next card (natural via re-call);
      • finished                        → post the final-reply approval card
                                          (or auto-send when configured).
    """
    pending_actions = _pending_actions(result)
    if pending_actions:
        await _request_action_approval(ctx, pending_actions)
        return  # HITL pause: run still in flight — leave the active-run marker set.

    messages = result.get("messages", []) if isinstance(result, dict) else []
    draft = next((m.content for m in reversed(messages) if getattr(m, "content", None)), None)
    if not draft:
        logger.warning("slack.no_draft", event_id=ctx.get("event_id"))
        await _release_dedupe(ctx.get("event_id", ""))
        await _clear_run_active(ctx.get("thread_id", ""))  # graph finished (empty) — terminal.
        return
    await _request_send_approval(ctx, draft)
    await _clear_run_active(ctx.get("thread_id", ""))  # graph finished (draft ready) — terminal.


# ── Irreversible-action approval (the mid-run HITL gate) ─────────────────────
async def _request_action_approval(ctx: dict, actions: list[dict]) -> None:
    """Persist the paused run and post a card describing the exact pending
    tool call(s). Fail-CLOSED: with no approval channel the run stays paused
    (nothing fires) rather than auto-running an irreversible action."""
    event_id = ctx["event_id"]
    approve_id = f"jessica-resume-approve-{event_id}"
    reject_id = f"jessica-resume-reject-{event_id}"

    pending = {"kind": "resume", "n_decisions": len(actions), "actions": actions, **ctx}
    stored = False
    try:
        r = await _get_redis()
        await r.set(f"jessica:pending:{approve_id}", json.dumps(pending, default=str), ex=APPROVAL_TTL)
        stored = True
    except Exception as exc:
        logger.warning("slack.redis_store_pending_failed", error=str(exc))

    if not APPROVAL_CHANNEL:
        # No auto-send bypass exists for irreversible actions — unlike the draft
        # reply, these must never fire without a human. The run remains paused;
        # ops must configure SLACK_APPROVAL_CHANNEL to release it.
        logger.error(
            "slack.approval_channel_missing_for_action",
            event_id=event_id,
            actions=[a.get("name") for a in actions],
        )
        return

    names = ", ".join(a.get("name", "?") for a in actions)

    if not stored:
        # The pending record could not be persisted (e.g. Redis is down), so an
        # Approve button would resolve to nothing — a dead button that returns
        # "expired or already handled" on click. Fail CLOSED: skip the actionable
        # card and post a clear alert instead. The run stays paused on the durable
        # checkpointer (nothing has fired); the action can be re-surfaced once the
        # backing store recovers.
        logger.error(
            "slack.action_card_suppressed_no_store",
            event_id=event_id,
            actions=[a.get("name") for a in actions],
        )
        try:
            await _slack_post("chat.postMessage", {
                "channel": APPROVAL_CHANNEL,
                "text": (
                    f"⚠️ IRIS needs approval to run: {names} — but the approval store is "
                    "temporarily unavailable, so it can't offer an Approve button right now. "
                    "The run is safely paused and nothing has run. Please retry once the issue clears."
                ),
            })
        except Exception:
            logger.error("slack.action_unavailable_notify_failed", exc_info=True)
        return

    blocks = _build_action_card_blocks(ctx, actions, approve_id, reject_id)
    await _slack_post("chat.postMessage", {
        "channel": APPROVAL_CHANNEL,
        "text": f"IRIS/Jessica needs approval to run: {names}",
        "blocks": blocks,
    })
    logger.info("slack.action_card_posted", event_id=event_id, actions=[a.get("name") for a in actions])


def _build_action_card_blocks(ctx: dict, actions: list[dict], approve_id: str, reject_id: str) -> list[dict]:
    """Block Kit card that shows the actual tool name + args awaiting approval."""
    clean_ts = str(ctx.get("ts", "")).replace(".", "")
    original_link = f"https://slack.com/archives/{ctx['channel_id']}/p{clean_ts}"

    action_sections: list[dict] = []
    for a in actions:
        name = a.get("name", "unknown_tool")
        try:
            args_str = json.dumps(a.get("args", {}), indent=2, default=str, ensure_ascii=False)
        except Exception:
            args_str = str(a.get("args", {}))
        if len(args_str) > 800:
            args_str = args_str[:800] + "\n… (truncated)"
        action_sections.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*`{name}`*\n```{args_str}```"},
        })

    return [
        {"type": "header", "text": {"type": "plain_text",
            "text": "🔐 Irreversible Action — Approval Required", "emoji": True}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Channel:* <#{ctx['channel_id']}>"},
            {"type": "mrkdwn", "text": f"*Requested by:* <@{ctx['user_id']}>"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Triggering message* (<{original_link}|View in Slack>):\n> {str(ctx.get('event_text',''))[:400]}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": "IRIS paused *before* running the action(s) below. Approve to let it proceed, or reject to cancel — IRIS will then continue without it."}},
        *action_sections,
        {"type": "divider"},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve & Run", "emoji": True},
             "style": "primary", "action_id": approve_id, "value": approve_id},
            {"type": "button", "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
             "style": "danger", "action_id": reject_id, "value": approve_id},
        ]},
    ]


# ── Final-reply approval (the existing draft→send gate) ──────────────────────
async def _request_send_approval(ctx: dict, draft: str) -> None:
    """Persist the finished draft and post the reply-approval card (unchanged
    behaviour, now driven by ctx so it also runs at the end of a resumed run)."""
    event_id = ctx["event_id"]
    action_id = f"jessica-approve-{event_id}"
    reject_id = f"jessica-reject-{event_id}"
    pending = {
        "kind": "send",
        "draft": draft,
        "channel_id": ctx["channel_id"],
        "thread_ts": ctx["thread_ts"],
        "user_id": ctx["user_id"],
    }

    try:
        r = await _get_redis()
        await r.set(f"jessica:pending:{action_id}", json.dumps(pending), ex=APPROVAL_TTL)
    except Exception as exc:
        logger.warning("slack.redis_store_pending_failed", error=str(exc))

    # If no approval channel is configured
    if not APPROVAL_CHANNEL:
        if not ALLOW_AUTO_SEND_WITHOUT_APPROVAL:
            logger.error("slack.approval_channel_missing", event_id=event_id)
            return
        logger.warning("slack.auto_send_enabled", event_id=event_id)
        await _slack_post("chat.postMessage", {
            "channel": ctx["channel_id"],
            "text": draft,
            "thread_ts": ctx["thread_ts"],
        })
        return

    # Construct rich Slack Block Kit Approval Card
    clean_ts = str(ctx.get("ts", "")).replace(".", "")
    original_link = f"https://slack.com/archives/{ctx['channel_id']}/p{clean_ts}"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📬 Slack Reply Approval Request",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Channel:* <#{ctx['channel_id']}>",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*User:* <@{ctx['user_id']}>",
                },
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Original Message* (<{original_link}|View in Slack>):\n> {str(ctx.get('event_text',''))[:400]}",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Proposed Draft Reply:*\n{draft}",
            },
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "✅ Approve & Send",
                        "emoji": True,
                    },
                    "style": "primary",
                    "action_id": action_id,
                    "value": action_id,
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "❌ Reject & Discard",
                        "emoji": True,
                    },
                    "style": "danger",
                    "action_id": reject_id,
                    "value": action_id,
                },
            ],
        },
    ]

    await _slack_post("chat.postMessage", {
        "channel": APPROVAL_CHANNEL,
        "text": f"IRIS/Jessica wants to reply to <@{ctx['user_id']}> in <#{ctx['channel_id']}>",
        "blocks": blocks,
    })
    logger.info("slack.approval_card_posted", event_id=event_id)


# ── Router ────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/slack", tags=["slack"])


@router.post("/events")
async def slack_events(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_signature: str | None = Header(default=None, alias="X-Slack-Signature"),
    x_slack_request_timestamp: str | None = Header(default=None, alias="X-Slack-Request-Timestamp"),
    x_slack_retry_num: str | None = Header(default=None, alias="X-Slack-Retry-Num"),
):
    """Handle incoming Slack events subscription callbacks."""
    raw_body = await request.body()
    _verify_signature(raw_body, x_slack_request_timestamp, x_slack_signature)

    envelope = SlackEventEnvelope.model_validate_json(raw_body)

    # URL Verification challenge for Slack app configuration
    if envelope.type == "url_verification" and envelope.challenge:
        return {"challenge": envelope.challenge}

    if envelope.type != "event_callback":
        return {"ok": True}

    event = _normalise(envelope)

    # Deduplicate event
    if not await _dedupe(event.event_id):
        logger.debug("slack.duplicate_event", event_id=event.event_id)
        return {"ok": True}

    # Ignore Slack retries when already in-flight
    if x_slack_retry_num is not None:
        return {"ok": True}

    if _should_ignore(event):
        return {"ok": True}

    background_tasks.add_task(_draft_and_request_approval, event, request.app.state)
    return {"ok": True}


@router.post("/interactions")
async def slack_interactions(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_signature: str | None = Header(default=None, alias="X-Slack-Signature"),
    x_slack_request_timestamp: str | None = Header(default=None, alias="X-Slack-Request-Timestamp"),
):
    """Handle Block Kit interactive button clicks (Approve / Reject).

    Two card families land here:
      • jessica-resume-*  — approve/reject a paused irreversible specialist action.
        Resolved by resuming the graph with Command(resume=...) in the background
        (the run may take many seconds and may pause again on the next action).
      • jessica-approve-/jessica-reject-  — approve/reject the final drafted reply
        (the original, fast, single-HTTP-call path).
    """
    raw_body = await request.body()
    _verify_signature(raw_body, x_slack_request_timestamp, x_slack_signature)

    form = await request.form()
    payload = json.loads(form.get("payload", "{}"))
    actions = payload.get("actions", [])

    if not actions:
        return {"ok": True}

    action = actions[0]
    action_id = action.get("action_id", "")
    value = action.get("value", "")

    r = await _get_redis()

    # ── Resume path: a mid-run irreversible action was gated by interrupt_on ──
    if action_id.startswith("jessica-resume-approve-") or action_id.startswith("jessica-resume-reject-"):
        approved = action_id.startswith("jessica-resume-approve-")
        key = f"jessica:pending:{value}"  # value == the approve id == redis key
        raw = await r.get(key)
        if not raw:
            await _update_approval_card(
                payload, "⚠️ This approval expired or was already handled.", approved=False
            )
            return {"ok": True}
        pending = json.loads(raw)
        await r.delete(key)

        agent = (
            getattr(request.app.state, "conversation_agent", None)
            or getattr(request.app.state, "iris_agent", None)
            or getattr(request.app.state, "agent", None)
        )
        if not agent:
            logger.error("slack.agent_not_ready_on_resume", action_id=action_id)
            await _update_approval_card(
                payload, "⚠️ Agent unavailable to resume the run — please retry.", approved=False
            )
            return {"ok": True}

        # One decision per gated action_request (the resume contract requires the
        # counts to match).
        n = int(pending.get("n_decisions", 1)) or 1
        if approved:
            decisions = [{"type": "approve"} for _ in range(n)]
            status = "✅ Approved — IRIS is running the action and continuing…"
        else:
            approver = payload.get("user", {}).get("name") or payload.get("user", {}).get("username", "a reviewer")
            decisions = [
                {"type": "reject", "message": f"Rejected by {approver} via Slack approval."}
                for _ in range(n)
            ]
            status = "❌ Rejected — IRIS will skip the action and continue…"

        # Ack the card immediately (Slack's 3s budget), then resume off-thread.
        await _update_approval_card(payload, status, approved=approved)
        ctx = {
            k: pending[k]
            for k in ("thread_id", "channel_id", "thread_ts", "user_id", "event_id", "event_text", "ts")
            if k in pending
        }
        background_tasks.add_task(_resume_and_continue, agent, ctx, decisions)
        return {"ok": True}

    # ── Final-reply send path (unchanged) ─────────────────────────────────────
    if action_id.startswith("jessica-approve-"):
        key = f"jessica:pending:{action_id}"
        raw = await r.get(key)
        if not raw:
            return {"ok": True}
        pending = json.loads(raw)
        await r.delete(key)

        # Post the approved reply to the target channel & thread
        await _slack_post("chat.postMessage", {
            "channel": pending["channel_id"],
            "text": pending["draft"],
            "thread_ts": pending["thread_ts"],
        })

        await _update_approval_card(payload, "✅ Approved — reply sent to channel.", approved=True)

    elif action_id.startswith("jessica-reject-"):
        approve_key = f"jessica:pending:{value}"
        await r.delete(approve_key)
        await _update_approval_card(payload, "❌ Rejected — draft discarded.", approved=False)

    return {"ok": True}


async def _resume_and_continue(agent: Any, ctx: dict, decisions: list[dict]) -> None:
    """Resume a paused run with the given HITL decisions, then route the result.

    Runs as a background task (a resume can take many seconds). If the run pauses
    again on the next irreversible action, _process_agent_result posts the next
    card; if it finishes, it posts the final-reply approval card.
    """
    try:
        # A resume puts the run back in flight — re-mark it active so a crash
        # during the resumed leg is still a recovery candidate.
        await _mark_run_active(ctx)
        result = await ainvoke_with_retry(
            agent,
            Command(resume={"decisions": decisions}),
            config={
                "configurable": {"thread_id": ctx["thread_id"]},
                "recursion_limit": RECURSION_LIMIT,
            },
            context={"iris_id": IRIS_ID, "user_id": ctx["user_id"]},
        )
        await _process_agent_result(agent, result, ctx)
    except Exception:
        logger.error(
            "slack.resume_failed",
            thread_id=ctx.get("thread_id"),
            event_id=ctx.get("event_id"),
            exc_info=True,
        )
        await _release_dedupe(ctx.get("event_id", ""))
        # The approval card was already flipped to "running"; without a signal
        # here the reviewer waits on an action that has silently failed. Post a
        # fail-closed alert to the ops approval channel so the stalled run is
        # visible. Guarded so a notify failure can never escape the background task.
        if APPROVAL_CHANNEL:
            try:
                await _slack_post("chat.postMessage", {
                    "channel": APPROVAL_CHANNEL,
                    "text": (
                        "⚠️ IRIS approved an action but hit an internal error while "
                        "running it. Nothing further ran automatically; the error has "
                        "been logged for review."
                    ),
                })
            except Exception:
                logger.error("slack.resume_failure_notify_failed", exc_info=True)


async def _update_approval_card(payload: dict, status_text: str, approved: bool):
    """Update the approval card in Slack to show approval or rejection status."""
    channel = payload.get("channel", {}).get("id")
    ts = payload.get("message", {}).get("ts")
    if not channel or not ts:
        return

    color = "#2eb886" if approved else "#e01e5a"
    approver = payload.get("user", {}).get("name") or payload.get("user", {}).get("username", "someone")

    await _slack_post("chat.update", {
        "channel": channel,
        "ts": ts,
        "text": status_text,
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "📬 Slack Reply Approval Status",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{status_text}*\n_Action taken by @{approver}_",
                },
            },
        ],
        "attachments": [{"color": color}],
    })