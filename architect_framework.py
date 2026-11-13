"""architect_framework.py — REBUILT 2026-09-30 (original source lost in the 09-30 wipe).

Provenance (no invented behaviour):
  * return schema = the last real artifact written by this module,
    `.lae/architect_blueprint_latest.json` (2026-09-28 13:44, status=proposed);
  * the two call sites in tl_ego_server.py:
      line 423  from architect_framework import ARCHITECT_TYPE, build_blueprint
      line 703  TYPE_NAMES = [... 18 names ..., ARCHITECT_TYPE]   -> 19th type
      line 21303 build_blueprint(state, existing_modules=[...])  -> dict, written to
                .lae/architect_blueprint_latest.json, status must be "proposed"
                with a non-empty "modules" list for the promotion gate to fire.

Only the trio of modules/constraints/acceptance the artifact carried is emitted;
the capability gap is derived from the live state dict handed in by the caller.
"""

from __future__ import annotations

import hashlib
import json
import time

ARCHITECT_TYPE = "architect"          # 19th neuron type (TYPE_NAMES + this = 19 observed live)
BENCHMARK_SCHEMA = "ego_capability_benchmark.v1"
BASELINE_REPEATED_RUNS = 3

_MODULES = [
    {
        "name": "architect_model",
        "responsibility": "Represent goals, module boundaries, contracts, and capability gaps.",
        "inputs": ["runtime_snapshot", "memory_gaps", "existing_module_inventory"],
        "outputs": ["architecture_blueprint"],
    },
    {
        "name": "architect_sandbox",
        "responsibility": "Evaluate candidate blueprints without mutating live topology or code.",
        "inputs": ["architecture_blueprint", "shadow_arena", "causal_evidence"],
        "outputs": ["verification_report"],
    },
    {
        "name": "architect_promotion_gate",
        "responsibility": "Require measurable capability gain and explicit human-reviewed promotion.",
        "inputs": ["verification_report", "baseline_capability_report", "candidate_capability_report"],
        "outputs": ["promotion_artifact"],
    },
]

_INTERFACES = [
    {"from": "architect_model", "to": "architect_sandbox", "contract": "blueprint_v1"},
    {"from": "architect_sandbox", "to": "architect_promotion_gate", "contract": "verification_v1"},
]

_CONSTRAINTS = [
    "No live neural-grid writes during design or sandbox evaluation.",
    "No code modification is implied by a blueprint.",
    "Promotion requires explicit approval and independently reproducible tests.",
]

_ACCEPTANCE = [
    "Blueprint has at least one explicit capability gap.",
    "Every proposed module has bounded inputs and outputs.",
    "A shadow verification report exists before promotion.",
    "Baseline and candidate each contain at least three repeated benchmark runs.",
    "Promotion requires measurable capability gain without material regression.",
]

# gap domains in priority order; first signal that is hot owns the blueprint
_DOMAIN_ORDER = (
    ("stagnation_streak", "trajectory", 10),
    ("type_switches", "type_transition", 1),
    ("high_value_gaps", "capability_gap", 1),
    ("competition", "integrator", 0.5),
)


def _capability_gap(state):
    # read only what the caller's state actually carries; missing keys stay 0
    # (the surviving 09-28 artifact had type_switches=0 / high_value_gaps=0)
    signals = {
        "competition": round(float(state.get("competition", 0.0) or 0.0), 4),
        "stagnation_streak": int(state.get("stagnation_streak", 0) or 0),
        "type_switches": int(state.get("type_switches", 0) or 0),
        "high_value_gaps": int(state.get("high_value_gaps", 0) or 0),
    }
    domains, summary = [], "Integrate cross-module evidence into a bounded candidate capability design."
    for key, domain, thresh in _DOMAIN_ORDER:
        if float(signals.get(key, 0) or 0) >= thresh:
            domains.append(domain)
    return {"domains": domains or ["trajectory"], "signals": signals, "summary": summary}


def build_blueprint(state, existing_modules=None):
    """Return one shadow-only blueprint dict (never touches live topology or code)."""
    state = state if isinstance(state, dict) else {}
    tick = int(state.get("tick", 0) or 0)
    now = time.time()
    bid = "arch-" + hashlib.sha1(f"{tick}:{now:.6f}".encode()).hexdigest()[:10]
    existing = sorted({str(m) for m in (existing_modules or [])})
    return {
        "schema": "architect_blueprint.v1",
        "blueprint_id": bid,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1e6):06d}+08:00",
        "status": "proposed",
        "benchmark_schema": BENCHMARK_SCHEMA,
        "baseline_repeated_runs": BASELINE_REPEATED_RUNS,
        "source_tick": tick,
        "capability_gap": _capability_gap(state),
        "existing_modules": existing,
        "modules": _MODULES,
        "interfaces": _INTERFACES,
        "constraints": _CONSTRAINTS,
        "acceptance": _ACCEPTANCE,
    }


if __name__ == "__main__":  # self-check: schema matches the surviving artifact's keys
    want = {"schema", "blueprint_id", "created_at", "status", "benchmark_schema", "baseline_repeated_runs",
            "source_tick", "capability_gap", "existing_modules", "modules", "interfaces", "constraints", "acceptance"}
    got = build_blueprint({"tick": 123, "competition": 0.9, "stagnation_streak": 0}, ["memory_bus"])
    assert set(got) == want, set(got) ^ want
    assert got["status"] == "proposed" and got["modules"] and ARCHITECT_TYPE == "architect"
    json.dumps(got)
    print("architect_framework self-check OK")
