"""
L10 Meta-Algorithm Generator
============================
从 TL-Ego 元认知诊断到"创造新算法候选"的完整链路。

核心机制：用"认知分歧"作为创造引擎
  S1 = TL-Ego 当前策略（从自身模块读取）
  S2 = EvoLver 的 gap→patch 思路
  S1 ≠ S2 = 分歧信号 → 驱动新算法生成

触发条件（信号驱动，非定时）：
  - dead_types ≥ 3
  - 自循环 streak ≥ 8 tick
  - dwell_reason 同类型 > 60%
  - metacontemplate 连续 3 次 S1=S2（认知"闭关锁国"）
  - 冷却期 COOLDOWN_TICKS（防震荡）
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

_WORKSPACE = Path(__file__).resolve().parent.parent.parent.parent
_LAE = _WORKSPACE / ".lae"
_GENERATOR_LOG = _LAE / "l10_meta_algorithm_generator.jsonl"
_GENERATOR_LOG.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class GeneratorConfig:
    """L10 Meta-Algorithm Generator 配置"""

    # 信号触发阈值（全是信号，非定时器）
    # ── 2026-09-02 lobster B 方案: 调低阈值，让 L10 在 ego 健康态也能继续探索 ──
    # 原阈值在 integrator 82% 锁定 / dead_types=4 设计, 当前 ego 已脱离该状态
    # (tick=6498, stagnation_streak=0, top type 19%, 14 类型全激活), 原阈值过高
    # 导致 generator 永不触发。温和放宽:
    #   dead_types 3→2, self_loop_streak 8→4, dwell 0.60→0.40, dissensus 3→2
    dead_types_threshold: int = 2
    self_loop_streak: int = 4
    dwell_overrely_ratio: float = 0.40
    dissensus_streak: int = 2

    # 冷却期（上一次生成后 N tick 不再触发，防震荡）
    # 50→30 tick：原 cooldown 在低频触发下基本等价于锁死整个 generator
    cooldown_ticks: int = 30

    # 最小分歧分数（低于此值认为 S1≈S2，不生成）
    # 0.30→0.20：允许更温和的探索性候选进入评估（仍由 L5 apply 闸门把关真实改动）
    min_dissensus_score: float = 0.20


    # 路径
    log_path: str = field(default_factory=lambda: str(_GENERATOR_LOG))
    observer_jsonl: str = field(default_factory=lambda: str(_LAE / "l10_observer.jsonl"))
    injection_jsonl: str = field(default_factory=lambda: str(_LAE / "l10_diversity_injection.jsonl"))


@dataclass
class BottleneckProfile:
    """瓶颈画像（来自 L10 observer）"""
    dead_type_count: int = 0
    dead_types: list[str] = field(default_factory=list)
    self_loop_streak: int = 0
    self_loop_type: Optional[str] = None
    dwell_overrely_ratio: float = 0.0
    dwell_top_type: Optional[str] = None
    primary_bottleneck: Optional[str] = None
    metacontemplate_same_s1s2: int = 0   # 连续 S1=S2 次数
    transition_diversity: float = 0.0
    confidence: float = 0.0
    # 2026-09-02: 从实际代码读取的当前状态（锚点同步）
    current_top_k: int = 1000
    newborn_types: set[str] = field(default_factory=lambda: {'doubt', 'veto', 'rule_modification'})
    low_edge_types: set[str] = field(default_factory=lambda: {'curiosity'})
    newborn_boost_floor: float = 1.5


@dataclass
class DissensusPair:
    """认知分歧对：S1 = Ego 当前策略，S2 = 替代方案"""
    s1_label: str
    s1_approach: str          # Ego 当前做了什么
    s2_label: str
    s2_approach: str          # EvoLver/模块扫描建议做什么
    score: float              # 分歧分数 0~1（越高分歧越大）
    rationale: str            # 为什么这两个方向有分歧


# ── QD 档案回喂 (2026-09-11) ─────────────────────────────────────────────────
# 进化轮把每个候选族的真实分数喂进 .lae/qd_archive/qd_archive.sqlite。若档案里
# 所有族都已到顶（best_fitness >= 1.0），继续提议同一族只是换参数重排、不产生新
# 信息 —— 实测后果：deepening_existing 分歧对分数被 dead_types 钉死在 1.0，连续
# 触发记录里只有 3 个算术检测器族。饱和时降权该族，改推 closed_self_reflection
# （产出 algorithm_selection + TypeBudgetKWTA DSL = 新族，走 l10_dsl_compiler）。

QD_ARCHIVE_SQLITE = _LAE / "qd_archive" / "qd_archive.sqlite"
QD_SATURATION_FITNESS = 0.999


def current_suite_prefix() -> str:
    """套件前缀 = 档案最后写入的那一行所属套件（推导，绝不写死字面量）。

    旧实现把默认值写死成 "hidden_v6"。v6 于 2026-09-22 打满 1.0 后退役，
    这个字面量却一直在读 v6 的格子 → saturated 恒 True → apply_qd_pressure
    把 deepening_existing 分数归零 → 生成器只剩人造的 closed_self_reflection
    对 → 每次只产出 algorithm_selection 死端。实测 2026-11-11：v6 best=1.0，
    现役 v7 best=0.8776，四个真实族（含新加的 curiosity 族）全部被挡在门外。
    每次换套件都靠人工改这个字面量，改漏一次就整环冻结 —— 改成从档案推导，
    换套件时自动跟随，没有可漏的字面量。

    niche 形如 "0|0|9|0|0|0|hidden_v7:kwta_threshold_expansion"：冒号前是 QD
    网格坐标 + 套件名。取冒号前最后一段 = 裸套件名（与旧字面量同形），坐标不
    参与分组，否则同一套件的格子会按坐标被拆散、少算 cells。"""
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{QD_ARCHIVE_SQLITE}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT niche FROM archive ORDER BY added_at DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        desc = str(row[0] or "").split(":", 1)[0] if row else ""
        return desc.rsplit("|", 1)[-1].strip()
    except Exception:
        return ""


def qd_saturation(suite_prefix: str = "") -> dict:
    """读进化轮 QD 档案的饱和状态（按套件前缀分组）。

    饱和只对**当前**套件有意义：旧套件的 1.0 是另一把尺子。前缀默认由
    current_suite_prefix() 从档案推导，不再有需要与别处同步的字面量
    （见该函数注释：写死的 "hidden_v6" 冻结了 2026-09-22 → 11-11 全部轮次）。
    档案不存在/读不到 = 未饱和（行为与从前一致）。"""
    prefix = suite_prefix or current_suite_prefix()
    if not prefix:
        return {"saturated": False, "cells": 0, "best": 0.0, "suite_prefix": ""}
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{QD_ARCHIVE_SQLITE}?mode=ro", uri=True)
        try:
            cells, best = con.execute(
                "SELECT COUNT(*), COALESCE(MAX(fitness), 0) FROM archive "
                "WHERE niche LIKE ?",
                (f"%{prefix}:%",),
            ).fetchone()
        finally:
            con.close()
        return {"saturated": int(cells) >= 2 and float(best) >= QD_SATURATION_FITNESS,
                "cells": int(cells), "best": float(best),
                "suite_prefix": prefix}
    except Exception:
        return {"saturated": False, "cells": 0, "best": 0.0, "suite_prefix": prefix}

def apply_qd_pressure(pairs: list[DissensusPair], saturation: dict) -> list[DissensusPair]:
    """档案已饱和时：降权已到顶的族（deepening_existing），并保证新族进入提案。"""
    if not saturation.get("saturated"):
        return pairs
    for p in pairs:
        if p.s1_label == "deepening_existing":
            p.score = 0.0   # 被 min_dissensus_score 过滤：已到顶的族不再重排
    if not any(p.s1_label == "closed_self_reflection" for p in pairs):
        pairs.append(DissensusPair(
            s1_label="closed_self_reflection",
            s1_approach="QD 档案饱和：已有族全部到顶（微准确率 1.0），重排旧检测器不再产生信息",
            s2_label="external_perspective",
            s2_approach="换族：生成新算法（TypeBudgetKWTA 显式类型预算）而不是调节旧检测器优先级",
            score=0.5,
            rationale="饱和反馈：同族重排的边际收益为 0，只有新族携带新信息",
        ))
    return pairs


# ─── 族注册表 (2026-11-11) ─────────────────────────────────────────────
# 生成器的族集此前是代码里的 if/elif 分支 —— 只有人能加族, 所以"改进的产物"永远
# 不是"改进能力本身"。这里把族变成**数据**: .lae/l10_family_registry.json 里的行
# 由 L5 目标 (ego 自己的执行器) 写入, 生成器每轮读一遍, 条件成立才产出候选。
# 写者是 LLM ⇒ 这里是信任边界: 行必须过字段校验, 且一律强制 shadow_only
# (只进提案日志被评估, 绝不进运行时 applier)。行有问题就丢弃并记账, 不猜不补。
FAMILY_REGISTRY_DIR = _LAE / "l10_family_registry"   # 一目标一文件, 不做共享 RMW
_REGISTRY_CAP = 8          # 一轮最多吃进来的族行数, 防止注册表失控拖慢 tick
_REG_REJECTS: list[dict] = []


def _iter_registry_rows() -> list[tuple[str, Any]]:
    """(来源标识, 行)。一目标一文件: 两个目标并行写不会互相覆盖, 也没有读写竞态;
    坏文件记一笔跳过, 绝不因为一个坏行让整轮生成失败。"""
    rows: list[tuple[str, Any]] = []
    try:
        paths = sorted(FAMILY_REGISTRY_DIR.glob("*.json"))[:32]
    except Exception as exc:
        _REG_REJECTS.append({"index": None, "reason": f"unreadable registry dir: {exc}"})
        return rows
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            _REG_REJECTS.append({"index": p.name, "reason": f"unreadable file: {exc}"})
            continue
        if isinstance(data, list):
            rows.extend((f"{p.name}#{i}", r) for i, r in enumerate(data))
        elif isinstance(data, dict):
            rows.append((p.name, data))
        else:
            _REG_REJECTS.append({"index": p.name, "reason": "file must hold an object or a list"})
    return rows


def _registry_row_problem(row: Any, profile: "BottleneckProfile") -> Optional[str]:
    """行不合法/条件不成立 → 返回原因; 合法且命中 → None。"""
    if not isinstance(row, dict):
        return "row is not an object"
    name = row.get("type")
    if not isinstance(name, str) or not (3 <= len(name) <= 48) or not name.isidentifier() \
            or name != name.lower():
        return "type must be lowercase snake_case (3-48 chars)"
    target = row.get("target")
    if not isinstance(target, str) or not target.strip():
        return "target (runtime param key) required"
    value = row.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "value must be a number"
    if value != value or value in (float("inf"), float("-inf")):
        return "value must be finite"
    if not str(row.get("hypothesis") or "").strip():
        return "hypothesis required"
    when = row.get("when") or {}
    if not isinstance(when, dict):
        return "when must be an object"
    unknown = set(when) - {"dead_types_any", "dead_type_count_min"}
    if unknown:
        return f"unsupported when keys {sorted(unknown)}"
    wanted = when.get("dead_types_any") or []
    if not isinstance(wanted, list) or any(not isinstance(t, str) for t in wanted):
        return "when.dead_types_any must be a list of type names"
    dead = set(profile.dead_types or ())
    if wanted and not any(t in dead for t in wanted):
        return "condition not met (dead_types_any)"
    try:
        floor = int(when.get("dead_type_count_min") or 0)
    except (TypeError, ValueError):
        return "when.dead_type_count_min must be an int"
    if profile.dead_type_count < floor:
        return "condition not met (dead_type_count_min)"
    return None


def registry_families(profile: "BottleneckProfile",
                      pair_label: str = "deepening_existing") -> list[dict]:
    """注册表里命中当前画像的族行 → 策略 dict（形状与内置族一致, 强制 shadow）。"""
    _REG_REJECTS.clear()
    rows = _iter_registry_rows()
    out: list[dict] = []
    for i, row in rows:
        if not isinstance(row, dict):
            _REG_REJECTS.append({"index": i, "type": None, "reason": "row is not an object"})
            continue
        if (row.get("pair") or "deepening_existing") != pair_label:
            continue
        problem = _registry_row_problem(row, profile)
        if problem:
            _REG_REJECTS.append({"index": i, "type": row.get("type") if isinstance(row, dict) else None,
                                 "reason": problem})
            continue
        key = str(row["target"]).strip()
        val = float(row["value"])
        out.append({
            "type": str(row["type"]),
            "operation": "shadow_parameter",
            "target": key,
            "s1_value": str(row.get("s1_value") or f"{key} = [current]"),
            "s2_value": str(row.get("s2_value") or f"{key} = {val:g} [candidate]"),
            "hypothesis": str(row["hypothesis"]),
            "shadow_only": True,
            "shadow_param": {"key": key, "value": val},
            "evidence": row.get("evidence") or {},
            "provenance": {"source": "family_registry",
                           "author": row.get("author"),
                           "registry_index": i},
        })
        if len(out) >= _REGISTRY_CAP:
            break
    return out


# ─── 代码锚点映射层 ──────────────────────────────────────────
# 每个策略 target 指向真实代码位置（file + 唯一锚点文本 + 当前值）。
# 候选携带 code_anchor 后，bridge/人工才能把“策略意图”翻译成精确 patch。
# 锚点全部取自 tl_ego_server.py 已确认唯一（count==1）的文本行。
_CODE_ANCHORS: dict[str, dict] = {
    "kwta_top_k_ratio": {
        "file": "projects/tl-ego/tl_ego_server.py",
        "anchor": "top_k = max(50, min(2000, int(_n_active * 2.5 - 12)))",
        "current_value": "top_k = max(50, min(2000, int(_n_active*2.5 - 12)))",
        "change_description": "KWTA 候选池上限：扩大可让死类型进入竞争（风险：过度分散浓度）",
    },
    "hp_types_set": {
        "file": "projects/tl-ego/tl_ego_server.py",
        "anchor": '_hp_types = {"integrator", "rule_modification"} if _int_press_ctr == 0 else {"integrator"}',
        "current_value": "integrator(+rule_modification) 恒为高优先级",
        "change_description": "高优先级类型集：轮换/加入死类型可打破 integrator 垄断（integrator_suppression 系列）",
    },
    "dq_self_loop_threshold": {
        "file": "projects/tl-ego/l10/diversity_quota_operator.py",
        "anchor": "SELF_LOOP_THRESHOLD = 5  # 自循环触发惩罚的连续帧数",
        "current_value": "SELF_LOOP_THRESHOLD = 5",
        "change_description": "自循环惩罚触发阈值：调低更早打断循环（DQO v0.1 参数）",
    },
    "dq_quota_window": {
        "file": "projects/tl-ego/l10/diversity_quota_operator.py",
        "anchor": "QUOTA_WINDOW      = 20   # 多样性检测窗口",
        "current_value": "QUOTA_WINDOW = 20",
        "change_description": "多样性配额检测窗口：调小更灵敏，调大更平滑",
    },
    "newborn_boost_decay": {
        "file": "projects/tl-ego/tl_ego_server.py",
        "anchor": "_newborn_boost = max(1.5, 5.0 - 0.01 * _current_tick)  # H1: 3.0鈫?.0, 350-tick decay to floor",
        "current_value": "max(1.5, 5.0 - 0.01*tick) 从 5.0 衰减到 1.5",
        "change_description": "新生类型 boost：提高 floor/衰减率可让新类型更久保持竞争力",
    },
}


class MetaAlgorithmGenerator:
    """
    L10 Meta-Algorithm Generator

    用法：
        gen = MetaAlgorithmGenerator()
        decision = gen.should_trigger(profile)
        if decision["triggered"]:
            result = gen.generate(profile)
            gen.write_result(result)
    """

    def __init__(self, config: Optional[GeneratorConfig] = None):
        self.config = config or GeneratorConfig()
        self._last_trigger_tick: Optional[int] = None
        self._last_trigger_reason: Optional[str] = None

    # ─── 信号触发判断 ────────────────────────────────────────────

    def should_trigger(self, profile: BottleneckProfile, current_tick: Optional[int] = None) -> dict:
        """纯信号判断，无定时器。多个信号叠加提高触发置信度."""
        now_tick = current_tick if current_tick is not None else self._current_tick()

        # 冷却期检查
        if self._last_trigger_tick is not None:
            if now_tick - self._last_trigger_tick < self.config.cooldown_ticks:
                return {
                    "triggered": False,
                    "reason": f"cooldown ({now_tick - self._last_trigger_tick}/{self.config.cooldown_ticks} tick remaining)",
                    "signals": {},
                }

        signals: dict[str, dict] = {}
        triggered_by = []

        if profile.dead_type_count >= self.config.dead_types_threshold:
            signals["dead_types"] = {
                "value": profile.dead_type_count,
                "threshold": self.config.dead_types_threshold,
            }
            triggered_by.append("dead_types")

        if profile.self_loop_streak >= self.config.self_loop_streak:
            signals["self_loop"] = {
                "value": profile.self_loop_streak,
                "type": profile.self_loop_type,
                "threshold": self.config.self_loop_streak,
            }
            triggered_by.append("self_loop")

        if profile.dwell_overrely_ratio >= self.config.dwell_overrely_ratio:
            signals["dwell_overrely"] = {
                "value": round(profile.dwell_overrely_ratio, 3),
                "top_type": profile.dwell_top_type,
                "threshold": self.config.dwell_overrely_ratio,
            }
            triggered_by.append("dwell_overrely")

        if profile.metacontemplate_same_s1s2 >= self.config.dissensus_streak:
            signals["dissensus_streak"] = {
                "value": profile.metacontemplate_same_s1s2,
                "threshold": self.config.dissensus_streak,
            }
            triggered_by.append("dissensus_streak")

        triggered = len(triggered_by) > 0

        if triggered:
            self._last_trigger_tick = now_tick
            self._last_trigger_reason = "+".join(triggered_by)

        return {
            "triggered": triggered,
            "reason": self._last_trigger_reason if triggered else "no_signals",
            "signals": signals,
            "trigger_count": len(triggered_by),
        }

    # ─── 瓶颈→认知分歧映射 ────────────────────────────────────────

    def build_dissensus_pairs(self, profile: BottleneckProfile) -> list[DissensusPair]:
        """
        将 L10 瓶颈诊断映射为认知分歧对（S1 vs S2）。
        这是"创造新算法"的核心：找到 TL-Ego 当前策略和替代策略的对立。
        """
        pairs: list[DissensusPair] = []

        # ── 分歧对 1：死类型 → "扩展候选池" vs "加深现有类型" ──
        if profile.dead_type_count >= self.config.dead_types_threshold:
            dead_types_str = ", ".join(profile.dead_types[:5])
            pairs.append(DissensusPair(
                s1_label="deepening_existing",
                s1_approach=f"当前策略：top_k={profile.current_top_k}，NEWBORN_TYPES={profile.newborn_types}，忽略 {profile.dead_type_count} 个死类型",
                s2_label="expanding_candidate_pool",
                s2_approach=f"扩展策略：激活 {dead_types_str} 等死类型，降低 KWTA 阈值让它们参与竞争",
                score=max(0.35, min(1.0, profile.dead_type_count / 14.0)),  # ≥threshold(3) 时保证过 min_dissensus_score(0.30); 修复评分死区 2026-08-03
                rationale=f"死类型持续为 0 说明当前 KWTA 竞争太保守，扩展候选池会产生新的激活路径",
            ))

        # ── 分歧对 2：自循环 → "强制多样性" vs "维持稳定振荡" ──
        if profile.self_loop_streak >= self.config.self_loop_streak:
            pairs.append(DissensusPair(
                s1_label="stable_oscillation",
                s1_approach=f"当前策略：{profile.self_loop_type} 连续 {profile.self_loop_streak} tick（无 SELF_LOOP_THRESHOLD 硬限制，靠 _int_press_counter 软约束）",
                s2_label="forced_diversity",
                s2_approach=f"替代策略：跳过 {profile.self_loop_type} 一轮，强制激活最久未主导的类型（DQ Quota）",
                score=min(1.0, profile.self_loop_streak / 20.0),
                rationale="维持稳定意味着停滞；强制多样性会产生 Ego 自主没有发现的激活路径",
            ))

        # ── 分歧对 3：过度依赖 → "复用成功路径" vs "探索新路径" ──
        if profile.dwell_overrely_ratio >= self.config.dwell_overrely_ratio:
            pairs.append(DissensusPair(
                s1_label="reuse_successful_path",
                s1_approach=f"当前策略：{profile.dwell_top_type} 占 {profile.dwell_overrely_ratio:.0%} 的停留原因，过度复用已知路径",
                s2_label="explore_unknown_path",
                s2_approach=f"替代策略：减少 {profile.dwell_top_type} 权重 30%，将资源导向过渡区类型",
                score=profile.dwell_overrely_ratio,
                rationale="超过 60% 依赖单一类型说明 Ego 在已知解里舒适区循环，丧失探索动力",
            ))

        # ── 分歧对 4：认知闭关 → S1=S2 连续 → 引入外部视角 ──
        if profile.metacontemplate_same_s1s2 >= self.config.dissensus_streak:
            pairs.append(DissensusPair(
                s1_label="closed_self_reflection",
                s1_approach="当前策略：metacontemplate 连续 3 次 S1=S2，认知系统在自言自语",
                s2_label="external_perspective",
                s2_approach="替代策略：引入 EvoLver 扫描或外部证据文件，迫使 S2 代表不同视角",
                score=min(1.0, profile.metacontemplate_same_s1s2 / 5.0),
                rationale="S1=S2 连续意味着认知系统自我封闭，分歧是打开它的唯一方式",
            ))

        return pairs

    # ─── 生成新算法候选 ────────────────────────────────────────

    def generate(
        self,
        profile: BottleneckProfile,
        module_map: Optional[dict[str, str]] = None,
        current_tick: Optional[int] = None,
    ) -> dict:
        """
        主入口：基于 L10 诊断生成新算法候选。

        Args:
            profile: L10 瓶颈画像
            module_map: {bottleneck_key: module_path} 定向映射
            current_tick: 外部传入的真实 tick（避免 HTTP 自调用超时返回 -1）

        Returns:
            包含 triggered / dissensus_pairs / algorithm_candidates 的字典
        """
        _effective_tick = current_tick if current_tick is not None else self._current_tick()
        trigger = self.should_trigger(profile, current_tick=current_tick)
        if not trigger["triggered"]:
            return {
                "triggered": False,
                "reason": trigger["reason"],
                "signals": trigger["signals"],
                "tick": _effective_tick,
                "timestamp": time.time(),
            }

        # ── 重复候选检查（内容指纹）─────────────────────────────
        _rf_path = _LAE / "l10_gen_recent_fingerprints.json"
        _recent_fps: set[str] = set()
        try:
            if _rf_path.exists():
                _recent_fps = set(json.loads(_rf_path.read_text(encoding="utf-8")))
        except Exception:
            pass

        # Step 1: 构建认知分歧对
        pairs = self.build_dissensus_pairs(profile)
        # QD 档案饱和 → 降权已到顶的族（2026-09-11）
        _qd = qd_saturation()
        pairs = apply_qd_pressure(pairs, _qd)

        if not pairs:
            return {
                "triggered": False,
                "reason": "no_valid_dissensus_pairs",
                "signals": trigger["signals"],
                "tick": _effective_tick,
                "timestamp": time.time(),
            }

        # Step 2: 为每个分歧对生成算法候选（策略族：1 个分歧 → 多个机制杠杆）+ 去重
        candidates = []
        new_fps: set[str] = set()
        for pair in pairs:
            if pair.score < self.config.min_dissensus_score:
                continue
            for candidate in self._draft_algorithm_candidates(pair, profile, module_map):
                fp = self._candidate_content_fp(candidate)
                if fp in _recent_fps:
                    continue  # 跳过重复内容
                candidates.append(candidate)
                new_fps.add(fp)

        if not candidates:
            return {
                "triggered": False,
                "reason": "all_candidates_duplicate_recent",
                "signals": trigger["signals"],
                "tick": _effective_tick,
                "timestamp": time.time(),
            }

        # 更新最近指纹集（保留最近 50 个）
        _recent_fps |= new_fps
        try:
            # 窗口 50→1 (2026-08-03): 候选内容由死类型集合决定,集合不变时 50 窗口导致
            # 永久 all_candidates_duplicate_recent → 影子干预永不执行 → 死类型无法激活。
            # 窗口=1 且候选数=3:每次至少 2 个候选通过 → 每轮冷却(50 tick)后周期性
            # 重新提议 → 死类型种子注入(幂等)周期性运行。
            _rf_path.write_text(
                json.dumps(list(_recent_fps)[-1:], ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

        result = {
            "triggered": True,
            "trigger_reason": trigger["reason"],
            "signals": trigger["signals"],
            "profile": asdict(profile),
            "dissensus_pairs": [asdict(p) for p in pairs],
            "algorithm_candidates": candidates,
            "qd_saturation": _qd,
            "meta_rationale": self._summarize_creation_logic(candidates),
            "tick": _effective_tick,
            "timestamp": time.time(),
        }
        return self._jsonable(result)

    @staticmethod
    def _jsonable(obj: Any) -> Any:
        """Recursively convert sets (and other non-JSON types) so result dicts
        survive json.dumps. Root cause of audit finding E: BottleneckProfile
        carries set fields; asdict() keeps them as sets → TypeError on write."""
        if isinstance(obj, dict):
            return {k: MetaAlgorithmGenerator._jsonable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [MetaAlgorithmGenerator._jsonable(v) for v in obj]
        if isinstance(obj, (set, frozenset)):
            return sorted(obj)
        return obj

    # ─── 内部：算法候选草稿（策略族）─────────────────────────────

    def _draft_algorithm_candidate(
        self,
        pair: DissensusPair,
        profile: BottleneckProfile,
        module_map: Optional[dict[str, str]] = None,
    ) -> dict:
        """兼容单候选入口：返回策略族第一个候选。"""
        return self._draft_algorithm_candidates(pair, profile, module_map)[0]

    def _draft_algorithm_candidates(
        self,
        pair: DissensusPair,
        profile: BottleneckProfile,
        module_map: Optional[dict[str, str]] = None,
    ) -> list[dict]:
        """
        将认知分歧转化为多个可验证的算法候选（策略族）。
        同一瓶颈提供多个机制杠杆，而非单一模板；
        每个候选携带 code_anchor（file+唯一锚点+当前值），
        使 bridge 能把策略意图翻译成精确 patch。
        """
        module_map = module_map or {}
        dead_head = profile.dead_types[:3] if profile.dead_types else ["unknown"]

        if pair.s1_label == "deepening_existing":
            strategies = [
                {
                    "type": "kwta_threshold_expansion",
                    "operation": "modify",
                    "target": "kwta_top_k_ratio",
                    "s1_value": "top_k = max(50, min(2000, n_active*2.5-12)) [当前]",
                    "s2_value": f"top_k = max(50, min(2400, n_active*3.0-12)) 且对死类型加权 [扩展]",
                    "hypothesis": f"KWTA 候选池扩大后，{dead_head} 获得首次激活机会",
                },
                {
                    "type": "hp_types_rotation",
                    "operation": "modify",
                    "target": "hp_types_set",
                    "s1_value": '{"integrator", "rule_modification"} [当前]',
                    "s2_value": f"每 {max(8, 20 - profile.dead_type_count)} tick 轮换加入一个死类型（先 {dead_head[0]}）",
                    "hypothesis": f"高优先级集合轮换后，{dead_head[0]} 会周期性进入核心竞争圈",
                },
                {
                    "type": "dead_type_booster_floor",
                    "operation": "modify",
                    "target": "newborn_boost_decay",
                    "s1_value": "newborn boost floor = 1.5 [当前]",
                    "s2_value": f"dead-type booster floor = 1.8（对 {dead_head} 生效）",
                    "hypothesis": f"死类型获得更高激活地板后，{dead_head} 不再被挤出竞争",
                },
            ]
            # Open a distinct, observer-gated family only when curiosity is
            # actually dead. Existing boost is 2.0; propose a bounded 2.5
            # shadow candidate, never an unconditional live write.
            if "curiosity" in profile.dead_types:
                strategies.append({
                    "type": "curiosity_activation_recovery",
                    "operation": "shadow_parameter",
                    "target": "curiosity_activation_boost",
                    "s1_value": "curiosity_activation_boost = 2.0 [current]",
                    "s2_value": "curiosity_activation_boost = 2.5 [candidate]",
                    "hypothesis": "When curiosity remains observer-dead, test a bounded 25% propagation boost",
                    "shadow_only": True,
                    "shadow_param": {"key": "curiosity_activation_boost", "value": 2.5},
                    "evidence": {"source": "l10_observer", "dead_type": "curiosity"},
                })
        elif pair.s1_label == "stable_oscillation":
            strategies = [
                {
                    "type": "diversity_quota_escalation",
                    "operation": "modify",
                    "target": "dq_self_loop_threshold",
                    "s1_value": "SELF_LOOP_THRESHOLD = 5 [当前]",
                    "s2_value": "SELF_LOOP_THRESHOLD = 3（更早打断自循环）",
                    "hypothesis": f"阈值降到 3 后，{profile.self_loop_type} 的连续 streak 会在 3 tick 内被打断",
                },
                {
                    "type": "quota_window_shrink",
                    "operation": "modify",
                    "target": "dq_quota_window",
                    "s1_value": "QUOTA_WINDOW = 20 [当前]",
                    "s2_value": "QUOTA_WINDOW = 12（更灵敏的多样性检测）",
                    "hypothesis": f"检测窗口缩短后，{profile.self_loop_type} 单型垄断在 12 tick 内即被识别",
                },
            ]
        elif pair.s1_label == "reuse_successful_path":
            strategies = [
                {
                    "type": "exploration_rate_boost",
                    "operation": "modify",
                    "target": "newborn_boost_decay",
                    "s1_value": "exploration floor = 1.5 [当前]",
                    "s2_value": f"dwell_{profile.dwell_top_type}>0.60 时探索 boost floor = 2.2 [动态]",
                    "hypothesis": f"在 {profile.dwell_top_type} 占比过高时自动提升探索地板，恢复过渡区激活",
                },
            ]
        elif pair.s1_label == "closed_self_reflection":
            strategies = [
                {
                    "type": "cross_system_perspective",
                    "operation": "inject",
                    "target": "metacontemplate_s2_source",
                    "s1_value": "S2 读取同一 NLU 模块 [当前]",
                    "s2_value": "S2 轮询 EvoLver gap list 或 MemoryBus 外部证据 [替代]",
                    "hypothesis": f"引入外部视角后，metacontemplate S1≠S2 比例 > 60%（当前 {profile.metacontemplate_same_s1s2} 次连续同向）",
                },
                {
                    "type": "algorithm_selection",
                    "operation": "generate",
                    "target": "kwta_algorithm",
                    "s1_value": "当前主策略：kwta_top_k_capping（隐式 top_k 竞争）",
                    "s2_value": "替代算法：TypeBudgetKWTA（显式类型预算 + 噪声 + 衰减）",
                    "hypothesis": "显式预算门控可减少单一类型长期垄断，死类型获得更稳定的首次激活路径",
                    "dsl": {
                        "kind": "algorithm_synthesis",
                        "bottleneck": profile.primary_bottleneck or "closed_self_reflection",
                        "target_module": "projects/tl-ego/tl_ego_server.py",
                        "algorithm": {
                            "name": "TypeBudgetKWTA",
                            "params": {
                                "type_budget": max(6, 10 - profile.dead_type_count),
                                "noise_scale": 0.05,
                                "decay_rate": 0.92,
                            },
                        },
                        "contract": {
                            "input": ["type_counts", "n_active"],
                            "output": ["modified_type_counts"],
                            "side_effects": "none",
                        },
                        "evidence": {
                            "source": "l10_observer",
                            "dead_types": profile.dead_types[:5],
                            "stagnation_streak": profile.self_loop_streak,
                            "metacontemplate_same_s1s2": profile.metacontemplate_same_s1s2,
                        },
                    },
                },
            ]
        else:
            strategies = [{"type": "unknown", "operation": "observe"}]

        # 族注册表（2026-11-11）：数据化的族行，由 L5 目标写入，条件成立才产出。
        strategies.extend(registry_families(profile, pair.s1_label))

        candidates = []
        for strategy in strategies:
            anchor = _CODE_ANCHORS.get(strategy.get("target", ""), {})
            candidate = {
                "candidate_id": f"l10-algo-{int(time.time()*1000) % 10**6}",
                "dissensus_pair": asdict(pair),
                "strategy": strategy,
                "code_anchor": anchor or None,
                "experiment_design": {
                    "duration_ticks": 50,
                    "primary_metric": strategy.get("hypothesis", ""),
                    "control": pair.s1_approach,
                    "treatment": pair.s2_approach,
                    "success_criteria": [
                        f"{profile.primary_bottleneck or 'bottleneck'} 数值改善 ≥ 15%",
                        "metacontemplate S1≠S2 比例提升",
                        "stagnation streak 归零",
                    ],
                },
                "route_to": "l5_patch_bridge",  # 明确：真实代码修改须经 l10_l5_patch_bridge
            }
            if strategy.get("type") == "algorithm_selection" and strategy.get("dsl"):
                candidate["dsl"] = strategy["dsl"]
            candidates.append(candidate)
        return candidates

    # ─── 内部：辅助方法 ─────────────────────────────────────────

    def _current_tick(self) -> int:
        """读 Ego 当前 tick"""
        try:
            import urllib.request
            req = urllib.request.Request(
                "http://127.0.0.1:8766/status",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())
                return data.get("tick", -1)
        except Exception:
            pass
        # 文件回退：服务器内嵌场景 HTTP 自调用会超时（单线程处理 resting tick），
        # bridge 会把真实 tick 持久化到 .lae/l10_gen_state.json
        try:
            _sf = _LAE / "l10_gen_state.json"
            if _sf.exists():
                return int(json.loads(_sf.read_text(encoding="utf-8")).get("last_tick", -1))
        except Exception:
            pass
        return -1

    def _in_cooldown(self) -> bool:
        if self._last_trigger_tick is None:
            return False
        return (self._current_tick() - self._last_trigger_tick) < self.config.cooldown_ticks

    @staticmethod
    def _read_current_strategy(module_key: Optional[str]) -> str:
        """从 TL-Ego 模块源码读取当前策略描述"""
        if not module_key:
            return "kwta_top_k_capping"  # 默认：当前主策略

        # 映射瓶颈 key → 模块路径 → 读取核心逻辑
        module_map = {
            "dead_types_pipeline": "projects/tl-ego/tl_ego_server.py::_type_cycle_impl",
            "integrator_bottleneck": "projects/tl-ego/tl_ego_server.py::_integrate",
            "action_selection": "projects/tl-ego/tl_ego_server.py::_action_from_decider",
        }
        path = module_map.get(module_key, "")
        return f"当前策略（读源码）：{path}" if path else "kwta_top_k_capping [默认]"

    def _evolver_scan_for_alternative(self, module_key: Optional[str]) -> str:
        """调用 EvoLver 扫描特定模块，返回替代方案方向"""
        try:
            # 调用 EvoLver 扫描
            scan_path = str(_WORKSPACE / ("projects/tl-ego" if module_key else "."))
            cmd = (
                f'H:\\unsloth_env\\Scripts\\python.exe '
                f'"{_WORKSPACE / "evolver/infra/scan.py"}" '
                f'--path "{scan_path}"'
            )
            result = os.popen(cmd).read().strip()
            return result[:300] if result else "EvoLver 扫描无产出"
        except Exception as e:
            return f"EvoLver 扫描异常: {e}"

    def _evolver_generate_patch(
        self,
        module: str,
        s1_baseline: str,
        s2_alternative: str,
    ) -> dict:
        """通过 EvoLver 生成补丁草稿"""
        try:
            # 构建任务描述给 EvoLver
            task_desc = (
                f"模块: {module}\n"
                f"当前策略 (S1): {s1_baseline}\n"
                f"替代策略 (S2): {s2_alternative}\n"
                f"任务: 生成使 S2 生效的代码补丁，保持与 S1 的接口兼容"
            )
            return {"task": task_desc, "status": "drafted", "route": "evolver_orchestrator"}
        except Exception as e:
            return {"task": "", "status": "error", "error": str(e)}

    @staticmethod
    def _candidate_content_fp(candidate: dict) -> str:
        """候选算法内容指纹（不含 candidate_id；按 策略类型+target 去重，
        避免同一机制仅数字不同被当成新候选刷屏）"""
        import hashlib
        s = candidate.get("strategy", {})
        parts = [
            s.get("type", ""),
            s.get("target", ""),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    @staticmethod
    def _dissensus_score(s1: str, s2: str) -> float:
        """S1/S2 分歧评分：关键词重叠度越低 → 分歧越大"""
        if not s1 or not s2:
            return 0.0
        w1 = set(s1.lower().split())
        w2 = set(s2.lower().split())
        if not w1 or not w2:
            return 0.0
        overlap = len(w1 & w2)
        union = len(w1 | w2)
        return round(1.0 - (overlap / union), 4)

    @staticmethod
    def _explain_creation_logic(s1, s2):
        return "L10 creation logic: S1!=S2 dissensus detected, using as generation signal."


    @staticmethod
    def _summarize_creation_logic(candidates: list[dict]) -> str:
        if not candidates:
            return "无有效候选"
        strategies = [c["strategy"]["type"] for c in candidates]
        return f"生成 {len(candidates)} 个算法候选（{', '.join(strategies)}），均基于认知分歧 S1≠S2，走 L9 沙盒验证"

    # ─── 日志 ─────────────────────────────────────────────────

    def write_result(self, result: dict) -> None:
        """持久化生成结果到 jsonl（由 bridge 在 DSL compile 之后调用）。"""
        self._write_log(result)

    def _write_log(self, result: dict) -> None:
        try:
            with open(self.config.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            # Audit finding E: silent swallow hid a deterministic serialization
            # failure for months. Make write failure visible, never raise into tick loop.
            print(f"[l10-generator] WARNING: log write failed for tick={result.get('tick')}: "
                  f"{type(e).__name__}: {e}", flush=True)

    # ─── 便捷入口 ─────────────────────────────────────────────

    @classmethod
    def from_l10_profile(cls, profile_dict: dict) -> dict:
        """
        最简入口：直接从 L10 observer 输出字典触发。

        Usage:
            from l10_meta_algorithm_generator import MetaAlgorithmGenerator
            profile = load_l10_profile()
            result = MetaAlgorithmGenerator.from_l10_profile(profile)
        """
        profile = BottleneckProfile(
            dead_type_count=profile_dict.get("dead_type_count", 0),
            dead_types=profile_dict.get("dead_types", []),
            self_loop_streak=profile_dict.get("self_loop_streak", 0),
            self_loop_type=profile_dict.get("self_loop_type"),
            dwell_overrely_ratio=profile_dict.get("dwell_overrely_ratio", 0.0),
            dwell_top_type=profile_dict.get("dwell_top_type"),
            primary_bottleneck=profile_dict.get("primary_bottleneck"),
            metacontemplate_same_s1s2=profile_dict.get("metacontemplate_same_s1s2", 0),
            transition_diversity=profile_dict.get("transition_diversity", 0.0),
            confidence=profile_dict.get("confidence", 0.0),
        )
        gen = cls()
        return gen.generate(profile)

