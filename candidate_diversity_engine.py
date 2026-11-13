"""
candidate_diversity_engine.py
L8 Novel Concept: Diversity gate before JEPA ranking.

Addresses: candidate_pool_too_narrow (residual_defect conf=0.78)
           top_action=cross_signal_reconcile repeated 30% (6/20)
           false_exploration=3, oscillation_lag=8

Mechanism: 3-stage gate (entropy_check → gap_analysis → cf_injection)
Safety: observe_only, max 3 injected per cycle, traceable via flag.
"""

import math
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# ── Tunable thresholds ──────────────────────────────────────────────
H_MIN_BITS = 1.5          # min Shannon entropy for action-type distribution
MAX_INJECTED = 5          # max counterfactual candidates per gate trigger
INJECTION_FRACTION = 0.30 # max injected as fraction of pool size
RECENT_WINDOW = 20        # look-back window for affordance history


# ── Stage 1: Shannon entropy of action-type distribution ────────────
def shannon_entropy(type_counts: dict[str, int]) -> float:
    """Compute Shannon entropy H = -sum(p_i * log2(p_i)) for action types."""
    total = sum(type_counts.values())
    if total == 0:
        return 0.0
    h = 0.0
    for count in type_counts.values():
        p = count / total
        if p > 0:
            h -= p * math.log2(p)
    return h


def entropy_check(affordances: list[dict], h_min: float = H_MIN_BITS) -> dict:
    """
    Stage 1: Check if the action-type distribution is too narrow.
    Returns dict with passed flag, entropy, and type distribution.
    """
    if not affordances:
        return {"passed": False, "entropy": 0.0, "type_counts": {}, "reason": "empty_pool"}

    # Only look at the recent window
    recent = affordances[-RECENT_WINDOW:]
    type_counts = Counter(a.get("counterfactual_target_type", a.get("name", "unknown")) for a in recent)
    h = shannon_entropy(type_counts)
    passed = h >= h_min

    return {
        "passed": passed,
        "entropy": round(h, 4),
        "type_counts": dict(type_counts),
        "pool_size": len(affordances),
        "recent_size": len(recent),
        "reason": "sufficient_diversity" if passed else f"narrow_pool_h={h:.3f}<{h_min}"
    }


# ── Stage 2: Gap analysis from multiple sources ─────────────────────
def extract_gaps(status_json: dict, existing_types: set | None = None) -> list[dict]:
    """
    Stage 2: Identify under-represented types from three evidence sources.

    Sources:
      1. residual_defect.primary_defect — system-flagged defect
      2. iit_state.inactive_types — cold types from IIT partition
      3. surprise_history — types with hard_type_mismatch

    Args:
        status_json: full /status JSON
        existing_types: set of counterfactual_target_type already present in the
            pool. Gap types already present are skipped (2026-07-31: previously
            high_salience_types were injected even when already present, so the
            gate kept adding integrator/architect duplicates instead of widening
            the pool).
    """
    gaps: list[dict] = []
    seen_targets: set[str] = set()

    def _add_gap(target_type: str, source: str, priority: int, reason: str) -> None:
        if existing_types and target_type in existing_types:
            return  # type already represented in the pool — not a gap
        if target_type not in seen_targets:
            seen_targets.add(target_type)
            gaps.append({
                "target_type": target_type,
                "source": source,
                "priority": priority,
                "reason": reason
            })

    # Source 1: residual_defect primary defect
    residual = status_json.get("residual_defect") or {}
    primary_defect = residual.get("primary_defect", "")
    if primary_defect == "candidate_pool_too_narrow":
        # The defect says "缺少反事实替代" — any type not recently seen is a gap
        seen_types = set()
        for g in gaps:
            seen_types.add(g["target_type"])

        # Add common high-salience types that are typically under-represented
        # 2026-07-31: skip types already present in the pool so the gate injects
        # genuinely new types (curiosity/planning/decider/action) instead of
        # duplicating integrator/architect counterfactuals that dominate the pool.
        # 2026-08-15 fix(K): 剔除当前已高频活跃的类型——raw_dominant_history 里
        # 计数>0 的类型是系统正在轮转的类型，不是 gap；把 integrator 当缺口注入
        # 反事实，会被 L5-bypass boost 到 3.0 再经 kwta-quota 注入 integrator
        # seeds，自增强 candidate_pool_too_narrow。只有真正稀有/冷的类型
        # (curiosity/planning/memory/doubt...) 才值得注入。
        # 2026-08-22 P0-fix: expand from 6 hardcoded types to dynamic scan.
        # Root cause: 13 of 19 neuron types (ethical/cultural/aesthetic/temporal/
        # emotional/spatial/social/meta/rule_modification/veto/doubt/sensory/architect)
        # were never in the inject pool, so L8 diversity gate could never widen
        # them. Result: 114 tick history, 10+ types with dom_rate=0, is_active=False.
        # Fix: rank types by their dominance frequency in raw_dominant_history,
        # inject the coldest ones first. This module doesn't import TYPE_NAMES
        # from tl_ego_server (avoid hard dep), so we use raw_hist.keys() — which
        # is the universe of types the system has actually observed, naturally
        # covering all 19 neuron types.
        high_salience_types = []
        try:
            _raw_hist = status_json.get("raw_dominant_history") or {}
            if isinstance(_raw_hist, dict) and _raw_hist:
                # Sort by count ascending — coldest (most under-represented) first
                _sorted_by_count = sorted(_raw_hist.items(), key=lambda kv: int(kv[1] or 0))
                # Candidate set = raw_hist keys, minus unknown (not a real type)
                _tracked = [t for t, _ in _sorted_by_count if t and t != "unknown"]
                # Cap at 12 so MAX_INJECTED=5 stays the upper bound for build_seed_candidates
                high_salience_types = _tracked[:12]
            else:
                # Backward-compat fallback when history is empty
                high_salience_types = ["curiosity", "planning", "decider", "action", "integrator", "memory"]
        except Exception:
            pass
        # final safety: if list is empty (no history, no fallback), use the legacy 6
        if not high_salience_types:
            high_salience_types = ["curiosity", "planning", "decider", "action", "integrator", "memory"]
        # dynamic exclusion: drop types currently active in raw dominant rotation
        # (mirrors original behavior — don't inject types already winning)
        try:
            _raw_hist2 = status_json.get("raw_dominant_history") or {}
            if isinstance(_raw_hist2, dict):
                _active_types = {t for t, c in _raw_hist2.items() if int(c or 0) > 50}
                high_salience_types = [t for t in high_salience_types if t not in _active_types]
        except Exception:
            pass
        for t in high_salience_types:
            _add_gap(t, "residual_defect", 2,
                     f"candidate_pool_too_narrow — {t} rarely in pool")

    # Source 2: IIT inactive types (cold types)
    iit = status_json.get("iit_state") or {}
    inactive = iit.get("inactive_types", [])
    if inactive:
        # Pick up to 3 inactive types not already flagged
        already = {g["target_type"] for g in gaps}
        for itype in inactive[:3]:
            if itype not in already:
                _add_gap(itype, "iit_inactive", 1,
                         f"IIT inactive type ({itype}) — phi partition gap")

    # Source 3: Surprise history hard type mismatches
    surprise_last = status_json.get("surprise_last") or {}
    predicted = surprise_last.get("predicted", "")
    actual = surprise_last.get("actual", "")
    if predicted and actual and predicted != actual:
        _add_gap(actual, "surprise_mismatch", 1,
                 f"surprise: expected {predicted}, actual {actual} — type mismatch")

    return gaps


# ── Stage 3: Build and inject counterfactual seed candidates ─────────
def build_seed_candidates(
    gaps: list[dict],
    affordance_pool: list[dict],
    max_injected: int = MAX_INJECTED
) -> list[dict]:
    """
    Stage 3: Convert gaps into concrete counterfactual seed candidates.
    Returns list of new affordance dicts with _diversity_injected=True.
    """
    if not gaps or not affordance_pool:
        return []

    existing_names = {a.get("name", "") for a in affordance_pool}
    injected: list[dict] = []

    # Sort gaps by priority (lower = more important)
    sorted_gaps = sorted(gaps, key=lambda g: g["priority"])

    # §2026-09-12 Anti-stagnation annealing: read recent jepa_residuals actions
    # to penalize repeating counterfactual actions and force true diversity.
    recent_action_counts = Counter()
    # l6p-cfrotate 2026-09-21: prefer types whose explore_* action has NOT
    # recently been executed. Root cause: annealing only lowered jepa_score,
    # but ego_jepa.rank_affordances promotes _diversity_reserve candidates by
    # POSITION (insert(0)), never reading that score — so the penalty was
    # decorative and the same coldest type (ethical) was injected and won
    # every tick (explore_ethical_path 15/20 repeats).
    try:
        res_file = Path(__file__).resolve().parents[2] / ".lae" / "jepa_residuals.jsonl"
        if res_file.exists():
            for line in res_file.read_text(encoding="utf-8").splitlines()[-25:]:
                if line.strip():
                    try:
                        r = json.loads(line)
                        act = r.get("action") or (r.get("actual") or {}).get("name")
                        if act:
                            recent_action_counts[act] += 1
                    except Exception:
                        pass
    except Exception:
        pass

    # Rotate the gap list so gaps whose explore_<type>_path action was NOT
    # recently executed come first. Stable sort keeps original priority order
    # within each group, so cold-type ranking still drives the pick.
    if recent_action_counts:
        def _recent_key(gap: dict) -> tuple[int, int]:
            t = gap.get("target_type", "")
            k = recent_action_counts.get(f"explore_{t}_path", 0) + \
                recent_action_counts.get(f"explore_{t}_diversity_path", 0)
            return (1 if k > 0 else 0, gap.get("priority", 2))

        sorted_gaps = sorted(sorted_gaps, key=_recent_key)
    else:
        sorted_gaps = sorted(sorted_gaps, key=lambda g: g["priority"])

    for gap in sorted_gaps[:max_injected]:
        target_type = gap["target_type"]
        name = f"explore_{target_type}_path"

        # Avoid duplicate names
        if name in existing_names:
            name = f"explore_{target_type}_diversity_path"

        # Base score 1.0, but if this exact action repeated recently, apply annealing penalty
        repeat_k = recent_action_counts.get(name, 0)
        # Each repeat knocks 0.25 off jepa_score (min 0.20)
        annealed_score = max(0.20, round(1.0 - repeat_k * 0.25, 2))

        candidate: dict[str, Any] = {
            "name": name,
            "counterfactual_target_type": target_type,
            "risk": "low",
            "mode": "observe_only",
            "reason": f"[diversity_gate] {gap['reason']}",
            "next_step": f"Activate {target_type} affordance to expand candidate pool diversity",
            "counterfactual_scenarios": [
                {
                    "scenario_type": "change_context",
                    "target_type": target_type,
                    "predicted_stagnation_change": -0.10,
                    "confidence": 0.50,
                    "narrative": (
                        f"If I shift toward {target_type} from integrator lock, "
                        f"stagnation may decrease by ~0.10. "
                        f"(Source: {gap['source']})"
                    )
                }
            ],
            "top_counterfactual": "change_context",
            "counterfactual_confidence": 0.50,
            "_diversity_injected": True,   # traceability flag
            "_diversity_source": gap["source"],
            "_diversity_reason": gap["reason"],
            # 2026-07-31: also tag _cf_injected=True so ego_jepa rank_affordances
            # treats these as counterfactuals (residual penalty path, scenario
            # override path). Previously only _diversity_injected was set, so JEPA
            # scored them as ordinary affordances and the injected diversity never
            # propagated past ranking.
            "_cf_injected": True,
            "jepa_base_score": 0.65,
            "jepa_candidate_pool_adjustment": 0.85,
            # Keep injected candidates visible to the downstream selector.  The
            # old gate widened the pool, but ordinary score sorting could still
            # discard every new type before selection (the recurring
            # candidate_pool_too_narrow loop).  Consumers can reserve one slot
            # for this candidate without changing the base score semantics.
            "_diversity_reserve": True,
            "_diversity_quota": 3,
            "jepa_score": annealed_score,
        }
        injected.append(candidate)
        existing_names.add(name)

    return injected


# ── Main gate: run all 3 stages ────────────────────────────────────
def diversity_gate(
    affordances: list[dict],
    status_json: dict | None = None,
    h_min: float = H_MIN_BITS,
    max_injected: int = MAX_INJECTED,
) -> dict:
    """
    Full 3-stage diversity gate.

    Args:
        affordances: current affordance pool from /status external_world.affordances
        status_json: full /status JSON (for residual_defect, iit_state, etc.)
        h_min: minimum entropy threshold (bits)
        max_injected: maximum number of candidates to inject

    Returns:
        dict with stages results, gate decision, and (possibly) injected candidates
    """
    if status_json is None:
        status_json = {}

    # Stage 1
    s1 = entropy_check(affordances, h_min=h_min)

    result: dict = {
        "gate_triggered": False,
        "stage1_entropy": s1,
        "stage2_gaps": [],
        "stage3_injected": [],
        "pool_before": len(affordances),
        "pool_after": len(affordances),
    }

    # 2026-07-31 (l5g-20260731-000050 follow-up): force gate when the classifier
    # explicitly reports candidate_pool_too_narrow with high confidence.
    # Previously the entropy check could PASS (because untyped category candidates
    # have distinct names, inflating H >= 1.5) and the gate returned pass_through,
    # so the repair_hint "add residual-tag-derived counterfactual affordances
    # before ranking" was never executed even though the defect was active.
    # When the defect is explicit, trust it: skip the pass_through and always
    # run gap analysis + injection.
    _rd_force = False
    _rd = status_json.get("residual_defect") or {}
    if _rd.get("primary_defect") == "candidate_pool_too_narrow":
        try:
            _rd_force = float(_rd.get("confidence", 0.0) or 0.0) >= 0.50
        except (TypeError, ValueError):
            _rd_force = False

    if s1["passed"] and not _rd_force:
        result["decision"] = "pass_through"
        result["decision_reason"] = s1["reason"]
        return result

    # Gate triggered — Stage 2
    # 2026-07-31: pass existing pool target types so gaps only cover missing types.
    _existing_types = {
        str(a.get("counterfactual_target_type") or "")
        for a in affordances
        if a.get("counterfactual_target_type")
    }
    gaps = extract_gaps(status_json, existing_types=_existing_types)
    result["stage2_gaps"] = gaps
    result["gate_triggered"] = True
    result["decision_reason_forced"] = _rd_force

    if not gaps:
        result["decision"] = "no_gaps_found"
        result["decision_reason"] = "entropy_low_but_no_gap_sources"
        return result

    # Stage 3
    injected = build_seed_candidates(gaps, affordances, max_injected=max_injected)
    result["stage3_injected"] = injected
    result["pool_after"] = len(affordances) + len(injected)
    result["decision"] = "injected"
    result["decision_reason"] = (
        f"entropy={s1['entropy']:.3f}<{h_min}, "
        f"{len(gaps)} gap(s) found, {len(injected)} candidate(s) injected"
    )

    return result


# ── Convenience: apply gate and return expanded pool ────────────────
def expand_pool(
    affordances: list[dict],
    status_json: dict | None = None,
    **kwargs: Any,
) -> tuple[list[dict], dict]:
    """
    Run the diversity gate and return (expanded_pool, gate_result).

    The expanded pool = original affordances + injected candidates (appended).
    """
    result = diversity_gate(affordances, status_json=status_json, **kwargs)
    pool = list(affordances)  # copy

    for candidate in result.get("stage3_injected", []):
        pool.append(candidate)

    return pool, result


# ── CLI self-test ───────────────────────────────────────────────────
def _make_sample_affordances(n: int = 20) -> list[dict]:
    """Build a sample pool with artificially narrow type distribution."""
    base = [
        {"name": "cross_signal_reconcile", "counterfactual_target_type": "integrator", "risk": "low", "mode": "observe_only"},
        {"name": "cross_signal_reconcile", "counterfactual_target_type": "integrator", "risk": "low", "mode": "observe_only"},
        {"name": "cross_signal_reconcile", "counterfactual_target_type": "integrator", "risk": "low", "mode": "observe_only"},
        {"name": "cross_signal_reconcile", "counterfactual_target_type": "integrator", "risk": "low", "mode": "observe_only"},
        {"name": "cross_signal_reconcile", "counterfactual_target_type": "integrator", "risk": "low", "mode": "observe_only"},
        {"name": "cross_signal_reconcile", "counterfactual_target_type": "integrator", "risk": "low", "mode": "observe_only"},
    ]
    return base * (n // 6) + base[: n % 6]


def _make_status_with_gaps() -> dict:
    """Build a minimal status_json with residual_defect + iit_inactive_types."""
    return {
        "residual_defect": {
            "primary_defect": "candidate_pool_too_narrow",
            "confidence": 0.78,
        },
        "iit_state": {
            "inactive_types": ["curiosity", "architect", "planning"],
        },
        "surprise_last": {
            "predicted": "integrator",
            "actual": "integrator",
        },
    }


def run_self_test(verbose: bool = True) -> dict:
    """Self-test: verify gate triggers and injects under narrow-pool conditions."""
    results: dict[str, Any] = {"tests": [], "passed": 0, "failed": 0}

    def check(name: str, condition: bool, detail: str = "") -> None:
        ok = bool(condition)
        results["tests"].append({"name": name, "passed": ok, "detail": detail})
        results["passed" if ok else "failed"] += 1
        if verbose:
            print(f"  [OK] {name}" + (f" - {detail}" if detail else "") if ok else f"  [FAIL] {name}" + (f" - {detail}" if detail else ""))

    # T1: narrow pool triggers gate
    narrow = _make_sample_affordances(20)
    r = diversity_gate(narrow, status_json=_make_status_with_gaps())
    check("T1: narrow_pool_triggers_gate", r["gate_triggered"], f"decision={r['decision']}")

    # T2: entropy below threshold
    s1 = r["stage1_entropy"]
    check("T2: entropy_below_threshold", not s1["passed"] and s1["entropy"] < H_MIN_BITS,
          f"H={s1['entropy']:.4f}")

    # T3: gaps detected
    gaps = r["stage2_gaps"]
    check("T3: gaps_detected", len(gaps) > 0, f"{len(gaps)} gap(s)")

    # T4: candidates injected
    injected = r["stage3_injected"]
    check("T4: candidates_injected", len(injected) > 0, f"{len(injected)} injected")

    # T5: injected count ≤ max
    check("T5: injected_count_respected", len(injected) <= MAX_INJECTED,
          f"injected={len(injected)}, max={MAX_INJECTED}")

    # T6: injected candidates have traceability flag
    all_flagged = all(c.get("_diversity_injected") for c in injected)
    check("T6: traceability_flag_set", all_flagged, f"_diversity_injected={all_flagged}")

    # T7: injected mode is observe_only
    all_observe = all(c.get("mode") == "observe_only" for c in injected)
    check("T7: observe_only_mode", all_observe)

    # T8: diverse pool passes through when NO defect is reported
    types = (["integrator", "sensory", "rule_modification", "veto", "doubt",
              "architect", "memory", "curiosity", "meta", "decider"] * 2)[:20]
    diverse: list[dict] = [
        {"name": f"action_{i}", "counterfactual_target_type": types[i], "risk": "low", "mode": "observe_only"}
        for i in range(20)
    ]
    r2 = diversity_gate(diverse, status_json={})  # no defect -> pass_through
    check("T8: diverse_pool_passes_through", not r2["gate_triggered"],
          f"decision={r2['decision']}, H={r2['stage1_entropy']['entropy']:.4f}")

    # T13 (2026-07-31): defect force — even a diverse pool MUST inject when the
    # classifier explicitly reports candidate_pool_too_narrow with conf>=0.50.
    # Previously entropy >= H_MIN_BITS caused pass_through and the repair_hint
    # was never executed; now the explicit defect overrides the entropy check.
    r2b = diversity_gate(diverse, status_json=_make_status_with_gaps())
    check("T13: defect_forces_injection", r2b["gate_triggered"] and len(r2b["stage3_injected"]) > 0,
          f"decision={r2b['decision']}, injected={len(r2b['stage3_injected'])}")

    # T14: forced injection skips types already present in the pool
    narrow2 = _make_sample_affordances(12) + [
        {"name": "synthesize_findings", "counterfactual_target_type": "integrator", "risk": "low", "mode": "observe_only"}
    ]
    r2c = diversity_gate(narrow2, status_json=_make_status_with_gaps())
    inj_types = {c.get("counterfactual_target_type") for c in r2c.get("stage3_injected", [])}
    check("T14: skip_existing_types", "integrator" not in inj_types,
          f"injected_types={inj_types}")

    # T9: empty pool triggers gate safely (no crash)
    r3 = diversity_gate([], status_json=_make_status_with_gaps())
    check("T9: empty_pool_safe", r3["stage1_entropy"]["reason"] == "empty_pool")

    # T10: expand_pool returns larger list
    expanded, _ = expand_pool(narrow, status_json=_make_status_with_gaps())
    check("T10: expand_pool_larger", len(expanded) > len(narrow),
          f"{len(narrow)} → {len(expanded)}")

    # T11: injected candidates have counterfactual_target_type
    types_seen = {c.get("counterfactual_target_type") for c in injected}
    check("T11: gap_types_injected", len(types_seen) > 0, f"types={types_seen}")

    # T12: injected candidates have unique names (no self-duplicates)
    inj_names = [c.get("name", "") for c in injected]
    check("T12: no_self_duplicate_injected", len(inj_names) == len(set(inj_names)),
          f"names={inj_names}")

    passed = results["passed"]
    total = results["passed"] + results["failed"]
    if verbose:
        print(f"\n  Self-test: {passed}/{total} passed")
    results["total"] = total
    return results


def main() -> dict:
    """Entry point. Returns gate result dict and prints summary."""
    # Load from stdin if piped JSON, otherwise use built-in test data
    if not sys.stdin.isatty():
        try:
            data = json.load(sys.stdin)
            affordances = data.get("affordances", [])
            status_json = data.get("status", {})
        except (json.JSONDecodeError, Exception):
            affordances = _make_sample_affordances(20)
            status_json = _make_status_with_gaps()
    else:
        affordances = _make_sample_affordances(20)
        status_json = _make_status_with_gaps()

    result = diversity_gate(affordances, status_json=status_json)

    # Print summary
    print(f"Diversity Gate Result:")
    print(f"  gate_triggered : {result['gate_triggered']}")
    print(f"  decision        : {result['decision']}")
    print(f"  pool_before     : {result['pool_before']}")
    print(f"  pool_after      : {result['pool_after']}")
    print(f"  entropy         : {result['stage1_entropy']['entropy']:.4f} "
          f"(min={H_MIN_BITS})")
    if result["stage2_gaps"]:
        print(f"  gaps            : {[g['target_type'] for g in result['stage2_gaps']]}")
    if result["stage3_injected"]:
        print(f"  injected        : {[c['name'] for c in result['stage3_injected']]}")

    return result


if __name__ == "__main__":
    # If --test flag, run self-test; otherwise run gate on built-in data
    if "--test" in sys.argv:
        r = run_self_test(verbose=True)
        sys.exit(0 if r["failed"] == 0 else 1)
    main()
