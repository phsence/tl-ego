# 🦞 TL-Ego: Triple-Loop Ego Architecture

## 一句话
STDP 涌现网络的 System 1 直觉 + LLM System 2 推理 + 在线持续学习 = 一个不断成长的三环自我。

## 三环结构（全在一个服务端流水线）

```
┌───────────────────────────────────────┐
│  tl_ego_server.py  /contemplate       │
│                                       │
│  Step 1  Embedding → BGE-M3 8002     │
│  Step 2  System 1: attractor KWTA     │
│          (multi_trial_kwta)           │
│  Step 3  GAC 知识注入 (optional)      │
│  Step 4  System 2: LLM classify       │
│          (llm_classify → 8001)        │
│  Step 5  Aggregator → verdict         │
│  Step 6  STDP 在线可塑性             │
│          (apply_stdp)                 │
│         /save-checkpoint 持久化       │
└───────────────────────────────────────┘
```

全链路单次调用完成：

```
embed → S1 attractor (multi_trial_kwta) → GAC → S2 LLM (llm_classify)
→ Aggregator (verdict) → STDP (apply_stdp) → save_checkpoint
```

### 三层详解

| 层 | 角色 | 实现函数 | 输入→输出 |
|----|------|---------|----------|
| **Layer C: 沉思** | Basin 竞争观察 | `multi_trial_kwta()`, `run_attractor_kwta()` | query embedding → dominant neuron type + convergence score |
| **Layer B: 双系统** | S1 直觉 + S2 LLM | `llm_classify()`, Aggregator (Step 3-4) | S1 signal + GAC hint → LLM classification → merged verdict |
| **Layer A: 在线可塑性** | 对话级 STDP | `apply_stdp()`, `/save-checkpoint` | KWTA activation history → LTP/LTD delta → SQLite persist |

## 服务端 API

| 端点 | 方法 | 功能 |
|------|------|------|
| `/contemplate` | POST | 全链路沉思（embed → S1 → GAC → S2 → Aggregator → STDP） |
| `/metacontemplate` | POST | Phase 4.0 递归元沉思（深度>1自我观察，收敛自动停止） |
| `/save-checkpoint` | POST | 将累积的 STDP 增量合并回 SQLite 主图 |
| `/status` | GET | 服务器运行状态 |

### `POST /contemplate` 请求体

```json
{
  "query": "这个问题我有把握回答吗？",
  "history_summary": "可选，对话历史摘要（用于 GAC 增强）",
  "confidence_fallback": 0.5
}
```

### 返回体

```json
{
  "query": "...",
  "dominant_type": "decider",
  "type_concentration": 0.47,
  "competition_level": 0.34,
  "confidence": 0.65,
  "speed": "medium",
  "llm_verdict": {
    "classification": "high_confidence_agree",
    "confidence": 0.85,
    "reasoning": "..."
  },
  "final_verdict": {
    "classification": "high_confidence_agree",
    "signal": "AGREED"
  }
}
```

### `POST /metacontemplate` Phase 4.0

```json
{
  "query": "为什么我刚才选择Phase 4.0而不是停下来？",
  "depth": 3
}
```

递归自我观察：每轮结果作为下一轮的元查询输入。连续两轮 verdict+S1+S2 全匹配时自动停止。

返回体包含 `rounds[]` (每轮 full_pipeline) + `meta_insight` (裁决/S1/S2 演化轨迹、收敛检测、认知稳定性评分)。

## 记忆管理（分层记忆防漂移）

| 层 | 范围 | 更新机制 | 持久化 |
|----|------|----------|--------|
| 快变层 | Session | 交互时 STDP | 对话结束快照 |
| 慢变层 | 天/周 | 睡眠期 CHL 批量 | 增量融合进基底 |
| 冻结层 | 基准 | 当前 11,170 轮 0.12+ | 沙盒基准线 |

## 涌现网络核心资产（四轮验真确认）

- **网络大小**: 100K 神经元, 11,170 轮 STDP+CHL 演化, 5.6M 条边
- **功能分化**: Sensory 39.4% → Integrator 50.1% → Decider 13.3% → Action 5.3%
- **递归吸引子**: 15 个双向强对, 跨类型互锁
- **自维持动力学**: KWTA=50 稀疏竞争 30 轮不灭
- **全链路**: S→I→D→A 7/7 PASS, Action 层激活最高(0.0051)
- **剪枝骨架**: 0.15+ 阈值 → 8,472 条强边, 99.3% 互锁对
- **三角基序**: 全图 455 个三角, 竞争路径 8-9 路分流
- **工作记忆**: 0.12+ 子图 3/3 PASS (激活自维持 + 多模式共存 + 新模式覆盖)

## 科学贡献潜力

- "Emergent Dual-Process in STDP-Evolved Neural Fields"
- 论文方向: 无监督演化 → 用涌现吸引子做 System 1 直觉

## 文件组织

```
tl-ego/
├── README.md                   ← 项目文档
├── tl_ego_server.py            ← 三层全链路服务端（8766端口）
├── phase1_contemplation.py     ← 独立沉思模块（CLI/测试用）
├── memory_bridge.py            ← 记忆桥接
├── fno_noise_adaptor.py        ← FNO 噪声适配
├── stage*.py                   ← Titan / S2 内部阶段模块
├── test_*.py                   ← 轻量验证脚本
└── core/                       ← 核心支持模块
```

## 发布说明

这个仓库是从本地 OpenClaw 工作区整理出的源码发布版，只包含可审查的源码、测试脚本和文档。

未随仓库发布的内容包括：运行日志、SQLite 数据库、轨迹 JSONL、模型权重、缓存、检查点、个人记忆数据和本地修复备份。完整运行通常还需要本地 embedding/LLM/GAC 服务以及对应的私有数据资产。
