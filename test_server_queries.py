import requests, json

queries = [
    "房间里能看到什么？",
    "连接这两篇论文的发现",
    "下一步该做什么？",
    "启动服务并打开浏览器",
]

for i, q in enumerate(queries):
    r = requests.post("http://127.0.0.1:8766/contemplate", json={"query": q}, timeout=30)
    d = r.json()
    s1 = d["system1"]
    s2 = d["system2"]
    agg = d["aggregator"]
    print(
        f"[{i+1}] {q:12s}"
        f"  S1={s1['dominant_type']:10s}(st={s1['stability']:.2f},c={s1['concentration']:.2f})"
        f"  S2={s2['type']:10s}(cf={s2['confidence']:.2f})"
        f"  agree={agg['agreement']}  ver={agg['verdict']:28s}"
        f"  fconf={agg['final_confidence']:.3f}  t={d['total_s']:.2f}s"
    )
