"""L2 bridge check: generator new-family candidate -> whitelisted param proposal.

Covers the 2026-11-10 patch to l10/l10_l5_patch_bridge.py:
  1. algorithm_selection (TypeBudgetKWTA) lands as type_budget_kwta_budget
     + paired type_budget_kwta_enable, value read from strategy.dsl.params
  2. missing dsl -> param_unmappable, no invented number
  3. out-of-range value clamps to PARAM_RANGES
  4. old found shape ({"candidate": ...} only) still proposes
  5. signals (dead_type_count / family_yield / conflict_rate) ride on every row
  6. dead-curiosity recovery lands on the live curiosity_activation_boost key
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "l10"))
import l10_l5_patch_bridge as bridge  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import evo_auto_round as auto_round  # noqa: E402


def _rows(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def run() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="l10bridge_"))
    bridge.PROPOSAL_LOG = tmp / "proposals.jsonl"
    bridge.PROPOSAL_LOG.write_text(
        json.dumps({"status": "appended_shadow_unwired"}) + "\n"
        + json.dumps({"status": "param_proposed"}) + "\n", encoding="utf-8")

    kwta_cand = {"candidate_id": "c-kwta", "strategy": {
        "type": "kwta_threshold_expansion",
        "s2_value": "expand pool: n_active*3.0", "hypothesis": "x"}}
    tbkwta_cand = {"candidate_id": "c-tb", "strategy": {
        "type": "algorithm_selection", "target": "kwta_algorithm",
        # real shape measured from generator log tick 70103: dsl.algorithm.{name,params}
        "dsl": {"kind": "algorithm_synthesis", "target_module": "projects/tl-ego/tl_ego_server.py",
                "algorithm": {"name": "TypeBudgetKWTA",
                              "params": {"type_budget": 7, "noise_scale": 0.05, "decay_rate": 0.92}}}}}
    found = {"candidates": [kwta_cand, tbkwta_cand], "tick": 70010,
             "profile": {"dead_type_count": 16}}

    # 1 + 5: both candidates land; signals on every row
    bridge.propose_param(found)
    rows = _rows(bridge.PROPOSAL_LOG)
    proposed = [r for r in rows if r.get("status") == "param_proposed" and "param" in r]
    keys = [r["param"]["key"] for r in proposed]
    assert keys == ["kwta_pool_mult", "type_budget_kwta_budget",
                    "type_budget_kwta_enable"], keys
    vals = {r["param"]["key"]: r["param"]["value"] for r in proposed}
    assert vals["kwta_pool_mult"] == 3.0, vals
    assert vals["type_budget_kwta_budget"] == 7.0, vals
    assert vals["type_budget_kwta_enable"] == 1.0, vals
    for r in proposed:
        assert r["signals"]["dead_type_count"] == 16, r
        assert r["signals"]["family_yield"] == 2, r
        assert r["signals"]["conflict_rate"] == 0.5, r  # 1 unwired of 2 seeded

    # 2: no dsl -> unmappable, no invention
    bridge.PROPOSAL_LOG.write_text("", encoding="utf-8")
    bridge.propose_param({"candidates": [{"candidate_id": "c-nodsl", "strategy": {
        "type": "algorithm_selection", "target": "kwta_algorithm"}}], "tick": 1})
    rows = _rows(bridge.PROPOSAL_LOG)
    assert len(rows) == 1 and rows[0]["status"] == "param_unmappable", rows
    assert "refusing to invent" in rows[0]["reason"], rows

    # 3: clamp to range (type_budget_kwta_budget range is 1..64)
    bridge.PROPOSAL_LOG.write_text("", encoding="utf-8")
    bridge.propose_param({"candidates": [{"candidate_id": "c-big", "strategy": {
        "type": "algorithm_selection",
        "dsl": {"name": "TypeBudgetKWTA", "params": {"type_budget": 200}}}}], "tick": 2})
    rows = [r for r in _rows(bridge.PROPOSAL_LOG) if r.get("status") == "param_proposed"]
    assert rows[0]["param"]["value"] == 64.0, rows[0]
    assert "clamped" in rows[0]["param"]["reason"], rows[0]

    # 4: old shape {"candidate": ...} only
    bridge.PROPOSAL_LOG.write_text("", encoding="utf-8")
    bridge.propose_param({"candidate": kwta_cand, "tick": 3})
    rows = _rows(bridge.PROPOSAL_LOG)
    assert len(rows) == 1 and rows[0]["status"] == "param_proposed", rows
    assert rows[0]["param"]["key"] == "kwta_pool_mult", rows

    # 6: state-gated dead-curiosity family lands on the verified live consumer.
    bridge.PROPOSAL_LOG.write_text("", encoding="utf-8")
    curiosity_cand = {"candidate_id": "c-curiosity", "strategy": {
        "type": "curiosity_activation_recovery", "shadow_only": True,
        "shadow_param": {"key": "curiosity_activation_boost", "value": 2.5},
        "s1_value": "curiosity_activation_boost = 2.0 [current]",
        "s2_value": "curiosity_activation_boost = 2.5 [candidate]",
        "hypothesis": "dead curiosity receives a bounded recovery boost",
        "evidence": {"dead_type": "curiosity"}}}
    bridge.propose_param({"candidates": [curiosity_cand], "tick": 4,
                          "profile": {"dead_types": ["curiosity"]}})
    rows = _rows(bridge.PROPOSAL_LOG)
    assert len(rows) == 1 and rows[0]["status"] == "shadow_only", rows
    assert rows[0]["shadow_param"] == {"key": "curiosity_activation_boost",
                                        "value": 2.5, "requested": 2.5}, rows[0]
    assert "param" not in rows[0], rows[0]  # must not be consumed by runtime applier
    proposal_file = auto_round.PROPOSALS
    old_file = auto_round.PROPOSALS
    auto_round.PROPOSALS = bridge.PROPOSAL_LOG
    try:
        # Even if the shadow artifact carries the exact key/value, the runtime
        # applier must ignore it: only param_proposed is consumable.
        assert auto_round._fresh_param_proposals(0) == []
    finally:
        auto_round.PROPOSALS = old_file

    print("OK: new-family param landing, no-invention, clamp, old-shape, signals, curiosity shadow-only")


if __name__ == "__main__":
    run()
