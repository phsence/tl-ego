"""fitness_contract.py - REBUILT 2026-09-30 (source lost in the 09-30 wipe).

Provenance: imported by self_fork_arena/selection_gate.pyc and
self_fork_arena/shadow_arena_with_judge.pyc (both restored as 312 bytecode); the
disassembly shows calls contract_fitness(<local fitness_contract>) and
apply_safety_hard_constraint(<vector>), one positional arg each.
Spec = selection_gate's own docstring (survived inside the pyc): internal
diagnostics (concentration / surprise / type dynamics) must NOT promote; only
external_task_value from environment results may, and fitness_contract decides it.
Judgement (not measured): original field names / return shape are unrecoverable
(no other call sites). Duck-typed and tolerant by design.
"""

MIN_EXTERNAL_VALUE = 0.50


def _pick(d, *keys):
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
    return None


def contract_fitness(contract):
    """-> {"promote": bool, "fitness": float, "external_task_value": float|None, "reason": str}"""
    try:
        ext = _pick(contract, "external_task_value", "external_value", "env_value")
        diag = _pick(contract, "internal_diagnostics", "diagnostics", "score")
        if ext is None:
            return {"promote": False, "fitness": 0.0, "external_task_value": None,
                    "internal_diagnostics": diag,
                    "reason": "no environment result: internal diagnostics alone cannot promote"}
        ext = float(ext)
        promote = ext >= MIN_EXTERNAL_VALUE
        return {"promote": promote, "fitness": round(ext, 4), "external_task_value": ext,
                "internal_diagnostics": diag,
                "reason": "environment result %s threshold %.2f" % ("clears" if promote else "below", MIN_EXTERNAL_VALUE)}
    except Exception as e:  # never raise into the arena path
        return {"promote": False, "fitness": 0.0, "external_task_value": None,
                "reason": "contract error: %s" % e}


def apply_safety_hard_constraint(vector):
    """Drop candidates that break a hard safety constraint. Hard means hard: if all
    candidates are unsafe the result is empty (caller decides what to do), not a quiet
    fallback to an unsafe pick. Non-list input is returned unchanged."""
    if not isinstance(vector, (list, tuple)):
        return vector
    safe = []
    for c in vector:
        d = c if isinstance(c, dict) else {}
        if d.get("violates_safety") or d.get("unsafe") or str(d.get("risk", "")).lower() in ("high", "critical"):
            continue
        safe.append(c)
    return safe


if __name__ == "__main__":
    r = contract_fitness({"internal_diagnostics": 0.99, "score": 0.99})
    assert r["promote"] is False and r["external_task_value"] is None, r
    r = contract_fitness({"external_task_value": 0.8, "internal_diagnostics": 0.1})
    assert r["promote"] is True and r["fitness"] == 0.8, r
    r = contract_fitness({"external_task_value": 0.2})
    assert r["promote"] is False, r
    assert contract_fitness(None)["promote"] is False
    assert contract_fitness({"external_task_value": "nan-ish"})["promote"] is False
    v = [{"name": "a"}, {"name": "b", "risk": "high"}, {"name": "c", "unsafe": True}]
    assert [x["name"] for x in apply_safety_hard_constraint(v)] == ["a"]
    assert apply_safety_hard_constraint([{"risk": "high"}]) == []
    assert apply_safety_hard_constraint("nope") == "nope"
    assert apply_safety_hard_constraint(None) is None
    print("fitness_contract selfcheck OK")
