"""runtime_governor.py - REBUILT 2026-09-30 (source lost in the 09-30 wipe).

Provenance: tl_ego_server.py imports build_governance_contract and calls it with
(state=, tool_name=, tool_config=, reason=, trigger_state=); its except-fallback is
{"version": "runtime-governor-v1", "error": ...} -> the return is a dict that carries
"version". Right above the call the server builds, for the same sandbox handoff:
  outcome_contract = {response_path ".lae/l5_response.json", required_fields [tick,
  action_type, outcome, expected_result, actual_result, task_id, state_before, state_after],
  prediction_error_rule "do NOT write prediction_error; Stage1 computes it", valid_outcomes
  ["success","partial","failed","timeout","blocked"]}
  delayed_verification = {window_ticks [3,6], success/failure/feedback rules}
The governor is the action side of that contract (comment: "Runtime Governor
(Fable-5 inspired): explicit action contract").
Judgement (not measured): the exact original field set is unrecoverable - this dict is
rebuilt from those neighbours and the state keys the caller actually passes.
"""

VALID_OUTCOMES = ["success", "partial", "failed", "timeout", "blocked"]


def build_governance_contract(state=None, tool_name=None, tool_config=None,
                              reason=None, trigger_state=None):
    st = state if isinstance(state, dict) else {}
    cfg = tool_config if isinstance(tool_config, dict) else {}
    trigger = trigger_state if isinstance(trigger_state, dict) else {}
    obs = {
        "tick": st.get("tick"),
        "stagnation_streak": st.get("stagnation_streak"),
        "competition": st.get("competition"),
        "concentration": st.get("concentration"),
        "dominant": st.get("last_dominant"),
        "prediction_error": st.get("last_prediction_error"),
    }
    return {
        "version": "runtime-governor-v1",
        "tool": tool_name,
        "authorized": bool(tool_name),
        "reason": (reason or "")[:400],
        "observed_state": obs,
        "trigger": {k: trigger.get(k) for k in ("reason", "domain", "tick") if k in trigger},
        "tool_config": {k: cfg.get(k) for k in ("risk", "timeout_s", "mode", "sandbox") if k in cfg},
        "expected_result": "tool completes inside the L5 sandbox and reports a measured outcome",
        "valid_outcomes": list(VALID_OUTCOMES),
        "response_path": ".lae/l5_response.json",
        "required_fields": ["tick", "action_type", "outcome", "expected_result", "actual_result",
                            "task_id", "state_before", "state_after"],
        "prediction_error_rule": "do not write prediction_error; Stage1 computes it from "
                                 "expected_result vs actual_result (copy jepa_expected_result)",
        "verification": {"window_ticks": [3, 6],
                         "success": "raw_dominant_history diversity rises, or stagnation_streak < 3, "
                                    "or concentration > 0.12 within 6 ticks",
                         "failure": "no measurable behavioural improvement after 6 ticks",
                         "feedback": "post .l5_feedback.json with task_id=ego_action_tick_<tick>"},
        "authority": {"live_mutation": bool(st.get("l5_live_mutation_authority")),
                      "proposal_only": bool(st.get("l5_proposal_only_legacy"))},
    }


if __name__ == "__main__":
    c = build_governance_contract(state={"tick": 12, "stagnation_streak": 9, "last_dominant": "doubt",
                                         "l5_live_mutation_authority": True},
                                  tool_name="run_quantum_experiment",
                                  tool_config={"risk": "low", "timeout_s": 30},
                                  reason="stagnation", trigger_state={"domain": "physics", "tick": 12})
    assert c["version"] == "runtime-governor-v1" and c["authorized"] is True
    assert c["valid_outcomes"] == VALID_OUTCOMES and c["observed_state"]["stagnation_streak"] == 9
    assert c["authority"]["live_mutation"] is True and c["tool_config"]["risk"] == "low"
    assert c["tool_config"]["timeout_s"] == 30 and "mode" not in c["tool_config"]
    bad = build_governance_contract()
    assert bad["authorized"] is False and bad["observed_state"]["tick"] is None
    huge = build_governance_contract(reason="x" * 900)
    assert len(huge["reason"]) == 400
    assert build_governance_contract(state="junk", tool_config=None, trigger_state=3)["authorized"] is False
    print("runtime_governor selfcheck OK")
