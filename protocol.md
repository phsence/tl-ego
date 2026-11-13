# TL-Ego Protocol — API Surface Spec (v1 draft)

> 生成日期: 2026-08-29 · 依据: 源码核对 (`tl_ego_server.py` / `ego_openai_shim.py` / `ego_memory_api.py`) + 活体响应采样
> 灵感: VCPToolBox vcpAPI 的"统一协议层"思路 — 把分散端点 formalize 成可扩展锚点
> 维护约定: 端点变更时同步更新本文件; 未标注"未核对"的条目均已实测或读源确认

---

## 0. 服务拓扑

| 端口 | 服务 | 文件 | 绑定 | 职责 |
|---|---|---|---|---|
| 8766 | TL-Ego core | `tl_ego_server.py` | 127.0.0.1 | 神经运行时 + 认知 API + 学习闭环 |
| 8770 | OpenAI shim | `ego_openai_shim.py` | 127.0.0.1 | OpenAI 兼容协议接入 (Hermes 入口) |
| 8772 | Memory API | `ego_memory_api.py` | 127.0.0.1 | 工具执行历史 (SQLite 只读视图) |
| 8002 | Embedding | llama-server (BGE-M3) | local | 1024d 向量 |
| 8767 | Causal-Lens | `causal-lens/causal_engine.py --serve` | local | 因果分析 |
| 8099 | Neural Grid | — | local | 网格可视化 |
| 3007 | MC Observer | numen-viewer | local | Minecraft 世界观 |

依赖方向: 8770 → 8766 · 8766 → 8002/8767 · 外部客户端 → 8770 (推荐) 或 8766 (直连)

## 1. 通用约定

- **传输**: 纯 HTTP/JSON, UTF-8 (`ensure_ascii=False`), 无认证 (loopback only — 不要暴露到 0.0.0.0)
- **CORS**: 8766 允许 `*`, 支持 `OPTIONS` 预检 (GET/POST/Content-Type)
- **错误形状**: 8766 序列化失败 → `{"status":"error","error":"response_serialization_failed","detail":"..."}` (500)
- **客户端断连**: 服务端容忍 BrokenPipe, 不崩溃 (2026-08-27 教训固化)
- **幂等性**: 无。POST 均有副作用 (tick 推进/状态写入/学习更新), 不要重试轰炸
- **并发模型**: 8766 = ThreadingHTTPServer; 8770/8772 = 单线程 HTTPServer

## 2. 8766 — TL-Ego Core

### 2.1 状态与遥测 (GET)

| 端点 | 说明 |
|---|---|
| `/status` | 全量运行时遥测。**主健康检查端点**。含 neurons/full_edges/edge_view/dominant_channels/continuity/stagnation/act 等。注意: `full_edges` 是 runtime cache 而非 EdgeStore 总量 (懒加载); 初始化完成前返回 `{"status":"initializing"}` |
| `/resting-log` | resting tick 日志 |
| `/l8` | L8 元观察状态 |
| `/l10` | L10 元层状态 |
| `/drives` | 内稳态驱动 |
| `/bdi-snapshot` | BDI 三元组 (`titan_stage2_bdi_snapshot_v1`): beliefs/desire/intention/confidence/pressures/pressure_rank |
| `/action-gateway` | 行动网关状态 |
| `/debug-stacks` | 线程栈转储 (诊断用) |

### 2.2 自我认知 (GET)

| 端点 | 说明 |
|---|---|
| `/self-model` | `lae_self_model.json` 视图: fingerprints/feedback_log(≤200)/self_narrative/self_narrative_ts/accumulated_narrative。⚠️ accumulated_narrative 可达 200KB+, 客户端必须截断 |
| `/know-yourself` | ⚠️ **已知损坏** (2026-08-29 实测): 读 `.lae/ego_self_model.json` 但该文件不存在 → `{"error":"self-model file not found"}`。自我模型实际由 `self_model_manifest.py` 经 `/contemplate` 注入 |
| `/autobiography/timeline` | 自传时间线 |
| `/autobiography/why` | "为什么"叙事 |
| `/long-term-memories` | 长期记忆列表 |
| `/reasoning_neurons` | 推理神经元 |
| `/intention` | 当前意图 |
| `/l7-status` `/l7-model` `/l7-value` `/l7-proposals` `/l7-phase3-status` | L7 子系统只读视图 |

### 2.3 认知执行 (POST)

| 端点 | 请求体 | 响应/语义 |
|---|---|---|
| `/contemplate` | `{query (必填, >2000字符截断), depth?=1, adaptive_depth?=false, fast?=false}` | **异步**: 立即返回 `{job_id, status:"running"}` |
| `/contemplate-job` | `{job_id}` | 轮询 job。内存未命中回落文件 `.lae/jobs/{job_id}.json`; 404=`job not found`。完成后 payload 内含 `result` |
| `/resting` | `{visual_seeds?, somatic_seeds?, adaptive_max_steps?=1}` | 同步执行 tick (ACT 控制器按 state 计算 steps)。返回 dominant/conc/comp/n_activated/act 等 |
| `/metacontemplate` | — | 元层沉思 |
| `/quantum-experiment` | `{experiment, noise, shots, seed}` | **严格字段白名单**, 多余字段 400 |

**Contemplate 异步流 (标准客户端契约, 见 `scripts/contemplate.ps1`)**:
```
POST /contemplate {query}        → {job_id}
POST /contemplate-job {job_id}   → {status:"running"}   (轮询, 建议 2-5s 间隔)
POST /contemplate-job {job_id}   → {status:"done", result:{...}}  (result 非空, response_fallback 缺席 = 健康)
```

### 2.4 学习闭环 (POST)

| 端点 | 说明 |
|---|---|
| `/feedback` | **L5 goal 终态唯一入口**。请求体: `{outcome, judge, reason, task_id, action_id?, ...}`。副作用链: ① goal 终态账本 (jsonl+sqlite) ② feedback_log 追加 (self-model, 200 上限) ③ embedding Hebbian/anti-Hebbian (judge∈{correct,success,positive}→吸引, 否则排斥, rate=0.05, ANN top-5) ④ 仲裁策略权重更新 ⑤ 工具演化评估 ⑥ action_impact 回链 |
| `/directive` | 指挥官指令注入 |
| `/approve` | 审批门 (L5 修复三闸) |

### 2.5 运维/维护 (POST)

`/save-checkpoint` `/rebuild-faiss` `/reset-stdp` `/find-seeds` `/inject-sensory` `/apply-parameter` `/noise` `/drive` `/run-many` `/web-search` `/scan-workspace` `/l7-explore` `/l7-mission` `/l7-proposals` `/l7-evolve` `/l7-evolution-report` `/l7-gaps`

> 参数未逐一核对 — 使用前先读 `tl_ego_server.py` do_POST 块对应分支 (GET 路由从 L20333 起, POST 从 L20898 起)。

## 3. 8770 — OpenAI Shim

OpenAI Chat Completions 兼容层。**两种模式互斥, 由请求是否携带 `tools` 决定**:

| 场景 | 行为 |
|---|---|
| 客户端带 `tools` (如 Hermes) | **纯透传**: LLM 的 tool_calls 原样返回 (`finish_reason=tool_calls`), 客户端回传 `role=tool` 消息, shim 无状态转发。两套工具不混用 |
| 客户端无 `tools` | **自含模式**: ego_prompts 中文模板 + 自我状态注入, ego 自己的 MC/记忆/状态工具集 |

| 端点 | 方法 | 说明 |
|---|---|---|
| `/v1/chat/completions` | POST | 主端点。缺 `messages` → 400; `stream:true` → SSE 仿真 (单 chunk: role + content + tool_calls? + finish + `[DONE]`, 非逐 token) |
| `/v1/tools` | GET | 当前路由器可用工具列表 (OpenAI list 格式) |
| `/health` | GET | `{status:"ok", shim:<version>}` |
| `/` | GET | 自描述 index |

错误: 400 invalid JSON / missing messages · 404 unknown path · 500 `{error}` (不静默 — 如实上报是设计原则)

## 4. 8772 — Memory API

工具执行历史的只读视图 (SQLite, 经 tool_router):

| 端点 | 参数 | 说明 |
|---|---|---|
| `/v1/memory/tool_executions` | `source?, tool?, since?(ISO ts), limit?(=100)` | `{object:"list", count, filters, data[]}` |
| `/v1/memory/summary` | `source?` | 按 tool/source 计数 + 错误总数 |
| `/v1/memory/usage` | — | 自描述帮助 (**借鉴 vcpAPI**: 端点自解释) |
| `/health` | — | 存活检查 |

## 5. 消费方契约摘要

| 消费方 | 用法 |
|---|---|
| Hermes / OpenAI SDK | → 8770 `/v1/chat/completions` (带 tools 透传) |
| `scripts/contemplate.ps1` | → 8766 contemplate 异步流 (有 pytest 守护 `test_contemplate_ps1.py`) |
| L5 sandbox 任务 | → `/status` 读态 + `/feedback` 终态回报 (强制四件套之一) |
| ego_openai_shim | → 8766 内部调用 + 8772 记忆查询 |
| 本协议文档读者 | 新端点先加这里, 再写代码 |

## 6. 已知缺口 (2026-08-29 实测)

1. `/know-yourself` 引用的 `.lae/ego_self_model.json` 不存在 — 要么修路径要么下线端点
2. `/self-model` 的 `accumulated_narrative` 无上限 (223KB+) — 建议服务端加截断或分页
3. POST 运维类端点 (§2.5) 无参数文档 — 逐个补齐属增量工作
4. 无认证机制 — 仅靠 loopback 绑定保证安全

## 7. 与 vcpAPI 的取舍

- ✅ 借鉴: 端点分组 + 自描述 (/usage, /) + 统一错误语义
- ❌ 不抄: vcpAPI 虚拟化 IPC/插件层 — 单 ego 场景是过度设计; 等多 agent 真落地再评估
