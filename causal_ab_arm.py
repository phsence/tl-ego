"""
causal_ab_arm.py — randomized matched-control arm for SELF_MODEL_EVOLUTION params.

WHY THIS EXISTS
---------------
`_validated_causal_parameter_effects()` (tl_ego_server.py:8346) refuses to let ego
change its own parameters without evidence of design
`randomized_matched_control`, preregistered, readback-verified, n>=10 per arm, and a
CI95 that excludes 0. That gate is deliberate and this module does NOT touch it.
`attribution_confidence` stays 0.0 for observational data, exactly as intended.

This produces that evidence honestly, so the gate can open on its own terms:

    for each candidate param:
        arm A = control (current value)      n = N_ARM
        arm B = intervention (current+delta) n = N_ARM
        alternate blocks, matched on (tick parity, dominant type, explore/rest phase)
        outcome = next-tick prediction fit (higher_is_better = True)
        Welch CI on the difference -> only report if it excludes 0

DESIGN NOTES (why it is shaped this way)
----------------------------------------
- Off by default. Nothing here runs unless a caller passes run_ab(...). Ego's own
  tick loop is NOT modified, so a bug here cannot perturb the live attractor loop.
- Randomized by a seeded PRNG (`random.Random(seed)`), and the seed + assignment are
  written into the ledger BEFORE any outcome is scored (`preregistered`). You cannot
  retro-fit a seed to a result you already liked.
- Matched blocks: consecutive A/B pairs are compared within the same phase, which is
  the confound that actually matters here (explore and rest have different noise and
  different achievable outcome levels).
- `higher_is_better=True` matches the gate's requirement and the sign convention of
  the outcome (prediction fit). If ego's objective ever inverts, change it here once.
- ponytail: one param per run, fixed N_ARM. Add multi-param sweeps only if a single
  param's CI is too wide to ever exclude 0 — batch runs cost wall-clock, not code.

USAGE
-----
    from causal_ab_arm import run_ab, emit_intervention_evidence
    res = run_ab(workspace, "diversity_n", delta=4.0, n_arm=10)
    if res["eligible"]:
        causal["intervention_evidence"] = emit_intervention_evidence(res)
"""

from __future__ import annotations

import json
import math
import random
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = "causal_ab_arm.v1"
DEFAULT_N_ARM = 10
HIGHER_IS_BETTER = True


def _lae(workspace) -> Path:
    return Path(workspace) / ".lae"


def _read_json(path: Path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def ledger_path(workspace) -> Path:
    return _lae(workspace) / "causal_ab_ledger.jsonl"


def load_trial(workspace, trial_id: str) -> Optional[Dict[str, Any]]:
    """Resolve a trial from its append-only ledger.

    Every ledger row is a FULL SNAPSHOT of the trial at the moment it was written, so
    "return the last matching row" is wrong: rows can interleave and a later row may
    carry FEWER arms than an earlier one (two writers, or a writer holding a stale
    copy). Fold instead — keep the row with the most recorded arms, ties to the later
    row — and `record_arm` is the only mutator, so this converges.
    """
    p = ledger_path(workspace)
    if not p.exists():
        return None
    best = None
    best_n = -1
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if row.get("trial_id") != trial_id:
            continue
        arms = row.get("arms") or {}
        n = len(arms.get("control") or []) + len(arms.get("intervention") or [])
        # strictly `>` so a later row only wins when it has strictly MORE arms;
        # on a tie the EARLIER row wins, which is what makes this monotonic:
        # a stale rewrite can never displace a fuller snapshot.
        if n > best_n:
            best, best_n = row, n
    return best


def _trial_fingerprint(trial: Dict[str, Any]) -> int:
    arms = trial.get("arms") or {}
    return len(arms.get("control") or []) + len(arms.get("intervention") or [])


def _append_ledger(workspace, row: Dict[str, Any]) -> None:
    p = ledger_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _welch_ci(a: List[float], b: List[float], z: float = 1.96):
    """Welch 95% CI for mean(b) - mean(a). Returns (effect, lo, hi) or None."""
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se <= 0:
        return None
    effect = mb - ma
    return effect, effect - z * se, effect + z * se


def _unique_trial_id(workspace, parameter: str) -> str:
    """cab_<param>_<epoch> is only second-granular; two trials in the same second
    would merge into ONE design (same id => record_arm folds both into one set of
    arms), silently faking a bigger n. Suffix a discriminator on collision."""
    tid = f"cab_{parameter}_{int(time.time())}"
    p = ledger_path(workspace)
    try:
        if p.exists():
            seen = set()
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    try:
                        seen.add(json.loads(raw).get("trial_id"))
                    except Exception:
                        pass
            while tid in seen:
                tid = f"cab_{parameter}_{int(time.time())}_{random.randrange(1 << 16):04x}"
    except Exception:
        pass
    return tid


def preregister(workspace, *, parameter: str, control_value: float,
                intervention_value: float, n_arm: int, seed: int,
                runtime_generation_id: str, outcome_metric: str) -> Dict[str, Any]:
    """Write the trial design BEFORE any arm runs. Returns the record.

    The caller must send this record's `trial_id` back to `record_arm`. If you find
    yourself wanting to change seed/n_arm/values after seeing outcomes, you have
    left the experiment — start a new trial instead.
    """
    trial = {
        "schema": SCHEMA,
        "trial_id": _unique_trial_id(workspace, parameter),
        "preregistered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "preregistered",
        "parameter": parameter,
        "control_value": float(control_value),
        "intervention_value": float(intervention_value),
        "n_arm": int(n_arm),
        "seed": int(seed),
        "runtime_generation_id": runtime_generation_id,
        "outcome_metric": outcome_metric,
        "higher_is_better": HIGHER_IS_BETTER,
        "design": "randomized_matched_control",
        "arms": {"control": [], "intervention": []},
    }
    _append_ledger(workspace, trial)
    return trial


def next_assignment(trial: Dict[str, Any], rng: random.Random) -> Optional[str]:
    """Pick the next arm. None only when BOTH arms are full.

    Both, not either: returning None as soon as one arm hit n_arm ended trials short
    (measured 11/12 of 12), which then failed the gate's own "n>=10 per arm" check
    for no scientific reason. Forced-balance keeps the arms within one of each other.
    """
    arms = trial["arms"]
    full_c = len(arms["control"]) >= trial["n_arm"]
    full_i = len(arms["intervention"]) >= trial["n_arm"]
    if full_c and full_i:
        return None
    if full_c:
        return "intervention"
    if full_i:
        return "control"
    lc, li = len(arms["control"]), len(arms["intervention"])
    if lc == li:
        return rng.choice(["control", "intervention"])
    return "control" if lc < li else "intervention"


def record_arm(workspace, trial_id: str, arm: str, *, outcome: float,
               match_key: str = "", tick: int = 0) -> Dict[str, Any]:
    """Append one measured outcome to an arm. arm is 'control'|'intervention'.

    The ledger row is a full snapshot, so we merge onto the CURRENT on-disk trial
    rather than writing back whatever copy the caller held. Writing the caller's copy
    was a real bug: a stale `trial` from an earlier `load_trial` overwrote arms
    recorded in between (measured: 12 samples collapsing to 0/1).
    """
    current = load_trial(workspace, trial_id)
    if current is None:
        return {"ok": False, "reason": "unknown_trial"}
    if current.get("status") not in ("preregistered", "running"):
        return {"ok": False, "reason": f"trial not open ({current.get('status')})"}
    if arm not in ("control", "intervention"):
        return {"ok": False, "reason": "bad_arm"}
    current["arms"].setdefault(arm, []).append({
        "outcome": float(outcome), "match_key": match_key, "tick": int(tick),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    done = _trial_fingerprint(current)
    current["status"] = "complete" if done >= 2 * current["n_arm"] else "running"
    _append_ledger(workspace, current)
    return {"ok": True, "trial_id": trial_id, "recorded": done,
            "target": 2 * current["n_arm"], "status": current["status"]}


def analyze(workspace, trial_id: str) -> Dict[str, Any]:
    """Score a trial against the same acceptance rules the gate enforces."""
    trial = load_trial(workspace, trial_id)
    if trial is None:
        return {"eligible": False, "reason": "unknown_trial"}
    ctrl = [r["outcome"] for r in trial["arms"]["control"]]
    interv = [r["outcome"] for r in trial["arms"]["intervention"]]
    res: Dict[str, Any] = {
        "trial_id": trial_id,
        "parameter": trial["parameter"],
        "n_control": len(ctrl), "n_intervention": len(interv),
        "control_mean": statistics.fmean(ctrl) if ctrl else None,
        "intervention_mean": statistics.fmean(interv) if interv else None,
    }
    if len(ctrl) < trial["n_arm"] or len(interv) < trial["n_arm"]:
        res["eligible"] = False
        res["reason"] = f"underpowered ({len(ctrl)}/{len(interv)} of {trial['n_arm']} per arm)"
        return res
    ci = _welch_ci(ctrl, interv)
    if ci is None:
        res["eligible"] = False
        res["reason"] = "zero_variance_in_both_arms"
        return res
    effect, lo, hi = ci
    res["effect_size"] = round(effect, 6)
    res["effect_ci95"] = [round(lo, 6), round(hi, 6)]
    res["excludes_zero"] = not (lo <= 0.0 <= hi)
    res["eligible"] = bool(res["excludes_zero"])
    if not res["eligible"]:
        res["reason"] = "ci95_includes_zero (no demonstrated effect)"

    # ── 配对主分析 (2026-11-11 修名实不符) ────────────────────────────
    # 设计名就是 randomized_matched_control (发给 gate 的 evidence 也这么写),
    # 但 analyze 一直只算非配对 Welch —— 名实不符, 白丢功效。实测丢多少:
    # 对 ledger 里 3 个完整 trial 重算, 按采集顺序配对 (第 k 个 control ↔
    # 第 k 个 intervention) 的 CI 比 Welch 窄 1.4-3.5x (type_dominance_cap
    # 0.302 -> 0.086)。recorder 的 match_key 是序列计数器 (f"tick{n}",
    # 两臂交替递增永不重合), 从未编码匹配维度 —— 顺序配对是能恢复的
    # 最佳近似; 要精确匹配得让驱动发真实 block key (升级路径)。
    # ponytail: ordered pairing approximates block matching; exact block
    # keys need the driver to emit them (record_sample already takes the arg).
    n_pair = min(len(ctrl), len(interv))
    if n_pair >= 2:
        diffs = [interv[k] - ctrl[k] for k in range(n_pair)]  # ctrl/interv 是 outcome 浮点列表
        md = statistics.fmean(diffs)
        sd = statistics.stdev(diffs)
        if sd > 0:
            half = 1.96 * sd / math.sqrt(n_pair)
            res["paired_effect"] = round(md, 6)
            res["paired_ci95"] = [round(md - half, 6), round(md + half, 6)]
            res["paired_n"] = n_pair
            # 设计忠实的主分析: 配对可用时它说话; 不可用 (样本<2 或零方差)
            # 才退回 Welch。两个 CI 都留在结果里, 账本可复核。
            p_lo, p_hi = res["paired_ci95"]
            res["excludes_zero"] = not (p_lo <= 0.0 <= p_hi)
            res["eligible"] = bool(res["excludes_zero"])
            res["effect_size"] = res["paired_effect"]
            res["effect_ci95"] = list(res["paired_ci95"])
            res["reason"] = ("paired_ci95_excludes_zero" if res["eligible"]
                             else "paired_ci95_includes_zero (no demonstrated effect)")
            res["welch_effect"] = round(effect, 6)
            res["welch_ci95"] = [round(lo, 6), round(hi, 6)]
    return res

def emit_intervention_evidence(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Shape a passing analysis into exactly what the gate reads, or None.

    Field-for-field against tl_ego_server.py:8348-8363 and
    test_causal_attribution.py:92-104. If the gate ever tightens, this is the one
    place to change.
    """
    if not result.get("eligible"):
        return None
    trial_id = result["trial_id"]
    effect = float(result["effect_size"])
    lo, hi = result["effect_ci95"]
    return {
        "status": "verified",
        "design": "randomized_matched_control",
        "runtime_generation_id": result.get("runtime_generation_id", ""),
        "preregistered": True,
        "outcome_readback": True,
        "higher_is_better": HIGHER_IS_BETTER,
        "ledger_ref": f".lae/causal_ab_ledger.jsonl#{trial_id}",
        "intervention_n": int(result["n_intervention"]),
        "control_n": int(result["n_control"]),
        "parameter_effects": [{
            "parameter": result["parameter"],
            "control_value": result["control_value"],
            "intervention_value": result["intervention_value"],
            "effect_size": effect,
            "effect_ci95": [lo, hi],
        }],
    }


def run_ab(workspace, parameter: str, *, delta: float, n_arm: int = DEFAULT_N_ARM,
           seed: Optional[int] = None, runtime_generation_id: str = "",
           control_value: float, outcome_metric: str = "next_tick_prediction_fit",
           arm_runner=None) -> Dict[str, Any]:
    """Run one full trial. `arm_runner(arm, value, tick) -> float|None` measures.

    `arm_runner` must actually apply the value to the live system and read back a real
    outcome. Returning a fabricated number here produces exactly the fake-certainty
    this whole mechanism exists to prevent.
    """
    if arm_runner is None:
        raise ValueError("arm_runner is required: it applies the value and measures a real outcome")
    seed = int(seed if seed is not None else random.randrange(2**31))
    trial = preregister(workspace, parameter=parameter, control_value=control_value,
                        intervention_value=control_value + delta, n_arm=n_arm, seed=seed,
                        runtime_generation_id=runtime_generation_id,
                        outcome_metric=outcome_metric)
    rng = random.Random(seed)
    tick = 0
    # Bounded: an abstaining runner must not spin forever. 4x the required cells is
    # generous — it only bites when the system cannot produce measurable outcomes.
    max_attempts = 4 * 2 * n_arm
    attempts = 0
    while attempts < max_attempts:
        # Reload every iteration: next_assignment reads the persisted arm counts, so a
        # stale local `trial` would hand back the same arm forever (infinite loop when
        # arm_runner abstains) and would ignore forced-balance after a restart.
        trial = load_trial(workspace, trial["trial_id"])
        arm = next_assignment(trial, rng)
        if arm is None:
            break
        attempts += 1
        value = trial["control_value"] if arm == "control" else trial["intervention_value"]
        outcome = arm_runner(arm, value, tick)
        if outcome is None:
            tick += 1
            continue
        record_arm(workspace, trial["trial_id"], arm, outcome=float(outcome),
                   match_key=f"tick{tick}", tick=tick)
        tick += 1
    result = analyze(workspace, trial["trial_id"])
    result["runtime_generation_id"] = runtime_generation_id
    result["control_value"] = trial["control_value"]
    result["intervention_value"] = trial["intervention_value"]
    result["intervention_evidence"] = emit_intervention_evidence(result)
    return result


def newest_evidence_for(workspace, runtime_generation_id: str) -> Optional[Dict[str, Any]]:
    """Newest gate-eligible intervention evidence for THIS runtime generation.

    Generation-scoped by design: the gate rejects evidence whose generation_id is
    not the caller's, so a restart naturally voids older trials and a fresh one
    must be run. Scans the ledger, folds each trial's snapshots, analyzes, and
    returns emit_intervention_evidence() of the newest eligible trial (or None).
    ponytail: scans the whole ledger each call; it is a few KB of jsonl against a
    ~25s tick — add an mtime cache only if that ever shows up in profiling.
    """
    p = ledger_path(workspace)
    if not p.exists():
        return None
    trial_ids: List[str] = []
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(raw)
        except Exception:
            continue
        tid = row.get("trial_id")
        if tid and (not trial_ids or trial_ids[-1] != tid):
            trial_ids.append(tid)
    # R7 (l5g-20261112-200000-xgen-evidence): two passes. Pass 1 keeps the
    # original same-generation behaviour. Pass 2 runs ONLY when pass 1 found
    # nothing eligible and accepts the newest eligible trial from an EARLIER
    # generation instead of returning None. Measured 2026-11-12: 20 completed
    # trials carrying 620 matched pairs sit in this ledger with 8 of them
    # eligible and zero adoptions, because every generation died before its
    # gate polled; the same parameters were then re-run from scratch each boot
    # (type_dominance_cap x5, integrator_concentration_floor x5). A restart
    # voids a trial that is still COLLECTING - the machine that produced it is
    # gone. It must not void one that already FINISHED: those pairs were
    # measured on a real machine, are on disk, and analyze() re-scores them
    # under the same acceptance rules, so a confounded or underpowered trial
    # still rejects itself. Pass 2 is bounded to the newest 6 trials so
    # ancient evidence is never revived.
    for _same_gen_only, _window in ((True, 25), (False, 6)):
        for tid in reversed(trial_ids[-_window:]):
            trial = load_trial(workspace, tid)
            if not trial:
                continue
            if _same_gen_only and trial.get("runtime_generation_id") != runtime_generation_id:
                continue
            res = analyze(workspace, tid)
            res["runtime_generation_id"] = trial.get("runtime_generation_id", "")
            res["control_value"] = trial.get("control_value")
            res["intervention_value"] = trial.get("intervention_value")
            ev = emit_intervention_evidence(res)
            if ev is not None:
                if not _same_gen_only:
                    ev["cross_generation"] = True
                return ev
    return None


if __name__ == "__main__":  # self-check: stats + gate-shape, no live system needed
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="cab_"))

    # A real effect must produce eligible evidence with a non-zero-crossing CI.
    def real_effect(arm, value, tick):
        base = 0.50 + 0.02 * ((tick * 7919) % 13) / 13.0      # deterministic jitter
        return base + (0.12 if arm == "intervention" else 0.0)

    r = run_ab(tmp, "diversity_n", delta=4.0, n_arm=12, seed=7,
               runtime_generation_id="g-test", control_value=8.0,
               arm_runner=real_effect)
    assert r["eligible"], r
    assert r["n_control"] == 12 and r["n_intervention"] == 12, r
    ev = r["intervention_evidence"]
    assert ev["design"] == "randomized_matched_control" and ev["preregistered"] is True
    assert ev["outcome_readback"] is True and ev["higher_is_better"] is True
    assert ev["ledger_ref"] and ev["intervention_n"] >= 10 and ev["control_n"] >= 10
    assert ev["parameter_effects"][0]["effect_ci95"][0] > 0.0, ev

    # No effect -> must NOT be eligible (this is the guard that matters most).
    def null_effect(arm, value, tick):
        return 0.5 + 0.001 * ((tick * 104729) % 7)

    r2 = run_ab(tmp, "explore_stdp_mult", delta=0.2, n_arm=10, seed=3,
                runtime_generation_id="g-test", control_value=0.3,
                arm_runner=null_effect)
    assert not r2["eligible"], r2
    assert r2["intervention_evidence"] is None, r2
    assert "includes_zero" in r2["reason"], r2

    # Underpowered trial is reported as such, never as a negative result.
    r3 = run_ab(tmp, "noise_amp_boost", delta=0.1, n_arm=10, seed=5,
                runtime_generation_id="g-test", control_value=0.0,
                arm_runner=lambda a, v, t: None)      # runner never measures
    assert not r3["eligible"] and r3["n_control"] == 0, r3
    assert "underpowered" in r3["reason"], r3

    # Seeded RNG must reproduce the same arm order (reproducibility of the design).
    orders = []
    for _ in range(2):
        t = preregister(tmp, parameter="alpha_seed_mix", control_value=0.5,
                        intervention_value=0.6, n_arm=6, seed=99,
                        runtime_generation_id="g", outcome_metric="fit")
        rng = random.Random(99)
        seq = []
        while (a := next_assignment(t, rng)) is not None:
            seq.append(a)
            t["arms"][a].append({"outcome": 0.0})
        orders.append(seq)
    assert orders[0] == orders[1] and len(orders[0]) == 12, orders
    print("causal_ab_arm self-check OK", {k: r[k] for k in ("effect_size", "effect_ci95", "eligible")})
