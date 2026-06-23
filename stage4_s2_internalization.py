#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 4: S2 Internalization.

Records repeated high-signal reasoning patterns as lightweight reusable records.
It does not mutate the neural grid; it creates an auditable pool first.
"""

import hashlib
import json
import time
from pathlib import Path


def _path(workspace):
    return Path(workspace) / ".lae" / "stage4_reasoning_neurons.json"


def load_neurons(workspace):
    path = _path(workspace)
    if not path.exists():
        return {"neurons": {}, "last_updated": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {"neurons": data.get("neurons", {}), "last_updated": data.get("last_updated")}


def save_neurons(workspace, state):
    path = _path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def observe_pattern(workspace, resting_entry, drive_snapshot=None, outcome_result=None):
    dominant_drive = (drive_snapshot or {}).get("dominant_drive", "none")
    prediction_error = float((outcome_result or {}).get("prediction_error", 0.0) or 0.0)
    concentration = float(resting_entry.get("concentration", 0.0) or 0.0)
    if prediction_error < 0.5 and concentration < 0.7:
        return None

    signature = "|".join([
        str(resting_entry.get("dominant", "unknown")),
        dominant_drive,
        "surprise" if prediction_error >= 0.5 else "focus",
    ])
    neuron_id = "rn_" + hashlib.sha1(signature.encode("utf-8")).hexdigest()[:12]
    state = load_neurons(workspace)
    neurons = state["neurons"]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    item = neurons.get(neuron_id, {
        "id": neuron_id,
        "signature": signature,
        "created_at": now,
        "uses": 0,
        "confidence": 0.5,
    })
    item["uses"] += 1
    item["last_seen"] = now
    item["last_tick"] = resting_entry.get("tick")
    item["dominant"] = resting_entry.get("dominant")
    item["dominant_drive"] = dominant_drive
    item["confidence"] = round(min(0.99, item.get("confidence", 0.5) + 0.05 + prediction_error * 0.05), 4)
    neurons[neuron_id] = item
    state["last_updated"] = now
    save_neurons(workspace, state)
    return item

