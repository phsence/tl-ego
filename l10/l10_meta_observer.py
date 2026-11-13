"""l10_meta_observer.py - REBUILT 2026-09-30 (source lost in the 09-30 wipe).

Provenance: tl_ego_server.py:524 does `from l10.l10_meta_observer import analyze as _l10_analyze`
and calls it from three places (resting tick 13703, /resting-log 23532, /l10 23561) as
  analyze(traj, runtime_generation_id=<gen>, analyzed_tick=<tick>)   # traj = trajectory[-50:]
with the result consumed as
  result["profile"]["dead_types"]            (len -> dead_types metric)
  result["analysis_metadata"]                -> RESTING_STATE["l10_analysis_metadata"]
  result["candidates"]                       -> RESTING_STATE["l10_candidates"] (read by
                                                _should_trigger_evolution)
  _write_l10_log(result, tick)               -> .lae/l10_observer.jsonl
The 8327 real rows in .lae/l10_observer.jsonl (last 2026-09-30 11:00:13) pin the exact
output schema and wording, which this build follows verbatim:
  profile keys: n_ticks, dominant{type,count,share}, attractor_lock{type,share,max_run},
    stag_runs[{type,start,end,len}], max_run{...}, active_types, never_dominant_types,
    dead_types, dead_type_semantics ("not_dominant_in_current_window"), trans_gaps,
    top_transitions[{from,to,count}], dwell_reasons, cross_domain_seeds, avg_diversity, phases
  candidates: [{name, evidence, structure}] with names attractor_self_loop / dead_type_zone /
    transition_gap / self_transition_dominance
  analysis_metadata: null in the observed rows (kept None - not invented)
The canonical 19-type universe below is recovered from the union of active_types +
dead_types of that real row.
Judgement (not measured): dwell_reasons / cross_domain_seeds / phases / avg_diversity
formulas are unobservable from the log alone - derived from trajectory entry fields when
present, with documented fallbacks.
"""

import time

TYPE_UNIVERSE = ("action", "aesthetic", "architect", "cultural", "curiosity", "decider", "doubt",
                 "emotional", "ethical", "integrator", "memory", "meta", "planning",
                 "rule_modification", "sensory", "social", "spatial", "temporal", "veto")
DEAD_SEMANTICS = "not_dominant_in_current_window"
STAG_MIN_RUN = 3
TOP_TRANSITIONS = 5


def _dominants(traj):
    out = []
    for e in traj or []:
        if isinstance(e, dict):
            d = e.get("dominant")
        else:
            d = e
        out.append(d if isinstance(d, str) and d else None)
    return out


def _runs(seq):
    """Consecutive equal non-None runs -> [{type,start,end,len}] (indices into the window)."""
    runs = []
    start = 0
    for i in range(1, len(seq) + 1):
        if i == len(seq) or seq[i] != seq[start]:
            if seq[start] is not None:
                runs.append({"type": seq[start], "start": start, "end": i - 1, "len": i - start})
            start = i
    return runs


def analyze(traj=None, runtime_generation_id=None, analyzed_tick=None):
    """Shadow meta-observer: profile a 50-tick trajectory and emit bottleneck candidates."""
    seq = _dominants(traj)
    n = len(seq)
    counts = {}
    for d in seq:
        if d:
            counts[d] = counts.get(d, 0) + 1
    dom_type = max(counts, key=counts.get) if counts else None
    dom_count = counts.get(dom_type, 0)
    share = round(dom_count / n, 4) if n else 0.0
    runs = _runs(seq)
    stag_runs = [r for r in runs if r["len"] >= STAG_MIN_RUN]
    max_run = max(runs, key=lambda r: r["len"]) if runs else None
    active = sorted(counts)
    dead = [t for t in TYPE_UNIVERSE if t not in counts]
    trans = {}
    for a, b in zip(seq, seq[1:]):
        if a and b:
            trans[(a, b)] = trans.get((a, b), 0) + 1
    top_trans = sorted(({"from": a, "to": b, "count": c} for (a, b), c in trans.items()),
                       key=lambda r: -r["count"])[:TOP_TRANSITIONS]
    trans_gaps = [t for t in TYPE_UNIVERSE if not any(to == t for _fr, to in trans)]
    dwell, cd_seeds, phases = {}, 0, {}
    for e in traj or []:
        if not isinstance(e, dict):
            continue
        if e.get("dwell_reason"):
            dwell[str(e["dwell_reason"])] = dwell.get(str(e["dwell_reason"]), 0) + 1
        if e.get("cross_domain_seed") or e.get("cross_domain"):
            cd_seeds += 1
        ph = e.get("phase") or e.get("noise_mode")
        if ph:
            phases[str(ph)] = phases.get(str(ph), 0) + 1
    avg_div = round(len(active) / n, 4) if n else 0.0
    profile = {
        "n_ticks": n,
        "dominant": {"type": dom_type, "count": dom_count, "share": share},
        "attractor_lock": {"type": dom_type, "share": share,
                           "max_run": max_run["len"] if max_run else 0},
        "stag_runs": stag_runs,
        "max_run": max_run or {},
        "active_types": active,
        "never_dominant_types": dead,
        "dead_types": dead,
        "dead_type_semantics": DEAD_SEMANTICS,
        "trans_gaps": trans_gaps,
        "top_transitions": top_trans,
        "dwell_reasons": dwell,
        "cross_domain_seeds": cd_seeds,
        "avg_diversity": avg_div,
        "phases": phases,
    }
    candidates = []
    if dom_type and share >= 0.5:
        candidates.append({
            "name": "attractor_self_loop",
            "evidence": "%s 占 %.1f%% tick, 最大连续 %d tick" % (dom_type, share * 100,
                                                                max_run["len"] if max_run else 0),
            "structure": "%s -> %s 自转移为主, 需蒸馏停滞处理子模式" % (dom_type, dom_type),
        })
    if dead:
        names = ", ".join(dead[:6])
        candidates.append({
            "name": "dead_type_zone",
            "evidence": "%d 个类型当前窗口未主导: %s" % (len(dead), names),
            "structure": "仅表示当前窗口的主导覆盖缺口，不表示这些能力不存在",
        })
    if trans_gaps:
        names = ", ".join(trans_gaps[:6])
        candidates.append({
            "name": "transition_gap",
            "evidence": "%d 个类型从未成为转移目标: %s" % (len(trans_gaps), names),
            "structure": "转移矩阵列缺失 = 无法到达的认知状态, 候选: 桥接算子",
        })
    if top_trans and top_trans[0]["from"] == top_trans[0]["to"]:
        t0 = top_trans[0]
        candidates.append({
            "name": "self_transition_dominance",
            "evidence": "%s->%s 自转移 %d 次为最高频转移" % (t0["from"], t0["to"], t0["count"]),
            "structure": "单点吸引子吸收态, 候选: 注入噪声/反Hebbian 逃逸算子",
        })
    return {
        "profile": profile,
        "candidates": candidates,
        "analysis_metadata": None,   # real rows carry null here - kept as observed
        "runtime_generation_id": runtime_generation_id,
        "analyzed_tick": analyzed_tick,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


if __name__ == "__main__":
    # synthetic window in the shape of the real rows: action dominant with two 5-runs
    traj = [{"tick": 100 + i, "dominant": ("action" if i in (7, 8, 9, 10, 11, 20, 21, 22, 23, 24)
                                            else ["doubt", "integrator", "memory", "planning", "cultural",
                                                  "decider"][i % 6]),
             "dwell_reason": "hold" if i % 3 else None} for i in range(50)]
    r = analyze(traj, runtime_generation_id="ego-test", analyzed_tick=150)
    p = r["profile"]
    assert p["n_ticks"] == 50 and p["dominant"]["type"] == "action", p["dominant"]
    assert p["dominant"]["count"] == 10 and p["dominant"]["share"] == 0.2
    assert {"type": "action", "start": 7, "end": 11, "len": 5} in p["stag_runs"], p["stag_runs"]
    assert p["max_run"]["len"] == 5 and p["attractor_lock"]["max_run"] == 5
    assert len(p["active_types"]) == 7 and len(p["dead_types"]) == 12
    assert set(p["active_types"]) | set(p["dead_types"]) == set(TYPE_UNIVERSE)
    assert p["dead_type_semantics"] == DEAD_SEMANTICS
    assert p["top_transitions"][0]["count"] >= 1 and all("from" in t and "to" in t for t in p["top_transitions"])
    assert p["dwell_reasons"].get("hold") and p["phases"] == {}
    names = [c["name"] for c in r["candidates"]]
    assert "dead_type_zone" in names and "transition_gap" in names, names
    assert "attractor_self_loop" not in names  # share 0.2 < 0.5
    assert r["analysis_metadata"] is None and r["analyzed_tick"] == 150
    # lock case: 60% action -> attractor_self_loop + self_transition_dominance fire
    traj2 = [{"dominant": ("action" if i % 10 < 6 else "doubt")} for i in range(50)]
    r2 = analyze(traj2)
    n2 = [c["name"] for c in r2["candidates"]]
    assert r2["profile"]["dominant"]["share"] == 0.6, r2["profile"]["dominant"]
    assert "attractor_self_loop" in n2 and any("占 60.0% tick" in c["evidence"] for c in r2["candidates"]), n2
    # empty / junk input must not explode
    e = analyze([])
    assert e["profile"]["n_ticks"] == 0
    assert [c["name"] for c in e["candidates"]] == ["dead_type_zone", "transition_gap"], e["candidates"]  # empty window = 19 gaps
    assert analyze([None, "x", 3, {}])["profile"]["n_ticks"] == 4
    print("l10_meta_observer selfcheck OK | candidates(lock-case)=%s" % n2)
