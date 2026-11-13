"""单条可运行检查: "L10 候选 -> 白名单参数" 这条接线还在不在。

2026-10-15 接的线: 新形状候选(strategy/code_anchor, 无 dsl) -> 参数处方
(param_proposed) -> POST /apply-parameter -> _apply_runtime_param(白名单+范围+
审计+重启重放) -> 消费点 tl_ego_server._resting_tick_impl 的 _resting_top_k。

断任何一环这个测试就该红, 不要放宽它:
    1. 白名单/范围里没有 kwta_resting_top_k  -> 处方永远被 403
    2. 消费点不再读 RESTING_STATE            -> 参数写进内存但没人用(=假接线)
    3. bridge 不再给新形状候选出口           -> 整族候选静默消失
    4. evo_auto_round 不再有参数落地腿       -> 处方躺在日志里没人执行

跑法:  py -3.12 -B tests/test_l10_param_wiring.py     (stdlib only, 不碰网络)
"""
import importlib.util
import re
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
SERVER = PROJ / "tl_ego_server.py"
BRIDGE = PROJ / "l10" / "l10_l5_patch_bridge.py"
AUTO_ROUND = PROJ / "evo_auto_round.py"

KEY = "kwta_pool_mult"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_landing_pad():
    txt = SERVER.read_text(encoding="utf-8", errors="replace")
    block = re.search(r"PARAM_WHITELIST\s*=\s*\{(.*?)\n\}", txt, re.S)
    assert block, "PARAM_WHITELIST block not found"
    assert f'"{KEY}"' in block.group(1), f"{KEY} missing from PARAM_WHITELIST"
    assert re.search(rf'"{KEY}"\s*:\s*\(\s*[\d.]+\s*,\s*[\d.]+\s*\)', txt), \
        f"{KEY} missing from PARAM_RANGES"
    i = txt.find("EGO_RESTING_KWTA_TOP_K")
    assert i > 0, "resting top_k consumer not found"
    assert f'RESTING_STATE.get("{KEY}")' in txt[max(0, i - 900):i], \
        "consumer no longer reads the param -> param would be a fake landing pad"
    assert "_resting_base * float(_p_mult)" in txt[i:i + 400], \
        "consumer no longer multiplies the baseline (semantics changed: check the denominator)"


def test_bridge_emits_and_clamps():
    b = _load("l10_l5_patch_bridge_check", BRIDGE)
    wl, rng = b._whitelist_and_ranges()
    assert KEY in wl, f"{KEY} not in parsed whitelist"
    assert KEY in rng, f"{KEY} not in parsed ranges"

    found = b.latest_triggered_candidate()
    assert found and found.get("candidate"), "generator log has no triggered candidate"
    strategy = found["candidate"].get("strategy") or {}
    stype = str(strategy.get("type") or "")
    mapped = b.STRATEGY_PARAM_MAP.get(stype)
    assert mapped, f"real candidate strategy '{stype}' has no landing pad in the map"
    assert b._value_from_strategy(strategy, mapped[1]) is not None, \
        f"real candidate carries no value in the accepted form {mapped[1]}: {strategy.get('s2_value')!r}"

    captured = []
    b._append_proposal_log = lambda rec: captured.append(rec)   # 不污染生产提案日志
    assert b.propose_param(found) == 0
    assert captured, "propose_param wrote nothing"
    rec = captured[-1]
    assert rec["status"] == "param_proposed", f"expected param_proposed, got {rec}"
    p = rec["param"]
    lo, hi = rng[KEY]
    assert p["key"] == KEY and lo <= p["value"] <= hi, f"out of range: {p}"


def test_auto_round_has_param_leg():
    txt = AUTO_ROUND.read_text(encoding="utf-8", errors="replace")
    assert '"param_proposed"' in txt, "evo_auto_round no longer handles param_proposed"
    assert "def _apply_param" in txt and "/apply-parameter" in txt, \
        "param apply leg missing -> proposals would never be executed"


if __name__ == "__main__":
    for fn in (test_landing_pad, test_bridge_emits_and_clamps, test_auto_round_has_param_leg):
        fn()
        print(f"[ok] {fn.__name__}")
    print("[ok] L10 -> param wiring intact")
