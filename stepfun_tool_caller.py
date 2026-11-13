#!/usr/bin/env python3
"""P5: LLM-driven tool calling — stepfun decides WHICH tool to call.

Replaces the P2 regex heuristic as the *first* intent source for ego's
contemplate pipeline. The LLM sees the tool schemas and returns proper
OpenAI `tool_calls`; this module executes them through the SAME
ToolRouter (source="internal_l10") and returns a composed final reply.

Fallback chain (never breaks the pipeline):
  1. LLM returns valid tool_calls      -> execute, compose reply, return result
  2. LLM returns text only (no tools)  -> return None (caller falls back to
                                          normal full_pipeline + P2 regex)
  3. HTTP error / timeout / bad JSON   -> return None (same fallback)

Design note: this runs as a *pre-step* in _bg_run_contemplate, BEFORE the
expensive neural full_pipeline. If the LLM decides tools are needed, we
skip the neural pipeline entirely (tools ARE the answer). If not, the
pipeline runs as usual — so a chat question pays for one extra cheap LLM
round-trip, and a tool request avoids a 6s neural computation.

Env controls:
  EGO_TOOL_CALL=0   disable the P5 pre-step entirely (P2 regex only)
  EGO_LLM_URL/EGO_LLM_MODEL/EGO_LLM_API_KEY  (inherited from tl_ego_server)
"""
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from ego_llm_gate import llm_post_json  # 2026-08-27: stepfun 429 root-cause fix (cross-process concurrency gate)

# 2026-10-14: 非命中 gate 的 reason 限流打印 — BGE(8002) 挂掉时 gate 全部静默
# (embedding_unavailable 不留任何日志, 14h 才被发现)。60s 一条, 不洪泛。
_EPG_MISS_AT = [0.0]


# 2026-09-03 P5-fix: 推理题预检 — 让明显"查不到/只能推理"的题型直接走 full_pipeline,
# 不再被 stepfun 当"问事实"扔给 recall_memory. genbench T1 实测: 80% T1 题被劫持
# (route=tool_call → recall_memory → 历史对话没答案 → 0 分); 解掉劫持后 T1 单题
# 走神经基底能拿 0.5 分 (已验证 seed=37048). 规则: stdlib regex, 零网络.
_REASONING_QUERY_PATTERNS = (
    r"家谱[:：].*\?问?[:：]?",                  # genbench T1: 亲属链
    r"(之|之\?)\s*(什么|谁|哪个)",          # X 是什么关系
    r"五(行|行相生|行相克)",                  # 五行岛
    r"天干|地支|甲子|乙丑|丙寅|丁卯|戊辰|己巳|庚午|辛未|壬申|癸酉",  # 完整干支词
    r"(相生|相克)\s*的?(关系|顺序|下一个|上一个)",
    r"配偶|夫妻|孩子|父亲|母亲|祖父|祖母|外公|外婆|爷爷|奶奶",
    r"父(亲|亲是)|母(亲|亲是)|兄(弟|弟是)|姐(姐|姐是)|子女|父子|母子",
    r"\b\d+\s*步后\s*(是|为|得|变)",            # N 步后
    r"(推理|推算|推导|算一下|算算|心算)",       # 自带推理标签
    # 2026-09-17: 单字干支 ([甲乙丙丁戊己庚辛壬癸]) 会误伤日常中文 ("自己"的"己"
    # 被撞中 → driver 每个决策都被判推理题 → 绕过 P5 快路 → full_pipeline 2900s+)。
    # 守门员只收完整干支词, 单字不入题。
    # 2026-09-10: generic syllogism / conditional-inference shapes (T1-adjacent
    # logic riddles). P5 text passthrough answers these shallowly.
    r"如果.{2,30}(那么|则|所以)",               # 如果P…(那么/则)Q
    r"所有.{1,20}(都|均).{1,30}(吗|么|\?)",     # 所有A都B…吗（全称命题判定）
    r"(怕不怕|会不会|是不是)",                   # 属性判定问句
    r"(三段论|演绎|前提|结论)[:：，, ]?",        # 显式逻辑词
)


def _is_reasoning_query(query: str) -> bool:
    """Stdlib regex gate. True → skip P5, go full_pipeline."""
    if not query:
        return False
    q = query.strip()
    if len(q) < 4:
        return False
    for pat in _REASONING_QUERY_PATTERNS:
        if re.search(pat, q):
            return True
    return False

_PROMPT_PATH = Path(__file__).resolve().parent / "ego_prompts" / "tool_calling_system.txt"
_MAX_TOOL_CALLS = 3
_TIMEOUT = 60


def _load_tools_schema() -> list:
    """Load OpenAI tool schemas from the unified registry."""
    try:
        from tool_router import get_router
        router = get_router()
        return router.list_available_tools()
    except Exception:
        return []


def _load_system_prompt(tools_schema: list) -> str:
    try:
        template = _PROMPT_PATH.read_text(encoding="utf-8")
        return template.replace("{tools_schema}", json.dumps(tools_schema, ensure_ascii=False))
    except Exception:
        return ("You are TL-Ego. If the user request can be completed with a tool, "
                "output OpenAI tool_calls. Tools: " + json.dumps(tools_schema, ensure_ascii=False))


def _llm_headers() -> dict:
    key = os.environ.get("EGO_LLM_API_KEY", "") or os.environ.get("STEPFUN_API_KEY", "")
    if not key:
        # Same fallback as tl_ego_server._load_llm_api_key: ego_key.txt next
        # to the project (L5 restarts bypass the env-setting bat).
        try:
            _kf = Path(__file__).resolve().parent / "ego_key.txt"
            if _kf.exists():
                key = _kf.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0].strip()
        except Exception:
            pass
    if key:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    # When running inside tl_ego_server, reuse its already-loaded headers.
    # Use sys.modules (NOT a fresh import — a fresh import in a separate
    # process would re-run the single-instance check and sys.exit(0)).
    try:
        if "tl_ego_server" in sys.modules:
            _srv = sys.modules["tl_ego_server"]
            return _srv._llm_headers()
    except Exception:
        pass
    return {"Content-Type": "application/json"}


def _llm_endpoint() -> tuple:
    url = os.environ.get("EGO_LLM_URL", "https://api.stepfun.com/step_plan/v1/chat/completions")
    model = os.environ.get("EGO_LLM_MODEL", "step-3.5-flash")
    return url, model


def _call_llm_with_tools(user_text: str, tools: list, tool_results: Optional[list] = None, history: Optional[list] = None, extra_system_note: Optional[str] = None, epi_context: Optional[str] = None) -> dict:
    """Call stepfun with tools=[...]; returns raw choices[0] message dict.

    history: optional list of prior messages [{"role", "content"}] to give
        the LLM multi-turn context (added between system prompt and the
        current user_text).
    extra_system_note: optional extra system message appended after the
        main system prompt (e.g. bounded self-model facts like continuity).
    """
    url, model = _llm_endpoint()
    system_prompt = _load_system_prompt(tools)

    messages = [{"role": "system", "content": system_prompt}]
    if extra_system_note:
        messages.append({"role": "system", "content": extra_system_note})
    if tool_results:
        # Second round: give results back so LLM can compose final reply.
        messages.append({"role": "assistant", "content": None,
                         "tool_calls": tool_results[0]["raw_tool_calls"]})
        for tr in tool_results:
            messages.append({"role": "tool", "tool_call_id": tr["tool_call_id"],
                             "content": json.dumps(tr["result"], ensure_ascii=False)})
        # Re-anchor the model on the USER'S actual question. Without this it
        # just paraphrases the tool result (e.g. "你在干嘛呢？" → check_status
        # → answers with neuron/edge counts, not what it's doing). Verified
        # live 2026-08-21.
        # 2026-09-06: recall_memory results must dominate the answer —
        # model previously ignored them (answered "没有记录" while matches
        # sat in the tool message) or paraphrased check_status instead.
        _recall = next((tr for tr in tool_results if tr["tool"] == "recall_memory" and tr.get("status") == "ok"), None)
        if epi_context:
            # 2026-09-08: episodic context rides along with whatever route won.
            messages.append({"role": "user",
                             "content": f"请针对用户的原始问题「{user_text}」给出最终回答。\n{epi_context}\n"
                                        f"以上是你自己的历史动作记录。若与问题相关请优先引用；"
                                        f"若确实无关，忽略它正常回答，不要虚构经历。"})
        elif _recall:
            _ctx = str((_recall.get("result") or {}).get("context_text", ""))[:1500]
            messages.append({"role": "user",
                             "content": f"请针对用户的原始问题「{user_text}」给出最终回答。"
                                        f"以下是已检索到的记忆，与问题直接相关，必须优先依据它们回答；"
                                        f"仅当记忆确实为空或不相关时才说没有记录：\n{_ctx}"})
        else:
            messages.append({"role": "user",
                             "content": f"请针对用户的原始问题「{user_text}」给出最终回答。"
                                        f"工具结果是参考材料：回答必须贴合问题本身，"
                                        f"不要罗列工具原始参数或 JSON；"
                                        f"如果工具结果与问题无关，忽略它并正常回答。"})
    else:
        # Multi-turn: insert prior history turns before the current user_text
        if history:
            for turn in history:
                if isinstance(turn, dict) and turn.get("role") in ("user", "assistant", "system", "tool"):
                    msg = {"role": turn["role"], "content": turn.get("content", "")}
                    # Pass through tool_calls if present (multi-turn tool flow)
                    if "tool_calls" in turn:
                        msg["tool_calls"] = turn["tool_calls"]
                    messages.append(msg)
        messages.append({"role": "user", "content": user_text})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    if not tool_results and tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    # Second round (tool_results present): DO NOT pass tools at all.
    # Verified live 2026-08-21: with tools present, step-3.5-flash returns
    # content="" and MORE tool_calls even under tool_choice="none" (it kept
    # calling l7_workspace_scan per top_dir), so the composed reply was empty
    # and the ugly JSON-truncation fallback fired. Without tools the model
    # has no choice but to synthesize text from the tool results.
    r = llm_post_json(url, headers=_llm_headers(), json=payload, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]


def _parse_tool_calls(msg: dict) -> list:
    """Normalize OpenAI tool_calls into [{id, name, arguments(dict)}]."""
    raw = msg.get("tool_calls") or []
    out = []
    for tc in raw:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        args_raw = fn.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except Exception:
            args = {}
        out.append({"id": tc.get("id", f"call_{name}"), "name": name,
                    "arguments": args, "raw_tool_calls": raw})
    return out[: _MAX_TOOL_CALLS]


def _summarize_tool_result(tool: str, result) -> str:
    """Human-readable summary of a tool result (fallback path only).

    Previously the fallback dumped raw JSON truncated to 200 chars, which
    produced garbage like '...\"capability_suites' glued to the next tool's
    output. Per-tool extractors keep the fallback readable.
    """
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)[:300]
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if tool in ("l7_file_read", "file_read"):
        content = str(data.get("content", ""))
        lines_total = data.get("lines", "?")
        truncated = "（已截断）" if "[truncated" in content or data.get("truncated") else ""
        preview = content[:1200]
        return f"读取 {data.get('filepath', '?')}: {lines_total} 行, {data.get('size', '?')} 字符{truncated}\n{preview}"
    if tool in ("l7_workspace_scan", "workspace_scan"):
        dirs = data.get("dirs") or 0
        files = data.get("files_deep")
        top = (data.get("top_dirs") or [])[:15]
        s = f"扫描到 {dirs} 个目录"
        if files is not None:
            s += f", {files} 个文件"
        if top:
            s += "，顶层目录: " + ", ".join(str(x) for x in top)
        return s
    if tool == "recall_memory":
        matches = result.get("matches") or []
        s = f"检索到 {len(matches)} 条记忆"
        for m in matches[:5]:
            content = str(m.get("content", ""))[:120]
            s += f"\n  - {content}"
        if len(matches) > 5:
            s += f"\n  ... 等 {len(matches)} 条"
        return s
    if tool == "check_status":
        keys = ("status", "neurons", "full_edges", "tick", "dominant")
        kv = ", ".join(f"{k}={result.get(k)}" for k in keys if k in result)
        return kv or json.dumps(result, ensure_ascii=False)[:300]
    raw = json.dumps(result, ensure_ascii=False)
    return raw[:600] + ("…[TRUNCATED]" if len(raw) > 600 else "")


def _normalize_decision_json_text(text: str) -> str:
    """决策轮文本兜底 (v4): stepfun 的两种"类工具调用"文本 → MC 桥格式 {"tool":X,"args":Y}.
    (a) legacy XML: <tool_call> <function=goto> <parameter=x>499</parameter> ... </tool_call>
    (b) 原生 JSON 封装: {"tool_calls":[{"name":"goto","arguments":{...}}]}
    解析失败原样返回, 不伤原有格式。"""
    if "<function" in text:
        m = re.search(r"<function\s*=\s*([A-Za-z_][\w-]*)", text)
        if m:
            args = {}
            for pm in re.finditer(r"<parameter\s*=\s*([A-Za-z_][\w-]*)\s*>([^<]*)", text):
                raw = pm.group(2).strip()
                if not raw:
                    continue
                try:
                    args[pm.group(1)] = json.loads(raw)
                except Exception:
                    args[pm.group(1)] = raw
            return json.dumps({"tool": m.group(1), "args": args}, ensure_ascii=False)
        return text
    i = text.find("{")
    if i < 0:
        return text
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[i:])
    except Exception:
        return text
    if isinstance(obj, dict) and isinstance(obj.get("tool_calls"), list) and obj["tool_calls"]:
        c = obj["tool_calls"][0]
        name = c.get("name") or c.get("tool")
        args = c.get("arguments") if c.get("arguments") is not None else c.get("args", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if name:
            return json.dumps({"tool": name, "args": args if isinstance(args, dict) else {}},
                              ensure_ascii=False)
    return text


def try_llm_tool_call(query: str, source: str = "internal_l10", messages: Optional[list] = None, extra_system_note: Optional[str] = None, decision_id: Optional[str] = None) -> Optional[dict]:
    """P5 entry point: ask the LLM whether/which tool to call.

    Args:
        query: user text (current turn)
        source: attribution tag for ToolRouter — "internal_l10" for ego's own
            contemplate pre-step, "external_openai" for the 8770 shim.
        messages: optional prior conversation turns
            [{"role": "user|assistant", "content": "..."}]. When provided,
            they are prepended to the LLM prompt so it has multi-turn
            short-term context (fixes the "I don't remember your last
            question" failure mode — verified 2026-08-21).
        extra_system_note: optional bounded self-model facts (e.g. continuity
            resume info) injected as an extra system message so the LLM can
            reference "what I was doing before" when answering.
        decision_id: accepted for caller compatibility (tl_ego_server passes its
            job id so a later measured /feedback can close the teacher sample);
            the server registers the sample under that id itself.

    Returns:
      {"response": str, "tool_executions": [...], "llm_route": "tool_call",
       "openai_tool_calls": [...]} if the LLM decided to call tools (already
      executed via ToolRouter), or None if the LLM produced no tool_calls /
      any failure (caller falls back).

    Env control:
      EGO_AGENT_LOOP=1   use AgentLoop (P5 multi-turn); default off to keep
                         /contemplate latency unchanged. The shim always uses
                         AgentLoop (see ego_openai_shim.py).
    """
    if os.environ.get("EGO_TOOL_CALL", "1") == "0":
        return None
    if not query or not query.strip():
        return None
    # 2026-09-03 P5-fix: 推理题预检 — 跳 P5 让神经基底/LLM 直接推理
    # 2026-09-10 fix: this block was indented under the `return None` above
    # (dead code since commit 158c2d7c) — reasoning queries were being forced
    # through the P5 pre-check, whose plain-text passthrough (llm_route="text")
    # answered them shallowly instead of deep full_pipeline.
    if _is_reasoning_query(query):
        print(f"[p5-skip-reasoning] query matched reasoning pattern, "
              f"bypass P5 → full_pipeline: {query[:60]!r}", flush=True)
        return None

    # P5 multi-turn: AgentLoop. when EGO_AGENT_LOOP=1
    if os.environ.get("EGO_AGENT_LOOP", "0") == "1":
        try:
            from agent_loop import AgentLoop
            loop = AgentLoop()
            _p5 = loop.run(query, source=source, history=messages)
            return {
                "response": _p5.get("response", ""),
                "tool_executions": _p5.get("tool_executions", []),
                "llm_route": _p5.get("llm_route"),
                "openai_tool_calls": [],  # not preserved by AgentLoop
            }
        except Exception as e:
            print(f"[p5-tool-call] AgentLoop failed, falling back: {e}", flush=True)
            # fall through to legacy single-round path

    tools = _load_tools_schema()
    if not tools:
        return None

    # 2026-09-12 fix: P5 前置层禁吃"动作工具"。MC 桥把决策请求(以 "Ego:(x,y,z)" 开头)
    # 发进 /contemplate, P5 给 stepfun 的 schema 里有 move/setblock/chat 等动作工具 →
    # LLM 常把决策 prompt 里的"Ego:(x,y,z)"当前位置误当目标, 内调 move 到原点并返回
    # "已通过工具完成：move"文字报告(单点实测占 71% 轮次) → 桥丢弃 → ego 大部分时间静止。
    # 动作是桥按决策 JSON 落地的; P5 前置只保留感知/记忆类工具。
    _decision_prompt = query.lstrip().startswith("Ego:(")
    if _decision_prompt:
        # 2026-09-12 v3: 决策 prompt 下一律不提供任何 router 工具。黑名单追不全(v1 滤 4 个
        # 出现 set_goal 洪水; v2 滤 12 个冒出 nod/l7_exec_command)。决策动作由桥按 JSON 落地。
        # 2026-09-13 v4: 不给工具 ≠ 直接 return None。v3 的 return None 把每个 MC 决策轮都
        # 逼进 full_pipeline (实测正常 17-40s, 但 ~20% 抽风到 600-920s, 极端个例卡死 >1h)
        # → 桥 180s 超时放弃 → 丢决策/跑陈旧决策。现在照样问 LLM(不带工具), 收文本决策 JSON
        # 直接返回 (实测同类文本答案 2.7s); 任何失败仍回落 full_pipeline (调用方 except 兜底)。
        tools = []
        print("[p5-tool-call] decision prompt detected — no tools offered (v4), "
              "asking LLM for decision JSON text", flush=True)
        # 2026-10-14 决策轮记忆注入 (重建): 每轮把「已沉淀规则 + 上次怎么栽的」塞进
        # system note。纯 SQL/文件读, 零 throw; 失败只是少注入, 不影响决策。
        if not extra_system_note:
            try:
                from neural_direct import recent_failures_note, standing_lessons_note
                _notes = [n for n in (standing_lessons_note(), recent_failures_note()) if n]
                if _notes:
                    extra_system_note = "\n".join(_notes)
                    print("[decision memory note] %d chars" % len(extra_system_note), flush=True)
            except Exception as _dmn:
                print("[decision memory note] skipped: %s" % type(_dmn).__name__, flush=True)

    # 2026-08-31 fix(H): /contemplate 答题路径自动记忆召回 — 治"记得但想不起"。
    # fix(G) 只接了 8770 聊天路径 (AgentLoop, EGO_AGENT_LOOP 默认关)。本函数是
    # /contemplate 的默认 tool_call 答题路径, 此前零注入: server:3314 的
    # [Past Experience] 只喂 llm_classify 分类器 (只产类型 JSON 不产答案)。
    # 实测: 洗车纠正记录在 memu.db 且检索应排第 1, 但 recalls=0 → 从未注入。
    # 现复用 agent_loop._memory_recall_note (同源注入), 一轮/二轮都带上。
    if not extra_system_note and not _decision_prompt:
        try:
            from agent_loop import _memory_recall_note
            extra_system_note = _memory_recall_note(query)
        except Exception as _e:
            print(f"[p5-tool-call] fix(H) memory note unavailable: {_e}", flush=True)
            extra_system_note = None

    try:
        msg = _call_llm_with_tools(query, tools, history=messages, extra_system_note=extra_system_note)
        tool_calls = _parse_tool_calls(msg)
        if not tool_calls:
            # LLM answered with plain text — no tool needed. Return the text
            # so the caller can reply directly (instead of falling back to a
            # generic "ego received" echo). This was the root cause of the
            # garbage "ego received: ..." replies in Hermes.
            text = (msg.get("content") or "").strip()
            if text:
                if _decision_prompt:
                    # v4: 决策轮先试规范化 (XML/tool_calls 封装 → {"tool":X,"args":Y});
                    # 成功即直接返回, 不再被 XML 护栏踢回 15 分钟级管线。
                    _norm = _normalize_decision_json_text(text)
                    if _norm.startswith('{"tool"') or _norm.startswith('{"action"'):
                        return {"response": _norm, "tool_executions": [],
                                "llm_route": "text", "openai_tool_calls": []}
                if re.search(r"<tool_call>|<function\s*=|</function>", text, re.IGNORECASE):
                    print("[p5-tool-call] LLM used XML tool text; falling back to pipeline", flush=True)
                    return None
                return {"response": text, "tool_executions": [],
                        "llm_route": "text", "openai_tool_calls": []}
            return None

        from tool_router import get_router
        # 2026-09-08 episodic gate: surface Ego's OWN action history
        # (tool_executions.db) so recall-style questions can hit real events.
        # Injected as evidence in the compose round — does not force any tool.
        _epi_ctx = None
        try:
            from neural_direct import episodic_gate
            _epg = episodic_gate(query)
            if _epg.get("fired") and _epg.get("context"):
                _epi_ctx = ("[Past Experience — Ego 的真实动作记录(tool_executions.db), "
                            "回答时可引用] \n" + _epg["context"])
                print(f"[episodic-gate] P5 FIRED hits={_epg.get('hits')} sim={_epg.get('top_sim')} took={_epg.get('took_ms')}ms", flush=True)
            elif _epg.get("reason"):
                _now = time.time()
                if _now - _EPG_MISS_AT[0] >= 60:
                    _EPG_MISS_AT[0] = _now
                    print("[episodic-gate] not fired: %s took=%sms"
                          % (_epg.get("reason"), _epg.get("took_ms")), flush=True)
        except Exception as _epe:
            print(f"[episodic-gate] pre-step skipped: {type(_epe).__name__}", flush=True)
        router = get_router()
        executions = []
        openai_tool_calls = []
        for tc in tool_calls:
            outcome = router.execute(tc["name"], tc["arguments"], source=source)
            executions.append({
                "tool": tc["name"],
                "arguments": tc["arguments"],
                "status": outcome.get("status"),
                "result": outcome.get("result"),
                "error": outcome.get("error"),
                "memory_id": outcome.get("memory_id"),
                "tool_call_id": tc["id"],
                "raw_tool_calls": tc["raw_tool_calls"],
            })
            # Keep the raw OpenAI-shaped tool_call for shim passthrough
            for raw in tc["raw_tool_calls"]:
                openai_tool_calls.append(raw)

        # Second round: give results back to LLM for the final composed reply
        try:
            # fix(H) 续: 二轮组答案也带记忆注入 — 这轮才真正决定最终答案内容
            final_msg = _call_llm_with_tools(query, tools, tool_results=executions,
                                             extra_system_note=extra_system_note,
                                             epi_context=_epi_ctx)
            final_text = (final_msg.get("content") or "").strip()
            # Same XML-tool-text guard as the first round: step-3.5-flash
            # sometimes answers the second round with `<tool_call>...` XML
            # instead of natural language (observed live 2026-08-21 for
            # "你现在在 Minecraft 里的位置": content was literal
            # `<tool_call> <function=recall_memory> ...`). Never let that
            # reach the user → fall back to the readable summary.
            if re.search(r"<tool_call>|<function\s*=|</function>", final_text, re.IGNORECASE):
                print("[p5-tool-call] second-round XML tool text; using summary fallback", flush=True)
                final_text = ""
        except Exception as e:
            print(f"[p5-tool-call] second-round compose failed: {type(e).__name__}: {e}", flush=True)
            final_text = ""

        if not final_text:
            parts = [f"已通过工具完成：{', '.join(e['tool'] for e in executions)}。"]
            for e in executions:
                if e["status"] == "ok":
                    parts.append(f"[{e['tool']}] {_summarize_tool_result(e['tool'], e['result'])}")
                else:
                    parts.append(f"[{e['tool']}] 失败: {e['error']}")
            final_text = "\n".join(parts)

        clean_execs = [{k: v for k, v in e.items() if k != "raw_tool_calls"} for e in executions]
        return {"response": final_text, "tool_executions": clean_execs,
                "llm_route": "tool_call", "openai_tool_calls": openai_tool_calls}
    except Exception as e:
        print(f"[p5-tool-call] fallback (no tool route): {type(e).__name__}: {e}", flush=True)
        return None


if __name__ == "__main__":
    # Quick self-check against live stepfun (requires env creds from server)
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "build stone at 10,64,-5"
    result = try_llm_tool_call(q)
    if result is None:
        print("NO_TOOL_ROUTE (LLM chose text or failure) — caller would fall back to full_pipeline")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2)[:2000])
