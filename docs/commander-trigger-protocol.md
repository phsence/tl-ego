# Commander Trigger Protocol — Task 0 核实记录（2026-08-29）

> 本文件是 commander 链路所有代码改动的协议依据。字段和链路均来自源码直读，非猜测。

## 1. 服务端 → 触发文件（tl_ego_server.py, 8766）

| 文件 | 路径 | 用途 | 关键字段 |
|---|---|---|---|
| 主触发 | `WORKSPACE/.l5_trigger.json` | 插件 watcher 的一级触发 | tick, goal_type, tool_name, goal, goal_id, success_criteria, risk, domain, reason, intent_type, governance, outcome_contract, stage2_ranking, state_before{d}, jepa_expected_result |
| 元数据 | `WORKSPACE/.l5_bridge.json` | subagent 结构化元数据 | trigger 字段 + action_ladder, delayed_verification, recommended_first_action, bdi_snapshot |
| 通讯触发 | `WORKSPACE/.l5_comm_trigger.json` | ego 主动向指挥官说话的通道（message_commander） | 同 trigger 结构，goal_type=message_commander，含 comm_motivation/dominant/stagnation/competition/phase |
| 消费标记 | `.l5_trigger.consumed` / `.l5_bridge.consumed.json` | 防重消费；重启时服务端清理 stale 标记 | tick 值 |

**goal_id 硬约束**（`_record_goal_terminal` L16715）：必须匹配 `^l5g-\d{8}-\d{6}`（前缀匹配，允许后缀）。**`l5g-cmd-*` 会通不过终态写入**。commander 目标 ID 必须用 `l5g-YYYYMMDD-HHMMSS[-可选后缀]`。

## 2. 插件消费（extensions/tl-ego/index.js）

- 2s 轮询（`l5WatchIntervalMs: 2000`），同时 watch `.l5_trigger.json` 与 `.l5_comm_trigger.json`
- **防重门**：goal dedup（`wasGoalDispatched` + `.lae/ego_dispatch_state.json`）、pid cooldown、全局单飞锁 `MAX_INFLIGHT_SUBAGENTS = 1`（L5 和 comm 共享一个槽）
- **执行器热切换**：`extensions/tl-ego/executor_config.json` — `{"executor":"hermes"}` → `hermes chat -q --yolo --provider moa --source ego-<kind>-l5`；`"executor":"openclaw"` → node openclaw.mjs agent doubao-lobster。每次派发重读配置
- spawn 后：写 `.lae/l5_dispatch_requests.jsonl`；初始化 `.lae/l5_response.json`（outcome=pending）供 Stage1 consequence sensor
- **完成检测**：`ego_sandbox/<goalId>/result.json` 存在且 mtime 30s 不变 = 完成 → 释放全局锁（`ARTIFACT_STABLE_MS = 30_000`）
- 生命周期账本：`.lae/ego_goal_lifecycle.sqlite`（create/transition/get/record_late_success，经 goal_lifecycle_bridge.py）

## 3. 沙盒契约（spawn task 模板，index.js L1753 区域）

subagent 必须在 `ego_sandbox/<goalId>/` 内：建 README.md + 1-5 个项目文件 → 跑一次 smoke test → 写 `result.json`（字段：intent, project_name, goal_id, files_created, tests_run, outcome, lesson, next_impulse_hint）。
**硬边界（2026-08-26 事故教训已写入模板）：生产代码只读，禁止 subagent 直接改 tl_ego_server.py。**

## 4. 终态回写（唯一入口：POST /feedback，8766 L20993）

subagent 完成/失败后调 `POST /feedback` `{outcome, judge, reason, task_id}`：
1. `_record_goal_terminal(task_id, outcome, judge, reason, _body)` → l5_goals.jsonl 终态行 + goal_lifecycle sqlite 同步（outcome 在 `_GOAL_TERMINAL_SUCCESS/FAIL` 才写终态，否则保持 running）
2. 写 `lae_self_model.json` feedback_log（200 条滚动）
3. **feedback embedding learning**：outcome 文本 → embedding → 找相似神经元 → attract（judge∈correct/success/positive）/ repel → NEURON_EMBED 更新 —— 这是"根据结果改进自己"的神经层通道，已存在

## 5. Commander 集成决策（基于以上事实）

1. **无需新触发文件类型**：commander 目标写 `.l5_comm_trigger.json`（第二通道已存在、被同一 watcher 消费、kind="comm"），与自主目标（.l5_trigger.json）天然隔离，不互相覆盖
2. **goal_id 格式**：`l5g-{YYYYMMDD}-{HHMMSS}-CMD`（前缀匹配通过，后缀可带 CMD 标识）
3. **outbox 挂点**：`_record_goal_terminal` 内部（拿到 task_id + terminal 的唯一点）
4. **改进自己的神经通道**：/feedback 已带 embedding learning —— commander 的 feedback 目标复用同一条路
5. **tracking 数据源**：`ego_goal_lifecycle.sqlite`（插件生命周期）+ `l5_goals.jsonl`（服务端账本）双源；list_goals 工具读 jsonl 即可
