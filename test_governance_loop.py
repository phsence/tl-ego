"""End-to-end dry run of the L7 -> L8 -> L9 governance loop, off the live service.

Exercises the exact three objects tl_ego_server._run_governance_check() builds:
  _L7.proposer.list_proposals(status="draft") -> create_governance().review_batch()
  -> L7 status writeback -> get_stats() -> create_monitor().check_loop()

Uses a scratch workspace so the real .lae/l7 + .lae/l8 ledgers are untouched.
Read-only against the real L7 proposer pyc, writing only into tmpdir.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from l8_governance import L8Governance, create_governance  # noqa: E402
from l9_autonomy_monitor import L9AutonomyMonitor  # noqa: E402


class _FakeProposer:
    """Stands in for L7's proposer: the same two methods the loop calls."""

    def __init__(self, props):
        self._p = {p["id"]: p for p in props}
        self.writes = []

    def list_proposals(self, status=None):
        rows = list(self._p.values())
        return [p for p in rows if status is None or p.get("status") == status]

    def update_status(self, pid, status, notes=""):
        self.writes.append((pid, status, notes))
        if pid in self._p:
            self._p[pid]["status"] = status
        return True


def main():
    drafts = [
        # doc-only -> approve -> applied
        {"id": "d1", "type": "GENERIC_FIX", "target_module": "tl_ego_server",
         "description": "Avg concentration low", "risk": "medium", "est_lines": 10,
         "status": "draft", "proposed_code": "# document the gap\n"},
        # low risk, small -> approve -> applied
        {"id": "d2", "type": "FIX", "target_module": "tl_ego_server",
         "description": "typo", "risk": "low", "est_lines": 12,
         "status": "draft", "proposed_code": "X = 1\n"},
        # medium risk with real code -> flag -> reviewed
        {"id": "d3", "type": "WIRE_MODULE", "target_module": "tl_ego_server",
         "description": "wire module", "risk": "medium", "est_lines": 15,
         "status": "draft", "proposed_code": "import foo\nfoo.bar()\n"},
        # high risk, large -> deny -> failed
        {"id": "d4", "type": "DECOMPOSE_SERVER", "target_module": "tl_ego_server",
         "description": "split server", "risk": "high", "est_lines": 500,
         "status": "draft", "proposed_code": "def a():\n    pass\n"},
        # placeholder body -> doc-only -> approve
        {"id": "d5", "type": "WIRE_MODULE", "target_module": "tl_ego_server",
         "description": "wire test", "risk": "medium", "est_lines": 15,
         "status": "draft",
         "proposed_code": "try:\n    from tool_router import <core_function>\nexcept ImportError:\n    pass\n"},
    ]
    proposer = _FakeProposer(drafts)

    with tempfile.TemporaryDirectory() as tmp:
        gov = L8Governance(tmp)
        pending = proposer.list_proposals(status="draft")
        assert len(pending) == 5, pending

        # the loop caps the batch at 5
        batch = pending[:5]
        decisions = gov.review_batch(batch)
        assert len(decisions) == len(batch), (len(decisions), len(batch))

        n_approve = n_flag = n_deny = 0
        for prop, dec in zip(batch, decisions):
            d = dec["decision"]
            if d == "approve":
                proposer.update_status(prop["id"], "applied", notes="l8: " + dec["reason"][:80])
                n_approve += 1
            elif d == "flag":
                proposer.update_status(prop["id"], "reviewed", notes="l8_flag: " + dec["reason"][:80])
                n_flag += 1
            elif d == "deny":
                proposer.update_status(prop["id"], "failed", notes="l8_deny: " + dec["reason"][:80])
                n_deny += 1

        print("batch: approve=%d flag=%d deny=%d" % (n_approve, n_flag, n_deny))
        assert (n_approve, n_flag, n_deny) == (3, 1, 1), (n_approve, n_flag, n_deny)

        # no draft may survive a completed pass over a 5-item batch
        assert proposer.list_proposals(status="draft") == [], "drafts left pending"
        # every write must carry a reason for the audit trail
        assert all(w[2] for w in proposer.writes), proposer.writes
        # terminal states are exactly the documented lifecycle
        assert {p["status"] for p in proposer.list_proposals()} == {"applied", "reviewed", "failed"}

        stats = gov.get_stats()
        assert stats["total_reviewed"] == 5, stats
        print("stats:", {k: stats[k] for k in ("total_reviewed", "approved", "flagged", "denied")})

        # L9AutonomyMonitor takes a pathlib.Path, not a str (it does
        # `self.workspace / ".lae" / "l9"`). The server passes WORKSPACE, which is
        # already a Path — L8Governance must accept either.
        from pathlib import Path
        mon = L9AutonomyMonitor(Path(tmp))
        # exact call shape tl_ego_server._run_governance_check() uses, including
        # runtime_generation_id — the 2026-10-01 TypeError came from omitting it
        r = mon.check_loop(tick=999, l8_stats=stats,
                           l7_proposals=proposer.list_proposals(), gap_snapshot={},
                           runtime_generation_id="ego-test-gen")
        st = mon.get_status()
        print("l9 status keys:", sorted(st.keys())[:8])
        assert isinstance(st, dict) and st, "L9 returned nothing"
        assert isinstance(r, dict), r
        # the tag must be recorded so the audit log can be split per boot
        state = mon._load_state()
        assert state.get("last_generation_id") == "ego-test-gen", state.get("last_generation_id")

        # the factory (no-arg, as the server calls it) and the class (workspace,
        # as the stats reader calls it) must agree on the real ledger path
        assert create_governance().log_path.endswith(
            os.path.join(".lae", "l8", "governance_log.jsonl"))
        # and L8Governance must tolerate a Path exactly as L9 does
        assert L8Governance(Path(tmp)).log_path == L8Governance(tmp).log_path
        assert L8Governance(tmp).log_path.endswith(
            os.path.join(".lae", "l8", "governance_log.jsonl"))

    print("\nOK: L7 draft -> L8 review -> status writeback -> L9 monitor, full lifecycle")


if __name__ == "__main__":
    main()
