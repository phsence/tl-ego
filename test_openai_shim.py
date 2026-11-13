#!/usr/bin/env python3
"""P3 tests: OpenAI-compatible HTTP shim.

Verifies:
- /v1/tools endpoint returns tool list
- /v1/chat/completions handles simple chat
- Tool-call intent detection works (setblock, move, chat, nod)
- Tool calls flow through tool_router with source='external_openai'
- Memory log captures external_openai attributions
"""
import json
import sys
import os
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Disable P5 LLM pre-step for the classic regex tests (they test P3 behavior).
# The P5 LLM route gets its own dedicated test below with mocked requests.
os.environ["EGO_TOOL_CALL"] = "0"

# Start shim in background thread
import threading
import ego_openai_shim as shim

# Pre-import bridges so executors are upgraded before shim starts.
import ego_tool_l7_bridge  # noqa: F401
import ego_internal_bridge  # noqa: F401

SHIM_HOST = "127.0.0.1"
SHIM_PORT = 8771  # different port to avoid conflict

_server = shim.HTTPServer((SHIM_HOST, SHIM_PORT), shim.ShimHandler)
_thread = threading.Thread(target=_server.serve_forever, daemon=True)
_thread.start()
time.sleep(0.5)  # let server start


def _post(path: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(f"http://{SHIM_HOST}:{SHIM_PORT}{path}",
                  data=body,
                  headers={"Content-Type": "application/json"},
                  method="POST")
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def _get(path: str) -> tuple[int, dict]:
    req = Request(f"http://{SHIM_HOST}:{SHIM_PORT}{path}")
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def test_root_endpoint():
    status, body = _get("/")
    assert status == 200
    assert body["name"] == "ego OpenAI shim"
    assert "/v1/chat/completions" in body["endpoints"]


def test_health_endpoint():
    status, body = _get("/health")
    assert status == 200
    assert body["status"] == "ok"


def test_list_tools_endpoint():
    status, body = _get("/v1/tools")
    assert status == 200
    assert body["object"] == "list"
    assert len(body["data"]) >= 17  # 12 action + 5 L7
    for tool in body["data"]:
        assert tool["type"] == "function"


def test_chat_no_tool_call():
    """Plain chat without tool intent → stop finish."""
    status, body = _post("/v1/chat/completions", {
        "model": "ego",
        "messages": [{"role": "user", "content": "hello, how are you?"}],
    })
    assert status == 200
    assert body["object"] == "chat.completion"
    assert body["model"] == "ego"
    assert len(body["choices"]) == 1
    choice = body["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert "content" in choice["message"]
    assert "tool_calls" not in choice["message"] or not choice["message"]["tool_calls"]


def test_chat_setblock_intent():
    """'build stone at 10,64,-5' → setblock tool call."""
    from tool_router import get_memory_log, clear_memory_log
    clear_memory_log()
    status, body = _post("/v1/chat/completions", {
        "model": "ego",
        "messages": [{"role": "user", "content": "build stone at 10,64,-5"}],
    })
    assert status == 200
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "setblock"
    args = json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"])
    assert args["block"] == "stone"
    assert args["x"] == 10
    # Memory log
    log = get_memory_log(source="external_openai")
    assert len(log) >= 1
    assert log[-1]["tool"] == "setblock"


def test_chat_move_intent():
    """'move to 100,64,200' → move tool call."""
    status, body = _post("/v1/chat/completions", {
        "model": "ego",
        "messages": [{"role": "user", "content": "move to 100,64,200"}],
    })
    assert status == 200
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "move"


def test_chat_chat_intent():
    """'say hello' → chat tool call."""
    status, body = _post("/v1/chat/completions", {
        "model": "ego",
        "messages": [{"role": "user", "content": "say hello world"}],
    })
    assert status == 200
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "chat"


def test_chat_nod_intent():
    """'nod' → nod tool call."""
    status, body = _post("/v1/chat/completions", {
        "model": "ego",
        "messages": [{"role": "user", "content": "please nod"}],
    })
    assert status == 200
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "nod"


def test_invalid_json_400():
    status, body = _post("/v1/chat/completions", {})
    # empty {} has no messages, should be 400
    assert status == 400
    assert "missing required field" in body["error"]


def test_missing_messages_400():
    # Send valid JSON but no messages
    body_str = '{"model": "ego"}'
    req = Request(f"http://{SHIM_HOST}:{SHIM_PORT}/v1/chat/completions",
                  data=body_str.encode(),
                  headers={"Content-Type": "application/json"},
                  method="POST")
    try:
        with urlopen(req, timeout=5) as resp:
            status = resp.status
    except HTTPError as e:
        status = e.code
    assert status == 400


def test_404_for_unknown_path():
    status, _ = _get("/v1/unknown")
    assert status == 404


def test_openai_response_shape():
    """Response matches OpenAI ChatCompletion schema fields."""
    status, body = _post("/v1/chat/completions", {
        "model": "ego",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert "id" in body
    assert "object" in body and body["object"] == "chat.completion"
    assert "created" in body
    assert "model" in body
    assert "choices" in body
    assert "usage" in body
    usage = body["usage"]
    assert "prompt_tokens" in usage
    assert "completion_tokens" in usage
    assert "total_tokens" in usage


# ── P5: LLM-driven tool route (mocked stepfun, no live API) ──────────────
def test_llm_tool_route_returns_tool_calls():
    """LLM decides tool → shim returns OpenAI tool_calls + executed result."""
    from unittest.mock import patch, MagicMock
    from stepfun_tool_caller import requests as _stc_requests

    def fake_post(url, headers=None, **kwargs):
        payload = kwargs.get("json")
        # Round 1: assistant returns a tool_call
        if not any(m.get("role") == "tool" for m in payload["messages"]):
            msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_llm1",
                    "type": "function",
                    "function": {"name": "setblock",
                                 "arguments": json.dumps({"x": 5, "y": 64, "z": 6, "block": "dirt"})},
                }],
            }
        else:
            # Round 2: final composed reply
            msg = {"role": "assistant", "content": "石头已经放好了。"}
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"choices": [{"message": msg}]}
        return resp

    with patch.dict(os.environ, {"EGO_TOOL_CALL": "1"}), patch.object(_stc_requests, "post", side_effect=fake_post):
        status, body = _post("/v1/chat/completions", {
            "model": "ego",
            "messages": [{"role": "user", "content": "build dirt at 5,64,6"}],
        })
    assert status == 200
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    tc = choice["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "setblock"
    assert "石头已经放好了" in choice["message"]["content"]
    # source must be external_openai
    from tool_router import get_memory_log
    log = get_memory_log(source="external_openai")
    assert any(r["tool"] == "setblock" for r in log)


def test_llm_route_text_only_returns_text():
    """LLM returns text only → shim returns that text directly (no regex)."""
    from unittest.mock import patch, MagicMock
    from stepfun_tool_caller import requests as _stc_requests

    def fake_post(url, headers=None, **kwargs):
        msg = {"role": "assistant", "content": "我是 TL-Ego，很高兴认识你。"}
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"choices": [{"message": msg}]}
        return resp

    with patch.dict(os.environ, {"EGO_TOOL_CALL": "1"}), patch.object(_stc_requests, "post", side_effect=fake_post):
        status, body = _post("/v1/chat/completions", {
            "model": "ego",
            "messages": [{"role": "user", "content": "你是谁"}],
        })
    assert status == 200
    choice = body["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert "TL-Ego" in choice["message"]["content"]
    assert choice["message"].get("tool_calls") is None


def run_all():
    tests = [
        test_root_endpoint,
        test_health_endpoint,
        test_list_tools_endpoint,
        test_chat_no_tool_call,
        test_chat_setblock_intent,
        test_chat_move_intent,
        test_chat_chat_intent,
        test_chat_nod_intent,
        test_invalid_json_400,
        test_missing_messages_400,
        test_404_for_unknown_path,
        test_openai_response_shape,
        test_llm_tool_route_returns_tool_calls,
        test_llm_route_text_only_returns_text,
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
    _server.shutdown()
    return failed == 0


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
