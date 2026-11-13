"""Replay the real 21069-row ledger through the rebuilt rule set.

The unit selfcheck proves the decision table matches my own expectations. This
one has a hard limit worth stating up front, because it looks stronger than it is:

  `.lae/l7/proposals.json` only retains proposals whose status is `reviewed` or
  `applied` — i.e. only the ones L8 APPROVED. So all 159 joinable rows are
  approvals, and this replay can validate the doc-only branch and nothing else.
  The flag and deny branches have no surviving inputs and cannot be replayed.
  (Measured: 159/159 joinable rows have decision == "approve".)

What it DOES prove, which is worth the trouble:
  * doc-only is decided BEFORE risk/lines. DECOMPOSE_SERVER appears as both
    approve(105) and deny(1098) at risk=high/500 lines; a risk-first rule would
    have denied all 1203. So the ordering is forced by the data.
  * the doc-only test must key on the CODE BODY, not the proposal type —
    WIRE_MODULE is 5165 approve vs 8070 flag, same type, same risk.
  * our rule reproduces every approval the original made.
"""
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from l8_governance import _is_doc_only, _lines, _risk  # noqa: E402

WS = r"C:\Users\Administrator\.openclaw\workspace"
GOV = os.path.join(WS, ".lae", "l8", "governance_log.jsonl")
PROPS = os.path.join(WS, ".lae", "l7", "proposals.json")


def decide(prop):
    """The rebuilt rule, isolated from ledger I/O for replay."""
    lines, risk = _lines(prop), _risk(prop)
    if _is_doc_only(prop):
        return "approve"
    if risk == "low" and lines <= 30:
        return "approve"
    if risk in ("high", "") or lines >= 500:
        return "deny"
    return "flag"


def main():
    with open(PROPS, encoding="utf-8") as fh:
        props = {p["id"]: p for p in json.load(fh)["proposals"]}
    rows = []
    with open(GOV, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    hit = [r for r in rows if r["proposal_id"] in props]
    agree = sum(1 for r in hit if decide(props[r["proposal_id"]]) == r["decision"])
    replayable = collections.Counter(r["decision"] for r in hit)
    print("replayable rows: %d / %d" % (len(hit), len(rows)))
    print("their decisions: %s" % dict(replayable))
    print("agreement:       %d / %d  (%.1f%%)" % (agree, len(hit), 100.0 * agree / max(1, len(hit))))
    if len(replayable) < len({"approve", "flag", "deny"}):
        print("  NOTE: only the doc-only/approve branch is replayable — the surviving")
        print("        proposals.json retains approved proposals only, so flag/deny")
        print("        inputs do not exist. Their rules rest on the measured")
        print("        thresholds in the log, not on a replay.")

    misses = [(r["decision"], decide(props[r["proposal_id"]]), r["proposal_id"],
               props[r["proposal_id"]].get("risk"), props[r["proposal_id"]].get("est_lines"),
               _is_doc_only(props[r["proposal_id"]])) for r in hit
              if decide(props[r["proposal_id"]]) != r["decision"]]
    for m in misses[:10]:
        print("  MISMATCH orig=%-8s mine=%-8s %s risk=%-7s lines=%-4s doc_only=%s" % m)

    # the counts must also line up with the surviving stats file
    with open(os.path.join(WS, ".lae", "l8", "governance_stats.json"), encoding="utf-8") as fh:
        st = json.load(fh)
    tally = {}
    for r in rows:
        tally[r["decision"]] = tally.get(r["decision"], 0) + 1
    print("\nledger vs stats.json:")
    for pair in (("approve", "approved"), ("flag", "flagged"), ("deny", "denied")):
        print("  %-8s ledger=%-6d stats=%-6s %s" % (pair[0], tally.get(pair[0], 0),
                                                    st.get(pair[1]),
                                                    "OK" if tally.get(pair[0], 0) == st.get(pair[1]) else "MISMATCH"))
    assert agree == len(hit), "rule disagrees with the original on %d rows" % (len(hit) - agree)
    assert set(replayable) == {"approve"}, (
        "expected only approvals to be replayable; if flag/deny inputs now survive, "
        "extend the replay instead of trusting this line")
    print("\nOK: rebuilt rule reproduces every replayable historical decision")
    print("    (doc-only branch validated; flag/deny thresholds measured from the log)")


if __name__ == "__main__":
    main()
