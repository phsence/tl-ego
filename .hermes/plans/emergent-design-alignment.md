# TL-Ego Plan — Emergent-Garden / Emergent 设计启示

## 来源
- `https://github.com/gyanantaran/emergent-garden` — Pygame 粒子涌现模拟
- `https://github.com/Ikaruga/Emergent` — 涌现式 AI 哲学与架构宣言（源码私有）

## 目的
把外部涌现思路收敛为 TL-Ego 可对照的设计坐标，不直接搬代码，只提炼对当前架构有判断价值的原则。

## 现状对照

| 外部原则 | TL-Ego 当前状态 | 差距 / 可借鉴点 |
|---|---|---|
| 规则递增式涌现（emergent-garden） | 已有基础运动/碰撞，再叠加吸引/排斥/heat | 方向一致；但 Ego 的规则目前更偏“人工编排”，缺少从简单规则自发组织的观察窗口 |
| Multi-View Collaboration（Ikaruga） | 有动态 MoE / 竞争信号 / KWTA | 偏竞争路由；可向“协作式多视角理解”扩展，而不是单纯稀疏激活 |
| Secret Garden / 私有内部状态 | 有 identity_anchor + manifest | 已有自省层固定回答；但尚未区分“处理中但暂不外显”的状态与“最终输出” |
| Organic Growth / 按需生长 | 已有 regime 扩展、候选池过窄修复 | 已有结构变化；但触发条件仍较人工，可参考 empty-scope detection |
| Native Ternary（Ikaruga） | 无直接对应 | 目前不落地；可作为“ abstain / 不知道 ”状态的设计参考，而非立即实现 |

## 建议下一步
1. 在 TL-Ego 增加一个最小“内部状态日志”：区分 `decided / processing / abstain`，优先验证 abstain 是否能稳定出现。
2. 对现有多视角输出增加一次“合并前协作检查”：不是只保留最高分，而是先看是否存在互补视角。
3. 用一个可控实验验证“规则简化是否仍涌现目标行为”：减少当前硬编码动作约束，观察 Ego 是否仍能完成基础任务。

## 非目标
- 不引入 ternary 硬件或新数值类型
- 不重构渲染/展示层
- 不追求与 Ikaruga 的 Rust 实现对齐

## 验证方式
- 实验 1：内部状态日志 + abstain 出现率
- 实验 2：协作视角合并 vs 当前竞争保留的行为对比
- 实验 3：简化规则下的任务完成率/决策质量

---
Created: 2026-08-09
Status: pending review
