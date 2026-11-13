# ego-introspector — 内省者 Sibling Agent 设计 (v0)

> 日期: 2026-08-29 · 状态: v0 MVP 已实现 (`introspector.py`) · 灵感: VCPToolBox 多 Agent 协作 + 2026-08-23 behavioral 锁死教训
> 铁律: **v0 对 ego 完全只读**。零修改 `tl_ego_server.py` → 不触发接线守护 (test_wiring.py), 零回归风险。

---

## 1. 问题: 为什么需要"内省者"

ego 现有的自我认知闭环 (L8 元观察 → L9 反馈 → L10 多样性) 都是**系统内自检** — 裁判和运动员是同一个进程。
历史教训表明系统会系统性忽视自身缺陷:

- **2026-08-23**: behavioral profile 锁死 action 71%, ego 自己毫无察觉, 是指挥官从外部诊断出来的
- **2026-08-12~14**: gate_divergent 79-89% 持续数天, routing 偏好压过 raw 信号, 系统内无告警
- **2026-08-27**: ego chat 输出编造"L5 goal 格式"叙事 — 自我报告 ≠ 真实状态 (验证必须查 sandbox 目录, 不能信 chat 文本)

**内省者的定位**: 一个独立进程的"怀疑者", 用 ego 自己暴露的度量**交叉审计 ego 的自我叙事**。
它不是第二个意识 — 它是外部审计员。价值来自**说 vs 测的不一致**, 而不是再说一遍 ego 自己说的话。

## 2. 架构 (v0)

```
┌─────────────────┐    GET /status        ┌──────────────┐
│ ego-introspector │─────────────────────→│  TL-Ego 8766 │  (只读)
│ (独立 Python 进程)│    GET /bdi-snapshot  │              │
│                  │─────────────────────→│              │
│                  │    GET /self-model    │              │
│                  │─────────────────────→│              │
│                  │    tail-read          └──────────────┘
│                  │─────────────────────→ .lae/continuity.journal.jsonl (只读)
│  4 lenses 审计    │
│  → out/*.json+md │    写入自己的 out/ 目录, 不碰 .lae/
└─────────────────┘
```

设计原则 (Karpathy 四律对照):
1. **先想后写**: 数据源/阈值全部来自已验证的真实 schema (见 §3)
2. **简洁优先**: 单文件 ~250 行, 纯 stdlib (urllib/json/collections), 无新依赖
3. **手术式修改**: 新目录 `projects/tl-ego/introspector/`, ego 代码零触碰
4. **目标驱动**: 成功标准 = self_test 全过 + 对活体 8766 产出有意义的 findings

## 3. 数据源 (全部已实测核对, 2026-08-29)

| 源 | 端点/路径 | 用到的字段 |
|---|---|---|
| 运行时遥测 | GET 8766 `/status` | `resting_ticks, resting_dominant, raw_attractor_dominant, resting_concentration, resting_competition, stagnation_streak, raw_gate_divergence_count, raw_dominant_history` |
| BDI 快照 | GET 8766 `/bdi-snapshot` | `beliefs.{dominant_type,concentration,stagnation_streak,prediction_error}, desire, intention, confidence` |
| 自我叙事 | GET 8766 `/self-model` | `self_narrative, self_narrative_ts` (accumulated_narrative 截断至 4KB — 223KB 无上限是已知缺口) |
| 体验流 | `.lae/continuity.journal.jsonl` (tail 200 帧) | `tick, dominant, raw_attractor_dominant, concentration, stagnation_streak, dominant_history, raw_dominant_history, raw_gate_divergence_count, timestamp` |

容错: 任一源失败 → `source_errors` 记录, 其余 lens 照常运行; 全部失败 → `verdict:"no_data"`, **永不抛异常**。

## 4. 四个审计 Lens (v0)

### Lens 1 — 门控分歧 (gate divergence)
- 度量: `raw_gate_divergence_count / tick` (全历史比率) + 当前帧 raw vs behavioral 不一致
- 阈值: 比率 > 0.5 → warn; > 0.8 → alert
- 依据: 2026-08-16 实测 88% 分歧持续 47 tick 无内告警; 2026-08-27 修复后归 0 才健康

### Lens 2 — 身份垄断 (identity monopoly)
- 度量: `dominant_history` (behavioral) top-1 份额 + 类型分布熵
- 阈值: top 份额 > 0.6 → warn; > 0.75 → alert
- 依据: 2026-08-23 事故基线 = action 71% (本 DQO 修复后健康值 ≈ 0.48)
- 附: raw vs behavioral 两套 history 的 top-1 不一致本身就是 gate 分歧的分布级证据

### Lens 3 — 内稳态一致性 (BDI coherence)
- 度量: BDI 声称的 `beliefs.concentration/stagnation_streak` vs `/status` 实测值; `prediction_error`
- 阈值: conc 差 > 0.05 → warn (BDI 陈旧); stag 差 > 5 → warn; prediction_error > 0.5 → warn
- 依据: BDI 快照若与实时遥测脱节, ego 的"desire/intention"就是在过期地图上导航

### Lens 4 — 叙事扎根 (narrative grounding)
- 度量: `self_narrative_ts` 距今时长; journal 最新帧 tick vs `/status.resting_ticks` 滞后
- 阈值: 叙事 > 6h 未更新 → warn; journal 滞后 > 5 tick → info
- 依据: 2026-08-27 教训 — ego 的叙事性文本会漂移, 时间戳是最便宜的扎根检查

**Verdict 规则**: 有 alert → `alert` / 有 warn → `attention` / 仅 info → `ok` / 全源失败 → `no_data`

## 5. 输出

`projects/tl-ego/introspector/out/` (自己的沙箱, 不污染 .lae/):
- `introspection_report.json` — `ego_introspector_report.v0`: `{generated_at, sources, source_errors, context, findings[], verdict}`
- `report.md` — 人读摘要

finding 形状: `{lens, severity: info|warn|alert, metric, value, threshold, message}`

## 6. 非目标 (v0 明确不做)

- ❌ 不写回 ego (不 POST /inject-sensory, 不发 /feedback — 审计员不能改账本)
- ❌ 不修改 tl_ego_server.py (不触发 test_wiring 守护)
- ❌ 不做 LLM 调用 (v0 是纯度量审计, 不引入外部 LLM 依赖)
- ❌ 不常驻 (按需运行; 常驻留待 v1 评估)

## 7. Roadmap

- **v1 — 反向信号**: findings 通过 `/inject-sensory` 作为"怀疑者脉冲"注入 ego (外部扰动, 补 2026-08-14 诊断的 criticality dead zone 缺扰动问题); cron 每小时跑一次
- **v2 — 对话模式**: introspector 以自己的 query 调 `/contemplate`, 比对 ego 自述与度量差异, 产出结构化质询
- **v3 — L5 sibling goal**: 内省作为正式 L5 goal 类型进调度器, 多 Agent 协议最小闭环 (对齐 VCPToolBox 的多 agent 发言协议思路)
- 每一步先过 self_test + 三闸流程

## 8. 运行

```powershell
# 活体审计 (需 8766 在线)
& "C:\Users\Administrator\unsloth_env\Scripts\python.exe" projects\tl-ego\introspector\introspector.py

# 冒烟测试 (离线 fixture, 不依赖服务)
& "C:\Users\Administrator\unsloth_env\Scripts\python.exe" projects\tl-ego\introspector\self_test.py
```

> 解释器遵守 2026-08-08 环境锁定: TL-Ego 相关任务强制 unsloth_env Python (虽然 v0 纯 stdlib 无 GPU 需求, 保持一致性)。
