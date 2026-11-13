"""Small, side-effect-free contract helpers for the TL-Ego S5 bridge."""


def build_ego_payload(*, self_model, state, trajectory, tick, runtime_generation_id, timestamp):
    self_model = self_model if isinstance(self_model, dict) else {}
    state = state if isinstance(state, dict) else {}
    identity = state.get("persistent_identity")
    if not isinstance(identity, dict):
        identity = {}
    surprise = state.get("self_prediction_surprise")
    if not isinstance(surprise, dict):
        surprise = {}

    model = {
        "dominant": state.get("dominant", "unknown"),
        "raw_attractor_dominant": state.get("raw_attractor_dominant", "unknown"),
        "competition": state.get("competition", 0.0),
        "concentration": state.get("concentration", 0.0),
        "stagnation_streak": state.get("stagnation_streak", 0),
        "prediction_error": state.get("prediction_error", surprise.get("surprise_score", 0.0)),
        "persistent_identity": identity,
    }
    return {
        "adapter": "ego",
        "self_model": model,
        "energy_level": self_model.get("energy_level", 0.5),
        "mood": self_model.get("mood", "unknown"),
        "focus_areas": self_model.get("focus_areas", []),
        "recent_fingerprints": self_model.get("recent_fingerprints", []),
        "self_narrative": self_model.get("self_narrative", ""),
        "raw_trajectory": list(trajectory or [])[-10:],
        "meta": {
            "domain": "ego",
            "timestamp": timestamp,
            "tick": int(tick),
            "runtime_generation_id": runtime_generation_id,
        },
    }


def extract_causal_gap(response, *, runtime_generation_id, tick):
    if not isinstance(response, dict):
        return {}
    nested = response.get("causal_lens")
    causal_lens = nested if isinstance(nested, dict) else response

    support = response.get("support_analysis")
    if not isinstance(support, dict):
        support = causal_lens.get("support_analysis")
    if not isinstance(support, dict):
        support = {}
    critical_nodes = support.get("critical_nodes")
    critical_nodes = critical_nodes if isinstance(critical_nodes, list) else []

    chains = response.get("causal_chains")
    if not isinstance(chains, list):
        chain_block = causal_lens.get("chains")
        chains = chain_block.get("chains", []) if isinstance(chain_block, dict) else []
    chains = [chain for chain in chains if isinstance(chain, dict)]
    negative = [chain for chain in chains if chain.get("polarity") == "negative"]
    worst = max(negative, key=lambda chain: float(chain.get("risk_score", 0.0) or 0.0), default=None)

    interventions = response.get("interventions")
    top_intervention = None
    if isinstance(interventions, list):
        top_intervention = interventions[0] if interventions else None
    else:
        legacy = causal_lens.get("interventions")
        if isinstance(legacy, dict):
            top_intervention = legacy.get("top_intervention")

    node = critical_nodes[0] if critical_nodes and isinstance(critical_nodes[0], dict) else None
    if node is None and worst:
        node = {
            "label": worst.get("trigger_label") or worst.get("trigger") or "?",
            "vulnerability": worst.get("risk_score", 0.0),
        }
    if not node and not top_intervention:
        return {}

    criticality = node.get("vulnerability", node.get("criticality", 0.0)) if node else 0.0
    try:
        criticality = max(0.0, min(1.0, float(criticality or 0.0)))
    except (TypeError, ValueError):
        criticality = 0.0
    return {
        "runtime_generation_id": runtime_generation_id,
        "tick": int(tick),
        "source_request_id": response.get("request_id"),
        "source_schema": response.get("schema"),
        "critical_node": node,
        "structural_vulnerability_proxy": criticality,
        "threat_count": len(negative),
        "chain_summary": (worst or {}).get("mechanism") or (worst or {}).get("final_state") or "",
        "intervention": top_intervention,
        "risk_score": response.get("risk_score", (worst or {}).get("risk_score")),
    }


def format_causal_context(gap, runtime_generation_id, tick, max_age=5):
    if not isinstance(gap, dict) or gap.get("runtime_generation_id") != runtime_generation_id:
        return ""
    try:
        age = int(tick) - int(gap.get("tick"))
    except (TypeError, ValueError):
        return ""
    if age < 0 or age > max_age:
        return ""

    node = gap.get("critical_node") or {}
    label = node.get("label") or node.get("id") or "未指定节点"
    strategy = (gap.get("intervention") or {}).get("strategy") or "未给出"
    summary = gap.get("chain_summary") or "未提供链路说明"
    return (
        "[Causal-Lens hypothesis; unverified, not a control instruction] "
        f"tick={gap.get('tick')} node={label}; "
        f"structural_vulnerability_proxy={float(gap.get('structural_vulnerability_proxy', 0.0) or 0.0):.3f} (not causal probability); "
        f"negative_chains={int(gap.get('threat_count', 0) or 0)}; "
        f"engine_risk_score={gap.get('risk_score')} (heuristic, not probability); "
        f"mechanism={summary}; suggested_intervention={strategy}. "
        "Treat as a hypothesis and check it against measured outcomes."
    )
