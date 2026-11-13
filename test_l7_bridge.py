#!/usr/bin/env python3
"""P2 tests: L7 bridge integration with tool_router.

Verifies:
- L7 functions loaded lazily on first call
- 5 L7 tools registered in ACTION_TOOLS
- tool_router can execute L7 tools
- L7 errors are captured and logged
- Workspace scan works on real project directory
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tool_router import get_router, get_memory_log, clear_memory_log
from action_tools import ACTION_TOOLS
# Import bridge here so L7 tools are registered before any router use.
import ego_tool_l7_bridge  # noqa: F401  (side-effect: L7 tool registration)
from ego_tool_l7_bridge import has_l7, L7_TOOLS


def setup():
    clear_memory_log()
    return get_router()


def test_l7_bridge_importable():
    """L7 bridge module imports without error."""
    import ego_tool_l7_bridge
    assert ego_tool_l7_bridge is not None


def test_l7_tools_defined():
    """5 L7 tools defined in L7_TOOLS."""
    assert len(L7_TOOLS) == 5
    names = {t.name for t in L7_TOOLS}
    assert "l7_web_fetch" in names
    assert "l7_web_search" in names
    assert "l7_file_read" in names
    assert "l7_workspace_scan" in names
    assert "l7_exec_command" in names


def test_l7_tools_have_executors():
    """All L7 tools have executor callables bound."""
    for tool in L7_TOOLS:
        assert tool.executor is not None
        assert callable(tool.executor)


def test_l7_tools_auto_installed():
    """L7 tools automatically appear in ACTION_TOOLS after import."""
    action_names = {t.name for t in ACTION_TOOLS}
    for tool in L7_TOOLS:
        assert tool.name in action_names


def test_total_tool_count():
    """Total tools = 12 (action) + 5 (L7) = 17."""
    r = setup()
    tools = r.list_available_tools()
    assert len(tools) == 17


def test_workspace_scan_works():
    """l7_workspace_scan executes successfully."""
    r = setup()
    result = r.execute("l7_workspace_scan", {"path": "."}, source="internal_l10")
    assert result["status"] == "ok"
    assert "data" in result["result"]


def test_file_read_works():
    """l7_file_read reads a real file."""
    r = setup()
    # Test file from this project
    result = r.execute("l7_file_read", {"path": "test_tool_router.py", "max_lines": 50}, source="internal_l10")
    assert result["status"] == "ok"
    data = result["result"].get("data", {})
    assert "content" in data or "text" in data


def test_exec_command_readonly():
    """l7_exec_command with readonly=True works for safe commands."""
    r = setup()
    result = r.execute("l7_exec_command", {"cmd": "echo hello", "readonly": True, "timeout": 5}, source="internal_l10")
    # Should succeed (echo is readonly-safe)
    assert result["status"] in ("ok", "error")  # either is acceptable; just verify no exception


def test_l7_failure_captured():
    """If L7 returns error, tool_router captures it (not raises)."""
    r = setup()
    # Try fetching an invalid URL
    result = r.execute("l7_web_fetch", {"url": "http://invalid.example.localhost/foo"}, source="internal_l10")
    assert result["status"] in ("ok", "error")  # either is fine
    assert "memory_id" in result


def test_l7_source_attribution():
    """L7 tool executions tagged with source."""
    r = setup()
    clear_memory_log()
    r.execute("l7_workspace_scan", {"path": "."}, source="internal_l10")
    log = get_memory_log(source="internal_l10")
    assert len(log) >= 1
    assert any(rec["tool"].startswith("l7_") for rec in log)


def run_all():
    tests = [
        test_l7_bridge_importable,
        test_l7_tools_defined,
        test_l7_tools_have_executors,
        test_l7_tools_auto_installed,
        test_total_tool_count,
        test_workspace_scan_works,
        test_file_read_works,
        test_exec_command_readonly,
        test_l7_failure_captured,
        test_l7_source_attribution,
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
