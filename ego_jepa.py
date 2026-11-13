"""
Minimal stub for ego_jepa.py — restores JEPA pipeline after module loss.
WARNING: This is a placeholder. Replace with full implementation when available.
"""

from typing import List, Dict, Any


def rank_affordances(ego_state: Dict[str, Any], observation: Dict[str, Any], affordances: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Score each affordance from counterfactual signal, then sort ascending.

    Stub replacement (l5g-20260808-000031-MUT), patched (l5g-20260825-001471):
    - Counterfactual affordances carry stagnation prediction in
      counterfactual_scenarios[0].predicted_stagnation_change (NOT predicted_outcome).
    - If cc > 0 and predicted_stagnation_change < 0, score = cc * min(1.0, abs(sd)*6).
    - Passive affordances and counterfactuals that won't relieve stagnation keep 0.5.
    - Sort ascending so the lowest-score (highest stagnation-relief potential) ranks first.
    """
    for a in affordances:
        if isinstance(a, dict):
            cc = a.get("counterfactual_confidence", 0.5)
            sd = 0.0
            if a.get("_cf_injected"):
                # l5g-20260831-005669-FIX: preserve upstream pre-computed jepa_score
                # for _cf_injected items (diversity gate jepa_score=1.0) before
                # recalculating from counterfactual_scenarios defaults. Without this,
                # rank_affordances unconditionally overwrites the diversity gate's
                # high-confidence signal (cc=0.5, sd=-0.1 -> score=0.3), starving
                # kwta-quota counterfactual seed injection and leaving
                # candidate_pool_too_narrow unresolved.
                _pre_score = a.get("jepa_score")
                if isinstance(_pre_score, (int, float)) and _pre_score > 0.0:
                    a["jepa_score"] = float(_pre_score)
                    if "jepa_confidence" not in a:
                        a["jepa_confidence"] = a["jepa_score"]
                    continue
                # Counterfactual affordances carry stagnation prediction in
                # counterfactual_scenarios[0].predicted_stagnation_change, NOT
                # in predicted_outcome (which is never set during injection).
                _sc = a.get("counterfactual_scenarios", [])
                if isinstance(_sc, list) and _sc:
                    try:
                        _raw = _sc[0].get("predicted_stagnation_change", 0.0)
                        sd = float(_raw)
                    except (TypeError, ValueError):
                        sd = 0.0
            else:
                po = a.get("predicted_outcome", {})
                sd = float(po.get("stagnation_delta", 0.0) or 0.0)
            if cc > 0 and sd < 0:
                a["jepa_score"] = round(cc * min(1.0, abs(sd) * 6), 4)
            else:
                a["jepa_score"] = 0.5
            if "jepa_confidence" not in a:
                a["jepa_confidence"] = a["jepa_score"]
    affordances.sort(key=lambda a: a.get("jepa_score", 0.0) if isinstance(a, dict) else 0.0, reverse=False)

    # Diversity-gate candidates must survive ranking, not merely be appended to
    # the pool.  Without this bounded reserve, a narrow incumbent family can
    # occupy every leading slot and the same defect is reported forever.
    reserved = [
        a for a in affordances
        if isinstance(a, dict) and a.get("_diversity_reserve")
    ]
    if reserved:
        selected_types = set()
        promoted = []
        # l6p-cfrotate 2026-09-21: pick the highest-scoring reserved candidate
        # per type, not the first in list order. The anneal penalty writes
        # jepa_score (1.0 - 0.25*repeats), so list order made the penalty
        # decorative and the same type kept winning. One pass over sorted-by-
        # score candidates keeps this O(n log n) on a tiny list.
        for candidate in sorted(reserved, key=lambda a: float(a.get("jepa_score", 0.0) or 0.0), reverse=True):
            target_type = str(candidate.get("counterfactual_target_type") or "")
            if target_type and target_type not in selected_types:
                promoted.append(candidate)
                selected_types.add(target_type)
                if len(promoted) >= 1:
                    break
        for candidate in reversed(promoted):
            affordances.remove(candidate)
            affordances.insert(0, candidate)
    return affordances


def predict_external(ego_state: Dict[str, Any], observation: Dict[str, Any]) -> Dict[str, Any]:
    """Re-export the real implementation from ego_jepa_debug.

    The server imports predict_external from this module; the real logic
    lives in ego_jepa_debug.py (stage5 external predictions with EXTERNAL_PRIORS,
    workspace event count and resource warning prediction).
    """
    try:
        from ego_jepa_debug import predict_external as _real
        return _real(ego_state, observation)
    except Exception:
        return {
            "status": "stub",
            "reason": "ego_jepa_debug not available",
            "workspace_events_predicted": 0,
            "workspace_events_actual": 0,
            "prediction_error": 0.0,
        }


def predict_outcome(ego_state: Dict[str, Any], action: Dict[str, Any], affordance: Dict[str, Any] = None) -> Dict[str, Any]:
    """Minimal outcome predictor."""
    return {"status": "stub", "predicted_error": 0.0}


def _observation_novelty(observation: Dict[str, Any]) -> float:
    """Minimal novelty signal: observed category count scaled like the server's
    fallback (categories * 0.08). Was hardcoded 0.0, which forced every
    reconcile PE toward 1.0 (|pred-0|/pred) and collapsed JEPA calibration
    (zero_miss). Same semantics as tl_ego_server's
    `actual_cats * 0.08` fallback, now actually reachable."""
    try:
        cats = observation.get("categories") or {}
        n = len(cats) if hasattr(cats, "__len__") else 0
        return min(1.0, n * 0.08)
    except Exception:
        return 0.0


__all__ = [
    "rank_affordances",
    "predict_external",
    "predict_outcome",
    "_observation_novelty",
]
