#!/usr/bin/env python3
"""P5 tests: LLM-driven tool calling via stepfun_tool_caller.

Verifies (with mocked requests — no live API):
- LLM returns tool_calls -> executed via ToolRouter, source=internal_l10
- LLM returns text only   -> None (fallback to full_pipeline)
- HTTP error / bad JSON   -> None (fallback, never raises)
- EGO_TOOL_CALL=0 disables the pre-step
- Tool results round 2 -> final composed reply
"""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stepfun_tool_caller as stc
from tool_router import get_router, get_memory_log, clear_memory_log


def _fake_response(content=None, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    payload = {"choices": [{"message": msg}]}
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def _tc(name, args, call_id="call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


class TestLlmToolCall(unittest.TestCase):

    def setUp(self):
        clear_memory_log()
        get_router()

    @patch("stepfun_tool_caller.requests.post")
    def test_llm_tool_call_executes_setblock(self, mock_post):
        clear_memory_log()
        mock_post.side_effect = [
            _fake_response(tool_calls=[_tc("setblock", {"x": 1, "y": 2, "z": 3, "block": "stone"})]),
            _fake_response(content="放好了。"),
        ]
        result = stc.try_llm_tool_call("build stone at 1,2,3")
        self.assertIsNotNone(result)
        self.assertEqual(result["llm_route"], "tool_call")
        self.assertEqual(len(result["tool_executions"]), 1)
        self.assertEqual(result["tool_executions"][0]["tool"], "setblock")
        self.assertEqual(result["tool_executions"][0]["status"], "ok")
        self.assertIn("放好了", result["response"])

        # source attribution lands in shared memory log
        log = get_memory_log(source="internal_l10")
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["tool"], "setblock")

    @patch("stepfun_tool_caller.requests.post")
    def test_llm_text_only_returns_text_route(self, mock_post):
        clear_memory_log()
        mock_post.return_value = _fake_response(content="我不需要工具，直接回答你。")
        result = stc.try_llm_tool_call("今天天气如何")
        # LLM text answer is returned as a text route (NOT dropped):
        # this fixes the "ego received: ..." garbage replies in Hermes.
        self.assertIsNotNone(result)
        self.assertEqual(result["llm_route"], "text")
        self.assertEqual(result["response"], "我不需要工具，直接回答你。")
        self.assertEqual(result["tool_executions"], [])
        self.assertEqual(len(get_memory_log()), 0)  # no tool executed

    @patch("stepfun_tool_caller.requests.post")
    def test_http_error_falls_back(self, mock_post):
        clear_memory_log()
        mock_post.side_effect = RuntimeError("HTTP 500")
        result = stc.try_llm_tool_call("build stone at 1,2,3")
        self.assertIsNone(result)  # never raises

    @patch("stepfun_tool_caller.requests.post")
    def test_bad_arguments_json_still_executes(self, mock_post):
        clear_memory_log()
        mock_post.side_effect = [
            _fake_response(tool_calls=[{
                "id": "call_x",
                "type": "function",
                "function": {"name": "nod", "arguments": "not-json{{{"},
            }]),
            _fake_response(content="好的。"),
        ]
        result = stc.try_llm_tool_call("点头")
        self.assertIsNotNone(result)
        self.assertEqual(result["tool_executions"][0]["tool"], "nod")
        self.assertEqual(result["tool_executions"][0]["status"], "ok")

    @patch.dict(os.environ, {"EGO_TOOL_CALL": "0"})
    def test_env_disable(self):
        result = stc.try_llm_tool_call("build stone at 1,2,3")
        self.assertIsNone(result)

    def test_no_query(self):
        self.assertIsNone(stc.try_llm_tool_call(""))
        self.assertIsNone(stc.try_llm_tool_call(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
