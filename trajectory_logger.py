"""
TL-Ego Trajectory Logger — persistent tick accumulator

Approach:
1. Poll /resting-log once per call
2. Diff against last logged tick
3. Append new ticks to trajectory_log.jsonl
4. Never duplicates, always append-only
"""

import json, os, time, sys
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "trajectory_log.jsonl"
STATE_TMP = DATA_DIR / "trajectory_log.tmp"
CHECKPOINT_FILE = DATA_DIR / "trajectory_log.checkpoint"
URL = "http://127.0.0.1:8766/resting-log"


def fetch():
    try:
        with urllib.request.urlopen(URL, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[tl-trajectory] fetch error: {e}", file=sys.stderr)
        return None


def load_state():
    """Return last persisted tick from lightweight checkpoint."""
    last = -1
    if CHECKPOINT_FILE.exists():
        try:
            last = int(CHECKPOINT_FILE.read_text().strip())
        except Exception:
            last = -1
    return last


def append(new_ticks, existing_last):
    """Append only ticks newer than existing_last; refresh checkpoint."""
    filtered = [e for e in new_ticks if e.get("tick", -1) > existing_last]
    if not filtered:
        return 0

    seen_batch = set()
    deduped = []
    for e in filtered:
        t = e.get("tick", -1)
        if t in seen_batch:
            continue
        seen_batch.add(t)
        deduped.append(e)

    with open(STATE_FILE, "a", encoding="utf-8") as f:
        for e in deduped:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    new_last = deduped[-1].get("tick", existing_last)
    try:
        CHECKPOINT_FILE.write_text(str(new_last))
    except Exception:
        pass

    # Periodic self-heal: reconcile checkpoint against actual file every 500 ticks
    try:
        if new_last and new_last % 500 == 0:
            last_from_file = -1
            for line in STATE_FILE.read_text(errors="ignore").split("\n"):
                if not line.strip():
                    continue
                try:
                    t = json.loads(line).get("tick", -1)
                    if isinstance(t, int) and t > last_from_file:
                        last_from_file = t
                except Exception:
                    pass
            if last_from_file > new_last:
                new_last = last_from_file
                CHECKPOINT_FILE.write_text(str(new_last))
    except Exception:
        pass

    return len(deduped)


def run():
    try:
        with urllib.request.urlopen(URL, timeout=30) as resp:
            raw = json.loads(resp.read().decode())
    except Exception as e:
        print(f"[tl-trajectory] fetch error: {e}", file=sys.stderr)
        return

    # Server returns {trajectory: [...], tick: <latest>, ...}
    traj = raw.get("trajectory", raw)
    if not isinstance(traj, list) or not traj:
        print(f"[tl-trajectory] empty or bad trajectory", file=sys.stderr)
        return

    server_latest = raw.get("tick", traj[-1].get("tick", -1))
    last_logged = load_state()

    new_ticks = [e for e in traj if e.get("tick", -1) > last_logged]
    if not new_ticks:
        print(
            f"[tl-trajectory] no_new_ticks total={last_logged} server_latest={server_latest} tick_range={traj[0].get('tick','?')}-{traj[-1].get('tick','?')}"
        )
        return

    added = append(new_ticks, last_logged)
    total = last_logged + added
    print(
        f"[tl-trajectory] added={added} total={total} tick_range={new_ticks[0].get('tick','?')}-{new_ticks[-1].get('tick','?')} server_latest={server_latest}"
    )


if __name__ == "__main__":
    run()
