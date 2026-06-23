#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 3: Causal Autobiography.

Append-only causal event log derived from Stage 1 outcomes, Stage 2 drives,
and resting ticks. This is intentionally simple and auditable.
"""

import json
import time
from pathlib import Path


def _path(workspace):
    return Path(workspace) / ".lae" / "stage3_timeline.jsonl"


def record_event(workspace, resting_entry, drive_snapshot=None, outcome_result=None):
    dominant_drive = None
    if drive_snapshot:
        dominant_drive = drive_snapshot.get("dominant_drive")
    cause = "resting_tick"
    effect = f"dominant={resting_entry.get('dominant', 'unknown')}"
    if outcome_result:
        cause = "action_outcome"
        pe_source = outcome_result.get("prediction_error_source")
        source_suffix = f" source={pe_source}" if pe_source else ""
        effect = f"prediction_error={outcome_result.get('prediction_error', 0):.3f}{source_suffix}"
    elif dominant_drive:
        cause = f"drive:{dominant_drive}"

    event = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tick": resting_entry.get("tick"),
        "cause": cause,
        "effect": effect,
        "dominant": resting_entry.get("dominant"),
        "dominant_drive": dominant_drive,
        "prediction_error": outcome_result.get("prediction_error") if outcome_result else None,
        "prediction_error_source": outcome_result.get("prediction_error_source") if outcome_result else None,
        "concentration": resting_entry.get("concentration"),
        "competition": resting_entry.get("competition"),
        "stagnation_streak": resting_entry.get("stagnation_streak"),
    }
    path = _path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def load_timeline(workspace, limit=50):
    path = _path(workspace)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-max(1, int(limit)):]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events


def explain_why(workspace, query=""):
    events = load_timeline(workspace, limit=20)
    if not events:
        return {"answer": "No causal autobiography events have been recorded yet.", "events": []}
    last = events[-1]
    parts = [
        f"Last recorded tick {last.get('tick')} had cause {last.get('cause')} and effect {last.get('effect')}.",
    ]
    if last.get("dominant_drive"):
        parts.append(f"The strongest drive was {last['dominant_drive']}.")
    if last.get("prediction_error") is not None:
        parts.append(f"The latest prediction error was {last['prediction_error']:.3f}.")
    return {"query": query, "answer": " ".join(parts), "events": events}
