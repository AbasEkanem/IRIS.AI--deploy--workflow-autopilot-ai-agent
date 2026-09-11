r"""test_resilience_status.py — durable regression coverage for the retry gate.

WHY THIS FILE IS TRACKED. The measured production failure (tmp/probe_nemotron.py,
2026-08-26, 40 live calls) was hosted nemotron-3-ultra-550b-a55b failing 30% of
tool-carrying calls as a BARE `Exception("[500] …")` — no `.response` attribute,
so it is neither an `_HTTP_STATUS_ERRORS` instance nor a `_TRANSIENT` one, and the
retry gate said "don't retry" and the run died. The fix (resilience._STATUS_IN_MESSAGE)
parses the leading `[NNN]` out of the message and gates it on the same 429/5xx set.

The only prior coverage lived in `tmp/`, which is gitignored — a shape this costly
deserves a test that ships with the code. This file asserts the behaviour through
`is_retryable_model_error`, the PUBLIC predicate actually wired into
`ModelRetryMiddleware(retry_on=...)`, not just the private `_is_transient`.

Runs under pytest, or directly:
    .\project_venv\Scripts\python.exe test_resilience_status.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
import requests

from resilience import _is_transient, is_retryable_model_error


def _httpx_status(code: int) -> httpx.HTTPStatusError:
    """A structured httpx status error — carries a `.response` the gate can read."""
    req = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    return httpx.HTTPStatusError(f"{code}", request=req, response=httpx.Response(code, request=req))


def _requests_status(code: int) -> requests.exceptions.HTTPError:
    """A requests status error — the ORDERING guard: requests.HTTPError subclasses
    RequestException, which is in _TRANSIENT, so the status-code check must run
    first or a 4xx would be blanket-retried."""
    resp = requests.Response()
    resp.status_code = code
    return requests.exceptions.HTTPError(f"{code}", response=resp)


def _nvidia_bare(code: int) -> Exception:
    """ChatNVIDIA's ACTUAL shape for an upstream HTTP error — verbatim from the
    probe: a bare Exception whose message starts `[NNN] `, with no `.response`."""
    return Exception(
        f"[{code}] {{'message': 'Internal server error', 'type': 'Internal Server "
        f"Error', 'code': {code}}}"
    )


# (label, exception, expected retryable?) — asserted against is_retryable_model_error.
_CASES: list[tuple[str, BaseException, bool]] = [
    # ── The measured hosted-Ultra failure: a BARE Exception, not an httpx error ──
    ("nvidia bare [500] (measured 30%% ultra failure)", _nvidia_bare(500), True),
    ("nvidia bare [429] rate limit", _nvidia_bare(429), True),
    ("nvidia bare [502]", _nvidia_bare(502), True),
    ("nvidia bare [503]", _nvidia_bare(503), True),
    ("nvidia bare [504]", _nvidia_bare(504), True),
    # The 4xx exclusion must survive the message-parsed path too. The 404 is the
    # live Tavia unknown-model failure — retrying it would just burn the budget.
    ("nvidia bare [400] bad request", _nvidia_bare(400), False),
    ("nvidia bare [401] auth", _nvidia_bare(401), False),
    ("nvidia bare [404] unknown model", Exception("[404] Unknown Error {'status': 404}"), False),
    ("nvidia bare [422] bad schema", _nvidia_bare(422), False),
    # ── Message-parse false-positive guards: bracket must LEAD, be 3 digits, retryable ──
    ("code not at start of message", Exception("upstream said [500] eventually"), False),
    ("bracketed non-status number", Exception("[2024] log line"), False),
    ("four digits", Exception("[5000] nonsense"), False),
    ("non-retryable 3-digit code", Exception("[418] teapot"), False),
    ("no bracket at all", Exception("500 internal server error"), False),
    # ── Structured httpx status errors (the transport-object path) ──
    ("httpx 429", _httpx_status(429), True),
    ("httpx 500", _httpx_status(500), True),
    ("httpx 503", _httpx_status(503), True),
    ("httpx 400", _httpx_status(400), False),
    ("httpx 404", _httpx_status(404), False),
    # ── requests status errors — the ORDERING regression guard (4xx must NOT retry) ──
    ("requests 500", _requests_status(500), True),
    ("requests 404", _requests_status(404), False),
    ("requests 401", _requests_status(401), False),
    # ── Connection/timeout shapes — always transient (unchanged prior behaviour) ──
    ("builtin ConnectionError", ConnectionError("reset"), True),
    ("builtin TimeoutError", TimeoutError("slow"), True),
    ("asyncio.TimeoutError", asyncio.TimeoutError(), True),
    ("requests.Timeout", requests.exceptions.Timeout("t"), True),
    ("httpx.ConnectTimeout", httpx.ConnectTimeout("t"), True),
    # ── Programming bugs must always propagate ──
    ("ValueError (bug)", ValueError("nope"), False),
    ("KeyError (bug)", KeyError("k"), False),
]


def test_is_retryable_model_error_status_gate() -> None:
    """The public retry predicate retries 429/5xx (object OR bare-message form) and
    propagates every 4xx and every programming bug."""
    failures = [
        f"{label}: got {is_retryable_model_error(exc)}, expected {expected}"
        for label, exc, expected in _CASES
        if is_retryable_model_error(exc) is not expected
    ]
    assert not failures, "retry-gate mismatches:\n  - " + "\n  - ".join(failures)


def test_bare_500_retries_and_bare_404_propagates() -> None:
    """The headline Group-E assertion, stated plainly: the measured hosted-Ultra
    bare-500 retries; the bare-404 unknown-model error does not."""
    assert is_retryable_model_error(Exception("[500] Internal server error")) is True
    assert is_retryable_model_error(Exception("[404] Unknown model")) is False


def test_requests_4xx_ordering_guard() -> None:
    """Load-bearing: because requests.HTTPError is already in _TRANSIENT, the
    status-code branch must be tested BEFORE the _TRANSIENT isinstance check, or a
    401/404 would be retried. Assert directly on _is_transient (the ordered core)."""
    assert _is_transient(_requests_status(500)) is True
    assert _is_transient(_requests_status(404)) is False
    assert _is_transient(_requests_status(401)) is False


def test_control_flow_exceptions_never_retried() -> None:
    """HITL safety: a LangGraph control-flow exception (a bubbling interrupt) must
    NEVER be retried — retrying it would let ModelRetryMiddleware swallow the pause
    and answer as if approval were granted."""
    try:
        from langgraph.errors import GraphBubbleUp
    except Exception:  # pragma: no cover - langgraph is a hard dep in practice
        return
    assert is_retryable_model_error(GraphBubbleUp()) is False


if __name__ == "__main__":
    test_is_retryable_model_error_status_gate()
    test_bare_500_retries_and_bare_404_propagates()
    test_requests_4xx_ordering_guard()
    test_control_flow_exceptions_never_retried()
    print(f"all {len(_CASES)} status cases + 3 invariant checks passed")
