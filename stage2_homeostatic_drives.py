#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 2: Homeostatic Drives.

Small persistent drive model for TL-Ego. It converts resting dynamics and
Stage 1 surprise into bounded internal pressures that other stages can inspect.
"""

import json
import time
from pathlib import Path


DEFAULT_DRIVES = {
    "exploration": 0.0,
    "integration": 0.0,
    "novelty": 0.0,
    "stability": 0.0,
}


DRIVE_INTENT_MAP = {
    "exploration": "seek fresh observations",
    "integration": "resolve competing attractors",
    "novelty": "test surprising world-model mismatch",
    "stability": "restore a stable attractor basin",
}


DRIVE_DESIRE_MAP = {
    "exploration": "increase type diversity without raising stagnation",
    "integration": "reduce competition between active clusters",
    "novelty": "lower prediction error by sampling what changed",
    "stability": "raise concentration into the target range",
}


def _clamp(value):
    return max(0.0, min(1.0, float(value)))


def _state_path(workspace):
    return Path(workspace) / ".lae" / "stage2_drives.json"


def load_drives(workspace):
    path = _state_path(workspace)
    if not path.exists():
        return {"drives": dict(DEFAULT_DRIVES), "history": [], "last_updated": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    drives = dict(DEFAULT_DRIVES)
    drives.update(data.get("drives", {}))
    return {
        "drives": {k: _clamp(v) for k, v in drives.items()},
        "history": data.get("history", [])[-100:],
        "last_updated": data.get("last_updated"),
    }


def save_drives(workspace, state):
    path = _state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_drives(workspace, resting_entry, outcome_result=None):
    state = load_drives(workspace)
    drives = state["drives"]

    concentration = float(resting_entry.get("concentration", 0.0) or 0.0)
    competition = float(resting_entry.get("competition", 0.0) or 0.0)
    stagnation = int(resting_entry.get("stagnation_streak", 0) or 0)
    diversity = int(resting_entry.get("diversity_n", 0) or 0)
    surprise = 0.0
    if outcome_result:
        surprise = float(outcome_result.get("prediction_error", 0.0) or 0.0)

    # Passive decay keeps drives responsive instead of permanently saturated.
    for key in drives:
        drives[key] *= 0.94

    if concentration > 0.65:
        drives["exploration"] += 0.04 + 0.02 * min(1.0, stagnation / 6.0)
    if competition > 0.75:
        drives["integration"] += 0.035
    if diversity >= 8 or surprise > 0.4:
        drives["novelty"] += 0.03 + 0.08 * surprise
    if concentration < 0.45 or stagnation >= 3:
        drives["stability"] += 0.04 + 0.02 * min(1.0, stagnation / 6.0)

    # Exploration homeostasis: soft floor prevents drive death spiral.
    # When exploration drops below 0.15, self-boost regardless of concentration
    # — otherwise the system can't recover from attractor lock because the
    # boost condition (concentration > 0.65) is the very symptom of low exploration.
    if drives["exploration"] < 0.15:
        deficit = 0.15 - drives["exploration"]
        drives["exploration"] += 0.02 + 0.06 * deficit

    for key in drives:
        drives[key] = round(_clamp(drives[key]), 4)

    dominant_drive = max(drives, key=drives.get)
    snapshot = {
        "tick": resting_entry.get("tick"),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dominant_drive": dominant_drive,
        "drives": dict(drives),
        "inputs": {
            "dominant": resting_entry.get("dominant"),
            "concentration": round(concentration, 4),
            "competition": round(competition, 4),
            "stagnation_streak": stagnation,
            "diversity_n": diversity,
            "prediction_error": round(surprise, 4),
        },
    }
    state["history"] = (state.get("history", []) + [snapshot])[-100:]
    state["last_updated"] = snapshot["ts"]
    save_drives(workspace, state)
    return snapshot


def build_bdi_snapshot(workspace, ego_state=None):
    """Build a deterministic BDI-style snapshot from Stage 2 drives.

    This is an observability bridge, not a symbolic planner: it maps the
    current homeostatic pressure surface into belief/desire/intention fields
    so Titan Stage 2 can be inspected without asking an LLM to narrate it.
    """
    ego_state = ego_state or {}
    state = load_drives(workspace)
    drives = state.get("drives", {}) or {}
    dominant_drive = max(drives, key=drives.get) if drives else "stability"
    dominant_value = _clamp(drives.get(dominant_drive, 0.0))
    sorted_pressures = sorted(
        ((name, _clamp(value)) for name, value in drives.items()),
        key=lambda item: item[1],
        reverse=True,
    )

    trajectory = ego_state.get("trajectory") or []
    last_point = trajectory[-1] if trajectory else {}
    dominant = ego_state.get("dominant") or ego_state.get("last_dominant") or last_point.get("dominant") or "unknown"
    concentration = float(ego_state.get("concentration", last_point.get("concentration", 0.0)) or 0.0)
    competition = float(ego_state.get("competition", last_point.get("competition", 0.0)) or 0.0)
    stagnation = int(ego_state.get("stagnation_streak", 0) or 0)
    prediction_error = ego_state.get("prediction_error")

    beliefs = {
        "dominant_type": dominant,
        "concentration": round(concentration, 4),
        "competition": round(competition, 4),
        "stagnation_streak": stagnation,
        "prediction_error": prediction_error,
        "dominant_pressure": dominant_drive,
        "dominant_pressure_value": round(dominant_value, 4),
    }

    desire = DRIVE_DESIRE_MAP.get(dominant_drive, "return to a measurable stable state")
    intention = DRIVE_INTENT_MAP.get(dominant_drive, "observe and stabilize")
    confidence = round(min(1.0, 0.45 + dominant_value * 0.5), 4)

    if stagnation >= 3 and dominant_drive != "stability":
        desire = f"{desire}; monitor rising stagnation"
    if competition >= 0.75 and dominant_drive != "integration":
        desire = f"{desire}; keep competition visible"

    return {
        "status": "ok",
        "model": "titan_stage2_bdi_snapshot_v1",
        "updated_at": state.get("last_updated"),
        "beliefs": beliefs,
        "desire": desire,
        "intention": intention,
        "confidence": confidence,
        "pressures": {name: value for name, value in sorted_pressures},
        "pressure_rank": [name for name, _ in sorted_pressures],
        "source": "stage2_homeostatic_drives",
    }


def build_l5_priority_ranking(workspace, trigger_state, domain_hint):
    """Build Stage 2 ranking metadata for L5 goal priority.

    This keeps BDI pressure influence deterministic and auditable: Stage 2 can
    nudge low-risk goal ordering, but it does not create new permissions or
    bypass the existing L5 trigger gates.
    """
    bdi_snapshot = build_bdi_snapshot(workspace, trigger_state)
    pressure_rank = bdi_snapshot.get("pressure_rank", []) or []
    pressures = bdi_snapshot.get("pressures", {}) or {}
    dominant_pressure = pressure_rank[0] if pressure_rank else None
    dominant_pressure_value = _clamp(pressures.get(dominant_pressure, 0.0)) if dominant_pressure else 0.0
    goal_pressure_match = dominant_pressure == domain_hint
    return {
        "model": "titan_stage2_bdi_l5_priority_v1",
        "dominant_pressure": dominant_pressure,
        "dominant_pressure_value": round(dominant_pressure_value, 4),
        "intention": bdi_snapshot.get("intention"),
        "pressure_rank": pressure_rank,
        "goal_pressure_match": goal_pressure_match,
        "priority_adjustment": round((0.08 if goal_pressure_match else 0.0) + dominant_pressure_value * 0.08, 4),
    }
