"""ego_growth_daily.py — daily, auditable growth measurement for TL-Ego.

One number per day, with the provenance needed to not lie about it:

  * scores the FROZEN baseline and the DEPLOYED champion on the current
    frozen hidden suite (hash-verified) plus back-tests on earlier suites;
  * reads the day's own ledger (L10->gate candidates, QD archive, episodes,
    JEPA calibration);
  * appends one JSON line to .lae/growth/growth_ledger.jsonl and writes
    .lae/growth/YYYY-MM-DD.json.

Honesty rules baked in (each one is a past failure here):
  * every score carries suite_version + sha256 of the challenges file;
  * a day whose champion params changed is flagged champion_params_changed
    and never silently chained to the previous day's delta;
  * suite integrity failure / unresolvable champion -> verdict
    "not_measurable", never a score;
  * verdict is a single-run selector score on a frozen suite. It says
    nothing about broad generalisation; the five-arm MC harness owns that
    claim and it is currently all hold/rollback.

Usage:  python ego_growth_daily.py [--date YYYY-MM-DD] [--quiet]
"""

from __future__ import annotations

import hashlib
import importlib
import json
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
WORKSPACE = PROJECT_DIR.parent.parent
LAE = WORKSPACE / ".lae"
SUITE_DIR = PROJECT_DIR / "capability_suites" / "hidden"
OUT_DIR = LAE / "growth"
LEDGER = OUT_DIR / "growth_ledger.jsonl"

# frozen baseline: pre-v3-trap primitives, pinned in run_evolution_round_r3
BASELINE_BLUEPRINT = {
    "policy_name": "baseline_champion_r2",
    "strategy_type": "kwta_threshold_expansion",
    "params": {"detectors": ["fib"]},
}
NOISE_FLOOR = 0.02  # below this a day-over-day delta is selector noise, not growth


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _load(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _round_state() -> dict:
    return _load(LAE / "evolution_round_state.json", {})


def _round_saturation() -> dict:
    """The bridge's own 'no headroom left' flag, if it wrote one."""
    b = _load(LAE / "l10_gate_policies_r3.json", {})
    sat = b.get("saturation")
    return sat if isinstance(sat, dict) else {}


def _resolve_champion() -> tuple[dict | None, str]:
    """Champion = round state's next_baseline + its params from the current
    bridge json. Returns (blueprint, reason); blueprint is None when the name
    cannot be resolved to a parameter set.

    Fallback: when the bridge skipped the round because the QD archive is
    saturated (blueprints=[]), the deployed champion is still the round
    state's next_baseline — its params live in the results[] entry it won
    with. Without this, a saturated archive reads as 'not_measurable'
    forever and the daily ledger goes dark."""
    rs = _round_state()
    name = rs.get("next_baseline")
    if not name:
        return None, "round_state has no next_baseline"
    bridge = _load(LAE / "l10_gate_policies_r3.json", {})
    for bp in bridge.get("blueprints", []):
        if bp.get("policy_name") == name:
            return bp, ""
    # saturated-round fallback: rebuild the champion from the winning result
    sat = bridge.get("saturation") or {}
    for res in rs.get("results", []):
        if res.get("policy") == name and res.get("verdict") == "retained":
            strat = name.rsplit("_r", 1)[0].replace("l10_", "", 1)
            params = ({"detectors": _deployed_detectors()} if strat == "kwta_threshold_expansion"
                      else {})
            return {"policy_name": name, "strategy_type": strat, "params": params,
                    "_fallback": "saturated_round_results"}, ""
    if sat.get("saturated"):
        return None, (f"bridge skipped round: {bridge.get('reason') or 'QD archive saturated'}; "
                      f"{name!r} not retained in round_state results")
    return None, f"next_baseline {name!r} not in current bridge blueprints"


def _deployed_detectors() -> list[str]:
    """Detector pool the bridge actually deployed (the v6 widening).

    Read from the bridge source instead of copied: the pool is edited there
    whenever the suite traps a new family, and a stale copy here would score
    the champion against a pool it never had."""
    import re
    src = PROJECT_DIR / "l10_gate_policy_bridge.py"
    m = re.search(r"POOL\s*=\s*\[(.*?)\]", src.read_text(encoding="utf-8"), re.S)
    if not m:
        return ["fib"]
    return re.findall(r'"([^"]+)"', m.group(1))


def _score(mod, blueprint: dict, suite_version: str) -> dict:
    """Score one policy on one suite version. Never raises."""
    challenges = SUITE_DIR / "assets" / f"challenges_{suite_version}.json"
    if not challenges.exists():
        return {"suite": suite_version, "measured": False,
                "reason": f"challenges_{suite_version}.json missing"}
    manifest = SUITE_DIR / f"manifest_{suite_version}.json"
    integrity = None
    if manifest.exists():
        try:
            from capability_task_protocol import validate_manifest
            validate_manifest(manifest, SUITE_DIR)
            integrity = True
        except Exception as exc:  # integrity failure is a hard stop, not a score
            return {"suite": suite_version, "measured": False,
                    "reason": f"manifest integrity failed: {exc}"}
    mod.CHALLENGES = challenges
    try:
        micro, detail = mod.micro_score(mod.make_policy(
            blueprint.get("policy_name", "champion"), blueprint))
    except Exception as exc:
        return {"suite": suite_version, "measured": False, "reason": f"score error: {exc}"}
    return {"suite": suite_version, "measured": True,
            "suite_integrity": integrity,
            "challenges_sha256": _sha256(challenges),
            "micro": micro, "detail": detail}


def _todays_candidates(day: str) -> dict:
    """L10->gate candidates registered on `day`, straight from the registry."""
    import sqlite3
    db = LAE / "candidate_registry.sqlite"
    out = {"n": 0, "by_suite": {}, "best_micro": None, "states": {}}
    if not db.exists():
        out["reason"] = "candidate_registry.sqlite missing"
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "select created_at, proposal_json, state from candidate_registry "
            "where mutation_operator='l10_brain_bridge_r3'").fetchall()
        con.close()
    except Exception as exc:
        out["reason"] = f"registry read failed: {exc}"
        return out
    start = datetime.strptime(day, "%Y-%m-%d")
    end = start + timedelta(days=1)
    for created, pj, state in rows:
        try:
            when = datetime.fromtimestamp(created)
        except Exception:
            continue
        if not (start <= when < end):
            continue
        try:
            p = json.loads(pj)
        except Exception:
            continue
        out["n"] += 1
        suite = p.get("suite_version", "?")
        out["by_suite"].setdefault(suite, []).append(p.get("micro_accuracy"))
        out["states"][state] = out["states"].get(state, 0) + 1
    for suite, micros in out["by_suite"].items():
        vals = [m for m in micros if m is not None]
        if vals:
            out["by_suite"][suite] = {"n": len(vals),
                                      "best": max(vals),
                                      "mean": round(statistics.mean(vals), 6)}
    best = [v["best"] for v in out["by_suite"].values() if isinstance(v, dict)]
    out["best_micro"] = max(best) if best else None
    return out


def _qd_best() -> dict:
    import sqlite3
    db = LAE / "qd_archive" / "qd_archive.sqlite"
    if not db.exists():
        return {"reason": "qd_archive.sqlite missing"}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "select niche, fitness, added_at from archive order by added_at").fetchall()
        con.close()
    except Exception as exc:
        return {"reason": f"qd read failed: {exc}"}
    if not rows:
        return {"niches": 0, "best_fitness": None, "last_added": None}
    return {"niches": len({r[0] for r in rows}),
            "best_fitness": max(r[1] for r in rows),
            "last_added": rows[-1][2]}


def _episodes_today(day: str) -> int:
    path = LAE / "causal_episodes.jsonl"
    if not path.exists():
        return -1
    needle = f"ep-{day.replace('-', '')}"  # episode_id: ep-YYYYMMDD-NNN
    n = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if needle in line:
                n += 1
    return n


def _jepa() -> dict:
    d = _load(LAE / "jepa_calibration.json", {})
    hist = d.get("history") or []
    if not hist:
        return {"reason": "no calibration history"}
    pes = [h.get("pe_before") for h in hist if h.get("pe_before") is not None]
    return {"calibration_factor": d.get("calibration_factor"),
            "ema_alpha": d.get("ema_alpha"),
            "n_history": len(hist),
            "pe_mean_last_10": round(statistics.mean(pes[-10:]), 4) if pes else None,
            "pe_mean_first_10": round(statistics.mean(pes[:10]), 4) if pes else None,
            "last_tick": hist[-1].get("tick")}


def _previous_record(day: str) -> dict | None:
    """Yesterday's ledger line, if any."""
    prev = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    p = OUT_DIR / f"{prev}.json"
    return _load(p, None)


def main() -> int:
    day = datetime.now().strftime("%Y-%m-%d")
    quiet = False
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--date" and i + 1 < len(argv):
            day = argv[i + 1]
        elif a == "--quiet":
            quiet = True

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(PROJECT_DIR))
    mod = importlib.import_module("run_evolution_round_r3")

    champion_bp, champ_reason = _resolve_champion()
    rs = _round_state()
    # v6 = v5 verbatim + traps on the post-v5 champion's arithmetic weaknesses
    # (n^5+ powers, geometric differences, affine recurrence, primes) plus
    # controls. The suite advanced 2026-09-22; scores from v5 and earlier are a
    # different ruler and must never be compared against v6 — the runner's
    # task_domain/suite_version literals and qd_saturation()'s suite_prefix are
    # separate copies of this version, keep them in lockstep.
    current_suite = "v7"  # bump when the frozen suite advances; never compare across

    record = {
        "date": day,
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "current_suite": current_suite,
        "evolution_round": {
            "round": rs.get("round"), "gate": rs.get("gate"),
            "baseline": rs.get("baseline"),
            "baseline_micro": rs.get("baseline_micro"),
            "next_baseline": rs.get("next_baseline"),
            "min_gain": rs.get("min_gain"),
            "generated_at": rs.get("generated_at"),
        },
        "auto_round": _load(LAE / "evo_auto_round_state.json", {}),
        # the offline line's own 'no room left' flag — the honest reason a
        # delta may be absent, as opposed to 'we failed to measure'
        "qd_saturation": _round_saturation(),
    }

    # --- the one comparable number: frozen baseline vs deployed champion ---
    base_cur = _score(mod, BASELINE_BLUEPRINT, current_suite)
    champ_cur = _score(mod, champion_bp, current_suite) if champion_bp else \
        {"suite": current_suite, "measured": False, "reason": champ_reason}
    record["current_suite_scores"] = {"baseline": base_cur, "champion": champ_cur}

    # --- back-test on earlier suites (marked; NOT chained into a trend) ---
    backtests = {}
    for v in ("v3", "v4"):
        backtests[v] = {
            "baseline": _score(mod, BASELINE_BLUEPRINT, v),
            "champion": _score(mod, champion_bp, v) if champion_bp else
                        {"suite": v, "measured": False, "reason": champ_reason},
        }
    record["backtest_suites"] = backtests

    # --- the day's own ledger ---
    record["ledger"] = {
        "l10_gate_candidates_today": _todays_candidates(day),
        "qd_archive": _qd_best(),
        "episodes_today": _episodes_today(day),
        "jepa_calibration": _jepa(),
        "ego_state": _load(LAE / "ego_state_snapshot.json", {}),
    }

    # --- verdict ---
    prev = _previous_record(day)
    record["previous_day"] = {"date": prev.get("date") if prev else None}

    ok = (base_cur.get("measured") and champ_cur.get("measured"))
    if not ok:
        record["verdict"] = "not_measurable"
        record["verdict_reason"] = (
            f"baseline={base_cur.get('reason', 'ok')} champion={champ_cur.get('reason', 'ok')}")
        record["champion_micro_delta_vs_prev_day"] = None
    else:
        b, c = base_cur["micro"], champ_cur["micro"]
        champ_params = json.dumps(champion_bp.get("params", {}), sort_keys=True)
        prev_params = None
        prev_micro = None
        if prev:
            pc = (prev.get("current_suite_scores") or {}).get("champion") or {}
            prev_micro = pc.get("micro")
            prev_params = prev.get("champion_params")
            # a suite bump is a different ruler: v5 0.75 -> v6 0.75 is not growth
            if prev.get("current_suite") and prev["current_suite"] != current_suite:
                prev_micro = None
                prev_params = None
        params_changed = (prev_params is not None and prev_params != champ_params)
        delta = None if (prev_micro is None or params_changed) else round(c - prev_micro, 6)
        record["champion_params"] = champ_params
        record["champion_micro_delta_vs_prev_day"] = delta
        record["champion_params_changed"] = params_changed
        record["champion_minus_baseline"] = round(c - b, 6)

        sat = bool((record.get("qd_saturation") or {}).get("saturated"))
        if prev is None:
            record["verdict"] = "baseline_day"
            record["verdict_reason"] = "first record; no previous day to compare"
        elif sat and c >= 1.0 - NOISE_FLOOR:
            # saturated archive + a maxed champion: headroom is gone whatever
            # yesterday said, so this must be judged before the comparability
            # branches (delta may be None and still the answer is 'no_headroom')
            record["verdict"] = "no_headroom"
            record["verdict_reason"] = (
                "QD archive saturated on this suite (best=1.0) and champion "
                f"{c:.4f} is at ceiling — the offline line has nothing left "
                "to score; advance the suite or move evolution pressure to "
                "the embodied line")
        elif params_changed:
            record["verdict"] = "not_comparable"
            record["verdict_reason"] = (
                "champion params changed since previous day — a delta here would "
                "measure the parameter swap, not growth")
        elif delta is None:
            record["verdict"] = "not_comparable"
            record["verdict_reason"] = "previous day has no comparable champion score"
        elif abs(delta) < NOISE_FLOOR:
            record["verdict"] = "flat"
            record["verdict_reason"] = (
                f"delta {delta:+.4f} is inside the ±{NOISE_FLOOR} selector noise floor")
        elif delta > 0:
            record["verdict"] = "improving"
            record["verdict_reason"] = f"champion micro {prev_micro} -> {c}"
        else:
            record["verdict"] = "regressing"
            record["verdict_reason"] = f"champion micro {prev_micro} -> {c}"

    # --- persist ---
    day_file = OUT_DIR / f"{day}.json"
    day_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    if not quiet:
        c = record["current_suite_scores"]["champion"]
        b = record["current_suite_scores"]["baseline"]
        led = record["ledger"]
        print(f"[growth] {day}  verdict={record['verdict']}  ({record['verdict_reason']})")
        print(f"[growth] suite={current_suite}  baseline={b.get('micro')}  "
              f"champion={c.get('micro')}  champion-baseline={record.get('champion_minus_baseline')}")
        print(f"[growth] delta_vs_prev={record.get('champion_micro_delta_vs_prev_day')}  "
              f"params_changed={record.get('champion_params_changed')}")
        print(f"[growth] today: candidates={led['l10_gate_candidates_today'].get('n')} "
              f"best_micro={led['l10_gate_candidates_today'].get('best_micro')} "
              f"episodes={led['episodes_today']} "
              f"qd_best={led['qd_archive'].get('best_fitness')}")
        print(f"[growth] jepa pe_mean(last10)={led['jepa_calibration'].get('pe_mean_last_10')} "
              f"calib={led['jepa_calibration'].get('calibration_factor')}")
        print(f"[growth] wrote {day_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
