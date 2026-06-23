#!/usr/bin/env python3
"""Quick test of route_with_attractor with large BFS."""
import time, sys, json, requests
from pathlib import Path

SKILL_DIR = Path(r"C:\Users\Administrator\.openclaw\skills\neural_grid")
sys.path.insert(0, str(SKILL_DIR))

DATA_DIR = Path(r"C:\Users\Administrator\.openclaw\workspace\neural_grid_data_100k_backup")
import neural_grid as ng
ng.DATA_DIR = DATA_DIR
ng.NEURON_REGISTRY = DATA_DIR / "neuron_registry.jsonl"
ng.GRID_EDGES_DB = DATA_DIR / "grid_edges.sqlite"
ng.EdgeStore._instance = None
ng._edge_store = None

print("[Init] Loading grid...", flush=True)
t0 = time.time()
ng.init_grid(preload_partitions=False)
print(f"[Init] Done in {time.time()-t0:.1f}s", flush=True)

# Embedding
resp = requests.post("http://127.0.0.1:8002/v1/embeddings",
    json={"input": "下一步该做什么？", "model": "default"}, timeout=30)
emb = resp.json()["data"][0]["embedding"]

# Route with attractor - large BFS
t0 = time.time()
res = ng.route_with_attractor(emb, top_n=50, propagation_rounds=10,
    max_bfs_candidates=5000, bfs_depth=2, return_details=True)
dt = time.time() - t0
print(f"\n[Results] Time: {dt:.1f}s")
print(f"  Seeds: {res['seeds']}")
print(f"  Initial candidates: {len(res.get('initial_candidates', []))}")
print(f"  Activated: {res['activated_count']} / initial {res['initial_count']}")
print(f"  Final state size: {len(res['final_state'])}")
final_top = sorted(res['final_state'].items(), key=lambda x: x[1], reverse=True)[:5]
for nid, act in final_top:
    print(f"    {nid}: {act:.4f}")
print("[Done]", flush=True)
