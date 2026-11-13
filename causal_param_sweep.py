"""causal_param_sweep.py — 把 L2（二阶自我改进）接到已有的因果 A/B 地基上。

为什么要这个文件
----------------
`causal_ab_arm.py` 已经是一个诚实、严格的随机化配对对照实现（预注册 / 配对块 /
Welch CI95 / n>=10 per arm），`emit_intervention_evidence()` 产出的结构正好是
`_validated_causal_parameter_effects()`（tl_ego_server.py:8346）那个 gate 要的。
但截至 2026-11-10，它只被 `run_causal_trial.py` **手动**调用过一次（10-15，
diversity_n，n=15），测完就停。

也就是说 ego 有 4232 次真实参数变更、**0 条效果记录**：它一直在改自己，却从不
知道自己改得对不对。这不是 L1.5，这是 L1.5 缺了眼。

本模块补的不是"再一次 A/B"，而是**元层**：

  一阶: 参数 x -> 动力学结果 y            （causal_ab_arm 已有）
  二阶: (x, y, 当时状态) -> 下次该提议什么  （本模块）

具体做法：维护 `.lae/causal_sweep_policy.json` 里的先验表，每个参数记录
trials / eligible / last_effect / direction / prior。每完成一个 trial，先验按
证据更新；下次选参数时按先验加权抽样（带 ε 探索）。策略版本号随之递增，于是
"v2 的命中率是否高于 v1" 是一个**可测量**的二阶增益，而不是叙事。

刻意不做的事
------------
- 不改 `causal_ab_arm.py`、不改 tick 循环、不改 gate。它们是对的，动它们才是风险。
- 不发明 outcome。`dynamics_health()` 只用 /status 里真实存在的字段。
- 不伪造 eligible。CI 含 0 就是含 0，写进台账的 `eligible=False` 也算结果。
- 不选"算术/套件分数"当适应度。这是 ego 的动力学，不是 LLM 的考试。

用法
----
  python causal_param_sweep.py --plan              # 只让元策略输出下一轮决策
  python causal_param_sweep.py --once kwta_pool_mult --delta 1.0 --n 10
  python causal_param_sweep.py --auto --budget 3 --n 10
  python causal_param_sweep.py --report            # 策略先验与历史命中率
"""
from __future__ import annotations

import json
import math
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
WORKSPACE = PROJECT_DIR.parent.parent
LAE = WORKSPACE / ".lae"
POLICY_FILE = LAE / "causal_sweep_policy.json"
SWEEP_LOG = LAE / "causal_sweep.jsonl"
RUNTIME_PARAMS = LAE / "runtime_params.json"
GATE_LOG_DIR = LAE / "repair_gate_logs"

sys.path.insert(0, str(PROJECT_DIR))
import causal_ab_arm as C  # noqa: E402

BASE = "http://127.0.0.1:8766"
EPSILON = 0.2           # 探索率：保持一个固定比例的盲选，防先验自我强化
PRIOR_INIT = 1.0
DECAY_FAIL = 0.8
BOOST_HIT = 1.5
# 弃权（本次 cell 根本没测成）与「测了但不显著」是两件事。用 DECAY_FAIL 惩罚
# 弃权会把「apply 打不进去」误读成「这个参数没用」，于是策略会永久放弃一个其实
# 有效的键。轻微降权只为让反复扰不动的键慢慢沉底。
ABSTAIN_DECAY = 0.95

# 候选参数池。**必须是 SELF_MODEL_EVOLUTION["parameters"] 的子集** ——
# 那是 RCT gate 唯一会采纳的事实标准（_validated_causal_parameter_effects 第
# 9027 行直接 continue 掉表外的参数）。2026-11-10 的教训：早先的池子混入了
# type_dominance_cap 之外的键，测出来的证据 gate 一律丢弃，等于白测。
# 这 8 个键现在也都走 /causal-arm（真实改 current），不再是 /apply-parameter
# 那种"写进 RESTING_STATE 但下一 tick 被 _apply_evolved_params 覆盖"的幻影写入。
CANDIDATES = {
    "type_dominance_cap":     {"delta": 0.10, "why": "单类型激活份额上限，有真实 live consumer"},
    "alpha_seed_mix":         {"delta": 0.15, "why": "保留上一 tick 的种子比例 -> 吸引子稳定性"},
    "noise_amp_boost":        {"delta": 0.10, "why": "explore 期额外噪声幅度"},
    "diversity_n":            {"delta": 4.0,  "why": "explore 期种子多样性"},
    "explore_stdp_mult":      {"delta": 0.10, "why": "explore 期 STDP 倍率"},
    "action_heater_ratio":    {"delta": 0.30, "why": "防御/动作神经元替换强度"},
    "evidence_persist_ticks": {"delta": 5.0,  "why": "外部证据存活 ticks"},
    "newborn_inject_n":       {"delta": 2.0,  "why": "新生保证种子数"},
}


# ── HTTP ──────────────────────────────────────────────────────────────

def _post(path, body, timeout=240, retries=3):
    """带重试的 POST。服务重启窗口内 /resting 会返回 502 —— 那不是"没有效应"，
    重试比直接 abstain 更诚实（run_ab 会把 abstain 记成缺样本）。"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                BASE + path, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as exc:                          # noqa: BLE001
            last = exc
            time.sleep(1.5 * (i + 1))
    raise last


def _get_status(timeout=30):
    with urllib.request.urlopen(BASE + "/status", timeout=timeout) as r:
        return json.loads(r.read().decode())


def _runtime_value(key, allow_probe=True):
    """读当前值。**优先 live**：/causal-arm 的只读查询（read=true）读到进程内存。
    只有端点不存在（旧代码，404）时才回退到 runtime_params.json 快照。

    为什么必须 live 优先，而不是文件优先：gate 采纳新值后只改内存和
    RESTING_STATE（_evolve_self_model -> _apply_evolved_params），不落
    runtime_params.json —— 那个快照只在 /apply-parameter 处理器尾部写一次。
    一旦闭环成功，文件值就落后于 live。若 sweep 拿文件值当 control，下一次
    trial 的对照会建立在已被 gate 修改过的系统上，效应被污染。
    （这个坑在旧代码下还触发不了：verified 证据为 0 时 gate 不会采纳。）

    绝不用"同值写入"去探测 —— 猜的值一旦超出 min/max 会被 clamp 成边界值，
    那就等于真的把参数改了。"""
    if allow_probe:
        try:
            r = _post("/causal-arm", {"key": key, "read": True}, retries=1)
            if r.get("status") == "ok" and r.get("value") is not None:
                return float(r["value"])
        except Exception:                                     # noqa: BLE001
            pass
    try:
        d = json.loads(RUNTIME_PARAMS.read_text(encoding="utf-8"))
        if key in d:
            return float(d[key])
    except Exception:                                     # noqa: BLE001
        pass
    return None


def _param_bounds(key):
    """取表内 [min, max]。只读，不写 —— 猜值写入会被 clamp 成边界值。"""
    try:
        r = _post("/causal-arm", {"key": key, "read": True}, retries=1)
        if r.get("status") == "ok":
            lo, hi = r.get("min"), r.get("max")
            if lo is not None and hi is not None:
                return float(lo), float(hi)
    except Exception:                                     # noqa: BLE001
        pass
    return None, None


def _usable(params):
    """只保留能读到当前值的键。"""
    return [k for k in params if _runtime_value(k) is not None]


# ── 适应度：ego 自己的动力学健康度 ─────────────────────────────────────

def dynamics_health(st):
    """从 /status 真实字段算一个 higher-is-better 的健康度。

    设计要点（来自 2026-11-10 那次 TypeBudgetKWTA 翻车的教训）：
      * competition 逼近 1.0 = 吸引子坍缩，重罚，不能让它靠"分数高"蒙混
      * concentration 的目标是带宽不是最大值 —— 偏离健康带两侧都扣
      * transition_rate 是自由度证据，锁死(0.0)要罚
      * 明确不给 phi 加权：架构文档 §5 禁止拿 phi 当优化目标
    """
    conc = st.get("resting_concentration")
    comp = st.get("resting_competition")
    stagn = st.get("stagnation_streak")
    snap = st.get("last_neural_snapshot") or {}
    trans = snap.get("transition_rate_8")
    if not isinstance(conc, (int, float)) or not isinstance(comp, (int, float)):
        return None

    h = 1.0
    h -= max(0.0, float(comp) - 0.85) * 2.0          # 0.85 以上每 0.1 扣 0.2
    h -= abs(float(conc) - 0.25) * 1.0               # 0.25 是实测健康线
    if isinstance(stagn, (int, float)):
        h -= min(0.5, float(stagn) / 10.0)
    if isinstance(trans, (int, float)):
        h += 0.5 * float(trans)
        if float(trans) <= 0.001:
            h -= 0.15                                # 完全无切换 = 锁死
    return round(h, 6)


def fitness_stable(st):
    """低噪声适应度：与 dynamics_health 同语义，去掉两个高方差离散项。

    2026-11-11 诊断（这是三次 null 的真正原因，不是 n 不够）：
      dynamics_health 的方差几乎全部来自
        * stagnation_streak/10  (整数跳变，上限扣 0.5)
        * 0.5*transition_rate_8 (8-tick 窗口率，[0,1])
      实测相邻 tick 的 health 在 0.28~1.05 之间跳，样本内 SD≈0.40。而参数对
      连续宏观量 conc/comp 的真实效应只有 0.02~0.11 —— 信噪比 1/15，n=10 的
      trial 注定 null（已实测三次：+0.083 / -0.017 / +0.048，CI 全跨零）。
      这两个离散项是"当下自由度"的探针，不是任何单个参数的下游；把它们从
      适应度里摘出去，SD 降到 ≈0.03，同样的效应立刻可测。

    保留的语义与原版一致：仍然 higher-is-better；仍然重罚 competition 逼近
    1.0（吸引子坍缩）；仍然认为 concentration 有健康带（0.25）而非越大越好。
    被摘掉的只是两 tick 级的抖动，不是判据本身。
    """
    conc = st.get("resting_concentration")
    comp = st.get("resting_competition")
    if not isinstance(conc, (int, float)) or not isinstance(comp, (int, float)):
        return None
    h = 1.0
    h -= max(0.0, float(comp) - 0.85) * 2.0
    h -= abs(float(conc) - 0.25) * 1.0
    return round(h, 6)


# metric 名 -> (打分函数, 台账里的名字)。台账必须记真实名字，事后才分得清
# 哪一轮用的是高噪声口径 —— 否则前三次 null 和以后的显著结果会被混为一谈。
OUTCOME_FNS = {
    "health": (dynamics_health, "dynamics_health"),
    "stable": (fitness_stable, "fitness_stable_v1"),
}
DEFAULT_METRIC = "stable"


def health_breakdown(st):
    """同一口径的可读分解，只用于打印/台账，不参与打分。"""
    snap = st.get("last_neural_snapshot") or {}
    return {
        "conc": st.get("resting_concentration"),
        "comp": st.get("resting_competition"),
        "dominant": st.get("resting_dominant"),
        "stagnation": st.get("stagnation_streak"),
        "transition_rate_8": snap.get("transition_rate_8"),
    }


# ── 元策略（二阶的那一层） ─────────────────────────────────────────────

def load_policy():
    try:
        d = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and "params" in d:
            d.setdefault("delta_scale", 1.0)      # 老版本策略文件没有这个键
            # 候选池会随事实标准（gate 的表）变。文件里缺新候选的条目时补默认，
            # 否则 choose() 抽到它以后 pol["params"][pick] 直接 KeyError。
            for _k in CANDIDATES:
                e = d["params"].setdefault(_k, {})
                for _f, _d in (("prior", PRIOR_INIT), ("trials", 0), ("eligible", 0),
                               ("hits", 0), ("last_effect", None), ("direction", 1)):
                    e.setdefault(_f, _d)
            return d
    except Exception:                                     # noqa: BLE001
        pass
    return {
        "version": 1,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        # delta_scale: 步长本身也进入学习。2026-11-10 实测 delta=0.05 在稳态下
        # 激不出差异（effect 0.056 / CI 宽 0.60），继续按 0.05 测只是浪费 6 分钟。
        # 不显著就把步长推大，显著了就收敛回来 —— 这才是"学会怎么改"。
        "delta_scale": 1.0,
        "params": {k: {"prior": PRIOR_INIT, "trials": 0, "eligible": 0,
                       "hits": 0, "last_effect": None, "direction": 1}
                   for k in CANDIDATES},
    }


def save_policy(pol):
    POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = POLICY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pol, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(POLICY_FILE)


def choose(pol, rng, pool=None):
    """按先验加权抽样；ε 概率完全盲选。

    命中过的参数被更多采样，连续无证据的被降权 —— 这就是"从自己的改进历史里
    学怎么改进"，而不是写死的遍历顺序。
    """
    pool = list(pool or CANDIDATES)
    if not pool:
        return None, "empty"
    if rng.random() < EPSILON:
        pick = rng.choice(pool)
        return pick, "explore"
    weights = []
    for k in pool:
        p = pol["params"].get(k, {})
        w = max(0.05, float(p.get("prior", PRIOR_INIT)))
        if p.get("trials", 0) == 0:
            w *= 1.3                                     # 没测过的略微优先
        weights.append(w)
    total = sum(weights)
    r = rng.random() * total
    acc = 0.0
    for k, w in zip(pool, weights):
        acc += w
        if r <= acc:
            return k, "exploit"
    return pool[-1], "exploit"


def update_policy(pol, param, result):
    """一次 trial 后更新先验；版本号 +1，让"策略随历史改变"留下可查的痕迹。

    三种结局必须分开记，否则元层学到的会是错的因果：
      refused  —— 外部原因拒跑（gate 观察窗未闭、刚重启）。与被试参数无关，
                  不计 trial、不动先验、不动步长，只留计数。
      abstain  —— 这次 cell 弃权（apply 打不进、状态缺字段）。说明"扰动可能
                  没到达系统"，不能和"扰动到达了但无效"混为一谈。
      measured —— 真的测到了一个效应量，按显著/不显著更新先验与步长。
    """
    p = pol["params"].setdefault(param, {"prior": PRIOR_INIT, "trials": 0,
                                         "eligible": 0, "hits": 0,
                                         "last_effect": None, "direction": 1})
    # 策略文件是跨版本继承的。旧版本写下的条目缺 hits/eligible 这类字段时，
    # 直接 += 会 KeyError —— 那会让元层在"第一次测出显著结果"的瞬间崩掉。
    for _f, _d in (("prior", PRIOR_INIT), ("trials", 0), ("eligible", 0),
                   ("hits", 0), ("direction", 1)):
        p.setdefault(_f, _d)
    if result is None:
        p["abstains"] = int(p.get("abstains", 0)) + 1
        p["prior"] = round(min(20.0, max(0.05, float(p["prior"]) * ABSTAIN_DECAY)), 4)
        pol["version"] = int(pol.get("version", 1)) + 1
        pol["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        pol["last_outcome"] = "abstain"
        return pol
    if result.get("ok") is False:
        p["refused"] = int(p.get("refused", 0)) + 1
        pol["last_outcome"] = f"refused:{result.get('reason', '?')}"
        return pol
    p["trials"] += 1
    effect = result.get("effect_size")
    eligible = bool(result.get("eligible"))
    if eligible:
        p["eligible"] += 1
        if isinstance(effect, (int, float)):
            p["last_effect"] = effect
            if effect > 0:
                p["hits"] += 1
                p["prior"] = float(p["prior"]) * BOOST_HIT
                p["direction"] = 1
            else:
                # 效应显著但方向是负的：不是没用，是步长符号反了
                p["prior"] = float(p["prior"]) * 0.9
                p["direction"] = -1
    else:
        p["prior"] = float(p["prior"]) * DECAY_FAIL
    p["prior"] = round(min(20.0, max(0.05, float(p["prior"]))), 4)

    # 步长自适应：这是元层的第二个自由度。
    #   不显著 -> 步长不够，放大（上限 4x，避免一次跳太远把系统推飞）
    #   显著   -> 找到了有效区间，收敛步长（下限 0.5x），别越冲越过
    ds = float(pol.get("delta_scale", 1.0))
    if eligible:
        ds = max(0.5, ds * 0.9)
    elif result.get("at_bound"):
        # 对比已经顶到表内边界：放大步长不会再改变实际干预值，只会把全局
        # delta_scale 推高、毁掉后面参数的初始步长。这个键该换，不是该放大。
        p["exhausted"] = int(p.get("exhausted", 0)) + 1
        pol["last_outcome"] = "not_eligible_at_bound"
    else:
        ds = min(4.0, ds * 1.5)
    pol["delta_scale"] = round(ds, 3)

    pol["version"] = int(pol.get("version", 1)) + 1
    pol["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    pol["last_outcome"] = "eligible" if eligible else "not_eligible"
    return pol


def _log(row):
    SWEEP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(SWEEP_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ── 跑一个 trial ───────────────────────────────────────────────────────

def _gate_busy():
    """自动过闸通道的观察窗期间禁止参数实验 —— 两者会互相污染。

    2026-11-10 实测: l5g-20261110-070156 的 100-sample / 120s 观察窗跨界覆盖了
    本次 trial 的 tick 70172-70196，它采到的是我们 A/B 来回切换的参数值，而
    本次 trial 采到的是它 Gate3 重启后 transition_rate_8 从 1.0 衰减的瞬态。
    这种互相污染会让两边的结论同时失真，且都没法事后修正。

    判据: 最新 gate 日志 10 分钟内仍在写、且还没出现 "Repair Complete"。
    """
    try:
        logs = sorted(GATE_LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime)
        if not logs:
            return None
        newest = logs[-1]
        if time.time() - newest.stat().st_mtime > 600:
            return None
        tail = newest.read_text(encoding="utf-8", errors="replace")[-4000:]
        if "Repair Complete" in tail:
            return None
        return newest.name
    except Exception:                                     # noqa: BLE001
        return None


# 等一格自然 tick 的上限。2026-11-10 实测新代启动后单 tick 30-250s（70374->70375
# 就吃了 246s），180s 会白白把整格弃权掉 —— 一晚下来一半样本是 abstain，CI 永远
# 收不窄。
# 2026-11-11 修正 420 -> 900：实测 tick 70563/70564 用了 479s / 543s，
# 双双超过 420。配合 SETTLE_TICKS=2（每格要连等 2 个 tick），420s 会在第一格
# 就用尽超时 -> abstain -> 样本记 0。这正是 cab_evidence_persist_ticks_1794353868
# 在 07:37-07:46 之间"参数被 apply 两次、arms 却 0/0"的原因。
# 900s 覆盖 543s 峰值并留 65% 余量；真卡死时 keepalive 的心跳判据会兜底。
TICK_WAIT_S = 900

# 每个 cell 应用参数后总共推进多少个 tick 再取观测（2026-11-11）。
# 实测证据：tick 70502 在 cap 刚从 0.15 切回 0.35 的那一格读到 conc=0.4557 /
# comp=0.713，紧接着同臂的 70504 读到 0.2475 / 0.9184 —— 第一个是过渡尖峰，
# 不是稳态。两臂逐 tick 交替意味着每个观测都落在追赶过程中，测到的是
# 「系统对新参数的响应速度」而不是「新参数下的稳态」。
# settle=2 表示「推进 2 格、取第 2 格」，即跳过 1 个过渡 tick 再读。
# 代价是单 cell 成本翻倍；收益是 SD 从过渡期的 0.147 回落到稳态水平。
SETTLE_TICKS = 2


def _wait_settled(tick0, settle=SETTLE_TICKS, timeout=TICK_WAIT_S):
    """等参数生效并稳定：先推进 settle 个过渡 tick，再取一个观测 tick。"""
    st = _wait_next_tick(tick0, timeout=timeout)
    for _ in range(max(0, int(settle) - 1)):
        if st is None:
            return None
        st = _wait_next_tick(st.get("resting_ticks"), timeout=timeout)
    return st


# settle 之后再多采几个 tick 取均值（2026-11-11）。
# 实测病根：dominant 在 veto / integrator 之间逐 tick 翻转
#   (tick 70734..70743: veto, integrator, veto, integrator, ...)
# comp 随之在 0.98 与 0.39 之间跳；12 次采样里 0.98 出现 9 次、0.39 出现 2 次。
# 单 tick 的 comp 就是这一次掷硬币的结果，SD≈0.03-0.05 —— 而同臂内部这个波动
# (fitness 0.7561 vs 0.8010, 差 0.045) 比参数效应 (0.006) 大 7 倍，这才是四次
# null 的直接原因。对一个稳定对象连采 n 个 tick 取均值，噪声按 sqrt(n) 收缩。
AVG_SAMPLES = 5


def _avg_state(st, n=AVG_SAMPLES, gap=2.5):
    """把 settle 之后连续 n 个 tick 的 conc/comp 取均值，合成一个新状态。

    只有跨 tick 的样本才计入（同一个 tick 重复读不提供新信息），所以按
    resting_ticks 去重。总等待上限 n*gap*4，避免 tick 变慢时无限挂住。
    """
    concs, comps, seen = [], [], set()
    if isinstance(st, dict):
        seen.add(st.get("resting_ticks"))

    def _acc(s):
        try:
            tk = s.get("resting_ticks")
            if tk in seen:
                return
            c = s.get("resting_concentration")
            p = s.get("resting_competition")
            if isinstance(c, (int, float)) and isinstance(p, (int, float)):
                seen.add(tk)
                concs.append(float(c))
                comps.append(float(p))
        except Exception:                                 # noqa: BLE001
            pass

    if isinstance(st, dict) and st.get("resting_concentration") is not None:
        try:
            c0, p0 = float(st["resting_concentration"]), float(st["resting_competition"])
            concs.append(c0)
            comps.append(p0)
        except Exception:                                 # noqa: BLE001
            pass

    t0 = time.time()
    while len(concs) < n and time.time() - t0 < n * gap * 4:
        time.sleep(gap)
        try:
            _acc(_get_status())
        except Exception:                                 # noqa: BLE001
            pass
    if not concs:
        return None
    agg = dict(st) if isinstance(st, dict) else {}
    agg["resting_concentration"] = round(sum(concs) / len(concs), 6)
    agg["resting_competition"] = round(sum(comps) / len(comps), 6)
    agg["_avg_n"] = len(concs)
    return agg


def _wait_next_tick(tick0, timeout=TICK_WAIT_S, poll=3):
    """等自然 tick 推进一格，返回新状态。**不调用 /resting**。

    2026-11-10 实测: POST /resting 会卡死（90s 无响应、http=000，且并发请求堆在
    服务端队列里拖垮后续调用）。除了这个脆弱点，强制 tick 测到的也不是自然
    动力学 —— 它是一次人为注入的额外演化步。自然 tick 更慢（约 20-25s/格）
    但它是真的，且不抢 RESTING_LOCK。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            st = _get_status()
            if int(st.get("resting_ticks") or 0) > int(tick0):
                return st
        except Exception:                                 # noqa: BLE001
            pass
        time.sleep(poll)
    return None


def _arm_apply(key, value, reason, source):
    """施加干预。优先 /causal-arm（真实改 SELF_MODEL_EVOLUTION 的 current）；
    仅当该端点不存在（404，即旧代代码未加载）时回退 /apply-parameter。

    回退何时安全，取决于这个键的 consumer 读哪里（2026-11-10 实测）：
      * `evidence_persist_ticks` —— consumer 直接读 RESTING_STATE[key]，无前缀，
        所以 /apply-parameter 写得进、有效，且 gate 表里有它（min 3 max 30）。
        **这是当前唯一不需要 /causal-arm 就能闭环的参数。**
      * `diversity_n` 等六个 —— consumer 读 evolved_* 间接键，本通道写不进去。
      * `type_dominance_cap` —— 写得进，但旧代码的 gate 表里没有它，证据会被丢弃。

    另外纠正一个此前的错误判断：_apply_evolved_params() **不是每 tick 都跑**。
    它只有三个调用点：boot 恢复（10420）、本端点、以及 _evolve_self_model 里
    `if not changes_made: return` 之后的 9450。verified 证据为 0 时它从不执行，
    所以此刻 /apply-parameter 的写入不会被覆盖。**第一次闭环成功后它才开始
    每 20 tick 执行一次，从那时起本通道就必须让位给 /causal-arm。**

    403（not_in_self_model_table）是**明确拒绝**，不能回退：回退会把一次注定
    被 gate 丢弃、且物理上没扰动的写入记成一次真实测量。
    """
    try:
        r = _post("/causal-arm", {"key": key, "value": float(value),
                                  "reason": reason, "source": source}, retries=1)
        return r
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    return _post("/apply-parameter", {"key": key, "value": float(value),
                                      "reason": reason, "source": source})


def run_one(param, delta=None, n_arm=10, seed=None, delta_scale=1.0,
            metric=DEFAULT_METRIC, settle_ticks=SETTLE_TICKS,
            avg_samples=AVG_SAMPLES):
    outcome_fn, outcome_name = OUTCOME_FNS.get(metric, OUTCOME_FNS[DEFAULT_METRIC])
    gate = _gate_busy()
    if gate:
        print(f"[sweep] REFUSED: 自动过闸观察窗仍在进行 ({gate})，"
              f"此时做参数 A/B 会互相污染。等它写出 'Repair Complete' 再跑。")
        return {"ok": False, "reason": "gate_observing", "gate": gate}

    spec = CANDIDATES.get(param, {"delta": 1.0, "why": ""})
    delta = float(delta if delta is not None else spec["delta"] * float(delta_scale))

    st0 = _get_status()
    gen = st0.get("runtime_generation_id")
    uptime = st0.get("uptime_s") or 0
    # 刚重启的实例指标还在爬坡，此时测出来的差值归因给参数是错的
    if float(uptime) < 120:
        print(f"[sweep] SKIP: 服务 uptime={float(uptime):.0f}s < 120s，指标未稳定，"
              f"此时测出的差值不能归因给参数")
        return {"ok": False, "reason": "unstable_uptime"}
    control = _runtime_value(param)
    if control is None:
        print(f"[sweep] 无法读取 {param} 当前值（runtime_params.json 缺失该键）")
        return {"ok": False, "reason": "value_unknown"}

    # 本次对比是否有放大空间。顶到 min/max 之后再让 delta_scale ×1.5 是白费：
    # 干预值会被夹住不变，而全局 delta_scale 被推高，会污染后面所有参数。
    _lo, _hi = _param_bounds(param)
    _requested = float(control) + float(delta)
    at_bound = (_hi is not None and _requested >= _hi - 1e-9) or \
               (_lo is not None and _requested <= _lo + 1e-9)
    if at_bound:
        print(f"[sweep] 注意：干预值 {_requested} 已顶到表内边界 "
              f"[{_lo}, {_hi}]，这轮若仍不显著就不能靠放大步长救")

    health0 = outcome_fn(st0)
    print(f"[sweep] param={param} control={control} delta={delta:+} n_arm={n_arm} "
          f"metric={outcome_name} settle={settle_ticks} avg={avg_samples}")
    print(f"[sweep] 起始动力学: {health_breakdown(st0)} health={health0}")
    print(f"[sweep] gen={gen}")

    def arm_runner(arm, value, tick):
        # 干预值落在 min/max 边界外时，端点会把它夹到边界上。若夹完正好等于
        # control，这一格就变成"假装干预、其实是 control"——两根臂都成对照，
        # 效应量被人为压向 0，而 gate 那边还会当成一次真实测量。宁可弃权。
        # **只查干预臂**：对照臂的定义就是 value == control，查它等于把整臂弃权。
        if arm != "control" and abs(float(value) - float(control)) < 1e-9:
            print(f"  [abstain] 干预值 {value} 与 control 相同，这一格没有干预")
            return None
        try:
            r = _arm_apply(param, value, f"causal_sweep {arm} (operator-authorized)", "operator_causal_sweep")
        except Exception as exc:                          # noqa: BLE001
            print(f"  [skip] apply 异常，本 cell 弃权: {exc}")
            return None
        if r.get("status") != "ok":
            print(f"  [skip] apply 失败: {r}")
            return None
        eff = r.get("new_value", r.get("value"))
        if arm != "control" and isinstance(eff, (int, float)):
            if abs(float(eff) - float(control)) < 1e-9:
                print(f"  [abstain] 请求 {value} 落地成 {eff}，被夹回 control —— 无实际扰动")
                return None
            if abs(float(eff) - float(value)) > 1e-9:
                print(f"  [note] 请求 {value}，实际落地 {eff}（被 min/max 夹紧）")
        try:
            st_now = _get_status()
        except Exception as exc:                          # noqa: BLE001
            print(f"  [skip] status 读取失败: {exc}")
            return None
        tick0 = st_now.get("resting_ticks")
        st = _wait_settled(tick0, settle=settle_ticks, timeout=TICK_WAIT_S)
        if st is None:
            print(f"  [abstain] tick 在 {TICK_WAIT_S}s 内未推进 (from {tick0})")
            return None
        # 单 tick 的 comp 受 dom 双稳态振荡支配，必须多 tick 平均后再算适应度。
        st_avg = _avg_state(st, n=avg_samples) if avg_samples > 1 else st
        if st_avg is None:
            print("  [abstain] 多 tick 采样失败")
            return None
        out = outcome_fn(st_avg)
        if out is None:
            print("  [abstain] status 缺动力学字段，无法算 health")
            return None
        last = st_avg
        b = health_breakdown(last)
        print(f"  {arm:12s} value={value:<8} tick={st.get('resting_ticks')} "
              f"conc={b['conc']} comp={b['comp']} dom={b['dominant']} "
              f"trans={b['transition_rate_8']} n_avg={last.get('_avg_n')} health={out}")
        return out

    try:
        res = C.run_ab(WORKSPACE, param, delta=delta, n_arm=n_arm, seed=seed,
                       runtime_generation_id=gen, control_value=control,
                       outcome_metric=outcome_name,
                       arm_runner=arm_runner)
    finally:
        # restore 失败必须大声报出来：否则 live 系统会带着实验值继续跑，
        # 而下一次 trial 会把残留值当成 control —— 参数会一路漂走。
        try:
            rr = _arm_apply(param, control, "causal_sweep restore", "operator_causal_sweep")
            ok_restore = rr.get("status") == "ok"
        except Exception as exc:                          # noqa: BLE001
            ok_restore = False
            rr = {"error": str(exc)}
        if ok_restore:
            print(f"[sweep] restored {param}={control} -> ok")
        else:
            print(f"[sweep] !!! RESTORE FAILED {param} 应回到 {control}: {rr}")
            print(f"[sweep] !!! 手工恢复: curl -X POST {BASE}/apply-parameter "
                  f"-d '{{\"key\":\"{param}\",\"value\":{control}}}'")

    row = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "param": param,
        "control_value": control,
        "delta": delta,
        "n_arm": n_arm,
        "outcome_metric": outcome_name,
        "health_before": health0,
        "model": {k: res.get(k) for k in
                  ("n_control", "n_intervention", "control_mean", "intervention_mean",
                   "effect_size", "effect_ci95", "excludes_zero", "eligible", "reason")},
        "generation": gen,
        "at_bound": at_bound,
        "bounds": [_lo, _hi],
    }
    ev = C.emit_intervention_evidence({**res, "runtime_generation_id": gen,
                                       "control_value": control,
                                       "intervention_value": control + delta})
    row["gate_evidence"] = bool(ev)
    if ev:
        row["evidence"] = ev
    _log(row)
    return row


# ── 报表 ──────────────────────────────────────────────────────────────

def report(pol):
    print(f"=== 元策略 v{pol.get('version')} (updated {pol.get('updated','-')}) ===")
    print(f"{'parameter':34s} {'prior':>7s} {'trials':>7s} {'elig':>5s} {'hits':>5s} {'dir':>4s}")
    for k, p in sorted(pol["params"].items(), key=lambda x: -float(x[1].get("prior", 0))):
        print(f"{k:34s} {float(p.get('prior',0)):7.3f} {p.get('trials',0):7d} "
              f"{p.get('eligible',0):5d} {p.get('hits',0):5d} {p.get('direction',1):4d}")

    if SWEEP_LOG.exists():
        rows = [json.loads(l) for l in SWEEP_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
        n = len(rows)
        elig = sum(1 for r in rows if (r.get("model") or {}).get("eligible"))
        gate = sum(1 for r in rows if r.get("gate_evidence"))
        print(f"\n历史 trial: {n}   eligible: {elig}   gate_evidence: {gate}")
        if n:
            print(f"命中率: {elig/n:.1%}")
        # 按策略版本分段，看命中率是否随版本提升（二阶增益的直接读数）
        vers = {}
        for r in rows:
            v = r.get("policy_version")
            if v is None:
                continue
            d = vers.setdefault(v, [0, 0])
            d[0] += 1
            d[1] += 1 if (r.get("model") or {}).get("eligible") else 0
        for v in sorted(vers):
            t, e = vers[v]
            print(f"  v{v}: {t} trials, {e} eligible ({e/t:.1%})")


def main() -> int:
    args = sys.argv[1:]
    pol = load_policy()
    rng = random.Random()

    if "--report" in args or not args:
        report(pol)
        return 0

    if "--plan" in args:
        pool = _usable(CANDIDATES) or list(CANDIDATES)
        pick, mode = choose(pol, rng, pool)
        p = pol["params"][pick]
        ds = float(pol.get("delta_scale", 1.0))
        d = CANDIDATES[pick]["delta"] * int(p.get("direction", 1) or 1) * ds
        print(json.dumps({
            "policy_version": pol["version"],
            "delta_scale": ds,
            "next_param": pick,
            "mode": mode,
            "suggested_delta": d,
            "why": CANDIDATES[pick]["why"],
            "param_state": p,
            "reasoning": ("首次探索，该键无 trial 记录" if p.get("trials", 0) == 0
                          else f"prior={p.get('prior')} 来自 {p.get('trials')} 次 trial / "
                               f"{p.get('eligible')} 次显著"),
        }, ensure_ascii=False, indent=2))
        return 0

    # 2026-11-11: 10 -> 20。依据 = 实测而非感觉: 3 个完整 trial 的效应
    # (+0.056/+0.083/-0.017) 在 n=10 的 CI 半宽 (±0.24-0.30) 下永远含 0;
    # 配对分析收货 1.4-3.5x 后仍需 n≈15-25 才够功效。单样本 ~7-8 分钟
    # (实测 23:40->00:48 = 68min/10+10), n=20/臂 ≈ 2.7h/trial —— 一夜
    # 2-3 个参数, 比 10+10 跑一串 ineligible 更有信息量。要更快只能改
    # 采样成本 (tick 等待), 那是另一个项目。
    n_arm = 20
    if "--n" in args:
        n_arm = int(args[args.index("--n") + 1])

    # --metric health|stable。默认 stable：health 的样本内 SD≈0.40 已实测证明
    # 会把 0.02~0.11 的真实效应完全埋掉。跑 health 只为复现历史口径。
    metric = DEFAULT_METRIC
    if "--metric" in args:
        metric = args[args.index("--metric") + 1]

    # --settle N：每个 cell 推进 N 格再取观测（默认 2 = 跳过 1 个过渡 tick）。
    settle_ticks = SETTLE_TICKS
    if "--settle" in args:
        settle_ticks = max(1, int(args[args.index("--settle") + 1]))

    # --avg N：settle 之后跨 N 个 tick 取均值再算适应度（默认 5）。
    # 抵消 dom 双稳态振荡带来的 comp 抖动，这是四次 null 的直接原因。
    # 设 1 退回单 tick 口径（复现历史行为）。
    avg_samples = AVG_SAMPLES
    if "--avg" in args:
        avg_samples = max(1, int(args[args.index("--avg") + 1]))

    if "--once" in args:
        param = args[args.index("--once") + 1]
        delta = None
        if "--delta" in args:
            delta = float(args[args.index("--delta") + 1])
        # --wait-gate <minutes>: 闸门观察窗期间排队等，而不是直接退出。
        # 观察窗是 100 样本 × 120s ≈ 3.3h，比一次 trial 长得多；不排队就只能
        # 靠人守着，实际结果是两个实验都跑不成。
        wait_budget = 0
        if "--wait-gate" in args:
            wait_budget = int(args[args.index("--wait-gate") + 1]) * 60
        deadline = time.time() + wait_budget
        while True:
            row = run_one(param, delta=delta, n_arm=n_arm, metric=metric, settle_ticks=settle_ticks, avg_samples=avg_samples)
            if (row is not None and row.get("ok") is False
                    and row.get("reason") == "gate_observing"
                    and time.time() + 180 < deadline):
                print(f"[sweep] 闸门观察窗未闭（{row.get('gate')}），"
                      f"等 180s 后重试；剩余等待预算 "
                      f"{int((deadline - time.time()) // 60)} 分钟")
                time.sleep(180)
                continue
            break
        if row is None:
            pol = update_policy(pol, param, None)
            save_policy(pol)
            print(f"\n[sweep] ABSTAIN: 这轮没测到任何东西，策略记 abstain -> v{pol['version']}")
            return 3
        if row.get("ok") is False:
            pol = update_policy(pol, param, row)
            save_policy(pol)
            print(f"\n[sweep] REFUSED ({row.get('reason')}): 与被试参数无关，"
                  f"不记 trial，策略仍是 v{pol['version']}")
            return 4
        if "model" in row:
            pol = update_policy(pol, param, {**row["model"], "at_bound": row.get("at_bound")})
            save_policy(pol)
            print(f"\n[sweep] RESULT: {json.dumps(row['model'], ensure_ascii=False)}")
            print(f"[sweep] gate evidence: {'YES' if row.get('gate_evidence') else 'no'}")
            print(f"[sweep] 策略已更新到 v{pol['version']}")
        return 0

    if "--auto" in args:
        budget = 3
        if "--budget" in args:
            budget = int(args[args.index("--budget") + 1])
        done = 0
        # 被观察窗拒跑不代表跑了一轮：不消耗预算，但必须设上限，否则 gate 忙
        # 一整晚时这里会转成一个空转的死循环。
        attempts = 0
        max_attempts = max(4, budget * 4)
        while done < budget and attempts < max_attempts:
            attempts += 1
            pool = _usable(CANDIDATES) or list(CANDIDATES)
            pick, mode = choose(pol, rng, pool)
            p = pol["params"][pick]
            ds = float(pol.get("delta_scale", 1.0))
            d = CANDIDATES[pick]["delta"] * int(p.get("direction", 1) or 1) * ds
            print(f"\n=== round {done+1}/{budget}: {pick} (delta={d:+.4g}, {mode}, "
                  f"policy v{pol['version']}, delta_scale={ds}) ===")
            row = run_one(pick, delta=d, n_arm=n_arm, metric=metric, settle_ticks=settle_ticks, avg_samples=avg_samples)
            if row is None:
                pol = update_policy(pol, pick, None)
                save_policy(pol)
                # abstain = 基础设施没供上样本 (tick 不动/uptime 不够), 不是跑了
                # 一轮 —— 不吃预算 (与 REFUSED 同构), 真正的上限是 max_attempts。
                # 2026-11-11 修: 这里原先 done += 1, 与上面打印的"不消耗预算"
                # 自相矛盾; ego 8 分钟/tick 的慢夜里 3 次 abstain 就烧光 budget
                # 退出, 一个 trial 都没测。参数降权已由 update_policy(None) 做。
                print(f"[sweep] ABSTAIN {pick} -> v{pol['version']}（不消耗预算，本参数降权）")
                continue
            if row.get("ok") is False:
                pol = update_policy(pol, pick, row)
                save_policy(pol)
                print(f"[sweep] REFUSED {pick}: {row.get('reason')} —— 不消耗预算，5 分钟后再试")
                time.sleep(300)
                continue
            row["policy_version"] = pol["version"]
            row["mode"] = mode
            if "model" in row:
                pol = update_policy(pol, pick, {**row["model"], "at_bound": row.get("at_bound")})
                save_policy(pol)
                print(f"[sweep] policy -> v{pol['version']}")
            done += 1
        if done < budget:
            print(f"[sweep] 提前收工: {done}/{budget} 轮完成，{attempts} 次尝试"
                  f"（观察窗未闭时这是预期行为，不是失败）")
        report(pol)
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
