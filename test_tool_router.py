#!/usr/bin/env python3
"""P1 tests: unified tool router with L4 memory injection.

Verifies:
- ToolRouter.execute() works for every action tool
- Source attribution is recorded in memory log
- Executor errors are captured and logged (not raised)
- Unknown tool names return structured error
- Invalid source rejected
- Memory log filters by source
- list_available_tools() returns 12 OpenAI schemas
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tool_router import ToolRouter, get_router, get_memory_log, clear_memory_log


def setup():
    clear_memory_log()
    return get_router()


def test_router_initializes():
    """Router singleton initializes without error."""
    r = setup()
    assert isinstance(r, ToolRouter)


def test_list_available_tools_count():
    """Should return 17 action tools in OpenAI format (12 base + 5 L7)."""
    r = setup()
    tools = r.list_available_tools()
    assert len(tools) == 17
    for t in tools:
        assert t["type"] == "function"
        assert "function" in t
        assert "name" in t["function"]
        assert "description" in t["function"]
        assert "parameters" in t["function"]


def test_execute_setblock_ok():
    """setblock with valid args returns ok + memory_id."""
    r = setup()
    result = r.execute(
        "setblock",
        {"x": 10, "y": 64, "z": -5, "block": "stone"},
        source="external_openai",
    )
    assert result["status"] == "ok"
    assert "result" in result
    assert "memory_id" in result
    assert result["memory_id"].startswith("tool_")


def test_execute_chat_ok():
    """chat tool works."""
    r = setup()
    result = r.execute("chat", {"message": "hello world"}, source="internal_l10")
    assert result["status"] == "ok"
    assert "spoken" not in result["result"]  # not tts
    assert result["result"]["message"] == "hello world"


def test_execute_recall_memory():
    """recall_memory returns list of matches."""
    r = setup()
    result = r.execute("recall_memory", {"query": "stone bridge", "limit": 3}, source="internal_l10")
    assert result["status"] == "ok"
    assert "matches" in result["result"]
    assert result["result"]["total"] >= 1


def test_execute_tts_speak():
    """tts_speak returns duration estimate."""
    r = setup()
    result = r.execute("tts_speak", {"text": "你好世界", "speed": 1.0}, source="external_openai")
    assert result["status"] == "ok"
    assert result["result"]["spoken"] == "你好世界"
    assert "duration_estimate" in result["result"]


def test_execute_move():
    """move tool returns destination + path info."""
    r = setup()
    result = r.execute("move", {"x": 100, "y": 64, "z": 200}, source="internal_l10")
    assert result["status"] == "ok"
    assert result["result"]["destination"] == (100, 64, 200)


def test_execute_look_at():
    """look_at tool works."""
    r = setup()
    result = r.execute("look_at", {"target": "100,64,200"}, source="external_openai")
    assert result["status"] == "ok"
    assert result["result"]["looking_at"] == "100,64,200"


def test_execute_nod():
    """nod (no-arg tool) works."""
    r = setup()
    result = r.execute("nod", {}, source="internal_l10")
    assert result["status"] == "ok"
    assert result["result"]["action"] == "nod"


def test_execute_unknown_tool():
    """Unknown tool name → structured error, not exception."""
    r = setup()
    result = r.execute("fly_to_mars", {}, source="external_openai")
    assert result["status"] == "error"
    assert "unknown tool" in result["error"]
    assert result["memory_id"] is not None  # error also injected


def test_execute_invalid_source():
    """Invalid source tag → rejected without execution."""
    r = setup()
    result = r.execute("setblock", {"x": 0, "y": 0, "z": 0, "block": "stone"}, source="bogus_source")
    assert result["status"] == "error"
    assert "invalid source" in result["error"]


def test_executor_exception_captured():
    """Executor raising exception → captured as error, not propagated."""
    r = setup()
    # Inject a broken executor
    from action_tools import get_action_tool
    tool = get_action_tool("setblock")
    original_executor = tool.executor

    def bad_executor(**kwargs):
        raise RuntimeError("simulated world server down")

    tool.executor = bad_executor
    try:
        result = r.execute("setblock", {"x": 0, "y": 0, "z": 0, "block": "stone"}, source="internal_l10")
        assert result["status"] == "error"
        assert "RuntimeError" in result["error"]
        assert "simulated world server down" in result["error"]
    finally:
        tool.executor = original_executor


def test_memory_log_attribution():
    """Memory log records source for each call."""
    r = setup()
    clear_memory_log()
    r.execute("setblock", {"x": 1, "y": 2, "z": 3, "block": "dirt"}, source="internal_l10")
    r.execute("chat", {"message": "hi"}, source="external_openai")
    r.execute("nod", {}, source="evolution_l5_l9")

    log = get_memory_log()
    assert len(log) == 3

    sources = [r["source"] for r in log]
    assert "internal_l10" in sources
    assert "external_openai" in sources
    assert "evolution_l5_l9" in sources


def test_memory_log_filter_by_source():
    """Filter memory log by source."""
    r = setup()
    clear_memory_log()
    r.execute("chat", {"message": "1"}, source="internal_l10")
    r.execute("chat", {"message": "2"}, source="external_openai")
    r.execute("chat", {"message": "3"}, source="internal_l10")

    internal = get_memory_log(source="internal_l10")
    external = get_memory_log(source="external_openai")
    assert len(internal) == 2
    assert len(external) == 1
    assert all(r["source"] == "internal_l10" for r in internal)


def test_memory_log_record_shape():
    """Each memory record has the required fields."""
    r = setup()
    clear_memory_log()
    r.execute("setblock", {"x": 5, "y": 64, "z": 5, "block": "oak_planks"}, source="internal_l10")

    log = get_memory_log()
    assert len(log) == 1
    rec = log[0]
    assert "memory_id" in rec
    assert rec["memory_type"] == "tool_execution"
    assert rec["tool"] == "setblock"
    assert rec["args"]["block"] == "oak_planks"
    assert rec["source"] == "internal_l10"
    assert "ts" in rec
    assert "iso_ts" in rec
    assert rec["error"] is None


def test_memory_log_includes_error():
    """Memory log captures error for failed calls."""
    r = setup()
    clear_memory_log()
    r.execute("bogus_tool", {}, source="internal_l10")

    log = get_memory_log()
    assert len(log) == 1
    assert log[0]["error"] is not None
    assert "unknown tool" in log[0]["error"]


def run_all():
    tests = [
        test_router_initializes,
        test_list_available_tools_count,
        test_execute_setblock_ok,
        test_execute_chat_ok,
        test_execute_recall_memory,
        test_execute_tts_speak,
        test_execute_move,
        test_execute_look_at,
        test_execute_nod,
        test_execute_unknown_tool,
        test_execute_invalid_source,
        test_executor_exception_captured,
        test_memory_log_attribution,
        test_memory_log_filter_by_source,
        test_memory_log_record_shape,
        test_memory_log_includes_error,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  � {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
