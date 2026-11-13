"""stage3_causal_autobiography.py — REBUILT 2026-09-30 (original source lost in the 09-30 wipe).

Provenance:
  * row schema = the surviving `.lae/stage3_timeline.jsonl` (21924 rows written by the
    original module): ts, runtime_generation_id, session_id, session_boot, lineage_id,
    goal_id, goal_type, tick, cause, effect, dominant, dominant_drive, prediction_error,
    prediction_error_source, concentration, competition, stagnation_streak, identity_hash.
  * session file = the surviving `.lae/stage3_session.json`
    {"pid","boot","session_id":"s3-YYYYMMDD-HHMMSS-<pid>-<boot>","identity_recorded":true}.
  * call sites: record_event(WORKSPACE, entry, drive_snapshot=, outcome_result=, goal=)
                load_timeline(WORKSPACE, limit)  /autobiography/timeline
                explain_why(WORKSPACE, query=)   /autobiography/why
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

SCHEMA = "stage3_timeline.v1"


def _paths(workspace):
    lae = Path(workspace) / ".lae"
    return lae / "stage3_timeline.jsonl", lae / "stage3_session.json"


def _session(workspace):
    """Own the session identity file: same pid keeps its session, a new pid gets boot+1."""
    _, spath = _paths(workspace)
    prev = {}
    try:
        prev = json.loads(spath.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    pid = os.getpid()
    if prev.get("pid") == pid and prev.get("session_id"):
        return prev
    boot = int(prev.get("boot", 0) or 0) + 1
    sid = f"s3-{time.strftime('%Y%m%d-%H%M%S')}-{pid}-{boot}"
    cur = {"pid": pid, "boot": boot, "session_id": sid, "identity_recorded": False}
    try:
        spath.parent.mkdir(parents=True, exist_ok=True)
        spath.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return cur


def _identity_hash(workspace, session):
    gen = os.environ.get("TL_EGO_GENERATION_ID", "")
    raw = f"{gen}|{session.get('session_id')}|{Path(workspace)}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def record_event(workspace, entry=None, drive_snapshot=None, outcome_result=None, goal=None):
    """Append one causal event. Returns the event dict (the caller stores it in entry['titan'])."""
    entry = entry if isinstance(entry, dict) else {}
    outcome = outcome_result if isinstance(outcome_result, dict) else {}
    drives = drive_snapshot if isinstance(drive_snapshot, dict) else {}
    goal = goal if isinstance(goal, dict) else {}
    session = _session(workspace)
    tpath, _ = _paths(workspace)

    pe = outcome.get("prediction_error")
    pe = float(pe) if isinstance(pe, (int, float)) else 0.0
    outcome_name = str(outcome.get("outcome", "") or "none")
    pe_src = str(outcome.get("prediction_error_source", "") or ("computed_structured_delta" if pe else "none"))
    tick = int(entry.get("tick", 0) or 0)
    goal_id = str(goal.get("goal_id") or entry.get("goal_id") or "")
    goal_type = str(goal.get("goal_type") or entry.get("goal_type") or "")
    event = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "runtime_generation_id": os.environ.get("TL_EGO_GENERATION_ID") or None,
        "session_id": session.get("session_id"),
        "session_boot": session.get("boot"),
        "lineage_id": "gl-" + _identity_hash(workspace, session)[:16],
        "goal_id": goal_id or None,
        "goal_type": goal_type or None,
        "tick": tick,
        "cause": str(entry.get("cause") or ("action_outcome" if outcome_name != "none" else "resting_tick")),
        "effect": f"prediction_error={pe:.3f} source={pe_src}",
        "dominant": str(entry.get("dominant", "unknown") or "unknown"),
        "dominant_drive": drives.get("dominant_drive"),
        "prediction_error": round(pe, 6),
        "prediction_error_source": pe_src,
        "concentration": entry.get("concentration"),
        "competition": entry.get("competition"),
        "stagnation_streak": int(entry.get("stagnation_streak", 0) or 0),
        "identity_hash": _identity_hash(workspace, session),
    }
    try:
        tpath.parent.mkdir(parents=True, exist_ok=True)
        with tpath.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass
    if not session.get("identity_recorded"):
        session["identity_recorded"] = True
        try:
            _, spath = _paths(workspace)
            spath.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return event


def load_timeline(workspace, limit=50):
    """Last *limit* events, oldest first (callers take events[-1] as the latest)."""
    tpath, _ = _paths(workspace)
    limit = max(0, int(limit or 0))
    if limit == 0:
        return []
    try:
        # cheap tail read: the file grows to ~12MB, so scan backwards for the last N lines
        with tpath.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block, lines, pos = b"", [], size
            while pos > 0 and len(lines) <= limit:
                step = min(262144, pos)
                pos -= step
                fh.seek(pos)
                block = fh.read(step) + block
                lines = block.split(b"\n")
            rows = [l for l in lines if l.strip()][-limit:]
    except Exception:
        return []
    out = []
    for line in rows:
        try:
            out.append(json.loads(line.decode("utf-8", "replace")))
        except Exception:
            continue
    return out


def explain_why(workspace, query=""):
    """Keyword search over the recent timeline; no LLM call, no invention."""
    query = (query or "").strip()
    events = load_timeline(workspace, limit=400)
    if not query:
        return {"query": query, "count": len(events), "events": events[-50:], "summary": "no query given"}
    terms = [t for t in query.lower().split() if t]
    hits = []
    for ev in events:
        blob = " ".join(str(ev.get(k, "")) for k in ("cause", "effect", "dominant", "dominant_drive",
                                                      "goal_type", "goal_id", "prediction_error_source")).lower()
        if all(t in blob for t in terms):
            hits.append(ev)
    summary = (f"{len(hits)} of the last {len(events)} events mention {query!r}"
               if hits else f"nothing in the last {len(events)} events mentions {query!r}")
    return {"query": query, "count": len(hits), "events": hits[-50:], "summary": summary}


if __name__ == "__main__":  # self-check: append + tail read + search
    import tempfile
    ws = Path(tempfile.mkdtemp())
    ev1 = record_event(ws, {"tick": 10, "dominant": "action", "concentration": 0.4, "competition": 0.5,
                            "stagnation_streak": 2}, {"dominant_drive": "integration"},
                       {"outcome": "success", "prediction_error": 0.25}, {"goal_id": "l5g-1", "goal_type": "world_act"})
    assert ev1["dominant_drive"] == "integration" and ev1["goal_type"] == "world_act"
    ev2 = record_event(ws, {"tick": 11, "dominant": "memory"}, {}, {"outcome": "failure"})
    tl = load_timeline(ws, limit=10)
    assert [e["tick"] for e in tl] == [10, 11] and ev2["session_boot"] >= 1
    assert load_timeline(ws, limit=1)[-1]["tick"] == 11
    assert explain_why(ws, "world_act")["count"] == 1 and explain_why(ws)["count"] == 2
    assert json.loads((ws / ".lae" / "stage3_session.json").read_text())["identity_recorded"] is True
    print("stage3_causal_autobiography self-check OK")
