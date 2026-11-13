# TL-Ego 大模型化（LLM-Native Agent）改造计划

> Created: 2026-08-21
> Status: draft, awaiting review
> Scope: 让 TL-Ego 像 GPT-4/Claude 那样**自主多步规划工具调用**，而不是"挑一个工具答一句"

---

## 0. 问题陈述（基于 2026-08-21 实测）

### 0.1 当前实测现状（已验证）

**链路**：Hermes → 8770 shim → ToolRouter → 17 个工具 + SQLite 落库 ✅ 全部通

**ego 的 step-3.5-flash 决策能力（实测 5/5 选对工具）**：

| 测试 | 期望 | 实际 |
|---|---|---|
| 查系统状态 | 调 `check_status` | ✅ |
| 问最近记忆 | 调 `recall_memory` | ✅（返回 11 条真实记忆）|
| 写新记忆 | 调 `remember` | ✅（写入 Hy-Memory）|
| 设目标 | 调 `set_goal` | ✅ |
| 文本类问题 | 不调工具 | ✅ 走 text 路由 |

### 0.2 真正的硬伤（用户视角）

ego **能挑一个工具**，但**不会自己规划多步任务**。具体表现：

1. **单轮限制**：用户问"查状态 → 然后回忆 → 最后记住 '通过'"，ego 只能执行 1 个工具就回 text。第 2 个工具需要用户再问一次。
2. **无失败回退**：工具报 `error`，ego 直接把错误当回答吐出，不会尝试别的工具。
3. **无上下文合成**：工具结果回来了，ego 在第二轮时（在 stepfun_tool_caller.py:96–111）被强制不带 tools，只能从结果生成文本。**这意味着"基于工具结果决定下一步动作"这条路是断的**。
4. **无预算/超时控制**：用户多步任务理论上可无限循环，需要 max_steps + total_tokens 双预算。
5. **无规划透明度**：ego 不会在回答前告诉用户"我打算做 A → B → C"，而是黑盒直接干。

这些是**大模型 Function Calling 的标准能力**——OpenAI/Anthropic 都在 system prompt 里实现多轮 tool_calls 循环直到模型返回 stop。ego 没有这个循环。

### 0.3 与现有计划的关系

- `2026-08-10_ego-causal-self-evolution-roadmap.md` — 自进化工程化（不同主题，不冲突）
- `emergent-design-alignment.md` — 涌现设计（哲学层，本计划是工程层）
- `tl-ego-泰坦方案.md §13.2` — "Skill Graph 能力匹配"已识别"调度靠手动 / 4 个硬编码候选"问题，本计划是其工程化落地路径之一
- `tl-ego-runtime-governor.md` — 行为准则（Observe → Diagnose → Verify → Act → Record），本计划是其 LLM 层的实施

---

## 1. 目标（可验证）

让 ego 在以下 5 个场景里**自主多步规划**（不再需要用户每次问一次）：

| 场景 | 步骤数 | 验证 |
|---|---|---|
| 查 + 写 | 2 | 调 `check_status`，再调 `remember` 记录状态 |
| 回忆 + 总结 | 2 | 调 `recall_memory`，基于结果合成总结文本 |
| 失败回退 | 2–3 | 工具 A 报错 → 自动选工具 B 替代 |
| 多源融合 | 3+ | 调 `check_status` + `recall_memory` + `remember`，基于全部结果答 |
| 主动拒绝 | 0 | 不该调工具时果断答 text（不会过度调用）|

**完成定义**：上述 5 场景在 8770 shim 的 `/v1/chat/completions` 上 100% 通过。

---

## 2. 现状摸底（已读完代码）

### 2.1 关键代码位置

| 文件 | 行号 | 作用 | 限制 |
|---|---|---|---|
| `stepfun_tool_caller.py:89-132` | `_call_llm_with_tools` | 单轮 LLM 调用（带 tools） | 第二轮**强制不带 tools**，断绝多步规划 |
| `stepfun_tool_caller.py:114-128` | `payload` 构造 | `temperature=0.2, max_tokens=4096` | 第二轮不带 tools 是已知 bug fix 后的妥协 |
| `ego_openai_shim.py:53-90` | `_handle_chat_completions` | shim 主路由 | 调 `tool_router.execute(source="external_openai")` 后直接返回，**没有循环** |
| `tool_router.py` | — | 工具执行 + SQLite 落库 | OK，已有 26 条日志验证 |
| `ego_internal_bridge.py:85-127` | `_real_remember` | 写 Hy-Memory | OK |
| `ego_internal_bridge.py:130-160` | `_real_check_status` | 调 8766 /status | OK |

### 2.2 缺失的关键组件

| 组件 | 描述 | 现状 |
|---|---|---|
| **Agent Loop** | "调 LLM → 有 tool_calls → 执行 → 把结果塞回 messages → 再调 LLM" 循环 | ❌ 不存在 |
| **执行器** | 把 OpenAI-format tool_calls 喂给 ToolRouter | ✅ 已有（shim 里） |
| **max_steps 预算** | 防止无限循环 | ❌ 无 |
| **失败策略** | 工具 error 时怎么办 | ❌ 无（直接 echo 错误） |
| **规划提示** | 让 LLM 先说"我要做 A→B→C"再动手 | ❌ 无 |
| **可观测性** | 每次循环的状态、tokens、决策 | ❌ 无（除 SQLite 单条记录） |

---

## 3. 实施方案

### 3.1 总体策略

**不重写 LLM 调用栈**。在 shim（`ego_openai_shim.py`）里加一个 **agent loop** 层，让它循环调用 stepfun 直到模型返回 stop。step-3.5-flash 本身支持 OpenAI tool_calls 多轮，把循环搬到客户端即可。

**为什么改 shim 不改 stepfun_tool_caller**：
- shim 是对外接口层，加 loop 影响面最小
- `/contemplate` 异步链路走 `stepfun_tool_caller.py`（P5 路径），可独立升级
- shim 改造可独立验证：所有 OpenAI 客户端（包括 hermes）都能立刻享受

### 3.2 架构

```
Hermes / OpenAI SDK / curl
  └─→ POST /v1/chat/completions (8770)
        └─→ AgentLoop.run(query, tools)
              ├─ for step in range(max_steps=8):
              │    ├─ stepfun(messages, tools) → msg
              │    ├─ if msg.tool_calls == []: break
              │    ├─ for tc in tool_calls:
              │    │    └─ ToolRouter.execute(tc, source="external_openai")
              │    ├─ messages += [assistant(tool_calls), tool(results)...]
              │    ├─ if total_tokens > 8000: break
              │    └─ if any tool returned error: continue_with_retry
              └─ final_answer = msg.content
        └─→ OpenAI-format response
```

### 3.3 详细步骤

#### Step 1 — 新增 `agent_loop.py`（核心，~200 行）

新建 `C:\Users\Administrator\.openclaw\workspace\projects\tl-ego\agent_loop.py`：

```python
class AgentLoop:
    def __init__(self, max_steps=8, max_total_tokens=8000, llm_timeout=60):
        self.max_steps = max_steps
        self.max_total_tokens = max_total_tokens
        self.llm_timeout = llm_timeout
        self.router = get_router()
        self.llm_url = os.environ.get("EGO_LLM_URL", "https://api.stepfun.com/step_plan/v1/chat/completions")
        self.llm_model = os.environ.get("EGO_LLM_MODEL", "step-3.5-flash")

    def run(self, user_query: str, tools: list) -> dict:
        """Multi-step agent loop. Returns OpenAI-format response."""
        messages = [
            {"role": "system", "content": _load_system_prompt(tools)},
            {"role": "user", "content": user_query},
        ]
        trace = []  # each step: {step, tool_calls, results, tokens}
        total_tokens = 0
        final_msg = None

        for step in range(self.max_steps):
            # 1) Call LLM
            payload = {
                "model": self.llm_model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0.2,
                "max_tokens": 4096,
            }
            resp = self._call_llm(payload)
            msg = resp["choices"][0]["message"]
            usage = resp.get("usage", {})
            total_tokens += usage.get("total_tokens", 0)

            tool_calls = msg.get("tool_calls") or []
            step_record = {"step": step, "tool_calls": [], "results": [], "llm_tokens": usage}

            if not tool_calls:
                # Final answer
                final_msg = msg
                trace.append({**step_record, "final": True})
                break

            # 2) Execute each tool call
            messages.append({"role": "assistant", "content": msg.get("content"),
                             "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = tc["function"]
                try:
                    args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
                except Exception:
                    args = {}
                result = self.router.execute(name=fn["name"], args=args, source="external_openai")
                tool_result_msg = {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False)[:8000],  # truncate
                }
                messages.append(tool_result_msg)
                step_record["tool_calls"].append({"name": fn["name"], "args": args})
                step_record["results"].append({"status": result.get("status"), "error": result.get("error")})

            trace.append(step_record)

            # 3) Budget checks
            if total_tokens > self.max_total_tokens:
                final_msg = {"role": "assistant", "content": "[budget exceeded, returning partial result]"}
                break

        return self._build_response(final_msg or msg, trace, total_tokens)
```

#### Step 2 — 改造 `ego_openai_shim.py:53-90`

把 `_handle_chat_completions` 的 `tool_router.execute` 调用替换成 `AgentLoop.run()`。保留所有现有功能（SSE、错误处理、ID 生成、usage 统计）。

#### Step 3 — System Prompt 升级（`stepfun_tool_caller.py:_PROMPT_PATH`）

提示词加入：
- "你可以连续调用多个工具。每个工具结果返回后再决定下一步"
- "如果工具报错，尝试别的工具或诚实告诉用户"
- "在第一次回答前先在心里规划步骤，不必说出来"

#### Step 4 — 失败回退策略（agent_loop.py 内）

```python
# 工具返回 status=error 时，messages 不停在该错误上
# 让 LLM 在下一轮基于错误结果自己决定怎么办
# 但每条工具最多重试 1 次（防死循环）
```

#### Step 5 — 可观测性

每次 `AgentLoop.run` 返回带 `agent_trace` 字段（OpenAI response 里加自定义字段）：
```json
{
  "choices": [...],
  "usage": {...},
  "agent_trace": [
    {"step": 0, "tools": ["check_status"], "results": ["ok"]},
    {"step": 1, "tools": ["remember"], "results": ["ok"]}
  ],
  "stop_reason": "natural_stop" | "max_steps" | "budget_exceeded"
}
```

shim `/v1/memory/summary` 也要聚合新维度：`by_stop_reason`。

#### Step 6 — `/contemplate` 异步链路同步升级

`stepfun_tool_caller.py:_call_llm_with_tools` 第二轮不带 tools 的限制改成：第二轮**仍带 tools**，允许模型再决策。配合 server 端 `agent_loop.py` 复用同一循环逻辑。

### 3.4 验证计划

| 验证项 | 命令/方式 | 通过标准 |
|---|---|---|
| 单步不退化 | shim 已有 5 个 case | 5/5 通过，与基线一致 |
| 2 步规划 | 新 case "查状态并记住" | 2 个 tool_calls 出现在同一 response 的 `agent_trace` |
| 失败回退 | 新 case "先调不存在工具" | LLM 自动选别的工具或诚实答 text |
| 预算保护 | 新 case "强制 10 步任务" | max_steps=8 截断，`stop_reason=max_steps` |
| ToolRouter 落库 | `/v1/memory/tool_executions?source=external_openai` | 每个 step 都落 1 条 |
| LLM 真选了工具 | 检查 `tool_choice=auto` 在 messages 里返回 | step-3.5-flash 返回 `tool_calls != null` |
| 不该调时不调 | case "你是谁" | `agent_trace` 为空，`stop_reason=natural_stop` |

### 3.5 文件改动清单

| 文件 | 类型 | 行数估计 |
|---|---|---|
| `agent_loop.py` | 新增 | ~200 |
| `ego_openai_shim.py` | 改 | ~80 行替换 |
| `stepfun_tool_caller.py` | 改 | ~30 行（system prompt + 第二轮逻辑） |
| `_PROMPT_PATH` 指向的 system prompt 文件 | 改 | ~20 行 |
| `test_agent_loop.py` | 新增 | ~150 |
| `.hermes/plans/2026-08-21_ego-agent-loop-implementation.md` | 新增（本文件） | — |

总计：**~480 行新代码 + ~110 行修改**。

---

## 4. 风险与权衡

### 4.1 已识别风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| step-3.5-flash 不擅长多轮 tool 规划 | 测试可能 fail | 实测 5/5 已选对单工具，第二轮 baseline 已有；用 max_steps=8 兜底 |
| Token 增长失控（多轮 tool 结果拼接） | 成本/超时 | max_total_tokens=8000 硬截断 + 单条 tool result 8KB 截断 |
| 工具循环依赖（A → B → A → B...） | 死循环 | 记录已调工具+args，重复直接跳过 |
| shim 改造影响现有客户端 | 兼容 | 保留所有 OpenAI 字段，自定义字段加在顶层；功能完全向后兼容 |
| LLM 在第二轮仍坚持调工具 | 已知 bug（现有 workaround） | 第二轮**带 tools 但 tool_choice="none"**，强制只生成文本（与现有逻辑一致） |
| ego 异步链路 `/contemplate` 也得改 | 范围扩大 | 同 agent_loop 复用代码，1 处升级两处受益 |

### 4.2 不做的事

- ❌ 不引入新依赖（保持 stdlib + requests）
- ❌ 不动 tool_router 内部（仅通过 `router.execute()` 调用）
- ❌ 不改 TL-Ego L0–L10（这是 LLM 层计划）
- ❌ 不动 Minecraft numen 集成
- ❌ 不在第一版做"主动拒绝"判断（LLM 自然行为即可）

---

## 5. 实施顺序（建议 2 个 sprint）

### Sprint 1（最小可用，预计 2–3 小时）
1. ✅ 写 `agent_loop.py` 骨架（Step 1）
2. ✅ 改 shim 接入（Step 2）
3. ✅ 跑通"查 + 写"2 步 case
4. ✅ ToolRouter 落库验证（Step 5）

### Sprint 2（完善，预计 1–2 小时）
1. ✅ 失败回退（Step 4）
2. ✅ system prompt 升级（Step 3）
3. ✅ 5 场景全过
4. ✅ `/contemplate` 异步链路同步（Step 6）

### Sprint 3（可选，1 小时）
1. ✅ 测试覆盖（`test_agent_loop.py`）
2. ✅ `agent_trace` 加到 summary
3. ✅ 写入 `MEMORY.md` 和 SKILL `tl-ego-agent-loop`

---

## 6. 完成定义（DoD）

- [ ] 5 场景验证全部通过
- [ ] `agent_trace` 在响应中可读
- [ ] ToolRouter SQLite 落库条数 = 实际 tool_calls 数
- [ ] 不退化：现有 `/contemplate` 异步 case 仍工作
- [ ] 写完 `MEMORY.md`（用户偏好 + 决策点）和更新相关 skill
- [ ] 与 `tl-ego-runtime-governor.md` 的"Observe → Diagnose → Verify → Act → Record"五阶段对齐（agent_trace 即 Record）

---

## 7. 开放问题

1. **max_steps 默认值？** 8 是估计。OpenAI Cookbook 建议 5–10。需实测调优。
2. **tool_choice="auto" vs "required"** 在第二步是否要强制？让 LLM 自由决策更灵活，但偶尔会过早收尾。
3. **是否暴露 SSE 流式中间步骤？** OpenAI 支持 tool_calls 流式增量。当前 shim 是非流式。第一版保持非流式，Sprint 3 可选。
4. **预算 8000 tokens 是否合理？** 取决于 LLM 在第二步会输出多少文字。需先跑一次真实测试。

---

## 8. 参考

- OpenAI Function Calling 文档（multi-turn tool use）
- Anthropic Tool Use 文档（loop 模式）
- 现有 `tl-ego-泰坦方案.md §13.2` Skill Graph 段落
- 现有 `tl-ego-runtime-governor.md` 五阶段框架
- 实测数据：8772 memory API summary 26 条 / 0 错误

---

> Author: Hermes (via /plan skill)
> Saved at: `.hermes/plans/2026-08-21_ego-agent-loop-implementation.md`
> Review by: user (commander)