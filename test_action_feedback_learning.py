#!/usr/bin/env python3
"""record_action_feedback: a measured outcome must become a durable verdict.

Rebuilt 2026-09-30 — tl_ego_server.py:2201 called a method that never existed,
so credit stopped at the envelope and nothing ever changed ego's behaviour.

Scope note: `eligibility` is write-only in this codebase (nothing reads traces
back), so the behaviour change that matters is the per-action verdict ledger.
An earlier version of this test asserted per-trace credit and passed only
because the whole 16000-edge table belonged to one tick — blanket boost, not
credit assignment. Don't re-add that assertion without a real trace consumer.

Run:  python test_action_feedback_learning.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from predictive_plasticity import PredictivePlasticity


def _pp():
    # No state_path -> the sidecar stays in memory and never touches real files.
    return PredictivePlasticity(state_path=None)


def test_success_and_failure_are_recorded():
    pp = _pp()
    ok = pp.record_action_feedback("setblock", "success", decision_id="d1",
                                  evidence="block placed")
    assert ok["updated"] == 1 and ok["outcome"] == "success", ok
    assert ok["action"] == "setblock" and ok["decision_id"] == "d1", ok

    bad = pp.record_action_feedback("setblock", "failed", decision_id="d2",
                                    evidence="wrong block")
    assert bad["updated"] == 1 and bad["outcome"] == "failed", bad

    assert dict(pp.action_outcomes["setblock"]) == {"success": 1, "failed": 1}, \
        pp.action_outcomes
    print("test_success_and_failure_are_recorded OK")


def test_outcome_vocabulary_is_mapped():
    """Subagent vocab is broad; both polarity sets must land on one of two verdicts."""
    pp = _pp()
    for word in ("completed", "verified", "landed", "confirmed", "correct"):
        pp.record_action_feedback(f"t_{word}", word)
    for word in ("timeout", "blocked", "crash", "abort", "error"):
        pp.record_action_feedback(f"t_{word}", word)
    assert dict(pp.action_outcomes["t_completed"]) == {"success": 1}, pp.action_outcomes
    assert dict(pp.action_outcomes["t_timeout"]) == {"failed": 1}, pp.action_outcomes
    print("test_outcome_vocabulary_is_mapped OK")


def test_unclassified_outcome_is_refused():
    """Unmeasured feedback must not create a verdict."""
    pp = _pp()
    r = pp.record_action_feedback("setblock", "who_knows", decision_id="d3")
    assert r["updated"] == 0 and r["reason"] == "unclassified_outcome", r
    assert "setblock" not in pp.action_outcomes, pp.action_outcomes
    assert pp.action_feedback_log == {}, pp.action_feedback_log
    print("test_unclassified_outcome_is_refused OK")


def test_action_name_is_normalized():
    pp = _pp()
    pp.record_action_feedback("  SetBlock ", "success")
    assert "setblock" in pp.action_outcomes, pp.action_outcomes
    pp.record_action_feedback("", "success")
    assert dict(pp.action_outcomes["unknown"]) == {"success": 1}, pp.action_outcomes
    print("test_action_name_is_normalized OK")


def test_verdicts_survive_restart():
    """Credit that dies on restart is not credit; ego would relearn nothing."""
    with tempfile.TemporaryDirectory() as td:
        state = str(Path(td) / "pred.json")
        pp = PredictivePlasticity(state_path=state)
        pp.record_action_feedback("setblock", "success", decision_id="d4")
        pp.record_action_feedback("attack", "failed", decision_id="d5")

        reloaded = PredictivePlasticity(state_path=state)
        assert dict(reloaded.action_outcomes["setblock"]) == {"success": 1}, \
            reloaded.action_outcomes
        assert dict(reloaded.action_outcomes["attack"]) == {"failed": 1}, \
            reloaded.action_outcomes
    print("test_verdicts_survive_restart OK")


def test_status_exposes_credit():
    pp = _pp()
    pp.record_action_feedback("mine", "success", decision_id="d6")
    st = pp.status()
    assert st["action_outcomes"]["mine"] == {"success": 1}, st
    assert st["action_feedback_log"]["decision_id"] == "d6", st
    # A refused credit must not leave a log claiming it happened.
    pp.record_action_feedback("mine", "who_knows", decision_id="d7")
    assert pp.status()["action_feedback_log"]["decision_id"] == "d6", \
        "refused credit overwrote the log"
    # A real second credit legitimately becomes the latest log entry.
    pp.record_action_feedback("mine", "failed", decision_id="d8")
    assert pp.status()["action_feedback_log"]["decision_id"] == "d8"
    assert pp.status()["action_outcomes"]["mine"] == {"success": 1, "failed": 1}
    print("test_status_exposes_credit OK")


if __name__ == "__main__":
    test_success_and_failure_are_recorded()
    test_outcome_vocabulary_is_mapped()
    test_unclassified_outcome_is_refused()
    test_action_name_is_normalized()
    test_verdicts_survive_restart()
    test_status_exposes_credit()
    print("ALL ACTION-FEEDBACK TESTS PASSED")
