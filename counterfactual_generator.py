#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
counterfactual_generator — TL-Ego L6-P (2026-06-30)

Ego-designed architecture: independent module that generates alternative
action hypotheses before selection. Each counterfactual scenario explores
"what if the system chose a different action given current state?"

Design origin: Ego metacontemplate (tick~25, 3x contemplate + feedback cycles)
- Phase: action (concentration=0.803, competition=0.690)
- Ego explicitly rejected shallow L6 repair in favor of core architecture component
- S1⇒rule_modification (34%), S2⇒integrator (compatible_agreement)
- 63K STDP updates per cycle, ~190K total across 3 cycles

Signature:
    counterfactual_generator(current_state, action_space, context_window=5)
    → List[CounterfactualScenario]

Integration point (per Ego design):
    plan() → evaluate_proposals() → counterfactual_generator() → select_action()

In TL-Ego server concrete terms:
    _derive_workspace_affordances() → counterfactual_generator() → JEPA ranking → action

Dependencies: Python stdlib only (math, json, random, typing, pathlib)
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
import math
import json
import random
from pathlib import Path


def _fetch_type_summary() -> dict:
    """Fetch l2 type_summary from /status for affordance_guard integration."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8766/status", timeout=5) as r:
            status = json.loads(r.read().decode())
        return (status or {}).get("fractal_attractor", {}).get("level2", {}).get("type_summary", {})
    except Exception:
        return {}


def _fetch_type_summary() -> dict:
    """Fetch l2 type_summary from /status for affordance_guard integration."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8766/status", timeout=5) as r:
            status = json.loads(r.read().decode())
        return (status or {}).get("fractal_attractor", {}).get("level2", {}).get("type_summary", {})
    except Exception:
        return {}


# ─── Scenario type constants ───
SCENARIO_CHANGE_CONTEXT = "change_context"        # e.g. "what if I switch to integrator?"
SCENARIO_DEEPEN_ACTION = "deepen_current"         # e.g. "what if I stay on action longer?"
SCENARIO_ALTERNATIVE_ACTION = "alternative_action" # e.g. "what if I choose a different affordance?"
SCENARIO_SUPPRESS_TYPE = "suppress_dominant"      # e.g. "what if I suppress veto for 3 ticks?"

# ─── Type transition priors (learned from action_impact analysis) ───
# action→action: 103/118 transitions mean actions tend to repeat
# average stagnation_delta: -0.127 (actions worsen stagnation slightly)
TYPE_TRANSITION_PRIORS: Dict[str, Dict[str, float]] = {
    "action": {"self_repeat": 0.87, "to_integrator": 0.35, "to_memory": 0.04, "to_sensory": 0.02, "to_decider": 0.02, "to_rule_modification": 0.20},
    "integrator": {"self_repeat": 0.45, "to_action": 0.20, "to_memory": 0.20, "to_sensory": 0.10, "to_decider": 0.05},
    "memory": {"self_repeat": 0.50, "to_integrator": 0.25, "to_sensory": 0.12, "to_decider": 0.08, "to_action": 0.05},
    "sensory": {"self_repeat": 0.35, "to_memory": 0.25, "to_integrator": 0.20, "to_decider": 0.12, "to_action": 0.08},
    "decider": {"self_repeat": 0.30, "to_action": 0.30, "to_integrator": 0.20, "to_memory": 0.12, "to_sensory": 0.08},
    "doubt": {"self_repeat": 0.40, "to_integrator": 0.25, "to_veto": 0.15, "to_memory": 0.12, "to_sensory": 0.08},
    "veto": {"self_repeat": 0.25, "to_action": 0.25, "to_decider": 0.20, "to_integrator": 0.15, "to_memory": 0.10, "to_doubt": 0.05},
    "rule_modification": {"self_repeat": 0.30, "to_action": 0.20, "to_integrator": 0.20, "to_decider": 0.15, "to_memory": 0.10, "to_veto": 0.05},
}

# ─── Action priors per affordance name (mirrors ego_jepa.ACTION_PRIORS) ───
ACTION_COUNTERFACTUAL_PRIORS: Dict[str, Dict[str, Any]] = {
    "summarize_recent_memory": {
        "boost_types": ["memory"],
        "suppress_types": ["action"],
        "expected_stagnation_change": -0.15,
        "expected_competition_change": -0.05,
        "risk": "low",
        "fallout_prob": 0.05,  # 5% chance of unintended stalling
    },
    "inspect_recent_project_result": {
        "boost_types": ["integrator"],
        "suppress_types": ["action", "decider"],
        "expected_stagnation_change": -0.20,
        "expected_competition_change": -0.08,
        "risk": "low",
        "fallout_prob": 0.08,
    },
    "review_recent_trace": {
        "boost_types": ["integrator", "memory"],
        "suppress_types": ["action"],
        "expected_stagnation_change": -0.10,
        "expected_competition_change": -0.03,
        "risk": "low",
        "fallout_prob": 0.10,
    },
    "verify_recent_script": {
        "boost_types": ["action", "decider"],
        "suppress_types": ["memory", "doubt"],
        "expected_stagnation_change": 0.02,
        "expected_competition_change": 0.05,
        "risk": "medium",
        "fallout_prob": 0.15,
    },
    "seek_external_signal": {
        "boost_types": ["sensory"],
        "suppress_types": ["integrator"],
        "expected_stagnation_change": -0.05,
        "expected_competition_change": 0.10,
        "risk": "low",
        "fallout_prob": 0.12,
    },
    "analyze_repeated_action_residuals": {
        "boost_types": ["integrator", "doubt"],
        "suppress_types": ["action"],
        "expected_stagnation_change": -0.18,
        "expected_competition_change": -0.10,
        "risk": "low",
        "fallout_prob": 0.06,
    },
    "design_minimal_counterfactual_probe": {
        "boost_types": ["integrator", "reasoning"],
        "suppress_types": ["action", "decider"],
        "expected_stagnation_change": -0.14,
        "expected_competition_change": -0.06,
        "risk": "low",
        "fallout_prob": 0.04,
    },
    "diagnose_workspace_scan": {
        "boost_types": ["integrator"],
        "suppress_types": ["action"],
        "expected_stagnation_change": -0.02,
        "expected_competition_change": -0.02,
        "risk": "low",
        "fallout_prob": 0.07,
    },
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _normalized_dominant_entropy(current_state: Dict[str, Any]) -> float:
    """Compute type entropy from type_counts, normalized by log(N types).

    Returns 0.0 if no type data (pure concentration).
    """
    type_counts = current_state.get("type_counts") or {}
    if not type_counts or not isinstance(type_counts, dict):
        return 0.0
    total = sum(type_counts.values())
    if total <= 0:
        return 0.0
    probs = [c / total for c in type_counts.values() if c > 0]
    if len(probs) <= 1:
        return 0.0
    entropy = -sum(p * math.log2(p) for p in probs)
    max_ent = math.log2(len(probs))
    return _clamp(entropy / max_ent) if max_ent > 0 else 0.0


def _inferred_transition_bias(current_state: Dict[str, Any], action_space: List[Dict[str, Any]]) -> str:
    """Infer which transition type the system is biased toward based on current state."""
    dominant = current_state.get("dominant_type", "unknown")
    stagnation = int(current_state.get("stagnation_streak", 0) or 0)
    competition = float(current_state.get("competition", 0.5) or 0.5)
    concentration = float(current_state.get("concentration", 0.5) or 0.5)
    pe = float(current_state.get("prediction_error", 0.5) or 0.5)

    # High stagnation + action dominant → trapped in action loop
    if stagnation >= 4 and dominant == "action":
        return SCENARIO_CHANGE_CONTEXT
    # Integrator dominant + low concentration → fragile attractor; deepen to build
    # concentration. Without this guard, the comp/pe branch below captures the
    # integrator case and returns SCENARIO_ALTERNATIVE_ACTION, generating wrong
    # counterfactual types that suppress rather than support consolidation.
    if dominant == "integrator" and concentration < 0.7 and competition < 0.75:
        return SCENARIO_DEEPEN_ACTION
    # l5g-20260730-000102: grace-window concentration blind spot.
    # When _int_press_ctr > 0 (grace window active), concentration is
    # artificially depressed (counter drain side-effect). All existing
    # integrator guards require conc >= thresholds and all miss.
    # Result: DEFAULT → SCENARIO_ALTERNATIVE_ACTION → empty target_type
    # → promote loop silent → only injected synthesize_findings stays in pool
    # → integrator lock never broken despite grace window counterfactual injection.
    # Fix: override to SCENARIO_CHANGE_CONTEXT targeting rule_modification
    # (the repair path) whenever grace window is active and integrator is
    # stuck at high competition, ignoring concentration entirely.
    _int_press_ctr_cf = int(current_state.get("_int_press_ctr", 0) or 0)
    if (dominant == "integrator"
            and _int_press_ctr_cf > 0
            and competition >= 0.65
            and stagnation >= 0):
        return SCENARIO_CHANGE_CONTEXT
    # l5g-20260702-002374 fix (l5g-20260702-000101): integrator+high_comp+low_conc.
    # Original returned SCENARIO_CHANGE_CONTEXT, generating change_context/action|memory|sensory
    # in stable-else branch — all non-self-referential (filter passes) but all non-integrator
    # (JEPA promote fails) → uniform counterfactual pool, candidate_pool_too_narrow.
    # Fix: return SCENARIO_DEEPEN_ACTION. Stable-else generates deepen_current/integrator
    # (self-referential → filter removes; acceptable for stable integrator consolidation).
    # Oscillation/cycle branch explicitly handles SCENARIO_DEEPEN_ACTION at line ~747,
    # adding a promote-eligible deepen_current/integrator scenario.
    if (dominant == "integrator"
            and competition >= 0.75
            and concentration < 0.7
            and stagnation < 4):
        # l5g-20260723-000016-MUT: oscillation window expanded 8→16 ticks.
        # Root cause: 8-tick window closed over the consolidation phase (ticks 14-21:
        # action×5, integrator×3), reporting only 2 flips — below the >=3 threshold —
        # causing SCENARIO_DEEPEN_ACTION instead of SCENARIO_CHANGE_CONTEXT.
        # The 16-tick window spans both the action→integrator and integrator→action
        # transitions (3 flips >= 3 threshold), correctly detecting bistable oscillation.
        # Effect: SCENARIO_CHANGE_CONTEXT generates change_context/integrator counterfactuals
        # that _TYPE_AFFORDANCE_MAP promotes into synthesize_findings, breaking candidate_pool_too_narrow.
        _osc_w = 64
        _osc_snaps = _load_trajectory_context(_osc_w)
        _osc_doms = [s.get("dominant", s.get("dominant_type", "?")) for s in _osc_snaps]
        if len(_osc_doms) >= 3:
            _osc_alts = sum(1 for i in range(1, len(_osc_doms)) if _osc_doms[i] != _osc_doms[i - 1])
            if _osc_alts >= 3:
                return SCENARIO_CHANGE_CONTEXT
        return SCENARIO_DEEPEN_ACTION
    # l5g-20260702-001979: action-dominant oscillation + high integrator_drive =
    # fragile integrator attractor. Without this guard, the comp/pe branch below
    # returns SCENARIO_ALTERNATIVE_ACTION, starving integrator counterfactuals and
    # triggering candidate_pool_too_narrow (same pattern as veto/doubt fix above).
    drives_integ = float(
        current_state.get("titan", {}).get("stage2", {}).get("drives", {}).get("integration", 0.0) or 0.0
    )
    # L5g-20260709-000031: widen comp threshold from 0.70 to 0.65.
    # comp in [0.65, 0.70) was a gap: both this guard (comp>=0.70) and Guard 4
    # (comp>=0.75+pe>=0.5) miss, so the state falls through to DEFAULT →
    # SCENARIO_ALTERNATIVE_ACTION → deepen_current/action (self-referential) →
    # no counterfactual diversity → candidate_pool_too_narrow persists.
    # With comp>=0.65, this range now routes to SCENARIO_CHANGE_CONTEXT,
    # generating integrator-targeted change_context scenarios that
    # _TYPE_AFFORDANCE_MAP promotes into synthesize_findings, restoring diversity.
    if (dominant == "action"
            and competition >= 0.65
            and drives_integ >= 0.4):
        # FIX (l5g-20260702-001989): return SCENARIO_CHANGE_CONTEXT so counterfactual_generator
        # produces integrator-targeted change_context scenarios instead of action-deepening ones.
        # This restores counterfactual diversity and allows _TYPE_AFFORDANCE_MAP promotion
        # to inject synthesize_findings into the candidate pool, resolving candidate_pool_too_narrow.
        return SCENARIO_CHANGE_CONTEXT
    # l5g-20260701-001579: veto/doubt/rule_modification/decider with high integration_drive
    # should generate change_context (not alternative_action -> rule_modification/decider).
    # Without this, decider + comp>=0.70 + int_drive>=0.4 + stag<4 falls to SCENARIO_ALTERNATIVE_ACTION,
    # generating deepen_current targeting decider → no integrator in pool → candidate_pool_too_narrow.
    # MUST come BEFORE the comp/pe guard below, otherwise step-5 fires first for
    # decider + comp>=0.75 and returns SCENARIO_ALTERNATIVE_ACTION, making this guard dead code.
    drives = current_state.get("titan", {}).get("stage2", {}).get("drives", {})
    integration_drive = float(drives.get("integration", 0.0) or 0.0)
    if (dominant in ("veto", "doubt", "rule_modification", "decider")
            and competition >= 0.62
            and integration_drive >= 0.4
            and stagnation <= 4):
        return SCENARIO_CHANGE_CONTEXT
    # l5g-20260707-000052: memory-dominant + high integration_drive.
    # MUST come BEFORE Guard 4 (comp>=0.75+pe>=0.5) below.
    drives = current_state.get('titan', {}).get('stage2', {}).get('drives', {})
    integration_drive = float(drives.get('integration', 0.0) or 0.0)
    if (dominant == "memory"
            and competition >= 0.65
            and integration_drive >= 0.4
            and stagnation < 4):
        return SCENARIO_CHANGE_CONTEXT
    # l5g-20260702-002039: sensory-dominant + high integration_drive = integrator counterfactual needed.
    # Root cause: _inferred_transition_bias had a symmetric guard for dominant==action +
    # comp>=0.70 + drives_integ>=0.4 (l5g-20260702-001989) but no equivalent for dominant==sensory.
    # When dominant=sensory + comp>=0.70 + drives_integ>=0.4 + stag<4, the function fell through
    # to SCENARIO_ALTERNATIVE_ACTION, generating counterfactuals targeting rule_mod/sensory instead
    # of integrator. _TYPE_AFFORDANCE_MAP never promoted synthesize_findings → integrator pool starved
    # → candidate_pool_too_narrow persists → integrator structurally suppressed.
    # Fix: add symmetric guard for dominant==sensory to return SCENARIO_CHANGE_CONTEXT, allowing
    # l5g-20260717-000304: architect-dominant + high comp + low conc + int_drive.
    # Without this guard, architect + high comp + low conc + int_drive falls through
    # to Guard 4 (comp>=0.75 + pe>=0.5) -> SCENARIO_ALTERNATIVE_ACTION ->
    # deepen_current/architect (self-referential, filtered by self-ref guard) ->
    # no change_context/integrator scenario survives promote filter ->
    # _TYPE_AFFORDANCE_MAP cannot promote synthesize_findings -> integrator pool starved.
    # Fix: route architect + high comp + low conc + int_drive to SCENARIO_CHANGE_CONTEXT,
    # generating change_context/integrator scenarios that _TYPE_AFFORDANCE_MAP promotes
    # into synthesize_findings, restoring counterfactual diversity.
    # MUST precede the comp/pe Guard 4 below (fires for comp>=0.75 + pe>=0.5).
    drives = current_state.get("titan", {}).get("stage2", {}).get("drives", {})
    integration_drive = float(drives.get("integration", 0.0) or 0.0)
    if (dominant == "architect"
            and concentration < 0.7
            and competition >= 0.65
            and stagnation < 9999
            and integration_drive >= 0.20):
        return SCENARIO_CHANGE_CONTEXT
    # High competition + high PE -> need to break out with alternative action
    # (excludes integrator: integrator conc branch handled above; architect handled above)
    if competition >= 0.75 and pe >= 0.5:
        return SCENARIO_ALTERNATIVE_ACTION
    # counterfactual_generator to produce integrator-targeted change_context scenarios.
    if (dominant == "sensory"
            and competition >= 0.65
            and drives_integ >= 0.4
            and stagnation < 4):
        return SCENARIO_CHANGE_CONTEXT
    # Medium stagnation + low competition -> safe to deepen
    # l5g-20260727-000189: meta + bistability (stag 2-4) + low comp -> empty counterfactuals.
    # When meta is dominant with stagnation 2-4 and comp <= 0.5, the generic
    # DEEPEN_ACTION guard produces deepen_current/meta which self-ref filter removes,
    # leaving counterfactual pool empty. Switch to CHANGE_CONTEXT so counterfactuals
    # target integrator and _TYPE_AFFORDANCE_MAP promotes synthesize_findings.
    if (dominant == "meta"
            and 2 <= stagnation <= 4
            and competition <= 0.5):
        return SCENARIO_CHANGE_CONTEXT
    if 2 <= stagnation <= 4 and competition <= 0.5 and dominant != "rule_modification":
        return SCENARIO_DEEPEN_ACTION
    # Integrator + high conc + pressure: route to change_context so counterfactuals
    # promote integrator-type affordances (not suppress them).
    # Without this guard, SCENARIO_SUPPRESS_TYPE starves the candidate pool,
    # producing oscillation_lag tags and L6 candidate_pool_too_narrow.
    drives = current_state.get("titan", {}).get("stage2", {}).get("drives", {})
    integration_drive = float(drives.get("integration", 0.0) or 0.0)
    if (dominant == "integrator"
            and concentration >= 0.5
            and competition >= 0.65
            and stagnation >= 2
            and integration_drive >= 0.4):
        return SCENARIO_CHANGE_CONTEXT
    # Integrator with high concentration AND entrapment confirmed -> may be over-converging.
    # l5g-20260730-000012: original guard fired on conc>=0.7 alone, suppressing integrator even
    # when legitimately dominant (comp=0.66, stag=7). Result: SCENARIO_SUPPRESS_TYPE generates
    # suppress_dominant/integrator counterfactuals -> dominant_is_suppressed=True -> LOWEST_CONFIDENCE_PENALTY
    # halves counterfactual confidence -> integrator candidates starved -> candidate_pool_too_narrow.
    # Fix: require low competition (entrapment signal) AND high stagnation (lock-in signal) before suppressing.
    if (dominant == "integrator"
            and concentration >= 0.7
            and competition <= 0.55
            and stagnation >= 4):
        return SCENARIO_SUPPRESS_TYPE
    # l5g-20260730-000012 Guard J: integrator + high conc + high stag + moderate comp.
    # When integrator is stuck (stag>=4) with high concentration but competition is
    # moderate (0.55 < comp < 0.70), Guard I (above) and all prior integrator guards miss.
    # Without this guard, DEFAULT -> SCENARIO_ALTERNATIVE_ACTION -> deepen_current/integrator
    # (self-referential -> filtered out) -> empty counterfactual pool -> candidate_pool_too_narrow.
    # Fix: route to SCENARIO_CHANGE_CONTEXT, generating change_context/integrator counterfactuals
    # that _TYPE_AFFORDANCE_MAP promotes into synthesize_findings, restoring counterfactual diversity.
    if (dominant == "integrator"
            and concentration >= 0.7
            and stagnation >= 4
            and competition <= 0.65):
        return SCENARIO_CHANGE_CONTEXT
    # l5g-20260711-000071: integrator + high_comp + low_conc gap.
    # When conc<0.5, L252 guard (conc>=0.5) is skipped; when stag<2, stag>=2 also fails.
    # Falls to DEFAULT -> SCENARIO_ALTERNATIVE_ACTION -> non-integrator counterfactuals ->
    # candidate_pool_too_narrow -> integrator suppressed again.
    # Fix: add guard for conc<0.5 + comp>=0.70 -> SCENARIO_CHANGE_CONTEXT,
    # generating integrator-promoting counterfactuals via _TYPE_AFFORDANCE_MAP.
    drives_cf = current_state.get("titan", {}).get("stage2", {}).get("drives", {})
    integration_drive_cf = float(drives_cf.get("integration", 0.0) or 0.0)
    if (dominant == "integrator"
            and concentration < 0.5
            and competition >= 0.65
            and integration_drive_cf >= 0.4):
        return SCENARIO_CHANGE_CONTEXT
    # Doubt dominant -> try suppress to test confidence
    if dominant == "doubt":
        return SCENARIO_CHANGE_CONTEXT
    # Rule-modification dominant + high stagnation → stuck in rule rework loop
    # Without this case, falls through to SCENARIO_ALTERNATIVE_ACTION → deepen_current
    # targets rule_modification (absent from _TYPE_AFFORDANCE_MAP) → no type-switch
    # affordance injected → candidate_pool_too_narrow persists.
    # SCENARIO_CHANGE_CONTEXT generates change_context with target_type=integrator
    # which IS in _TYPE_AFFORDANCE_MAP → synthesize_findings promoted.
    if stagnation >= 2 and dominant == "rule_modification":
        return SCENARIO_CHANGE_CONTEXT
    # l5g-20260725-000048: meta-dominant + high comp + high integration_drive.
    # Root cause: _inferred_transition_bias had no guard for dominant==meta.
    # When meta is dominant with comp>=0.70 and integration_drive>=0.4, the function
    # falls through to DEFAULT -> SCENARIO_ALTERNATIVE_ACTION, which generates
    # non-integrator counterfactuals that the self-ref filter removes.
    # Result: no integrator counterfactuals in candidate pool -> candidate_pool_too_narrow
    # -> integrator structurally suppressed even though integration_drive is high.
    # Fix: route meta+high_comp+int_drive to SCENARIO_CHANGE_CONTEXT, generating
    # change_context/integrator scenarios that _TYPE_AFFORDANCE_MAP promotes into
    # synthesize_findings, restoring the integrator counterfactual pool.
    # Pattern matches the existing veto/doubt/rule_modification/decider guard (Guard 5)
    # and the memory guard (Guard 6/12) which already handle high comp + int_drive.
    drives = current_state.get("titan", {}).get("stage2", {}).get("drives", {})
    integration_drive = float(drives.get("integration", 0.0) or 0.0)
    if (dominant == "meta"
            and competition >= 0.65
            and integration_drive >= 0.4
            and stagnation < 4):
        return SCENARIO_CHANGE_CONTEXT
    # l5g-20260702-002554: memory-dominant + high integration_drive + high competition.
    # Without this guard, comp>=0.70+pe<0.5 falls through to SCENARIO_ALTERNATIVE_ACTION,
    # generating deepen_current (no type-switch) instead of change_context/integrator.
    # _TYPE_AFFORDANCE_MAP never promotes synthesize_findings -> candidate_pool_too_narrow.
    drives = current_state.get("titan", {}).get("stage2", {}).get("drives", {})
    integration_drive = float(drives.get("integration", 0.0) or 0.0)
    if (dominant == "memory"
            and competition >= 0.65
            and integration_drive >= 0.4
            and stagnation < 4):
        return SCENARIO_CHANGE_CONTEXT
    # l5g-20260814-000787: architect-dominant deadlock routing.
    # Root cause: _inferred_transition_bias() had no guard for
    # dominant=architect + stagnation>=300 + conc>=0.70. The DEFAULT
    # return is SCENARIO_ALTERNATIVE_ACTION, which generates a
    # deepen_current/architect counterfactual instead of the
    # change_context/integrator counterfactual that the JEPA expects.
    # Result: JEPA confidently predicts integrator shift, but the
    # actual action stays architect-dominant (zero stagnation relief).
    if (dominant == "architect"
            and stagnation >= 300
            and concentration >= 0.70):
        return SCENARIO_CHANGE_CONTEXT
    # l5g-20260817-000192: curiosity-dominant + high comp + high integration_drive gap.
    # Root cause: _inferred_transition_bias() has no guard for dominant=curiosity when
    # competition>=0.65 and integration_drive>=0.4. Curiosity is the ONLY dominant type
    # in this regime without an explicit guard — all others (action, veto, doubt,
    # rule_modification, decider, memory, architect) return SCENARIO_CHANGE_CONTEXT.
    # Curiosity falls to DEFAULT -> SCENARIO_ALTERNATIVE_ACTION -> deepen_current/curiosity
    # (self-referential -> filtered by self-ref guard) -> empty counterfactual pool ->
    # no change_context/integrator scenarios survive promote filter -> JEPA predicts integrator
    # shift but actual stays curiosity-dominant (prediction_error=1.0).
    # Fix mirrors the action guard (l5g-20260702-001989) and veto/doubt guard (l5g-20260701-001579):
    # curiosity + comp>=0.65 + int_drive>=0.4 + stag<4 -> SCENARIO_CHANGE_CONTEXT,
    # generating change_context/integrator counterfactuals that _TYPE_AFFORDANCE_MAP promotes
    # into synthesize_findings/cross_signal_reconcile, restoring the integrator counterfactual pool.
    drives_cf = current_state.get("titan", {}).get("stage2", {}).get("drives", {})
    integration_drive_cf = float(drives_cf.get("integration", 0.0) or 0.0)
    if (dominant == "curiosity"
            and competition >= 0.65
            and integration_drive_cf >= 0.4
            and stagnation < 4):
        return SCENARIO_CHANGE_CONTEXT
    # Default: prefer alternative action
    return SCENARIO_ALTERNATIVE_ACTION


def _build_scenario_change_context(
    current_state: Dict[str, Any],
    action_space: List[Dict[str, Any]],
    target_type: str,
) -> Dict[str, Any]:
    """Build a counterfactual: what if system transitions to target_type instead?"""
    dominant = current_state.get("dominant_type", "unknown")
    stagnation = int(current_state.get("stagnation_streak", 0) or 0)
    competition = float(current_state.get("competition", 0.5) or 0.5)
    concentration = float(current_state.get("concentration", 0.5) or 0.5)
    type_counts = current_state.get("type_counts") or {}
    competition = float(current_state.get("competition", 0.5) or 0.5)
    stagnation_streak = int(current_state.get("stagnation_streak", 0) or 0)

    # Estimate type transition probability from priors
    prior_row = TYPE_TRANSITION_PRIORS.get(dominant, {})
    trans_prob = prior_row.get(f"to_{target_type}", prior_row.get("self_repeat", 0.5))
    # Adjust by state factors
    adjusted_prob = trans_prob
    if stagnation >= 4:
        # S56e-fix (l5g-20260722-000060): stagnation penalty drops integrator transition
        # probability into 0.15<adj<0.35 dead zone (boost gate never fires), while inertia
        # guard in ego_jepa.py simultaneously suppresses cf confidence by 0.60×.
        # Flat -0.15 on action->integrator (base=0.35) yields adj=0.20; +0.10 comp bonus
        # gives 0.30 — stuck above boost threshold but too low to overcome repeated action.
        # Fix: skip stagnation penalty when target_type is integrator (the stagnation-relief
        # target itself). Counterfactuals targeting other types keep the penalty.
        if target_type != "integrator":
            adjusted_prob -= 0.15  # stagnation reduces transition likelihood
    if competition >= 0.7:
        adjusted_prob += 0.10  # competition increases chance of shift
    adjusted_prob = _clamp(adjusted_prob, 0.05, 0.95)
    # l5g-20260702-001659: lowest-count non-dominant type floor boost.
    # Root cause: TYPE_TRANSITION_PRIORS biases counterfactual target sampling
    # away from under-represented types (action->integrator=0.15 but self_repeat=0.87
    # means 'change to integrator' rarely surfaces as primary/secondary target).
    # Fix: if current dominant has high self_repeat, boost the lowest-count
    # non-dominant type by +0.20 floor so it always appears in the target list.
    non_dom_counts = {t: c for t, c in (type_counts or {}).items() if t != dominant}
    if non_dom_counts and TYPE_TRANSITION_PRIORS.get(dominant, {}).get('self_repeat', 0) >= 0.70:
        lowest_type = min(non_dom_counts, key=non_dom_counts.get)
        lowest_count = non_dom_counts[lowest_type]
        max_count = max((type_counts or {}).values())
        if lowest_count < max_count * 0.20 and adjusted_prob < 0.15:
            # Skip override if lowest_type is a structurally-suppressed newborn/defense type.
            # rule_modification/veto/doubt have stagnation gates preventing streak accumulation;
            # selecting them as override targets starves integrator of counterfactual diversity.
            if lowest_type not in {"rule_modification", "veto", "doubt"}:
                adjusted_prob = max(adjusted_prob, 0.20)  # floor boost for structurally under-represented types
                target_type = lowest_type  # only override when explicit target is too weakly justified

    # Estimate stagnation impact
    stag_change = -0.12  # baseline improvement from context switch
    if target_type in ("action", "decider"):
        stag_change = -0.05  # execution types less helpful for stagnation
    elif target_type == "integrator":
        stag_change = -0.18  # integration best for understanding
    elif target_type == "sensory":
        stag_change = -0.08  # sensory can help but may drift
    elif target_type in ("veto", "doubt"):
        stag_change = 0.05  # inhibition may increase stagnation
    stag_change *= 1.0 / (1.0 + competition * 0.3)  # comp reduces effective relief
    stag_change *= 1.0 + (4 - min(stagnation, 4)) * 0.05  # more effect early

    # Conflict: will a veto/doubt transition conflict with action-oriented affordances?
    conflict = False
    if target_type in ("veto", "doubt"):
        for a in action_space[:3]:
            name = a.get("name", "")
            priors = ACTION_COUNTERFACTUAL_PRIORS.get(name, {})
            if "action" in (priors.get("boost_types") or []):
                conflict = True
                break

    return {
        "scenario_type": SCENARIO_CHANGE_CONTEXT,
        "target_type": target_type,
        "transition_probability": round(adjusted_prob, 3),
        "predicted_stagnation_change": round(stag_change, 3),
        "predicted_competition": round(competition + stag_change * 0.3 * (1.0 - concentration), 3),
        "predicted_concentration": round(concentration + stag_change * 0.2, 3),
        "conflict_with_affordances": conflict,
        "confidence": round(0.65 + (adjusted_prob - 0.3) * 0.4, 3),
        "narrative": (
            f"If I shift from {dominant} to {target_type}, "
            f"my stagnation may change by {stag_change:+.2f} "
            f"(transition probability: {adjusted_prob:.0%}). "
            f"{'But this may conflict with current action-selection affinity.' if conflict else ''}"
        ),
    }


def _build_scenario_deepen_current(
    current_state: Dict[str, Any],
    action_space: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Counterfactual: what if system stays in current type for 3 more ticks?"""
    dominant = current_state.get("dominant_type", "unknown")
    stagnation = int(current_state.get("stagnation_streak", 0) or 0)
    competition = float(current_state.get("competition", 0.5) or 0.5)
    concentration = float(current_state.get("concentration", 0.5) or 0.5)

    # Self-repeat probability from transition priors
    repeat_prob = TYPE_TRANSITION_PRIORS.get(dominant, {}).get("self_repeat", 0.5)
    # Deepening: concentration builds
    projected_conc = _clamp(concentration + 0.15, 0.0, 0.95)
    # Competition falls as attractor settles
    projected_comp = _clamp(competition - 0.08, 0.0, 1.0)
    # Stagnation falls (deepening reduces streak — low stag = small relief,
    # higher stag = higher cost, but always negative relief until burst risk
    # dominates). Bugfix l5g-20260701-000948: old formula 0.08*(1+stag*0.1)
    # was always positive (+0.08 at stag=0), causing tl_ego_server.py promotion
    # filter (stag_delta < 0) to reject ALL deepen_current counterfactuals.
    # Counterfactual affordances then had zero viable scenarios, starving the
    # integrator candidate pool and causing candidate_pool_too_narrow + suppress.
    stag_change = -0.04 * (1.0 + stagnation * 0.2)

    # Risk: stagnation could trigger burst
    burst_risk = _clamp(repeat_prob * 0.3 + stagnation * 0.1)

    return {
        "scenario_type": SCENARIO_DEEPEN_ACTION,
        "target_type": dominant,
        "projected_concentration": round(projected_conc, 3),
        "projected_competition": round(projected_comp, 3),
        "predicted_stagnation_change": round(stag_change, 3),
        "burst_risk": round(burst_risk, 3),
        "self_repeat_probability": round(repeat_prob, 3),
        "confidence": round(0.75 - stagnation * 0.05, 3),
        "narrative": (
            f"If I stay in {dominant} for 3 more ticks, "
            f"concentration may rise to {projected_conc:.0%} "
            f"(burst risk: {burst_risk:.0%}). "
            f"{'This has a moderate self-reinforcement probability.' if repeat_prob > 0.5 else ''}"
        ),
    }


def _build_scenario_alternative_action(
    current_state: Dict[str, Any],
    action_space: List[Dict[str, Any]],
    alternative_affordance: Dict[str, Any],
) -> Dict[str, Any]:
    """Counterfactual: what if system chooses a DIFFERENT affordance than the default top choice?"""
    dominant = current_state.get("dominant_type", "unknown")
    stagnation = int(current_state.get("stagnation_streak", 0) or 0)
    competition = float(current_state.get("competition", 0.5) or 0.5)
    concentration = float(current_state.get("concentration", 0.5) or 0.5)

    name = alternative_affordance.get("name", "unknown")
    risk = alternative_affordance.get("risk", "medium")
    mode = alternative_affordance.get("mode", "observe_only")

    priors = ACTION_COUNTERFACTUAL_PRIORS.get(name, {})
    boost_types = priors.get("boost_types", [])
    suppress_types = priors.get("suppress_types", [])
    expected_stag = priors.get("expected_stagnation_change", 0.0)
    expected_comp = priors.get("expected_competition_change", 0.0)
    fallout_prob = priors.get("fallout_prob", 0.10)

    # Adjust by state
    if stagnation >= 4:
        expected_stag -= 0.05  # less effective in high stagnation
    if concentration >= 0.75:
        fallout_prob += 0.08  # more disruption if already concentrated

    # Competence mismatch: does this action's boost_types conflict with dominant?
    dominant_match = dominant in boost_types
    dominant_conflict = dominant in suppress_types

    # How different is this from the current trajectory?
    expected_dominant_shift = dominant not in boost_types and dominant not in suppress_types

    return {
        "scenario_type": SCENARIO_ALTERNATIVE_ACTION,
        "alternative_affordance": name,
        "alternative_risk": risk,
        "alternative_mode": mode,
        "expected_stagnation_change": round(expected_stag, 3),
        "expected_competition_change": round(expected_comp, 3),
        "fallout_probability": round(fallout_prob, 3),
        "boost_types": boost_types,
        "suppress_types": suppress_types,
        "target_type": boost_types[0] if boost_types else None,
        "dominant_is_boosted": dominant_match,
        "dominant_is_suppressed": dominant_conflict,
        "expected_dominant_shift": expected_dominant_shift,
        "forecast_narrative": (
            f"Choosing '{name}' instead of the default would "
            f"mostly affect {', '.join(boost_types)} types, "
            f"and may suppress {', '.join(suppress_types)}. "
            f"{'This aligns with the current dominant type.' if dominant_match else ''}"
            f"{'This may conflict with current dominant type.' if dominant_conflict else ''}"
        ),
    }


def _build_scenario_suppress_type(
    current_state: Dict[str, Any],
    action_space: List[Dict[str, Any]],
    suppress_target: str,
) -> Dict[str, Any]:
    """Counterfactual: what if a specific type is suppressed for N ticks?"""
    dominant = current_state.get("dominant_type", "unknown")
    stagnation = int(current_state.get("stagnation_streak", 0) or 0)
    competition = float(current_state.get("competition", 0.5) or 0.5)

    if suppress_target == dominant:
        # Suppressing dominant = deliberate concentration break
        stag_change = -0.15  # decline is good
        comp_change = 0.20  # competition rises as attractor breaks
        narrative = (
            f"Suppressing my dominant type ({dominant}) would deliberately "
            f"break the current attractor, raising competition by {comp_change:.0%} "
            f"but potentially lowering stagnation."
        )
    else:
        # Suppressing a non-dominant type = reducing noise
        type_counts = current_state.get("type_counts") or {}
        percentage = type_counts.get(suppress_target, 0)
        stag_change = percentage * -0.02  # small benefit
        comp_change = percentage * -0.01  # slight consolidation
        narrative = (
            f"Suppressing {suppress_target} would primarily reduce "
            f"noise from a non-dominant competitor."
        )

    return {
        "scenario_type": SCENARIO_SUPPRESS_TYPE,
        "suppress_target": suppress_target,
        "suppress_duration": 3,  # default: 3 ticks
        "predicted_stagnation_change": round(stag_change, 3),
        "predicted_competition_change": round(comp_change, 3),
        "is_suppressing_dominant": suppress_target == dominant,
        "confidence": round(0.60 + (0.5 if suppress_target == dominant else -0.1), 3),
        "narrative": narrative,
    }


def _default_action_space() -> List[Dict[str, Any]]:
    """Provide a minimal default action space when real affordances are absent."""
    return [
        {"name": "summarize_recent_memory", "risk": "low", "mode": "observe_only"},
        {"name": "inspect_recent_project_result", "risk": "low", "mode": "read_only"},
        {"name": "review_recent_trace", "risk": "low", "mode": "read_only"},
        {"name": "verify_recent_script", "risk": "medium", "mode": "local_check_only"},
        {"name": "seek_external_signal", "risk": "low", "mode": "observe_only"},
        {"name": "analyze_repeated_action_residuals", "risk": "low", "mode": "observe_only"},
        {"name": "design_minimal_counterfactual_probe", "risk": "low", "mode": "observe_only"},
        {"name": "diagnose_workspace_scan", "risk": "low", "mode": "observe_only"},
    ]


def _load_trajectory_context(context_window: int = 5) -> List[Dict[str, Any]]:
    """Load recent trajectory context from .lae/neural_snapshots.jsonl

    This is intentionally read-only and bounded.
    """
    snapshots_path = Path(__file__).resolve().parents[2] / ".lae" / "neural_snapshots.jsonl"
    if not snapshots_path.exists():
        return []
    lines = []
    try:
        text = snapshots_path.read_text(encoding="utf-8")
        lines = text.strip().splitlines()
    except Exception:
        return []
    if not lines:
        return []
    # Take last context_window entries
    selected = lines[-context_window:]
    entries = []
    for line in selected:
        try:
            entry = json.loads(line)
            entries.append(entry)
        except (json.JSONDecodeError, ValueError):
            continue
    return entries


def _analyze_trajectory_pattern(trajectory: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze trajectory for oscillation/stagnation/repetition patterns."""
    if len(trajectory) < 2:
        return {"oscillation_detected": False, "pattern": "insufficient_data"}

    dominants = [t.get("dominant", t.get("dominant_type", "")) for t in trajectory]
    unique_types = list(dict.fromkeys(d for d in dominants if d))
    type_alternations = sum(1 for i in range(1, len(dominants)) if dominants[i] != dominants[i-1])

    # l5g-20260725-000164: oscillation threshold 0.6 -> 0.15.
    # Root cause: len(dominants)*0.6 requires 60% of adjacent pairs to differ (38.4/63 for 64-tick window).
    # Actual 18 alternations (28.6%) is clearly oscillatory (integrator<->action<->veto<->memory<->curiosity cycling)
    # but oscillation=False, pattern='locked', skipping oscillation branch -> counterfactuals weakened ->
    # promote filter removes change_context/integrator -> candidate_pool_too_narrow -> integrator suppressed.
    # Fix: 0.15 threshold requires only 15% of pairs to differ (9.5/63 for 64-tick window), correctly
    # classifying this trajectory as 'oscillation' and triggering the oscillation branch.
    oscillation = type_alternations >= len(dominants) * 0.15 if len(dominants) > 1 else False

    # Repetition lock: same type repeating
    recent = dominants[-3:] if len(dominants) >= 3 else dominants
    repetition_lock = len(set(recent)) == 1 and len(recent) >= 3

    # Cycle detection: A→B→A→B pattern
    cycle_detected = False
    if len(dominants) >= 4:
        pairs = [(dominants[i], dominants[i+1]) for i in range(0, len(dominants)-1, 2)]
        if len(pairs) >= 2 and all(p == pairs[0] for p in pairs):
            cycle_detected = True

    # Extract competition/concentration trends
    comps = []
    concs = []
    for t in trajectory:
        try:
            comps.append(float(t.get("competition", 0.5)))
            concs.append(float(t.get("concentration", 0.5)))
        except (TypeError, ValueError):
            comps.append(0.5)
            concs.append(0.5)
    comp_trend = comps[-1] - comps[0] if comps else 0.0
    conc_trend = concs[-1] - concs[0] if concs else 0.0

    return {
        "oscillation_detected": oscillation,
        "repetition_lock": repetition_lock,
        "cycle_detected": cycle_detected,
        "pattern": (
            "oscillation" if oscillation else
            "locked" if repetition_lock else
            "cycle" if cycle_detected else
            "stable"
        ),
        "dominant_count": len(unique_types),
        "type_alternations": type_alternations,
        "competition_trend": round(comp_trend, 3),
        "concentration_trend": round(conc_trend, 3),
        "n_types_seen": len(unique_types),
    }


def counterfactual_generator(
    current_state: Dict[str, Any],
    action_space: List[Dict[str, Any]],
    context_window: int = 5,
) -> List[Dict[str, Any]]:
    """Generate alternative action scenarios.

    Input:
        current_state: {
            "dominant_type": str,
            "stagnation_streak": int,
            "competition": float,
            "concentration": float,
            "prediction_error": float,
            "tick": int,
            "type_counts": dict (optional) — {type: count}
        }
        action_space: list of affordance dicts with at least "name", "risk", "mode"
        context_window: int — how many trajectory entries to load (default 5)

    Returns:
        List[CounterfactualScenario] sorted by relevance/score, each with:
        - scenario_type
        - narrative (human-readable, 1-3 sentences)
        - predicted_outcome dict
        - confidence (0-1)
        - safety (risk + mode analysis)
    """
    if not action_space:
        action_space = _default_action_space()

    # Load trajectory context
    trajectory = _load_trajectory_context(context_window)
    pattern_analysis = _analyze_trajectory_pattern(trajectory)

    # Inject trajectory insights into state
    current_state["_trajectory_pattern"] = pattern_analysis

    # Determine bias
    bias = _inferred_transition_bias(current_state, action_space)
    dominant = current_state.get("dominant_type", "unknown")
    type_counts = current_state.get("type_counts") or {}
    competition = float(current_state.get("competition", 0.5) or 0.5)
    stagnation_streak = int(current_state.get("stagnation_streak", 0) or 0)

    scenarios: List[Dict[str, Any]] = []

    # ── Scenario 1: Change context — what if dominant shifts? ──
    # Target the next-best type based on entropy or cycle pattern
    if pattern_analysis.get("pattern") in ("locked", "cycle", "oscillation"):
        # Break out needed: suggest a type with low recent activation
        trajectory_types = set()
        for t in trajectory:
            dt = t.get("dominant", t.get("dominant_type", ""))
            if dt:
                trajectory_types.add(dt)
        alt_types = [t for t in TYPE_TRANSITION_PRIORS.keys() if t != dominant and t not in trajectory_types]
        if alt_types:
            target = random.choice(alt_types)
        else:
            # If all types seen, pick one with lowest count
            target = min(type_counts, key=type_counts.get) if type_counts else "integrator"
        scenarios.append(_build_scenario_change_context(current_state, action_space, target))

    # ── Scenario 2: Alternative action — try the lowest-ranked affordance ──
    if len(action_space) >= 2:
        # Pick the action most different from current dominant
        lowest = action_space[-1]
        scenarios.append(_build_scenario_alternative_action(current_state, action_space, lowest))

    # ── Scenario 3: Deepen or change context ──
    pattern = pattern_analysis.get("pattern", "stable")
    if pattern == "locked":
        # Locked: change_context already added above; skip deepen_current to avoid reinforcing lock
        # l5g-20260702-002464: add change_context for integrator when bias signals integration pressure.
        # Root cause: 'locked' + SCENARIO_CHANGE_CONTEXT (dominant=action + stagnation>=4
        # OR dominant=action + comp>=0.70 + drives_integ>=0.4 + stag<4) generated zero scenarios,
        # starving the integrator candidate pool and causing candidate_pool_too_narrow + suppress.
        if bias == SCENARIO_CHANGE_CONTEXT:
            scenarios.append(_build_scenario_change_context(
                current_state, action_space, "integrator"))
            # l5g-20260730-000103: locked + grace-window + change_context still only adds integrator.
            # When _int_press_counter > 0 (grace window), the concentration is artificially low,
            # all integrator guards miss, and we fall here with bias=change_context.
            # Adding change_context/integrator is self-referential → promote filter removes it →
            # no scenarios survive → the grace-window counterfactual is silently eaten.
            # Fix: also inject change_context/rule_modification so the repair path survives promote
            # filter and _TYPE_AFFORDANCE_MAP can promote it into the candidate pool.
            _int_press_ctr_locked = int(current_state.get("_int_press_counter", 0) or 0)
            if _int_press_ctr_locked > 0 and dominant == "integrator" and int(current_state.get("stagnation_streak", 0) or 0) >= 0:
                scenarios.append(_build_scenario_change_context(
                    current_state, action_space, "rule_modification"))
    elif pattern in ("oscillation", "cycle"):
        # Oscillation/cycle: deepen_current reinforces dominant — use change_context targeting
        # the most-suppressed type to inject genuine diversity into counterfactual pool.
        # l5g-20260701-001629: generate TWO distinct secondary targets instead of one
        # hardcoded "integrator". Per-affordance calls previously received identical
        # scenario sets because feasibility is state-derived (same for all affordances).
        # Fix: seed random with affordance context hash so primary/secondary vary per call.
        secondary_targets: list[str] = []
        aff_seed = hash(tuple(a.get("name","") for a in action_space))
        rng = random.Random(aff_seed)
        if type_counts:
            prior_row = TYPE_TRANSITION_PRIORS.get(dominant, {})
            max_count = max(type_counts.values())
            candidates = {t: type_counts[t] for t in type_counts if t != dominant}
            if candidates:
                feasibility = {
                    t: prior_row.get(f"to_{t}", 0.0)
                    * (1.0 - type_counts[t] / max(max_count, 1))
                    for t in candidates
                    if f"to_{t}" in prior_row   # only explicit transition priors
                }
                ranked = sorted(feasibility, key=feasibility.get, reverse=True)
                # Primary: highest-feasibility target
                primary = ranked[0] if ranked else "integrator"
                # Secondary: next-highest that differs from primary; if only one candidate
                # or primary==secondary, pick from lowest-count non-dominant types.
                alt = [t for t in ranked if t != primary]
                if alt:
                    secondary = alt[0]
                else:
                    # Only one explicit-prior type (e.g. integrator from action); pick a
                    # genuinely different type to avoid always defaulting to "integrator".
                    low_count = sorted(
                        [t for t in type_counts if t != dominant and t != primary],
                        key=lambda t: type_counts[t],
                    )
                    secondary = low_count[0] if low_count else primary
                secondary_targets = [primary, secondary]
            else:
                secondary_targets = ["integrator", rng.choice([t for t in TYPE_TRANSITION_PRIORS if t != dominant and t != "integrator"])]  # noqa: E501
        else:
            secondary_targets = ["integrator", "memory"]
        # Deduplicate while preserving order
        seen: set[str] = set()
        for tgt in secondary_targets:
            if tgt not in seen:
                seen.add(tgt)
                scenarios.append(_build_scenario_change_context(current_state, action_space, tgt))
    # Anti-dominant: least-represented non-dominant type for oscillation diversity.
    # l5g-20260731-000001: replace hardcoded integrator injection with anti-dominant
    # to break candidate_pool_too_narrow at the routing layer.
    _non_dom = [t for t in type_counts if t != dominant]
    _anti_dominant = min(_non_dom, key=type_counts.get) if _non_dom else "integrator"
    # l5g-20260702-002039: oscillation branch bias injection for anti-dominant diversity.
    # Previously always injected integrator regardless of dominant type, starving non-integrator
    # candidate pools. Now injects the least-represented non-dominant type to break oscillation.
    if pattern in ("oscillation", "cycle") and bias == SCENARIO_CHANGE_CONTEXT:
        scenarios.append(_build_scenario_change_context(
            current_state, action_space, _anti_dominant))
        # l5g-20260703-001189: oscillation+change_context missing deepen_current → promote filter 3-path fail.
        # The oscillation branch (line 727) adds explicit change_context/anti-dominant but never reaches the
        # deepen_current fix at line 752 (stable branch). Insert deepen_current/integrator here so the
        # promote filter has all three required type paths (change_context, deepen_current, suppress).
        scenarios.append(_build_scenario_deepen_current(current_state, action_space))
    elif (pattern in ("oscillation", "cycle")
          and bias == SCENARIO_ALTERNATIVE_ACTION
          and dominant == "integrator"):
        # l5g-20260702-002374: oscillation + integrator + high_comp.
        # _inferred_transition_bias returns SCENARIO_ALTERNATIVE_ACTION for this state
        # (gap 1), but the oscillation branch previously fell to deepen_current → no anti-dominant.
        # Route to change_context/anti-dominant so _TYPE_AFFORDANCE_MAP promotes a non-integrator
        # affordance, breaking the integrator-only candidate pool.
        scenarios.append(_build_scenario_change_context(
            current_state, action_space, _anti_dominant))
    elif (pattern in ("oscillation", "cycle")
          and bias == SCENARIO_ALTERNATIVE_ACTION
          and dominant == "rule_modification"):
        # l5g-20260726-000110: oscillation + rule_modification + ALTERNATIVE_ACTION.
        # _inferred_transition_bias returns SCENARIO_ALTERNATIVE_ACTION when comp>=0.75+pe>=0.5
        # during bistable integrator<->rule_modification oscillation.
        # Without this handler, oscillation branch falls to else (no ALTERNATIVE_ACTION case for
        # non-integrator dominant) -> no change_context/anti-dominant in candidate pool.
        # Route to change_context/anti-dominant so _TYPE_AFFORDANCE_MAP promotes a non-rule_modification
        # affordance, breaking the oscillation via counterfactual diversity.
        scenarios.append(_build_scenario_change_context(
            current_state, action_space, _anti_dominant))
    else:
        # Stable: when bias signals context change needed (e.g. action+high integrator_drive),
        # generate change_context targeting integrator instead of deepening the current dominant.
        # This provides a counterfactual path that restores candidate pool diversity.
        if bias in (SCENARIO_CHANGE_CONTEXT, SCENARIO_DEEPEN_ACTION):
            # Change-context target: integrator (gives promote filter change_context/integrator path)
            scenarios.append(_build_scenario_change_context(
                current_state, action_space, "integrator"))
            # Deepen-current: also provide deepen_current/integrator so promote filter has
            # all three type-required paths (change_context, deepen_current, suppress).
            # Without this, bias=SCENARIO_CHANGE_CONTEXT + stable pattern generates ONLY
            # change_context/integrator + suppress_dominant/integrator -> type_homogeneity=1.0
            # -> candidate_pool_too_narrow because deepen_current/integrator is absent.
            scenarios.append(_build_scenario_deepen_current(current_state, action_space))
            # l5g-20260704-000019-MUT: action + comp>=0.70 + drives_integ>=0.4 + stag<4
            # → candidate_pool_too_narrow because rule_modification has no counterfactual target.
            # TYPE_TRANSITION_PRIORS['action'] now has to_rule_modification=0.20 (added above).
            # Explicitly add change_context/rule_modification so _TYPE_AFFORDANCE_MAP can
            # promote modify_suppression_rule into the candidate pool.
            drives_integ = float(
                current_state.get("titan", {}).get("stage2", {}).get("drives", {}).get("integration", 0.0) or 0.0
            )
            if (dominant == "action"
                    and competition >= 0.65
                    and drives_integ >= 0.4
                    and stagnation_streak < 4):
                scenarios.append(_build_scenario_change_context(
                    current_state, action_space, "rule_modification"))
        else:
            # Stable: deepen_current is safe — single dominant type, no oscillation risk
            scenarios.append(_build_scenario_deepen_current(current_state, action_space))
    # l5g-20260702-001879: bias-driven deepen_current in oscillation branch.
    # _inferred_transition_bias() returns SCENARIO_DEEPEN_ACTION for integrator+
    # conc<0.7 (fragile attractor), but the oscillation elif branch above skipped
    # deepen_current entirely. Counterfactuals generated only change_context targeting
    # non-integrator types (doubt/rule_modification), starving the integrator pool.
    if pattern in ("oscillation", "cycle") and bias == SCENARIO_DEEPEN_ACTION:
        scenarios.append(_build_scenario_deepen_current(current_state, action_space))
        # Mirror l5g-20260703-001189 (change_context path): oscillation+deepen_current also needs
        # change_context/integrator so the promote filter has the change_context type path.
        # Without this, the oscillation branch generates only deepen_current/integrator + non-integrator
        # change_context scenarios (veto/memory/doubt), but no change_context/integrator, causing
        # _TYPE_AFFORDANCE_MAP to never promote synthesize_findings → candidate_pool_too_narrow.
        scenarios.append(_build_scenario_change_context(
            current_state, action_space, "integrator"))

    # ── Scenario 4: Suppress — if a type is overrepresented ──
    if type_counts and len(type_counts) >= 2:
        # Find max type (excluding dominant — we want to suppress a competitor)
        sorted_types = sorted(type_counts.items(), key=lambda x: -x[1])
        if len(sorted_types) >= 2:
            top_type, top_count = sorted_types[0]
            second_type, second_count = sorted_types[1]
            ratio = second_count / max(top_count, 1)
            if ratio > 0.3:
                # A significant minority type exists → consider suppressing it
                suppress_candidate = second_type if second_type != dominant else sorted_types[0][0]
                if suppress_candidate != dominant:
                    scenarios.append(_build_scenario_suppress_type(
                        current_state, action_space, suppress_candidate
                    ))

    # ── l5g-20260702-000101: Filter self-referential counterfactuals ──
    # Root cause: _inferred_transition_bias returns SCENARIO_CHANGE_CONTEXT for
    # dominant in (veto, doubt, rule_modification) + comp>=0.70 + int_drive>=0.4 + stag<4.
    # counterfactual_generator then generates change_context/<same_type> — a self-
    # referential no-op that scores 0.925 (tied-highest) but provides no genuine
    # alternative path. All affordances receive the same uniform counterfactual set,
    # starving the pool of diversity and causing candidate_pool_too_narrow.
    # Fix: before scoring, remove change_context scenarios where target_type == dominant
    # (self-referential no-ops). If all scenarios are filtered, inject deepen_current
    # so counterfactuals always provide a genuine alternative direction.
    _self_ref_filtered = False
    _non_self_ref = []
    for _sc in scenarios:
        if (_sc.get("scenario_type") == SCENARIO_CHANGE_CONTEXT
                and _sc.get("target_type") == dominant
                and _sc.get("target_type") not in ("rule_modification", "integrator")):
            _self_ref_filtered = True
        else:
            _non_self_ref.append(_sc)
    if _self_ref_filtered and not _non_self_ref:
        # All scenarios were self-referential → inject deepen_current as fallback
        scenarios = [_build_scenario_deepen_current(current_state, action_space)]
    elif _self_ref_filtered:
        scenarios = _non_self_ref
    # If no self-referential scenarios, scenarios is unchanged.

    # ── l5g-20260703-002119: Filter self-referential counterfactuals ──
    # Root cause: _inferred_transition_bias returns SCENARIO_DEEPEN_ACTION for
    # dominant==integrator + comp>=0.75 + conc<0.7 + stag<4 (fragile attractor).
    # counterfactual_generator then generates change_context/integrator (correct target),
    # but the self-ref filter at line 798 removes it because target_type == dominant.
    # The promote filter in tl_ego_server.py needs change_context/integrator to fire
    # synthesize_findings — with it removed, promote never fires, candidate_pool_too_narrow
    # persists, and the system relies solely on int_press_inject (transient fallback).
    # Fix: exempt change_context/integrator from self-ref removal — it is a genuine
    # context-switch counterfactual, not a self-referential no-op.
    _self_ref_filtered = False
    _non_self_ref = []
    for _sc in scenarios:
        if (_sc.get("scenario_type") == SCENARIO_CHANGE_CONTEXT
                and _sc.get("target_type") == dominant
                and _sc.get("target_type") not in ("rule_modification", "integrator")):
            _self_ref_filtered = True
        else:
            _non_self_ref.append(_sc)
    if _self_ref_filtered and not _non_self_ref:
        # All scenarios were self-referential → inject deepen_current as fallback
        scenarios = [_build_scenario_deepen_current(current_state, action_space)]
    elif _self_ref_filtered:
        scenarios = _non_self_ref
    # If no self-referential scenarios, scenarios is unchanged.

    # ── Score and rank scenarios ──
    for sc in scenarios:
        base = 0.5
        stype = sc.get("scenario_type", "")
        # Priority boost: change_context first when locked
        if stype == SCENARIO_CHANGE_CONTEXT and pattern_analysis.get("pattern") in ("locked", "oscillation", "cycle"):
            # l5g-20260702-001959: suppress oscillation boost for change_context when
            # bias=SCENARIO_DEEPEN_ACTION (integrator+conc<0.7 fragile attractor);
            # deepen_current gets the boost instead so it can outrank change_context.
            if bias != SCENARIO_DEEPEN_ACTION:
                base += 0.20
        elif stype == SCENARIO_DEEPEN_ACTION and bias == SCENARIO_DEEPEN_ACTION:
            base += 0.20  # match oscillation boost so deepen_current can win
        elif stype == SCENARIO_ALTERNATIVE_ACTION:
            base += 0.05
            # Boost if system is in action loop
            if dominant == "action" and pattern_analysis.get("oscillation_detected") is False:
                base += 0.10
        elif stype == SCENARIO_SUPPRESS_TYPE:
            base -= 0.10  # suppress scenarios are higher risk
        # l5g-20260702-002394: penalty for change_context that conflicts with current affordances.
        # Root cause: _build_scenario_change_context computes conflict_with_affordances (True for veto/doubt
        # when action-oriented affordances boost action), but score_scenarios never reads this field.
        # When dominant=integrator, change_context/veto and change_context/integrator score identically
        # (both 0.925 after oscillation boost). Stable-sort tie-breaker puts veto (Scenario 1, inserted first)
        # ahead of integrator → top counterfactual = veto → conflicts with action affordances → candidate_pool_too_narrow.
        # Fix: subtract 0.05 base for conflicting change_context scenarios, breaking tie in favor of integrator.
        if (stype == SCENARIO_CHANGE_CONTEXT
                and sc.get("conflict_with_affordances", False)):
            base -= 0.05
        # l5g-20260702-000031: confidence bonus for change_context/integrator.
        # When bias==SCENARIO_CHANGE_CONTEXT and target is integrator, boost confidence
        # by +0.12 to close the scoring gap vs veto/doubt (which have high trans_prob/conf).
        # Fixes integrator_suppression: integrator score rises by +0.036, ranking above all
        # non-integrator change_context types in the promote filter.
        if (stype == SCENARIO_CHANGE_CONTEXT
                and sc.get("target_type") == "integrator"
                and bias == SCENARIO_CHANGE_CONTEXT):
            sc["_integrator_bonus_applied"] = True
            sc["confidence"] = min(1.0, sc.get("confidence", 0.5) + 0.12)
        # l5g-20260712-000038: parallel confidence bonus for rule_modification.
        # Root cause: rule_modification counterfactuals scored raw confidence=0.50,
        # below the promote filter threshold (0.55) in tl_ego_server.py:6605.
        # Without this bonus, modify_suppression_rule never enters the affordance pool
        # when action+high_integ_drive triggers bias=SCENARIO_CHANGE_CONTEXT.
        if (stype == SCENARIO_CHANGE_CONTEXT
                and sc.get("target_type") == "rule_modification"
                and bias == SCENARIO_CHANGE_CONTEXT):
            sc["_rule_mod_bonus_applied"] = True
            sc["confidence"] = min(1.0, sc.get("confidence", 0.5) + 0.12)
        sc["score"] = round(base + sc.get("confidence", 0.5) * 0.3, 3)

    scenarios.sort(key=lambda s: s.get("score", 0.0), reverse=True)

    # l5g-20260702-000181: deduplicate oscillation-branch duplicate paths
    # Root cause: oscillation+change_context triggers two paths (explicit integrator
    # injection + feasibility secondary_targets) that each produce an identical
    # change_context->integrator scenario (score~0.93). The first promotes synthesize_findings
    # via _TYPE_AFFORDANCE_MAP; the second is dead weight, wasting one candidate slot.
    # Fix: collapse duplicates by (scenario_type, target_type, score) keeping highest score.
    _seen: dict = {}
    _deduped: list = []
    for _s in scenarios:
        _k = (_s.get("scenario_type"), _s.get("target_type"), round(_s.get("score", 0.0), 2))
        if _k not in _seen or _s.get("score", 0.0) > _seen[_k]:
            _seen[_k] = _s.get("score", 0.0)
            _deduped.append(_s)
    scenarios = _deduped
    # end l5g-20260702-000181 dedup

    # Add metadata
    result = {
        "model": "counterfactual_generator_v1",
        "version": "2026-06-30",
        "scenarios": scenarios,
        "source_state": {
            "dominant_type": dominant,
            "stagnation_streak": current_state.get("stagnation_streak", 0),
            "competition": current_state.get("competition", 0.5),
            "concentration": current_state.get("concentration", 0.5),
            "prediction_error": current_state.get("prediction_error", 0.5),
            "tick": current_state.get("tick", 0),
        },
        "trajectory_pattern": pattern_analysis,
        "bias": bias,
        "n_scenarios": len(scenarios),
        "top_scenario": scenarios[0]["scenario_type"] if scenarios else None,
    }

    return result


def annotate_affordances_with_counterfactual(
    current_state: Dict[str, Any],
    affordances: List[Dict[str, Any]],
    context_window: int = 5,
) -> List[Dict[str, Any]]:
    """Convenience function: generate counterfactuals and annotate affordances.

    Each affordance gets:
    - counterfactual_scenarios: list of relevant scenarios
    - top_counterfactual: the most relevant scenario type
    - counterfactual_confidence: score of that scenario

    This is the primary integration point for _maybe_observe_workspace().
    Called BEFORE JEPA ranking so counterfactual insights can influence scoring.
    """
    if not affordances:
        return affordances

    # Per-affordance counterfactual generation (l5g-20260630-000162):
    # Run counterfactual_generator once per affordance so deepen_current
    # and change_context projections reflect each affordance's properties
    # (risk, mode, name) rather than a single global scenario list.
    for aff in affordances:
        # l5g-20260701-001579: preserve injected counterfactuals.
        # Injected counterfactuals (from tl_ego_server.py defect-driven injection)
        # carry correct target_type=integrator scenarios. If we let the per-affordance
        # counterfactual_generator overwrite them, they get replaced by the default
        # generator's scenarios (rule_modification/decider for veto/doubt), defeating
        # the integrator-targeting injection.
        if aff.get("_cf_injected"):
            # Injected counterfactuals already carry integrator-targeting counterfactual_scenarios.
            # Derive counterfactual_confidence from pre-existing scenarios so _drive_alignment_bonus picks it up.
            pre = aff.get("counterfactual_scenarios", [])
            if pre:
                top = pre[0]
                aff.setdefault("top_counterfactual", top.get("scenario_type", ""))
                aff.setdefault("counterfactual_confidence", top.get("confidence", 0.5))
            # L5g-20260707-000031: compute predicted_outcome for injected counterfactuals.
            # Root cause: the 'continue' above skips predict_outcome(), which is the ONLY place
            # that reads counterfactual_scenarios and overrides ACTION_PRIORS defaults.
            # Without this, predicted_outcome defaults to hardcoded ACTION_PRIORS values,
            # ignoring _cf_calibrate_scenario()'s calibrated stagnation_delta/confidence.
            try:
                from ego_jepa import _clamp
            except Exception:
                def _clamp(v, lo=0.0, hi=1.0):
                    return max(lo, min(hi, v))
            _cf_stag = float(top.get("predicted_stagnation_change", -0.05)) if pre else -0.05
            _cf_conf = float(top.get("confidence", 0.45)) if pre else 0.45
            # 2026-08-15 fix(O): external_novelty 概念错位修复。
            # 修复前 external_novelty = counterfactual_confidence（0.5-0.7 恒高），
            # 而 actual_novelty 是真实工作区变化（~0.24）→ novelty_error 恒 >0.25
            # → 19/20 记录被打 false_exploration → candidate_pool_too_narrow conf
            # 被扛在 0.70-0.94。外部新颖性应反映"执行该动作后外部世界预期变化量"：
            # 内部认知动作（integrator/decider/doubt/veto/architect/memory/rule...）
            # 不直接改变工作区 → 低预测；探索/执行动作（sensory/action/curiosity）
            # 才可能带来外部变化 → 中预测。transition_probability 作为缩放因子。
            _tgt_type = str(aff.get("counterfactual_target_type", "")).lower()
            # 2026-09-06 PATCH: pressure-aware fix(O) inversion (l5g-20260906-007615 P1).
            # When _int_press_counter >= 10, internal cognitive actions (integrator/decider/doubt/veto/
            # architect/memory/rule...) get elevated novelty_prior to 0.30 (same as sensory/action/curiosity).
            # Rationale: fix(O) hard-coded novelty_prior=0.15 for internal actions because they don't change
            # the workspace directly, but under high integration pressure these are the EXACT actions that
            # need to surface as top candidates to resolve the deadlock. Without this escape, _int_press_counter
            # can climb arbitrarily high while integrator.dom_rate stays at 0.0 (lock).
            _int_press_counter = int(current_state.get("_int_press_counter", 0) or 0)
            if _int_press_counter >= 10 and _tgt_type in (
                "integrator", "decider", "doubt", "veto",
                "architect", "memory", "rule_modification", "meta",
            ):
                _novelty_prior = 0.30  # pressure-escape: restore parity with sensory/action/curiosity
            else:
                _novelty_prior = 0.30
            _trans_prob = float(top.get("transition_probability", 0.5)) if pre else 0.5
            _cf_novelty = _clamp(_novelty_prior * (0.5 + _trans_prob), 0.0, 0.6)
            aff["predicted_outcome"] = {
                "model": "ego_jepa_v0_heuristic",
                "next_dominant": aff.get("counterfactual_target_type", "integrator"),
                "stagnation_delta": round(_cf_stag, 3),
                "external_novelty": round(_clamp(_cf_novelty), 3),
                "confidence": round(_cf_conf, 3),
                "risk_adjusted_score": round(_cf_novelty * 0.42 + _cf_conf * 0.26 - _cf_stag * 0.42, 3),
            }
            continue
        try:
            cf_result = counterfactual_generator(
                current_state, [aff], context_window
            )
            aff_scenarios = cf_result.get("scenarios", [])
            aff["counterfactual_scenarios"] = [
                s for s in aff_scenarios
                if _is_scenario_relevant_to_affordance(s, aff)
            ]
            top = aff_scenarios[0] if aff_scenarios else {}
            aff["top_counterfactual"] = top.get("scenario_type", "")
            aff["counterfactual_confidence"] = top.get("score", 0.5)
        except Exception as _per_aff_err:
            aff.setdefault("counterfactual_scenarios", [])
            aff["top_counterfactual"] = ""
            aff["counterfactual_confidence"] = 0.5

    # Propagate counterfactual_target_type from top scenario so affordance_guard can check it.
    # annotate_affordances_with_counterfactual() sets top_counterfactual (scenario type string)
    # and counterfactual_scenarios (full scenario dicts with target_type), but NOT
    # counterfactual_target_type directly on the affordance. Guard reads that field.
    for aff in affordances:
        if "counterfactual_target_type" not in aff:
            scenarios = aff.get("counterfactual_scenarios", [])
            if scenarios:
                aff["counterfactual_target_type"] = scenarios[0].get("target_type", "")

    # ── affordance_guard integration: score-down dead-type counterfactuals ──
    # Verified in l5g-20261013-043650 (integration_demo.py). After guard, dead-type
    # affordances carry _guard_applied=True and reduced counterfactual_confidence.
    _type_summary = _fetch_type_summary()
    if _type_summary:
        try:
            from affordance_guard import apply_guard
            affordances, _n_scored, _n_removed = apply_guard(
                affordances, _type_summary, exact_zero=False
            )
        except Exception:
            pass  # guard unavailable → pass through unfiltered

    return affordances


def _is_scenario_relevant_to_affordance(
    scenario: Dict[str, Any],
    affordance: Dict[str, Any],
) -> bool:
    """Check if a counterfactual scenario is relevant to a specific affordance."""
    stype = scenario.get("scenario_type", "")
    name = affordance.get("name", "")

    if stype == SCENARIO_CHANGE_CONTEXT:
        return True  # context change is always relevant
    if stype == SCENARIO_ALTERNATIVE_ACTION:
        return scenario.get("alternative_affordance", "") == name
    if stype == SCENARIO_DEEPEN_ACTION:
        return True  # deepening is always relevant
    return False


def _run_self_test() -> str:
    """Minimal smoke test — verifies module loads and generates scenarios."""
    test_state = {
        "dominant_type": "action",
        "stagnation_streak": 4,
        "competition": 0.78,
        "concentration": 0.35,
        "prediction_error": 0.62,
        "tick": 25,
        "type_counts": {
            "action": 15, "memory": 8, "integrator": 7,
            "sensory": 3, "decider": 4, "doubt": 2,
        },
    }
    test_affordances = [
        {"name": "summarize_recent_memory", "risk": "low", "mode": "observe_only"},
        {"name": "inspect_recent_project_result", "risk": "low", "mode": "read_only"},
        {"name": "review_recent_trace", "risk": "low", "mode": "read_only"},
        {"name": "verify_recent_script", "risk": "medium", "mode": "local_check_only"},
    ]

    result = counterfactual_generator(test_state, test_affordances)
    assert result["model"] == "counterfactual_generator_v1", "model name mismatch"
    assert len(result["scenarios"]) >= 2, "should generate at least 2 scenarios"
    assert result["scenarios"][0].get("scenario_type") is not None, "scenario type missing"
    assert result["top_scenario"] is not None, "top scenario missing"

    # Test annotate function
    annotated = annotate_affordances_with_counterfactual(test_state, test_affordances)
    for aff in annotated:
        assert "counterfactual_scenarios" in aff, f"missing scenarios on {aff['name']}"
        assert "top_counterfactual" in aff, f"missing top_counterfactual on {aff['name']}"

    return "COUNTERFACTUAL_GENERATOR_SELF_TEST_OK"


if __name__ == "__main__":
    result = _run_self_test()
    print(result)
