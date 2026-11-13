"""stage4_s2_internalization.py — REBUILT 2026-09-30 (original source lost in the 09-30 wipe).

Provenance:
  * consumer ledger schema = the surviving `.lae/s2_internalization_consumer.json`
    {"schema","consumed"{decision_id: ts},"last_run","last_result"} — the last real run
    recorded last_result {"path":...s2_training_samples.jsonl,"read":7,"applied":0,
    "already_consumed":7,"invalid":0,"reindexed":0,"status":"ok"}; /status merges
    s2_consumer_status() under s2_internalization.consumer together with "consumed": 7.
  * input rows = the surviving `.lae/s2_training_samples.jsonl` (7 rows,
    schema s2_training_sample.v1: decision_id, query, s1{...}, teacher{type,confidence},
    actual_outcome{outcome,judge,reason,ts}).
  * creative-candidate rows = the surviving `.lae/stage4_creative_candidates.jsonl`
    {"timestamp","tick","candidate"{source_neuron,kind,trigger{prediction_error,dissonance,
    stagnation},scores{novelty,usefulness,risk},ts,proposal{from,to,why},...}}.
  * neuron file `.lae/stage4_reasoning_neurons.json` was zero-filled by the same incident;
    load_neurons() therefore treats it as empty instead of guessing, and the s2rn_* neurons
    minted from now on are re-derived only from samples that are not in the ledger yet
    (the 7 already-minted ones stay consumed — no double training).
  * call sites: observe_pattern / generate_creative_candidates (per-tick titan block),
    load_neurons (/reasoning_neurons, persist), apply_feedback (POST /feedback),
    consume_verified_teacher_samples (boot + after each verified teacher sample).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

SCHEMA = "stage4_reasoning_neurons.v1"
CONSUMER_SCHEMA = "s2_internalization_consumer.v1"
MAX_NEURONS = 5000
TRAIN_NEURONS = "s2_training_samples.jsonl"

# Outcome -> confidence calibration for teacher-internalized neurons (2026-10-15).
# _NEGATIVE_CONF sits just above S2_SHADOW_MIN_CONFIDENCE (0.50) so a partial/failed
# outcome still trains the ranker, while _POSITIVE_CONF keeps proven rows ahead of it.
# Recalibrate here, not at the call site. ponytail: two knobs; add per-outcome
# granularity only once a third outcome class shows up in the ledger.
_NEGATIVE_CONF = 0.55
_POSITIVE_CONF = 0.95


def _lae(workspace):
    return Path(workspace) / ".lae"


def _read_json(path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def _write_json(path, data):
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _mint(prefix, seed):
    return f"{prefix}_{hashlib.sha1(str(seed).encode('utf-8', 'replace')).hexdigest()[:12]}"


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(v)))


# ── reasoning neurons ────────────────────────────────────────────────────────
def load_neurons(workspace):
    data = _read_json(_lae(workspace) / "stage4_reasoning_neurons.json", {})
    neurons = data.get("neurons")
    if not isinstance(neurons, list):
        neurons = []
    return {"schema": SCHEMA, "neurons": neurons, "count": len(neurons),
            "updated_at": data.get("updated_at")}


def _save_neurons(workspace, neurons):
    return _write_json(_lae(workspace) / "stage4_reasoning_neurons.json",
                       {"schema": SCHEMA, "neurons": neurons[-MAX_NEURONS:],
                        "count": min(len(neurons), MAX_NEURONS),
                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})


def observe_pattern(workspace, entry=None, drive_snapshot=None, outcome_result=None):
    """Mint a reasoning neuron only when the tick carries a real pattern, else None."""
    entry = entry if isinstance(entry, dict) else {}
    outcome = outcome_result if isinstance(outcome_result, dict) else {}
    drives = drive_snapshot if isinstance(drive_snapshot, dict) else {}
    pe = outcome.get("prediction_error")
    pe = float(pe) if isinstance(pe, (int, float)) else 0.0
    conc = float(entry.get("concentration", 0.0) or 0.0)
    comp = float(entry.get("competition", 0.0) or 0.0)
    stag = int(entry.get("stagnation_streak", 0) or 0)
    dominant = str(entry.get("dominant", "unknown") or "unknown")
    tick = int(entry.get("tick", 0) or 0)

    informative = pe >= 0.15 or stag >= 3 or (conc < 0.10 and comp > 0.85)
    if not informative:
        return None

    trigger = {"concentration": round(conc, 4), "competition": round(comp, 4), "stagnation": stag,
               "prediction_error": round(pe, 4)}
    drive = drives.get("dominant_drive") or "unknown"
    neuron = {
        "id": _mint("rn", f"{tick}|{dominant}|{json.dumps(trigger, sort_keys=True)}"),
        "kind": "association",
        "source": "observe_pattern",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tick": tick,
        "trigger": trigger,
        "dominant": dominant,
        "drive": drive,
        "hypothesis": (f"{dominant} under conc={conc:.2f} comp={comp:.2f} stag={stag} "
                       f"pe={pe:.2f} -> next time favour {drive}"),
        "confidence": round(_clamp(0.5 + 0.2 * (1.0 - pe)), 4),
        "feedback": {"updates": 0, "last_outcome": None, "last_judge": None, "ts": None},
    }
    state = load_neurons(workspace)
    neurons = state["neurons"] + [neuron]
    _save_neurons(workspace, neurons)
    return neuron


# ── creative candidates (one per tick, same as the surviving jsonl's cadence) ─
def _target_type(dominant, drive):
    table = {"stability": "memory", "integration": "rule_modification",
             "exploration": "curiosity", "novelty": "planning"}
    if drive in table and table[drive] != dominant:
        return table[drive]
    return "memory" if dominant != "memory" else "planning"


def generate_creative_candidates(workspace, context=None):
    context = context if isinstance(context, dict) else {}
    tick = int(context.get("tick", 0) or 0)
    pe = float(context.get("prediction_error", 0.0) or 0.0)
    stag = int(context.get("stagnation_streak", 0) or 0)
    conc = float(context.get("concentration", 0.0) or 0.0)
    dominant = str(context.get("dominant", "unknown") or "unknown")

    state = load_neurons(workspace)
    source = state["neurons"][-1]["id"] if state["neurons"] else "rn_none"
    drive = "unknown"
    if state["neurons"]:
        drive = state["neurons"][-1].get("drive", "unknown")
    dissonance = round(_clamp(1.0 - conc), 4)
    scores = {"novelty": round(_clamp(pe + 0.05 * min(stag, 10)), 4),
              "usefulness": round(_clamp(0.5 + 0.1 * min(stag, 5)), 4),
              "risk": round(_clamp(1.0 - conc), 4)}
    candidate = {
        "candidate_id": _mint("cc", f"{tick}|{source}|{dominant}"),
        "source_neuron": source,
        "kind": "association",
        "trigger": {"prediction_error": round(pe, 4), "dissonance": dissonance, "stagnation": float(stag)},
        "scores": scores,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "proposal": {"from": dominant, "to": _target_type(dominant, drive),
                     "why": f"stagnation={stag} pe={pe:.2f}; widen selection away from {dominant}"},
        "birth_state": {"concentration": round(conc, 4), "dominant": dominant,
                        "stagnation_streak": stag,
                        "raw_dominant_history": dict(context.get("raw_dominant_history") or {}),
                        "creative_variant": context.get("creative_variant", 0)},
    }
    row = {"timestamp": time.strftime("%H:%M:%S"), "tick": tick, "candidate": candidate}
    written = False
    try:
        path = _lae(workspace) / "stage4_creative_candidates.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        written = True
    except Exception:
        pass
    return {"schema": "stage4_creative.v1", "ts": row["timestamp"], "tick": tick, "written": written,
            "candidate_id": candidate["candidate_id"], "source_neuron": source,
            "kind": candidate["kind"], "scores": scores, "proposal": candidate["proposal"]}


# ── feedback ─────────────────────────────────────────────────────────────────
def apply_feedback(workspace, body=None):
    """Bind an external verdict back to the neuron it belongs to (no match -> updated 0)."""
    body = body if isinstance(body, dict) else {}
    did = str(body.get("decision_id") or body.get("task_id") or "")
    nid = str(body.get("reasoning_neuron") or body.get("neuron_id") or "")
    if not did and not nid:
        return {"updated": 0, "reason": "no decision_id/neuron id"}
    state = load_neurons(workspace)
    neurons = state["neurons"]
    outcome = str(body.get("outcome") or "")
    judge = str(body.get("judge") or "")
    updated = 0
    touched = None
    for n in neurons:
        if (nid and n.get("id") == nid) or (did and n.get("decision_id") == did):
            fb = n.setdefault("feedback", {})
            fb["updates"] = int(fb.get("updates", 0) or 0) + 1
            fb["last_outcome"] = outcome
            fb["last_judge"] = judge
            fb["last_reason"] = str(body.get("reason") or "")[:280]
            fb["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            delta = 0.05 if outcome == "success" else (-0.10 if outcome in ("failure", "failed") else 0.0)
            if judge == "verified":
                delta += 0.05
            n["confidence"] = round(_clamp(float(n.get("confidence", 0.5)) + delta, 0.05, 0.99), 4)
            updated += 1
            touched = n
    if updated:
        _save_neurons(workspace, neurons)
    return {"updated": updated, "neuron": (touched or {}).get("id"),
            "confidence": (touched or {}).get("confidence")}


# ── S2 consumer (training samples -> reasoning neurons) ──────────────────────
def _consumer_path(workspace):
    return _lae(workspace) / "s2_internalization_consumer.json"


def s2_consumer_status(workspace):
    data = _read_json(_consumer_path(workspace), {})
    consumed = data.get("consumed") if isinstance(data.get("consumed"), dict) else {}
    last = data.get("last_result") if isinstance(data.get("last_result"), dict) else {}
    out = dict(last)
    out["consumed"] = len(consumed)
    out["last_run"] = data.get("last_run")
    return out


def consume_verified_teacher_samples(workspace):
    """Consume each verified training sample exactly once (ledger-keyed by decision_id)."""
    lae = _lae(workspace)
    src = lae / TRAIN_NEURONS
    ledger = _read_json(_consumer_path(workspace), {})
    consumed = ledger.get("consumed") if isinstance(ledger.get("consumed"), dict) else {}
    result = {"path": str(src), "read": 0, "applied": 0, "already_consumed": 0,
              "invalid": 0, "reindexed": 0, "status": "ok"}
    rows = []
    try:
        with src.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    rows.append(line)
    except Exception as exc:
        result["status"] = f"error: {type(exc).__name__}: {exc}"
        return result
    result["read"] = len(rows)

    neurons = load_neurons(workspace)["neurons"]
    # 2026-11-09 (l5g-20261109-068193, commander P1): the consumer ledger keys
    # exactly-once minting, but the neuron pool lost most minted s2rn_* neurons
    # (file wipes; this module docstring records the 09-30 zero-fill) while the
    # ledger survived. Measured this session: ledger consumed=3005 vs pool 76
    # s2rn_* (74 unique decision_ids) -> 2931 consumed samples have NO neuron,
    # so applied stays 0 forever and S2 shadow accuracy froze at 0.0487
    # (last_sample 2026-10-16). Snapshot the pool decision_ids so a
    # consumed-but-missing sample can be re-minted below.
    _pool_dids = {n.get("decision_id") for n in neurons
                  if n.get("kind") == "s2_teacher_internalization"}
    minted = 0
    for line in rows:
        try:
            row = json.loads(line)
        except Exception:
            result["invalid"] += 1
            continue
        if not isinstance(row, dict):
            result["invalid"] += 1
            continue
        did = str(row.get("decision_id") or "")
        if not did or not row.get("query") or not isinstance(row.get("teacher"), dict):
            result["invalid"] += 1
            continue
        _stype = str(((row.get("s1") or {}).get("type")) or "")
        _ttype = str(row["teacher"].get("type") or "")
        _selfagree = bool(_stype and _ttype and _stype == _ttype)
        if did in consumed:
            # 2026-11-09 (l5g-20261109-068193, commander P1): consumed in the
            # ledger but its neuron is gone from the pool (wipe/desync) ->
            # re-admit through the normal gates below instead of skipping;
            # counted in "reindexed". Without this, applied stays 0 forever.
            if did in _pool_dids:
                result["already_consumed"] += 1
                continue
            # 2026-11-10 (l5g-20261110-070237, commander P1): consumed-but-missing
            # is a wiped mint ONLY if the row could ever have minted. A
            # self-agreeing row never mints (the gate below rejects it), so it is
            # a memoized skip, not a lost neuron -- measured this session:
            # 0/1313 pool neurons come from a self-agreeing row while
            # 1943/1943 reindexed rows self-agree. Re-admitting them every run
            # is churn that pins reindexed at 1943 and applied at 0 forever.
            if _selfagree:
                result["skipped_self_agreement"] = result.get("skipped_self_agreement", 0) + 1
                continue
            result["reindexed"] += 1
        outcome = row.get("actual_outcome") if isinstance(row.get("actual_outcome"), dict) else {}
        # 2026-10-15: the gate was `judge == "verified"`, but POST /feedback defaults
        # judge to "neutral" (tl_ego_server.py:25183) and only ever emits
        # verified/neutral/error/positive. Measured histogram over 2061 rows:
        #   957 judge=neutral outcome=partial | 768 verified/success | 324 error/failed
        # So `partial` — the single most informative outcome class — was discarded
        # every run and applied stayed 0 forever; the pool froze at 772 neurons and
        # local accuracy sat at 0.057 while claiming 0.97 confidence.
        # Admit any MEASURED outcome, and let the outcome carry the signal instead of
        # a hand-maintained synonym list. Confidence encodes outcome polarity:
        #   partial/failed -> low, success/completed/landed -> high.
        _judge = str(outcome.get("judge", "") or "")
        _out = str(outcome.get("outcome", "") or "")
        # Keep the teacher's own confidence as the ceiling, then re-weight by what
        # actually happened: partial/failed -> floor it, success -> keep it.
        # Floor is 0.55, deliberately ABOVE S2_SHADOW_MIN_CONFIDENCE (0.50): a
        # partial outcome still carries the teacher's type label, so the neuron must
        # be able to compete in ranking — but it must rank BELOW any success-backed
        # neuron, which is what the gap to _POSITIVE_CONF provides. Flooring at 0.12
        # made every partial-row neuron dead on arrival (measured: all 1282 minted at
        # 0.12, none could ever pass the 0.50 shadow gate).
        _tconf = float(row["teacher"].get("confidence", 0.5) or 0.5)
        # 2026-10-30: 1863/2848 samples (65.4%) carry a teacher label identical to the
        # system's OWN S1 prediction. Minting those pays full credit for confirming
        # what ego already believed -> the pool has no gradient and a chronological
        # split showed lift ~0 over the majority class. Keep only rows where the
        # teacher disagrees with S1 (real correction) or S1 is absent.
        if _selfagree:
            result["skipped_self_agreement"] = result.get("skipped_self_agreement", 0) + 1
            # 2026-11-10 (l5g-20261110-070237, commander P1): a skipped row was never
            # minted, so it must NOT enter the consumed ledger. Marking it consumed
            # made the 2026-11-09 reindex path re-admit it every run and re-veto it
            # via this same gate -- measured: reindexed=1943 with
            # reindexed_would_mint=0 and applied=0 on every run. consumed is the mint
            # ledger; only a minted neuron belongs in it.
            continue
        _negative = _out in ("partial", "failed", "failure", "error", "unknown", "")
        _positive = _out in ("success", "completed", "landed", "explored", "verified")
        if not _out:
            result["invalid"] += 1
            continue
        _conf = _POSITIVE_CONF if _positive else (_NEGATIVE_CONF if _negative else _tconf)
        neuron = {
            "id": _mint("s2rn", did),
            "kind": "s2_teacher_internalization",
            "source": "consume_verified_teacher_samples",
            "decision_id": did,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "query": str(row.get("query"))[:400],
            "teacher": {"type": row["teacher"].get("type"), "confidence": row["teacher"].get("confidence")},
            "s1": row.get("s1") if isinstance(row.get("s1"), dict) else {},
            # 2026-10-15: this field is what makes the neuron ELIGIBLE at all.
            # tl_ego_server.py:5942 skips any record without s2_type/s2_label
            # ("Only a record explicitly carrying an s2_type/s2_label may
            # participate in local S2"), so a consumer that only writes
            # teacher.type mints neurons that can never be selected — they sit in
            # the pool inflating the count while the ranker keeps using the 772
            # legacy rows. Measured: applied=1282 yet s2-typed stayed 772, and all
            # 1282 new rows had zero candidates. Mirror the teacher label here.
            "s2_type": str(row["teacher"].get("type") or ""),
            "s2_label": str(row["teacher"].get("type") or ""),
            "hypothesis": (f"teacher {row['teacher'].get('type')} beats S1 "
                           f"{str((row.get('s1') or {}).get('type'))} on this query"),
            # Teacher confidence, re-weighted by what actually happened. A partial
            # outcome must NOT mint a 0.9-confidence neuron — that is exactly how
            # 314 confident 'action' neurons outvoted 7 integrator ones.
            "confidence": round(_clamp(_conf, 0.05, 0.99), 4),
            "feedback": {"updates": 0, "last_outcome": _out,
                         "last_judge": _judge or "unknown", "ts": outcome.get("ts")},
        }
        neurons.append(neuron)
        consumed[did] = time.strftime("%Y-%m-%d %H:%M:%S")
        result["applied"] += 1
        minted += 1
    if minted:
        _save_neurons(workspace, neurons)
        print(f"[s2-consumer] minted {minted} s2rn_* reasoning neurons", flush=True)

    ledger = {"schema": CONSUMER_SCHEMA, "consumed": consumed,
              "last_run": time.strftime("%Y-%m-%d %H:%M:%S"), "last_result": result}
    _write_json(_consumer_path(workspace), ledger)
    out = dict(result)
    out["consumed"] = len(consumed)
    out["last_run"] = ledger["last_run"]
    return out


if __name__ == "__main__":  # self-check: mint-once semantics + ledger + creative row
    import tempfile
    ws = Path(tempfile.mkdtemp())
    (ws / ".lae").mkdir()
    sample = {"schema": "s2_training_sample.v1", "decision_id": "814f5ffeffea", "query": "rain implies wet ground?",
              "s1": {"type": "action", "concentration": 0.51}, "teacher": {"type": "integrator", "confidence": 0.95},
              "actual_outcome": {"outcome": "success", "judge": "verified", "reason": "ok", "ts": "2026-09-25 16:40:11"}}
    (ws / ".lae" / TRAIN_NEURONS).write_text(json.dumps(sample) + "\n", encoding="utf-8")
    r1 = consume_verified_teacher_samples(ws)
    assert (r1["read"], r1["applied"], r1["already_consumed"], r1["invalid"]) == (1, 1, 0, 0), r1
    r2 = consume_verified_teacher_samples(ws)
    assert (r2["read"], r2["applied"], r2["already_consumed"]) == (1, 0, 1), r2      # no double training
    st = s2_consumer_status(ws)
    assert st["consumed"] == 1 and st["applied"] == 0 and st["already_consumed"] == 1, st
    # 2026-10-30: a sample whose teacher label equals ego's own S1 prediction is
    # confirming prior belief -> recorded as consumed but never minted.
    dup = dict(sample, decision_id="dupidsame", s1={"type": "integrator", "concentration": 0.51})
    with (ws / ".lae" / TRAIN_NEURONS).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dup) + "\n")
    r3 = consume_verified_teacher_samples(ws)
    assert (r3["applied"], r3.get("skipped_self_agreement")) == (0, 1), r3
    assert len(load_neurons(ws)["neurons"]) == 1, "self-agreement row must not mint"
    n = load_neurons(ws)["neurons"][0]
    assert n["id"].startswith("s2rn_") and n["decision_id"] == "814f5ffeffea"
    fb = apply_feedback(ws, {"decision_id": "814f5ffeffea", "outcome": "success", "judge": "verified"})
    assert fb["updated"] == 1 and fb["confidence"] > 0.5, fb
    rn = observe_pattern(ws, {"tick": 5, "dominant": "action", "concentration": 0.05, "competition": 0.9,
                              "stagnation_streak": 0}, {"dominant_drive": "stability"}, {"prediction_error": 0.0})
    assert rn and rn["id"].startswith("rn_")
    assert observe_pattern(ws, {"tick": 6, "dominant": "action", "concentration": 0.5, "competition": 0.5}, {}, {}) is None
    cc = generate_creative_candidates(ws, {"tick": 6, "prediction_error": 0.3, "stagnation_streak": 4,
                                           "concentration": 0.2, "dominant": "action"})
    assert cc["written"] and (ws / ".lae" / "stage4_creative_candidates.jsonl").read_text().strip()
    print("stage4_s2_internalization self-check OK")
