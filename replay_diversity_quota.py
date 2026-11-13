"""Replay the real 22144-frame trajectory through the rebuilt DQO.

The unit selfcheck proves the rule matches the contract and my own expectations.
This one answers the only question that matters for a per-tick intervention:
over the trajectory ego actually produced, how often does it fire, does it ever
recommend the type that is currently running, and does it concentrate on a few
types or actually break the observed monopolies?

Run: python replay_diversity_quota.py
"""
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "l10"))
from diversity_quota_operator import (  # noqa: E402
    QUOTA_WINDOW, SELF_LOOP_THRESHOLD, TYPE_UNIVERSE, diversity_quota_compute,
)

WS = r"C:\Users\Administrator\.openclaw\workspace"
TIMELINE = os.path.join(WS, ".lae", "stage3_timeline.jsonl")


def main():
    if not os.path.exists(TIMELINE):
        print("SKIP: %s not found" % TIMELINE)
        return 1

    seq = []
    with open(TIMELINE, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("dominant"):
                seq.append(d["dominant"])
    print("frames:            %d" % len(seq))
    print("types observed:    %d" % len(set(seq)))
    print("universe size:     %d" % len(TYPE_UNIVERSE))
    unknown = set(seq) - set(TYPE_UNIVERSE)
    assert not unknown, "trajectory has types outside TYPE_UNIVERSE: %s" % sorted(unknown)
    assert QUOTA_WINDOW == 20 and SELF_LOOP_THRESHOLD == 5

    fired = 0
    same_as_running = 0
    suggested = collections.Counter()
    runs_seen = 0
    prev_run = 0

    for i in range(len(seq)):
        # the server only calls DQO once the trajectory holds >= 20 frames
        if i + 1 < QUOTA_WINDOW:
            continue
        r = diversity_quota_compute(seq[: i + 1])
        if r["self_loop_len"] >= SELF_LOOP_THRESHOLD and r["self_loop_len"] != prev_run:
            runs_seen += 1
        prev_run = r["self_loop_len"]
        t = r["suggested_next_type"]
        if t:
            fired += 1
            suggested[t] += 1
            if t == r["self_loop_type"]:
                same_as_running += 1
            assert t in TYPE_UNIVERSE, t

    total = len(seq) - QUOTA_WINDOW + 1
    print("\nDQO evaluations:   %d" % total)
    print("fired:             %d  (%.1f%%)" % (fired, 100.0 * fired / total))
    print("self-loop runs >= %d: %d" % (SELF_LOOP_THRESHOLD, runs_seen))
    print("recommended the running type: %d  <-- must be 0" % same_as_running)
    assert same_as_running == 0, "DQO deepened a self-loop %d times" % same_as_running

    print("\nsuggested type distribution (top 12):")
    for t, c in suggested.most_common(12):
        share = c / max(1, fired)
        print("  %-20s %5d  %5.1f%% of fires" % (t, c, 100 * share))
    if suggested:
        print("distinct suggested types: %d of %d" % (len(suggested), len(TYPE_UNIVERSE)))

    # the whole point is to break monopolies, not to keep re-seeding the loser
    monopoly = [t for t, c in suggested.items() if share_of(c, fired) > 0.5]
    assert not monopoly, "DQO concentrated >50%% on %s" % monopoly

    print("\nOK: real-trajectory replay — fired %.1f%% of ticks, never deepened a "
          "self-loop, spread across %d types" % (100.0 * fired / total, len(suggested)))
    return 0


def share_of(c, total):
    return (c / total) if total else 0.0


if __name__ == "__main__":
    raise SystemExit(main())
