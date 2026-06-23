import requests, json

queries = ["room observation", "connect two papers", "what to do next", "start service and open browser"]

for i, q in enumerate(queries):
    r = requests.post("http://127.0.0.1:8766/contemplate", json={"query": q}, timeout=60)
    d = r.json()
    s1 = d["system1"]
    s2 = d["system2"]
    agg = d["aggregator"]
    print(
        f"[{i+1}] {q:40s}"
        f"  S1={s1['dominant_type']:10s}(st={s1['stability']:.2f},c={s1['concentration']:.2f})"
        f"  S2={s2['type']:10s}(cf={s2['confidence']:.2f})"
        f"  agree={agg['agreement']}  ver={agg['verdict']:28s}"
        f"  fconf={agg['final_confidence']:.3f}  t={d['total_s']:.1f}s"
    )
