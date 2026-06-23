"""
TL-Ego 效果数据分析 v3 — GBK safe + self-model debug
"""
import json, urllib.request, sys, os, time, statistics
from datetime import datetime

BASE = "http://127.0.0.1:8766"

def api(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type":"application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

# 0. Self-model raw
print("=== /self-model raw ===")
sm = api("GET", "/self-model")
print(json.dumps(sm, indent=2, ensure_ascii=False)[:3000])

# 1. Status
print("\n=== /status ===")
s = api("GET", "/status")
print(json.dumps(s, indent=2, ensure_ascii=False)[:1500])

# 2. 20 contemplations
print("\n=== 20 contemplate runs ===")
results = []
for i in range(20):
    r = api("POST", "/contemplate", {"query":"ego-v3-slot-%d" % i, "depth":1})
    if "error" in r:
        print(f"  [{i}] ERROR: {r['error']}")
        continue
    s2 = r.get("system2", {})
    agg = r.get("aggregator", {})
    agree = agg.get("agreement")
    bar = "Y" if agree else ("N" if agree is False else "?")
    results.append({
        "s2_type": s2.get("type",""),
        "s2_confidence": s2.get("confidence",0),
        "verdict": agg.get("verdict",""),
        "final_confidence": agg.get("final_confidence",0),
        "agree": agree,
    })
    sys.stdout.write(f"\r  [{i:2d}] {bar} s2={s2.get('type','?'):10s} conf={s2.get('confidence',0):.2f} verdict={agg.get('verdict','?'):35s}")
    sys.stdout.flush()

print(f"\nCollected: {len(results)}")

if results:
    agree_c = sum(1 for r in results if r["agree"] is True)
    disagree_c = sum(1 for r in results if r["agree"] is False)
    confs = [r["final_confidence"] for r in results]
    avg_c = sum(confs)/len(confs)
    std_c = statistics.stdev(confs) if len(confs)>1 else 0
    verdicts = {}
    for r in results:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"],0)+1
    s2types = {}
    for r in results:
        s2types[r["s2_type"]] = s2types.get(r["s2_type"],0)+1

    print(f"\n{'='*55}")
    print(f"  TL-Ego 数据分析 v3 (n={len(results)})")
    print(f"{'='*55}")
    print(f"  agree_rate:       {agree_c:3d}/{len(results)} = {agree_c/len(results)*100:.1f}%")
    print(f"  disagree_rate:    {disagree_c:3d}/{len(results)} = {disagree_c/len(results)*100:.1f}%")
    print(f"  avg_confidence:   {avg_c:.4f}")
    print(f"  std_confidence:   {std_c:.4f}")
    print(f"\n  Verdict dist:")
    for v,c in sorted(verdicts.items(), key=lambda x:-x[1]):
        print(f"    {v:45s} {c:3d} ({c/len(results)*100:.1f}%)")
    print(f"\n  S2 type dist:")
    for t,c in sorted(s2types.items(), key=lambda x:-x[1]):
        print(f"    {t:45s} {c:3d} ({c/len(results)*100:.1f}%)")

    fname = "ego_analysis_%s.json" % datetime.now().strftime("%Y%m%d_%H%M")
    with open(fname, "w", encoding="utf-8") as f:
        json.dump({"timestamp":datetime.now().isoformat(),"n":len(results),
                   "feedback_count":0,"fingerprint_count":0,
                   "agree_rate":agree_c/len(results),"disagree_rate":disagree_c/len(results),
                   "avg_confidence":avg_c,"std_confidence":std_c,
                   "verdict_distribution":verdicts,"s2_type_distribution":s2types,"runs":results},
                  f, indent=2, ensure_ascii=False)
    print(f"\n  Saved: {fname}")

print("\n--- done ---")
