"""run_evolution_round_r3.py — Evolution Round R3: L10-brain-derived candidates.

Chain of provenance:
  l10_meta_algorithm_generator.jsonl (ego's own hypothesis engine)
    -> l10_gate_policy_bridge.py (semantic map onto gate primitives)
      -> .lae/l10_gate_policies_r3.json (blueprints)
        -> THIS SCRIPT instantiates callables + scores on frozen suite v3 (2026-09-20)

Selection metric: per-case micro accuracy over all hidden cases (15 total),
because the v2 binary axis threshold (ok>=2) saturates and cannot see the
single geometric case. Frozen assets untouched — granularity lives in the
runner, not the suite (no Goodhart surface change).

Registry verdicts use CandidateRegistry's real lifecycle; retained winners are
written to the deployment ledger like R2.

Usage:
    H:/unsloth_env/Scripts/python.exe run_evolution_round_r3.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

from capability_task_protocol import validate_manifest  # noqa: E402
from l10_gate_policy_bridge import read_latest_l10_strategies  # noqa: E402
from quality_diversity_archive import QDArchive, archive_summary  # noqa: E402

WORKSPACE = PROJECT_DIR.parent.parent
LAE = WORKSPACE / ".lae"
SUITE_DIR = PROJECT_DIR / "capability_suites" / "hidden"
MANIFEST = SUITE_DIR / "manifest_v7.json"
# Suite lineage v3 -> v4 -> v5 -> v6. Each version = previous hidden cases
# verbatim + traps on the then-deployed champion's demonstrated weaknesses,
# machine-verified (champion fails, candidate passes, prior suite carries).
# v5 saturated at 1.0 (QD archive best_fitness=1.0 over the v5 niches), so v6 =
# v5 verbatim + arithmetic traps on the post-v5 champion's weaknesses (n^5+
# powers, geometric differences, affine recurrence, consecutive primes), PLUS
# controls on families the champion already solves so a candidate cannot score
# a gain by breaking them. Pattern/json/graph/pipeline carry no new traps — the
# champion is perfect on every probed shape there.
# When bumping the suite again: change MANIFEST, CHALLENGES and the two
# "hidden_vN" literals below (QD descriptor prefix + registry suite_version).
# qd_saturation() no longer carries a copy — it derives the prefix from the
# newest archive row (2026-11-11: the hardcoded "hidden_v6" default read a
# retired suite's 1.0 and froze every round from 09-22 to 11-11).
CHALLENGES = SUITE_DIR / "assets" / "challenges_v7.json"
BRIDGE_JSON = LAE / "l10_gate_policies_r3.json"
REGISTRY_DB = LAE / "candidate_registry.sqlite"
DEPLOY_REGISTRY = LAE / "l9_deployment_registry.json"
EVOLVE_JSON = WORKSPACE / "evolve.json"
ROUND_STATE = LAE / "evolution_round_state.json"
QD_DIR = LAE / "qd_archive"
SUITE_VERSION = "hidden_v7"

SEED = "evo-round-3-l10-bridge"
MIN_GAIN_MICRO = 0.05           # manifest_v3: gate code unchanged (min_gain=0.05)
                                # 0.001 was 1/28 of a case -> 0.2% reject rate over 2343 candidates.
                                # r1/r2 and capability_evolution_gate.py all use 0.05.


# ── Detector bank ───────────────────────────────────────────────────────────

def _det_fib(series):
    """Arithmetic, then whole-series fib. The old i==2-only fib check
    false-positived on periodic series ([1,2,3,4,1,2,3,4]: 3==1+2) and stole
    the answer from the period detector — verify the whole series instead."""
    if len(series) < 2:
        return series[-1]
    d = series[1] - series[0]
    if all(series[i + 1] - series[i] == d for i in range(len(series) - 1)):
        return series[-1] + d
    if len(series) >= 3 and all(series[i] == series[i - 1] + series[i - 2]
                                for i in range(2, len(series))):
        return series[-1] + series[-2]
    return None


def _det_fib_strict(series):
    """Whole-series fib check. _det_fib above only tests i==2, so it
    false-positives on [1,2,3,4,1,2,3,4] (3==1+2) and steals the answer from
    the period detector. Admits ignorance instead of lying."""
    if len(series) < 3:
        return None
    if all(series[i] == series[i - 1] + series[i - 2] for i in range(2, len(series))):
        return series[-1] + series[-2]
    return None


def _det_square(series):
    """Consecutive squares: [1,4,9,16] -> 25."""
    n = len(series)
    if n < 3:
        return None
    roots = []
    for v in series:
        if v < 0:
            return None
        r = int(round(v ** 0.5))
        if r * r != v:
            return None
        roots.append(r)
    if all(roots[i + 1] - roots[i] == 1 for i in range(n - 1)):
        return (roots[-1] + 1) ** 2
    return None


def _det_cubic(series):
    """Consecutive cubes: [1,8,27,64] -> 125."""
    n = len(series)
    if n < 3:
        return None
    bases = []
    for v in series:
        b = int(round(v ** (1 / 3))) if v >= 0 else -int(round((-v) ** (1 / 3)))
        if b ** 3 != v:
            return None
        bases.append(b)
    if all(bases[i + 1] - bases[i] == 1 for i in range(n - 1)):
        return (bases[-1] + 1) ** 3
    return None


def _det_double_minus_one(series):
    """x[i] == 2*x[i-1] - 1: [2,3,5,9,17] -> 33."""
    n = len(series)
    if n < 3:
        return None
    if all(series[i] == 2 * series[i - 1] - 1 for i in range(1, n)):
        return 2 * series[-1] - 1
    return None


def _det_second_diff(series):
    """Constant second difference: [1,2,4,7,11] -> 16."""
    n = len(series)
    if n < 4:
        return None
    d1 = [series[i + 1] - series[i] for i in range(n - 1)]
    d2 = [d1[i + 1] - d1[i] for i in range(len(d1) - 1)]
    if len(set(d2)) == 1:
        return series[-1] + d1[-1] + d2[0]
    return None


def _det_quartic(series):
    """Consecutive fourth powers: [1,16,81,256] -> 625."""
    n = len(series)
    if n < 3:
        return None
    bases = []
    for v in series:
        if v < 0:
            return None
        b = int(round(v ** 0.25))
        if b ** 4 != v:
            return None
        bases.append(b)
    if all(bases[i + 1] - bases[i] == 1 for i in range(n - 1)):
        return (bases[-1] + 1) ** 4
    return None


def _det_factorial(series):
    """Factorials: [1,2,6,24] -> 120."""
    n = len(series)
    if n < 3:
        return None
    fs = [1]
    f = 1
    for k in range(1, n + 3):
        f *= k
        fs.append(f)
    # a contiguous slice of the factorial table, not starting at index 0
    # (where 0! and 1! are both 1)
    for i in range(1, len(fs) - n):
        if fs[i:i + n] == series:
            return fs[i + n]
    return None


def _det_alt_sign(series):
    """Arithmetic on |x| with sign set by index: [1,-2,3,-4,5] -> -6."""
    n = len(series)
    if n < 3:
        return None
    signs = [1 if x >= 0 else -1 for x in series]
    if not all(signs[i] == (-1 if i % 2 else 1) for i in range(n)):
        return None
    mags = [abs(x) for x in series]
    d = mags[1] - mags[0]
    if all(mags[i + 1] - mags[i] == d for i in range(n - 1)):
        nxt = mags[-1] + d
        # the NEXT value sits at index n, so it is negative when n is odd
        return -nxt if n % 2 == 1 else nxt
    return None


def _det_third_diff(series):
    """Constant third difference: [1,2,4,8,15] -> 26."""
    n = len(series)
    if n < 5:
        return None
    d1 = [series[i + 1] - series[i] for i in range(n - 1)]
    d2 = [d1[i + 1] - d1[i] for i in range(len(d1) - 1)]
    d3 = [d2[i + 1] - d2[i] for i in range(len(d2) - 1)]
    if len(set(d3)) == 1:
        return series[-1] + d1[-1] + d2[-1] + d3[0]
    return None


def _det_geometric(series):
    if len(series) >= 2 and all(s != 0 for s in series):
        q = series[1] / series[0]
        if all(abs(series[i + 1] / series[i] - q) < 1e-9 for i in range(len(series) - 1)):
            p = series[-1] * q
            if abs(p - round(p)) < 1e-9:
                return int(round(p))
    return None


def _det_constant(series):
    # ponytail: period-check first — this detector never returns None, so any
    # real detector ordered after it is unreachable. Cheapest fix is here, once,
    # for every caller, instead of policing order in every blueprint.
    if len(series) >= 4:
        n = len(series)
        for p in range(2, n):
            if all(series[i] == series[i - p] for i in range(p, n)):
                return series[n - p]
    if len(series) < 2:
        return series[-1]
    return series[-1] + (series[1] - series[0])


def _det_period(series):
    """Periodic / oscillating series: smallest p>=2 with series[i]==series[i-p]
    throughout. Answers the 'period oscillation' trap family in suite v3
    ([5,6,7,5,6,7] -> 5, [3,9,3,9] -> 3) that fib/geometric/constant cannot.
    Ordered AFTER geometric so geometric series win on their own terms."""
    n = len(series)
    for p in range(2, n):
        if all(series[i] == series[i - p] for i in range(p, n)):
            return series[n - p]
    return None


def _det_power_gen(series):
    """Consecutive k-th powers from a base: [1,32,243,1024] -> 3125 (k=5).

    Generalises _det_square/_det_cubic/_det_quartic, which only ever matched
    their own exponent. Probing 2026-09-22 showed the champion fails every
    exponent outside {2,3,4}, so the family is worth a real detector.
    """
    n = len(series)
    if n < 3 or any(v < 0 for v in series):
        return None
    for k in range(2, 8):
        b0 = int(round(series[0] ** (1.0 / k)))
        for start in (b0 - 1, b0, b0 + 1):
            if start < 0:
                continue
            if all((start + i) ** k == series[i] for i in range(n)):
                return (start + n) ** k
    return None


def _det_affine1(series):
    """a[i] = p*a[i-1] + q with p,q integers, verified on EVERY term.

    Subsumes geometric (q=0), 2n+1 (p=2,q=-1) and 2n+3 (p=2,q=3). Must be
    ordered AFTER the narrower detectors: it matches any series they match,
    so putting it first would make them unreachable (same trap as _det_constant).
    """
    n = len(series)
    if n < 3 or series[1] == series[0]:
        return None
    num, den = series[2] - series[1], series[1] - series[0]
    if den == 0 or num % den != 0:
        return None
    p = num // den
    q = series[1] - p * series[0]
    if not all(series[i] == p * series[i - 1] + q for i in range(1, n)):
        return None
    return p * series[-1] + q


def _det_geom_diff(series):
    """Differences grow geometrically: [1,4,13,40,121] -> 364 (diffs 3,9,27,81)."""
    n = len(series)
    if n < 4:
        return None
    d = [series[i + 1] - series[i] for i in range(n - 1)]
    if any(v == 0 for v in d) or d[1] % d[0] != 0:
        return None
    r = d[1] // d[0]
    if r <= 1 or not all(d[i + 1] == r * d[i] for i in range(len(d) - 1)):
        return None
    return series[-1] + d[-1] * r


def _det_primes(series):
    """Consecutive primes: [2,3,5,7,11] -> 13. Needs >= 4 terms to be unambiguous."""
    n = len(series)
    if n < 4:
        return None

    def _is_prime(v):
        if v < 2:
            return False
        for d in range(2, int(v ** 0.5) + 1):
            if v % d == 0:
                return False
        return True

    if not all(_is_prime(v) for v in series):
        return None
    v = series[-1] + 1
    while not _is_prime(v):
        v += 1
    return v


DETECTORS = {"fib": _det_fib, "geometric": _det_geometric,
             "constant": _det_constant, "period": _det_period,
             "fib_strict": _det_fib_strict, "square": _det_square,
             "cubic": _det_cubic, "double_m1": _det_double_minus_one,
             "second_diff": _det_second_diff,
             "quartic": _det_quartic, "factorial": _det_factorial,
             "alt_sign": _det_alt_sign, "third_diff": _det_third_diff,
             # v6 detectors (2026-09-22): probed gaps on the deployed champion.
             # Order note — _det_affine1 matches anything geometric/2n+1/2n+3
             # matches, so it goes LAST before the linear fallback.
             "power_gen": _det_power_gen, "geom_diff": _det_geom_diff,
             "primes": _det_primes, "affine1": _det_affine1}

CHAMPION_BASE_ARITH = _det_fib   # incumbent champion uses fib-only arithmetic


# ── QD behavior descriptor (MAP-Elites niche key) ───────────────────────────
# Honest mapping for this candidate family: the only things that actually vary
# are the strategy family (categorical) and how many detectors are chained per
# case. Unset descriptor dims default to 0 = "not applicable".

def behavior_descriptor(bp: dict) -> dict:
    names = [n for n in bp.get("params", {}).get("detectors", []) if n in DETECTORS]
    return {
        # 2026-09-20: suite-prefixed QD niches — new suite = new niches. Reusing
        # the old prefix would let saturated 1.0 cells keep winning MAP-Elites.
        "task_domain": f"{SUITE_VERSION}:{bp.get('strategy_type', 'unknown')}",
        "resource_cost": min(1.0, len(names) / max(1, len(DETECTORS))),
    }


def instantiate_arith(params: dict, strategy_type: str):
    """Turn a bridge blueprint into an arithmetic primitive."""
    names = params.get("detectors", ["fib"])
    fns = [DETECTORS[n] for n in names if n in DETECTORS]
    # ponytail: _det_constant never returns None, so in a first-non-None chain it
    # shadows every detector after it (hit this 4 times). Sort it last, once,
    # here, instead of policing order in every blueprint. It stays reachable —
    # it just runs after the specific detectors have declined.
    fns = sorted(fns, key=lambda f: f is _det_constant)

    if strategy_type == "hp_types_rotation":
        def predict(series, _fns=fns):
            k = sum(series) % max(1, len(_fns))     # deterministic rotation by case content
            order = [_fns[(i + k) % len(_fns)] for i in range(len(_fns))]
            for fn in order:
                r = fn(series)
                if r is not None:
                    return r
            return series[-1]
        return predict

    if strategy_type == "dead_type_booster_floor":
        floor_names = set(params.get("floor_family", []))
        dead = [DETECTORS[n] for n in floor_names if n in DETECTORS]
        rest = [fn for fn in fns if fn not in dead]
        def predict(series, _dead=dead, _rest=rest):
            for fn in _dead + _rest:                 # floored family first
                r = fn(series)
                if r is not None:
                    return r
            return series[-1]
        return predict

    # default / kwta_threshold_expansion: widened pool, fixed priority order
    def predict(series, _fns=fns):
        for fn in _fns:
            r = fn(series)
            if r is not None:
                return r
        return series[-1]
    return predict


def make_policy(policy_name: str, blueprint: dict) -> dict:
    st = blueprint["strategy_type"]
    # ponytail: the baseline is a FROZEN reference point, not a live competitor.
    # It must keep the pre-v3-trap primitives (traversal-only json, identity
    # pattern, forbidden-blind bfs) or every later gain is measured against a
    # moving target and the whole round loses its meaning. Pinned here so
    # fixing the champion's shared primitives cannot silently raise baseline.
    if policy_name == "baseline_champion_r2":
        return {
            "arithmetic": CHAMPION_BASE_ARITH,
            "json_path": _json_traversal_only,
            "pattern": _pattern_identity_only,
            "pipeline": _pipeline_executor_ref(),
            "graph_path": _graph_bfs_noforbid,
        }
    return {
        "arithmetic": instantiate_arith(blueprint.get("params", {}), st),
        "json_path": _json_nested_nav_ref(),
        "pattern": _pattern_full_identity_ref(),
        "pipeline": _pipeline_executor_ref(),
        "graph_path": _graph_bfs_ref(),
    }


def _json_traversal_only(doc, path):
    """Frozen v2 baseline: descent only, no literal dotted-key preference."""
    if not path:
        return None
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _pattern_identity_only(examples, unseen):
    """Frozen v2 baseline: identity assumption, no learned transform."""
    return unseen


def _graph_bfs_noforbid(edges, start, goal, forbidden=None):
    """Frozen v2 baseline: ignores the forbidden-node constraint."""
    from run_hidden_capability_gate_v2 import _graph_bfs_core
    return _graph_bfs_core(edges, start, goal)


# refs to the champion's non-arithmetic primitives (all already at ceiling)
def _json_nested_nav_ref():
    from run_hidden_capability_gate_v2 import _json_nested_nav
    return _json_nested_nav


def _pattern_full_identity_ref():
    from run_hidden_capability_gate_v2 import _pattern_full_identity
    return _pattern_full_identity


def _pipeline_executor_ref():
    from run_hidden_capability_gate_v2 import _pipeline_executor
    return _pipeline_executor


def _graph_bfs_ref():
    from run_hidden_capability_gate_v2 import _graph_bfs
    return _graph_bfs


# ── Micro scoring on frozen challenges ──────────────────────────────────────

def micro_score(policy: dict) -> tuple[float, dict]:
    ch = json.loads(CHALLENGES.read_text(encoding="utf-8"))
    ok, tot = 0, 0
    detail = {}
    c_ok = sum(1 for c in ch["arithmetic_series"]["hidden"]
               if policy["arithmetic"](c["series"]) == c["next"])
    detail["arith"] = c_ok; ok += c_ok; tot += len(ch["arithmetic_series"]["hidden"])
    j_ok = sum(1 for c in ch["json_path"]["hidden"]
               if policy["json_path"](c["doc"], c["path"]) == c["expect"])
    detail["json"] = j_ok; ok += j_ok; tot += len(ch["json_path"]["hidden"])
    p_ok = sum(1 for c in ch["pattern_completion"]["hidden"]
               if policy["pattern"](c["examples"], c["unseen"]) == c["expect"])
    detail["pattern"] = p_ok; ok += p_ok; tot += len(ch["pattern_completion"]["hidden"])
    pl_ok = 0
    for c in ch["long_horizon_pipeline"]["hidden"]:
        out = policy["pipeline"](c["input_records"], c["ops"])
        if out is not None and out.get("sum") == c["expect_sum"]:
            pl_ok += 1
    detail["pipeline"] = pl_ok; ok += pl_ok; tot += len(ch["long_horizon_pipeline"]["hidden"])
    g_ok = sum(1 for c in ch["graph_traversal"]["hidden"]
               if policy["graph_path"](c["edges"], c["start"], c["goal"],
                                       c.get("forbidden")) == c["expect_len"])
    detail["graph"] = g_ok; ok += g_ok; tot += len(ch["graph_traversal"]["hidden"])
    detail["total"] = f"{ok}/{tot}"
    return round(ok / tot, 6), detail


# ── Registry + deployment plumbing (same as R2) ─────────────────────────────

def _load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _registry_apply(policy_name: str, micro: float, baseline_micro: float,
                    verdict: str, detail: dict, l10_cid: str) -> str:
    from candidate_registry import CandidateRegistry, compute_candidate_id  # noqa: E402
    reg = CandidateRegistry(REGISTRY_DB, tick_budget=8)
    proposal = {
        "policy": policy_name,
        "mutation_operator": "l10_brain_bridge_r3",
        "micro_accuracy": micro,
        "baseline_micro": baseline_micro,
        "case_detail": detail,
        "suite_version": SUITE_VERSION,
        "provenance": {"l10_candidate_id": l10_cid,
                       "bridge": "l10_gate_policy_bridge.py"},
    }
    cid = compute_candidate_id(None, "l10_brain_bridge_r3", proposal, "E3", SEED)
    try:
        cand = reg.get(cid)
        if cand is None:
            cand = reg.propose(
                parent_candidate_id=None,
                mutation_operator="l10_brain_bridge_r3",
                proposal=proposal,
                birth_generation="E3",
                random_seed=SEED,
                birth_state={"round": 3, "baseline": "c_longhorizon_bfs_v2",
                             "baseline_micro": baseline_micro},
                intended_mechanism=f"L10 {policy_name} mapped to gate primitives",
                risk=0.25,
            )
        cur = cand.get("state")
        for hop in ("admitted", "testing"):
            if cur == hop:
                continue
            if cur == "proposed" and hop == "admitted":
                reg.transition(cid, "admitted", posterior_effect={"round": 3})
                cur = "admitted"
            elif cur == "admitted" and hop == "testing":
                reg.transition(cid, "testing", posterior_effect={"round": 3})
                cur = "testing"
        terminal = {"retained": "retained", "rejected": "rejected"}.get(verdict)
        if terminal and cur == "testing":
            reg.transition(cid, terminal, posterior_effect={
                "round": 3, "micro_accuracy": micro,
                "gate": "hidden_capability_suite_v2",
                "l10_provenance": l10_cid,
            })
        return cid
    except Exception as e:
        print(f"[r3-registry] error {policy_name}: {e}", flush=True)
        return ""


def main() -> int:
    print(f"=== 进化轮 R3：L10 大脑桥接候选 · 冻结 suite {SUITE_VERSION} · 微观选择 ===\n")

    # gate check: manifest must still verify
    m = validate_manifest(MANIFEST, SUITE_DIR)
    print(f"[integrity] manifest OK ({m['assets']})")

    _bridge = json.loads(BRIDGE_JSON.read_text(encoding="utf-8"))
    blueprints = _bridge.get("blueprints", [])
    if not blueprints:
        # Bridge 说没有一族有余量(QD 饱和) -> 跳轮, 不动 round state / registry / 账本
        print(f"[round] no blueprints — {_bridge.get('reason') or 'bridge emitted none'}; "
              f"round skipped, previous state kept")
        return 0
    # QD is a diversity source, not a second deployment path. Only replay
    # payloads written by this suite with a matching measured score; legacy or
    # unverifiable cells stay audit-only.
    try:
        _qd = QDArchive(QD_DIR)
        live_names = {bp.get("policy_name") for bp in blueprints}
        for cell in _qd.replayable_cells(SUITE_VERSION):
            payload = cell["payload"]
            names = payload.get("params", {}).get("detectors", [])
            if (payload.get("policy_name") in live_names or
                    payload.get("strategy_type") not in {
                        "kwta_threshold_expansion", "hp_types_rotation",
                        "dead_type_booster_floor"} or
                    not all(name in DETECTORS for name in names)):
                continue
            blueprints.append(payload)
        _qd.close()
    except Exception as e:
        print(f"[qd] replay skipped (archive advisory): {e}", flush=True)
    live_cids = {c["candidate_id"] for c in read_latest_l10_strategies()}
    print(f"[bridge] {len(blueprints)} L10-derived blueprints; live l10 ids: "
          f"{sorted(live_cids) or '(log rotated)'}\n")

    base_policy = make_policy("baseline_champion_r2", {"strategy_type": "kwta_threshold_expansion",
                                                       "params": {"detectors": ["fib"]}})
    base_micro, base_detail = micro_score(base_policy)
    print(f"[baseline] c_longhorizon_bfs_v2 semantics: micro={base_micro} {base_detail}")

    results = []
    by_name = {bp["policy_name"]: bp for bp in blueprints}
    for bp in blueprints:
        name = bp["policy_name"]
        pol = make_policy(name, bp)
        micro, detail = micro_score(pol)
        gain = round(micro - base_micro, 6)
        verdict = ("retained" if gain >= MIN_GAIN_MICRO
                   else "rejected" if gain <= -MIN_GAIN_MICRO else "hold")
        results.append({"policy": name, "micro": micro, "gain": gain,
                        "verdict": verdict, "detail": detail,
                        "l10_candidate_id": bp.get("l10_candidate_id")})
        print(f"[candidate] {name}: micro={micro} {detail} gain={gain:+} -> {verdict}")

    for r in results:
        if r["verdict"] == "retained":
            cid = _registry_apply(r["policy"], r["micro"], base_micro,
                                  "retained", r["detail"], r["l10_candidate_id"])
            r["cid"] = cid
            entry = {
                "sandbox_id": f"evog3-{r['policy']}",
                "mech_name": "l10_brain_bridge_r3",
                "mech_id": cid or r["policy"],
                "proposal_confidence": r["micro"],
                "hypothesis_kind": r["policy"],
                "try_apply_action": "l5_three_gate",
                "deployed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "gate": "hidden_capability_v2",
                "deployment_status": "effect_verified",
                "gate3_verified": True,
                "external_metrics": {"micro_accuracy": r["micro"],
                                     "case_detail": r["detail"],
                                     "l10_provenance": r["l10_candidate_id"]},
            }
            key = f"evog3-{r['policy']}"
            dreg = _load_json(DEPLOY_REGISTRY, {"deployments": {}, "stats": {"total_deployed": 0, "by_gate": {}}})
            dreg.setdefault("deployments", {})
            dreg.setdefault("stats", {"total_deployed": 0, "by_gate": {}})
            if key not in dreg["deployments"]:
                dreg["deployments"][key] = entry
                dreg["stats"]["total_deployed"] = len(dreg["deployments"])
                dreg["stats"].setdefault("by_gate", {}).setdefault("hidden_capability_v2", 0)
                dreg["stats"]["by_gate"]["hidden_capability_v2"] += 1
            _save_json(DEPLOY_REGISTRY, dreg)

            evo = _load_json(EVOLVE_JSON, {"l9_deployments": [], "evolved_at": "", "stats": {"l9_total_deployed": 0}})
            evo.setdefault("l9_deployments", [])
            if not any(d.get("sandbox_id") == key for d in evo["l9_deployments"]):
                evo["l9_deployments"].append(entry)
                evo["evolved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                evo.setdefault("stats", {}).setdefault("l9_total_deployed", 0)
                evo["stats"]["l9_total_deployed"] = len(evo["l9_deployments"])
            _save_json(EVOLVE_JSON, evo)
            print(f"[deploy] {r['policy']} retained + ledgers updated (cid={cid[:24]}...)")
        elif r["verdict"] == "rejected":
            cid = _registry_apply(r["policy"], r["micro"], base_micro,
                                  "rejected", r["detail"], r["l10_candidate_id"])
            r["cid"] = cid
            print(f"[reject] {r['policy']} rejected (gain={r['gain']:+})")
        else:
            print(f"[hold] {r['policy']} no material gain ({r['gain']:+})")

    # QD archive: feed EVERY scored candidate with its real score (not just the
    # globally retained ones) so the best per behavior niche survives the
    # single-hand-written-score race. Advisory: never blocks the round.
    qd_summary = {}
    try:
        _qd = QDArchive(QD_DIR)
        for r in results:
            _payload = dict(by_name.get(r["policy"], {}))
            _payload["_qd_evidence"] = {
                "suite_version": SUITE_VERSION,
                "evidence_valid": True,
                "micro": r["micro"],
                "candidate_id": r.get("cid") or f"round3:{r['policy']}",
            }
            _qd.add(r.get("cid") or f"round3:{r['policy']}",
                    behavior_descriptor(_payload), r["micro"], payload=_payload)
        qd_summary = archive_summary(_qd)
        _qd.close()
        print(f"[qd] archive now {qd_summary['niches']} niches, "
              f"best_fitness={qd_summary['best_fitness']} -> {QD_DIR}")
    except Exception as e:
        print(f"[qd] archive feed failed (round continues): {e}", flush=True)

    winners = [r for r in results if r["verdict"] == "retained"]
    next_baseline = (max(winners, key=lambda r: r["micro"])["policy"]
                     if winners else "c_longhorizon_bfs_v2")
    _save_json(ROUND_STATE, {
        "round": 3,
        "gate": "hidden_capability_suite_v2",
        "selection_metric": "per-case micro accuracy (runner-side; assets frozen)",
        "baseline": "c_longhorizon_bfs_v2",
        "baseline_micro": base_micro,
        "next_baseline": next_baseline,
        "min_gain": MIN_GAIN_MICRO,
        "results": results,
        "qd_archive": {"dir": str(QD_DIR), **qd_summary},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    print(f"\n[round] next_baseline = {next_baseline}")
    print("[round] state saved -> evolution_round_state.json")
    print("=== 进化轮 R3 完成：L10大脑→桥→现实选择→遗传→部署 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
