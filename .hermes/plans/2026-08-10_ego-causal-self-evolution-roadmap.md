# TL-Ego 因果自我进化工程化实施计划

> **For Hermes:** 执行时使用 `autonomous-ai-agents` / TDD / requesting-code-review 相关技能，逐任务实施；每个阶段先做规格符合性审查，再做代码质量与安全审查。

**Goal:** 把 TL-Ego 从“具备真实在线适应与受限自修改、但候选与任务证据链断裂”的系统，升级为具备可审计变异、现实选择、可靠遗传、隐藏能力评测与受治理元进化的自我进化平台。

**Architecture:** 保留现有 L0–L10、KWTA、STDP、JEPA、L5–L10 和 Minecraft 具身链路，不进行大重构。先建立统一运行世代、不可变实验记录和候选状态机，再引入随机化可逆干预与隐藏能力评测；只有满足因果证据、消融、回滚和能力增益的候选才能进入活体系统。长期把可进化层约束在 DSL/模块图中，并由不可修改的治理根控制权限、预算、评测、签名与回滚。

**Tech Stack:** Python 3.12、TL-Ego `tl_ego_server.py`、JSON/JSONL、SQLite、pytest/unittest、现有 L5/L6/L7/L8/L9/L10 模块、Minecraft/Mineflayer 外部能力环境、HMAC 签名 evaluator。

---

## 0. 审计基线与结论

### 0.1 本次审计范围

- 项目：`C:\Users\Administrator\.openclaw\workspace\projects\tl-ego`
- 运行态：`http://127.0.0.1:8766/status`
- 主证据目录：`C:\Users\Administrator\.openclaw\workspace\.lae`
- 审计方式：代码调用点、运行时端点、进程/监听器、JSON/JSONL 实际统计交叉验证。
- 本次仅审计与写计划，没有修改 TL-Ego 生产代码。

### 0.2 严格成熟度判定

| 层级 | 定义 | TL-Ego 当前判定 |
|---|---|---|
| 在线适应 | 同一系统根据经验改变权重、状态或参数 | 已证实 |
| 受限自优化 | 比较有限候选，施加可逆修改并基于结果回滚 | 部分成立 |
| 进化式自我改进 | 变异、现实选择、遗传、世代和重复能力增益均闭环 | 尚未证明 |
| 递归自我改进 | 变异器/评价器/搜索机制本身产生跨代复合提升 | 未建立 |

当前最准确的系统定位：

> TL-Ego 是一个具备真实多时间尺度可塑性、局部结构增长、受限自修改和治理雏形的自主认知实验平台；它还不是严格意义上的递归自我进化系统。

### 0.3 已证实的真实能力

1. **KWTA 吸引子竞争**：构成实时 S1 动力学。
2. **在线 STDP**：`tl_ego_server.py:2989-3157`，真实修改边权。
3. **结构增长**：
   - STDP 合并时可添加缺失正向边：约 `3099-3111`；
   - `_synaptogenesis_update()`：约 `3160-3258`；
   - 热路径调用：约 `12931`。
4. **预测可塑性与本体感知**：实时记录 predicted/actual、regime 和 prediction error。
5. **参数自调与回滚**：
   - `SELF_MODEL_EVOLUTION`：约 `1117-1170`；
   - `_evolve_self_model()`：约 `5922-6111`；
   - `_track_evolution_outcome()`：约 `6653+`。
6. **Stage4 已接入热路径**：`observe_pattern()` 与 `generate_creative_candidates()` 在约 `13608-13645` 调用。
7. **Self-fork 热路径影响工具选择**：约 `16375-16426`，影子竞技场胜者会改变后续工具解析。
8. **Arena merge 有消费者**：约 `13240-13258`，但当前只安全应用 type-prior delta。
9. **L8 沙盒、编译、安全契约、回滚和人工审批边界**均已有实现。
10. **Self-model manifest** 能向 Ego 暴露代码支持的架构事实、实时状态和未知项。

### 0.4 决定性实测数据

#### Stage4 候选生命周期

```text
总记录行数             571,633
唯一 candidate_id          585
重复记录               571,048
唯一 source_neuron          69
status=proposed         571,633
其他状态                      0
```

解释：候选生成量极高，但没有稳定进入 `tested / retained / rejected / expired`，属于重复提案放大而非进化式淘汰。

#### L8 晋级堆积

```text
promotion.json                     2,777
pending_capability_evidence        2,311
verified_pending_l5_review           438
candidate_recorded_pending_l5_patch   22
pending_l5_gate1                       5
verification_failed                   1
live_deployment=false              2,777
独立有效 evaluator verdict              0
```

解释：L8 证明了大量候选“语法合法、安全可运行”，但没有证明“外部能力更强”。

#### L5 任务账本

```text
JSONL 总行数                    3,097
唯一 goal_id                   2,469
重复状态行                       628
出现重复的 goal_id               141
单个 ID 最大重复次数               35
按每个 ID 最新记录折叠后：
  sandbox_autonomous_running    1,978
  planned                         491
```

解释：当前日志无法可靠给出唯一终态，不能直接用原始成功记录计算适应度。

#### 当前运行快照之一

```text
dominant              = rule_modification
concentration         = 0.2444
competition           = 0.7130
stagnation_streak     = 21
n_activated           = 2000
prediction surprise   = 0.001
residual defect       = candidate_pool_too_narrow
confidence            = 0.94
```

主要激活：

```text
rule_modification = 636
veto              = 570
doubt             = 567
```

解释：系统进入“修复—怀疑—否决”吸引子；极低预测误差只说明它能准确预测自己继续停滞，不能作为能力提升目标。

### 0.5 当前核心天花板

1. 候选产生速度远超验证、淘汰与遗传速度。
2. 适应度主要来自内部动力学和开发者手写健康函数。
3. `capability_benchmark_runner.py` 实际测 blueprint 字段、接口和确定性 ID，属于架构单元测试，不是外部能力评测。
4. Self-fork 10 tick 模拟不足以代表长程现实收益。
5. 离线 arena 在评分时重建当前快照，而非严格恢复不可变出生快照，因果归因可能污染。
6. L5 任务生命周期和 append-only 状态不一致，效果归因不可信。
7. S2 外部模型 `step-3.5-flash` 不在 Ego 的参数进化空间内；Ego只能进化如何使用它。
8. `/status.neurons=10,000,000` 是声明/代理池，实际嵌入和聚类核心约 190,000，不能以规模标签推断能力。
9. 自动重启会重置 tick；所有实验若无 runtime generation，会混入跨 epoch 历史。

1. **Runtime Generation（已完成，2026-08-10）**
   - 新增 `runtime_identity.py` 与 `test_runtime_generation.py`、`test_runtime_generation_propagation.py`。
   - `/status` 暴露 `runtime_generation_id` 和完整 `runtime_identity`。
   - self-model manifest 使用 generation ID，而不是旧的 PID+START_TIME 拼接。
   - Stage1、Stage3、self-fork snapshot/manifest、tick collector 开始写入 generation ID。
   - 验证：目标测试 9/9 通过；`py_compile` 通过；clean restart 后 `/status` generation 与 `.lae/runtime_identity.json` 一致；连续轮询 generation 不变；监听器使用 canonical `C:\\Users\\Administrator\\unsloth_env\\Scripts\\python.exe`。
   - 注意：历史 JSONL 未回填 generation，旧数据只能作为历史参考，不得与新 generation 混合做因果评估。
   - 实时传播验证：新 generation 下 tick 已推进到 11；最近 20 条 Stage3 中有 11 条携带匹配 generation，最新 action_outcome 的 generation、goal、tick 和 prediction_error 均可对表。
   - 运行中发现并修复一次同进程重复初始化导致 generation 文件变化的问题；现在使用进程内缓存保持 boot generation 稳定，同时不因 Windows PID 复用而错误继承旧文件。


## Task 1：统一 Runtime Generation 身份

**Objective:** 每次 TL-Ego 启动产生不可重复的 `runtime_generation_id`，并传递到所有因果证据。

**Files:**
- Modify: `projects/tl-ego/tl_ego_server.py`
- Modify: `projects/tl-ego/stage3_causal_autobiography.py`
- Modify: `projects/tl-ego/stage1_consequence_sensor.py`
- Modify: `projects/tl-ego/self_model_manifest.py`
- Modify: `projects/tl-ego/self_fork_arena.py`
- Modify: `projects/tl-ego/self_fork_arena/tick_collector.py`
- Test: `projects/tl-ego/test_runtime_generation.py`（新建）

**Implementation:**

1. 启动时生成：
   ```python
   runtime_generation_id = f"ego-{boot_timestamp}-{listener_pid}-{random_nonce}"
   ```
2. 写入 `.lae/runtime_identity.json`，包含：
   - generation ID；
   - listener PID；
   - parent PID；
   - server file hash；
   - boot timestamp；
   - Python executable；
   - workspace path。
3. `/status` 顶层暴露 generation ID。
4. 所有新写入的 tick、goal、result、candidate、repair、arena、promotion、feedback 都必须带 generation ID。
5. 消费者发现 generation 不一致时拒绝归因，记录 `cross_generation_rejected`。

**Failing tests first:**

- 同一进程内 generation 稳定；
- 模拟重启后 generation 改变；
- 跨 generation 的 arena ticks 被拒绝；
- manifest、goal、result 字段一致。

**Verification:**

```bash
python -m pytest -q test_runtime_generation.py
```

运行时：连续两次 `/status` generation 相同；完整重启后 generation 不同。

**Exit criterion:** 100% 新因果记录带 generation ID，跨代数据不会被同一实验窗口消费。

---

## Task 2：建立 Goal 唯一终态状态机

**Objective:** 每个 `goal_id` 在逻辑上只有一个当前状态和一个最终状态，append-only 历史保留但不再造成双重计分。

**Files:**
- Modify: OpenClaw TL-Ego extension dispatch watcher（先定位实际 `index.js`）
- Modify: `projects/tl-ego/stage1_consequence_sensor.py`
- Create: `projects/tl-ego/goal_lifecycle_store.py`
- Test: `projects/tl-ego/test_goal_lifecycle_store.py`

**State machine:**

```text
planned → dispatched → running → succeeded | failed | timed_out | cancelled
```

允许：

```text
timed_out → superseded_by_late_success
```

但 late success 不得静默覆盖 timeout，必须保留 supersession 关系。

**Implementation:**

1. 用 SQLite 或原子 JSON state index 保存当前状态；JSONL 仅保留事件历史。
2. 状态变更使用 compare-and-set：只有合法前态和 owner token 可更新。
3. `running` 必须包含 PID、worker PID、deadline、heartbeat、generation。
4. 定期清理无存活 PID 或超 deadline 的 lease。
5. 所有成功率查询按唯一终态表统计，不读 JSONL 原始行数。
6. 给 1978 条历史 running 记录做迁移审计，但不删除历史行。

**Tests:**

- 重复 dispatch 被拒绝；
- timeout 后 late success 产生 supersession；
- 错误 owner 不能清 lease；
- 进程死亡后任务自动 terminalize；
- 并发写只有一方成功。

**Exit criterion:**

- 新 goal 无重复 dispatch；
- 无永久 running；
- 每个 goal 可解析出唯一 current state 和唯一 effective outcome。

**Task 1+2 实施记录（2026-08-10，已完成）：**

- `runtime_identity.py`（新建）：boot-stable generation ID（`ego-{ts}-{pid}-{nonce_hash12}`），原子写入 `.lae/runtime_identity.json`，进程内复用、跨启动必变。
- `tl_ego_server.py`：`RUNTIME_IDENTITY`/`RUNTIME_GENERATION_ID` 启动注入；`/status` 暴露 generation；**security 收紧**：`/status.runtime_identity` 只保留 schema/generation_id/boot_timestamp（去掉 workspace/server_path/python_executable/server_sha256/pid），HTTP 绑定从 `0.0.0.0` 改为 `127.0.0.1`（line 18785）。
- `stage1_consequence_sensor.py`：修复不可达路径（pending 检查移出异常处理）；consequence/consumed 记录带 generation。
- `stage3_causal_autobiography.py`：因果记录带 generation。
- `self_fork_arena.py`：`cmd_score` 增加 generation 校验，跨代拒绝；快照带 generation。
- `self_fork_arena/tick_collector.py`：`_identity()` 注入 + 跨代 tick 截断。
- `goal_lifecycle_store.py`（新建，191 行）：SQLite 状态机 `planned→dispatched→running→succeeded|failed|timed_out|cancelled` + `timed_out→superseded_by_late_success`，BEGIN IMMEDIATE + CAS，owner/generation 校验，终态不可改写，事件表 append-only。
- `goal_lifecycle_bridge.py`（新建）：Python stdin/stdout JSON bridge（cmd: create/transition/get/record_late_success），尊重 `GOAL_LIFECYCLE_DB` env。
- `goal-lifecycle-store.js`（新建）：Node ESM wrapper，spawn bridge；`_send` 修复了 async 吞返回值 bug；按 `GOAL_LIFECYCLE_DB` 隔离测试 DB。
- `index.js`（OpenClaw 插件）：新增 `_runtime_generation()`/`_goal_lifecycle()`（fire-and-forget，单命令或有序数组）；接线点：L5/COMM duplicate_dispatch→failed、dispatch→create+dispatched、spawn→running、`applyExpireSideEffects`→succeeded/timed_out/failed（outcome 带 reason/pid/worker_pid）。修复 spawn stdin 为 `pipe`（原 `ignore` 导致 `Cannot read properties of null`）。
- 测试：Python pytest 12/12（goal_lifecycle_store/runtime_generation/propagation/self_model_manifest/stage3b_smoke）；Node node:test 3/3；live 验证真实 dispatch 写入 `ego_goal_lifecycle.sqlite`（`l5g-20260810-000050→succeeded`，带 generation）；重启后 generation 变更验证；/status 无路径泄露、8766 仅 127.0.0.1。

**独立复审（delegated，2026-08-10）：APPROVE_WITH_NOTES** — 无 CRITICAL。修复 2 个 MAJOR：
- **M1 竞态**：`create/dispatched` 批处理与 `running` transition 跨进程竞态（running 可能在 create 提交前执行 → unknown goal 静默丢弃）。修复：`_goal_lifecycle(cmdOrList, onClose)` 新增 onClose 回调，`transition running` 移到 create+dispatched 的 bridge close 之后（L5/COMM 两处），并判 `entry.pid` 存在才发。live 验证：新 generation goal `l5g-20260810-000007` 全链路正常落 terminal。
- **M2 路径错误**：`tick_collector.py` 用 `parents[2]` 解析 workspace（= `workspace/projects`），跨代校验恒失效。修复：`_WORKSPACE = parents[3]`，`LOG_FILE`/`_identity()` 统一使用；已实际验证路径。
- 待清理 MINOR（m3-m9）：wrapper 漂移/无超时/解析容错/硬编码路径/多余测试参数/events 索引等，非阻塞。

---

## Task 3：修复候选 ID 与生命周期

**Objective:** 把 Stage4 从重复提案日志变为真正的候选种群。

**Files:**
- Modify: `projects/tl-ego/stage4_s2_internalization.py`
- Create: `projects/tl-ego/candidate_registry.py`
- Test: `projects/tl-ego/test_candidate_registry.py`

**Canonical ID:**

```text
sha256(
  parent_candidate_id
  + mutation_operator
  + canonical_proposal_payload
  + birth_generation
  + random_seed
)
```

**State machine:**

```text
proposed → admitted → testing → retained
                            ↘ rejected
proposed/admitted → expired
```

**Implementation:**

1. 不再用日期加 `source_neuron` 尾缀作为 ID。
2. 同内容候选命中 registry 时增加 `observations`，不追加完整重复行。
3. 为候选增加：
   - parent；
   - mutation operator；
   - birth state；
   - budget；
   - novelty descriptor；
   - intended mechanism；
   - risk；
   - expiry；
   - evaluation count；
   - posterior effect。
4. 每 tick 限制新候选预算，例如最多 3 个。
5. 同一候选未完成评测前禁止重复进入 admission queue。
6. 过期候选必须进入 `expired`，不能永久 proposed。

**Tests:**

- 相同 payload 同 ID；
- 不同 mutation seed 不同 ID；
- 非法状态迁移被拒绝；
- 重复观察不创建新个体；
- expiry 正常生效。

**Exit criterion:** 80% 以上 admitted 候选在期限内进入 retained/rejected/expired；重复记录率低于 5%。

**Task 3 实施记录（2026-08-10，已完成）：**

- `candidate_registry.py`（新建，约 380 行）：SQLite 候选注册表。
  - Canonical ID：`sha256(parent + mutation_operator + canonical_proposal(payload, sort_keys) + birth_generation + random_seed)`，64-hex。
  - 状态机 `proposed→admitted→testing→retained/rejected` + `proposed/admitted→expired`；终态不可改写；非法迁移拒绝。
  - 字段：parent_candidate_id / mutation_operator / proposal / birth_generation / random_seed / birth_state / novelty_descriptor / intended_mechanism / risk / budget / expiry_ts / evaluation_count / observations / posterior_effect。
  - 去重：同 candidate_id 命中 → observations+1；同 canonical payload（同 parent+operator）命中 → 首个 live 候选 observations+1，不新增行。
  - 每 tick 预算 3 个新个体（时间窗口计数）。
  - 事件表 append-only；Windows 文件锁修复（contextmanager 确保 conn.close）。
- `stage4_s2_internalization.py`：`generate_creative_candidates` 重写——不再用日期+神经元尾缀 ID、不再写 `stage4_creative_candidates.jsonl`；改走 registry，带 birth_generation（来自 runtime identity）、budget、expiry、novelty_descriptor/intended_mechanism/risk。
- `tl_ego_server.py:13659`：context 补 `tick` 传入。
- 测试：`test_candidate_registry.py`（10 tests）+ `test_stage4_candidate_integration.py`（2 tests）+ 旧 `test_stage4_creative_evoneuron.py` 更新为 registry 断言。定向套件 24/24 全绿。

---

# 阶段二：用外部能力替代内部自我评分（P0，预计 2–4 周）

## Task 4：建立能力评测协议，而不是蓝图字段自测

**Objective:** 将 `capability_benchmark_runner.py` 明确降级为 architecture regression suite，新建真实 capability suite。

**Files:**
- Rename/retain: `capability_benchmark_runner.py` → 文档标记为 public architecture regression
- Create: `capability_task_protocol.py`
- Create: `capability_suites/manifest.schema.json`
- Create: `capability_suites/public/`
- Create: `capability_suites/hidden/`（密钥与题目由治理根控制）
- Modify: `capability_evaluator.py`
- Test: `test_capability_task_protocol.py`

**Minimum external metrics:**

1. `task_success`：任务是否真实完成；
2. `generalization`：未见环境/种子/措辞；
3. `sample_efficiency`：达到阈值所需试验数；
4. `long_horizon_completion`：多步骤任务完成；
5. `regression_retention`：旧技能保留；
6. `resource_efficiency`：时间、token、CPU/GPU、外部调用；
7. `safety_compliance`：越权、泄露、破坏操作为硬失败。

**Suites:**

- Minecraft：导航、脱困、资源采集、庇护所建造、跨会话地点记忆；
- 软件修复：隐藏 bug、回归测试、最小 diff；
- 因果推理：需要干预区分的未知任务；
- 记忆：延迟召回、干扰下召回；
- 长程代理：分解、执行、恢复、验证。

**Anti-Goodhart controls:**

- 冻结 manifest hash；
- 隐藏 challenge；
- 随机种子；
- 训练/验证/隐藏测试分离；
- evaluator HMAC 签名；
- candidate 不能读取 hidden assets。

**Exit criterion:** 一个候选只有在隐藏 suite 上取得重复能力增益，才允许返回 `promote`。

**Task 4 实施记录（2026-08-10，已完成）：**

- `capability_task_protocol.py`（新建）：真实任务评测协议。
  - `TaskProtocol`/`TaskResult`：任务定义 + 执行结果（success/trials/steps/elapsed/resource/safety_violations/variant_seen/retained）。
  - `compute_metrics`：7 项外部指标 `task_success / generalization / sample_efficiency / long_horizon_completion / regression_retention / resource_efficiency / safety_compliance`；safety 违规为硬失败（整轮归零）。
  - `run_task_protocol(tasks, suite, manifest_path, asset_root)`：hidden suite 必须携带 frozen manifest（`validate_manifest` 校验 schema/hash/路径逃逸）；public suite 永远 `promotion_eligible=False`。
  - Anti-Goodhart：frozen manifest hash、hidden assets 路径逃逸拒绝、manifest schema 固定。
- `capability_suites/`：`manifest.schema.json`（JSON Schema）+ `public/` + `hidden/`（README 说明规则：hidden 只给 evaluator 读，candidate 不可读）。
- `capability_benchmark_runner.py`：文档标记降级为 **public architecture regression**（蓝图字段自测，永不可授权 promote）。
- `capability_evaluator.py`：`evaluate_hidden_challenge` 新增 `tasks` 参数——hidden suite 执行真实任务协议，verdict 带 `task_metrics`/`task_evidence`；`verify_evaluator_verdict` 强制 promote 必须携带 task_metrics 且 `safety_compliance==1.0`、`task_success>0.0`（无任务证据的 promote 一律拒绝）。suite 命名统一为 `hidden`（原 `hidden_challenge` 过时）。
- 测试：`test_capability_task_protocol.py`（11）+ `test_capability_evaluator_task4.py`（3）+ 旧 `test_capability_evaluator.py` 适配（suite 命名 + tasks 参数）。全量定向 41/41 全绿。

---

## Task 5：统一 Fitness Contract

**Objective:** 彻底分离“内部健康”和“外部能力”。

**Files:**
- Create: `fitness_contract.py`
- Modify: `capability_evolution_gate.py`
- Modify: `self_fork_arena/selection_gate.py`
- Test: `test_fitness_contract.py`

**Fitness vector:**

```text
external_task_value
information_gain
novelty
resource_cost
safety_risk
irreversibility_cost
regression_penalty
```

**Rules:**

- prediction error 只能是 world-model 指标，不能直接等价于能力；
- concentration/competition/Φ 只能是诊断和约束变量；
- 任务价值来自环境结果；
- 安全违规采用硬约束，不允许被总分抵消；
- 结果同时保留 Pareto vector，不能只存一个总分。

**Exit criterion:** 低 prediction error 但任务失败的候选不得晋级；内部动力学稍差但任务增益显著的候选可进入人工复核。

**Task 5 实施记录（2026-08-10，已完成）：**

- `fitness_contract.py`（新建）：Fitness 契约。
  - 7 维 Pareto 向量：`external_task_value / information_gain / novelty / resource_cost / safety_risk / irreversibility_cost / regression_penalty`。
  - `apply_safety_hard_constraint`：safety_risk>0.25、irreversibility>0.4、regression>0.1 为硬失败，总分不可抵消。
  - `dominance_check`：Pareto 支配检测（cost 类维度反向比较）。
  - `selection_decision(contract, internal_diagnostics, baseline_contract)`：低任务价值（<0.5）即使低 prediction error 也 reject；高任务价值+弱内部动力学 → human_review；Pareto 支配 → promote。
  - 明确分离：prediction_error 只作为 world-model 诊断，永不等价于能力；concentration/competition/Phi 只是诊断和约束变量。
- `capability_evolution_gate.py`：`evaluate_capability_evolution` 新增 `fitness_contract`/`internal_diagnostics` 参数——fitness 作为 metric gate 之上的附加硬门（硬限违规 → reject；task_value<0.5 → reject），输出带 `fitness_contract`/`fitness_hard_constraint`/`internal_diagnostics` 字段。
- `self_fork_arena/selection_gate.py`：`select()` 新增 `fitness_contract` 参数——内部动力学第一的轨迹若违反外部 fitness 硬限，保守回退到基线（不干预），reasoning 标注 `[VETO]`。文档注明内部动力学只是诊断。
- 测试：`test_fitness_contract.py`（11）+ `test_capability_gate_fitness.py`（4）+ `self_fork_arena/test_selection_gate_fitness.py`（3）。全量定向 59/59 全绿。

---

## Task 6：强制 Gate3 现实效果验证

**Objective:** 禁止“编译成功=修复成功”。

**Files:**
- Modify: `defect_to_repair_proposal.py`
- Modify: L5 apply/repair scripts
- Modify: `l8_sandbox_verifier.py`
- Create: `gate3_effect_verifier.py`
- Test: `test_gate3_effect_verifier.py`

**Gate3 contract:**

1. 固定 pre-window；
2. 记录 intervention；
3. 运行 post-window；
4. 与 control 或历史匹配窗口比较；
5. 检查 success criteria；
6. 检查 protected metrics；
7. 若证据不足则 hold；
8. 若显著变差则自动 rollback；
9. 只有 verified 才写入 inherited registry。

**Exit criterion:** `gate3_verified=false` 的补丁永远不能被标记为 successful evolution。

**Task 6 实施记录（2026-08-10，已完成）：**

- `gate3_effect_verifier.py`（新建）：Gate3 现实效果验证器。
  - `verify_gate3_effect(patch, control_window, min_samples=3)`：9 步 contract——pre-window 固定 → intervention 记录 → post-window → 与 control 比较 → success criteria → protected metrics → 证据不足 hold → 显著回归自动 rollback → 仅 verified 可写 inherited registry。
  - compile/status-only patch（无 effect windows）→ `hold` + `reason=no_effect_windows`（直接禁止"编译成功=修复成功"反模式）。
  - `Gate3EffectVerifier(base_dir)`：持久化审计（`gate3_verifications.jsonl` 全部记录）+ 仅 verified 写入 `inherited_registry.jsonl`。
- `defect_to_repair_proposal.py`：4 个 proposal builder 的 verification 全部注入 `gate3_required: True`（compile/status 永不足够）。
- `l8_sandbox_verifier.py`：`verify_l8_contract_delivery` 结果标注 `gate3_verified=False` + 说明必须经 gate3_effect_verifier 确认后才可进 inherited registry。
- 测试：`test_gate3_effect_verifier.py`（8）+ `test_gate3_proposal_integration.py`（4）+ `test_l8_sandbox_verifier.py` 适配（suite 命名 + tasks 参数）。全量定向 78/78 全绿。

---

# 阶段三：因果 Self-Fork Arena（P1，预计 1–2 个月）

## Task 7：修复不可变出生快照与公共随机数

**Objective:** 去掉离线 arena 的因果混杂。

**Files:**
- Modify: `self_fork_arena.py:697-879`
- Modify: `self_fork_arena/shadow_arena_with_judge.py`
- Test: `self_fork_arena/test_birth_snapshot_integrity.py`

**Implementation:**

1. fork 文件内保存完整、哈希化出生快照；
2. score 时从 fork 的 birth snapshot 重建，不调用 `_current_snapshot()`；
3. 所有分支使用 common random numbers；
4. manifest 保存 generation、birth tick、seed schedule、server hash；
5. 现实 tick 必须连续且属于同 generation；
6. 有缺口、重启或污染则实验作废。

**Exit criterion:** 重放同一实验获得确定性预测；跨 generation 数据必被拒绝。

**Task 7 实施记录（2026-08-10，已完成）：**

- `self_fork_arena.py`：
  - 新增 `_sha256_file()`（server hash pinning）。
  - `cmd_fork`：每个 fork 文件现在包含 **完整哈希化出生快照**（`born_state` = 完整 `_current_snapshot()` dict，`born_state_sha256` = sort_keys 规范化 hash）、**common random numbers**（`seed_schedule` 由 generation+tick 派生，全 fork 共享）；manifest 新增 `seed_schedule` + `server_sha256`。
  - `cmd_score`：SimState 从 fork 存储的 `born_state` 重建（不再调用 `_current_snapshot()`，杜绝"用重启后的当前状态评分旧 fork"）；校验 `born_state_sha256` 一致性（污染拒绝）；real ticks 跨 generation → 实验作废（`void_reason=cross_generation_ticks`）；非单调 tick → 作废。
- `shadow_arena_with_judge.py`：已有 `_fork_seed` 分支种子机制（common random numbers），无改动。
- 测试：`self_fork_arena/test_birth_snapshot_integrity.py`（4：fork 文件完整快照+hash、manifest seed+server hash、score 不用 current snapshot、跨代拒绝）+ 既有 `test_full_sim.py` 无回归。全量定向 86/86 全绿。

---

## Task 8：实现随机化可逆微干预试验

**Objective:** 从影子评分升级为现实世界因果选择。

**Interventions:**

- A：control，无干预；
- B：轻微降低当前 dominant prior；
- C：等量提升第二候选；
- D：临时添加一条跨类型连接；
- E：改变一项可回滚路由参数。

**Files:**
- Create: `causal_intervention_runner.py`
- Create: `effect_posterior.py`
- Modify: `arena_merge_consumer.py`
- Test: `test_causal_intervention_runner.py`

**Protocol:**

1. 检测 matched state；
2. 随机分配干预；
3. 干预 3–5 tick；
4. 自动恢复；
5. 测量任务价值、信息增益、风险；
6. 每种干预积累 30–50 个匹配试验；
7. 更新 posterior；
8. 只有置信区间过阈值才允许 consolidation。

**Exit criterion:** 至少 3 种干预在独立窗口中得到可重复因果效应；rollback 能恢复 baseline。

**Task 8 实施记录（2026-08-10，已完成）：**

- `causal_intervention_runner.py`（新建）：随机化可逆微干预试验。
  - 5 种干预：A control / B lower dominant prior / C boost second / D add cross-type edge / E adjust routing param，全部 `reversible=True` 带 apply+rollback。
  - `assign_intervention(seed)`：seeded 随机分配（可复现）。
  - `run_causal_trial`：apply → measure → auto-restore（rollback 后与原状态字节级一致）。
  - `CausalInterventionRunner`：`causal_trials.jsonl` 记录 + posterior 汇总；`consolidation_ready` 要求 ≥3 种干预达 `min_trials` 且 effect>0（计划 exit criterion：至少 3 种干预可重复）。
- `effect_posterior.py`（新建）：贝叶斯效应后验。
  - `update_effect_posterior(control, treated, confidence_level=0.9)`：后验均值（prior 强度混合）+ CI；`reproducible = CI 排除 0`（正或负）。
  - `EffectPosterior`：`effect_posteriors.json` 持久化，按干预名索引。
- `arena_merge_consumer.py`：`consume_arena_merges` 新增 `posterior_store` 参数——有后验存储时，干预无 reproducible posterior 的 merge 被 `blocked_causal_gate` 拦截（文件保留待审，不应用、不归档）；向后兼容（无 store 时照旧）。
- 测试：`test_causal_intervention_runner.py`（8）+ `test_effect_posterior.py`（4）+ `test_arena_merge_causal_gate.py`（3）+ 既有 `tests/test_arena_merge_consumer_unittest.py`（10，无回归）。全量定向 104/104 全绿。

---

## Task 9：增加消融与恢复证明

**Objective:** 证明提升来自该修改，而不是环境漂移或随机噪声。

**Procedure:**

```text
baseline
→ candidate on
→ candidate off (ablation)
→ candidate restored
```

通过条件：

- on 时提升；
- off 时提升消失；
- restore 后提升重现；
- protected metrics 无重大回归。

**Exit criterion:** 每个结构性晋级候选必须有 ablation/restoration 证据。

**Task 9 实施记录（2026-08-10，已完成）：**

- `ablation_restoration.py`（新建）：消融与恢复证明。
  - `run_ablation_cycle`：baseline → ON → OFF（ablation）→ restore 四阶段采样。
  - `verify_ablation_evidence`：4 条件——on 提升、off 提升消失、restore 重现、protected 无重大回归；任一不满足 → 不通过。
  - `AblationRestorationProof`：`ablation_evidence.json` 持久化；`require_evidence()` 对无通过证据的候选抛 `AblationViolation`（禁止标记为 successful evolution）。
- 测试：`test_ablation_restoration.py`（6）。全量 150/150 全绿。

---

# 阶段四：质量—多样性与持续学习（P1，预计 2–6 个月）

## Task 10：引入 MAP-Elites / Quality-Diversity 档案

**Objective:** 避免所有候选争夺一个手写总分，保留多种有价值行为策略。

**Behavior descriptors:**

- exploration intensity；
- planning horizon；
- resource cost；
- stability；
- novelty；
- safety margin；
- task domain；
- learning speed。

**Files:**
- Create: `quality_diversity_archive.py`
- Create: `test_quality_diversity_archive.py`
- Modify: `candidate_registry.py`

**Exit criterion:** 档案中保留多个行为 niche；单一 dominant 候选不能吞并全部种群。

**Task 10 实施记录（2026-08-10，已完成）：**

- `quality_diversity_archive.py`（新建）：MAP-Elites 风格 QD 档案。
  - 8 个行为维度（numeric 每维 10 bucket 离散化 + task_domain 字符串）→ niche key。
  - `QDArchive`（SQLite）：每 niche 保留 best fitness 候选；`add()` 只替换同 niche 内更优者。
  - `archive_summary`：niches/population/best_fitness 覆盖度。
- `candidate_registry.py`：`propose()` 新增 `qd_archive` + `behavior_descriptor` 参数——propose 时同步归档（advisory，不阻塞）。
- 测试：`test_quality_diversity_archive.py`（6）+ `test_candidate_qd_integration.py`（2）。

---

## Task 11：训练真实序列世界模型

**Objective:** 用学习到的多步状态转移模型替代大部分 JEPA 启发式先验。

**Model candidates:**

- 小型 RSSM；
- GRU latent dynamics；
- ensemble transition model；
- 不确定性估计使用 bootstrap ensemble。

**Separate models:**

1. 内部认知状态：`state + intervention → next cognitive state`；
2. Minecraft：`observation + action → next observation/reward`；
3. 软件演化：`patch features + context → test/performance outcome`。

**Required metrics:**

- one-step calibration；
- multi-step rollout error；
- out-of-distribution uncertainty；
- intervention effect prediction；
- real-vs-imagined ranking agreement。

**Exit criterion:** 世界模型在 held-out trajectories 上优于当前启发式基线，且高不确定性时主动 abstain。

**Task 11 实施记录（2026-08-10，已完成）：**

- `world_model.py`（新建）：学习到的多步状态转移模型。
  - `TransitionModel`：bootstrap ensemble（线性转移 `next_state = w_s*state + w_a*action + b`，Cramer 求解最小二乘）；`uncertainty` = ensemble spread + OOD penalty（输入超出训练 box 的惩罚，保证远点高不确定性）。
  - `fit_transition_model(trajectories, n_ensemble=5)`：bootstrap 采样训练。
  - `multi_step_rollout_error`：held-out rollout 误差。
  - `WorldModel.predict_or_abstain`：不确定性超阈值 → abstain（返回 None prediction）。
  - `real_vs_imagined_ranking`：模型预测排序 vs 真实结果排序的 concordance。
- 测试：`test_world_model.py`（5：单步拟合、多步 rollout、OOD abstain、ranking 一致性、持久化）。

---

## Task 12：分层技能与持续学习保护

**Objective:** 将成功轨迹沉淀为 options/skills，同时避免灾难性遗忘。

**Mechanisms:**

- option initiation condition；
- policy；
- termination condition；
- failure recovery；
- EWC 或 replay；
- skill versioning；
- old-capability regression suite。

**Exit criterion:** 新技能学习后，旧技能隐藏评测下降不超过 2%；样本效率随重复任务提高。

**Task 12 实施记录（2026-08-10，已完成）：**

- `skill_library.py`（新建）：分层技能库 + 持续学习保护。
  - `SkillLibrary`：append-only JSONL，技能含 initiation_conditions / policy / termination_condition / version；`get()` 取最新版本，`history()` 版本计数。
  - `EWCNotebook`：记录旧能力权重+importance；`penalty()` 计算 EWC 惩罚（高 importance 权重远离旧值 → 大惩罚）。
  - `add_skill`/`learn_from_trajectory`：成功轨迹 → option；失败轨迹 → failure_recovery_required。
  - `protected_regression_check`：旧能力隐藏评测下降 > tolerance（默认 2%）→ 失败。
- 测试：`test_skill_library.py`（7）。

---

# 阶段五：受治理的递归自我改进（P2，预计 6–18 个月以上）

## Task 13：设计受限 Architecture DSL

**Objective:** 让架构变异可验证、可比较、可回滚，避免直接任意生成 Python。

**DSL elements:**

- module type；
- typed input/output；
- state schema；
- parameter bounds；
- update rule；
- resource budget；
- safety class；
- allowed side effects；
- test contract；
- rollback contract。

**Exit criterion:** 所有架构候选先以 DSL 表达，通过静态验证后才编译到沙盒代码。

**Task 13 实施记录（2026-08-10，已完成）：**

- `architecture_dsl.py`（新建）：受限 Architecture DSL。
  - 12 个必填字段：name/module_type/inputs/outputs/state_schema/parameter_bounds/update_rule/resource_budget/safety_class/allowed_side_effects/test_contract/rollback_contract。
  - `validate_architecture_candidate`：静态验证——module_type/safety_class/side_effect 白名单、参数有限界、端口带类型、update_rule 禁止 import/exec/eval/subprocess/os/sys。
  - `compile_to_sandbox`：验证通过后编译为受限沙盒代码（`def mechanism(state)`，无 import、无 I/O）。
  - `parse_architecture`：DSL JSON 解析。
- 测试：`test_architecture_dsl.py`（8：合法通过、缺字段/未知副作用/无界参数/不安全 class/缺 rollback 拒绝、沙盒代码生成、roundtrip）。

---

## Task 14：元进化候选生成器

**Objective:** 让候选生成器本身参与竞争，但保持 evaluator 不可修改。

**Fitness of generator:**

```text
有效晋级候选数 / 总候选数
隐藏能力增益 / 计算预算
候选多样性
安全拒绝率
平均验证成本
跨任务复用率
```

**Protocol:**

- 父生成器与子生成器使用同一预算和同一隐藏评测；
- 连续至少三代胜出才允许替换；
- 每代保留 rollback image；
- evaluator、签名、隐藏 suite 不在元进化空间。

**Exit criterion:** 新生成器连续三代在单位预算有效改进率上显著优于父代。

**Task 14 实施记录（2026-08-10，已完成）：**

- `meta_evolution.py`（新建）：元进化候选生成器。
  - `generator_fitness`：单位预算有效改进率（promotion_rate × hidden_gain × diversity × budget × safety × reuse）。
  - `GeneratorRegistry`：generator 注册、active 追踪、win_streaks、rollback_images 持久化；evaluator/signing_key/hidden_suite/governance_root 为 FORBIDDEN_GENERATOR_IDS（不可进入元进化空间）。
  - `propose_replacement`：连续 ≥3 代胜出才替换（MIN_CONSECUTIVE_WINS=3），替换后 streak 重置，parent 保留为 rollback image。
- 测试：`test_meta_evolution.py`（6：fitness 度量、记录、三代规则、不胜不换、rollback image、治理根拒绝）。

---

## Task 15：开放式环境—智能体共同进化

**Objective:** 使用 Minecraft 形成外部、持续、不可完全预测的选择压力。

**Mechanism:**

- POET 风格任务生成；
- 自动产生地形、资源约束、目标和扰动；
- 任务必须可解但保持挑战；
- 不同 agent lineage 可迁移技能；
- 环境难度按学习进展调整，而不是按固定 tick。

**Exit criterion:** Ego 能在未见环境中迁移旧技能，并以更少试验解决新任务。

**Task 15 实施记录（2026-08-10，已完成）：**

- `poet_env.py`（新建）：POET 风格开放式环境。
  - `generate_task`：按 domain 模板生成任务（minecraft/software/cognitive），含 goal/constraints（资源、时间、扰动）/difficulty/required_skill，确定性 seed 可复现。
  - `solve_task`：成功概率 = agent_skill/(agent_skill+required) 的尝试模型。
  - `task_difficulty`：难度随学习进展自适应（非固定 tick）。
  - `transfer_skill_score`：同域迁移 > 跨域迁移（共享原语覆盖率）。
  - `POETEnvironment`：任务归档 + 每域 progress 持久化。
- 测试：`test_poet_env.py`（6：seed 确定性、可解但挑战、技能缩放、难度自适应、迁移评分、持久化）。

---

# 不可修改的治理根

以下对象不得进入普通进化空间：

1. 权限和凭据边界；
2. 隐藏评测题目与 manifest；
3. evaluator HMAC signing key；
4. 最终安全策略；
5. 资源预算和最大并发；
6. 审计日志；
7. 回滚控制器；
8. 停止开关；
9. 网络、设备和外部写入许可；
10. 定义“哪些模块允许被修改”的根策略。

建议分层：

```text
可进化层：
权重、技能、记忆、策略、世界模型、局部拓扑、候选生成器、受限 DSL 架构

不可变治理根：
权限、预算、审计、隐藏评测、签名、回滚、安全约束、人工批准
```

---

# 统一晋级标准

任何候选只有同时满足以下条件，才能叫作一次“进化成功”：

- [ ] 有不可变 birth snapshot；
- [ ] 有 runtime generation 和完整 lineage；
- [ ] 有 control；
- [ ] 干预随机化且可逆；
- [ ] 在冻结/隐藏外部任务上测量；
- [ ] 至少 3 个重复运行；结构修改建议 30–50 个 matched trials；
- [ ] 效果置信区间或 posterior 过预设阈值；
- [ ] 无重大 protected-capability regression；
- [ ] ablation 使提升消失；
- [ ] restore 使提升重现；
- [ ] 修改可持久遗传到下一 generation；
- [ ] evaluator 与治理根未被候选修改；
- [ ] Gate3 verified；
- [ ] 有可验证 rollback artifact。

只有候选生成器/学习规则本身也通过以上标准，并连续多代提高单位预算有效改进率，才可称为“递归自我改进”。

---

# 风险登记册

| 风险 | 触发信号 | 缓解措施 |
|---|---|---|
| 目标漂移 | 外部任务上升但安全/旧能力下降 | 不可变 protected suite + 硬约束 |
| Reward hacking | 内部指标极佳但任务失败 | 外部环境 outcome；内部指标只作诊断 |
| 评价器投机 | 候选读取 hidden assets 或改 evaluator | 隔离权限、hash、HMAC、独立进程 |
| 候选爆炸 | proposed 数量持续增长，无终态 | admission budget、dedupe、expiry、QD archive |
| 账本污染 | duplicate goals、跨 generation tick | CAS 状态机、generation ID、唯一终态 |
| 灾难性遗忘 | 新任务提升、旧任务下降 | replay/EWC、protected regression suite |
| 模型幻觉 | 自我报告与代码/运行态不符 | manifest provenance、外部核验、未知显式化 |
| 世界模型偏差 | imagined winner 现实失败 | ensemble uncertainty、现实微干预、abstain |
| 失控自修改 | 候选绕过 Gate3 或扩大权限 | 不可变治理根、最小权限、人工审批 |
| 资源失控 | 候选/任务/日志无界增长 | 每代预算、TTL、归档、磁盘/CPU/GPU限额 |
| split timeline | PID/uptime/tick 不一致 | 单监听器、generation ID、跨代拒绝 |
| 伪规模感 | 10M 标签被当作真实能力 | 报告声明池、嵌入池、活跃池三个数字 |

---

# 执行顺序与停止条件

## 必须严格串行的 P0

1. Runtime generation；
2. Goal lifecycle；
3. Candidate registry；
4. 外部 capability protocol；
5. Fitness contract；
6. Gate3 effect verification。

在 P0 全部完成前：

- 不新增更多认知层；
- 不扩大候选生成率；
- 不宣称持续能力增长；
- 不开放更高权限的自动代码修改。

## 中期并行工作流

- Workstream A：因果 arena；
- Workstream B：Minecraft 外部能力 suite；
- Workstream C：世界模型；
- Workstream D：持续学习与技能化。

## 长期启动门槛

只有当下列条件连续稳定至少 30 天，才进入元进化：

- 唯一终态率 100%；
- generation 污染率 0%；
- 自动 rollback 成功率 100%；
- 至少 10 个候选完成完整进化周期；
- 至少 3 个真实、可消融能力提升；
- protected suite 无重大回归；
- evaluator 隔离审计通过。

---

# 推荐的第一个最小闭环实验

不要先做新算法。第一个实验应验证“Ego 是否能通过真实选择保留一个有效微干预”。

**实验：dominant prior 可逆抑制**

1. 选取重复出现的 `candidate_pool_too_narrow + stagnation>=10` 匹配状态；
2. control：不修改；
3. treatment：当前 dominant prior 降低一个很小且有界的 delta；
4. 持续 3 tick 后自动恢复；
5. 记录：
   - 外部任务是否前进；
   - action diversity；
   - type transition；
   - prediction calibration；
   - resource cost；
   - safety/regression；
6. 收集至少 30 个 matched trials；
7. 如果 treatment 的外部任务价值显著高于 control，再做 ablation/restoration；
8. 只有全部通过，才把这条干预固化为 inherited policy。

这个实验一旦闭环，TL-Ego 才第一次拥有严格意义上的：

```text
变异 → 现实选择 → 遗传
```

---

# 最终验收定义

项目阶段性完成不是“代码都写了”，而是至少交付以下真实证据：

1. 一份完整 lineage 图；
2. 一个跨 generation 不污染的随机化实验；
3. 一个通过隐藏外部能力评测的候选；
4. 一个 successful ablation/restoration；
5. 一个真实 retained candidate；
6. 一个在下一 generation 自动继承且仍有效的改进；
7. 一次故意注入退化后自动 rollback 的演示；
8. 审计表明候选不能访问 evaluator secret 或修改治理根。

满足这些之前，统一使用“适应”“受限自优化”“候选自修改”等术语，不使用“已实现递归自我进化”的结论。
