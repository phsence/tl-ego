"""continuity.py — REBUILT 2026-09-30 (original source lost in the 09-30 wipe).

Provenance (call sites in tl_ego_server.py; the comments there are the spec):
  * tl_ego_server.py:13441 `_append_continuity_frame(entry)` builds a StateFrame dict
    (tick, dominant, concentration, competition, stagnation_streak, phase, surprise,
    noise_phase, schedule_idx, schedule_tick_in_phase, noise_schedule,
    comm_suppress_until, dominant_history, raw_dominant_history,
    raw_gate_divergence_count, runtime_generation_id, timestamp) and calls
    `append_frame(frame)` — one JSONL line per tick, journal must never kill a tick.
  * tl_ego_server.py:24986 `restore_into(RESTING_STATE)` must return a dict with keys
    resumed_tick / continuation_of / frame_count (the boot print reads exactly those),
    or None when there is no journal ("fresh life").
  * status contract (23341): `RESTING_STATE["continuity"]` or a fallback that checks
    `WORKSPACE/.lae/continuity.journal.jsonl` — hence the journal lives there.
No state file existed when this was rebuilt, so the journal is new: the first life
after the rebuild is honestly a "fresh life"; the second boot resumes it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

JOURNAL_NAME = "continuity.journal.jsonl"
# frame key -> RESTING_STATE key (the frame says "dominant", the state says "last_dominant")
RESUME_MAP = {"dominant": "last_dominant", "stagnation_streak": "stagnation_streak",
              "noise_phase": "noise_phase", "schedule_idx": "schedule_idx",
              "schedule_tick_in_phase": "schedule_tick_in_phase",
              "noise_schedule": "noise_schedule", "comm_suppress_until": "comm_suppress_until",
              "dominant_history": "dominant_history",
              "raw_dominant_history": "raw_dominant_history",
              "raw_gate_divergence_count": "raw_gate_divergence_count"}
TAIL_BYTES = 262144


def _journal():
    for var in ("OPENCLAW_WORKSPACE", "EGO_WORKSPACE"):
        root = os.environ.get(var)
        if root:
            return Path(root) / ".lae" / JOURNAL_NAME
    return Path(__file__).resolve().parents[2] / ".lae" / JOURNAL_NAME


def append_frame(frame):
    """Append one StateFrame. Never raises (the caller's tick must survive)."""
    if not isinstance(frame, dict):
        return False
    try:
        path = _journal()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except Exception:
        return False


def _tail_frames(limit=200):
    path = _journal()
    if not path.exists():
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            block = b""
            lines = []
            while pos > 0 and len(lines) <= limit:
                step = min(65536, pos)
                pos -= step
                fh.seek(pos)
                block = fh.read(step) + block
                lines = block.split(b"\n")
        rows = [ln for ln in lines if ln.strip()][-limit:]
    except Exception:
        return []
    out = []
    for ln in rows:
        try:
            out.append(json.loads(ln.decode("utf-8", "replace")))
        except Exception:
            continue
    return out


def frame_count():
    path = _journal()
    if not path.exists():
        return 0
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except Exception:
        return 0


def restore_into(resting_state):
    """Resume the experience stream: last frame -> RESTING_STATE. None when no journal."""
    frames = _tail_frames(limit=8)
    if not frames:
        return None
    last = frames[-1]
    try:
        n = frame_count()
    except Exception:
        n = len(frames)
    resumed = {
        "resumed_tick": last.get("tick"),
        "continuation_of": last.get("runtime_generation_id"),
        "frame_count": n,
        "last_frame_ts": last.get("timestamp"),
        "resumed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "journal": str(_journal()),
    }
    if isinstance(resting_state, dict):
        try:
            if last.get("tick") is not None:
                resting_state["tick"] = int(last["tick"])        # keep counting, don't restart at 0
            for frame_key, state_key in RESUME_MAP.items():
                if last.get(frame_key) is not None:
                    resting_state[state_key] = last[frame_key]
            resting_state["tick"] = int(resting_state.get("tick", 0) or 0)
        except Exception:
            pass
    return resumed


if __name__ == "__main__":  # self-check: append -> resume round trip, and fresh life
    import tempfile
    ws = tempfile.mkdtemp()
    os.environ["OPENCLAW_WORKSPACE"] = ws
    assert restore_into({"tick": 0}) is None                      # fresh life
    assert append_frame({"tick": 41, "dominant": "memory", "runtime_generation_id": "g-1",
                         "stagnation_streak": 3, "noise_schedule": "white", "schedule_idx": 2,
                         "timestamp": "2026-09-30T15:00:00+0800"})
    assert append_frame({"tick": 42, "dominant": "action", "runtime_generation_id": "g-1",
                         "stagnation_streak": 0, "timestamp": "2026-09-30T15:00:10+0800"})
    state = {"tick": 0}
    res = restore_into(state)
    assert res and res["resumed_tick"] == 42 and res["continuation_of"] == "g-1" and res["frame_count"] == 2, res
    assert state["tick"] == 42 and state["last_dominant"] == "action"
    assert append_frame("not a dict") is False
    print("continuity self-check OK", res)
