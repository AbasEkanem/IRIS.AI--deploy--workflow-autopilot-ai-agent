"""test_hitl_sync.py — pre-push gate proving the three HITL representations agree.

The structural HITL gate lives in three places that must never drift apart:
  1. `hitl_tools.IRREVERSIBLE_TOOLS` — the tuple wired into deepagents'
     `interrupt_on` in IRIS.py (the SSOT).
  2. The categorical HITL clause in the orchestrator prompt files.
  3. The per-tool copy in ui/src/components/ApprovalCard.tsx.

The IRIS boot guard runs `verify_hitl_sync(strict=False)` warn-only, so a drift
in prod is logged but never crashes boot. This test runs the SAME check in STRICT
mode, so drift is a HARD failure before a push reaches the live deploy — which is
exactly where the historical HITL failure (a gated tool absent from the prompt →
reworded-redispatch loop → worker CancelledError) needs to be caught.

Runs both as a plain script (`python test_hitl_sync.py`) and under pytest.
"""

from __future__ import annotations

from hitl_tools import (
    HITL_TOOL_CATEGORIES,
    IRREVERSIBLE_TOOLS,
    verify_hitl_sync,
)

# The gate currently covers exactly these tools. This constant is a deliberate
# tripwire: when it changes, the prompt prose, the approval card, and the code
# tuple were all meant to change together — update it consciously, never to make
# a red test go green.
EXPECTED_GATED_COUNT = 29


def test_strict_sync_passes() -> None:
    """verify_hitl_sync(strict=True) returns [] — every representation agrees.

    Raises HITLSyncError (an AssertionError subclass) with the full drift detail
    if the category map, either prompt file, or the approval card has diverged.
    """
    assert verify_hitl_sync(strict=True) == []


def test_category_map_equals_gated_set() -> None:
    """HITL_TOOL_CATEGORIES flattens to exactly the gated tuple — no gaps, no extras."""
    flat = [t for tools in HITL_TOOL_CATEGORIES.values() for t in tools]
    assert len(flat) == len(set(flat)), "a tool appears in more than one category"
    assert set(flat) == set(IRREVERSIBLE_TOOLS), (
        "category map and IRREVERSIBLE_TOOLS disagree: "
        f"missing={set(IRREVERSIBLE_TOOLS) - set(flat)}, "
        f"extra={set(flat) - set(IRREVERSIBLE_TOOLS)}"
    )


def test_no_duplicate_gated_tools() -> None:
    """The SSOT tuple lists each tool exactly once."""
    dupes = sorted({t for t in IRREVERSIBLE_TOOLS if IRREVERSIBLE_TOOLS.count(t) > 1})
    assert not dupes, f"duplicate gated tools: {dupes}"


def test_gated_count_is_stable() -> None:
    """The gate covers the expected number of tools (tripwire — see EXPECTED_GATED_COUNT)."""
    assert len(IRREVERSIBLE_TOOLS) == EXPECTED_GATED_COUNT, (
        f"gated-tool count changed to {len(IRREVERSIBLE_TOOLS)}; if intentional, "
        "update EXPECTED_GATED_COUNT together with the prompt prose and ApprovalCard.tsx"
    )


if __name__ == "__main__":
    verify_hitl_sync(strict=True)
    test_category_map_equals_gated_set()
    test_no_duplicate_gated_tools()
    test_gated_count_is_stable()
    print(
        f"HITL sync OK — {len(IRREVERSIBLE_TOOLS)} gated tools across "
        f"{len(HITL_TOOL_CATEGORIES)} categories; prompt prose + approval card in sync."
    )
