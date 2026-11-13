"""evolution_trigger_policy.py — REBUILT 2026-09-30 (original source lost in the 09-30 wipe).

Provenance (call sites in tl_ego_server.py + the live log, nothing invented):
  13719/13739  should_suppress_evolution_trigger(state, runtime_generation_id=..., tick=..., reason=...)
               called twice: once with reason="pending_signal" (pre-check), once with the real reason.
  18331        record_evolution_trigger(state, runtime_generation_id=..., tick=..., reason=...)
  observed cadence in logs/tlego-stdout.log:  "[evo] trigger=dead_types_excess tick=28125/28175/
               28225/28275/28325"  -> one fired trigger every 50 ticks, same reason, same generation.
               A new generation must re-arm immediately (that is what the generation id is carried for).

record_evolution_trigger() also appends one audit line to .lae/evolution_triggers.jsonl so the
policy's decisions stay auditable after the rebuild.
"""

from __future__ import annotations

import json
import os
import time

COOLDOWN_TICKS = 50          # observed: same reason fires at most once per 50 ticks
DIFFERENT_REASON_TICKS = 10  # a different signal may re-arm sooner

_LAE = os.path.join(os.environ.get("OPENCLAW_WORKSPACE", r"C:\Users\Administrator\.openclaw\workspace"), ".lae")
_AUDIT = os.path.join(_LAE, "evolution_triggers.jsonl")


def _fresh_generation(state, runtime_generation_id):
    old = state.get("runtime_generation_id")
    return bool(old) and bool(runtime_generation_id) and old != runtime_generation_id


def should_suppress_evolution_trigger(state, runtime_generation_id=None, tick=0, reason="pending_signal"):
    """True -> do NOT trigger this tick. Purely a function of the caller's own state dict."""
    try:
        if not isinstance(state, dict) or not state:
            return False                                  # no history -> never suppress
        if _fresh_generation(state, runtime_generation_id):
            return False                                  # new generation re-arms
        if reason == "pending_signal":
            return False                                  # pre-check probe, real reason comes next
        last_tick = state.get("last_trigger_tick")
        if last_tick is None:
            return False
        window = COOLDOWN_TICKS if state.get("last_reason") == reason else DIFFERENT_REASON_TICKS
        return (int(tick) - int(last_tick)) < window
    except Exception:
        return False                                      # fail open: a broken policy must not wedge evolution


def record_evolution_trigger(state, runtime_generation_id=None, tick=0, reason=""):
    """Stamp the caller's state dict and append one audit line. Returns the stamped dict."""
    if not isinstance(state, dict):
        return state
    tick = int(tick or 0)
    state["last_trigger_tick"] = tick
    state["last_reason"] = reason
    state["runtime_generation_id"] = runtime_generation_id
    state["count"] = int(state.get("count", 0) or 0) + 1
    try:
        os.makedirs(_LAE, exist_ok=True)
        with open(_AUDIT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "tick": tick,
                                 "reason": reason, "generation": runtime_generation_id,
                                 "count": state["count"], "source": "evolution_trigger_policy.rebuilt"},
                                ensure_ascii=False) + "\n")
    except Exception:
        pass
    return state


if __name__ == "__main__":  # self-check: cooldown window + generation re-arm
    st = {}
    assert should_suppress_evolution_trigger(st, runtime_generation_id="g1", tick=100, reason="pending_signal") is False
    record_evolution_trigger(st, runtime_generation_id="g1", tick=100, reason="dead_types_excess")
    assert should_suppress_evolution_trigger(st, runtime_generation_id="g1", tick=120, reason="dead_types_excess") is True
    assert should_suppress_evolution_trigger(st, runtime_generation_id="g1", tick=151, reason="dead_types_excess") is False
    assert should_suppress_evolution_trigger(st, runtime_generation_id="g2", tick=101, reason="dead_types_excess") is False
    assert should_suppress_evolution_trigger(st, runtime_generation_id="g1", tick=105, reason="stagnation_persist") is True
    print("evolution_trigger_policy self-check OK")
