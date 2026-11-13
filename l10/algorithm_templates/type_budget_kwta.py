"""
TypeBudgetKWTA — 显式类型预算版 KWTA 算法模板

接口约定:
  apply(type_counts: dict[str, int], context: dict) -> dict[str, int]

语义:
  在标准 KWTA 竞争后再施加一层“类型预算”门控：
  - 每个类型有一个预算上限 budget = round(total * ratio)
  - 若某类型激活数超过预算，则超出部分被抑制并重新分配给最久未激活的类型
  - noise_scale 控制随机抖动，避免完全确定性垄断
  - decay_rate 控制预算随时间的收缩（避免新类型永远无法退出）
"""

from __future__ import annotations

import random
from typing import Dict


class TypeBudgetKWTA:
    def __init__(
        self,
        type_budget: int = 8,
        noise_scale: float = 0.05,
        decay_rate: float = 0.92,
    ) -> None:
        self.type_budget = int(type_budget)
        self.noise_scale = float(noise_scale)
        self.decay_rate = float(decay_rate)
        self._last_activation_tick: Dict[str, int] = {}
        self._tick = 0

    def apply(self, type_counts: Dict[str, int], context: Dict | None = None) -> Dict[str, int]:
        ctx = context or {}
        total = int(ctx.get("n_active", sum(type_counts.values())))
        self._tick += 1
        result = dict(type_counts)

        # 噪声扰动（只加给低激活类型，避免放大垄断）
        noise = {}
        for t, c in result.items():
            if c == 0:
                noise[t] = 0.0
                continue
            # 激活越少，噪声相对越大
            scale = self.noise_scale * (1.0 / max(c, 1))
            noise[t] = random.uniform(-scale, scale)
        for t in result:
            result[t] = max(0, result[t] + int(round(noise.get(t, 0.0))))

        # 类型预算门控: type_budget 是唯一硬顶。
        # 旧实现拿 self._budgets 的运行均值当预算, 均值恒等于 fair share,
        # 于是门控退化成 no-op (线上实测 38/39 次 100->100)。
        # ponytail: 单轮轮转分配, 无处可分的 excess 直接抑制(总数收缩)——
        # 别为保总数把 excess 倒回垄断类型(旧实现的第二个 bug: 换了宿主的垄断)。
        for t, c in list(result.items()):
            if c <= 0:
                continue
            budget = max(1, int(self.type_budget))
            if c <= budget:
                continue
            excess = c - budget
            result[t] = budget
            room = sorted((x for x in result if x != t and result[x] < budget),
                          key=lambda x: self._last_activation_tick.get(x, 0))
            for x in room:
                if excess <= 0:
                    break
                take = min(excess, budget - result[x])
                result[x] += take
                excess -= take

        # 记录本次激活时间
        for t, c in result.items():
            if c > 0:
                self._last_activation_tick[t] = self._tick

        return result
