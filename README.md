# 🦞 TL-Ego — 十层神经符号自我（L0–L10）

> 一个跑在 GPU 吸引子网络上的持续学习智能体：100K 神经元经 STDP 无监督演化出 System 1 直觉，LLM 承担 System 2 推理，五重内在驱动（好奇/胜任/新奇/自主/联结）提供自主动机，L5–L10 三层闸门让它在运营中**自己改自己**——每次改动带备份、审计、行为验收，失败自动回滚。

- **服务端**：`tl_ego_server.py`（30,897 行，stdlib `http.server`，`127.0.0.1:8766`）
- **许可证**：MIT（见 [LICENSE](LICENSE)）

## 目录

- [系统架构](#系统架构)
- [一次沉思的生命周期](#一次沉思的生命周期)
- [快速开始](#快速开始)
- [API 端点](#api-端点)
- [自进化闭环](#自进化闭环)
- [身体与桥接](#身体与桥接)
- [模块地图](#模块地图)
- [数据与运行时](#数据与运行时)
- [涌现网络资产](#涌现网络资产四轮验真确认)
- [仓库边界](#仓库边界)

## 系统架构

单进程十层，全部挂在 `tl_ego_server.py` 的 tick 循环上：

| 层 | 名称 | 职责 |
|----|------|------|
| L0 | 吸引子网络 | 100K 神经元 KWTA 稀疏竞争，System 1 直觉（dominant type + convergence） |
| L1 | STDP 可塑性 | 对话级 LTP/LTD，增量经 `/save-checkpoint` 合并回 SQLite 主图 |
| L2 | 内在驱动 | EFE 推导的五驱动：curiosity / competence / novelty / autonomy / relatedness |
| L3 | 因果自传 | 反事实归因，回答"我为什么这么做" |
| L4 | S2 内化 | teacher 判定 → 行为改变的闭环 |
| L5 | 自主修复 | 三闸门自修改（见[自进化闭环](#自进化闭环)） |
| L6 | JEPA 世界模型 | 预测外部结果 / 可供性排名 / 观测新奇度 |
| L7 | 元演化 | 价值对齐 gate + 使命规划 + 自修改提案 |
| L8 | 认知实验室 | review-only：只出提案，不自动应用 |
| L9 | 自主性监控 | 自主行为审计 |
| L10 | 元算法生成 | DSL 编译候选算法 + 家族评审 + 参数映射 |

横切模块：身份锚（identity drift 锁）、价值检查、Hebbian 聚合器（涌现判定，取代规则裁决）、静息态振荡（v3.4 多模态噪声 + 退火）、睡眠重放（P4）、脉冲编解码。

```
   query ──► tl_ego_server.py :8766 ──┬──► ego_openai_shim.py :8770   (OpenAI 兼容 API)
                                      ├──► ego_memory_api.py  :8772   (记忆查询 API)
                                      ├──► ego_mc_body_bridge / ego_sc2_body_bridge  (Minecraft / StarCraft II 身体)
                                      └──► causal_lens_bridge / memory_bus_bridge    (因果引擎 / 共享记忆总线)
```

## 一次沉思的生命周期

`POST /contemplate` 的全链路（单次调用内完成）：

```
embed (BGE-M3 :8002)
  → L0 吸引子 KWTA 竞争（multi-trial）
  → L2 五驱动状态注入
  → L3 因果上下文（causal_lens_bridge）
  → L6 JEPA 预期结果
  → Hebbian 聚合器 → verdict（AGREED / DISAGREED / …）
  → L1 STDP 在线更新
```

`/metacontemplate` 在此之上做递归元沉思：每轮结果作为下一轮的元查询输入，连续两轮 verdict+S1+S2 全匹配自动停止，输出 `rounds[]` + `meta_insight`（认知稳定性评分）。

`/contemplate-job` 为异步版（RA 桥接等高频调用方用，job store 有 TTL 驱逐 + 洪水防护）。

## 快速开始

```bat
:: Windows 一键启动（配置读 ego.env，LLM 密钥读 ego_key.txt——两者都不入库）
run_tlego_service.bat

:: 或直接裸启（配置从 ego.env fallback 读取，真实环境变量优先）
python tl_ego_server.py
```

```bash
# 沉思
curl -X POST http://localhost:8766/contemplate \
  -H 'Content-Type: application/json' \
  -d '{"query": "这个问题我有把握回答吗？"}'

# 运行状态
curl http://localhost:8766/status

# OpenAI 兼容调用（shim 侧车，:8770）
curl -X POST http://localhost:8770/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "tl-ego", "messages": [{"role": "user", "content": "在干嘛"}]}'
```

`/contemplate` 请求体：`query`（必填）、`history_summary`（可选，喂给 GAC 增强）、`confidence_fallback`。
返回体含 `dominant_type`、`type_concentration`、`competition_level`、`confidence`、`llm_verdict`、`final_verdict.signal`。

## API 端点

约 45 个，按职能分六组：

| 组 | 端点 |
|----|------|
| 沉思 | `/contemplate` `/contemplate-job` `/metacontemplate` `/save-checkpoint` `/reset-stdp` |
| 自我 | `/know-yourself` `/self-model` `/bdi-snapshot` `/intention` `/directive` `/drive` `/drives` |
| 自进化 | `/l7-status` `/l7-evolve` `/l7-proposals` `/l7-evolution-report` `/l8` `/l10` `/apply-parameter` `/approve` |
| 感知/行动 | `/inject-sensory` `/action-gateway` `/feedback` `/noise` `/resting` `/resting-log` |
| 记忆 | `/long-term-memories` `/rebuild-faiss` `/autobiography/timeline` `/autobiography/why` |
| 实验 | `/causal-arm` `/quantum-experiment` `/reasoning_neurons` `/find-seeds` `/web-search` `/scan-workspace` `/run-governance` `/run-many` `/debug-stacks` |

## 自进化闭环

本系统的核心特征：ego 在运营中观察自己的失败，自己提出改动，过三道闸门后应用，再用行为验收验证。

```
ego_self_proposal.py ──► 读取 .lae/benchmarks/ 独立判分报告 + .lae/selector_decisions/ 上轮判决
        │
        ▼
   失败摘要 + 白名单参数表 + 已试候选（防重复）
        │
        ▼
   /contemplate 问 ego ──► repair.json 提案（纯 ASCII，一 goal 一 file，消费后不重写）
        │
   ┌────┴────┐
   │ Gate 1  │ 审查（operator gate，不因本次改动放宽）
   │ Gate 2  │ 备份 + apply + 审计日志（.lae/）
   │ Gate 3  │ 行为验收：15-tick 观测，失败自动回滚
   └────┴────┘
        │
        ▼
   L10 元算法生成：DSL 编译 → 家族评审（l10_family_judge）→ 参数映射进 L5
```

- **键白名单 + 范围钳制**：ego 只能改授权参数，越界即拒；`runtime_identity` 记录每代 boot_ts 验证进程真伪
- **独立判分**：能力基准由外部验证器打分，不是 ego 自述（见 `capability_suites/`：`public/` 可读，`hidden/` 由治理根控制、SHA-256 清单钉死，候选永远拿不到资产内容）
- **闭环驱动**：`ego_evolution_loop.py`（`--check` 干跑 / `--loop` 常驻自转）

## 身体与桥接

| 身体/通道 | 模块 | 说明 |
|-----------|------|------|
| Minecraft | `ego_minecraft_bridge.py` `ego_mc_body_bridge.py` | mineflayer 假玩家，世界聊天（T 键）对话 |
| StarCraft II | `ego_sc2_body_bridge.py` | 指令落 `cmd.jsonl`，桥接执行 retail 客户端 |
| GMod | `ego_gmod_body` | 源引擎身体 |
| OpenAI 兼容 | `ego_openai_shim.py` (:8770) | 统一 ToolRouter：ego 内部 L10 / 外部客户端 / 演化管线同一执行路径 |
| 记忆 API | `ego_memory_api.py` (:8772) | 长期记忆查询 |
| 语义记忆 | hy-memory 侧车 (:19527) | HTTP API 向量记忆 |
| 知识图谱 | graph-memory (SQLite) | 关系型记忆 |
| 因果引擎 | causal-lens 侧车 | 反事实/归因，`causal_lens_bridge.py` 对接 |

工具调用走单一 `ToolRouter.execute()`，每次调用带 source 归属（internal_l10 / external_openai / …）进同一份记忆日志——ego 能自省"这是我做的还是用户让我做的"。

## 模块地图

93 个顶层 `.py`（另有约 70 个一次性脚本 `_*`/`fix_*`/`probe_*` 等，属于开发过程产物）。核心模块按角色：

**神经引擎**
`neural_direct.py`（NeuralDirect gate）`snn_layer.py`（LIF + STDP）`fractal_attractor.py` `criticality_regulator.py`（临界性稳态）`edge_store_v2.py` `edge_sparsifier.py` `pulse_codec.py` `calibrate_snn.py` `fno_train.py`（FNO 噪声适配）

**认知层**
`logic_layer.py` `agent_loop.py` `architect_framework.py` `continuity.py` `identity_anchor.py` `value_check.py` `self_model_manifest.py` `l0_meta_observer.py` `need_synthesizer.py` `proprioceptive_loop.py`

**演化与治理**
`ego_self_proposal.py` `ego_evolution_loop.py` `ego_growth_daily.py` `evolution_trigger_policy.py` `defect_to_repair_proposal.py` `external_candidate_selector.py` `candidate_diversity_engine.py` `fitness_contract.py` `l10_*.py` `l7_bridge.py` `l7_init.py` `l8_cognitive_lab.py` `l8_governance.py` `l8_sandbox_verifier.py` `l9_autonomy_monitor.py`

**策略钳制**
`runtime_governor.py` `policy_runtime.py` `runtime_policy_loader.py` `min_dwell_policy.py` `runtime_identity.py` `arch_auto_approve.py`

**记忆**
`memory_bridge.py`（hy-memory + graph-memory）`memory_bus_bridge.py` `lesson_consolidate.py` `predictive_plasticity.py`

**世界模型与感知**
`ego_jepa.py`（可供性排名 / 外部预测）`jepa_residual_attributor.py` `causal_lens_bridge.py` `counterfactual_generator.py` `stage1_consequence_sensor.py`

**驱动与动机**
`stage2_homeostatic_drives.py` `stage3_causal_autobiography.py` `stage4_s2_internalization.py` `commander_goal.py` `commander_outbox_watcher.py`

**工具与执行**
`tools_registry.py` `tool_router.py` `action_tools.py` `executor_registry.py` `stepfun_tool_caller.py` `ego_llm_gate.py`（跨进程 API 闸门，防 429）

**子目录**

```
l10/          元算法生成（DSL 编译器 / 候选草案 / 家族评审 / L5 补丁桥）
tests/        验收测试（闭环节点、多样性 gate、JEPA 预测、参数保持…）
scripts/      运维（ego_keepalive.ps1）
docs/         协议文档（commander 触发协议 / OpenAI API）
capability_suites/   能力基准（public / minecraft / hidden）
self_fork_arena/     影子竞技场（候选算法对战评审）
audits/       演化审计记录
```

> 注：`l7/` 子目录只剩历史 `.pyc` 与备份，L7 源码在顶层（`l7_bridge.py` / `l7_init.py`）。

## 数据与运行时

| 组件 | 事实 |
|------|------|
| 嵌入 | BGE-M3 本地服务 `:8002`，1024 维 |
| 向量索引 | FAISS（`/rebuild-faiss` 重建） |
| 图存储 | SQLite：STDP 增量 → `/save-checkpoint` 合并回主图 |
| GPU | 吸引子网络 CUDA KWTA（`EGO_GPU_KWTA=1`）+ GPU SNN 热池（`EGO_GPU_SNN=1`），任何错误自动 CPU 回退 |
| 可塑性 | `EGO_LTP_GAIN=4.0`（LTP/LTD 平衡，0 = 恢复旧 0.95 偏遗忘增益）；`EGO_STDP_TAU_S` 跨 tick STDP 时钟 |
| LLM 网关 | `ego_llm_gate.py` 跨进程信号量（stepfun key 曾被三进程共抢打满） |
| Python | 3.12（unsloth_env），PyTorch |

单实例锁 + `runtime_identity` 防双开；job store 有 TTL 防内存泄漏；contemplate 有 15s 洪水防护。

## 涌现网络资产（四轮验真确认）

- **网络**：100K 神经元，11,170 轮 STDP+CHL 演化，5.6M 条边
- **功能分化**：Sensory 39.4% → Integrator 50.1% → Decider 13.3% → Action 5.3%
- **递归吸引子**：15 个双向强对，跨类型互锁
- **自维持动力学**：KWTA=50 稀疏竞争 30 轮不灭
- **全链路**：S→I→D→A 7/7 PASS，Action 层激活最高（0.0051）
- **剪枝骨架**：0.15+ 阈值 → 8,472 条强边，99.3% 互锁对
- **三角基序**：全图 455 个三角，竞争路径 8–9 路分流
- **工作记忆**：0.12+ 子图 3/3 PASS（激活自维持 + 多模式共存 + 新模式覆盖）

论文方向：*Emergent Dual-Process in STDP-Evolved Neural Fields*——无监督演化出的吸引子直接充当 System 1 直觉。

## 仓库边界

以下本地数据**有意不入库**（`.gitignore` 已排除）：

- `ego_key.txt` / `ego.env` — LLM 密钥与运行时配置
- `memory/` — ego 的情景记忆笔记
- `capability_suites/hidden/` — 隐藏能力测试集（防基准污染）
- `logs/` `data/` `checkpoints/` `ego_sandbox/` `pack-test/` `videos/` `avatar/` — 运行时产物
- `*.bak*` `*_bak-*` `_tmp_*` 等 — 开发过程备份

需要复现运行时，按[快速开始](#快速开始)配置 `ego.env` 与 `ego_key.txt` 即可。
