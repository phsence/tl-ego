"""proprioceptive_loop.py - REBUILT 2026-09-30 (source lost in the 09-30 wipe).

Provenance: server 18269-18281 calls proprio_tick(ns, RESTING_STATE, previous_dominant=,
actual_dominant=, raw_attractor_dominant=, current_tick=); status at 23461 via
get_proprio_status(); avg_error read at 15141 RESTING_STATE["proprioceptive_loop"] and
17936 RESTING_STATE["_prop"]; evidence injection per comment 18271 ("inject
proprioceptive signal into RESTING_STATE['external_evidence']"); real ledger
.lae/causal_events.jsonl tag proprio_spike 1684 rows pre/post={"predicted","actual","error"}.
Judgement (not measured): per-type-pair error magnitude unrecoverable -> error = 0.0 if
equal else 1.0; avg = EMA(ALPHA); spike threshold 0.75 (observed spike band 0.76-0.85).
"""
import time
from collections import deque

ALPHA = 0.15
SPIKE_THRESHOLD = 0.75
_S = {"avg": 0.0, "ticks": 0, "spikes": 0, "last": {}, "hist": deque(maxlen=120)}


def _inject(resting_state, status):
    ev = resting_state.get("external_evidence")
    if not isinstance(ev, dict):
        ev = {}
        resting_state["external_evidence"] = ev
    ev["proprioceptive_feedback"] = {
        "domain": "proprioception",
        "avg_error": status["avg_error"],
        "predicted": status["predicted"],
        "actual": status["actual"],
        "tick": status["tick"],
        "ts": status["ts"],
    }


def proprio_tick(ns, resting_state, previous_dominant="unknown", actual_dominant="unknown",
                 raw_attractor_dominant=None, current_tick=0):
    """One self-referential feedback step. Never raises (caller wraps, but stay quiet)."""
    pred = str(previous_dominant or "unknown")
    act = str(actual_dominant or "unknown")
    err = 0.0 if pred == act else 1.0
    _S["avg"] = round((1 - ALPHA) * _S["avg"] + ALPHA * err, 4)
    _S["ticks"] += 1
    prev_avg = _S["last"].get("avg_error")
    status = {
        "available": True,
        "avg_error": _S["avg"] if _S["ticks"] > 1 else round(err, 4),
        "last_error": err,
        "predicted": pred,
        "actual": act,
        "raw_attractor_dominant": str(raw_attractor_dominant or "unknown"),
        "drift": pred != act,
        "spikes": _S["spikes"],
        "ticks": _S["ticks"],
        "tick": int(current_tick or 0),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _S["hist"].append((status["tick"], err))
    if err >= SPIKE_THRESHOLD:
        _S["spikes"] += 1
        status["spikes"] = _S["spikes"]
        writer = getattr(ns, "_write_causal_event", None)
        if writer is not None and prev_avg is not None and prev_avg != status["avg_error"]:
            try:
                writer("proprio_spike",
                       {"predicted": _S["last"].get("predicted"), "actual": _S["last"].get("actual"),
                        "error": _S["last"].get("last_error")},
                       {"predicted": pred, "actual": act, "error": err})
            except Exception:
                pass
    _S["last"] = status
    resting_state["proprioceptive_loop"] = status
    resting_state["_prop"] = status
    _inject(resting_state, status)
    return status


def get_proprio_status():
    if not _S["last"]:
        return {"available": True, "avg_error": 0.0, "ticks": 0, "note": "no tick yet"}
    return dict(_S["last"])


if __name__ == "__main__":
    class NS:
        events = []
        def _write_causal_event(self, tag, pre, post):
            self.events.append((tag, pre, post))
    ns, st = NS(), {"external_evidence": {}}
    assert get_proprio_status()["ticks"] == 0
    s1 = proprio_tick(ns, st, previous_dominant="memory", actual_dominant="memory", current_tick=1)
    assert s1["last_error"] == 0.0 and s1["drift"] is False
    s2 = proprio_tick(ns, st, previous_dominant="memory", actual_dominant="integrator", current_tick=2)
    assert s2["last_error"] == 1.0 and s2["spikes"] == 1, s2
    assert ns.events and ns.events[0][0] == "proprio_spike", ns.events
    assert st["proprioceptive_loop"] is st["_prop"] and st["proprioceptive_loop"]["avg_error"] > 0
    assert st["external_evidence"]["proprioceptive_feedback"]["domain"] == "proprioception"
    assert "events" not in st
    st2 = {}
    proprio_tick(ns, st2, previous_dominant=None, actual_dominant=None, current_tick=None)
    assert st2["_prop"]["avg_error"] >= 0.0
    print("proprioceptive_loop selfcheck OK", {k: s2[k] for k in ("avg_error", "last_error", "spikes")})
