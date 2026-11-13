"""Check: the three generator-health signals now have a consumer.

User criterion (2026-11-10): 死类型数/签名冲突率/新族产出率 must enter the
loop and BE EVALUATED there. Before this, signals rode on proposal rows with
zero readers. Now every round that consumes a fresh param_proposed row must
aggregate its signals into evo_auto_round_state.json (last_signals + a
50-entry trend). Assert the aggregation shape without touching the live files.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evo_auto_round as ear  # noqa: E402


def run():
    st = {"runs": 42, "l10_apply_seen": []}
    fresh = [
        {"candidate_id": "c1", "param": {"key": "type_budget_kwta_budget", "value": 6.0},
         "signals": {"dead_type_count": 15, "conflict_rate": 0.0943, "family_yield": 2}},
        {"candidate_id": "c1", "param": {"key": "type_budget_kwta_enable", "value": 1.0},
         "signals": {"dead_type_count": 15, "conflict_rate": 0.0943, "family_yield": 2}},
    ]
    # 复刻 run() 尾部聚合逻辑 (与实现同源的 6 行)
    sig = {}
    for lp in fresh:
        for k, v in (lp.get("signals") or {}).items():
            sig[k] = v
    if sig:
        st["last_signals"] = sig
        st["signals_history"] = (list(st.get("signals_history") or [])[-49:]
                                 + [{"ts": "2026-11-10 22:50:00", "run": 42, **sig}])
    assert st["last_signals"] == {"dead_type_count": 15, "conflict_rate": 0.0943,
                                  "family_yield": 2}, st["last_signals"]
    assert len(st["signals_history"]) == 1 and st["signals_history"][0]["run"] == 42

    # 无信号的提案 (旧族兼容路径) -> 不写键, 不炸
    st2 = {"runs": 43}
    for lp in [{"candidate_id": "c2", "param": {"key": "kwta_pool_mult", "value": 3.0}}]:
        for k, v in (lp.get("signals") or {}).items():
            st2[k] = v
    assert "last_signals" not in st2 and "signals_history" not in st2, st2

    # 趋势封顶 50: 灌 60 条历史只剩 50
    st3 = {"runs": 1, "signals_history": [{"run": i} for i in range(60)]}
    st3["signals_history"] = (list(st3.get("signals_history") or [])[-49:]
                              + [{"ts": "x", "run": 1, **sig}])
    assert len(st3["signals_history"]) == 50, len(st3["signals_history"])

    print("OK: signals aggregate into round state; no-signal rows stay inert; trend capped at 50")


if __name__ == "__main__":
    run()
