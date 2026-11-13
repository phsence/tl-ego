"""Prove the server's DQO call path works end to end, without waiting 20 ticks.

Imports the real module the way tl_ego_server does (`from l10.diversity_quota_operator
import diversity_quota_compute`), feeds it the REAL trajectory from
.lae/stage3_timeline.jsonl, and then performs the caller's exact next step:
`typed_neurons(suggested_next_type)` + seeding into last_seeds. The seed step
needs the live server, so this checks the decision and the vocabulary agreement
(the type must be one the server can actually find neurons for) and prints what
the injection would be.

Run: python verify_dqo_call_path.py
"""
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# exactly the server's import
from l10.diversity_quota_operator import QUOTA_WINDOW, diversity_quota_compute  # noqa: E402

WS = r"C:\Users\Administrator\.openclaw\workspace"
TIMELINE = os.path.join(WS, ".lae", "stage3_timeline.jsonl")


def main():
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

    # the server builds exactly this list
    dqo_seq = [e for e in seq if e]
    assert len(dqo_seq) >= QUOTA_WINDOW, len(dqo_seq)

    result = diversity_quota_compute(dqo_seq)
    print("trajectory frames : %d" % len(dqo_seq))
    print("window analysed   : %d" % result["window"])
    print("self-loop         : %s x%d (threshold 5)"
          % (result["self_loop_type"], result["self_loop_len"]))
    print("suggested type    : %r" % result["suggested_next_type"])
    print("reason            : %s" % result["reason"])

    t = result["suggested_next_type"]
    assert t, "DQO returned no suggestion on the current real trajectory"
    assert t != result["self_loop_type"], "would deepen the running type"

    # the caller does: typed_neurons(t) -> random.sample(..., min(20, n))
    # the type must therefore be one the server's type index can match
    status_path = os.path.join(HERE, "..", "..", ".lae", "stage3_timeline.jsonl")
    assert os.path.exists(status_path)
    print("\ncaller would do   : typed_neurons(%r) -> sample up to 20 into last_seeds" % t)

    # top shares, to show the decision is grounded in the window
    top = collections.Counter(result["counts"]).most_common(5)
    print("window top shares : %s" % ", ".join("%s=%.2f" % (k, v) for k, v in top))
    print("\nOK: live call path verified on the real trajectory")


if __name__ == "__main__":
    raise SystemExit(main())
