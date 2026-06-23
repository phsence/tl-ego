"""Fast test: coordination hint effect on agree rate (timeout-safe)."""
import urllib.request, json, time

queries = [
    "analyze neural activation pattern from last contemplation",
    "check system health and resource usage",
    "what patterns emerge from recent S1/S2 disagreement data",
    "should I run maintenance scripts or continue exploring",
    "log current self-model state for future reference",
    "detect anomalies in resting oscillation trajectory",
    "connect recent STDP changes to feedback outcomes",
    "run supervisor check on all cron services",
    "store current concentration lock count for trending",
    "what are implications of max_bias approaching 1.0",
    "synthesize last 5 contemplations into a single insight",
    "prioritize top 3 actionable items from recent logs",
]

def contemplate(q, retries=2):
    for attempt in range(retries):
        try:
            payload = json.dumps({"query": q, "depth": 1}).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:8766/contemplate",
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return {"error": str(e), "aggregator": {}}

agree = 0
disagree = 0
errors = 0
results = []

import sys
for i, q in enumerate(queries):
    print(f"[{i+1}/{len(queries)}] sending: {q[:60]}...", flush=True)
    r = contemplate(q)
    agg = r.get("aggregator", {})
    s1 = agg.get("s1_type", "?")
    s2 = agg.get("s2_type", "?")
    conf = agg.get("final_confidence", 0)

    if r.get("error"):
        errors += 1
        print(f"[{i+1}/{len(queries)}] ERROR: {r['error'][:60]}")
    else:
        a = s1 == s2
        if a:
            agree += 1
        else:
            disagree += 1
        results.append((s1, s2, conf))
        agree_mark = "AGREE" if a else f"disagree (lock={agg.get('verdict','?')})"
        print(f"[{i+1}/{len(queries)}] S1={s1} S2={s2} {agree_mark} conf={conf:.3f}" if conf else f"[{i+1}/{len(queries)}] S1={s1} S2={s2} {agree_mark}")
    time.sleep(0.3)

total = agree + disagree
print(f"\n=== Results (coordination hint active) ===")
print(f"Total: {total}, Errors: {errors}")
print(f"Agree: {agree}/{total} ({agree*100//max(1,total)}%)")
print(f"Disagree: {disagree}/{total} ({disagree*100//max(1,total)}%)")
if results:
    from collections import Counter
    c = Counter(f"{s1}->{s2}" for (s1, s2, _) in results)
    for pair, count in c.most_common():
        print(f"  {pair}: {count}")
