#!/usr/bin/env python3
"""test_live_param_keys.py — 校验 tl_ego_server._live_param_keys 的"有哪些消费者"判定。

为什么要有它: /apply-parameter 能改 22 个白名单键, 但其中一部分全机没有任何读取方 —— 改它们
等于没改 (2026-10-16 实测 10+ 次这样的"假升级", 执行器 footer 至今还在推荐其中一个)。
本测试把服务端里的真实函数抽出来跑, 防止白名单/接线状况变了而判定悄悄失准。

跑法: python -B tests/test_live_param_keys.py   (断言式, 无框架, 不碰网络)
"""
import re
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "tl_ego_server.py"
src = SERVER.read_text(encoding="utf-8", errors="replace")


def _extract_fn(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^def |^class |\Z)", src, re.S | re.M)
    assert m, f"找不到 {name}"
    return m.group(0)


def _const(name: str) -> str:
    m = re.search(rf"^{name} = \{{.*?^\}}", src, re.S | re.M)
    assert m, f"找不到 {name}"
    return m.group(0)


ns = {"re": re, "time": time, "Path": Path, "__file__": str(SERVER),
      "_LIVE_PARAM_CACHE": None}   # 模块级缓存变量(服务端有, 这里补桩)
exec(_const("PARAM_WHITELIST"), ns)          # noqa: S102 - 抽真实常量, 不重抄
exec(_const("PARAM_RANGES"), ns)
exec(_extract_fn("_live_param_keys"), ns)
live = set(ns["_live_param_keys"]())
wl = set(ns["PARAM_WHITELIST"])

assert live <= wl, f"live 不在白名单: {live - wl}"
for k in ("stdp_learning_rate", "kwta_pool_mult", "stagnation_threshold", "evidence_persist_ticks",
          "integrator_concentration_floor"):
    assert k in live, f"{k} 掉了 (判定漏杀 live 键)"
# 2026-10-16 实测全机无消费者的键 —— 若某天真接线了, 这里失败就是信号: 更新期望, 别当噪声
for k in ("attractor_threshold", "novelty_drive_weight", "competition_floor", "counterfactual_top_n"):
    assert k not in live, f"{k} 已接线? 更新本测试 + _recent_web_knowledge_note 的说明"
assert len(live) >= 8, f"live 键过少: {sorted(live)}"
print(f"[test_live_param_keys] OK: whitelist={len(wl)} live={len(live)} dead={len(wl) - len(live)}")
print("live:", ", ".join(sorted(live)))
print("dead:", ", ".join(sorted(wl - live)))

# Regression for the newly granted floor: it must change activation, not just
# return a boolean, and must leave the default no-op path untouched.
_ns2 = {}
exec(_extract_fn("_apply_integrator_concentration_floor"), _ns2)
_apply_floor = _ns2["_apply_integrator_concentration_floor"]
assert _apply_floor({"integrator": 1.0, "sensory": 9.0}, 0.0) is False
_acts = {"integrator": 1.0, "sensory": 9.0}
assert _apply_floor(_acts, 0.25) is True
_share = _acts["integrator"] / sum(_acts.values())
assert abs(_share - 0.25) < 1e-9, (_acts, _share)
_acts2 = {"integrator": 5.0, "sensory": 5.0}
assert _apply_floor(_acts2, 0.25) is False and _acts2["integrator"] == 5.0
print("[test_live_param_keys] floor helper OK")
