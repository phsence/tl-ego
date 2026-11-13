"""Deterministic, evidence-driven proposal selection for the L8 Cognitive Lab.

This module only ranks bounded diagnostic hypotheses. It never imports,
executes, or deploys a proposal into the live TL-Ego process.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _unit(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def rank_mechanism_hypotheses(
    problem: dict[str, Any],
    state: dict[str, Any],
    previously_proposed: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return diagnostic hypotheses ranked by evidence."""
    previous = previously_proposed or set()
    label = str(problem.get("label", "")).lower()
    severity = _unit(problem.get("severity", 0.5))
    competition = _unit(state.get("competition", 0.5))
    concentration = _unit(state.get("concentration", 0.5))
    stagnation = max(0, int(state.get("stagnation_streak", 0) or 0))
    switches = max(0, int(state.get("type_switches", 0) or 0))
    recurring = problem.get("type") == "recurring_defect"
    _prob_type = str(problem.get("type", ""))
    window_signal = recurring and ("window" in label or "observation" in label)
    gap_signal = _prob_type == "knowledge_gap"
    window_fit = max(
        float(window_signal),
        0.65 * competition + 0.25 * min(stagnation, 3) / 3 + 0.10 * severity,
    )
    diversity_fit = min(
        1.0,
        0.45 * competition
        + 0.30 * (1.0 - concentration)
        + 0.15 * min(switches, 4) / 4
        + 0.10 * float(recurring),
    )
    probe_fit = min(
        1.0,
        0.50 * float(gap_signal)
        + 0.25 * severity
        + 0.15 * min(stagnation, 3) / 3
        + 0.10 * (1.0 - competition),
    )
    candidates = [
        {
            "kind": "bounded_observation_window",
            "hypothesis": (
                "A bounded multi-tick observation window may distinguish "
                "transient competition from persistent instability."
            ),
            "recommendation": (
                "aggregate_recent_state_before_reclassification"
            ),
            "condition": "competition >= 0.70 or stagnation >= 2",
            "fit": window_fit,
        },
        {
            "kind": "transition_diversity_probe",
            "hypothesis": (
                "Tracking type switches with concentration may reveal an "
                "oscillation pattern that a single-state classifier misses."
            ),
            "recommendation": (
                "measure_transition_diversity_before_route_selection"
            ),
            "condition": (
                "(competition >= 0.65 and concentration <= 0.55) "
                "or stagnation >= 3"
            ),
            "fit": diversity_fit,
        },
        {
            "kind": "focused_information_probe",
            "hypothesis": (
                "A small, explicitly scoped information probe may reduce "
                "uncertainty before proposing a structural repair."
            ),
            "recommendation": "collect_one_missing_signal_then_reassess",
            "condition": (
                "stagnation >= 1 and concentration >= 0.30 "
                "and competition <= 0.95"
            ),
            "fit": probe_fit,
        },
    ]
    evidence = {
        "severity": round(severity, 4),
        "competition": round(competition, 4),
        "concentration": round(concentration, 4),
        "stagnation": stagnation,
        "type_switches": switches,
        "recurring": recurring,
    }
    for candidate in candidates:
        previously_proposed = candidate["kind"] in previous
        novelty = 0.35 if previously_proposed else 1.0
        candidate["previously_proposed"] = previously_proposed
        candidate["novelty"] = round(novelty, 4)
        candidate["safety"] = 1.0
        candidate["score"] = round(
            0.60 * candidate["fit"] + 0.25 * novelty + 0.15,
            4,
        )
        candidate["evidence"] = evidence
    # A recently proposed diagnostic remains visible for audit, but cannot displace
    # an otherwise valid fresh alternative solely through a higher evidence score.
    return sorted(
        candidates,
        key=lambda item: (item["previously_proposed"], -item["score"], item["kind"]),
    )


def repeat_of_previous(path, signature: dict, tol: float = 0.05) -> bool:
    """True when `signature` repeats the last recorded proposal for a near-identical state.

    L8 synthesis is re-armed on every boot while tick/state are persisted, so
    without this gate it re-proposes the same mechanism for the same state and
    the promotion queue fills with duplicate bundles (7094 rows, 0 applied).
    """
    try:
        prev = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(prev, dict) or not prev.get("label"):
        return False
    if prev.get("label") != signature.get("label"):
        return False
    if prev.get("dominant") != signature.get("dominant"):
        return False
    for key in ("competition", "concentration"):
        try:
            if abs(float(prev.get(key, 0.0)) - float(signature.get(key, 0.0))) > tol:
                return False
        except (TypeError, ValueError):
            return False
    try:
        if abs(int(prev.get("stagnation", 0)) - int(signature.get("stagnation", 0))) > 1:
            return False
    except (TypeError, ValueError):
        return False
    return True


def record_l8_signature(path, signature: dict, skipped: bool = False) -> dict:
    """Persist the last real proposal, or bump the skip counter for a repeat."""
    p = Path(path)
    try:
        prev = json.loads(p.read_text("utf-8"))
        if not isinstance(prev, dict):
            prev = {}
    except (OSError, ValueError):
        prev = {}
    if skipped:
        prev["skipped_n"] = int(prev.get("skipped_n", 0)) + 1
        prev["last_skip_ts"] = signature.get("ts")
    else:
        keep = prev.get("skipped_n", 0)
        prev = dict(signature)
        prev["skipped_n"] = keep
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prev, ensure_ascii=False), "utf-8")
    return prev


def render_diagnostic_mechanism(
    function_name: str,
    mechanism_id: str,
    proposal_tick: int,
    problem_label: str,
    candidate: dict[str, Any],
) -> str:
    """Render one restricted, state-read-only sandbox mechanism."""
    return f'''def {function_name}(state: dict) -> dict:
    """L8 sandbox-only diagnostic; it cannot mutate live TL-Ego state."""
    competition = float(state.get("competition", 0.0))
    stagnation = int(state.get("stagnation_streak", 0))
    concentration = float(state.get("concentration", 0.0))
    condition_met = {candidate["condition"]}
    return {{
        "applied": False,
        "reason": {problem_label!r} + ": " + {candidate["hypothesis"]!r},
        "confidence": {candidate["score"]:.4f},
        "condition_met": condition_met,
        "mech_id": {mechanism_id!r},
        "proposal_tick": {proposal_tick},
        "hypothesis_kind": {candidate["kind"]!r},
        "recommendation": {candidate["recommendation"]!r},
    }}
'''


def render_contract_test(mechanism_name: str) -> str:
    """Render the bundle's test_module.py (sandbox contract test, review artifact only).

    REBUILT 2026-09-30 (source lost in the 09-30 wipe).
    Provenance: tl_ego_server.py:21759 writes this text to <sandbox>/test_module.py as part
    of the L8 promotion bundle. Judgement: the original template is unrecoverable; this one
    is deliberately import-light (plain `import module`) because the sandbox verifier runs a
    static scan that rejects os/sys/subprocess/socket/shutil/ctypes/importlib and
    eval/exec/compile/open - renderer and verifier must agree or every bundle fails.
    """
    return (
        '"""Contract test for %s (generated by l8_cognitive_lab.render_contract_test)."""\n'
        'import module\n'
        '\n'
        '\n'
        'def contract_check():\n'
        '    public = [(n, o) for n, o in vars(module).items()\n'
        '              if not n.startswith("_") and callable(o)]\n'
        '    assert public, "module exposes no public callable"\n'
        '    mech = dict(public).get("mechanism")\n'
        '    if mech is not None:\n'
        '        out = mech({})\n'
        '        assert isinstance(out, dict), "mechanism(state) must return a dict"\n'
        '    return {"callables": [n for n, _ in public]}\n'
        '\n'
        '\n'
        'if __name__ == "__main__":\n'
        '    print("%s contract OK:", contract_check())\n'
    ) % (mechanism_name, mechanism_name)


def render_integration_guide(mechanism_name: str, selected=None) -> str:
    """Render the bundle's integration.md (handoff note, never a live import).

    REBUILT 2026-09-30 (source lost in the 09-30 wipe).
    Provenance: tl_ego_server.py:21762 writes this text to <sandbox>/integration.md with
    (mechanism_name, selected) where selected is the winning candidate run dict.
    """
    import json as _json
    sel = selected if isinstance(selected, dict) else {}
    detail = _json.dumps({k: sel.get(k) for k in ("candidate", "mech_name", "proposal_path",
                                                  "hypothesis_kind", "score") if k in sel},
                         ensure_ascii=False, indent=2)
    return (
        "# Integration guide: %s\n\n"
        "Status: **review artifact** - this bundle is a handoff format, not a live import.\n\n"
        "## Contents\n"
        "- `module.py` - the selected candidate implementation (verbatim).\n"
        "- `test_module.py` - the contract test (run it from this directory: `python test_module.py`).\n"
        "- `integration.md` - this note.\n\n"
        "## Selected candidate\n```json\n%s\n```\n\n"
        "## Promote it\n"
        "1. Read `module.py` and confirm the contract in `test_module.py`.\n"
        "2. Re-run the contract test in isolation:\n"
        "   `python -c \"import test_module; print(test_module.contract_check())\"`\n"
        "3. Only a human-approved change may copy the mechanism into the live tree; this\n"
        "   artifact itself grants no authority and is never imported by the runtime.\n"
    ) % (mechanism_name, detail)
