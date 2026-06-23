#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TL-Ego Phase 1: Contemplation Module (沉思模块)
Takes a query → BGE-M3 embedding → Attractor basin dynamics →
Stability/Competition/Confidence report.

No modification to neural_grid engine — uses public API + manual history capture.
"""

import sys, json, time, requests, random
import numpy as np
from pathlib import Path
from collections import Counter

# ── 1. Patch neural_grid to use 100k backup data ──
SKILL_DIR = Path(r"C:\Users\Administrator\.openclaw\skills\neural_grid")
sys.path.insert(0, str(SKILL_DIR))

DATA_DIR = Path(r"C:\Users\Administrator\.openclaw\workspace\neural_grid_data_100k_backup")

import neural_grid as ng
ng.DATA_DIR = DATA_DIR
ng.NEURON_REGISTRY = DATA_DIR / "neuron_registry.jsonl"
ng.GRID_EDGES_DB = DATA_DIR / "grid_edges.sqlite"
ng.CHECKPOINT_DIR = DATA_DIR / "checkpoints"
ng.EdgeStore._instance = None
ng._edge_store = None

# ── 2. Init grid ──
print("[TL-Ego Phase 1] Initializing Neural Grid (100k backup)...", flush=True)
ng.init_grid(preload_partitions=False)
print(f"[OK] Grid initialized. Neurons: {len(ng._neuron_ids_list)}", flush=True)

# ── 3. BGE-M3 Client ──
BGE_URL = "http://127.0.0.1:8002/v1/embeddings"

def get_embedding(text: str) -> list:
    resp = requests.post(BGE_URL, json={"input": text, "model": "default"}, timeout=30)
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]

# ── 4. Neuron type parser ──
def parse_neuron_type(nid: str) -> str:
    parts = nid.split("-")
    return parts[1] if len(parts) >= 2 else "unknown"

TYPE_ORDER = ["sensory", "integrator", "decider", "action", "memory", "rule_modification", "veto", "doubt", "unknown"]

# ── 5. Contemplation Engine ──
def contemplate(query_embedding: list,
                propagation_rounds: int = 1,
                top_n: int = 50,
                top_k_seeds: int = 3,
                bfs_depth: int = 2) -> dict:
    # 5a. Seed selection with type diversity (2026-06-19: ensure veto/doubt/rule_modification get a chance)
    RARE_TYPES = {"veto", "doubt", "rule_modification"}
    # Fetch more seeds than needed to allow diversity filtering
    raw_seeds = ng.find_seed_neurons(query_embedding, top_k=max(top_k_seeds * 3, 15))
    raw_seed_types = [parse_neuron_type(s) for s in raw_seeds]
    # Build seeds: first pass from top similarity, then inject missing rare types
    seeds = []
    seen_types = set()
    for s, st in zip(raw_seeds, raw_seed_types):
        if st not in seen_types:
            seeds.append(s)
            seen_types.add(st)
        if len(seeds) >= top_k_seeds + len(RARE_TYPES):
            break
    # If still missing rare types, force-inject them from the grid
    missing_rare = RARE_TYPES - seen_types
    if missing_rare and ng._neuron_ids_list:
        for rare_type in missing_rare:
            # Find first neuron of this type from the grid
            for nid in ng._neuron_ids_list:
                if parse_neuron_type(nid) == rare_type:
                    seeds.append(nid)
                    seen_types.add(rare_type)
                    break
    seed_types = [parse_neuron_type(s) for s in seeds]

    # 5b. Candidate expansion
    hop1 = ng.bfs_one_hop(seeds)
    candidate_ids = [cid for cid, _ in hop1]
    if bfs_depth >= 2 and len(candidate_ids) < top_n * 2:
        hop2 = ng.bfs_two_hop(seeds, max_candidates=top_n * 2)
        candidate_ids.extend(hop2)
    candidate_ids = list(set(candidate_ids) - set(seeds))
    if len(candidate_ids) > top_n * 4:
        random.shuffle(candidate_ids)
        candidate_ids = candidate_ids[:top_n * 4]

    # 5c. Load neuron embeddings for candidates
    neurons_map = {}
    if ng._neuron_id_to_index is not None:
        for nid in candidate_ids:
            idx = ng._neuron_id_to_index.get(nid)
            if idx is not None and ng._embeddings_matrix is not None:
                neurons_map[nid] = ng._embeddings_matrix[idx]

    if len(neurons_map) < len(candidate_ids):
        missing = set(candidate_ids) - set(neurons_map.keys())
        with open(ng.NEURON_REGISTRY, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                n = json.loads(line)
                if n['id'] in missing:
                    neurons_map[n['id']] = np.array(n['embedding'], dtype=np.float32)

    # 5d. Initial activation scoring
    query_vec = np.array(query_embedding) / (np.linalg.norm(query_embedding) + 1e-8)
    edge_store = ng.EdgeStore()
    batch_data = edge_store.batch_all_edges(candidate_ids)
    weight_map = {}
    for nid, raw in batch_data.items():
        incoming = raw.get('in', [])
        tw = sum(w for _, w in incoming) if incoming else 0.0
        weight_map[nid] = np.log1p(tw) if tw > 0 else 0.0

    ALPHA = 0.8
    init_acts = []
    for nid in candidate_ids:
        emb = neurons_map.get(nid)
        if emb is None: continue
        sim = float(np.dot(emb / (np.linalg.norm(emb) + 1e-8), query_vec))
        score = sim + ALPHA * weight_map.get(nid, 0.0)
        init_acts.append((nid, score, emb.tolist()))
    init_acts.sort(key=lambda x: x[1], reverse=True)
    init_acts = init_acts[:top_n]

    # 5e. Build local adjacency
    valid_ids = set(candidate_ids)
    mini_adj = {}
    for src in candidate_ids:
        if src in batch_data:
            filtered = [(d, w) for d, w in batch_data[src].get('out', []) if d in valid_ids]
            if filtered:
                mini_adj[src] = filtered

    valid_emb_set = set(neurons_map.keys())
    for src in list(mini_adj.keys()):
        mini_adj[src] = [(d, w) for d, w in mini_adj[src] if d in valid_emb_set]
        if not mini_adj[src]:
            del mini_adj[src]

    # 5f. Run attractor dynamics
    attractor = ng.AttractorNetwork(
        propagation_rounds=propagation_rounds,
        top_k=15,
        inhibition_strength=0.5,
        leak=0.3
    )
    final_state, _ = attractor.step(query_embedding, init_acts, neurons_map, mini_adj)
    history = attractor.history

    # 5g. Type trajectory
    type_trajectory = []
    for rnd, aids in enumerate(history):
        tc = Counter(parse_neuron_type(n) for n in aids)
        type_trajectory.append({
            "round": rnd, "total_activated": len(aids),
            "type_distribution": {t: tc.get(t, 0) for t in TYPE_ORDER},
            "dominant_type": tc.most_common(1)[0][0] if tc else "none"
        })

    # 5h. Metrics from final state
    final_dist = type_trajectory[-1]["type_distribution"]
    total_final = sum(final_dist.values())
    if total_final > 0:
        dominant_type = max(final_dist, key=final_dist.get)
        type_concentration = final_dist[dominant_type] / total_final
        probs = [v / total_final for v in final_dist.values() if v > 0]
        entropy = -sum(p * np.log2(p) for p in probs)
        competition_level = entropy / np.log2(len(TYPE_ORDER))
        active_types = sum(1 for v in final_dist.values() if v > 0)
        if active_types <= 2:  base_stab = 1.0
        elif active_types == 3: base_stab = 0.6
        else: base_stab = 0.3
        basin_stability = max(0.0, min(1.0, base_stab - competition_level * 0.3))
    else:
        type_concentration = 0.0; competition_level = 1.0
        basin_stability = 0.0; dominant_type = "unknown"

    # Round-over-round convergence
    if len(history) >= 3:
        fset = set(history[-1]); pset = set(history[-2])
        change = len(fset - pset) / max(len(fset), 1)
        attractor_convergence = 1.0 - change
    else:
        attractor_convergence = 0.0

    # 5i. Confidence signal (v2: support divergent/emergent signals)
    # High competition + multi-type co-activation is an EMERGENCE signal, not a conflict
    active_type_count = sum(1 for v in final_dist.values() if v > 0)
    if competition_level > 0.75 and active_type_count >= 4 and type_concentration < 0.4:
        # Many types competing equally = divergent attractor = early emergence
        cs = "divergent"
    elif competition_level > 0.75 and type_concentration > 0.5:
        # One type dominating despite competition = genuine conflict
        cs = "conflict"
    elif type_concentration > 0.6 and attractor_convergence > 0.8:
        cs = "high"
    elif type_concentration > 0.4 or attractor_convergence > 0.6:
        cs = "medium"
    else:
        cs = "low"

    # 5i+. Autonomy trigger: if no external query driving this,
    # inject a self-generated "inner state" query every N rounds
    # (implemented in server via /resting endpoint; here we flag it)
    autonomy_flag = {
        "can_generate_inner_state": True,
        "trigger_after_rounds": 3,
        "inner_state_template": "what is my current mental state?"
    }

    # 5j. Summary
    if cs == "high":
        summary = f"Strong {dominant_type} attractor ({type_concentration:.0%} conc, {attractor_convergence:.0%} converge)."
    elif cs == "conflict":
        tops = sorted(final_dist.items(), key=lambda x: x[1], reverse=True)[:3]
        tops_str = ', '.join(f'{t}({c})' for t, c in tops if c > 0)
        summary = f"Multiple basins competing: {tops_str}."
    elif cs == "divergent":
        tops = sorted(final_dist.items(), key=lambda x: x[1], reverse=True)[:4]
        tops_str = ', '.join(f'{t}({c})' for t, c in tops if c > 0)
        summary = f"Divergent attractor — {active_type_count} types co-active ({tops_str}). Possible emergence signal."
    elif cs == "medium":
        summary = f"Moderate {dominant_type} attractor ({type_concentration:.0%} conc, {attractor_convergence:.0%} converge)."
    else:
        n_types = sum(1 for v in final_dist.values() if v > 0)
        summary = f"Weak attractor ({type_concentration:.0%} conc). Spread across {n_types} types."

    return {
        "query_type_hint": Counter(seed_types).most_common(1)[0][0] if seed_types else "unknown",
        "seeds": seeds, "seed_types": seed_types,
        "total_rounds": propagation_rounds,
        "basin_stability": round(basin_stability, 4),
        "type_concentration": round(type_concentration, 4),
        "attractor_convergence": round(attractor_convergence, 4),
        "competition_level": round(competition_level, 4),
        "dominant_type": dominant_type,
        "final_activated_count": len(final_state),
        "type_trajectory": type_trajectory,
        "final_type_distribution": final_dist,
        "confidence_signal": cs,
        "contemplation_summary": summary,
        "autonomy_flag": autonomy_flag,
        "final_state_top10": sorted(final_state.items(), key=lambda x: x[1], reverse=True)[:10],
    }


# ── 6. Test harness ──
if __name__ == "__main__":
    queries = [
        "\u623f\u95f4\u91cc\u80fd\u770b\u5230\u4ec0\u4e48\uff1f",
        "\u8fde\u63a5\u8fd9\u4e24\u7bc7\u8bba\u6587\u7684\u53d1\u73b0",
        "\u4e0b\u4e00\u6b65\u8be5\u505a\u4ec0\u4e48\uff1f",
        "\u542f\u52a8\u670d\u52a1\u5e76\u6253\u5f00\u6d4f\u89c8\u5668",
    ]

    report = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "queries": [], "overall_assessment": None}

    for q in queries:
        print(f"\n{'='*60}", flush=True)
        print(f"[Query] {q}", flush=True)
        print(f"{'='*60}", flush=True)
        t0 = time.time()
        emb = get_embedding(q)
        print(f"[Embedding] {len(emb)}-dim in {time.time()-t0:.2f}s", flush=True)

        t1 = time.time()
        res = contemplate(emb, propagation_rounds=1, top_n=50)
        dt = time.time() - t1
        print(f"[Contemplation] {dt:.2f}s", flush=True)
        print(f"  Seeds:     {res['seeds']}", flush=True)
        print(f"  Stability: {res['basin_stability']:.3f}", flush=True)
        print(f"  Conc:      {res['type_concentration']:.3f}", flush=True)
        print(f"  Converge:  {res['attractor_convergence']:.3f}", flush=True)
        print(f"  Compet:    {res['competition_level']:.3f}", flush=True)
        print(f"  Dominant:  {res['dominant_type']}", flush=True)
        print(f"  Conf:      {res['confidence_signal']}", flush=True)
        print(f"  Summary:   {res['contemplation_summary']}", flush=True)
        print(f"  Final act: {res['final_activated_count']} neurons", flush=True)
        print(f"  Final dist: {res['final_type_distribution']}", flush=True)

        report["queries"].append({
            "query": q,
            "embedding_time_s": round(time.time() - t0 - dt, 3),
            "contemplation_time_s": round(dt, 3),
            "result": {k: res[k] for k in [
                "query_type_hint", "seeds", "basin_stability", "type_concentration",
                "attractor_convergence", "competition_level", "dominant_type",
                "final_activated_count", "confidence_signal", "contemplation_summary",
                "final_type_distribution"
            ]}
        })

    stabs = [e["result"]["basin_stability"] for e in report["queries"]]
    comps = [e["result"]["competition_level"] for e in report["queries"]]
    avg_s, avg_c = sum(stabs) / len(stabs), sum(comps) / len(comps)
    report["overall_assessment"] = {
        "avg_basin_stability": round(avg_s, 4),
        "avg_competition_level": round(avg_c, 4),
        "n_queries": len(queries),
        "interpretation": "Strong attractor network" if avg_s > 0.7 else "Moderate attractor network"
    }

    print(f"\n{'='*60}", flush=True)
    print(f"[TL-Ego Phase 1] Complete", flush=True)
    print(f"  Avg stability:   {avg_s:.3f}", flush=True)
    print(f"  Avg competition: {avg_c:.3f}", flush=True)
    print(f"  Assessment:      {report['overall_assessment']['interpretation']}", flush=True)
    print(f"{'='*60}", flush=True)

    rp = Path(r"C:\Users\Administrator\.openclaw\workspace\reports\tl_ego_phase1_report.json")
    with open(rp, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"[Saved] {rp}", flush=True)
