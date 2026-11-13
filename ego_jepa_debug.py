"""
ego_jepa_debug.py — real external-world predictor, restored 2026-10-15.

Why this file exists
--------------------
ego_jepa.py:predict_external() has been a shim since the 09-30 wipe:

    try:
        from ego_jepa_debug import predict_external as _real
        return _real(ego_state, observation)
    except Exception:
        return {"status": "stub", "reason": "ego_jepa_debug not available", ...}

ego_jepa_debug.py did not exist anywhere in the repo, so the `except` branch
ran on every call and /status reported jepa_external = stub with
predicted=0.0 / actual=0 / error=0.0. The downstream Stage-5.5 calibration
EMA in tl_ego_server._jepa_external_predict() was fully wired and has been
waiting on real numbers ever since.

Provenance: implementation restored verbatim from this repo's own backup
    ego_jepa.py.l5bak.l5g-20260806-000031.20260806-220105
(lines 570-651: EXTERNAL_PRIORS + predict_external). No new behavior invented.

Signature note: the backup names the 2nd arg `external_world`; the shim passes
it positionally as `observation`. Same object (_build_external_world_snapshot()).

Deliberately NOT changed: the prior constants. They are the calibration baseline
the live EMA is fitted to; re-deriving them here would reset a converged factor.
"""

from typing import Any, Dict


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


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

    ws = external_world.get("workspace", {}) or {}
    health = external_world.get("system_health", {}) or {}

    # ── Predict workspace event count ──
    current_changes = ws.get("total_changes", 0)
    cats = ws.get("categories", {}) or {}
    memory_changes = cats.get("memory", 0)
    script_changes = cats.get("scripts", 0)

    prior = EXTERNAL_PRIORS["workspace_event_count"]
    predicted_events = prior["base"]
    predicted_events += memory_changes * prior["memory_bump"]
    predicted_events += script_changes * prior["script_bump"]
    # Attenuate: high competition → less external attention
    predicted_events *= max(0.3, 1.0 - competition * prior["competition_factor"])
    # l5g-20261112-071381: stagnation boost REMOVED - measured wrong-signed.
    # 50-tick window 71358-71408: corr(raw_predicted, actual) = -0.47; actual
    # averaged 7.75 when raw ran high (stag~12 -> raw 11.1-11.9) vs 8.88 when
    # raw ran low (stag~0 -> raw 7.4-8.0). The +5%/tick boost over-predicted
    # up to x1.6 exactly when real workspace activity FELL, forcing the scalar
    # calibration factor to oscillate 0.607..1.142 instead of converging.
    # The EMA calibrator exists to absorb a residual CONSTANT bias, not a
    # state-dependent one.

    # ── Predict resource warning ──
    rp = EXTERNAL_PRIORS["resource_warning"]
    warning_prob = rp["base_prob"]
    mem = health.get("memory", {}) or {}
    if isinstance(mem, dict) and mem.get("used_pct", 0) > 85:
        warning_prob += rp["mem_above_85"]
    disk = health.get("disk", {}) or {}
    if isinstance(disk, dict):
        # No break: the backup sums every low drive, then clamps at the end.
        for _drive, info in disk.items():
            if isinstance(info, dict) and info.get("free_gb", 999) < 10:
                warning_prob += rp["disk_below_10gb"]
    gpu = health.get("gpu", {}) or {}
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


if __name__ == "__main__":
    # self-check: must NOT look like the stub, and must respond to its inputs
    out = predict_external(
        {"competition": 0.5, "stagnation_streak": 0},
        {"workspace": {"total_changes": 7, "categories": {"memory": 2, "scripts": 1}},
         "system_health": {"memory": {"used_pct": 40}, "disk": {"C:": {"free_gb": 200}},
                           "gpu": {"temp_c": 50}}},
    )
    assert out["model"] == "ego_jepa_v1_external", out
    assert out["workspace_events_actual"] == 7, out
    assert out["workspace_events_predicted"] > 0.0, out
    assert out["verifiable"] is True

    # competition must damp the prediction (the one behavioral lever that matters)
    hot = predict_external({"competition": 1.0, "stagnation_streak": 0},
                           {"workspace": {"total_changes": 0, "categories": {}},
                            "system_health": {}})["workspace_events_predicted"]
    cold = predict_external({"competition": 0.0, "stagnation_streak": 0},
                            {"workspace": {"total_changes": 0, "categories": {}},
                             "system_health": {}})["workspace_events_predicted"]
    assert hot < cold, (hot, cold)

    # resource warning must add up its priors and clamp at 1.0
    # .05 base + .35 mem95 + .25 disk5 + .15 gpu90 = 0.80
    warn = predict_external({"competition": 0.5, "stagnation_streak": 0},
                            {"workspace": {"total_changes": 0, "categories": {}},
                             "system_health": {"memory": {"used_pct": 95},
                                               "disk": {"C:": {"free_gb": 5}},
                                               "gpu": {"temp_c": 90}}})
    assert warn["resource_warning_prob"] == 0.8, warn
    # two low drives must exceed 1.0 and clamp, not wrap
    over = predict_external({"competition": 0.5, "stagnation_streak": 0},
                            {"workspace": {"total_changes": 0, "categories": {}},
                             "system_health": {"memory": {"used_pct": 95},
                                               "disk": {"C:": {"free_gb": 5},
                                                        "D:": {"free_gb": 5}},
                                               "gpu": {"temp_c": 90}}})
    assert over["resource_warning_prob"] == 1.0, over

    # empty snapshot must not raise (server passes partial dicts)
    assert predict_external({}, {})["workspace_events_actual"] == 0
    print("ego_jepa_debug self-check OK", out)
