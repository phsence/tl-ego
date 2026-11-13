"""stage2_homeostatic_drives.py — REBUILT 2026-09-30 (original source lost in the 09-30 wipe).

Provenance (every number/name below is anchored to surviving artifacts):
  * drive vocabulary + magnitudes = the last readable stage2 status,
    `.lae/_l6p_smoke_status.json` -> drives {exploration .1574, integration .5826,
    novelty .1557, stability .7065} (snapshot 2026-09-15 18:22:16). Same names appear as
    stage3_timeline.jsonl's "dominant_drive".
  * state file `.lae/stage2_drives.json` {drives, history[<=100], last_updated} — the
    surviving copy was zero-filled by the same incident, so load_drives() reseeds from
    those last-known magnitudes instead of pretending to recover the history.
  * call sites: update_drives(WORKSPACE, entry, outcome_result=) -> drive_snapshot
                load_drives(WORKSPACE); build_bdi_snapshot(WORKSPACE, ego_state)
                build_l5_priority_ranking(WORKSPACE, trigger_state, domain_hint)
                snapshot["priority_adjustment"] is added to the L5 goal priority (±0.15 cap).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

SCHEMA = "stage2_drives.v1"
DRIVES = ("exploration", "integration", "novelty", "stability")
SEED = {"exploration": 0.1574, "integration": 0.5826, "novelty": 0.1557, "stability": 0.7065}
HISTORY_MAX = 100
DECAY = 0.01          # pull every drive 1%/tick toward 0.5 (the "homeostatic" part)


def _path(workspace):
    return Path(workspace) / ".lae" / "stage2_drives.json"


def _default_state():
    return {"schema": SCHEMA, "drives": dict(SEED), "history": [],
            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")}


def _clamp(v):
    return max(0.0, min(1.0, float(v)))


def _load(path):
    """Never raise: the 09-30 incident left this file zero-filled."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        drives = data.get("drives")
        if not isinstance(drives, dict) or not drives:
            raise ValueError("no drives")
        out = _default_state()
        out["drives"].update({k: _clamp(v) for k, v in drives.items() if isinstance(v, (int, float))})
        hist = data.get("history")
        out["history"] = hist[-HISTORY_MAX:] if isinstance(hist, list) else []
        out["last_updated"] = data.get("last_updated") or out["last_updated"]
        return out
    except Exception:
        return _default_state()


def _save(path, state):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


# Anchored 2026-09-30: the earliest surviving .lae/stage2_drives.json history snapshot
# (ts 2026-09-30 14:46:27) -> the four drive keys and their baseline values. The server
# (24484-24501) uses this dict for key membership on POST /drive.
DEFAULT_DRIVES = {"exploration": 0.1608, "integration": 0.5818, "novelty": 0.1732, "stability": 0.6811}


def save_drives(workspace, state):
    """Persist a drive state dict (server 24510). Same store as load_drives()."""
    _save(_path(workspace), state)
    return state


def load_drives(workspace):
    return _load(_path(workspace))


def _dominant(drives):
    return max(drives, key=drives.get) if drives else "unknown"


def update_drives(workspace, entry=None, outcome_result=None, tuning=None):
    """One homeostatic step from this tick's outcome; returns the tick's drive snapshot.

    2026-10-30: tuning={"decay":..., "gain":...} lets the runtime override the
    hardcoded dynamics (server passes RESTING_STATE["drive_decay"]/["drive_gain"]
    through the /apply-parameter channel). Default None = byte-identical behavior.
    gain scales every outcome/pe/concentration/stagnation delta; decay scales
    the homeostasis pull toward 0.5.
    """
    t = tuning if isinstance(tuning, dict) else {}

    def _num(x, default):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return default
        return default if (v != v or abs(v) == float("inf")) else v

    decay = max(0.0, min(0.5, _num(t.get("decay"), DECAY)))
    gain = max(0.0, min(5.0, _num(t.get("gain"), 1.0)))
    entry = entry if isinstance(entry, dict) else {}
    outcome = outcome_result if isinstance(outcome_result, dict) else {}
    path = _path(workspace)
    state = _load(path)
    d = state["drives"]

    ok = str(outcome.get("outcome", "") or "").lower()
    pe = outcome.get("prediction_error")
    pe = float(pe) if isinstance(pe, (int, float)) else 0.0
    conc = float(entry.get("concentration", 0.0) or 0.0)
    stag = int(entry.get("stagnation_streak", 0) or 0)

    for k in DRIVES:
        d[k] = _clamp(d.get(k, 0.5) + decay * (0.5 - d.get(k, 0.5)))     # homeostasis
    if ok == "success":
        d["stability"] = _clamp(d["stability"] + gain * 0.010)
        d["integration"] = _clamp(d["integration"] + gain * 0.005)
    elif ok in ("failure", "failed", "partial"):
        d["stability"] = _clamp(d["stability"] - gain * 0.020)
        d["exploration"] = _clamp(d["exploration"] + gain * 0.015)
        d["novelty"] = _clamp(d["novelty"] + gain * 0.010)
    d["stability"] = _clamp(d["stability"] - gain * 0.05 * pe)                  # surprise hurts stability
    d["novelty"] = _clamp(d["novelty"] + gain * 0.03 * pe)
    d["integration"] = _clamp(d["integration"] + gain * 0.02 * pe)              # surprise demands integration (l5g-20261109-069079)
    d["exploration"] = _clamp(d["exploration"] + gain * 0.02 * pe)              # surprise opens exploration (intrinsic motivation)
    if conc and conc < 0.10:                                             # scattered attention -> explore
        d["exploration"] = _clamp(d["exploration"] + gain * 0.010)
    if stag >= 7:                                                       # stuck -> novelty demand
        d["novelty"] = _clamp(d["novelty"] + gain * 0.010)

    tick = int(entry.get("tick", 0) or 0)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    snap = {"schema": SCHEMA + ".snapshot", "ts": ts, "tick": tick,
            "drives": {k: round(v, 4) for k, v in d.items()},
            "dominant_drive": _dominant(d), "outcome": ok or "unknown", "prediction_error": round(pe, 4)}
    state["history"].append(snap)
    state["history"] = state["history"][-HISTORY_MAX:]
    state["last_updated"] = ts
    _save(path, state)
    return snap


def build_bdi_snapshot(workspace, ego_state=None):
    """Beliefs from the live ego_state, desires from drive levels, intentions from the ranking."""
    ego_state = ego_state if isinstance(ego_state, dict) else {}
    state = _load(_path(workspace))
    d = state["drives"]
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    beliefs = []
    for key, label in (("concentration", "attention is concentrated"),
                       ("competition", "types are competing"),
                       ("stagnation_streak", "trajectory is stalled"),
                       ("prediction_error", "the world surprised me")):
        val = ego_state.get(key)
        if val is not None:
            beliefs.append({"key": key, "value": val, "label": label})
    order = sorted(d, key=d.get, reverse=True)
    desires = [{"drive": k, "level": round(d[k], 4)} for k in order[:2]]
    intentions = [{"intent": f"pursue {k}", "weight": round(d[k], 4),
                   "because": "highest non-stability drive"} for k in order if k != "stability"][:2]
    if d.get("stability", 0.0) < 0.35:
        intentions.insert(0, {"intent": "restore stability", "weight": round(1.0 - d["stability"], 4),
                              "because": "stability below floor"})
    return {"schema": "bdi_snapshot.v1", "ts": ts, "beliefs": beliefs,
            "desires": desires, "intentions": intentions,
            "drive_levels": {k: round(v, 4) for k, v in d.items()}, "dominant_drive": _dominant(d)}


def build_l5_priority_ranking(workspace, trigger_state=None, domain_hint=None):
    """Small bounded nudge for the L5 goal priority (±0.15) derived from drive pressure."""
    trigger_state = trigger_state if isinstance(trigger_state, dict) else {}
    state = _load(_path(workspace))
    d = state["drives"]
    pe = float(trigger_state.get("prediction_error", 0.0) or 0.0)
    adj = 0.05 * (d.get("stability", 0.5) - 0.5) - 0.05 * min(pe, 1.0)
    if domain_hint and str(domain_hint).lower() in d:
        adj += 0.05 * (d[str(domain_hint).lower()] - 0.5)
    adj = max(-0.15, min(0.15, adj))
    ranking = [{"drive": k, "level": round(v, 4)} for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)]
    return {"schema": "l5_priority_ranking.v1", "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "priority_adjustment": round(adj, 4), "ranking": ranking,
            "domain_hint": domain_hint, "dominant_drive": _dominant(d)}


if __name__ == "__main__":  # self-check: seeds, homeostasis, clamp, priority cap
    import tempfile
    ws = Path(tempfile.mkdtemp())
    st0 = load_drives(ws)
    assert set(st0["drives"]) == set(DRIVES) and abs(st0["drives"]["stability"] - 0.7065) < 1e-9
    s1 = update_drives(ws, {"tick": 1, "concentration": 0.05, "stagnation_streak": 0}, {"outcome": "success"})
    assert s1["drives"]["stability"] > 0.7065 and s1["dominant_drive"] == "stability"
    s2 = update_drives(ws, {"tick": 2}, {"outcome": "failure", "prediction_error": 1.0})
    assert s2["drives"]["stability"] < s1["drives"]["stability"] and s2["drives"]["novelty"] > s1["drives"]["novelty"]
    assert len(load_drives(ws)["history"]) == 2
    r = build_l5_priority_ranking(ws, {"prediction_error": 1.0}, "exploration")
    assert -0.15 <= r["priority_adjustment"] <= 0.15
    bdi = build_bdi_snapshot(ws, {"concentration": 0.2})
    assert bdi["desires"] and bdi["beliefs"]
    # 2026-10-30 self-modification contract: gain=0+decay=0 freezes drives regardless of outcome.
    ws2 = Path(tempfile.mkdtemp())
    load_drives(ws2)
    f1 = update_drives(ws2, {"tick": 1, "concentration": 0.05}, {"outcome": "failure", "prediction_error": 1.0},
                       tuning={"decay": 0.0, "gain": 0.0})["drives"]
    f2 = update_drives(ws2, {"tick": 2, "concentration": 0.05}, {"outcome": "success"},
                       tuning={"decay": 0.0, "gain": 0.0})["drives"]
    assert f1 == f2 == {k: round(v, 4) for k, v in SEED.items()}, (f1, f2)
    (ws / ".lae" / "stage2_drives.json").write_text("\x00" * 100, encoding="utf-8")   # incident shape
    assert set(load_drives(ws)["drives"]) == set(DRIVES)
    print("stage2_homeostatic_drives self-check OK", s1["drives"], s2["drives"])
