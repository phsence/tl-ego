#!/usr/bin/env python3
"""Credit-assignment round trip: /contemplate writes an envelope, /feedback
joins the measured outcome onto it, and an unknown decision is NOT credited.

This is the chain that was silently dead: tl_ego_server imported
get_decision_envelope / upsert_decision_envelope / record_action_outcome from
tool_router and none of them existed, so every teacher sample stayed
pending_outcome and ego never got told whether its actions worked.

Run:  python test_credit_assignment.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tool_router as tr

_DID = "test_decision_roundtrip"


def _reset():
    """Drop any leftover row so the test never passes on stale state."""
    with tr._ENVELOPE_LOCK:
        conn = tr._envelope_connect()
        try:
            conn.execute("DELETE FROM decision_envelopes WHERE decision_id=?", (_DID,))
            conn.commit()
        finally:
            conn.close()


def test_roundtrip():
    _reset()

    # 1) /contemplate: decision happens, envelope written before tools run.
    written = tr.upsert_decision_envelope(
        _DID, route="full_pipeline", state_before={"query": "build stone at 10,64,-5"}
    )
    assert written["written"] == 1, written

    # 2) L10 appends the actions it dispatched (must not clobber state_before).
    tr.upsert_decision_envelope(_DID, actions=[{"tool": "setblock", "status": "ok"}])
    mid = tr.get_decision_envelope(_DID)
    assert mid["state_before"] == {"query": "build stone at 10,64,-5"}, mid
    assert mid["actions"] == [{"tool": "setblock", "status": "ok"}], mid
    assert mid["outcome"] is None, mid

    # 3) /feedback: measured outcome joins credit onto THAT decision.
    upd = tr.record_action_outcome(
        _DID, "success", evidence="block placed, verified in world",
        judge="verified", tool="setblock", source="stage1_consequence_sensor",
    )
    assert upd["updated"] == 1, upd
    env = tr.get_decision_envelope(_DID)
    assert len(env["outcome"]) == 1, env
    got = env["outcome"][0]
    assert got["outcome"] == "success" and got["judge"] == "verified", got
    assert got["tool"] == "setblock", got

    # 4) A second measured outcome appends, never replaces the first.
    tr.record_action_outcome(_DID, "failed", evidence="re-placed wrong block",
                             judge="consequence_sensor", tool="setblock")
    env = tr.get_decision_envelope(_DID)
    assert [o["outcome"] for o in env["outcome"]] == ["success", "failed"], env

    print("test_roundtrip OK")


def test_unknown_decision_is_not_credited():
    _reset()
    upd = tr.record_action_outcome("no_such_decision_xyz", "success", judge="verified")
    assert upd["updated"] == 0, upd
    assert upd["reason"] == "unknown_decision_id", upd
    assert tr.get_decision_envelope("no_such_decision_xyz")["error"] == "not_found"
    print("test_unknown_decision_is_not_credited OK")


def test_missing_id_is_rejected():
    assert tr.upsert_decision_envelope("", route="x")["reason"] == "missing_decision_id"
    assert tr.record_action_outcome("", "success")["reason"] == "missing_decision_id"
    print("test_missing_id_is_rejected OK")


if __name__ == "__main__":
    test_roundtrip()
    test_unknown_decision_is_not_credited()
    test_missing_id_is_rejected()
    print("ALL CREDIT-ASSIGNMENT TESTS PASSED")
