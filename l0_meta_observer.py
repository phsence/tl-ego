"""l0_meta_observer.py — REBUILT 2026-09-30 (original source lost in the 09-30 wipe).

Provenance (call sites; the comments at those sites are the spec):
  * 15027-15032: "L0 MetaObserver: immediate seed injection (Stage 27 Layer 1) —
    bypasses the external_evidence queue (1-tick delay) and directly injects L0
    attention targets into the current tick's seed pool before attractor propagation."
      `l0_inject_seeds(sys.modules[__name__], seeds, tick)`
  * 18299-18302: `l0_tick(sys.modules[__name__], tick)` once per tick.
  * 23333-23335 / 23459: `get_l0_status()` must provide
      broadcast_count (int), current_salience (dict), last_attention (str).
  * 15091-15095: `RESTING_STATE["_identity_drift_l0_override"]` = "broadcast threshold is
    lowered during drift"  -> that flag must actually lower this module's threshold.
  * no `.lae` l0 state file ever existed, so broadcasts are logged to
    `.lae/l0_observer.jsonl` from now on (same pattern as the L10 observer log).

Design (bounded, no invention beyond the spec above): salience = per-type share of the
current tick's seed pool; a "broadcast" fires when one type's share crosses the threshold
(0.35 normally, 0.20 while the drift override is set) and the injection step feeds that
type back into the pool when it is underrepresented.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

SHARE_THRESHOLD = 0.35
DRIFT_SHARE_THRESHOLD = 0.20
INJECT_BELOW_SHARE = 0.08      # same underrepresentation rule as the value-bias layer
MAX_INJECT = 2

_STATE = {
    "broadcast_count": 0,
    "current_salience": {},
    "last_attention": "",
    "attention_type": None,
    "ticks_seen": 0,
    "last_tick": None,
    "history": [],              # bounded recent shares
}


def _lae():
    for var in ("OPENCLAW_WORKSPACE", "EGO_WORKSPACE"):
        root = __import__("os").environ.get(var)
        if root:
            return Path(root) / ".lae"
    return Path(__file__).resolve().parents[2] / ".lae"


def _log(record):
    try:
        path = _lae() / "l0_observer.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _type_share(ns, seeds):
    try:
        parse_type = getattr(ns, "parse_type", None)
        if parse_type is None or not seeds:
            return {}
        counts = {}
        for s in seeds:
            t = parse_type(s)
            counts[t] = counts.get(t, 0) + 1
        total = float(len(seeds)) or 1.0
        return {t: c / total for t, c in counts.items()}
    except Exception:
        return {}


def _drift_override(ns):
    try:
        return bool((getattr(ns, "RESTING_STATE", {}) or {}).get("_identity_drift_l0_override"))
    except Exception:
        return False


def l0_tick(ns, tick=None):
    """Observe this tick's attention: where is the seed pool concentrated?"""
    try:
        resting = getattr(ns, "RESTING_STATE", {}) or {}
        # the server records the tick's seed composition here before attractor propagation
        share = dict((resting.get("_pre_kwta_seed_state") or {}).get("seed_type_share") or {})
        if not share:
            seeds = resting.get("_last_kwta_seeds") or resting.get("last_seeds") or []
            share = _type_share(ns, seeds)
        if not share:
            # no seed pool visible this tick: fall back to the reported dominant type
            dom = resting.get("last_dominant")
            share = {dom: 1.0} if dom else {}
        _STATE["ticks_seen"] += 1
        _STATE["last_tick"] = tick
        if not share:
            return _STATE
        top_type, top_share = max(share.items(), key=lambda kv: kv[1])
        threshold = DRIFT_SHARE_THRESHOLD if _drift_override(ns) else SHARE_THRESHOLD
        salience = {"tick": tick, "dominant_type": top_type, "share": round(top_share, 4),
                    "threshold": threshold, "types": {k: round(v, 4) for k, v in
                                                      sorted(share.items(), key=lambda kv: -kv[1])[:6]}}
        _STATE["current_salience"] = salience
        _STATE["drift_override"] = threshold == DRIFT_SHARE_THRESHOLD
        _STATE["make_attention"] = top_type if top_share >= threshold else None
        if _STATE.get("make_attention"):
            _STATE["attention_type"] = top_type
            _STATE["broadcast_count"] += 1
            _STATE["last_attention"] = f"type={top_type} share={top_share:.3f} tick={tick}"
            _log({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "tick": tick, "broadcast": True,
                  "attention": _STATE["last_attention"], "drift_override": _drift_override(ns)})
        hist = _STATE["history"]
        hist.append({"tick": tick, "type": top_type, "share": round(top_share, 4)})
        if len(hist) > 200:
            del hist[:-200]
        return _STATE
    except Exception:
        return _STATE


def l0_inject_seeds(ns, seeds, tick=None):
    """Feed the current L0 attention target back into this tick's seed pool."""
    try:
        target = _STATE.get("attention_type")
        if not target or not seeds:
            return 0
        parse_type = getattr(ns, "parse_type", None)
        typed = getattr(ns, "typed_neurons", None)
        if parse_type is None or typed is None:
            return 0
        # Use broadcast-time share from _pre_kwta_seed_state instead of current seeds share.
        # Current seeds share is diluted by downstream mechanisms (fairness, value-bias, L7)
        # between broadcast (l0_tick reads _pre_kwta_seed_state) and injection (l0_inject_seeds),
        # causing 86% of broadcasts to skip injection even though they should reinforce the target.
        resting = getattr(ns, "RESTING_STATE", {}) or {}
        snap = (resting.get("_pre_kwta_seed_state") or {}).get("seed_type_share", {})
        share = snap if snap else _type_share(ns, seeds)
        if share.get(target, 0.0) >= INJECT_BELOW_SHARE:
            return 0
        pool = typed(target)
        if not pool:
            return 0
        protected = set(getattr(seeds, "_newborn_positions", set()) or set())
        replaceable = [i for i in range(len(seeds))
                       if i not in protected and parse_type(seeds[i]) != target]
        if not replaceable:
            return 0
        import random
        injected = 0
        for _ in range(min(MAX_INJECT, len(replaceable))):
            idx = random.choice(replaceable)
            seeds[idx] = random.choice(pool)
            seeds._newborn_positions.add(idx)
            replaceable.remove(idx)
            injected += 1
        if injected:
            _log({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "tick": tick, "inject": target,
                  "count": injected, "share_before": round(share.get(target, 0.0), 4)})
        return injected
    except Exception:
        return 0


def get_l0_status():
    return {
        "broadcast_count": _STATE["broadcast_count"],
        "current_salience": dict(_STATE["current_salience"]),
        "last_attention": _STATE["last_attention"],
        "ticks_seen": _STATE["ticks_seen"],
        "last_tick": _STATE["last_tick"],
        "drift_override": _STATE.get("drift_override", False),
    }


if __name__ == "__main__":  # self-check: threshold, drift override, injection bound
    import os
    import tempfile
    os.environ["OPENCLAW_WORKSPACE"] = tempfile.mkdtemp()

    class NS:
        RESTING_STATE = {"_identity_drift_l0_override": False, "last_dominant": "action"}
        @staticmethod
        def parse_type(n):
            return n.split("-")[0]
        @staticmethod
        def typed_neurons(t):
            return [f"{t}-x{i}" for i in range(5)]

    class Seeds(list):
        _newborn_positions = set()

    ns = NS()
    seeds = Seeds(["action-a", "action-b", "action-c", "memory-d", "memory-e", "action-f"])
    ns.RESTING_STATE["_pre_kwta_seed_state"] = {"seed_type_share": {"action": 0.667, "memory": 0.333}}
    l0_tick(ns, 100)
    st = get_l0_status()
    assert st["broadcast_count"] == 1 and st["current_salience"]["dominant_type"] == "action", st
    # scattered pool -> no broadcast
    ns.RESTING_STATE["_pre_kwta_seed_state"] = {"seed_type_share": {"action": 0.30, "memory": 0.25,
                                                                  "integration": 0.25, "doubt": 0.20}}
    l0_tick(ns, 101)
    assert get_l0_status()["broadcast_count"] == 1
    # broadcast-time snapshot says the attention target is underrepresented
    ns.RESTING_STATE["_pre_kwta_seed_state"] = {"seed_type_share": {"memory": 0.95, "action": 0.05}}
    assert l0_inject_seeds(ns, Seeds(["memory-a", "memory-b", "memory-c"])) == 2   # target underrepresented
    assert l0_inject_seeds(ns, Seeds(["action-a", "action-b", "action-c"])) == 0   # already represented
    # drift override lowers the threshold -> 0.5 share broadcasts at 0.35..0.5 too
    ns.RESTING_STATE["_identity_drift_l0_override"] = True
    ns.RESTING_STATE["_pre_kwta_seed_state"] = {"seed_type_share": {"integration": 0.45, "action": 0.55}}
    before = get_l0_status()["broadcast_count"]
    l0_tick(ns, 102)
    assert get_l0_status()["broadcast_count"] == before + 1
    assert (Path(os.environ["OPENCLAW_WORKSPACE"]) / ".lae" / "l0_observer.jsonl").exists()
    print("l0_meta_observer self-check OK", get_l0_status())
