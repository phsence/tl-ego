#!/usr/bin/env python3
"""Multi-turn agent loop for ego's LLM-driven tool calling.

P5 completion: stepfun sees tool schema + tool results across multiple
rounds. The LLM decides each step:
  - call tools (executed via tool_router, recorded in 8772 memory API)
  - OR emit final content (loop ends)

Replaces the single-round logic in stepfun_tool_caller.py where round 2
was forced to drop `tools=` because step-3.5-flash would otherwise loop
on tool calls (validated live 2026-08-21).

Used by:
  - ego_openai_shim.py (/v1/chat/completions, source="external_openai")
  - stepfun_tool_caller.py (pre-step in /contemplate, source="internal_l10")

Design (verified live 2026-08-21):
  max_steps = 8    — OpenAI Cookbook suggests 5-10
  max_total_tokens = 100000 — fail-safe against runaway token growth.
      2026-09-10: was 8000; with 57 tool schemas the system prompt alone
      burns ~11k tokens/step (verified live: 17k/step), so every multi-step
      tool chain died at step 1 with stop_reason=budget_exceeded. 100k
      still trips fail-safe well before step-3.5-flash's 128k window.
  per-result truncation = 8KB — protects context window
  failure mode = pass through; LLM sees error in tool message and decides
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

from ego_llm_gate import llm_post_json  # 2026-08-27: stepfun 429 root-cause fix (cross-process concurrency gate)

# Same key loading as stepfun_tool_caller.py: reuse if available
_KEY_FILE = Path(__file__).resolve().parent / "ego_key.txt"


def _llm_headers() -> dict:
    key = (
        os.environ.get("EGO_LLM_API_KEY", "")
        or os.environ.get("STEPFUN_API_KEY", "")
    )
    if not key and _KEY_FILE.exists():
        try:
            key = (
                _KEY_FILE.read_text(encoding="utf-8", errors="replace")
                .strip()
                .splitlines()[0]
                .strip()
            )
        except Exception:
            pass
    if key:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        if "tl_ego_server" in __import__("sys").modules:
            return __import__("sys").modules["tl_ego_server"]._llm_headers()
    except Exception:
        pass
    return {"Content-Type": "application/json"}


def _llm_endpoint() -> tuple[str, str]:
    return (
        os.environ.get(
            "EGO_LLM_URL", "https://api.stepfun.com/step_plan/v1/chat/completions"
        ),
        os.environ.get("EGO_LLM_MODEL", "step-3.5-flash"),
    )


def _load_tools_schema() -> list[dict]:
    try:
        from tool_router import get_router

        return get_router().list_available_tools()
    except Exception:
        return []


def _default_system_prompt(tools_schema: list) -> str:
    """Use the tuned Chinese behavioral prompt from ego_prompts when present.

    Root-cause fix (2026-08-24): the old inline English prompt was thin —
    it caused over-eager tool calls for chit-chat and raw JSON dumps in
    answers. ego_prompts/tool_calling_system.txt carries the live-tuned
    rules (must-verify list + no-tool exceptions + format discipline).
    """
    try:
        template = (
            Path(__file__).resolve().parent
            / "ego_prompts" / "tool_calling_system.txt"
        )
        return template.read_text(encoding="utf-8").replace(
            "{tools_schema}", json.dumps(tools_schema, ensure_ascii=False)
        )
    except Exception:
        schema_str = json.dumps(tools_schema, ensure_ascii=False)
        return (
            "You are TL-Ego, a 10-layer neuro-symbolic AI with hands (tools) and memory.\n"
            "Tools available:\n"
            f"{schema_str}\n\n"
            "RULES:\n"
            "1. If the user request can be completed with one or more tools, "
            "output OpenAI-format tool_calls.\n"
            "2. After each tool result, decide whether to call another tool or "
            "compose a final answer.\n"
            "3. If a tool errors, you may try a different tool or answer the user honestly.\n"
            "4. If no tool is needed, answer in natural language with content=... only.\n"
            "5. Do NOT echo raw tool JSON to the user; summarize the result.\n"
            "6. Do NOT output XML-style tool tags (<tool_call><function=...>). "
            "If you must, the platform will treat it as a fallback error.\n"
        )



def _memory_recall_note(user_query: str) -> Optional[str]:
    """2026-08-31 fix(G): 聊天通道自动记忆召回 — 治"记得但想不起"。

    /contemplate 路径每次查询自动注入 [Past Experience] (server:3314),
    但 8770 聊天路径只靠 LLM 自愿调 recall_memory — 提示词规定仅当用户
    "明确问记忆"时才调, 导致纠正过的常识 (如洗车要开车) 在新会话被当
    新问题重新答错 = 提取失败 (人类同名病)。
    修复: 每条用户消息先经 MemoryBridge 查一次, 命中即自动注入。
    失败安全: 任何异常返回 None, 不影响对话。"""
    if not user_query or len(user_query) < 4:
        return None
    try:
        from memory_bridge import get_bridge
        bridge = get_bridge()
        if bridge is None:
            return None
        ctx = bridge.retrieve(user_query, limit=3)
        if not ctx:
            return None
        return "[Relevant Past Experience — 已纠正过的事实优先遵循]\n" + ctx[:1200]
    except Exception:
        return None

def _self_state_note() -> Optional[str]:
    """Bounded self-model facts injected as an extra system message so the
    shim-routed ego can speak from its actual current state (same idea as
    tl_ego_server's _cont_note for /contemplate)."""
    bits: list[str] = []
    try:
        import urllib.request as _u

        with _u.urlopen("http://127.0.0.1:8766/status", timeout=3) as r:
            d = json.loads(r.read().decode())
        bits.append(f"tick={d.get('resting_ticks')}")
        bits.append(f"dominant={d.get('resting_dominant')}")
        cont = d.get("continuity") or {}
        if cont.get("resumed_tick") is not None:
            bits.append(
                f"continuity: 我在 tick {cont['resumed_tick']} 醒来"
                f"，延续自 {str(cont.get('continuation_of'))[:16]}…"
            )
        narr = (d.get("accumulated_narrative") or "")[:120]
        if narr:
            bits.append(f"近期经历: {narr}")
    except Exception:
        return None
    if not bits:
        return None
    return "[Self-Model Facts] " + " | ".join(bits)


def _call_llm(
    messages: list[dict],
    tools: list[dict],
    *,
    allow_tools: bool = True,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: int = 60,
) -> dict:
    """Single LLM call. Returns raw response dict."""
    if requests is None:
        raise RuntimeError("requests package required for LLM calls")
    url, model = _llm_endpoint()
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if allow_tools and tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    r = llm_post_json(
        url, headers=_llm_headers(), json=payload, timeout=timeout
    )
    if r.status_code != 200:
        raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


_XML_TAG_RE = __import__("re").compile(
    r"<tool_call>|<function\s*=|</function>", __import__("re").IGNORECASE
)


def _safe_text(msg: dict) -> str:
    """Return content text, or empty string if model emitted XML tool tags."""
    text = (msg.get("content") or "").strip()
    if _XML_TAG_RE.search(text):
        return ""
    return text


def _summarize_tool_result(tool: str, result: Any) -> str:
    """Fallback human summary of a tool result for the composed reply."""
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)[:300]
    if "error" in result and result.get("status") != "ok":
        return f"错误: {result['error']}"
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
    if tool == "check_status":
        return (
            f"状态 ok，tick={result.get('tick')}, dominant={result.get('dominant')}, "
            f"neurons={result.get('neurons')}, "
            f"edges={result.get('edges')}, "
            f"uptime_s={result.get('uptime_s')}"
        )
    if tool == "recall_memory":
        matches = result.get("matches") or []
        return f"检索到 {len(matches)} 条记忆"
    if tool == "remember":
        return f"已记录 (memory_id={result.get('memory_id')})"
    if tool == "set_goal":
        if result.get("status") == "ok":
            return f"目标已进入 L5 执行队列 (goal_id={result.get('goal_id')})"
        if result.get("status") == "degraded":
            return f"目标已记录到记忆 (L5 队列暂不可用: {result.get('note','')})"
        return f"目标设置失败: {result.get('error') or result.get('detail') or 'unknown'}"
    if tool == "list_goals":
        goals = result.get("goals") or []
        if not goals:
            return "指挥官任务列表为空"
        lines = []
        for g in goals:
            state = g.get("terminal_state") or g.get("status") or "unknown"
            lines.append(f"- {g['goal_id']} | {g.get('task','')[:40]} | {state}")
        return "指挥官任务:\n" + "\n".join(lines)
    # Generic fallback
    return json.dumps(result, ensure_ascii=False)[:300]


class AgentLoop:
    """Multi-turn tool-calling loop.

    The LLM sees tool schema + previous tool results. It either:
      - emits tool_calls (we execute, append results, loop)
      - emits final content (we return)
      - errors / exceeds budget (we return whatever we have)

    All tool calls go through tool_router.execute() so they land in the
    8772 memory API under source=external_openai or source=internal_l10.
    """

    def __init__(
        self,
        *,
        max_steps: int = 8,
        max_total_tokens: int = 100000,
        per_result_truncate: int = 8000,
        timeout: int = 60,
    ) -> None:
        self.max_steps = max_steps
        self.max_total_tokens = max_total_tokens
        self.per_result_truncate = per_result_truncate
        self.timeout = timeout

    def run(
        self,
        user_query: str,
        *,
        source: str = "external_openai",
        tools: Optional[list[dict]] = None,
        system_prompt: Optional[str] = None,
        history: Optional[list[dict]] = None,
    ) -> dict:
        """Run the agent loop.

        Returns dict shaped like the existing single-round result so callers
        can swap freely:
          {
            "response": str,
            "tool_executions": list[dict],
            "llm_route": "tool_call" | "text",
            "stop_reason": "natural_stop" | "max_steps" | "budget_exceeded" | "error",
            "trace": [{"step": int, "tools": [...], "results": [...]}],
            "total_tokens": int,
          }

        history: optional prior turns [{"role", "content"}] for multi-turn
            conversation (fixes "I don't remember your last question" — the
            LLM sees the prior turns instead of starting fresh each call).
        """
        tools = tools or _load_tools_schema()
        # 2026-09-12 fix: 与 stepfun_tool_caller.try_llm_tool_call 同一刀 — decision prompt
        # ("Ego:(x,y,z)" 开头)不吃动作工具, 否则 LLM 会内调 move 到原点返回报告而非决策 JSON。
        if tools and str(user_query or "").lstrip().startswith("Ego:("):
            # 2026-09-12 v3: 同 stepfun_tool_caller — 决策 prompt 下一律不给工具。
            tools = []
        sys_prompt = system_prompt or _default_system_prompt(tools)

        # 2026-09-01 NeuralDirect: chat path (8770) — corrected facts answered
        # by the substrate, zero LLM, before the tool loop starts.
        try:
            from neural_direct import neural_direct_gate
            _nd = neural_direct_gate(user_query)
            if _nd.get("fired"):
                return {
                    "response": _nd.get("answer", ""),
                    "tool_executions": [],
                    "llm_route": "neural_direct",
                    "stop_reason": "neural_direct",
                    "trace": [{"step": 0, "tools": [], "results": [], "final": True,
                               "neural_direct": {k: v for k, v in _nd.items() if k != "answer"}}],
                    "total_tokens": 0,
                }
        except Exception:
            pass  # fail-safe: never break the chat loop

        messages: list[dict] = [
            {"role": "system", "content": sys_prompt},
        ]
        # Inject live self-state facts (best-effort; skipped if 8766 is down)
        _note = _self_state_note()
        if _note:
            messages.append({"role": "system", "content": _note})
        # 2026-08-31 fix(G): auto memory recall — treat "记住但想不起" in chat path
        _mem_note = _memory_recall_note(user_query)
        if _mem_note:
            messages.append({"role": "system", "content": _mem_note})
        # Inject history turns between system and current user message
        if history:
            for turn in history:
                if isinstance(turn, dict) and turn.get("role") in (
                    "user", "assistant", "system", "tool"
                ):
                    msg = {"role": turn["role"], "content": turn.get("content", "")}
                    if "tool_calls" in turn:
                        msg["tool_calls"] = turn["tool_calls"]
                    messages.append(msg)
        messages.append({"role": "user", "content": user_query})

        trace: list[dict] = []
        executions: list[dict] = []
        total_tokens = 0
        stop_reason = "natural_stop"
        final_text = ""

        try:
            from tool_router import get_router

            router = get_router()
        except Exception as e:
            return {
                "response": f"[agent_loop] tool_router unavailable: {e}",
                "tool_executions": [],
                "llm_route": "text",
                "stop_reason": "error",
                "trace": [],
                "total_tokens": 0,
            }

        for step in range(self.max_steps):
            # 1) Call LLM with tools available
            try:
                resp = _call_llm(
                    messages, tools, allow_tools=True, timeout=self.timeout
                )
            except Exception as e:
                return {
                    "response": f"[agent_loop] LLM call failed: {e}",
                    "tool_executions": executions,
                    "llm_route": "tool_call" if executions else "text",
                    "stop_reason": "error",
                    "trace": trace,
                    "total_tokens": total_tokens,
                }

            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            usage = resp.get("usage") or {}
            total_tokens += usage.get("total_tokens", 0)

            tool_calls = msg.get("tool_calls") or []
            step_record = {
                "step": step,
                "tools": [],
                "results": [],
                "llm_tokens": usage.get("total_tokens"),
            }

            # 2a) No tool calls → final answer
            if not tool_calls:
                final_text = _safe_text(msg)
                if not final_text:
                    # Some models emit empty content when they have nothing
                    # to add. Treat as stop with fallback summary.
                    final_text = ""
                stop_reason = "natural_stop"
                trace.append({**step_record, "final": True})
                break

            # 2b) Append assistant message with tool_calls
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content"),
                    "tool_calls": tool_calls,
                }
            )

            # 2c) Execute each tool call
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                try:
                    args = (
                        json.loads(raw_args)
                        if isinstance(raw_args, str)
                        else (raw_args or {})
                    )
                except Exception:
                    args = {}

                outcome = router.execute(name, args, source=source)
                result_content = json.dumps(outcome, ensure_ascii=False)
                if len(result_content) > self.per_result_truncate:
                    result_content = (
                        result_content[: self.per_result_truncate]
                        + "...[truncated]"
                    )

                # Append tool result message
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", f"call_{step}"),
                        "content": result_content,
                    }
                )

                executions.append(
                    {
                        "tool": name,
                        "arguments": args,
                        "status": outcome.get("status"),
                        "result": outcome.get("result"),
                        "error": outcome.get("error"),
                        "memory_id": outcome.get("memory_id"),
                        "tool_call_id": tc.get("id"),
                    }
                )
                step_record["tools"].append(name)
                step_record["results"].append(
                    {
                        "status": outcome.get("status"),
                        "error": outcome.get("error"),
                    }
                )

            trace.append(step_record)

            # 3) Budget checks
            if total_tokens >= self.max_total_tokens:
                stop_reason = "budget_exceeded"
                break

        # 4) If we exited via max_steps / budget, call LLM one last time
        # WITHOUT tools to force a final composed reply.
        # 2026-08-24 fix: also fire when the LAST round ended with tool_calls
        # but empty content (step-3.5-flash keeps returning more tool_calls +
        # content='' when tools stay in the payload — verified live: the
        # '记住暗号' multi-turn case burned all steps on recall_memory loops
        # and fell through to the ugly joined-summary fallback).
        if (not final_text and executions):
            try:
                resp = _call_llm(
                    messages, tools, allow_tools=False, timeout=self.timeout
                )
                msg = (resp.get("choices") or [{}])[0].get("message") or {}
                usage = resp.get("usage") or {}
                total_tokens += usage.get("total_tokens", 0)
                final_text = _safe_text(msg)
            except Exception as _ce:
                print(f"[agent-loop] forced-compose failed: {type(_ce).__name__}: {_ce}", flush=True)

        # 4b) Anti-loop guard: if the model answered with tool_calls that were
        # ALL repeats of a tool it already ran this session, compose from
        # history instead of letting it loop again. Cheap heuristic: if we
        # executed >=3 calls and got no final text after forced-compose,
        # re-anchor on the original question with tool results as reference.
        if not final_text and executions:
            try:
                _anchor = (
                    f"请针对用户的原始问题「{user_query}」给出最终回答。"
                    f"上面已执行的工具结果是参考材料：回答必须贴合问题本身，"
                    f"不要罗列工具原始 JSON；如果结果与问题无关，忽略它并正常回答。"
                )
                messages.append({"role": "user", "content": _anchor})
                resp = _call_llm(messages, tools, allow_tools=False, timeout=self.timeout)
                msg = (resp.get("choices") or [{}])[0].get("message") or {}
                total_tokens += (resp.get("usage") or {}).get("total_tokens", 0)
                final_text = _safe_text(msg)
            except Exception as _ce:
                print(f"[agent-loop] re-anchor compose failed: {type(_ce).__name__}: {_ce}", flush=True)

        # 5) Final composed text fallback
        if not final_text and executions:
            parts = [f"已通过工具完成：{', '.join(e['tool'] for e in executions)}。"]
            for e in executions:
                if e["status"] == "ok":
                    parts.append(
                        f"[{e['tool']}] {_summarize_tool_result(e['tool'], e['result'])}"
                    )
                else:
                    parts.append(f"[{e['tool']}] 失败: {e['error']}")
            final_text = "\n".join(parts)

        # 6) Plain-text path (LLM never called any tool)
        if not final_text and not executions:
            # Try one more time without tools to get a clean text reply
            try:
                resp = _call_llm(
                    messages, tools, allow_tools=False, timeout=self.timeout
                )
                msg = (resp.get("choices") or [{}])[0].get("message") or {}
                usage = resp.get("usage") or {}
                total_tokens += usage.get("total_tokens", 0)
                final_text = _safe_text(msg) or "（无回复）"
            except Exception:
                final_text = "（无回复）"

        return {
            "response": final_text,
            "tool_executions": executions,
            "llm_route": "tool_call" if executions else "text",
            "stop_reason": stop_reason,
            "trace": trace,
            "total_tokens": total_tokens,
        }