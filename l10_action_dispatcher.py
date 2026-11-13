#!/usr/bin/env python3
"""P2: L10 action dispatcher — ego's INTERNAL autonomous tool use.

Bridges the gap the plan called out as critical: ego's own decisions
("I want to build stone") previously fell back to fragile natural-language
parsing outside tool_router, splitting cognition from action. This module
lets ego's own contemplate response text trigger the SAME unified
tool_router that external clients use, tagged source="internal_l10".

Design:
  - Parse intent from ego's response text (heuristic, same patterns as
    ego_openai_shim so internal/external behavior stays consistent).
  - Execute via tool_router.execute(name, args, source="internal_l10").
  - Every execution lands in the shared SQLite memory log (P7), so
    ego can introspect "what did I do today" via ego_memory_api (8772).
  - Pure best-effort: any parse/execute failure is swallowed and returns
    [] — NEVER breaks the contemplate pipeline (fallback preserved).

Usage (injected into tl_ego_server._bg_run_contemplate):
    from l10_action_dispatcher import dispatch_to_tool_router
    execs = dispatch_to_tool_router(resp_text, source="internal_l10")
    if execs:
        result["tool_executions"] = execs
"""
import json
import re
from typing import Optional


# ── Intent patterns ────────────────────────────────────────────────────
# Keep in sync with ego_openai_shim._detect_tool_call_intent where possible.
_INTENT_PATTERNS: list[tuple[re.Pattern, str, list[str]]] = [
    # setblock: "build stone at 10,64,-5" / "放石头在 10,64,-5" / "在 10,64,-5 放石头"
    (re.compile(r"(?:build|place|放|建)\s+(\w+)\s+(?:at|在|@)\s*\(?(-?\d+)[,\s]+(-?\d+)[,\s]+(-?\d+)\)?"), "setblock", ["block", "x", "y", "z"]),
    (re.compile(r"(?:在|@)\s*\(?(-?\d+)[,\s]+(-?\d+)[,\s]+(-?\d+)\)?\s*(?:放|建)\s+(\w+)"), "setblock", ["x", "y", "z", "block"]),
    # move: "move to 100,64,200" / "移动到 100,64,200"
    (re.compile(r"(?:move to|go to|移动到|去)\s*\(?(-?\d+)[,\s]+(-?\d+)[,\s]+(-?\d+)\)?"), "move", ["x", "y", "z"]),
    # chat: "say hello" / "说 hello" / "告诉 world hello"
    (re.compile(r"(?:say|chat|说|告诉)\s+(.+?)(?:[。.!?]|$)"), "chat", ["message"]),
    # nod / shake_head (no word-boundary: Chinese chars are \w, \b fails mid-sentence)
    (re.compile(r"(?:nod|点头)"), "nod", []),
    (re.compile(r"(?:shake|摇头)"), "shake_head", []),
    # recall: "recall stone bridge" / "回忆 石桥" / "回忆一下石桥" — ego asks itself
    (re.compile(r"(?:recall|remember|回忆|查询记忆)\s*(.+?)(?:[。.!?]|$)"), "recall_memory", ["query"]),
    # l7_web_search: "search the web for X" / "搜索 X"
    (re.compile(r"(?:search(?: the web)?(?: for)?|搜索)\s+(.+?)(?:[。.!?]|$)"), "l7_web_search", ["query"]),
    # l7_web_fetch: "fetch https://..." / "打开网页 https://..."
    (re.compile(r"(?:fetch|打开网页|访问)\s+(https?://\S+)"), "l7_web_fetch", ["url"]),
]


def parse_intent(text: str) -> Optional[dict]:
    """Extract a single tool-call intent from ego's response text.

    Returns {"name": str, "arguments": dict} or None if no clear intent.
    """
    if not text:
        return None
    text_lower = text.lower()
    for pattern, name, arg_names in _INTENT_PATTERNS:
        m = pattern.search(text_lower)
        if not m:
            continue
        # Re-match on the ORIGINAL text (span-preserving) so captured
        # argument values keep their original case (e.g. "AGI" stays "AGI").
        raw = text[m.start():m.end()]
        m2 = pattern.search(raw)
        groups = list(m2.groups()) if m2 else []
        # no-arg tools (nod / shake_head)
        if not arg_names:
            return {"name": name, "arguments": {}}
        # numeric args -> int where possible
        args: dict = {}
        for i, arg_name in enumerate(arg_names):
            val = groups[i] if i < len(groups) else ""
            if arg_name in ("x", "y", "z"):
                try:
                    args[arg_name] = int(val)
                except (TypeError, ValueError):
                    args[arg_name] = val
            else:
                args[arg_name] = val
        return {"name": name, "arguments": args}
    return None


def dispatch_to_tool_router(text: str, source: str = "internal_l10",
                            decision_id: str = "") -> list[dict]:
    """Best-effort: parse intent from ego text and execute via tool_router.

    Returns a list of execution records:
        [{"tool": str, "arguments": dict, "status": str, "result": dict|None,
          "error": str|None, "memory_id": str|None}]
    Empty list if no intent or execution failed — callers must treat this
    as a no-op and continue the normal pipeline.

    decision_id links the execution to its /contemplate envelope so a later
    measured /feedback can join credit onto it (silently dropped before).
    """
    if not text:
        return []
    intent = parse_intent(text)
    if intent is None:
        return []
    try:
        from tool_router import get_router
        router = get_router()
        available = {t["function"]["name"] for t in router.list_available_tools()}
        if intent["name"] not in available:
            return []
        outcome = router.execute(intent["name"], intent["arguments"], source=source)
        return [{
            "tool": intent["name"],
            "arguments": intent["arguments"],
            "status": outcome.get("status"),
            "result": outcome.get("result"),
            "error": outcome.get("error"),
            "memory_id": outcome.get("memory_id"),
        }]
    except Exception as e:
        # Never let tool dispatch break the contemplate pipeline.
        return [{
            "tool": intent["name"],
            "arguments": intent["arguments"],
            "status": "error",
            "result": None,
            "error": f"{type(e).__name__}: {e}",
            "memory_id": None,
        }]


if __name__ == "__main__":
    # Quick self-check
    samples = [
        "build stone at 10,64,-5",
        "我想在 20,70,-8 放 oak_planks",
        "move to 100,64,200",
        "say hello world",
        "nod",
        "搜索 AGI 工具调用",
        "今天天气不错",
    ]
    for s in samples:
        intent = parse_intent(s)
        print(f"{s!r:40} -> {intent}")
