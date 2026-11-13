#!/usr/bin/env python3
"""P3: OpenAI-compatible HTTP shim for tool_router.

Exposes two endpoints:
- POST /v1/chat/completions  (OpenAI Chat Completions protocol)
- GET  /v1/tools             (list available action tools)

The chat endpoint:
1. Receives OpenAI-format request with messages + tools
2. Routes to tool_router.execute() with source='external_openai'
3. Returns OpenAI-format chat.completion response

Uses stdlib http.server (no FastAPI needed — keeps deps minimal).

Run: python ego_openai_shim.py --port 8770
"""
import argparse
import hashlib
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from tool_router import get_router
# Importing bridges auto-registers real executors into ACTION_TOOLS.
import ego_tool_l7_bridge  # noqa: F401  (L7 tools: web/file/cmd)
import ego_mc_body_bridge  # noqa: F401  (MC body tools: attack/goto/scan/follow... via numen MCP 8765)
import ego_internal_bridge  # noqa: F401  (ego_internal: recall/remember/set_goal/check_status)
import ego_minecraft_bridge  # noqa: F401  (real MC: setblock/move/chat/look_at + mc_world_state)
try:
    import ego_openra_body_bridge  # noqa: F401  (OpenRA body: ra_state/build/train/deploy/attack... via 8773 ctrl plane)
except ModuleNotFoundError as e:
    # 2026-10-15: module never existed in this project (lost/never-committed) -> the
    # whole shim died on import and the watchdog re-spawned it every 5 min since 15:45.
    # Optional body: skip it, keep the other 4 bridges live.
    print("[shim] ego_openra_body_bridge missing (%s) - OpenRA tools not registered" % e.name)
import agent_loop  # for _llm_endpoint() in passthrough mode
from ego_llm_gate import llm_post_json  # 2026-08-27: stepfun 429 root-cause fix (cross-process concurrency gate)


SHIM_VERSION = "0.1.0"


def _make_completion_id(seed: str) -> str:
    return "ego-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def _messages_to_text(messages: list) -> str:
    """Extract last user message text from OpenAI messages array."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # OpenAI vision format: extract text parts
                texts = [p.get("text", "") for p in content if p.get("type") == "text"]
                return " ".join(texts)
    return ""


def _messages_to_history(messages: list) -> list:
    """Extract PRIOR turns (everything before the last user message) as
    AgentLoop history, so multi-turn context survives into the LLM call.

    Root-cause fix (2026-08-24): the old shim only extracted the LAST user
    message and dropped everything else — every request looked like turn 1,
    so ego could never remember '我叫指挥官' two turns later.
    """
    history: list = []
    # Find index of last user message; everything before it is history.
    last_user_idx = -1
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            last_user_idx = i
    for msg in messages[:last_user_idx] if last_user_idx > 0 else []:
        role = msg.get("role")
        if role not in ("user", "assistant", "system", "tool"):
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if p.get("type") == "text"
            )
        entry = {"role": role, "content": content or ""}
        if msg.get("tool_calls"):
            entry["tool_calls"] = msg["tool_calls"]
        if msg.get("tool_call_id"):
            entry["tool_call_id"] = msg["tool_call_id"]
        history.append(entry)
    return history


def _detect_tool_call_intent(text: str, available_tools: list) -> dict | None:
    """Best-effort: extract a single tool call from natural language.

    Returns {name, arguments} or None if no clear intent.
    P3 uses heuristic parsing; P5 will swap in LLM-driven schema parsing.
    """
    import re
    text_lower = text.lower()

    # setblock: "build stone at 10,64,-5" / "放石头在 10,64,-5"
    m = re.search(r"(?:build|place|放|建)\s+(\w+)\s+(?:at|在|@)\s*\(?(-?\d+)[,\s]+(-?\d+)[,\s]+(-?\d+)\)?", text_lower)
    if m:
        return {"name": "setblock", "arguments": {"block": m.group(1), "x": int(m.group(2)), "y": int(m.group(3)), "z": int(m.group(4))}}

    # move: "move to 100,64,200" / "移动到 100,64,200"
    m = re.search(r"(?:move to|go to|移动到)\s*\(?(-?\d+)[,\s]+(-?\d+)[,\s]+(-?\d+)\)?", text_lower)
    if m:
        return {"name": "move", "arguments": {"x": int(m.group(1)), "y": int(m.group(2)), "z": int(m.group(3))}}

    # chat: "say hello" / "说 hello"
    m = re.search(r"(?:say|chat|说|告诉)\s+(.+?)(?:\.|$)", text_lower)
    if m:
        msg = m.group(1).strip()
        if msg:
            return {"name": "chat", "arguments": {"message": msg}}

    # nod / shake_head: "nod" / "shake" / "点头" / "摇头"
    if re.search(r"\b(?:nod|点头)\b", text_lower):
        return {"name": "nod", "arguments": {}}
    if re.search(r"\b(?:shake|摇头)\b", text_lower):
        return {"name": "shake_head", "arguments": {}}

    return None


def _call_llm_raw(messages: list, tools: list, *, temperature: float = 0.4) -> dict:
    """Transparent passthrough call: forward the client's FULL message list
    (including role=tool results from previous rounds) + tool schemas to the
    underlying LLM and return its raw response.

    This is the 'real LLM mode': when a client like Hermes declares its own
    tools, ego behaves exactly like any OpenAI-compatible provider — the
    model's tool_calls come back verbatim with finish_reason='tool_calls',
    and the client executes them and sends results back as role=tool turns.
    """
    import requests

    url, model = agent_loop._llm_endpoint()
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 4096,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {"Content-Type": "application/json"}
    try:
        key = open(os.path.join(os.path.dirname(__file__), "ego_key.txt"), encoding="utf-8").read().strip()
        headers["Authorization"] = f"Bearer {key}"
    except Exception:
        pass
    r = llm_post_json(url, headers=headers, json=payload, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def _is_tool_result_round(messages: list) -> bool:
    """True if the last non-empty message is a role=tool result — meaning the
    client just executed our (passed-through) tool_calls and is continuing
    the conversation. In that case we must NOT re-run AgentLoop; forward the
    whole exchange to the LLM so it can compose the final answer or request
    another tool."""
    for msg in reversed(messages):
        role = msg.get("role")
        if role == "tool":
            return True
        if role in ("user", "assistant"):
            return False
    return False


# ── numen adapter (2026-09-15): 把内置大脑接成 ego ──────────────────────
# numen 同伴的内置脑是工具循环: 它只执行模型返回的 tool_calls. 直连 stepfun
# 时决策=stepfun; 接到本 shim 后: 用 ego 的 P5 决策层选动作(capture 模式,
# 不执行), 翻译成 numen 工具词汇交回 numen 自己执行 — 决策来自 ego,
# 感知/执行走 numen 原生路径。
# 判据: numen 请求必带 scan_blocks (ego 没有的同名工具), 或其 system
# prompt 以 "You are Numen" 开头。开关: EGO_NUMEN_ADAPTER=0 关闭此适配。

_NUMEN_MARKER = "scan_blocks"
# 当前 mod build 每轮可能只下发已展开工具的 schema（懒加载），不含 scan_blocks；
# 把这一代 numen 原生工具名也认成标记（2026-10-18 实测：只认 scan_blocks 时
# ego/aria 的请求掉进通用 chat 路径 → 模型自己内部执行工具、永远不产 tool_call）。
_NUMEN_MARKERS = (
    "scan_blocks",
    "scan_nearby_entities",
    "get_self_status",
    "get_world_info",
)


def _is_numen_request(client_tools: set, messages: list) -> bool:
    hit = next((m for m in _NUMEN_MARKERS if m in client_tools), "")
    if hit:
        print(f"[shim-detect] numen=True (tool marker: {hit})", flush=True)
        return True
    for m in messages[:2]:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str) and "you are numen" in c.lower():
            print("[shim-detect] numen=True (system marker)", flush=True)
            return True
    print(f"[shim-detect] numen=False tools={sorted(client_tools)[:10]}", flush=True)
    return False


def _numen_query(messages: list) -> str:
    """最新 user 文本 + (尾巴里最近一条)工具结果 — 作为 ego 的决策输入."""
    parts: list = []
    ut = _messages_to_text(messages)
    if ut:
        parts.append(ut[:6000])
    for msg in reversed(messages[-6:]):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "tool":
            c = msg.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            parts.append("【上一步动作结果】" + str(c)[:1200])
            break
    return "\n".join(parts)


def _numen_system_prompt(tools: list) -> str:
    try:
        from agent_loop import _default_system_prompt
        base = _default_system_prompt(tools)
    except Exception:
        base = "You are TL-Ego, deciding for a body with tools."
    return (
        base
        + "\n\n[NUMEN 身体模式] 你现在是一个 Minecraft numen 同伴的决策脑。"
        "依据下面的事件/对话选择下一步身体动作(mc_* 工具)并调用; "
        "如果此刻只想说话, 不要调用工具, 直接输出要说的内容; "
        "一次只选一个动作, 不要在文字里解释工具调用。"
    )


def _numen_args_goto(a: dict):
    out: dict = {}
    for k in ("x", "y", "z"):
        v = a.get(k)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        out[k] = int(f) if f.is_integer() else f
    return out or None


def _numen_args_mine(a: dict):
    block = a.get("block") or a.get("block_id")
    if not block:
        return None
    block = str(block)
    if ":" not in block:
        block = "minecraft:" + block
    try:
        count = int(a.get("count") or 8)
    except (TypeError, ValueError):
        count = 8
    return {"block_ids": [block], "count": max(1, min(256, count))}


def _numen_args_attack(a: dict):
    ids = a.get("entity_ids")
    return {"entity_ids": ids} if ids else {}


def _numen_args_scan(a: dict):
    try:
        radius = float(a.get("radius") or 16)
    except (TypeError, ValueError):
        radius = 16.0
    return {"type_filter": a.get("type_filter") or "hostile", "radius": radius}


def _numen_args_eat(a: dict):
    item = a.get("item") or a.get("item_id")
    return {"item_id": item} if item else {}


def _numen_args_craft(a: dict):
    item = a.get("recipe") or a.get("item_id") or a.get("item")
    out = {"item_id": item} if item else {}
    try:
        if a.get("count"):
            out["count"] = int(a["count"])
    except (TypeError, ValueError):
        pass
    return out


def _numen_args_drop(a: dict):
    item = a.get("item") or a.get("item_id")
    out = {"item_id": item} if item else {}
    try:
        if a.get("count"):
            out["count"] = int(a["count"])
    except (TypeError, ValueError):
        pass
    return out


def _numen_args_xyz(a: dict):
    try:
        return {"x": int(a["x"]), "y": int(a["y"]), "z": int(a["z"])}
    except (KeyError, TypeError, ValueError):
        return None


# ego 动作词汇 → numen 工具词汇 (名字与参数都翻译; 近似 1:1)
_NUMEN_ACTION_MAP = {
    "mc_goto": ("goto", _numen_args_goto),
    "mc_mine": ("mine", _numen_args_mine),
    "mc_attack": ("attack", _numen_args_attack),
    "mc_scan_nearby_entities": ("scan_nearby_entities", _numen_args_scan),
    "mc_look_around": ("look_around", lambda a: {}),
    "mc_get_self_status": ("get_self_status", lambda a: {}),
    "mc_get_world_info": ("get_world_info", lambda a: {}),
    "mc_collect_items": ("collect_items", lambda a: {}),
    "mc_eat": ("eat", _numen_args_eat),
    "mc_craft": ("craft", _numen_args_craft),
    "mc_drop_items": ("drop_items", _numen_args_drop),
    "mc_inspect_block": ("inspect_block", _numen_args_xyz),
    "mc_interact_at": ("interact_at", _numen_args_xyz),
    "mc_task_status": ("task_status", lambda a: {}),
    "mc_task_stop": ("task_stop", lambda a: {"task_id": a.get("task_id") or ""}),
    "mc_sleep": ("sleep", lambda a: {}),
    "mc_fish": ("fish", lambda a: {}),
    "mc_locate_biome": ("locate_biome", lambda a: {"biome": a.get("biome") or ""}),
    "mc_locate_structure": ("locate_structure", lambda a: {"structure": a.get("structure") or ""}),
}


def _numen_decision(messages: list) -> dict:
    """Run ego's P5 decision layer (capture mode) over a numen request.

    Returns {"tool_call": (numen_name, numen_args)} or {"content": text}.
    """
    from agent_loop import AgentLoop

    query = _numen_query(messages)
    # 观测: 记录每轮的角色序列+尾部消息, 便于复盘 numen 多轮交互 (2026-10-18)
    try:
        _dbg_dir = os.path.join(os.path.dirname(__file__), "logs", "numen_rounds")
        os.makedirs(_dbg_dir, exist_ok=True)
        _old = sorted(f for f in os.listdir(_dbg_dir) if f.endswith(".json"))
        for _f in _old[:-40]:
            os.remove(os.path.join(_dbg_dir, _f))
        with open(os.path.join(_dbg_dir, time.strftime("%m%d-%H%M%S") + f"-{int(time.time()*1000)%1000}.json"), "w", encoding="utf-8") as _fh:
            json.dump(
                {
                    "roles": [m.get("role") for m in messages if isinstance(m, dict)],
                    "last2": messages[-2:],
                },
                _fh,
                ensure_ascii=False,
                indent=1,
            )
    except Exception:
        pass
    history: list = []
    for h in _messages_to_history(messages):
        if not isinstance(h, dict) or h.get("role") == "system":
            continue
        h = dict(h)
        if h.get("role") == "tool":
            # 2026-10-18: 二轮请求里客户端回传的 tool 结果没有 tool_call_id, 直接
            # 喂给下游 LLM 会 400 (invalid tool message, tool_call_id is required)。
            # 结果已由 _numen_query 以【上一步动作结果】并入 query, 这里剔除。
            continue
        h.pop("tool_calls", None)
        c = h.get("content")
        if isinstance(c, str) and len(c) > 4000:
            h["content"] = c[:4000] + "…"
        if h.get("role") == "assistant" and not (isinstance(c, str) and c.strip()):
            continue
        history.append(h)
    history = history[-14:]

    router = get_router()
    tools = []
    for t in router.list_available_tools():
        n = (t.get("function") or {}).get("name", "")
        if n.startswith("mc_") or n == "chat":
            tools.append(t)

    try:
        loop = AgentLoop(capture_tool_calls=True)
    except TypeError:
        # 当前 agent_loop 无 capture 模式（2026-10-18 实测）。此前这里整段抛
        # TypeError，被 _chat_completion 的 except 吞掉、静默降级到 chat 路径，
        # 结果 ego 永远只会聊天、从不产出 numen 工具调用。
        loop = AgentLoop()
    out = loop.run(query, source="external_openai", tools=tools,
                   system_prompt=_numen_system_prompt(tools), history=history)
    raw = out.get("raw_tool_calls") or []
    if not raw:
        # 无 capture：从 loop 内部执行过的工具里抠出意图（tool_executions 带
        # 原始参数），再按 _NUMEN_ACTION_MAP 翻译回 numen 工具交给客户端执行。
        for e in (out.get("tool_executions") or []):
            nm = e.get("tool") or ""
            if not nm:
                continue
            try:
                astr = json.dumps(e.get("arguments") or {}, ensure_ascii=False)
            except Exception:
                astr = "{}"
            raw.append({"function": {"name": nm, "arguments": astr}})
    print(f"[shim-numen] decided stop={out.get('stop_reason')} raw_tool_calls={len(raw)}", flush=True)

    for tc in raw[:4]:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        if not isinstance(args, dict):
            args = {}
        if name == "chat":
            return {"content": (args.get("message") or "").strip() or (out.get("response") or "")}
        mapped = _NUMEN_ACTION_MAP.get(name)
        if mapped:
            nname, conv = mapped
            try:
                nargs = conv(args)
            except Exception:
                nargs = None
            if nargs is not None:
                print(f"[shim-numen] map {name} -> {nname} {nargs}", flush=True)
                return {"tool_call": (nname, nargs)}
    return {"content": (out.get("response") or "").strip()}


def _numen_response(messages: list, model: str) -> dict:
    decision = _numen_decision(messages)
    completion_id = _make_completion_id(f"{time.time()}-numen")
    if "tool_call" in decision:
        nname, nargs = decision["tool_call"]
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_" + completion_id,
                        "type": "function",
                        "function": {
                            "name": nname,
                            "arguments": json.dumps(nargs, ensure_ascii=False),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "system_fingerprint": f"ego-shim-{SHIM_VERSION}-numen",
        }
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": decision.get("content") or ""},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "system_fingerprint": f"ego-shim-{SHIM_VERSION}-numen",
    }


def _chat_completion(req: dict) -> dict:
    """Handle /v1/chat/completions request.

    Pipeline:
    1. Extract last user message
    2. Detect tool call intent (heuristic for P3)
    3. If intent: execute via tool_router
    4. Format response as OpenAI chat.completion
    """
    messages = req.get("messages", [])
    tools_offered = req.get("tools", [])
    model = req.get("model", "ego")

    user_text = _messages_to_text(messages)

    router = get_router()
    available = router.list_available_tools()
    available_names = {t["function"]["name"] for t in available}

    # Client tool-system detection: if the request declares tools but NONE of
    # them are ego's own tools, the client (e.g. Hermes) has its own tool
    # execution loop and CANNOT run ego's tools (check_status/setblock/...).
    # In that mode ego executes its tools internally (via tool_router) and
    # returns the results as plain text ONLY — no tool_calls in the response,
    # otherwise the client tries to execute unknown tools and errors.
    client_tools = {t.get("function", {}).get("name", "") for t in tools_offered if isinstance(t, dict)}
    ego_tool_names = available_names
    client_has_own_tools = bool(client_tools) and not (client_tools & ego_tool_names)

    # ── numen adapter: 决策=ego, 手=numen (see _numen_decision) ─────────
    if (
        user_text
        and os.environ.get("EGO_TOOL_CALL", "1") != "0"
        and os.environ.get("EGO_NUMEN_ADAPTER", "1") != "0"
        and _is_numen_request(client_tools, messages)
    ):
        try:
            return _numen_response(messages, model)
        except Exception as e:
            import traceback
            print(
                f"[shim] numen adapter failed: {type(e).__name__}: {e}\n{traceback.format_exc()}",
                flush=True,
            )
            # 降级: 走原逻辑 — 至少给 numen 一个响应

    completion_id = _make_completion_id(f"{time.time()}-{user_text[:50]}")
    created = int(time.time())

    # ── Passthrough mode (2026-08-24): real-LLM tool calling ─────────────
    # When the client declares its OWN tools (Hermes etc.), ego acts like any
    # OpenAI provider: forward messages+tools verbatim to the LLM, return its
    # tool_calls untouched for the client to execute. Two entry conditions:
    #   a) client_has_own_tools AND this is a tool-result round (client just
    #      executed our previous tool_calls) → MUST continue the exchange;
    #   b) client_has_own_tools on a fresh user turn → let the model decide
    #      between client tools and plain text (its own tools stay internal).
    if client_has_own_tools and os.environ.get("EGO_TOOL_CALL", "1") != "0":
        # Fresh user turn OR tool-result round both pass through — the model
        # decides between client tools and plain text either way.
        try:
            resp = _call_llm_raw(messages, tools_offered)
            msg = (resp.get("choices") or [{}])[0].get("message") or {}
            usage = resp.get("usage") or {}
            tc = msg.get("tool_calls")
            finish = "tool_calls" if tc else "stop"
            response_msg: dict = {"role": "assistant", "content": msg.get("content")}
            if tc:
                response_msg["tool_calls"] = tc
            return {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "message": response_msg, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
                "system_fingerprint": f"ego-shim-{SHIM_VERSION}",
            }
        except Exception as e:
            print(f"[shim] passthrough failed: {type(e).__name__}: {e}", flush=True)
            return {
                "id": _make_completion_id(f"{time.time()}-perr"),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": (
                        f"[ego 内部错误] LLM 调用失败: {type(e).__name__}: {e}"
                    )},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "system_fingerprint": f"ego-shim-{SHIM_VERSION}",
            }

    # P5 completion (2026-08-21): multi-turn agent loop.
    # AgentLoop lets step-3.5-flash call multiple tools in sequence
    # across rounds, with budget protection and failure passthrough.
    # Replaces the single-round try_llm_tool_call() call.
    tool_result = None
    llm_tool_calls = None
    agent_trace = None
    # EGO_TOOL_CALL=0 disables the P5/AgentLoop pre-step entirely (legacy
    # regex path only). Test suites set this to keep LLM out of the loop;
    # the old shim ignored it here, which made those tests hit live stepfun.
    if user_text and os.environ.get("EGO_TOOL_CALL", "1") != "0":
        try:
            from agent_loop import AgentLoop

            loop = AgentLoop()
            _p5 = loop.run(
                user_text,
                source="external_openai",
                history=_messages_to_history(messages),
            )
            # Backward-compat: same fields the old code expected
            tool_result = {
                "status": "ok",
                "result": {
                    "executions": _p5.get("tool_executions", []),
                    "llm_route": _p5.get("llm_route"),
                },
            }
            llm_tool_calls = None  # AgentLoop doesn't preserve raw OpenAI tool_calls
            agent_trace = _p5.get("trace")
            stop_reason = _p5.get("stop_reason")

            response_msg = {
                "role": "assistant",
                "content": _p5.get("response", ""),
                "tool_calls": None,  # clients with own tools can't run ours
            }
            finish_reason = "stop"
            completion_id = _make_completion_id(f"{time.time()}-{user_text[:50]}")
            created = int(time.time())
            return {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": response_msg,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,  # AgentLoop tracks real usage internally
                    "completion_tokens": 0,
                    "total_tokens": _p5.get("total_tokens", 0),
                },
                "system_fingerprint": f"ego-shim-{SHIM_VERSION}",
                # Custom extension: agent_trace for clients that care
                "agent_trace": agent_trace,
                "stop_reason": stop_reason,
            }
        except Exception as e:
            print(f"[shim] P5 agent loop skipped: {e}", flush=True)
            # Error visibility (2026-08-24): never silently fall through to
            # the 'ego received' echo — tell the client what actually failed.
            response_msg = {
                "role": "assistant",
                "content": (
                    f"[ego 内部错误] AgentLoop 调用失败: {type(e).__name__}: {e}。"
                    f"这不是你的问题，请稍后重试或联系运维检查 stepfun API / ego_key.txt。"
                ),
            }
            return {
                "id": _make_completion_id(f"{time.time()}-err"),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": response_msg, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "system_fingerprint": f"ego-shim-{SHIM_VERSION}",
            }

    intent = _detect_tool_call_intent(user_text, available) if user_text else None

    if intent and intent["name"] in available_names:
        # Execute tool
        tool_result = router.execute(intent["name"], intent["arguments"], source="external_openai")
        # Include the execution result in the response for visibility
        result_text = json.dumps(tool_result, ensure_ascii=False)[:500]
        if client_has_own_tools:
            # Client has its own tool system (e.g. Hermes) — return text only
            response_msg = {
                "role": "assistant",
                "content": f"[Executed {intent['name']} via tool_router] {result_text}",
            }
            finish_reason = "stop"
        else:
            # Format as OpenAI tool_calls
            response_msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_" + completion_id,
                    "type": "function",
                    "function": {
                        "name": intent["name"],
                        "arguments": json.dumps(intent["arguments"], ensure_ascii=False),
                    }
                }]
            }
            finish_reason = "tool_calls"
            response_msg["content"] = f"[Executed {intent['name']} via tool_router] {result_text}"
    else:
        # No tool call — return plain content echo
        response_msg = {
            "role": "assistant",
            "content": f"ego received: '{user_text}'. Available tools: {len(available)}. To call a tool, mention it in your message (e.g. 'build stone at 10,64,-5').",
        }
        finish_reason = "stop"

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": response_msg,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": len(user_text),
            "completion_tokens": len(response_msg.get("content") or ""),
            "total_tokens": len(user_text) + len(response_msg.get("content") or ""),
        },
        "system_fingerprint": f"ego-shim-{SHIM_VERSION}",
    }


class ShimHandler(BaseHTTPRequestHandler):
    """HTTP handler for /v1/chat/completions and /v1/tools."""

    def log_message(self, fmt, *args):
        """Suppress default access log."""
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {"status": "ok", "shim": SHIM_VERSION})
        elif parsed.path == "/v1/tools":
            router = get_router()
            self._json(200, {
                "object": "list",
                "data": router.list_available_tools(),
            })
        elif parsed.path == "/":
            self._json(200, {
                "name": "ego OpenAI shim",
                "version": SHIM_VERSION,
                "endpoints": ["/v1/chat/completions", "/v1/tools", "/health"],
            })
        else:
            self._json(404, {"error": f"not found: {parsed.path}"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/v1/chat/completions":
            self._json(404, {"error": f"POST not supported on {parsed.path}"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            req = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._json(400, {"error": f"invalid JSON: {e}"})
            return

        if "messages" not in req:
            self._json(400, {"error": "missing required field: messages"})
            return

        try:
            response = _chat_completion(req)
            # SSE streaming support: Hermes/OpenAI clients send "stream": true
            # and expect text/event-stream chunks. We return the complete
            # response as a single data chunk + [DONE] (no token-by-token
            # streaming, but protocol-compliant).
            if req.get("stream"):
                self._send_sse(200, response)
            else:
                self._json(200, response)
        except Exception as e:
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, status: int, payload: dict):
        """Send a streaming SSE response (OpenAI protocol compliant).

        Clients (Hermes, OpenAI SDK) with stream=true expect chunks shaped
        as: {"choices":[{"delta":{"content":"..."},"index":0}]} with a final
        chunk carrying finish_reason, terminated by 'data: [DONE]'.

        We translate the non-stream response into proper delta chunks:
          1. role chunk          (delta.role=assistant)
          2. content chunk       (delta.content=<full text>)
          3. tool_calls chunks   (if any, delta.tool_calls)
          4. finish chunk        (delta={}, finish_reason=stop/tool_calls)
        """
        choice = payload["choices"][0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason")
        created = payload.get("created", int(time.time()))
        mid = payload.get("id", f"ego-{int(time.time())}")

        def _chunk(delta: dict, fr: str | None = None) -> bytes:
            obj = {
                "id": mid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": payload.get("model", "ego"),
                "choices": [{"index": 0, "delta": delta, "finish_reason": fr}],
            }
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")

        parts = []
        parts.append(_chunk({"role": "assistant", "content": ""}))
        content = message.get("content") or ""
        if content:
            parts.append(_chunk({"content": content}))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            parts.append(_chunk({"tool_calls": tool_calls}))
        parts.append(_chunk({}, fr=finish_reason or "stop"))
        parts.append(b"data: [DONE]\n\n")

        body = b"".join(parts)
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(host: str = "127.0.0.1", port: int = 8770):
    """Start the shim HTTP server."""
    # Pre-warm router so executors bind
    get_router()
    server = HTTPServer((host, port), ShimHandler)
    print(f"[ego-openai-shim] v{SHIM_VERSION} listening on http://{host}:{port}")
    print(f"  POST /v1/chat/completions  (OpenAI protocol)")
    print(f"  GET  /v1/tools")
    print(f"  GET  /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ego-openai-shim] shutting down")
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    run_server(args.host, args.port)
