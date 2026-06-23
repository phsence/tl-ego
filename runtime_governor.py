#!/usr/bin/env python3
"""Runtime Governor for TL-Ego L5 autonomy impulses.

This module is intentionally deterministic and side-effect free. It does not
decide whether TL-Ego should externalize an impulse; it adds an explicit action
contract to impulses that have already passed the existing gates.
"""


def build_governance_contract(state, tool_name, tool_config, reason, trigger_state):
    """Return an auditable governance contract for one L5 externalization."""
    tick = int(state.get("tick", 0) or 0)
    stagnation = int(state.get("stagnation_streak", 0) or 0)
    competition = float(state.get("competition", 0.5) or 0.5)
    concentration = float(state.get("concentration", 0.5) or 0.5)
    goal_type = tool_config.get("goal_type", "unknown")
    domain = tool_config.get("domain_hint", "unknown")

    risk_level = "low"
    if goal_type in {"default", "curiosity_explore", "integrate_high_comp"}:
        risk_level = "medium"
    if stagnation >= 12 or competition >= 0.93:
        risk_level = "medium"

    return {
        "version": "runtime-governor-v1",
        "tick": tick,
        "tool_name": tool_name,
        "goal_type": goal_type,
        "domain": domain,
        "risk_level": risk_level,
        "when_to_act": [
            "existing L5 externalization gates have already passed",
            "action is represented as an auditable bridge file",
            "first action is observation unless explicit evidence justifies escalation",
        ],
        "when_not_to_act": [
            "service health is unknown or degraded",
            "required outcome verification cannot be recorded",
            "action would require irreversible external side effects",
        ],
        "required_verification": [
            "write .lae/l5_response.json with expected_result and actual_result",
            "include explicit prediction_error when known, otherwise include enough expected_result and actual_result for semantic calculation",
            "never use prediction_error=0.5 as a placeholder; omit prediction_error when it is unknown",
            "record prediction_error_source after outcome consumption",
            "check dominant_type or stagnation change within delayed verification window",
        ],
        "failure_response": [
            "record outcome as partial, failed, timeout, or blocked",
            "do not immediately repeat the same externalization path",
            "feed the failure back through Stage 1 consequence sensor",
        ],
        "expected_result": f"Plugin spawns subagent for {tool_name} and writes auditable outcome",
        "actual_result": None,
        "blast_radius": "local_files_only_by_default",
        "reason": reason,
        "state_snapshot": {
            "dominant_type": trigger_state.get("dominant_type"),
            "competition": round(competition, 4),
            "concentration": round(concentration, 4),
            "stagnation_streak": stagnation,
        },
    }
