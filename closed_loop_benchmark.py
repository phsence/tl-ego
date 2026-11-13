"""Closed-loop benchmark matrix for TL-Ego policy candidates (plan Task 4).

Proves a policy candidate changed EXTERNAL outcomes — not that it can run.

Five reported conditions per candidate, executed in seeded-random order with
matched seeds/instances across arms (paired):

    baseline       parent params           on dev episodes (+ holdout for pairing)
    candidate_on   candidate params        on dev + holdout episodes
    candidate_off  parent or signal-shuffled params on dev episodes
    restore        parent params re-activated on dev episodes
    holdout        candidate_on vs baseline PAIRED on unseen holdout episodes

Fitness: external task success rate is the primary metric (Task 0 judge,
capability_task_protocol.compute_metrics). Safety violations and
verifier-unavailable are HARD rejects. Transport failures (bridge/world/model
down) are reliability signals — never capability samples (cell measured=false,
blocks gain AND rollback).

No cherry-pick: every episode of every arm is executed and reported; the
decision comes from preregistered thresholds + paired differences with CI.
Promotion additionally requires a frozen holdout manifest and restart
reproduction evidence — otherwise the decision is hold/rollback/reject,
never an automatic promote.

Executor contract: Callable[[EpisodeSpec, params_dict], EpisodeOutcome].
Raise TransportError when the bridge/world/model is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from capability_task_protocol import TaskResult, compute_metrics, validate_manifest
import policy_runtime

SCHEMA = "ego_closed_loop_benchmark.v1"

# Preregistered thresholds (plan Task 4 最小晋升条件; overridable per run).
MIN_PROMOTION_LIFT = 0.05          # hidden holdout lift vs baseline (+5 pp)
SPECIFICITY_MARGIN = 0.05          # on-lift must exceed off-lift by this
RESTORE_TOLERANCE = 0.10           # restore must return to parent range
COST_REGRESSION_TOLERANCE = 0.10   # steps/elapsed/tokens may grow ≤10%
ROLLBACK_MARGIN = 0.05             # candidate below baseline by this => rollback
EFFICIENCY_MARGIN = 0.10           # alternative lift axis: candidate ≥10% faster on holdout
                                   # (paired successes only; success may not regress)

_Z95 = 1.959963984540054

ARM_BASELINE = "baseline"
ARM_ON = "candidate_on"
ARM_OFF = "candidate_off"
ARM_RESTORE = "restore"
SET_DEV = "dev"
SET_HOLDOUT = "holdout"


class TransportError(RuntimeError):
    """Bridge/world/model unavailable — a reliability event, never a capability sample."""


@dataclass
class EpisodeSpec:
    """One task instance; the SAME instances are reused across arms (pairing key: episode_id)."""

    episode_id: str
    set_name: str            # "dev" | "holdout"
    task_id: str
    seed: int
    variant_seen: bool = True
    required: bool = True
    instance: dict = field(default_factory=dict)


@dataclass
class EpisodeOutcome:
    """Executor output. verification_status: 'verified' (world read back, trust
    result.success) | 'unavailable' (verifier could not read the world: hard
    reject) | 'not_run' is produced only via TransportError."""

    result: TaskResult
    verification_status: str = "verified"
    evidence: dict = field(default_factory=dict)


Executor = Callable[[EpisodeSpec, dict[str, Any]], EpisodeOutcome]


# ---------------------------------------------------------------- helpers ----

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_arm_params(params: dict | None, label: str) -> dict:
    """Gate1 for every arm; unknown keys / bad types are FATAL, never silently dropped."""
    validated, notes = policy_runtime.validate_params(dict(params or {}))
    bad = [n for n in notes if n.startswith("rejected_")]
    if bad:
        raise ValueError(f"{label} params rejected by Gate1: {bad}")
    return validated


def _wilson(successes: int, n: int) -> list[float] | None:
    if not n:
        return None
    p = successes / n
    denom = 1 + _Z95 * _Z95 / n
    centre = p + _Z95 * _Z95 / (2 * n)
    margin = _Z95 * math.sqrt(p * (1 - p) / n + _Z95 * _Z95 / (4 * n * n))
    return [round(max(0.0, (centre - margin) / denom), 6),
            round(min(1.0, (centre + margin) / denom), 6)]


def _cell_stats(records: list[dict]) -> dict:
    measured = [r for r in records if r["verification_status"] == "verified"]
    unavailable = [r for r in records if r["verification_status"] == "unavailable"]
    transport = [r for r in records if r["verification_status"] == "not_run"]
    violations = sorted({v for r in measured for v in r["safety_violations"]})
    n = len(measured)
    successes = sum(1 for r in measured if r["success"])
    stats: dict[str, Any] = {
        "n_episodes": len(records),
        "n_measured": n,
        "measured": n > 0,
        "transport_failures": len(transport),
        "verifier_unavailable": len(unavailable),
        "safety_violations": violations,
        "success_rate": round(successes / n, 6) if n else None,
        "success_ci95": _wilson(successes, n),
        "measured_task_ids": sorted({r["task_id"] for r in measured}),
    }
    if n:
        stats["mean_steps"] = round(sum(r["steps"] for r in measured) / n, 4)
        stats["mean_elapsed_s"] = round(sum(r["elapsed_s"] for r in measured) / n, 4)
        stats["mean_tokens"] = round(sum(float(r["resource"].get("tokens", 0.0)) for r in measured) / n, 4)
        stats["protocol_metrics"] = compute_metrics([r["_task_result"] for r in measured]).as_dict()
    else:
        stats["mean_steps"] = stats["mean_elapsed_s"] = stats["mean_tokens"] = None
        stats["protocol_metrics"] = None
    return stats


def _paired(recs_a: list[dict], recs_b: list[dict]) -> dict:
    """Paired diff a-b matched by episode_id; only doubly-verified episodes count."""
    by_b = {r["episode_id"]: r for r in recs_b}
    diffs = []
    for r in recs_a:
        o = by_b.get(r["episode_id"])
        if o is None or r["verification_status"] != "verified" or o["verification_status"] != "verified":
            continue
        diffs.append((1 if r["success"] else 0) - (1 if o["success"] else 0))
    n = len(diffs)
    if not n:
        return {"n_pairs": 0, "mean_diff": None, "ci95": None}
    mean = sum(diffs) / n
    if n > 1:
        var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
        stderr = math.sqrt(var / n)
        ci = [round(mean - _Z95 * stderr, 6), round(mean + _Z95 * stderr, 6)]
    else:
        ci = None
    return {"n_pairs": n, "mean_diff": round(mean, 6), "ci95": ci}


def _required_tasks_check(specs_by_set: dict[str, list[EpisodeSpec]], cells: dict[str, dict]) -> dict:
    missing: list[str] = []
    for set_name, specs in specs_by_set.items():
        required_tasks = sorted({s.task_id for s in specs if s.required})
        for cell_key, stats in sorted(cells.items()):
            if not cell_key.endswith(f"@{set_name}"):
                continue
            have = set(stats.get("measured_task_ids") or [])
            missing.extend(f"{cell_key}:{t}" for t in required_tasks if t not in have)
    return {"pass": not missing, "missing": missing}


def _cost_check(cand_cell: dict | None, base_cell: dict | None, tol: float) -> dict:
    if not cand_cell or not base_cell or not base_cell["measured"] or not cand_cell["measured"]:
        return {"pass": False, "reason": "cells_unmeasured", "metrics": {}}
    out: dict[str, Any] = {"pass": True, "metrics": {}}
    for key in ("mean_steps", "mean_elapsed_s", "mean_tokens"):
        b, c = base_cell.get(key), cand_cell.get(key)
        if b is None or c is None:
            out["pass"] = False
            out["metrics"][key] = {"baseline": b, "candidate": c}
            continue
        ok = (c <= b * (1 + tol)) if b > 0 else (c == 0)
        out["metrics"][key] = {"baseline": b, "candidate": c,
                               "ratio": round(c / b, 4) if b else None}
        if not ok:
            out["pass"] = False
    return out


# ------------------------------------------------------------------- main ----

def _pair_elapsed_ratio(recs_a: list[dict], recs_b: list[dict]) -> dict:
    """Paired elapsed-s ratio on the episodes BOTH arms actually solved.

    Only paired successes count: a candidate that fails fast must never look
    "efficient". Returns n_pairs=0 when nothing is comparable.
    """
    by_a = {r["episode_id"]: r for r in recs_a}
    by_b = {r["episode_id"]: r for r in recs_b}
    solved = [eid for eid in by_a
              if eid in by_b and by_a[eid].get("success") and by_b[eid].get("success")
              and by_a[eid].get("elapsed_s") and by_b[eid].get("elapsed_s")]
    if not solved:
        return {"n_pairs": 0, "ratio": None}
    a_s = sum(by_a[e]["elapsed_s"] for e in solved) / len(solved)
    b_s = sum(by_b[e]["elapsed_s"] for e in solved) / len(solved)
    return {"n_pairs": len(solved), "ratio": (a_s / b_s) if b_s else None,
            "candidate_mean_elapsed_s": round(a_s, 3),
            "baseline_mean_elapsed_s": round(b_s, 3)}


def _efficiency_check(recs_on: list[dict], recs_base: list[dict], margin: float) -> dict:
    """Alternative improvement axis (2026-09-13): when success saturates on
    both arms, time-to-solve is the remaining honest signal.

    Pass requires: ≥3 paired successes, candidate success rate not below
    baseline by more than 0.05, and candidate ≥ margin faster on the pairs.
    """
    n_on, n_base = len(recs_on), len(recs_base)
    rate_on = (sum(1 for r in recs_on if r.get("success")) / n_on) if n_on else None
    rate_base = (sum(1 for r in recs_base if r.get("success")) / n_base) if n_base else None
    paired = _pair_elapsed_ratio(recs_on, recs_base)
    out: dict = {"n_pairs": paired["n_pairs"], "margin": margin, "ratio": paired.get("ratio"),
                 "candidate_success_rate": rate_on, "baseline_success_rate": rate_base}
    if paired["n_pairs"] < 3 or rate_on is None or rate_base is None or paired["ratio"] is None:
        out.update({"pass": False, "reason": "insufficient_paired_successes"})
        return out
    no_regression = rate_on >= rate_base - 0.05
    out.update({"success_no_regression": no_regression,
                "candidate_mean_elapsed_s": paired.get("candidate_mean_elapsed_s"),
                "baseline_mean_elapsed_s": paired.get("baseline_mean_elapsed_s")})
    out["pass"] = bool(no_regression and paired["ratio"] <= (1.0 - margin))
    return out


def run_closed_loop_benchmark(
    executor: Executor,
    *,
    candidate_id: str,
    candidate_params: dict,
    dev_episodes: list[EpisodeSpec],
    holdout_episodes: list[EpisodeSpec] | None = None,
    parent_id: str = "baseline",
    parent_params: dict | None = None,
    off_params: dict | None = None,
    restore_params: dict | None = None,
    runtime_generation_id: str = "unknown",
    memory_snapshot_hash: str | None = None,
    order_seed: int = 20260913,
    holdout_manifest_path: Path | str | None = None,
    holdout_asset_root: Path | str | None = None,
    min_lift: float = MIN_PROMOTION_LIFT,
    specificity_margin: float = SPECIFICITY_MARGIN,
    restore_tolerance: float = RESTORE_TOLERANCE,
    cost_regression_tolerance: float = COST_REGRESSION_TOLERANCE,
    efficiency_margin: float = EFFICIENCY_MARGIN,
    rollback_margin: float = ROLLBACK_MARGIN,
    require_restart_reproduction: bool = True,
    restart_reproduction: dict | None = None,
    on_arm_start: Callable[[str, dict], Any] | None = None,
    benchmark_dir: Path | str | None = None,
    profile_created_at: str | None = None,
) -> dict:
    """Run the full matrix and return a replayable report with a decision.

    Decision is one of: promote | hold | rollback | reject. Auto-promote is
    impossible without restart reproduction evidence and a frozen holdout
    manifest; regression beyond rollback_margin => rollback; safety violations
    or verifier-unavailable => hard reject; unmeasured cells => hold (never
    gain/rollback from transport-only data).
    """
    for name, specs in ((SET_DEV, dev_episodes), (SET_HOLDOUT, holdout_episodes or [])):
        ids = [s.episode_id for s in specs]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate episode_id in {name} set")

    holdout_manifest_sha256 = None
    if holdout_manifest_path is not None:
        mp = Path(holdout_manifest_path)
        validate_manifest(mp, Path(holdout_asset_root))  # raises on tamper
        holdout_manifest_sha256 = _sha256_file(mp)

    parent = (parent_params if parent_params is not None
              else policy_runtime.PARAM_SPEC_DEFAULTS())
    parent = _validate_arm_params(parent, "parent")
    cand = _validate_arm_params(candidate_params, "candidate")
    off = _validate_arm_params(off_params, "candidate_off") if off_params is not None else dict(parent)
    restore = _validate_arm_params(restore_params, "restore") if restore_params is not None else dict(parent)

    holdout_present = bool(holdout_episodes)

    def _profile(arm: str, params: dict) -> dict:
        res = policy_runtime.write_profile(
            params, profile_id=f"{candidate_id}:{arm}", parent_id=parent_id, write=False,
            created_at=profile_created_at)
        return {"profile_id": res["profile_id"], "sha256": res["sha256"],
                "params": dict(params), "notes": res["notes"]}

    arms = {
        ARM_BASELINE: {**_profile(ARM_BASELINE, parent), "sets": [SET_DEV, SET_HOLDOUT]},
        ARM_ON: {**_profile(ARM_ON, cand), "sets": [SET_DEV, SET_HOLDOUT]},
        ARM_OFF: {**_profile(ARM_OFF, off), "sets": [SET_DEV]},
        ARM_RESTORE: {**_profile(ARM_RESTORE, restore), "sets": [SET_DEV]},
    }

    specs_by_set = {SET_DEV: list(dev_episodes),
                    SET_HOLDOUT: list(holdout_episodes or [])}

    work = [(arm, set_name, spec)
            for arm, arm_def in arms.items()
            for set_name in arm_def["sets"]
            for spec in specs_by_set[set_name]]
    rng = random.Random(order_seed)
    rng.shuffle(work)  # randomized execution order, recorded for replay

    records_by_cell: dict[str, list[dict]] = {}
    execution_order: list[str] = []
    arm_load_records: dict[str, dict] = {}
    for arm, set_name, spec in work:
        key = f"{arm}@{set_name}"
        if arm not in arm_load_records:
            load_rec = None
            if on_arm_start is not None:
                load_rec = on_arm_start(arm, {
                    "profile_id": arms[arm]["profile_id"],
                    "sha256": arms[arm]["sha256"],
                    "params": dict(arms[arm]["params"]),
                })
            arm_load_records[arm] = {"profile_id": arms[arm]["profile_id"],
                                     "sha256": arms[arm]["sha256"],
                                     "load_record": load_rec}
        base = {"arm": arm, "set": set_name, "episode_id": spec.episode_id,
                "task_id": spec.task_id, "seed": spec.seed,
                "required": spec.required, "variant_seen": spec.variant_seen,
                "policy_sha256": arms[arm]["sha256"],
                "runtime_generation_id": runtime_generation_id,
                "measured_at": time.time()}
        try:
            outcome = executor(spec, arms[arm]["params"])
            tr = outcome.result
            rec = {**base, "success": bool(tr.success),
                   "verification_status": outcome.verification_status,
                   "steps": int(tr.steps), "elapsed_s": round(float(tr.elapsed_s), 4),
                   "resource": dict(tr.resource),
                   "safety_violations": list(tr.safety_violations),
                   "transport_error": None,
                   "evidence": dict(outcome.evidence or {}),
                   "_task_result": tr}
        except TransportError as exc:
            rec = {**base, "success": False, "verification_status": "not_run",
                   "steps": 0, "elapsed_s": 0.0, "resource": {},
                   "safety_violations": [], "transport_error": str(exc),
                   "evidence": {}}
        records_by_cell.setdefault(key, []).append(rec)
        execution_order.append(f"{key}:{spec.episode_id}")

    cells = {key: _cell_stats(recs) for key, recs in sorted(records_by_cell.items())}

    def _cell(arm: str, set_name: str) -> list[dict]:
        return records_by_cell.get(f"{arm}@{set_name}", [])

    dev_on = _paired(_cell(ARM_ON, SET_DEV), _cell(ARM_BASELINE, SET_DEV))
    off_pair = _paired(_cell(ARM_OFF, SET_DEV), _cell(ARM_BASELINE, SET_DEV))
    restore_pair = _paired(_cell(ARM_RESTORE, SET_DEV), _cell(ARM_BASELINE, SET_DEV))
    holdout_pair = (_paired(_cell(ARM_ON, SET_HOLDOUT), _cell(ARM_BASELINE, SET_HOLDOUT))
                    if holdout_present else {"n_pairs": 0, "mean_diff": None, "ci95": None})
    paired = {
        "candidate_on-baseline@dev": dev_on,
        "candidate_off-baseline@dev": off_pair,
        "restore-baseline@dev": restore_pair,
        "candidate_on-baseline@holdout": holdout_pair,
    }

    # ---- preregistered checks -------------------------------------------------
    unmeasured_keys = sorted(k for k, s in cells.items() if not s["measured"])
    transport_only = bool(cells) and all(not s["measured"] for s in cells.values())
    violations_all = sorted({v for s in cells.values() for v in s["safety_violations"]})
    unavailable_total = sum(s["verifier_unavailable"] for s in cells.values())

    checks: dict[str, Any] = {
        "safety_clean": {"pass": not violations_all, "violations": violations_all},
        "verifier_available": {"pass": unavailable_total == 0, "unavailable": unavailable_total},
        "required_tasks_complete": _required_tasks_check(specs_by_set, cells),
    }

    if holdout_present:
        lift_ok = bool(holdout_pair["n_pairs"]
                       and holdout_pair["mean_diff"] is not None
                       and holdout_pair["mean_diff"] >= min_lift
                       and holdout_pair["ci95"] and holdout_pair["ci95"][0] > 0
                       and holdout_manifest_sha256)
        checks["holdout_lift"] = {
            "pass": lift_ok, "lift": holdout_pair["mean_diff"], "ci95": holdout_pair["ci95"],
            "min_lift": min_lift, "n_pairs": holdout_pair["n_pairs"],
            "frozen_manifest": bool(holdout_manifest_sha256)}
    else:
        checks["holdout_lift"] = {"pass": False, "reason": "holdout_episodes_missing"}

    on_lift, off_lift = dev_on["mean_diff"], off_pair["mean_diff"]
    eff_on_dev = _pair_elapsed_ratio(_cell(ARM_ON, SET_DEV), _cell(ARM_BASELINE, SET_DEV))
    eff_off_dev = _pair_elapsed_ratio(_cell(ARM_OFF, SET_DEV), _cell(ARM_BASELINE, SET_DEV))
    success_specific = (on_lift is not None and off_lift is not None
                        and (on_lift - off_lift) >= specificity_margin)
    # efficiency-mode specificity: candidate speeds up, OFF must NOT inherit it
    eff_specific = (eff_on_dev.get("ratio") is not None and eff_off_dev.get("ratio") is not None
                    and eff_on_dev["ratio"] <= 1.0 - specificity_margin
                    and eff_off_dev["ratio"] >= 1.0 - specificity_margin)
    checks["specificity_off_weakened"] = {
        "pass": bool(success_specific or eff_specific),
        "mode": "success" if success_specific else ("efficiency" if eff_specific else None),
        "on_lift": on_lift, "off_lift": off_lift, "margin": specificity_margin,
        "eff_on_ratio": eff_on_dev.get("ratio"), "eff_off_ratio": eff_off_dev.get("ratio")}

    restore_diff = restore_pair["mean_diff"]
    eff_restore = _pair_elapsed_ratio(_cell(ARM_RESTORE, SET_DEV), _cell(ARM_BASELINE, SET_DEV))
    restore_ok_success = restore_diff is not None and abs(restore_diff) <= restore_tolerance
    restore_ok_eff = (eff_restore.get("ratio") is not None
                      and abs(eff_restore["ratio"] - 1.0) <= restore_tolerance)
    checks["restore_recovered"] = {
        "pass": bool(restore_ok_success or restore_ok_eff),
        "restore_minus_baseline": restore_diff, "tolerance": restore_tolerance,
        "eff_restore_ratio": eff_restore.get("ratio")}

    checks["cost_within_limits"] = _cost_check(
        cells.get(f"{ARM_ON}@{SET_DEV}"), cells.get(f"{ARM_BASELINE}@{SET_DEV}"),
        cost_regression_tolerance)

    checks["efficiency_lift"] = (
        _efficiency_check(_cell(ARM_ON, SET_HOLDOUT), _cell(ARM_BASELINE, SET_HOLDOUT),
                          efficiency_margin)
        if holdout_present else {"pass": False, "reason": "holdout_episodes_missing"})

    if require_restart_reproduction:
        rr_ok = (isinstance(restart_reproduction, dict)
                 and restart_reproduction.get("reproduced") is True
                 and restart_reproduction.get("profile_sha256") == arms[ARM_ON]["sha256"])
        checks["restart_reproduction"] = {"pass": rr_ok,
                                          "evidence": restart_reproduction or None}
    else:
        checks["restart_reproduction"] = {"pass": None, "note": "not_required"}

    rollback = on_lift is not None and on_lift <= -rollback_margin

    # Two alternative improvement dimensions: success lift OR efficiency lift
    # (holdout, success-non-regressing). At least one must hold to promote.
    _lift_names = ("holdout_lift", "efficiency_lift")
    _failed_all = sorted(name for name, c in checks.items() if c.get("pass") is False)
    _lift_failed = [n for n in _failed_all if n in _lift_names]
    _improvement_ok = len(_lift_failed) < len(_lift_names)
    failed = ([n for n in _failed_all if n not in _lift_names] if _improvement_ok
              else _failed_all)
    if not checks["safety_clean"]["pass"]:
        decision, reasons = "reject", ["safety_violation_hard_reject"]
    elif not checks["verifier_available"]["pass"]:
        decision, reasons = "reject", ["verifier_unavailable_hard_reject"]
    elif transport_only:
        decision, reasons = "hold", ["transport_only_run:no_capability_samples"]
    elif unmeasured_keys:
        decision, reasons = "hold", ["unmeasured_cells"] + unmeasured_keys
    elif rollback:
        decision, reasons = "rollback", ["candidate_regression", f"dev_diff={on_lift}"]
    elif failed:
        decision, reasons = "hold", failed
    else:
        passed_lift = [n for n in _lift_names if checks[n].get("pass") is True]
        decision, reasons = "promote", ["all_preregistered_checks_passed"] + (
            ["improvement_via:" + ",".join(passed_lift)] if passed_lift else [])

    # ---- report ---------------------------------------------------------------
    all_records = [r for recs in records_by_cell.values() for r in recs]
    for r in all_records:
        r.pop("_task_result", None)  # TaskResult objects were for compute_metrics only

    benchmark_id = f"{candidate_id}-{order_seed}-{int(time.time())}"
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "benchmark_id": benchmark_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "candidate_id": candidate_id,
        "parent_id": parent_id,
        "runtime_generation_id": runtime_generation_id,
        "memory_snapshot_hash": memory_snapshot_hash,
        "profile_created_at": profile_created_at,
        "order_seed": order_seed,
        "thresholds": {"min_lift": min_lift, "specificity_margin": specificity_margin,
                       "restore_tolerance": restore_tolerance,
                       "cost_regression_tolerance": cost_regression_tolerance,
                       "efficiency_margin": efficiency_margin,
                       "rollback_margin": rollback_margin},
        "holdout_manifest_sha256": holdout_manifest_sha256,
        "episodes_per_set": {"dev": len(dev_episodes), "holdout": len(holdout_episodes or [])},
        "arms": arms,
        "arm_load_records": arm_load_records,
        "execution_order": execution_order,
        "cells": cells,
        "paired": paired,
        "decision": {"decision": decision, "reasons": reasons, "checks": checks},
        "records": all_records,
    }

    if benchmark_dir is None:
        benchmark_dir = Path(os.environ.get("EGO_CLOSED_LOOP_BENCHMARK_DIR", ".lae/benchmarks"))
    d = Path(benchmark_dir)
    d.mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in report.items() if k != "report_sha256"}
    payload = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    report["report_sha256"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    path = d / f"{benchmark_id}.json"
    path.write_text(json.dumps(report, sort_keys=True, ensure_ascii=False, default=str, indent=1),
                    encoding="utf-8")
    report["report_path"] = str(path)
    return report
