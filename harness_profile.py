"""harness_profile.py — make the model a tuning knob instead of a silent recalibration.

What was wrong
--------------
deepagents resolves a `HarnessProfile` per `provider:model-id` and uses it to
overlay prompt text, exclude tools/middleware, and append `extra_middleware`.
IRIS registered nothing. Every one of its models resolved to the empty default,
and the only Nemotron profile that ships with deepagents is keyed to
`nemotron-3-ultra-550b-a55b` — a model IRIS does not run — so its 12 middleware
were unreachable code.

That miss is silent BY CONSTRUCTION. `harness_profiles.py` logs a total miss at

    level = logging.WARNING if _has_any_harness_profile() else logging.DEBUG

so the branch that would have warned could not fire while no profile existed
anywhere. Registering anything at all flips that switch on permanently: from now
on a model whose profile does not resolve logs at WARNING instead of vanishing.

Measured cost of the miss (tmp/probe_middleware_order.py): orchestrator 21
middleware, each domain subagent 11, and the auto-added `general-purpose`
subagent **6 — none of IRIS's guards**. `extra_middleware` is the ONLY channel
that reaches that third stack (graph.py:765); nothing in IRIS.py or
subagent_config.py can touch it.

What this module puts in the profile — and what it deliberately does not
-----------------------------------------------------------------------
The profile is not a place to move the existing stack into. Three measured
constraints bound it:

1. POSITION. graph.py:859 appends profile `extra_middleware` AFTER the caller's
   `middleware=`, so profile middleware lands INNERMOST for `wrap_model_call`.
   `ReasoningTrimMiddleware` documents that it must be outermost, and
   `CachingMemoryMiddleware` that nothing downstream may append to the system
   message. Moving either into the profile inverts its own contract.
2. NO DEDUP. `_merge_middleware` dedupes by type only BETWEEN two profiles,
   never against the caller's `middleware=`. Anything added here that is also
   in IRIS.py runs twice.
3. BLAST RADIUS. `PIIMiddleware` x2 is a security control and
   `CachingMemoryMiddleware` structurally injects the memory block. Behind a
   string key, a typo or a model rename would turn "silent recalibration" into
   silent DISARMAMENT.

So `extra_middleware` here carries exactly one entry —
`TemporalFrameMiddleware`, which is in neither caller list (so it cannot
double-run), is position-indifferent, and is wanted by all seven stacks
including the general-purpose one that manual wiring cannot reach. The
per-model discipline that the other guards would have carried is delivered as
`system_prompt_suffix` instead, which reaches every stack with no ordering
question attached.

Keys are DERIVED, never typed
-----------------------------
The registration key is computed by calling the same `get_model_provider` /
`get_model_identifier` helpers the resolver itself uses, against the live model
instances from loadenv. A key cannot be typo'd into a silent miss because it is
not written down. Measured on this checkout, they come out as:

    openrouter:anthropic/claude-opus-5              orchestrator, maya
    NVIDIA:nvidia/nemotron-3-super-120b-a12b        aurther, sienna, tavia, grace

Note the capital `NVIDIA` (ChatNVIDIA._get_ls_params, chat_models.py:556) and
the lowercase `openrouter`. Both case spellings are registered anyway, plus a
PROVIDER-LEVEL key per family: `_get_harness_profile` falls back from the exact
spec to the provider prefix and MERGES the two, so a model rename or an env flip
downgrades to the provider profile rather than falling off a cliff. This is what
makes the design survive Railway being on a different model than local — as it
currently is (`a860df3` moved local's orchestrator to Claude; prod is on
nemotron-3.5-lightning).

Why the suffix is static and the frame is not
---------------------------------------------
`system_prompt_suffix` is concatenated onto the system prompt
(graph.py:911-920), which is inside both prompt-cache breakpoints
(prompt_caching.py). A STATIC string is byte-identical on every call, so it
invalidates the ~17k-token cached prefix once per deploy, not once per call. The
temporal frame is minute-resolution and therefore must never live there — it
rides as a trailing request-only message instead (see temporal_frame.py).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from deepagents import register_harness_profile
from deepagents.profiles import GeneralPurposeSubagentProfile, HarnessProfile

from temporal_frame import TemporalFrameMiddleware

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

#: Set IRIS_REQUIRE_HARNESS_PROFILE=1 to turn a failed resolution into a startup
#: crash instead of an ERROR log. Off by default: a profile that fails to
#: resolve degrades IRIS to its previous (shipped, working) behaviour, and taking
#: prod down for a degradation is worse than logging it loudly. On in CI is the
#: intended use.
_REQUIRE = os.getenv("IRIS_REQUIRE_HARNESS_PROFILE", "0").strip().lower() in {"1", "true", "yes", "on"}

# ── Per-model discipline, delivered as prompt text ───────────────────────────
# Each suffix states the OUTPUT CONTRACT for one model family, and only the parts
# that family measurably gets wrong. It is not a second copy of the orchestrator
# prompt: this text reaches the five domain subagents and the general-purpose
# subagent, none of which read prompts/iris/execution-protocol.md, so it carries
# the handful of rules that must hold everywhere.
#
# Each line below pairs with a middleware that repairs the same failure after the
# fact. The middleware stays — prompts are advisory and these models are the ones
# that ignore them. Saying it here is simply cheaper than repairing it: a repair
# costs a re-billed ~17k-token prefix, a sentence costs ~40 cached tokens.

_NEMOTRON_SUFFIX = """\
## Output contract (harness-enforced)

- DATES. A current-time block is appended to every request you receive. Anchor
  every date you write on it. Never write a date from memory — your weights are
  stale and the result is confidently wrong. (Measured: two different Nemotrons
  produced "2026-09-09" and "28 August 2026" on the same real day.)
- TOOL CALLS. Emit a tool call through the tool-call channel only. Never write a
  call as JSON in your message text — that text reaches the user verbatim and the
  call never runs. If you catch yourself typing `{"name": ...}`, stop and issue
  the call properly.
- ONE AT A TIME. One tool call per turn, then read its result before deciding the
  next. Do not batch a plan's worth of calls into a single turn.
- NEVER ANSWER EMPTY. An empty message ends the turn and the work silently
  stalls. If you have nothing to add, say what you did and what remains.
- NO IDENTICAL RETRIES. If a call or a delegation already failed with the same
  arguments, change something material or report it as blocked. Repeating it
  verbatim is a loop, and the harness will cut it off.
"""

_CLAUDE_SUFFIX = """\
## Output contract (harness-enforced)

- DATES. A current-time block is appended to every request you receive. Anchor
  every date you write on it — due dates, deadlines, event times, and any
  relative reference. Never write a date from memory.
- NO IDENTICAL RETRIES. If a call or a delegation already failed with the same
  arguments, change something material or report it as blocked rather than
  repeating it verbatim.
"""

# ── The general-purpose subagent ─────────────────────────────────────────────
# deepagents auto-adds a `general-purpose` subagent (graph.py:750) whose measured
# stack is 6 middleware — filesystem, summarization, patch-tool-calls, skills,
# prompt-caching — and NONE of IRIS's guards. It cannot inherit them either:
# graph.py:776 only carries over caller middleware whose `.name` matches a DEFAULT
# GP slot, and every IRIS guard has a unique name.
#
# It is left ENABLED and armed rather than disabled. Disabling it removes a
# fallback for work no specialist covers; arming it costs two profile fields.
#
# `system_prompt` is deliberately left None. Setting it REPLACES deepagents' own
# GP base prompt (graph.py:800), and only `system_prompt_suffix` layers back on
# top — so the cheap win (the discipline block) would cost the harness's own
# instructions. With None, graph.py:806 appends the suffix to the stock prompt,
# which is exactly what is wanted.
#
# `description` IS overridden, because it is the only text the ORCHESTRATOR reads
# when deciding whether to delegate here. Naming the specialists inside it turns
# a silent mis-route into an obvious one.
_GP_SUBAGENT = GeneralPurposeSubagentProfile(
    enabled=True,
    description=(
        "General-purpose fallback with filesystem and skills access but NO domain "
        "tools. Use ONLY for work no specialist covers — file/scratchpad work, "
        "or synthesis across results you already have. It cannot reach Attio "
        "(aurther), Jira (maya), Slack (sienna), the web (tavia), or Google "
        "Workspace (grace); routing domain work here silently accomplishes nothing."
    ),
)


def _iris_guard_middleware() -> list[Any]:
    """Fresh guard instances for one agent stack. Called once per stack.

    Registered as a CALLABLE, not a sequence: `materialize_extra_middleware()`
    re-invokes this at each of the three assembly sites (main agent
    graph.py:859, general-purpose graph.py:765, each declarative subagent
    graph.py:682), so every stack gets its own instances. A fixed sequence would
    share one object across all seven agents — safe for a stateless guard today,
    a cross-agent state leak the moment one carries per-run state. Merging
    preserves this: `_merge_middleware` returns a closure that re-resolves both
    sides at call time rather than collapsing them to instances.

    Only `TemporalFrameMiddleware` belongs here. See the module docstring for the
    three measured reasons the rest of IRIS's stack must NOT be moved in — in
    short: profile middleware lands innermost, it is not deduped against the
    caller's `middleware=`, and a resolution miss would disarm security layers.
    """
    return [TemporalFrameMiddleware()]


# ── Key derivation — computed, never typed ───────────────────────────────────
def _provider_and_identifier(model: BaseChatModel) -> tuple[str | None, str | None]:
    """`(provider, identifier)` for `model`, via the resolver's OWN helpers.

    Imported from `deepagents._models` on purpose, private though it is: these
    are the exact two functions `_harness_profile_for_model` calls to build the
    lookup key (harness_profiles.py:1283-1291). Reimplementing them here would
    let the registration key and the lookup key drift apart, which is the precise
    failure this module exists to remove. The fallback covers the import moving —
    it duplicates the same two lines rather than guessing differently.
    """
    try:
        from deepagents._models import get_model_identifier, get_model_provider
    except Exception:  # noqa: BLE001 — upstream refactor must not break startup
        logger.warning("harness_profile: deepagents._models helpers unavailable; deriving keys locally", exc_info=True)

        def get_model_identifier(m: Any) -> str | None:  # type: ignore[misc]
            for attr in ("model_name", "model"):
                value = getattr(m, attr, None)
                if isinstance(value, str) and value:
                    return value
            return None

        def get_model_provider(m: Any) -> str | None:  # type: ignore[misc]
            try:
                return (m._get_ls_params() or {}).get("ls_provider")
            except Exception:  # noqa: BLE001
                return None

    try:
        return get_model_provider(model), get_model_identifier(model)
    except Exception:  # noqa: BLE001
        logger.warning("harness_profile: could not derive a profile key from %s", type(model).__name__, exc_info=True)
        return None, None


def _spec_keys(provider: str, identifier: str | None) -> list[str]:
    """Every key one model should be registered under, most specific first.

    Both case spellings of the provider, because the two providers IRIS uses
    disagree: `ChatNVIDIA._get_ls_params` reports a capitalised `"NVIDIA"`
    (chat_models.py:556) while ChatOpenRouter reports lowercase `"openrouter"`.
    Registering both costs a dict entry and survives either one changing.

    The bare PROVIDER key matters as much as the exact one: `_get_harness_profile`
    falls back from the exact spec to the provider prefix and MERGES the two
    (harness_profiles.py:1086-1090), so an env flip to an unlisted model still
    resolves to the provider profile instead of falling off a cliff. That is not
    hypothetical here — prod runs nemotron-3.5-lightning while this checkout's
    orchestrator is on Claude.
    """
    providers = list(dict.fromkeys([provider, provider.lower(), provider.upper()]))
    keys = [f"{p}:{identifier}" for p in providers] if identifier else []
    return keys + providers


def _iris_models() -> list[tuple[str, Any]]:
    """The six configured model instances, labelled. Imported lazily.

    Lazily so importing this module never forces loadenv's model construction
    ahead of the caller's own import order — IRIS.py and subagent_config.py both
    import loadenv already, so by registration time these are cached instances.
    A `None` entry means loadenv could not build that model (missing key or
    model name); it is reported, not raised on.
    """
    import loadenv

    return [
        ("orchestrator", getattr(loadenv, "orchestrator_model", None)),
        ("aurther", getattr(loadenv, "attio_subagent_model", None)),
        ("maya", getattr(loadenv, "jira_subagent_model", None)),
        ("sienna", getattr(loadenv, "slack_subagent_model", None)),
        ("tavia", getattr(loadenv, "tavily_subagent_model", None)),
        ("grace", getattr(loadenv, "google_workspace_subagent_model", None)),
    ]


def _suffix_for(provider: str) -> str:
    """The output contract for `provider`'s model family.

    Split by measured failure mode, not by preference. The Nemotron contract
    carries the malformed-tool-JSON, empty-completion and one-call-at-a-time
    rules because those are the failures measured on hosted NIM Nemotrons. Claude
    via OpenRouter does not exhibit them at a rate worth spending prefix tokens
    on, so it gets the date anchor and the no-identical-retry rule only. Any
    future provider inherits the shorter contract — additive and safe.
    """
    return _NEMOTRON_SUFFIX if provider.upper() == "NVIDIA" else _CLAUDE_SUFFIX


_REGISTERED = False


def register_iris_harness_profiles(*, force: bool = False) -> dict[str, Any]:
    """Register a harness profile for every model IRIS actually runs.

    Must be called BEFORE any agent is built — profiles resolve during
    `create_deep_agent`, so a registration that lands afterwards silently does
    nothing to an already-assembled graph.

    Idempotent by default. Upstream registration is additive (it merges onto an
    existing key), so re-registering the same profile is harmless but pointless;
    `force=True` is for tests that re-register after clearing the registry.

    Returns:
        A report: `keys` registered, per-model `resolved` key, and any models
            that could not be keyed. Fed to `verify_iris_harness_profiles`.
    """
    global _REGISTERED  # noqa: PLW0603 — module-level once-guard
    report: dict[str, Any] = {"keys": [], "resolved": {}, "unkeyed": [], "skipped": False}
    if _REGISTERED and not force:
        report["skipped"] = True
        return report

    seen: set[str] = set()
    for label, model in _iris_models():
        if model is None:
            report["unkeyed"].append(f"{label}: model is None (missing key or model name)")
            continue
        provider, identifier = _provider_and_identifier(model)
        if not provider:
            report["unkeyed"].append(f"{label}: no provider from {type(model).__name__}._get_ls_params")
            continue
        profile = HarnessProfile(
            system_prompt_suffix=_suffix_for(provider),
            extra_middleware=_iris_guard_middleware,
            general_purpose_subagent=_GP_SUBAGENT,
        )
        keys = _spec_keys(provider, identifier)
        report["resolved"][label] = keys[0] if keys else None
        for key in keys:
            if key in seen:
                continue
            register_harness_profile(key, profile)
            seen.add(key)
            report["keys"].append(key)

    _REGISTERED = True
    logger.info(
        "harness_profile: registered %d keys for %d models (%s)",
        len(report["keys"]),
        len(report["resolved"]),
        ", ".join(f"{k}->{v}" for k, v in report["resolved"].items()),
    )
    for problem in report["unkeyed"]:
        logger.warning("harness_profile: %s", problem)
    return report


def verify_iris_harness_profiles() -> dict[str, Any]:
    """Resolve each model's profile the way deepagents will, and say so loudly.

    This is the whole point of the exercise. The failure being guarded against is
    not "the profile is wrong" — it is "the profile silently is not there", which
    is how IRIS ran with an unreachable ultra profile for its entire life. So the
    check re-runs the REAL resolver against the REAL model instances and confirms
    the profile that comes back is the one registered above: a lookup that
    matches nothing returns an empty `HarnessProfile()`, which is truthy and
    therefore easy to mistake for success — it is identified here by its EMPTY
    fields, not by `is None`.

    Logs at ERROR on a miss, and raises only when IRIS_REQUIRE_HARNESS_PROFILE is
    set (CI). A miss is a degradation to previously-shipped behaviour, not a
    corruption, so taking prod down for it would be the worse failure.

    Returns:
        `{"ok": bool, "models": {label: {...}}, "unverified": str | None}`.
    """
    try:
        from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model
    except Exception as exc:  # noqa: BLE001 — verification must never break startup
        logger.warning("harness_profile: cannot verify resolution (%s); registration still applied", exc)
        return {"ok": True, "models": {}, "unverified": str(exc)}

    models: dict[str, Any] = {}
    failures: list[str] = []
    for label, model in _iris_models():
        if model is None:
            continue
        provider, identifier = _provider_and_identifier(model)
        expected = f"{provider}:{identifier}" if provider and identifier else None
        profile = _harness_profile_for_model(model, None)
        has_suffix = bool(getattr(profile, "system_prompt_suffix", None))
        guards = [type(m).__name__ for m in profile.materialize_extra_middleware()]
        armed = getattr(profile, "general_purpose_subagent", None) is not None
        ok = has_suffix and TemporalFrameMiddleware.__name__ in guards
        models[label] = {
            "key": expected,
            "suffix": has_suffix,
            "guards": guards,
            "general_purpose_armed": armed,
            "ok": ok,
        }
        if not ok:
            failures.append(f"{label} ({expected})")

    if failures:
        logger.error(
            "harness_profile: NO PROFILE RESOLVED for %s — IRIS is running the empty default; "
            "the temporal frame and the per-model output contract are BOTH absent. "
            "Registered keys this process: %s",
            ", ".join(failures),
            ", ".join(sorted(_registered_keys())) or "<none>",
        )
        if _REQUIRE:
            raise RuntimeError(f"harness profile did not resolve for: {', '.join(failures)}")
    else:
        logger.info("harness_profile: resolution verified for %d models", len(models))
    return {"ok": not failures, "models": models, "unverified": None}


def _registered_keys() -> list[str]:
    """The registry's current keys, for the error message. Best-effort."""
    try:
        from deepagents.profiles.harness.harness_profiles import _HARNESS_PROFILES

        return list(_HARNESS_PROFILES)
    except Exception:  # noqa: BLE001
        return []


def install_iris_harness_profiles() -> dict[str, Any]:
    """Register, then verify. The single call sites should use.

    Kept as one entry point so a caller cannot register without verifying — an
    unverified registration is exactly the state this module was written to end.
    """
    registration = register_iris_harness_profiles()
    verification = verify_iris_harness_profiles()
    return {"registration": registration, "verification": verification}
