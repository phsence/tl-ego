#!/usr/bin/env python3
"""P4 tests: ego_internal_bridge — real executor upgrades.

Verifies:
- 4 ego_internal tools upgraded from stub to real
- recall_memory: returns structured context or graceful error
- remember: writes to Hy-Memory (or returns clear error if unavailable)
- check_status: queries /status or returns clear error if ego down
- set_goal: records to L4 memory + returns goal_id
- Bridge fails gracefully (never crashes router)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tool_router import get_router, get_memory_log, clear_memory_log
from action_tools import ACTION_TOOLS, get_action_tool
# Import bridges here so executors are upgraded before any router use.
import ego_internal_bridge  # noqa: F401  (side-effect: upgrades 4 executors)


def setup():
    clear_memory_log()
    return get_router()


def test_bridge_auto_installed():
    """ego_internal_bridge auto-upgrades 4 executors on import."""
    import ego_internal_bridge
    ego_tools = [t for t in ACTION_TOOLS if t.name in ("recall_memory", "remember", "set_goal", "check_status")]
    assert len(ego_tools) == 4
    for tool in ego_tools:
        assert tool.category == "ego_internal_real", f"{tool.name} category: {tool.category}"


def test_recall_memory_returns_structured():
    """recall_memory returns structured result or graceful error."""
    r = setup()
    result = r.execute("recall_memory", {"query": "test query", "limit": 3}, source="internal_l10")
    assert result["status"] in ("ok", "error")
    if result["status"] == "ok":
        assert "matches" in result["result"]
        assert "context_text" in result["result"]
    else:
        # Error case: must have fallback info
        assert "fallback" in result


def test_remember_returns_status():
    """remember returns ok or graceful error."""
    r = setup()
    result = r.execute("remember", {"content": "P4 test memory entry", "tags": ["test"], "importance": 0.8}, source="internal_l10")
    assert result["status"] in ("ok", "error")
    assert "memory_id" in result


def test_check_status_returns_data_or_clear_error():
    """check_status returns ego data or clear connection error."""
    r = setup()
    result = r.execute("check_status", {}, source="internal_l10")
    assert result["status"] in ("ok", "error")
    # Router returns {status, result?, error?, memory_id}
    if result["status"] == "ok":
        res = result.get("result", {})
        assert "tick" in res or "dominant" in res
    else:
        # On error, router swallows executor error detail
        assert "error" in result
        assert "memory_id" in result


def test_set_goal_records_to_memory():
    """set_goal returns goal_id AND writes to memory."""
    r = setup()
    clear_memory_log()
    result = r.execute("set_goal", {"goal": "Test goal from P4", "priority": "high"}, source="internal_l10")
    assert result["status"] == "ok"
    assert "goal_id" in result["result"]
    # Should have triggered a remember (memory log should have ≥1 entry)
    log = get_memory_log(source="internal_l10")
    assert len(log) >= 1


def test_bridge_failures_dont_crash_router():
    """Even with broken bridge, router still works."""
    r = setup()
    # Force a failure by passing invalid args
    result = r.execute("recall_memory", {"query": ""}, source="internal_l10")
    assert result["status"] in ("ok", "error")
    assert "memory_id" in result


def test_real_executor_differs_from_stub():
    """Real executors produce different output than stubs (or both error gracefully)."""
    r = setup()
    # Real recall_memory (should hit MemoryBridge or return clear error)
    real_result = r.execute("recall_memory", {"query": "ego state"}, source="internal_l10")
    # Has source attribution field that stub didn't
    if real_result["status"] == "ok":
        assert "source" in real_result["result"]


def test_existing_stub_tools_still_work():
    """Stub tools (setblock, nod, etc.) still work after bridge install."""
    r = setup()
    result = r.execute("setblock", {"x": 1, "y": 2, "z": 3, "block": "dirt"}, source="external_openai")
    assert result["status"] == "ok"
    result2 = r.execute("nod", {}, source="internal_l10")
    assert result2["status"] == "ok"


def test_l7_tools_still_working():
    """L7 tools still work after ego_internal_bridge install (no regression)."""
    r = setup()
    result = r.execute("l7_workspace_scan", {"path": "."}, source="internal_l10")
    assert result["status"] == "ok"


def run_all():
    tests = [
        test_bridge_auto_installed,
        test_recall_memory_returns_structured,
        test_remember_returns_status,
        test_check_status_returns_data_or_clear_error,
        test_set_goal_records_to_memory,
        test_bridge_failures_dont_crash_router,
        test_real_executor_differs_from_stub,
        test_existing_stub_tools_still_work,
        test_l7_tools_still_working,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
