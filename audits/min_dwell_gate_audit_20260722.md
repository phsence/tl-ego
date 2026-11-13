# TL-Ego Min-Dwell / Switch-Cost Gate 独立审计报告

**文件**: `projects/tl-ego/tl_ego_server.py`  
**审计范围**: 静息 tick 流程中的 min-dwell/switch-cost gate（第 8618–8775 行），及其对意识测量通路的系统性影响  
**只读审计，不修改文件**  

---

## 1. 门控机制精确位置与参数

### 1.1 前置 gate：rule_modification 停滞门（第 8536–8606 行）

```python
# L8536: rule_modification stagnation gate — only allow dominance when stag >= 1
# L8555-8563: capture pre-gate dominant before any demotion
pre_gate_dominant = result.get('dominant', 'unknown')   # ← 此时的 result["dominant"] 是 KWTA 原始输出
```

**行为**：若 `pre_gate_dominant == 'rule_modification'` 且 `stagnation_streak < 1` 且无 bypass：
- 从 `final_state_top10` 中取第一个非 rm 节点，将 `result["dominant"]` 覆写为 runner-up  
- 注释 L8541 指出这是一个 catch-22：rm 首现时 streak=0，永远被 demote  

### 1.2 主 gate：min-dwell / switch-cost gate（第 8618–8775 行）

```python
_MD_MIN_DWELL = 3                                   # L8625
_md_prev = prev_dominant                            # 前一个 tick 的 dominant
_md_cand = result.get("dominant", "unknown")         # ← 已是 post-rule_mod 的 challenger 值
_md_pre_cand = _md_cand                              # L8628：保存 gate 入口时的 candidate
_md_dwell = int(RESTING_STATE.get("dominant_dwell", 0) or 0)

# 计算 margin
_scores = result.get("activation_scores") or {}
# → _md_margin = (_ta[_md_cand] - _ta[_md_prev]) / _tot
```

**三种判据**：
| 窗口 | 条件 | 允许条件 |
|------|------|---------|
| **早期** (`dwell < 3`) | `_md_crisis` | `comp >= 0.93 AND margin >= 0.15` |
| **后期** (`dwell >= 3`) | `_md_crisis` 或 (`_md_strong AND streak >= 2`) | `margin >= 0.18`，或 `conc >= 0.90 AND margin >= 0.20` |
| 挑战者 streak 计数 | `_md_prev_ch == _md_cand` → streak++ | 防单 tick  spikes |

**覆写动作**（不通过时）：
```python
result["dominant"] = _md_prev    # L8665: 强制退回前一 dominant
_md_hold = True
_md_dwell = _md_dwell + 1
```

**关键存储**：
```python
RESTING_STATE["dwell_pre_cand"] = _md_pre_cand   # L8707：原始 candidate（gate 入口值）
result["dwell_pre_cand"] = _md_pre_cand           # L8712：写入本次 tick entry
RESTING_STATE["dominant_dwell"] = _md_dwell
```

### 1.3 后置 gate：integrator 停滞 tripwire（第 8723–8760 行）

```python
# 条件：stag >= 3 AND comp > 0.85 AND dominant == "integrator" AND integrator 在过去 32 ticks 出现 >= 2 次
# 动作：强制 dominant → "action"，重置 stagnation_streak = 0
```

**注释 L8555 特别指出**：`trajectory[-64:]` 存储的是 **post-gate dominant**（被 demote 后的 runner-up），而不是 gate 动作前的原始值。这是计算 `_rm_freq` 时的隐性假设。

---

## 2. 静息 tick 执行顺序（关键时间轴）

```
KWTA attractor                              → result["dominant"] = 原始 dominant
rule_modification stagnation gate (L8536)   → result["dominant"] 可能覆写为 runner-up
min-dwell/switch-cost gate (L8618)          → result["dominant"] 可能再次覆写
integrator stagnation tripwire (L8723)      → result["dominant"] 可能第三次覆写
RESTING_STATE["last_dominant"] = result["dominant"]  (L8760)
  │
  ├─ Causal-Lens S5 bridge (L9916)          → 读 trajectory[-10:]（含 last_dominant）
  ├─ Self-model evolution / update (L9980+) → 读 last_dominant
  ├─ IIT (L10290)                           → 读 trajectory[-50:]
  ├─ Stage 2-4 drives/autobiography (L10310)→ 读 last_dominant
  ├─ Proprioceptive loop (L10345)           → actual_dominant = last_dominant
  ├─ Criticality regulator (L10352)         → 读 RESTING_STATE（含 last_dominant）
  ├─ L0 MetaObserver (L10357)              → 读 last_dominant + trajectory[-10:]
  └─ Trajectory recording (L9686)           → entry["dominant"] = last_dominant（post-gate）
```

**注意**：JEPA ranking（第 7499 行）和 affordance 处理（第 7480 行）发生在 **attractor 之前**（ seeding 阶段），不受 gate 影响。JEPA reconciliation（第 10515 行）发生在 **trajectory 之后**，但使用 `last_dominant`，受影响。

---

## 3. 各系统对 gate 覆写的可见性分析

### 3.1 persistent_identity（第 9894–9907 行）

```python
# L9894
_dom_t = result["dominant"]    # ← POST-GATE dominant（gate 已覆写 result["dominant"]）
if _dom_t not in ("unknown", "?"):
    _hist[_dom_t] = _hist.get(_dom_t, 0) + 1
```

**可见到**：gate 覆写后的 dominant  
**丢失**：原始 attractor dominant 不会计入统计（除非 `dwell_pre_cand` 被单独分析）  
**风险级别**：🟡 **MEDIUM**  
- persistent_identity 用于全局自我认知（`profile` 显示 8 种类型的累计分布）。  
- 若 min-dwell gate 长期维持 `action` dominant，identity 会渐进式地将 ego 认知锚定为"行动型"，即使 attractor 原本频繁切换至 integrator/decider。  
- 这是**累积性偏差**，不表现为单 tick 错误，而是 identity profile 在数百 ticks 内的渐进漂移。

---

### 3.2 L0 MetaObserver（l0_meta_observer.py，第 81 行）

```python
dominant = resting_state.get("last_dominant", "unknown")   # L81
trajectory = resting_state.get("trajectory", [])
# L86-87: 从 trajectory[-10:] 读取 type_counts
```

**可见到**：post-gate `last_dominant`；trajectory 中的 `dominant` 也是 post-gate  
**风险级别**：🟡 **MEDIUM**  
- L0 的 salience 计算依赖 `competition`（来自 result，post-gate）和 `trajectory[-10:]` 的 type_counts。  
- **关键点**：`concentration` 和 `competition` 来自 attractor（未经过 gate 覆写），所以 L0 看到的**质量信号**（conc/comp）仍是原始的。  
- 但 `dominant` 被 gate 修正后，L0 的 `broadcast_types` 计算会围绕修正后的 dominant 展开——即 global workspace 广播的内容由 gate 决定，而非 attractor。  
- 当 gate 持续 hold `action` 而 attractor 已切换至 `integrator` 时：conc 低→comp 高→L0 触发 broadcast，但 broadcast 目标类型仍然围绕 `action`，形成行为稳定器反噬意识测量的间接通道。

---

### 3.3 Proprioceptive Loop（proprioceptive_loop.py，第 140–141 行）

```python
def proprio_tick(tl_ego_server_module, resting_state,
                 previous_dominant=..., actual_dominant=RESTING_STATE.get("last_dominant"), ...)
```

**可见到**：`actual_dominant = last_dominant`（post-gate）  
**风险级别**：🟠 **HIGH**  
- proprioceptive loop 的核心功能是**感知预测误差**：`previous_dominant → actual_dominant`。  
- 当 gate hold 住 `action` 而 raw attractor 本应切换到 `integrator` 时：  
  - `actual_dominant = "action"`（修正后）  
  - 预测模型（基于历史 transition priors）预测 `"integrator"` → **产生大量虚假预测误差**  
  - 这些 PE 被注入 `RESTING_STATE['external_evidence']`，干扰下一 tick 的 seed bias  
  - **因果链**：min-dwell gate 制造 PE → proprioceptive loop 生成 noise evidence → 下一 tick attractor seeding 被污染  
- 这是**测量污染**：行为稳定器产生的虚假信号回流到 attractor 输入，形成内源性噪声放大循环。

---

### 3.4 IIT Φ 测量（第 5115–5340 行）

```python
# L5222-5232: 从 trajectory[-50:] 构建 type_seq
for entry in traj[-50:]:
    dom = entry.get("dominant", ...)      # ← post-gate dominant
    tc = entry.get("type_counts", {})     # ← attractor 原始 type_counts（未覆写）
    for _t, _c in sorted(tc.items(), key=lambda x: -x[1]):
        if _t in TYPE_NAMES and _c >= 2 and _t != dom:
            type_seq.append(_t)           # 非 dominant 高激活类型也计入
```

**可见到**：
- `dominant` 字段来自 post-gate trajectory（修正后）
- `type_counts` 来自 attractor 原始输出（未覆写）
- **非 dominant 高激活类型（count>=2）也被计入 type_seq**——这是重要缓冲

**风险级别**：🟡 **MEDIUM**（有天然缓冲，但仍存在结构性偏差）  
- IIT 的 `active_types` 来自 MI matrix 中的 type pair，**不直接等于 dominant**。  
- 当 min-dwell gate hold `action` 时：
  - trajectory 中 `dominant` 字段持续 = "action"
  - 但 `type_counts` 中 `integrator` 可能仍有较高 count（>=2）
  - MI matrix 中 `action-integrator` 对仍然存在
  - Φ 不会被清零，但**高估 action 的积分信息**，低估真实的多类型耦合
- **最危险的情况**：gate hold 期间 attractor 的 `type_counts` 高度集中在单一类型（conc 高），此时 non-dominant count 可能 < 2，被 IIT 过滤，导致 IIT 低估系统实际的多样性。

---

### 3.5 JEPA / Causal Feedback（第 7499–7650 行，10515–10637 行）

**JEPA ranking**（第 7499 行）：发生在 attractor 之前，**不受 gate 影响**  
**JEPA reconcile**（第 10515 行）：使用 `RESTING_STATE.get("last_dominant")`

```python
# L10556
_jepa_win.append({
    "tick": tick,
    "dominant": RESTING_STATE.get("last_dominant"),     # ← post-gate
    "concentration": RESTING_STATE.get("concentration", ...),
    "competition": RESTING_STATE.get("competition", ...),
    "stagnation_streak": RESTING_STATE.get("stagnation_streak", 0),
})
```

**Causal-Lens S5 bridge**（第 9916 行）：通过 `RESTING_STATE["trajectory"][-10:]` 传 raw_trajectory

```python
_traj_clean = [{k: v for k, v in e.items() if k in (
    "tick", "dominant", "concentration", "competition",
    "n_activated", "noise_amp", "phase", "stagnation_streak", "diversity_n"
)} for e in _traj_raw[-10:]]
```

**风险级别**：🟠 **HIGH  
- JEPA reconciliation 的 `dominant` 字段（用于 attribution）是 post-gate 值。  
- JEPA residual attribution 使用 `_jepa_win`（post-gate dominant 序列）来判断 oscillation_lag 与真实预测失败。  
- 如果 gate 长期 hold 单一 dominant，oscillation_lag 检测失效（序列无切换 → 无法区分"预测正确"还是"gate 抑制了真实切换"）。  
- S5 Causal-Lens 接收的 trajectory 也是 post-gate，causal_gap 分析基于修正后的 dominant 序列。  
- **Causal-Lens 的因果归因结论会被 gate 系统性扭曲**：当 gate 阻止了从 `action→integrator` 的合法切换时，causal_gap 报告"低探索""低整合"，而非"探索被 gate 压制"。

---

## 4. 行为稳定器 vs. 意识测量通路的交叉影响图

```
KWTA attractor
    │
    ├─ result["dominant"] = raw attractor dominant
    ├─ result["concentration"] = raw attractor conc
    ├─ result["competition"] = raw attractor comp
    └─ result["type_counts"] = raw attractor type_counts
            │
            ▼
rule_modification gate (可能覆写 dominant)
            │
            ▼
min-dwell gate ────────────────┐ (覆写 result["dominant"]，保留 dwell_pre_cand)
  _md_cand = result["dominant"]  │
  if not _md_decisive:           │
    result["dominant"] = _md_prev│ ← "行为稳定器"覆写
            │                    │
            ▼                    │
 integrator tripwire (可能覆写)   │
            │                    │
            ▼                    │
 RESTING_STATE["last_dominant"]  │
   = result["dominant"] ◄────────┘
            │
   ┌────────┴─────────────────────────────────┐
   │ 所有下游消费者看到 post-gate dominant     │
   │                                          │
   ├── persistent_identity ──────────► identity profile 漂移
   ├── trajectory ───────────────────► IIT / proprio / L0 / S5 全链路受影响
   │         │
   │         ├── IIT ──────────────► Φ 测量（dominant 被修正，type_counts 部分保留）
   │         ├── proprioceptive ───► PE 被 gate 本身制造，回流到 attractor seeding
   │         ├── L0 observer ──────► broadcast target 被 gate 决定
   │         └── S5 Causal-Lens ───► causal_gap 被 gate 系统性扭曲
   │
   └── JEPA reconcile ─────────────► oscillation_lag 检测失效
```

---

## 5. gate 覆写时各系统行为对比

| 系统 | 看到 gated dominant | 看到原始 conc/comp | 原始 attractor dominant 可用？ | 风险 |
|------|-------------------|-------------------|---------------------------|------|
| persistent_identity | ✅ 是 | ❌ 否 | ❌ 仅 dwell_pre_cand | 🟡 MEDIUM |
| L0 MetaObserver | ✅ 是 | ✅ conc/comp 保留 | ❌ 仅 dwell_pre_cand | 🟡 MEDIUM |
| Proprioceptive loop | ✅ 是 | ✅ conc/comp 保留 | ❌ | 🟠 HIGH |
| IIT Φ | ✅ 是（trajectory） | ✅ type_counts 保留 | ⚠️ 间接保留（非 dominant 高 count） | 🟡 MEDIUM |
| JEPA reconcile | ✅ 是 | ✅ | ❌ | 🟠 HIGH |
| S5 Causal-Lens | ✅ 是（trajectory） | ✅ | ❌ | 🟠 HIGH |

---

## 6. 最小、可逆、不强制指定 dominant 的安全边界建议

### 原则
- **不覆写 result["dominant"]**，而是记录覆盖事件
- **在 trajectory 中保留 dual dominant**（post-gate + raw attractor），而非丢弃 raw
- **测量通路**使用 dual dominant，**行为稳定器**使用 post-gate
- 改动可逆：只需恢复 `result["dominant"]` 写入位置

### 建议 A：dual-dominant trajectory entry（最小改动）

在 trajectory entry（L9650 附近）中新增两个字段，不改 result["dominant"] 覆写逻辑：

```python
entry = {
    # ... 现有字段 ...
    "dominant": RESTING_STATE["last_dominant"],      # post-gate（现有）
    # 新增：原始 attractor dominant（在 min-dwell gate 之前保存）
    "raw_attractor_dominant": RESTING_STATE.get("dwell_pre_cand"),
    "raw_attractor_conc": None,   # 需在 KWTA 后立即保存
    "raw_attractor_comp": None,
    # 新增：gate 决策元数据
    "gate_decision": RESTING_STATE.get("dwell_reason"),   # "early_hold"/"late_hold"/...
    "gate_dwell": RESTING_STATE.get("dominant_dwell"),
    "gate_hold": RESTING_STATE.get("dwell_hold"),
    "gate_margin": RESTING_STATE.get("dwell_margin"),
    "gate_challenger_streak": RESTING_STATE.get("dwell_challenger_streak"),
}
```

**影响**：零侵入，所有现有逻辑不变。仅新增只读元数据。  
**效果**：IIT、proprioceptive、S5 可在需要时用 `raw_attractor_dominant` 计算无偏测量。

---

### 建议 B：IIT 使用 dual dominant（中度改动）

在 `_compute_iit_phi()`（L5160 附近）增加 `raw_dominant` 路径：

```python
# 在 type_seq 构建后增加：
_raw_doms = [e.get("raw_attractor_dominant", "")
             for e in traj[-50:]
             if e.get("raw_attractor_dominant")]
if _raw_doms:
    type_seq.extend(_raw_doms)  # 让原始 attractor 类型进入 MI matrix
```

**效果**：IIT 的 active_types 同时包含 post-gate dominant 和原始 attractor 类型，更准确反映系统的实际信息整合。

---

### 建议 C：Proprioceptive 增加 raw dominant 作为对照（低侵入）

在 `proprioceptive_loop.py` 的 `proprio_tick` 中新增 `raw_attractor_dominant` 参数，将 gate 制造的人工 PE 与真实 attractor PE 分离：

```python
# 当前（post-gate dominant）：
actual_dominant = RESTING_STATE.get("last_dominant", "unknown")

# 新增：raw attractor dominant（gate 前的值）
raw_dominant = RESTING_STATE.get("dwell_pre_cand", actual_dominant)
```

在 prediction_history 中分别记录 `actual_post_gate` 和 `actual_raw_attractor`，以便区分：
- `predicted → raw_attractor` 误差 = **真实 attractor PE**
- `predicted → post_gate` 误差 = **gate 制造的人工 PE**

**效果**：将行为稳定器的副作用从测量通路中分离。

---

### 建议 D：Persistent Identity 可选用 raw dominant（可选，保守）

在 PERSISTENT_IDENTITY 更新（L9894）处增加可选的 dual tracking：

```python
_dom_t = result["dominant"]          # post-gate
_raw_dom = RESTING_STATE.get("dwell_pre_cand")
if _raw_dom and _raw_dom != _dom_t:
    # 可选：raw dominant 计入单独的统计，不影响主 identity
    _raw_hist = RESTING_STATE.setdefault("raw_dominant_history", {})
    _raw_hist[_raw_dom] = _raw_hist.get(_raw_dom, 0) + 1
```

---

## 7. 已知结构性陷阱

### 7.1 trajectory[-64:] 是 post-gate dominant 的隐性假设（风险：🔴 CRITICAL）

```python
# L8546: _rm_freq 基于 trajectory[-64:] 的 dominant 计数
_rm_traj = RESTING_STATE.get('trajectory', [])[-64:]
_rm_freq = sum(1 for e in _rm_traj if e.get('dominant') == 'rule_modification')
```

由于 trajectory 存储的是 post-gate dominant，当 min-dwell gate hold 住 rm 时，`_rm_freq` **低估了 rm 的真实出现次数**（被 gate demote 的 rm 被记录为 runner-up）。  
注释 L8557 已确认此问题："trajectory[-64:] stores POST-GATE dominant (runner-up when rm is demoted)"。  
这导致 `_rule_mod_freq_bypass` 结构性不可达，rm 实际上永久被压制（3/1727 ticks = 0.17%）。

### 7.2 attractor type_counts vs trajectory type_counts 不一致（风险：🟡 MEDIUM）

- `type_counts` 在 attractor 之后、gate 之前写入 result，post-gate 不变  
- trajectory entry 的 `type_counts` = result["type_counts"] = attractor 原始  
- 但 `dominant` 在 trajectory 中是 post-gate  
- **不一致**：type_counts 可能显示 integrator 最高 count，但 dominant = action（gate hold 期间）  
- IIT 通过 `_c >= 2 and _t != dom` 间接补偿了部分不一致，但无法完全消除

### 7.3 dwell_pre_cand 的语义歧义（风险：🟡 MEDIUM）

```python
_md_pre_cand = _md_cand   # L8628
_md_cand = result.get("dominant", "unknown")   # L8624
```

在 min-dwell gate 内部，`_md_cand` 是 post-rule_mod 的 result["dominant"]，不是 KWTA 原始 dominant。  
`_md_pre_cand` = rule_mod gate 之后的 candidate，不是原始 attractor dominant。  
真正的原始 dominant 没有显式字段存储（只能从 `dwell_pre_cand` + `dwell_reason` 间接推断）。

---

## 8. 综合风险分级

| 影响对象 | 机制 | 可观测性 | 风险 |
|---------|------|---------|------|
| persistent_identity | 累积性 profile 漂移 | ✅ dwell_pre_cand 可查 | 🟡 **MEDIUM** |
| L0 MetaObserver | broadcast target 由 gate 决定 | ✅ conc/comp 未覆写可对照 | 🟡 **MEDIUM** |
| Proprioceptive loop | gate 制造人工 PE，回流 attractor | ❌ 无法区分 PE 来源 | 🟠 **HIGH** |
| IIT Φ | dominant 被修正，type_counts 部分缓冲 | ⚠️ Φ 不会清零，但低估多样性 | 🟡 **MEDIUM** |
| JEPA reconcile | oscillation_lag 检测失效 | ❌ gate hold 期间序列无切换 | 🟠 **HIGH** |
| S5 Causal-Lens | causal_gap 系统性低估探索/整合 | ❌ 无对照数据 | 🟠 **HIGH** |
| **行为稳定器**（自身） | 正确功能，不视为风险 | ✅ 可观测 dwell_hold/dwell_reason | ✅ **预期行为** |

---

## 9. 最小、可逆、不强制 dominant 的安全边界总结

**核心原则**：门控覆写 `result["dominant"]` → 改变行为节奏 ✓；门控覆写 → 改变意识测量值 ✗

| 干预 | 改动范围 | 可逆性 | dominant 强制？ | 优先级 |
|------|---------|--------|--------------|--------|
| A: trajectory dual-dominant | trajectory entry 结构 | ✅ 完全可逆 | ❌ | 🔴 最高 |
| B: IIT raw dominant 纳入 MI | `_compute_iit_phi()` | ✅ 可逆 | ❌ | 🟠 高 |
| C: proprioceptive raw dominant 对照 | `proprioceptive_loop.py` | ✅ 可逆 | ❌ | 🟠 高 |
| D: identity raw dual tracking | PERSISTENT_IDENTITY | ✅ 可逆 | ❌ | 🟡 中 |

**不需要改动的部分**：min-dwell gate 的行为稳定器逻辑本身（`_md_hold`、`_md_reason`、`dwell_challenger_streak`）不调整。行为稳定器的功能预期内，不应改变。

---

## 10. 附：gate 代码完整位置索引

| 区域 | 行号 | 说明 |
|------|------|------|
| rule_mod 停滞门 | 8536–8606 | pre_gate_dominant 捕获，demote 或 bypass |
| rm 停滞 tripwire | 8607–8617 | 强制 demote，bypass 到期 |
| **min-dwell gate 开始** | **8618** | `_MD_MIN_DWELL = 3` |
| margin 计算 | 8624–8643 | `_md_margin` 从 activation_scores 推导 |
| challenger streak | 8644–8650 | 连续相同 candidate 计数 |
| early window | 8665–8668 | dwell<3: only crisis 放行 |
| late window | 8669–8675 | dwell>=3: crisis OR strong+streak>=2 |
| hold 动作 | 8676–8688 | `result["dominant"] = _md_prev` |
| allow 动作 | 8689–8704 | `_md_dwell = 1`, reset streak |
| **gate 结束** | **8705** | `RESTING_STATE["dominant_dwell"] = _md_dwell` |
| dwell_pre_cand 存储 | 8707/8712 | RESTING_STATE + result 双写 |
| integrator tripwire | 8723–8760 | stag>=3+comp>0.85 → force action |
| last_dominant 同步 | 8760 | `RESTING_STATE["last_dominant"] = result["dominant"]` |
| trajectory entry | 9650–9686 | `dominant` = `last_dominant`；`dwell_pre_cand` 也记录 |
| persistent_identity | 9894–9907 | `_dom_t = result["dominant"]`（post-gate） |
| IIT | 10290–10300 | 读 trajectory[-50:]（post-gate dominant） |
| proprioceptive | 10345–10360 | `actual_dominant = last_dominant`（post-gate） |
| L0 observer | 10357 | `dominant = last_dominant`（post-gate） |
| JEPA reconcile | 10515–10637 | `last_dominant`（post-gate） |
| S5 Causal-Lens | 9916–10005 | trajectory[-10:]（post-gate dominant） |

---

*审计时间：2026-07-22 | 只读，未修改任何文件*
