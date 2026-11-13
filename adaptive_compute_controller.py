"""adaptive_compute_controller.py — REBUILT 2026-09-30 (original source lost in the 09-30 wipe).

Provenance (anchors, not guesses):
  * choose_activation_capacity(state, current_top_k) — the only surviving observation is the
    live log line the original emitted: "[rkwta-adaptive] conc=0.041..0.069 comp=0.888..0.952
    stag=0 top_k=1000->750"  (6/6 samples identical) -> very low concentration cuts capacity
    by 25%. Other buckets are the rebuild's judgement and are marked as such.
  * compute_budget / should_continue / ponder_cost / summarize_act — consumed by the ACT loop
    in tl_ego_server.py:13626-13652: budget["max_steps"], budget.get("ponder_lambda", 0.02),
    should_continue(prev, cur, step, max_steps, lam) -> (continue, stop_reason),
    summarize_act(steps, stop_reason, total_cost, budget).
"""

from __future__ import annotations

SCHEMA = "adaptive_compute.v1"
PONDER_LAMBDA = 0.02      # server's own fallback default when the key is absent
MAX_STEPS_CAP = 12
CAP_TOTAL_COST = 0.25     # stop before the ACT loop spends more than this on thinking


def choose_activation_capacity(state, current_top_k):
    """Return the top_k to use for this kwta round. Anchored bucket: (conc<0.10, comp>0.85) -> x0.75."""
    state = state if isinstance(state, dict) else {}
    try:
        conc = float(state.get("concentration", 0.5) or 0.0)
        comp = float(state.get("competition", 0.5) or 0.0)
        stag = int(state.get("stagnation_streak", 0) or 0)
        top_k = int(current_top_k)
    except Exception:
        return int(current_top_k or 0)
    if top_k <= 0:
        return top_k
    if stag >= 20:                                  # deep stall: server boosts separately, do not cut
        return min(2000, int(top_k * 1.25))
    if conc < 0.10 and comp > 0.85:                 # observed anchor
        return max(64, int(top_k * 0.75))
    if conc < 0.25:                                 # judgement
        return max(64, int(top_k * 0.90))
    if conc > 0.60:                                 # judgement: locked-in attention, widen a little
        return min(2000, int(top_k * 1.05))
    return top_k


def compute_budget(state=None, context="resting"):
    state = state if isinstance(state, dict) else {}
    base = {"resting": 3, "rest": 2, "contemplate": 6, "think": 6}.get(str(context), 3)
    stag = int(state.get("stagnation_streak", 0) or 0)
    steps = base + (2 if stag >= 7 else 0)
    return {"schema": SCHEMA + ".budget", "context": str(context),
            "max_steps": max(1, min(MAX_STEPS_CAP, steps)),
            "ponder_lambda": PONDER_LAMBDA, "stagnation_streak": stag}


def ponder_cost(step, lam=PONDER_LAMBDA):
    return float(lam) * max(1, int(step))


def should_continue(prev_state, cur_state, step, max_steps, lam=PONDER_LAMBDA):
    """(keep_going, stop_reason) — cheap checks first, never raises."""
    prev = prev_state if isinstance(prev_state, dict) else {}
    cur = cur_state if isinstance(cur_state, dict) else {}
    step, max_steps = int(step or 0), int(max_steps or 0)
    if step >= max_steps:
        return False, "max_steps_reached"
    if float(cur.get("stagnation_streak", 0) or 0) < float(prev.get("stagnation_streak", 0) or 0):
        return True, "stagnation_improving"
    if float(cur.get("concentration", 0.0) or 0.0) > float(prev.get("concentration", 0.0) or 0.0):
        return True, "attention_sharpening"
    if ponder_cost(step + 1, lam) > CAP_TOTAL_COST:
        return False, "ponder_cost_exceeded"
    return False, "no_progress"


def summarize_act(steps, stop_reason, total_cost, budget=None):
    budget = budget if isinstance(budget, dict) else {}
    return {"schema": SCHEMA + ".act", "steps": int(steps or 0),
            "stop_reason": str(stop_reason or "unknown"),
            "ponder_cost": round(float(total_cost or 0.0), 6),
            "lambda": budget.get("ponder_lambda", PONDER_LAMBDA),
            "max_steps": budget.get("max_steps")}


if __name__ == "__main__":  # self-check: the one observed anchor + cost/loop rules
    assert choose_activation_capacity({"concentration": 0.041, "competition": 0.936, "stagnation_streak": 0}, 1000) == 750
    assert choose_activation_capacity({"concentration": 0.069, "competition": 0.888, "stagnation_streak": 0}, 1000) == 750
    assert choose_activation_capacity({"concentration": 0.5, "competition": 0.5, "stagnation_streak": 0}, 1000) == 1000
    assert choose_activation_capacity({"stagnation_streak": 25}, 1000) == 1250
    assert choose_activation_capacity({}, 0) == 0
    b = compute_budget({"stagnation_streak": 12}, context="resting")
    assert b["max_steps"] == 5 and b["ponder_lambda"] == 0.02
    assert abs(ponder_cost(3) - 0.06) < 1e-9
    keep, why = should_continue({"stagnation_streak": 5}, {"stagnation_streak": 3}, 1, 6)
    assert keep and why == "stagnation_improving"
    keep, why = should_continue({"concentration": 0.5}, {"concentration": 0.4}, 6, 6)
    assert not keep and why == "max_steps_reached"
    keep, why = should_continue({"concentration": 0.3}, {"concentration": 0.2}, 2, 6)
    assert not keep and why == "no_progress"
    assert summarize_act(2, "no_progress", 0.06, b)["ponder_cost"] == 0.06
    print("adaptive_compute_controller self-check OK")
