#!/usr/bin/env python3
"""P0 tests: unified Tool dataclass + OpenAI schema for tools_registry.

Verifies:
- Existing TOOLS dict still works (backward compatibility — no regression).
- New Tool dataclass wraps existing entries correctly.
- to_openai_schema() produces valid OpenAI function format.
- list_tools() returns ≥10 tools.
- get_tool(name) returns Tool or None.
- to_l5_prompt(state) renders prompt_template with state vars (legacy behavior).
"""
import sys
import os

# Add project dir to path so we can import tools_registry as a top-level module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools_registry import (
    TOOLS,
    Tool,
    get_registry,
    list_tools,
    get_tool,
    reset_registry_cache,
    resolve_tool_by_name,  # existing API, must keep working
)


def test_existing_tools_dict_preserved():
    """Existing TOOLS dict must still exist and have entries (backward compat)."""
    assert len(TOOLS) >= 10, f"TOOLS dict shrunk: {len(TOOLS)} entries"
    assert "spontaneous_action" in TOOLS
    assert "deep_probe_unstable" in TOOLS


def test_existing_resolve_api_works():
    """Existing resolve_tool_by_name must still work (zero regression).
    Returns a single dict (or None), not a tuple."""
    tool_cfg = resolve_tool_by_name("deep_probe_unstable")
    assert tool_cfg is not None
    assert isinstance(tool_cfg, dict)
    assert "description" in tool_cfg
    assert "prompt_template" in tool_cfg


def test_unified_registry_built():
    """Unified registry should wrap every TOOLS entry as a Tool dataclass."""
    reset_registry_cache()
    reg = get_registry()
    assert len(reg) == len(TOOLS)
    sample = reg["deep_probe_unstable"]
    assert isinstance(sample, Tool)
    assert sample.name == "deep_probe_unstable"
    assert "competition" in sample.description.lower() or "unstable" in sample.description.lower()


def test_tool_to_openai_schema():
    """to_openai_schema must produce OpenAI-compatible function format."""
    tool = get_tool("deep_probe_unstable")
    schema = tool.to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "deep_probe_unstable"
    assert isinstance(schema["function"]["description"], str)
    assert "parameters" in schema["function"]
    assert schema["function"]["parameters"]["type"] == "object"


def test_list_tools_returns_openai_format():
    """list_tools() must return list of OpenAI schemas, ≥10 entries."""
    reset_registry_cache()
    tools = list_tools()
    assert isinstance(tools, list)
    assert len(tools) >= 10
    for t in tools:
        assert t["type"] == "function"
        assert "function" in t
        assert "name" in t["function"]
        assert "description" in t["function"]
        assert "parameters" in t["function"]


def test_get_tool_returns_none_for_missing():
    """get_tool(missing) returns None, not KeyError."""
    assert get_tool("nonexistent_tool_xyz") is None


def test_get_tool_returns_tool_instance():
    """get_tool(name) returns Tool dataclass instance for existing."""
    tool = get_tool("causal_lens")
    assert tool is not None
    assert isinstance(tool, Tool)
    assert tool.name == "causal_lens"


def test_to_l5_prompt_renders_state():
    """to_l5_prompt must substitute state vars (existing behavior)."""
    tool = get_tool("deep_probe_unstable")
    state = {
        "dominant": "action",
        "competition": 0.95,
        "concentration": 0.30,
        "stagnation": 10,
        "tick": 1234,
    }
    rendered = tool.to_l5_prompt(state)
    assert "{dominant}" not in rendered, "format() didn't substitute"
    assert "{competition" not in rendered
    assert "action" in rendered
    assert "0.950" in rendered or "0.95" in rendered
    assert "1234" in rendered


def test_executor_defaults_to_none():
    """P0: executor is None by default. P1 will fill in real callables."""
    tool = get_tool("deep_probe_unstable")
    assert tool.executor is None


def test_parameters_default_empty_schema():
    """P0: parameters defaults to empty schema. Will be filled per-tool later."""
    tool = get_tool("deep_probe_unstable")
    assert tool.parameters == {}
    schema = tool.to_openai_schema()
    assert schema["function"]["parameters"]["properties"] == {}
    assert schema["function"]["parameters"]["required"] == []


def run_all():
    tests = [
        test_existing_tools_dict_preserved,
        test_existing_resolve_api_works,
        test_unified_registry_built,
        test_tool_to_openai_schema,
        test_list_tools_returns_openai_format,
        test_get_tool_returns_none_for_missing,
        test_get_tool_returns_tool_instance,
        test_to_l5_prompt_renders_state,
        test_executor_defaults_to_none,
        test_parameters_default_empty_schema,
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
