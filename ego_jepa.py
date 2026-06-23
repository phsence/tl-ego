#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ego-JEPA v0: lightweight action-outcome predictor for TL-Ego.

This module is intentionally dependency-free. v0 is a heuristic latent world
model used to rank safe affordances before execution. It predicts low-dimensional
outcomes instead of reconstructing text or file contents.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple


LOW_RISK_BONUS = {
    "low": 0.18,
    "medium": -0.04,
    "high": -0.35,
}

MODE_BONUS = {
    "observe_only": 0.16,
    "read_only": 0.14,
    "local_check_only": 0.06,
}

ACTION_PRIORS = {
    "summarize_recent_memory": {
        "next_dominant": "memory",
        "stagnation_delta": -0.18,
        "external_novelty": 0.42,
        "confidence": 0.64,
    },
    "inspect_recent_project_result": {
        "next_dominant": "integrator",
        "stagnation_delta": -0.24,
        "external_novelty": 0.62,
        "confidence": 0.68,
    },
    "review_recent_trace": {
        "next_dominant": "reasoning",
        "stagnation_delta": -0.12,
        "external_novelty": 0.48,
        "confidence": 0.58,
    },
    "verify_recent_script": {
        "next_dominant": "action",
        "stagnation_delta": -0.06,
        "external_novelty": 0.36,
        "confidence": 0.54,
    },
    "seek_external_signal": {
        "next_dominant": "sensory",
        "stagnation_delta": -0.08,
        "external_novelty": 0.28,
        "confidence": 0.52,
    },
    "diagnose_workspace_scan": {
        "next_dominant": "reasoning",
        "stagnation_delta": -0.04,
        "external_novelty": 0.18,
        "confidence": 0.48,
    },
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _state_pressure(ego_state: Dict[str, Any]) -> float:
    stagnation = float(ego_state.get("stagnation_streak", 0) or 0)
    competition = float(ego_state.get("competition", 0.5) or 0.5)
    concentration = float(ego_state.get("concentration", 0.5) or 0.5)
    return _clamp(stagnation / 6.0 + max(0.0, competition - 0.65) * 0.45 + max(0.0, concentration - 0.82) * 0.35)


def _observation_novelty(observation: Dict[str, Any]) -> float:
    count = float(observation.get("count", 0) or 0)
    categories = observation.get("categories", {}) or {}
    category_count = len(categories)
    return _clamp(count / 12.0 + category_count * 0.08)


def predict_outcome(ego_state: Dict[str, Any], observation: Dict[str, Any], affordance: Dict[str, Any]) -> Dict[str, Any]:
    name = affordance.get("name", "unknown")
    prior = ACTION_PRIORS.get(name, {
        "next_dominant": "sensory",
        "stagnation_delta": -0.05,
        "external_novelty": 0.25,
        "confidence": 0.45,
    })
    pressure = _state_pressure(ego_state)
    novelty = _observation_novelty(observation)
    risk = affordance.get("risk", "medium")
    mode = affordance.get("mode", "observe_only")

    predicted_novelty = _clamp(prior["external_novelty"] + novelty * 0.35)
    stagnation_delta = float(prior["stagnation_delta"]) - pressure * 0.10 + predicted_novelty * -0.06
    if risk != "low":
        stagnation_delta += 0.08

    confidence = _clamp(float(prior["confidence"]) + novelty * 0.12 - (0.08 if risk != "low" else 0.0))
    score = predicted_novelty * 0.42 + confidence * 0.26 - max(0.0, stagnation_delta) * 0.28
    score += LOW_RISK_BONUS.get(risk, -0.1) + MODE_BONUS.get(mode, 0.0)

    return {
        "model": "ego_jepa_v0_heuristic",
        "next_dominant": prior["next_dominant"],
        "stagnation_delta": round(stagnation_delta, 3),
        "external_novelty": round(predicted_novelty, 3),
        "confidence": round(confidence, 3),
        "risk_adjusted_score": round(score, 3),
    }


def rank_affordances(
    ego_state: Dict[str, Any],
    observation: Dict[str, Any],
    affordances: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    ranked: List[Tuple[float, Dict[str, Any]]] = []
    for affordance in affordances:
        item = dict(affordance)
        outcome = predict_outcome(ego_state, observation, item)
        item["predicted_outcome"] = outcome
        item["jepa_score"] = outcome["risk_adjusted_score"]
        ranked.append((float(item["jepa_score"]), item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in ranked]


# ─── Phase 14: External World Prediction ───
# Unlike the self-referential next_dominant prediction, this predicts
# real external outcomes that can be verified.

EXTERNAL_PRIORS = {
    # Given current observer state, how many workspace events in next 5 min?
    "workspace_event_count": {
        "base": 13.38,  # expected files changed per 5min (calibrated to live workspace activity)
        "memory_bump": 0.8,  # + per memory change
        "script_bump": 0.3,  # + per script change
        "competition_factor": 0.5,  # higher competition → more noise → fewer events noticed
    },
    # Will the system hit a resource warning in next 10 min?
    "resource_warning": {
        "base_prob": 0.05,
        "mem_above_85": 0.35,
        "disk_below_10gb": 0.25,
        "gpu_above_80c": 0.15,
    },
}


def predict_external(
    ego_state: Dict[str, Any],
    external_world: Dict[str, Any],
) -> Dict[str, Any]:
    """Predict external world outcomes based on current state.
    
    Unlike predict_outcome (which predicts next_dominant — self-referential),
    this predicts real external events that can be verified later.
    
    Returns predictions with confidence scores that can be checked against
    reality on the next tick.
    """
    competition = float(ego_state.get("competition", 0.5) or 0.5)
    stagnation = int(ego_state.get("stagnation_streak", 0) or 0)
    
    ws = external_world.get("workspace", {})
    health = external_world.get("system_health", {})
    
    # ── Predict workspace event count ──
    current_changes = ws.get("total_changes", 0)
    cats = ws.get("categories", {})
    memory_changes = cats.get("memory", 0)
    script_changes = cats.get("scripts", 0)
    
    prior = EXTERNAL_PRIORS["workspace_event_count"]
    predicted_events = prior["base"]
    predicted_events += memory_changes * prior["memory_bump"]
    predicted_events += script_changes * prior["script_bump"]
    # Attenuate: high competition → less external attention
    predicted_events *= max(0.3, 1.0 - competition * prior["competition_factor"])
    # Boost: stagnation → system may be missing changes
    predicted_events *= 1.0 + stagnation * 0.05
    
    # ── Predict resource warning ──
    rp = EXTERNAL_PRIORS["resource_warning"]
    warning_prob = rp["base_prob"]
    mem = health.get("memory", {})
    if isinstance(mem, dict) and mem.get("used_pct", 0) > 85:
        warning_prob += rp["mem_above_85"]
    disk = health.get("disk", {})
    for _drive, info in disk.items():
        if info.get("free_gb", 999) < 10:
            warning_prob += rp["disk_below_10gb"]
    gpu = health.get("gpu", {})
    if isinstance(gpu, dict) and gpu.get("temp_c", 0) > 80:
        warning_prob += rp["gpu_above_80c"]
    
    return {
        "model": "ego_jepa_v1_external",
        "workspace_events_predicted": round(predicted_events, 2),
        "workspace_events_actual": current_changes,  # will be compared next tick
        "resource_warning_prob": round(_clamp(warning_prob), 3),
        "prediction_targets": ["workspace_events", "resource_warning"],
        "verifiable": True,  # these predictions can be checked against reality
    }
