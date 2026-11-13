# 09-14 双系统修复 fast_path_dual_system

## 设计裁决（用户原话）
"S2回答了94.6%的查询，这个设计不对，应该S1和S2都有，这才是设计的根本。"
双系统 = 每个认知动作 S1、S2 都要说话 + 仲裁器裁决。LLM 快速 ≠ 免除神经侧发言。

## 已落地的修复（tl_ego_server.py `_bg_run_contemplate`，约 L93-148）
P5 快路径返回后、full_pipeline 判定前，插入：
- S1 = `RESTING_STATE.last_neural_snapshot` 的 dominant/concentration/competition/entropy（零计算，快照）
- S2 = `llm_classify(query[:4000])` 一次调用（~2s）
- 真仲裁：`_classify_s1_s2_alignment` → aggregator（含 `arbiter_route: fast_path_dual_system`, `source_s1: resting_snapshot`）
- verdict 只有 agree/disagree 两档（快路径不重算 Hebbian 权重）

## 豁免（有意保留）
- **NeuralDirect**：神经基底直答，本无 LLM 主张可仲裁——设计如此
- **MC 决策 prompt**（query lstrip 后以 `Ego:(` 开头）：答案是身体运动 JSON 不是认知立场；仲裁它会重新引入 v4 刚修掉的 15 分钟级管线

## 验证（09-14 实测，UAC 重启后新 generation ego-202609141320089061430800-20064）
- 门控自检 6/6 PASS（tool_call 才仲裁 / neural_direct 跳过 / Ego:( 前导空白也识别）
- agree 案例：S1=integrator S2=integrator agree=True axis=same_type
- disagree 案例：行动规划题 S1=integrator S2=action agree=False axis=role_conflict
- 重启前同题返回 agg_route=None（旧代码）——改 server.py 必须重启才生效（stale-code 陷阱第三次踩）

## 已知边界
- S1 用快照不是即时 KWTA：tick 掉速期快照可能很旧（当日 25-45min/tick）。缓解条件是先修内部吞吐（审计报告优先级2）
- confidence 公式 0.5+0.5*agree 是占位标定，非 Hebbian 全权重；要精细就用 full_pipeline
- 8770 shim 也 import server 模块，等它被 watchdog 重启后才同样生效
